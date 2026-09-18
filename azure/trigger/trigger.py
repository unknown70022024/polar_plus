#!/usr/bin/env python3
"""
azure/trigger/trigger.py — ask GitHub Actions to run the pipeline.

Why this exists
---------------
GitHub's own `schedule:` trigger is unreliable for a nowcast that needs a
predictable cadence. Measured over 13 days (60 scheduled runs, 2026-09-06 to
2026-09-18) the workflow asked for 12 runs/day and got 2-6, with a median gap
of 4.57h and a worst gap of 7.90h against a 2h design interval. Every single
run was late, none early. Roughly 60% of the schedule was dropped silently.

Azure Container Apps cron, by contrast, was measured on-time to the second.
So the split is: Azure owns the *clock*, GitHub owns the *compute*. This
container is the whole of Azure's side. It makes one HTTPS call and exits.

    az containerapp job start  ->  POST /repos/.../dispatches  ->  GitHub run

Cost: ~0.25 vCPU for a few seconds, 12 times a day. See azure/TRIGGER.md.

Design notes
------------
* Standard library only. No pip install, no build dependencies, no lockfile
  to keep fresh — this image exists to make one HTTP request.
* The token never goes near the command line, a file, or a log line. It is
  read from GH_DISPATCH_TOKEN, which the Container Apps job injects from a
  job secret.
* Exit code is the alertable signal: 0 means GitHub accepted the dispatch
  (HTTP 204). Anything else is a real operational problem — most often an
  expired token — and must be loud, because when it breaks, the pipeline
  stops and nothing else will notice.

Environment
-----------
    GH_DISPATCH_TOKEN   required. PAT with `Actions: write` on the repo.
    GH_REPO             default unknown70022024/polar_plus
    GH_WORKFLOW         default pipeline.yml (file name or numeric id)
    GH_REF              default main (must be a branch holding the workflow)

Usage
-----
    python trigger.py              dispatch, exit 0/1
    python trigger.py --check      validate configuration, send nothing
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
API_VERSION = "2022-11-28"

DEFAULT_REPO = "unknown70022024/polar_plus"
DEFAULT_WORKFLOW = "pipeline.yml"
DEFAULT_REF = "main"

ATTEMPTS = 5
TIMEOUT_S = 20
# 429 and 5xx are worth another try; 401/403/404/422 will not change and are
# returned immediately so the failure is reported fast and unambiguously.
RETRYABLE = frozenset({429, 500, 502, 503, 504})

# HTTP status -> what the operator should do about it. Written out because
# this container is the only thing standing between a bad token and a dead
# pipeline, and the Azure log is where someone will read it at 3am.
DIAGNOSIS = {
    401: "token rejected (expired or revoked) — issue a new PAT and update "
         "the job secret",
    403: "token lacks permission (needs 'Actions: write'), or it is "
         "rate-limited / blocked by SSO",
    404: "repo or workflow not found, or the workflow has no "
         "workflow_dispatch trigger on this ref",
    422: "ref does not exist, or the dispatch payload was rejected",
}


def log(msg: str) -> None:
    print(f"[trigger] {msg}", flush=True)


def config() -> tuple[str, str, str, str]:
    token = os.environ.get("GH_DISPATCH_TOKEN", "").strip()
    repo = os.environ.get("GH_REPO", DEFAULT_REPO).strip()
    workflow = os.environ.get("GH_WORKFLOW", DEFAULT_WORKFLOW).strip()
    ref = os.environ.get("GH_REF", DEFAULT_REF).strip()
    return token, repo, workflow, ref


def dispatch_url(repo: str, workflow: str) -> str:
    return f"{API}/repos/{repo}/actions/workflows/{workflow}/dispatches"


def dispatch(repo: str, workflow: str, ref: str, token: str) -> tuple[int | None, str]:
    """POST the dispatch event. Returns (http_status, detail); None = never
    reached the server."""
    url = dispatch_url(repo, workflow)
    payload = json.dumps({"ref": ref}).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "Content-Type": "application/json",
        "User-Agent": "polar-plus-trigger",
    }

    for attempt in range(1, ATTEMPTS + 1):
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                return response.status, ""
        except urllib.error.HTTPError as exc:
            detail = exc.read(400).decode("utf-8", "replace").strip()
            if exc.code in RETRYABLE and attempt < ATTEMPTS:
                wait = 2 ** attempt
                log(f"HTTP {exc.code} on attempt {attempt}/{ATTEMPTS}, retrying in {wait}s")
                time.sleep(wait)
                continue
            return exc.code, detail
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # DNS failure, TLS reset, connect timeout. Observed in practice on
            # this network as "SSL: UNEXPECTED_EOF_WHILE_READING", which is
            # transient often enough to be worth retrying.
            if attempt < ATTEMPTS:
                wait = 2 ** attempt
                log(f"transport error on attempt {attempt}/{ATTEMPTS} "
                    f"({exc}), retrying in {wait}s")
                time.sleep(wait)
                continue
            return None, str(exc)

    return None, "retries exhausted"


def newest_run(repo: str, workflow: str, token: str) -> str | None:
    """Best-effort: report which run this dispatch created.

    Purely for the log — the acceptance signal is the 204, not this. Any
    failure here is swallowed so it can never turn a good dispatch into a
    failed job execution.
    """
    url = f"{API}/repos/{repo}/actions/workflows/{workflow}/runs?per_page=1"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "polar-plus-trigger",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            runs = json.load(response).get("workflow_runs", [])
        if not runs:
            return None
        run = runs[0]
        return f"#{run.get('run_number')} {run.get('status')} {run.get('html_url')}"
    except Exception as exc:  # noqa: BLE001 — deliberately unfailable
        log(f"(could not read back the run: {exc})")
        return None


def main(argv: list[str]) -> int:
    token, repo, workflow, ref = config()

    if "--check" in argv:
        log(f"repo      : {repo}")
        log(f"workflow  : {workflow}")
        log(f"ref       : {ref}")
        log(f"endpoint  : POST {dispatch_url(repo, workflow)}")
        log(f"token     : {'set (' + str(len(token)) + ' chars)' if token else 'MISSING'}")
        return 0 if token else 1

    if not token:
        log("FATAL: GH_DISPATCH_TOKEN is not set")
        return 1

    log(f"dispatching {repo} :: {workflow} @ {ref}")
    started = time.time()
    status, detail = dispatch(repo, workflow, ref, token)
    elapsed = time.time() - started

    if status == 204:
        log(f"accepted in {elapsed:.3f}s (HTTP 204)")
        run = newest_run(repo, workflow, token)
        if run:
            log(f"run: {run}")
        return 0

    log(f"FAILED after {elapsed:.3f}s: HTTP {status if status else 'no response'}")
    if detail:
        log(f"response: {detail}")
    if status in DIAGNOSIS:
        log(f"=> {DIAGNOSIS[status]}")
    elif status is None:
        log("=> api.github.com unreachable from this environment")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
