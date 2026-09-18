#!/usr/bin/env python3
"""
azure/trigger/status_app.py — a small page showing whether the clock is working.

Why this exists
---------------
Splitting the schedule out of GitHub removes GitHub's 60%-drop problem, but it
also means the Azure trigger job becomes the single source of truth for "did
the pipeline get asked to run?". If that job stops firing, nothing else
notices: the site simply stops updating.

This page answers that question directly, from the two authoritative sources:

  * Azure job executions (via the ARM API) — did the clock actually fire?
  * GitHub workflow runs (via the public API) — did the pipeline actually run?

Reading them side by side is the point. An Azure execution with no matching
GitHub run means the dispatch was accepted but GitHub dropped it. A gap in the
Azure column means the clock itself missed. Either is invisible if you only
look at one of them.

It also prints the measured inter-execution gaps against the 2h design
interval, because "is the schedule actually keeping up?" is the one number
worth staring at — and it is the number GitHub's own cron could not hold.

Design notes
------------
* Standard library only, same as trigger.py, so it ships in the same image and
  only one package has to be public.
* The ARM call authenticates with the container app's **system-assigned
  managed identity**; there is no credential in this container at all.
* Unauthenticated GitHub reads are capped at 60/hour per source IP, and Azure
  egress IPs are shared. Responses are cached and a rate limit degrades to a
  message instead of a broken page.
* Min replicas is 0: the app scales away when nobody is looking, so it costs
  nothing to leave running.

Environment
-----------
    SUBSCRIPTION_ID   required for the Azure table
    RESOURCE_GROUP    default polar-plus-rg
    JOB_NAME          default polar-trigger
    GH_REPO           default unknown70022024/polar_plus
    KEEP              default 12  (how many rows to show)
    PORT              default 8080
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARM = "https://management.azure.com"
GH = "https://api.github.com"
ARM_API = "2024-03-01"
CACHE_TTL_S = 60

SUBSCRIPTION_ID = os.environ.get("SUBSCRIPTION_ID", "").strip()
RESOURCE_GROUP = os.environ.get("RESOURCE_GROUP", "polar-plus-rg").strip()
JOB_NAME = os.environ.get("JOB_NAME", "polar-trigger").strip()
GH_REPO = os.environ.get("GH_REPO", "unknown70022024/polar_plus").strip()
KEEP = int(os.environ.get("KEEP", "12"))
PORT = int(os.environ.get("PORT", "8080"))

# The design interval, in hours, that reliability is judged against.
DESIGN_INTERVAL_H = 2.0

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def cached(key: str, producer):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    value = producer()
    with _lock:
        _cache[key] = (now, value)
    return value


# ---------------------------------------------------------------- azure ---

def managed_identity_token(resource: str) -> str:
    """Get an AAD token for this container app's system-assigned identity.

    Container Apps injects IDENTITY_ENDPOINT / IDENTITY_HEADER. IMDS is the
    fallback for environments that only expose the instance metadata service.
    """
    endpoint = os.environ.get("IDENTITY_ENDPOINT")
    header = os.environ.get("IDENTITY_HEADER")
    if endpoint and header:
        url = f"{endpoint}?resource={resource}&api-version=2019-08-01"
        request = urllib.request.Request(url, headers={"X-IDENTITY-HEADER": header})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)["access_token"]

    url = ("http://169.254.169.254/metadata/identity/oauth2/token"
           f"?api-version=2018-02-01&resource={resource}")
    request = urllib.request.Request(url, headers={"Metadata": "true"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["access_token"]


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def problem(source: str, message: str) -> dict:
    """Log a failed fetch and return it for inline display on the page.

    The page shows the error to whoever opens it, but nobody is watching a
    status page continuously — the log line is what makes a broken ARM
    permission or a GitHub rate limit visible after the fact.
    """
    print(f"[status] {source} FAILED: {message}", flush=True)
    return {"error": message}


def fetch_executions() -> dict:
    """Last KEEP executions of the job, newest first."""
    if not SUBSCRIPTION_ID:
        return problem("azure", "SUBSCRIPTION_ID is not set")
    url = (f"{ARM}/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
           f"/providers/Microsoft.App/jobs/{JOB_NAME}/executions?api-version={ARM_API}")
    try:
        token = managed_identity_token(ARM)
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode("utf-8", "replace").strip()
        return problem("azure", f"ARM HTTP {exc.code}: {detail}")
    except Exception as exc:  # noqa: BLE001
        return problem("azure", f"{type(exc).__name__}: {exc}")

    rows = []
    for item in payload.get("value", []):
        props = item.get("properties", {}) or {}
        start, end = parse_ts(props.get("startTime")), parse_ts(props.get("endTime"))
        rows.append({
            "name": item.get("name", "?"),
            "status": props.get("status", "?"),
            "start": props.get("startTime"),
            "end": props.get("endTime"),
            "seconds": round((end - start).total_seconds()) if start and end else None,
            "_start": start,
        })
    rows.sort(key=lambda r: r["_start"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
              reverse=True)
    rows = rows[:KEEP]

    # Gaps between consecutive fires, oldest -> newest. This is the number that
    # matters: GitHub's cron could not hold 2h (measured median 4.57h, worst
    # 7.90h), so it is worth seeing the same statistic for Azure.
    timed = sorted([r for r in rows if r["_start"]], key=lambda r: r["_start"])
    gaps = [round((b["_start"] - a["_start"]).total_seconds() / 3600, 2)
            for a, b in zip(timed, timed[1:])]
    for row in rows:
        row.pop("_start", None)

    stats = {}
    if gaps:
        stats = {
            "n": len(gaps),
            "mean": round(sum(gaps) / len(gaps), 2),
            "median": sorted(gaps)[len(gaps) // 2],
            "max": max(gaps),
            "design": DESIGN_INTERVAL_H,
        }
    print(f"[status] azure ok: {len(rows)} executions, "
          f"{len(gaps)} gaps, max={max(gaps) if gaps else '-'}h", flush=True)
    return {"rows": rows, "gaps": gaps, "stats": stats}


# --------------------------------------------------------------- github ---

def fetch_runs() -> dict:
    url = f"{GH}/repos/{GH_REPO}/actions/runs?per_page={KEEP}"
    try:
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "polar-plus-status",
        })
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        note = "rate limited" if exc.code == 403 else f"HTTP {exc.code}"
        return problem("github", f"{note} (unauthenticated GitHub reads are 60/hour per IP)")
    except Exception as exc:  # noqa: BLE001
        return problem("github", f"{type(exc).__name__}: {exc}")

    rows = []
    for run in payload.get("workflow_runs", [])[:KEEP]:
        start, end = parse_ts(run.get("run_started_at")), parse_ts(run.get("updated_at"))
        rows.append({
            "number": run.get("run_number"),
            "event": run.get("event"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "created": run.get("created_at"),
            "seconds": round((end - start).total_seconds()) if start and end else None,
            "url": run.get("html_url"),
        })
    print(f"[status] github ok: {len(rows)} runs", flush=True)
    return {"rows": rows}


# ----------------------------------------------------------------- html ---

CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       margin: 0 auto; padding: 24px; max-width: 1100px; }
h1 { font-size: 18px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 28px 0 8px; }
.sub { opacity: .65; margin-bottom: 20px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 5px 10px; border-bottom: 1px solid rgba(128,128,128,.28); }
th { font-weight: 600; opacity: .7; }
td.num { text-align: right; }
.ok { color: #1a7f37; } .bad { color: #cf222e; } .warn { color: #9a6700; }
.err { background: rgba(207,34,46,.10); border-left: 3px solid #cf222e;
       padding: 8px 12px; margin: 8px 0; }
.card { border: 1px solid rgba(128,128,128,.28); border-radius: 6px;
        padding: 12px 16px; margin: 8px 0; }
.note { opacity: .7; font-size: 13px; }
"""


