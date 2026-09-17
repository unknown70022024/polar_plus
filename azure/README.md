# Running polar_plus as a container on Azure

This directory replaces the four-job GitHub Actions workflow with one
container image and one scheduled Azure Container Apps job. Nothing here
depends on GitHub: the image builds anywhere, the schedule is Azure's, and the
publish target is configured, not compiled in.

## What changed, and why

The old pipeline was shaped by GitHub Actions, not by the task. Four jobs
communicated through `$GITHUB_ENV`, the publish URL was derived from
`GITHUB_REPOSITORY`, and "no healthy GCC file" was signalled by a non-zero exit
code that existed purely to skip a downstream deploy job. Migrating that
literally would have carried all of it along.

| GitHub Actions | Here |
|---|---|
| 4 jobs, `needs:` chain | 1 container, `entrypoint.sh` |
| `$GITHUB_ENV` for `GCC_TIMESTAMP` | `output/latest/root.json` is the handoff |
| `GH_PAGES_BASE` / `GITHUB_REPOSITORY` | `POLAR_PUBLIC_BASE_URL` |
| `upload-pages-artifact` + `deploy-pages` | `publish.py --target swa` |
| workflow red X | Container Apps execution status |
| GitHub Secrets | Container Apps secrets |
| `timeout-minutes: 15` | `replica-timeout` (up to 86400 s) |

The publish host is the only thing that had to change in `polar_plus/`
itself — the client reads `baseUrl` out of `root.json`, so the Android app
follows the tiles to a new host without being rebuilt. `baseUrl` was already
the seam; this just stops hard-coding it.

## Files

| File | Role |
|---|---|
| `Dockerfile` | image definition; build from the **repo root** |
| `entrypoint.sh` | one full pass: run.py → storms → aurora → publish |
| `publish.py` | uploads the staged tree; targets `local`, `blob`, `swa` |
| `selftest.py` | verifies the compute path without a NASA download |
| `connectivity_test.py` | probes every external endpoint from wherever it runs |
| `requirements.txt` | runtime deps (much smaller than the repo-root one) |

## Build and run locally

```bash
cd ~/polar_plus          # the directory containing azure/ and polar_plus/

# Build. The context must be the repo root — it needs polar_plus/ and azure/.
docker build -f azure/Dockerfile -t polar-plus .

# On a network where Docker Hub is blocked, use a mirror instead of editing
# the host's registries.conf:
podman build -f azure/Dockerfile -t polar-plus \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .

# One pass, publishing into ./output/published
docker run --rm -it \
  -v "$PWD/output:/data/output" \
  -e POLAR_PUBLIC_BASE_URL=http://localhost:8080 \
  -e POLAR_PUBLISH_TARGET=local \
  -e SSEC_API_KEY="$SSEC_API_KEY" \
  polar-plus
```

`-v "$PWD/output:/data/output"` is worth keeping: it preserves the debug PNGs
and the staged tree between runs, and it is what `selftest.py` reads.

Set `INSTALL_SWA_CLI=0` for a much faster, smaller build if you only need the
`local` or `blob` target. Measured: **696 MB with the SWA CLI, 286 MB
without.**

```bash
docker build -f azure/Dockerfile -t polar-plus --build-arg INSTALL_SWA_CLI=0 .
```

Drop into a shell, or run anything else, by passing a command — the entrypoint
execs it instead of running the pipeline:

```bash
docker run --rm -it polar-plus bash
docker run --rm -it polar-plus python azure/selftest.py --from-output /data/output
```

## Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `POLAR_PUBLIC_BASE_URL` | yes, to publish | — | site root; `root.json` lands at `{it}/root.json`, tiles at `{it}/tiles/` |
| `POLAR_PUBLISH_TARGET` | no | `local` | `local`, `blob` or `swa` |
| `SSEC_API_KEY` | no | empty | SSEC RealEarth key. **Not required** — GitHub Actions never set it either; with it empty the code omits `accesskey` and uses anonymous access, which works (verified HTTP 200). Only worth setting if you hit anonymous rate limits. |
| `OUTPUT_DIR` | no | `/data/output` | working directory |
| `POLAR_NO_DATA_EXIT` | no | `0` | exit code when no healthy GCC file was found |
| `POLAR_SEARCH_HOURS` | no | `48` | how many hours back to search for a complete GCC file |
| `POLAR_PUBLISH_LOCAL_DIR` | no | `$OUTPUT_DIR/published` | destination for the `local` target |
| `AZURE_STORAGE_CONNECTION_STRING` | for `blob` | — | blob auth |
| `AZURE_STORAGE_ACCOUNT_URL` | for `blob` | — | blob auth via managed identity |
| `AZURE_STORAGE_CONTAINER` | no | `$web` | blob container |
| `SWA_DEPLOYMENT_TOKEN` | for `swa` | — | Static Web App deployment token |
| `SWA_ENV` | no | `production` | SWA environment name |
| `POLAR_HEALTH_ENFORCE` | no | `1` | `0` = log-only gate (shadow mode) |
| `POLAR_FLOOR_TS` | no | auto | `none` disables the backward-search floor; `YYYYMMDD_HHMM` forces it |

