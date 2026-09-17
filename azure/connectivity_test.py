#!/usr/bin/env python3
"""
azure/connectivity_test.py — probe every external endpoint the polar_plus
pipeline depends on, from wherever this runs.

Purpose: decide whether the pipeline can be hosted in an Azure region at all.
The known risk is NASA's satcorps.larc.nasa.gov, which is intermittently
unreachable / extremely slow. If it is worse from an Azure data centre than
from the current dev machine, no amount of Container Apps / Static Web Apps
work is worth doing.

Stdlib only, on purpose: this must run unmodified in
  * Azure Cloud Shell          (python3 connectivity_test.py)
  * the pipeline container     (python3 azure/connectivity_test.py)
  * the dev machine            (same)
so it deliberately avoids fsspec / h5py / requests.

What it measures per endpoint:
  * DNS resolution time
  * TCP connect time
  * TLS handshake time
  * time to first byte (TTFB) and total time for a real request

For NASA specifically it does the thing that actually matters: list the day's
directory, HEAD the newest files to find one that is actually COMPLETE (a
finished GCC file is ~875-1420 MB; one still being written is a few MB, and a
damaged one is markedly smaller — benchmarking a stub would measure nothing),
then pull a Range block from it and repeat, because the failure mode is
intermittent stalling rather than consistent slowness.

Usage:
    python3 azure/connectivity_test.py                  # 3 repeats, 16 MB each
    python3 azure/connectivity_test.py --repeats 5 --range-mb 32
    python3 azure/connectivity_test.py --quick          # 1 repeat, no TCP/TLS
    python3 azure/connectivity_test.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Endpoints the pipeline actually uses (kept in sync with polar_plus/config.py)
# --------------------------------------------------------------------------
GCC_BASE = ("https://satcorps.larc.nasa.gov/prod/GCC-GEO-LEO/v2a/"
            "visst-pixel-netcdf")
SSEC_WMS = "https://realearth.ssec.wisc.edu/cgi-bin/mapserv"
SSEC_API = "http://re.ssec.wisc.edu/api/image"
BLITZORTUNG = "http://bo-service.tryb.de/"
NOAA_AURORA = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"

# Range block the pipeline's fsspec layer actually uses
# (gcc_load.py: block_size=8*1024*1024). The test defaults to a slightly
# larger sample so throughput is measured over more than one block.
RANGE_BYTES = 8 * 1024 * 1024

UA = "polar_plus-connectivity-test/1.0"


def _timed(fn):
    """Run fn(), return (result, elapsed_seconds)."""
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


# --------------------------------------------------------------------------
# Layer probes
# --------------------------------------------------------------------------
def probe_dns(host: str, timeout: float = 10.0) -> tuple[float | None, str | None]:
    """Return (seconds, None) or (None, error)."""
    try:
        infos, dt = _timed(lambda: socket.getaddrinfo(
            host, 443, proto=socket.IPPROTO_TCP))
        return dt, None
    except Exception as exc:                       # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def probe_tcp(host: str, port: int = 443, timeout: float = 15.0):
    try:
        sock, dt = _timed(lambda: socket.create_connection((host, port),
                                                           timeout=timeout))
        sock.close()
        return dt, None
    except Exception as exc:                       # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def probe_tls(host: str, port: int = 443, timeout: float = 20.0):
    """Full TCP + TLS handshake on one fresh connection."""
    try:
        ctx = ssl.create_default_context()

        def do():
            raw = socket.create_connection((host, port), timeout=timeout)
            tls = ctx.wrap_socket(raw, server_hostname=host)
            return tls

        tls, dt = _timed(do)
        peer = tls.version()
        tls.close()
        return dt, peer, None
    except Exception as exc:                       # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {exc}"


def http_get(url: str, headers: dict | None = None,
             timeout: float = 30.0, read_cap: int | None = None):
    """GET url, return (status, nbytes, ttfb, total, error).

    ttfb is the time until urlopen() returns, i.e. DNS + TCP + TLS + server
    think time + response headers — a good proxy for "is this host responsive".
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               **(headers or {})})
    try:
        t0 = time.perf_counter()
        resp = urllib.request.urlopen(req, timeout=timeout)
        ttfb = time.perf_counter() - t0
        total_bytes = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if read_cap is not None and total_bytes >= read_cap:
                break
        total = time.perf_counter() - t0
        return resp.status, total_bytes, ttfb, total, None
    except urllib.error.HTTPError as exc:
        return exc.code, 0, None, None, f"HTTP {exc.code}"
    except Exception as exc:                       # noqa: BLE001
        return None, 0, None, None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# NASA-specific: find a real file, then Range-read it
