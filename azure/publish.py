#!/usr/bin/env python3
"""
azure/publish.py — upload the staged site to whatever is hosting it.

The pipeline produces a self-contained site tree in $OUTPUT_DIR/latest:

    root.json          manifest the app polls; carries baseUrl + timestamp
    tiles/*.jpg        six 1024x1024 cubemap faces (~2 MB)
    storms.json        Blitzortung lightning
    aurora.json        NOAA OVATION oval

This module uploads that tree. It is the ONLY place that knows about the
host, so moving from Azure Static Web Apps to Blob storage (or to a local
directory for testing) is a flag, not a code change.

Targets
-------
local   copy the tree to POLAR_PUBLISH_LOCAL_DIR (default: a sibling
        "published" directory). For testing the plumbing without any cloud
        credentials.

blob    Azure Blob static website. Needs either
          AZURE_STORAGE_CONNECTION_STRING, or
          AZURE_STORAGE_ACCOUNT_URL  (+ DefaultAzureCredential)
        and uploads into the $web container.

swa     Azure Static Web Apps, via the `swa` CLI and a deployment token in
        SWA_DEPLOYMENT_TOKEN (or AZURE_STATIC_WEB_APPS_API_TOKEN).

Ordering matters
----------------
root.json is uploaded LAST, after every other file. It is the client's commit
point: the app polls it, compares `timestamp`, and only then downloads the six
faces. Uploading it first would advertise a new timestamp while the new faces
were still landing, so clients would fetch a half-published set.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] publish: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("publish")

# The app polls these on a schedule and must always see the newest version, so
# they are revalidated on every request. The faces are overwritten in place
# (flat tiles/ directory), which rules out long-lived caching for them too.
NO_CACHE = "no-cache"

CONTENT_TYPES = {
    ".json": "application/json",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".txt": "text/plain",
}

# root.json advertises this host when nothing is configured; publishing it
# would point every client at a dead URL. "owner.github.io" is the legacy
# pipeline's fallback — it substituted the literal string "owner" for the repo
# owner whenever GITHUB_REPOSITORY was unset, producing a realistic-looking
# host that would have shipped silently. That is exactly why it is checked.
PLACEHOLDER_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "owner.github.io")


def content_type_for(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def collect(source: Path) -> tuple[list[Path], Path]:
    """Return (files_to_upload_in_order, root_json_path).

    Every file is uploaded before root.json, so the returned list has
    root.json moved to the end.
    """
    if not source.is_dir():
        raise SystemExit(f"publish source is not a directory: {source}")

    files = sorted(p for p in source.rglob("*") if p.is_file())
    if not files:
        raise SystemExit(f"publish source is empty: {source}")

    root_json = source / "root.json"
    if not root_json.is_file():
        raise SystemExit(
            f"no root.json in {source} — refusing to publish a site without "
            f"a manifest, clients would never see it")

    others = [f for f in files if f != root_json]
    return others + [root_json], root_json


def check_manifest(root_json: Path, allow_placeholder: bool) -> dict:
    """Validate root.json before anything is uploaded."""
    try:
        data = json.loads(root_json.read_text())
    except Exception as exc:                       # noqa: BLE001
        raise SystemExit(f"root.json is not valid JSON ({root_json}): {exc}")

    base_url = (data.get("baseUrl") or "").strip()
    timestamp = (data.get("timestamp") or "").strip()

    if not base_url:
        raise SystemExit("root.json has no baseUrl — refusing to publish")
    if not timestamp:
        raise SystemExit("root.json has no timestamp — refusing to publish")

    host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    if host in PLACEHOLDER_HOSTS and not allow_placeholder:
        raise SystemExit(
            f"root.json baseUrl is the local placeholder ({base_url}).\n"
            f"Set POLAR_PUBLIC_BASE_URL to the real site root before "
            f"publishing, or pass --allow-placeholder if this is a local "
            f"smoke test.")
    return data


def write_swa_config(source: Path) -> Path:
    """Write staticwebapp.config.json so Static Web Apps sends no-cache.

    Static Web Apps controls response headers from a config file in the site
    root; without it, the platform default could cache root.json and clients
    would never notice a new timestamp.
    """
    cfg = source / "staticwebapp.config.json"
    cfg.write_text(json.dumps({
        "globalHeaders": {"Cache-Control": NO_CACHE},
    }, indent=2))
    return cfg


def write_index_html(source: Path, manifest: dict) -> Path:
    """Write a minimal index.html.

    Required, not decorative: `swa deploy` refuses to deploy a folder with no
    default document —

        Failed to find a default file in the app artifacts folder.
        Valid default files: index.html, Index.html.

    — and that failure surfaces only after the whole tree has been uploaded, so
    it is an expensive way to discover a missing file. Blob static website
    hosting also wants an index document, so this is written for every target.

    Content is deliberately tiny: the tiles are the product, this is just a
    human-readable landing page that also states which timestamp is live.
    """
    base = (manifest.get("baseUrl") or "").rstrip("/")
    ts = manifest.get("timestamp") or "(unknown)"
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>polar_plus cloud tiles</title>
<style>
 body {{ font: 15px/1.6 system-ui, sans-serif; margin: 3rem auto; max-width: 40rem;
        padding: 0 1rem; color: #222; }}
 code {{ background: #f2f2f2; padding: .1rem .3rem; border-radius: 3px; }}
</style>
</head>
<body>
<h1>polar_plus</h1>
<p>Cloud cubemap tile endpoint. This page exists because the deployment
   tooling requires a default document; the client never requests it.</p>
<ul>
  <li>published timestamp: <code>{ts}</code></li>
  <li>manifest: <a href="root.json">root.json</a></li>
  <li>tiles: <code>{base}/&lt;face&gt;.jpg</code></li>
  <li>lightning: <a href="storms.json">storms.json</a></li>
  <li>aurora: <a href="aurora.json">aurora.json</a></li>
</ul>
</body>
</html>
"""
    index = source / "index.html"
    index.write_text(html)
    return index


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
def publish_local(files: list[Path], source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for src in files:
        rel = src.relative_to(source)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    logger.info("copied %d file(s) to %s", len(files), dest)


def publish_blob(files: list[Path], source: Path) -> None:
    try:
        from azure.storage.blob import (BlobServiceClient,
                                        ContentSettings)
    except ImportError:
        raise SystemExit(
            "azure-storage-blob is not installed; it is in "
            "azure/requirements.txt and baked into the image")

    conn = (os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
    account_url = (os.environ.get("AZURE_STORAGE_ACCOUNT_URL") or "").strip()
    container = (os.environ.get("AZURE_STORAGE_CONTAINER")
                 or "$web").strip()

    if conn:
        service = BlobServiceClient.from_connection_string(conn)
    elif account_url:
        from azure.identity import DefaultAzureCredential
        service = BlobServiceClient(account_url=account_url,
                                    credential=DefaultAzureCredential())
    else:
        raise SystemExit(
            "set AZURE_STORAGE_CONNECTION_STRING, or AZURE_STORAGE_ACCOUNT_URL "
            "to use the managed identity")

    for src in files:
        rel = src.relative_to(source).as_posix()
        blob = service.get_blob_client(container=container, blob=rel)
        with open(src, "rb") as fh:
            blob.upload_blob(fh, overwrite=True, content_settings=ContentSettings(
                content_type=content_type_for(src),
                cache_control=NO_CACHE,
            ))
        logger.info("  -> %s/%s", container, rel)


def publish_swa(files: list[Path], source: Path) -> None:
    token = (os.environ.get("SWA_DEPLOYMENT_TOKEN")
             or os.environ.get("AZURE_STATIC_WEB_APPS_API_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "set SWA_DEPLOYMENT_TOKEN (Azure portal -> Static Web App -> "
            "Manage deployment token) to use the swa target")

    cmd = ["swa", "deploy", str(source),
           "--deployment-token", token,
           "--env", os.environ.get("SWA_ENV", "production")]
    logger.info("running: %s", " ".join(
        "***" if c == token else c for c in cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout:
        logger.info(proc.stdout.strip())
    if proc.returncode != 0:
        if proc.stderr:
            logger.error(proc.stderr.strip())
        raise SystemExit(f"swa deploy failed with exit code {proc.returncode}")
    logger.info("Static Web Apps deployment complete")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="publish the staged site tree")
    ap.add_argument("--target", default=os.environ.get("POLAR_PUBLISH_TARGET",
                                                       "local"),
                    choices=("local", "blob", "swa"))
    ap.add_argument("--source", default=None,
                    help="site tree to publish (default $OUTPUT_DIR/latest)")
    ap.add_argument("--dest", default=None,
                    help="destination for --target local")
    ap.add_argument("--allow-placeholder", action="store_true",
                    help="permit the localhost placeholder baseUrl (smoke "
                         "tests only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and list what would be uploaded")
    args = ap.parse_args()

    output_dir = Path(os.environ.get("OUTPUT_DIR", "/data/output"))
    source = Path(args.source) if args.source else output_dir / "latest"

    root_json = source / "root.json"
    if not root_json.is_file():
        raise SystemExit(
            f"no root.json in {source} — refusing to publish a site without a "
            f"manifest, clients would never see it")
    manifest = check_manifest(root_json, args.allow_placeholder)

    # Both files are generated into the source tree, so they must exist before
    # the upload list is taken below — otherwise they are silently not shipped
    # and `swa deploy` fails on the missing default document.
    write_index_html(source, manifest)
    if args.target == "swa":
        write_swa_config(source)

    files, root_json = collect(source)

    print(f"  target    : {args.target}")
    print(f"  source    : {source}")
    print(f"  baseUrl   : {manifest.get('baseUrl')}")
    print(f"  timestamp : {manifest.get('timestamp')}")
    print(f"  files     : {len(files)} (root.json last)")

    if args.dry_run:
        for f in files:
            print(f"    {f.relative_to(source)}")
        print("  dry run: nothing uploaded")
        return 0

    if args.target == "local":
        dest = (Path(args.dest) if args.dest
                else Path(os.environ.get("POLAR_PUBLISH_LOCAL_DIR")
                          or (output_dir / "published")))
        publish_local(files, source, dest)
    elif args.target == "blob":
        publish_blob(files, source)
    elif args.target == "swa":
        publish_swa(files, source)

    logger.info("published %s (%s)", manifest.get("timestamp"), args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