`POLAR_PUBLIC_BASE_URL` is deliberately host-neutral. The legacy
`GH_PAGES_BASE` / `PAGES_ROOT_URL` / `GITHUB_REPOSITORY` variables are still
honoured so the existing Actions workflow keeps running during the cutover;
once the Azure job is the only producer they can be deleted from
`config.public_base_url()`.

## The exit-code contract

`polar_plus.run` exits **2** when no complete GCC file is found in the allowed
window. That is a normal outcome: the run deliberately publishes nothing, and
the live site keeps serving its previous version. This is the behaviour that
got the broken 2026-09-15 15:00Z data replaced.

In GitHub Actions that non-zero code was load-bearing — it failed the job so
the separate deploy job would be skipped. Here the publish step is *inside*
`entrypoint.sh`, so the skip is handled explicitly and the exit code's only
remaining purpose is monitoring. The default is therefore to swallow it
(`POLAR_NO_DATA_EXIT=0`): alerting on it would page you every time NASA's
archive is late, which by the 2026-09 measurements is often.

Set `POLAR_NO_DATA_EXIT=2` if you would rather see it as a failed execution.

## Deploying to Azure

**If this is your first deployment, follow [`DEPLOY.md`](DEPLOY.md) instead.**
It walks through every resource with verification steps and a troubleshooting
table. This is the condensed version.

### Region is not a free choice

Azure for Students subscriptions carry a built-in **"Allowed resource
deployment regions"** policy; deploying outside it fails with
`RequestDisallowedByAzure`. The allowed list is **different for every
subscription** and cannot be bypassed. Check yours:

```bash
az policy assignment list \
  --query "[?contains(displayName, 'Allowed')].parameters.allowedLocations.value" -o json
```

or in the portal: **Policy → Authoring → Assignments → "Allowed resource
deployment regions" → Parameters**.

Static Web Apps in turn is available only in `westus2`, `centralus`,
`eastus2`, `westeurope` and `eastasia`. **Everything must live in the
intersection of those two sets** — if it is empty, SWA is impossible and you
need `publish.py --target blob` instead (Blob storage is available far more
widely and is equally free at this scale).

```bash
RG=polar-plus-rg
LOC=eastasia              # MUST be in your policy's allow-list AND SWA-capable
SWA=polar-plus-swa-2026   # globally unique; becomes your hostname
ENV=polar-plus-env
JOB=polar-plus
GHCR_USER=your-github-user
IMAGE_TAG=v1

az group create -n $RG -l $LOC

# ---- image ---------------------------------------------------------------
# No Azure Container Registry: ACR Basic costs ~$5/month and is the resource
# most likely to be blocked by the region policy. ghcr.io is free for public
# packages, and a public image needs no credentials at all.
podman build -f azure/Dockerfile \
  -t "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG" \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .
echo "$GHCR_PAT" | podman login ghcr.io -u "$GHCR_USER" --password-stdin
podman push "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG"
# then set the package to Public in GitHub package settings

# ---- hosting (Static Web Apps, Free) -------------------------------------
az staticwebapp create -n $SWA -g $RG -l $LOC --sku Free
SWA_HOST=$(az staticwebapp show -n $SWA -g $RG --query defaultHostname -o tsv)
SWA_TOKEN=$(az staticwebapp secrets list -n $SWA -g $RG --query properties.apiKey -o tsv)

# ---- scheduled job -------------------------------------------------------
az containerapp env create -n $ENV -g $RG -l $LOC

az containerapp job create -n $JOB -g $RG --environment $ENV \
  --trigger-type Schedule \
  --cron-expression "48 */2 * * *" \
  --container-name polar-plus \
  --image "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG" \
  --cpu 2 --memory 4Gi \
  --replica-timeout 1800 --replica-retry-limit 0 \
  --parallelism 1 --replica-completion-count 1 \
  --secrets swa-token="$SWA_TOKEN" \
  --env-vars \
      POLAR_PUBLIC_BASE_URL="https://$SWA_HOST" \
      POLAR_PUBLISH_TARGET=swa \
      OUTPUT_DIR=/data/output \
      SWA_DEPLOYMENT_TOKEN=secretref:swa-token
```