def verdict_class(text: str) -> str:
    t = (text or "").lower()
    if t in ("succeeded", "success"):
        return "ok"
    if t in ("failed", "failure", "cancelled", "timedout"):
        return "bad"
    return "warn"


def fmt_local_utc(value: str | None) -> str:
    ts = parse_ts(value)
    return ts.strftime("%m-%d %H:%M:%S") if ts else (value or "-")


def render_stats(stats: dict) -> str:
    if not stats:
        return '<p class="note">还没有足够的历史来计算间隔。</p>'
    over = stats["max"] > stats["design"]
    cls = "bad" if over else "ok"
    return (
        '<div class="card">'
        f'最近 {stats["n"]} 个间隔：'
        f'中位数 <b>{stats["median"]}h</b> · '
        f'均值 <b>{stats["mean"]}h</b> · '
        f'最大 <b class="{cls}">{stats["max"]}h</b>'
        f'（设计 {stats["design"]}h）'
        '<div class="note">对照：GitHub 自身 cron 实测中位数 4.57h、最大 7.90h，'
        '且约 60% 的调度被静默丢弃。</div></div>'
    )


def render_page(azure: dict, github: dict) -> str:
    parts = [
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<meta http-equiv='refresh' content='60'>",
        f"<title>polar_plus 触发记录</title><style>{CSS}</style></head><body>",
        "<h1>polar_plus 触发记录</h1>",
        f"<div class='sub'>{html.escape(GH_REPO)} · "
        f"Azure job <code>{html.escape(JOB_NAME)}</code> · 每 60 秒自动刷新 · "
        f"生成于 {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}</div>",
    ]

    parts.append("<h2>一、Azure 时钟（ARM 执行记录）</h2>")
    if "error" in azure:
        parts.append(f"<div class='err'>{html.escape(azure['error'])}</div>")
    else:
        parts.append(render_stats(azure.get("stats") or {}))
        parts.append("<table><tr><th>执行</th><th>状态</th><th>开始 (UTC)</th>"
                     "<th class='num'>耗时</th></tr>")
        for row in azure.get("rows", []):
            cls = verdict_class(row["status"])
            secs = f"{row['seconds']}s" if row["seconds"] is not None else "-"
            parts.append(
                f"<tr><td>{html.escape(str(row['name']))}</td>"
                f"<td class='{cls}'>{html.escape(str(row['status']))}</td>"
                f"<td>{fmt_local_utc(row['start'])}</td>"
                f"<td class='num'>{secs}</td></tr>")
        parts.append("</table>")

    parts.append("<h2>二、GitHub 管线运行（公开 API）</h2>")
    if "error" in github:
        parts.append(f"<div class='err'>{html.escape(github['error'])}</div>")
    else:
        parts.append("<table><tr><th>#</th><th>触发方式</th><th>结论</th>"
                     "<th>创建 (UTC)</th><th class='num'>耗时</th></tr>")
        for row in github.get("rows", []):
            concl = row["conclusion"] or row["status"]
            cls = verdict_class(concl)
            secs = f"{row['seconds']}s" if row["seconds"] is not None else "-"
            link = row["url"] or "#"
            parts.append(
                f"<tr><td><a href='{html.escape(link)}'>{row['number']}</a></td>"
                f"<td>{html.escape(str(row['event']))}</td>"
                f"<td class='{cls}'>{html.escape(str(concl))}</td>"
                f"<td>{fmt_local_utc(row['created'])}</td>"
                f"<td class='num'>{secs}</td></tr>")
        parts.append("</table>")

    parts.append(
        "<h2>三、怎么读这一页</h2><div class='card'>"
        "<p><b>Azure 有记录、GitHub 没有对应 run</b> —— 派发被接受了但 GitHub 没跑起来。</p>"
        "<p><b>Azure 那一列有缺口</b> —— 时钟本身漏了，需要查 Azure。</p>"
        "<p><b>GitHub 显示 failure</b> —— 只有非 0 退出码才会红；"
        "「没有更新的健康文件」现在是绿色跳过，不是故障。</p>"
        "<p class='note'>注意 GitHub 那一列主要被 Azure 触发（<code>workflow_dispatch</code>），"
        "手工触发也会混进来。Azure 那一列同理——<code>az containerapp job start</code> "
        "产生的手工执行也会计入，短期内会把间隔中位数拉低；等 12 条定时执行攒满，"
        "2 小时的节拍才是主导。</p></div>"
        "</body></html>",
    )
    return "".join(parts)