# --------------------------------------------------------------------------
def file_stamp(name: str) -> str:
    """'...glob-comp.2026258.1300.3km.nc' -> '2026258.1300' (doy.hhmm)."""
    m = re.search(r"glob-comp\.(\d{7})\.(\d{4})\.3km\.nc$", name)
    return f"{m.group(1)}.{m.group(2)}" if m else name


def list_gcc_day(day) -> list[tuple[str, str]]:
    """Return [(url, filename)] for one UTC day, sorted by name.

    The NASA index is a small HTML page:
        <a href='/prod/.../foo.3km.nc' title='...'>foo.3km.nc</a><br/>
    It carries NO file sizes, so sizes need a separate HEAD per file.
    """
    path = f"{GCC_BASE}/{day.year}/{day.month:02d}/{day.day:02d}/"
    req = urllib.request.Request(path, headers={"User-Agent": UA})
    try:
        body = urllib.request.urlopen(req, timeout=60).read().decode(
            "utf-8", "replace")
    except Exception:                              # noqa: BLE001
        return []
    hrefs = re.findall(r"href='([^']+\.3km\.nc)'", body)
    out = []
    for h in sorted(set(hrefs)):
        url = h if h.startswith("http") else "https://satcorps.larc.nasa.gov" + h
        out.append((url, h.rsplit("/", 1)[-1]))
    return out


def head_size(url: str, timeout: float = 60.0) -> int | None:
    """Content-Length via HEAD, or None."""
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:                              # noqa: BLE001
        return None


def pick_benchmark_target(days_back: int, min_bytes: int):
    """Find the newest COMPLETE-looking GCC file to benchmark against.

    Files in this archive range ~875-1420 MB once complete; a file still being
    written is a few MB, and a damaged one is markedly smaller. Benchmarking
    against a stub would measure nothing useful (and silently understate the
    time a real run takes), so candidates below `min_bytes` are skipped.

    Returns (url, filename, size, [(day, name, size)] table).
    """
    from datetime import datetime, timedelta, timezone

    table: list[tuple[str, str, int | None]] = []
    day = datetime.now(timezone.utc)
    best = None
    for _ in range(max(1, days_back)):
        label = f"{day.year}-{day.month:02d}-{day.day:02d}"
        files = list_gcc_day(day)
        # HEAD only the newest few — enough to find a usable target.
        for url, name in reversed(files[-3:]):
            size = head_size(url)
            table.append((label, name, size))
            if size and size >= min_bytes and best is None:
                best = (url, name, size)
        if best:
            break
        day -= timedelta(days=1)
    return best, table


