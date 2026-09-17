#!/usr/bin/env bash
#
# azure/entrypoint.sh — one full pipeline pass.
#
# Steps, mirroring what the GitHub Actions workflow used to do across four
# jobs, but as one process in one container:
#
#   1. GCC cloud composite -> cubemap faces   (polar_plus.run)
#   2. lightning fusion                       (polar_plus.lightning.pipeline)
#      NOAA GOES GLM + EUMETSAT MTG LI + Blitzortung(only Asia-Pacific)
#   3. NOAA OVATION aurora                    (fetch_aurora.py)
#   4. publish the staged tree                (azure/publish.py)
#
# Exit-code contract
# ------------------
# polar_plus.run exits 2 when the completeness gate finds no healthy GCC file
# inside the allowed window. That is a NORMAL outcome, not a fault: the run
# deliberately publishes nothing so the live site keeps serving the previous
# version. In the GitHub Actions world that non-zero code was load-bearing —
# it failed the job so the separate deploy job would be skipped. Here the
# publish step is inside this script, so the skip is handled below and the
# code's only remaining job is monitoring.
#
# That is why the default is to swallow it (POLAR_NO_DATA_EXIT=0). Alerting on
# it would page you every time NASA's archive is late, which — see the 2026-09
# measurements — is often. Set POLAR_NO_DATA_EXIT=2 if you would rather have
# the Container Apps execution show up as failed.

set -uo pipefail

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Allow `docker run ... bash` or `... python -c '...'` for debugging.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

: "${OUTPUT_DIR:=/data/output}"
export OUTPUT_DIR
mkdir -p "$OUTPUT_DIR"

log "=============================================================="
log "polar_plus pipeline container"
log "  output dir : $OUTPUT_DIR"
log "  publish to : ${POLAR_PUBLISH_TARGET:-local}"
log "  site root  : $(python -c 'from polar_plus.config import describe_base_url; print(describe_base_url())')"
log "=============================================================="

# --------------------------------------------------------------------------
# 1. GCC cloud composite
# --------------------------------------------------------------------------
log "[1/4] NASA GCC cloud composite -> cubemap"
python -m polar_plus.run
run_rc=$?

if [ "$run_rc" -eq 2 ]; then
    log "[1/4] no complete healthy GCC file this round."
    log "      Nothing was produced and nothing will be published;"
    log "      the live site keeps serving its previous version."
    exit "${POLAR_NO_DATA_EXIT:-0}"
fi

if [ "$run_rc" -ne 0 ]; then
    log "[1/4] FAILED with exit code $run_rc"
    exit "$run_rc"
fi

# The manifest run.py just staged is the handoff to the remaining steps.
ROOT_JSON="$OUTPUT_DIR/latest/root.json"
if [ ! -f "$ROOT_JSON" ]; then
    log "[1/4] FAILED: run.py exited 0 but staged no $ROOT_JSON"
    exit 1
fi

GCC_TIMESTAMP="$(python -c '
import json, sys
print(json.load(open(sys.argv[1]))["timestamp"])
' "$ROOT_JSON")" || { log "could not read timestamp from $ROOT_JSON"; exit 1; }
export GCC_TIMESTAMP
log "[1/4] staged GCC timestamp $GCC_TIMESTAMP"

# --------------------------------------------------------------------------
# 2 + 3. Auxiliary layers. These are non-fatal on purpose: a NOAA or
# Blitzortung outage should not block the cloud tiles, which are the main
# product. On failure the previous storms.json / aurora.json simply stays in
# the staged tree and gets republished unchanged.
# --------------------------------------------------------------------------
log "[2/4] lightning fusion (GOES GLM + EUMETSAT MTG LI + Blitzortung)"
if ! python -m polar_plus.lightning.pipeline; then
    log "[2/4] WARN: lightning fusion failed; continuing with the previous file"
fi

log "[3/4] NOAA OVATION aurora"
if ! python polar_plus/fetch_aurora.py; then
    log "[3/4] WARN: aurora fetch failed; continuing with the previous file"
fi

# --------------------------------------------------------------------------
# 4. Publish
# --------------------------------------------------------------------------
log "[4/4] publishing ($GCC_TIMESTAMP)"
if ! python azure/publish.py \
        --target "${POLAR_PUBLISH_TARGET:-local}" \
        --source "$OUTPUT_DIR/latest"; then
    log "[4/4] FAILED to publish"
    exit 1
fi

log "done: published $GCC_TIMESTAMP"