# --------------------------------------------------------------- server ---

class Handler(BaseHTTPRequestHandler):
    server_version = "polar-status"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return

        azure = cached("azure", fetch_executions)
        github = cached("github", fetch_runs)

        if path == "/data.json":
            body = json.dumps({"azure": azure, "github": github},
                              ensure_ascii=False, indent=2).encode()
            self._send(200, body, "application/json; charset=utf-8")
            return

        if path not in ("/", "/index.html"):
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return

        self._send(200, render_page(azure, github).encode(),
                   "text/html; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        print(f"[status] {self.address_string()} {fmt % args}", flush=True)


def selftest() -> int:
    """Render both the happy and the degraded path without any network.

    Worth having: render_page() is the only place that can take the whole page
    down, and a TypeError in it (an append() given two arguments, say) is
    invisible until someone opens the URL.
    """
    azure = {
        "rows": [{"name": "polar-trigger-abc", "status": "Succeeded",
                  "start": "2026-09-18T07:55:18+00:00",
                  "end": "2026-09-18T07:55:41+00:00", "seconds": 23}],
        "gaps": [2.0, 4.5],
        "stats": {"n": 2, "mean": 3.25, "median": 4.5, "max": 4.5, "design": 2.0},
    }
    github = {"rows": [{"number": 557, "event": "workflow_dispatch",
                        "status": "completed", "conclusion": "success",
                        "created": "2026-09-18T08:02:40Z", "seconds": 90,
                        "url": "https://example.invalid/557"}]}

    page = render_page(azure, github)
    assert page.startswith("<!doctype html"), "page must be a full document"
    assert page.rstrip().endswith("</html>"), "page must be closed"
    for needle in ("polar-trigger-abc", "Succeeded", "557", "success", "4.5h"):
        assert needle in page, f"missing {needle!r} in rendered page"

    # A failure in either source must still produce a usable page, not a 500.
    degraded = render_page({"error": "ARM HTTP 403: denied"},
                           {"error": "rate limited"})
    assert "ARM HTTP 403" in degraded and "rate limited" in degraded
    assert degraded.rstrip().endswith("</html>")

    # Gap statistics are the point of the page; an empty history must not crash.
    empty = render_page({"rows": [], "gaps": [], "stats": {}}, {"rows": []})
    assert empty.rstrip().endswith("</html>")

    print("selftest ok")
    return 0


def main() -> None:
    import sys
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(f"[status] listening on 0.0.0.0:{PORT}  job={JOB_NAME}  repo={GH_REPO}",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