def benchmark_range(url: str, nbytes: int, repeats: int,
                    timeout: float = 120.0) -> dict:
    """Pull `nbytes` from `url` via Range, `repeats` times. Report stability."""
    samples: list[dict] = []
    for i in range(repeats):
        headers = {"Range": f"bytes=0-{nbytes - 1}"}
        status, got, ttfb, total, err = http_get(url, headers=headers,
                                                 timeout=timeout)
        if err:
            samples.append({"ok": False, "error": err})
        else:
            samples.append({
                "ok": status in (200, 206),
                "status": status,
                "bytes": got,
                "ttfb": ttfb,
                "total": total,
                "mbps": (got * 8 / 1e6) / total if total else 0.0,
            })
        if i < repeats - 1:
            time.sleep(2)
    ok = [s for s in samples if s.get("ok")]
    out = {
        "url": url,
        "requested_bytes": nbytes,
        "attempts": repeats,
        "successes": len(ok),
        "samples": samples,
    }
    if ok:
        out["ttfb_median"] = statistics.median(s["ttfb"] for s in ok)
        out["total_median"] = statistics.median(s["total"] for s in ok)
        out["mbps_median"] = statistics.median(s["mbps"] for s in ok)
        out["mbps_min"] = min(s["mbps"] for s in ok)
    return out


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def fmt(v, unit="s", nd=2):
    return "n/a" if v is None else f"{v:.{nd}f}{unit}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3,
                    help="Range-read repeats against NASA (default 3)")
    ap.add_argument("--range-mb", type=int, default=16,
                    help="size of each Range read in MB (default 16; the "
                         "pipeline itself uses 8 MB HDF5 block reads)")
    ap.add_argument("--min-file-mb", type=int, default=500,
                    help="ignore files smaller than this when picking a "
                         "benchmark target (default 500; a complete GCC file "
                         "is ~875-1420 MB)")
    ap.add_argument("--days-back", type=int, default=3,
                    help="how many days to search for a complete file")
    ap.add_argument("--quick", action="store_true",
                    help="1 repeat, skip per-host TCP/TLS breakdown")
    ap.add_argument("--json", metavar="PATH",
                    help="also write the full result as JSON")
    args = ap.parse_args()
    repeats = 1 if args.quick else max(1, args.repeats)
    range_bytes = max(1, args.range_mb) * 1024 * 1024
    min_bytes = max(1, args.min_file_mb) * 1024 * 1024

    import platform
    print("=" * 74)
    print("polar_plus connectivity test")
    print("=" * 74)
    print(f"python      : {platform.python_version()} ({platform.system()} "
          f"{platform.release()})")
    print(f"host        : {socket.gethostname()}")
    print(f"utc now     : {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}")
    print(f"range block : {range_bytes / 1e6:.0f} MB x {repeats} repeats")
    print(f"target file : newest GCC file >= {min_bytes / 1e6:.0f} MB")
    print()

    result: dict = {"host": socket.gethostname(), "repeats": repeats}

    # ---------------- Layer probes ----------------
    hosts = [
        ("satcorps.larc.nasa.gov", 443, "NASA GCC (primary)"),
        ("realearth.ssec.wisc.edu", 443, "SSEC RealEarth WMS"),
        ("re.ssec.wisc.edu", 80, "SSEC RealEarth API (http)"),
        ("bo-service.tryb.de", 80, "Blitzortung"),
        ("services.swpc.noaa.gov", 443, "NOAA SWPC OVATION"),
    ]
    print("-- DNS / TCP / TLS ---------------------------------------------------")
    print(f"{'host':30} {'dns':>8} {'tcp':>8} {'tls':>8}  tls-ver")
    layer: dict = {}
    for host, port, label in hosts:
        dns, dns_err = probe_dns(host)
        if dns_err:
            print(f"{host:30} {'FAIL':>8}  {dns_err}")
            layer[host] = {"dns_error": dns_err}
            continue
        if args.quick:
            print(f"{host:30} {fmt(dns):>8} {'skip':>8} {'skip':>8}")
            layer[host] = {"dns": dns}
            continue
        tcp, tcp_err = probe_tcp(host, port)
        tls_t, ver, tls_err = probe_tls(host, port)
        print(f"{host:30} {fmt(dns):>8} {fmt(tcp):>8} {fmt(tls_t):>8}  "
              f"{ver or tcp_err or tls_err or '-'}")
        layer[host] = {"dns": dns, "tcp": tcp, "tls": tls_t,
                       "tls_version": ver,
                       "tcp_error": tcp_err, "tls_error": tls_err}
    result["layers"] = layer
    print()

    # ---------------- Lightweight endpoint checks ----------------
    print("-- endpoint reachability ---------------------------------------------")
    checks = [
        (NOAA_AURORA, "NOAA ovation_aurora_latest.json"),
        (SSEC_WMS + "?service=WMS&request=GetCapabilities", "SSEC WMS capabilities"),
        (BLITZORTUNG, "Blitzortung bo-service"),
        (SSEC_API + "?products=globalir&x=0&y=0&z=0&width=256&height=256",
         "SSEC API image"),
    ]
    endpoints: dict = {}
    for url, label in checks:
        status, n, ttfb, total, err = http_get(url, timeout=30)
        if err:
            print(f"{label:36} FAIL  {err}")
        else:
            print(f"{label:36} {status}  {n/1024:8.1f} KB  "
                  f"ttfb {fmt(ttfb)}  total {fmt(total)}")
        endpoints[label] = {"url": url, "status": status, "bytes": n,
                            "ttfb": ttfb, "total": total, "error": err}
    result["endpoints"] = endpoints
    print()

    # ---------------- NASA: the one that matters ----------------
    print("-- NASA GCC: find a COMPLETE file, then Range-read it --------------")
    t0 = time.perf_counter()
    target, table = pick_benchmark_target(args.days_back, min_bytes)
    print(f"search took {time.perf_counter() - t0:.1f}s")
    if table:
        print("  archive sizes (newest few per day):")
        for label, name, size in table:
            stamp = file_stamp(name)
            shown = "HEAD failed" if size is None else f"{size / 1e6:9.1f} MB"
            print(f"    {label} {stamp:>6}  {shown}")
    if not target:
        print(f"  NO COMPLETE GCC FILE FOUND (>= {min_bytes / 1e6:.0f} MB) in "
              f"the last {args.days_back} day(s).")
        print("  A run would find nothing publishable right now. This is a "
              "data-availability problem, not a network one, but it makes the "
              "throughput measurement impossible.")
        result["gcc"] = {"error": "no complete file found", "table": table}
    else:
        url, name, size = target
        print(f"  target: {name}  ({size / 1e6:.1f} MB)")
        print(f"  url   : {url}")
        bench = benchmark_range(url, range_bytes, repeats)
        bench["file_bytes"] = size
        bench["file_name"] = name
        result["gcc"] = bench
        for i, s in enumerate(bench["samples"], 1):
            if s.get("ok"):
                print(f"    try {i}: {s['status']}  {s['bytes'] / 1e6:6.2f} MB  "
                      f"ttfb {s['ttfb']:6.2f}s  total {s['total']:6.2f}s  "
                      f"{s['mbps']:6.1f} Mbit/s")
            else:
                print(f"    try {i}: FAIL  {s.get('error')}")
        if bench["successes"]:
            mbps = bench["mbps_median"]
            short = [s for s in bench["samples"] if s.get("ok")
                     and s["bytes"] < range_bytes]
            print(f"  --> {bench['successes']}/{bench['attempts']} ok, "
                  f"median {mbps:.1f} Mbit/s, worst {bench['mbps_min']:.1f} "
                  f"Mbit/s, median ttfb {bench['ttfb_median']:.2f}s")
            if short:
                print(f"  --> NOTE {len(short)} read(s) returned fewer bytes "
                      f"than requested (server closed early)")
            # Extrapolate. The pipeline does not pull the whole file — it
            # range-reads BT_10.8um plus cloud_phase — but a full-file figure
            # is the right order of magnitude for "can a run finish in time".
            full_s = size * 8 / 1e6 / mbps
            print(f"  --> {file_stamp(name)} is "
                  f"{size / 1e6:.0f} MB; that file end-to-end at this speed "
                  f"= ~{full_s / 60:.1f} min")
    print()

    # ---------------- Verdict ----------------
    print("=" * 74)
    gcc = result.get("gcc") or {}
    if gcc.get("error"):
        verdict = "INCONCLUSIVE — no GCC file could be located to benchmark."
    elif not gcc.get("successes"):
        verdict = ("FAIL — NASA unreachable for Range reads from this host. "
                   "Do not migrate to this region.")
    else:
        rate = gcc["successes"] / gcc["attempts"]
        mbps = gcc["mbps_median"]
        if rate < 1.0:
            verdict = (f"UNSTABLE — {gcc['successes']}/{gcc['attempts']} reads "
                       f"succeeded. Intermittent stalls will trip the health "
                       f"gate and cause skipped publishes.")
        elif mbps < 5:
            verdict = (f"SLOW — {mbps:.1f} Mbit/s median. Each run downloads "
                       f"~1 GB, so expect multi-minute fetch times.")
        else:
            verdict = f"OK — {mbps:.1f} Mbit/s median, no failures."
    print("VERDICT:", verdict)
    print("=" * 74)
    result["verdict"] = verdict

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        print(f"full results written to {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