No `SSEC_API_KEY` (GitHub Actions never set one either) and no registry
credentials (the image is public).

Every `--env-vars` value must be listed on every update: the flag replaces the
set wholesale rather than merging.

The cron is UTC-only and matches the old workflow's schedule (`:48` past every
even hour, 48 minutes after the GCC file for `HH:00Z` finalises).

Test a single execution without waiting for the schedule:

```bash
az containerapp job start -n $JOB -g $RG
az containerapp job execution list -n $JOB -g $RG -o table

# The container name must match --container-name above.
az containerapp job logs show -n $JOB -g $RG \
  --container polar-plus --tail 100 --format text
```

Then point the app at `https://$SWA_HOST` in `DataUrls.java`.

### Cost

Everything here fits inside standing free grants, so the running cost is **$0**
— no student credit consumed:

| Item | Cost |
|---|---|
| Container Apps job (Consumption, 2 vCPU / 4 GiB, ~360 s, 12×/day) | within the 180,000 vCPU-s / 360,000 GiB-s monthly grant at low frequency; see the note below |
| Image registry (ghcr.io, public package) | **$0** — GitHub: "usage is free for public packages" |
| Hosting (Static Web Apps Free, 100 GB/month) | **$0** |
| Log Analytics | ~**$0** — a few MB per run against a 5 GB/month free grant |
| Inbound from NASA (~360 GB/month) | **$0** — inbound is always free |
| Cross-region (everything in one region) | **$0** |

The Container Apps grant is a standing monthly allowance rather than a student
perk, so it survives graduation.

Two things to keep an eye on, neither of which is a real risk at this scale:

- **Log Analytics** is the only place this architecture can surprise you. Set a
  daily cap on the workspace.
- **Static Web Apps Free has no overage option.** At 100 GB/month the site
  stops serving until the next cycle. One client pulls ~2 MB per update and the
  app refreshes at most every 3 h on WiFi, so roughly 485 MB/month per client —
  comfortable for a small user base. `publish.py --target blob` is the escape
  hatch, since Blob egress degrades to ~$0.087/GB instead of stopping.

## Verifying a build without a NASA download

A full run reads roughly a gigabyte from `satcorps.larc.nasa.gov`. On a slow
link that is hours (measured: 0.10 Mbit/s from one machine = ~22 h for 1 GB),
which makes "does this container work?" impractical to answer by just running
it.

`selftest.py` answers it a different way: it takes a **real** gap-filled
density map that a previous run wrote, pushes it through the same
post-process → dateline-repair → cubemap → JPEG sequence the pipeline uses,
and compares the result with the tiles that same run published.

```bash
docker run --rm -v "$PWD/output:/data/output" polar-plus \
  python azure/selftest.py --from-output /data/output
```

On exact parity it prints `mean|d| 0.000  max|d| 0` for all six faces. That
covers everything except the download itself, which is why it is worth running
after any dependency or base-image change.

## First-run behaviour worth knowing

The backward-search window is **48 hours**, walked from the most recent
eligible hour (2 h ago, since younger files are still being written) back one
hour at a time. The floor that stops the walk early comes from reading the live
`root.json`; on a brand-new Static Web App that file does not exist yet, so the
window is the only bound.

48 h rather than something tighter because NASA's archive produces runs of
damaged files lasting a day or more — on 2026-09-15/16 a 12 h window meant
publishing nothing at all for over a day while healthy files sat just outside
it. Override per run with `POLAR_SEARCH_HOURS`.

Also note the cron is UTC and Container Apps does not accept a timezone, which
is fine here because every timestamp the pipeline handles is already
`timezone.utc`.

## Still to validate

- **NASA throughput from the chosen Azure region.** Not yet measured. The
  reasoning that GitHub Actions runs on Azure infrastructure is sound, but the
  region matters and this job's egress is not the same path. Run
  `connectivity_test.py` once from inside the deployed job.
- **SSEC reachability** from the same region (`realearth.ssec.wisc.edu`).
- **Cold-start time.** The image installs Node and the SWA CLI, so first-pull
  startup is slower than a bare Python image. `replica-timeout 1800` leaves
  plenty of room.
