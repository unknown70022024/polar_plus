# syntax=docker/dockerfile:1
#
# polar_plus pipeline container.
#
# Build from the REPO ROOT (the context needs both polar_plus/ and azure/):
#     docker build -f azure/Dockerfile -t polar-plus .
#
# Run one pipeline pass, publishing to a local directory:
#     docker run --rm -it \
#       -v "$PWD/output:/data/output" \
#       -e POLAR_PUBLIC_BASE_URL=http://localhost:8080 \
#       -e POLAR_PUBLISH_TARGET=local \
#       -e POLAR_PUBLISH_LOCAL_DIR=/data/output/latest-published \
#       -e SSEC_API_KEY=... \
#       polar-plus
#
# Design notes
# ------------
# * Base is python:3.12-slim. h5py and Pillow publish manylinux wheels that
#   bundle their native libraries (HDF5 included), so no compiler and no
#   libhdf5-dev are needed — the image stays small and the build is fast.
# * Runs as root on purpose. The pipeline is a batch job over public data with
#   no untrusted input, and the most likely local invocation bind-mounts a host
#   directory onto /data/output; a non-root UID would then fail to write.
#   Switch with --user if you later attach a volume with different ownership.
# * The Static Web Apps CLI needs Node. It is only used by the publish step,
#   so it is behind a build arg: pass --build-arg INSTALL_SWA_CLI=0 for a
#   much smaller/faster image when you only need the local or blob target.
# * BASE_IMAGE is overridable because Docker Hub is not reachable from every
#   network. On a blocked network build against a mirror instead of editing
#   the host's registries.conf:
#       podman build -f azure/Dockerfile -t polar-plus \
#         --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .
#   The default is the canonical image, which is what Azure build agents and
#   most CI can pull directly.

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ARG INSTALL_SWA_CLI=1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    TZ=UTC \
    OUTPUT_DIR=/data/output

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code edits do not invalidate the pip layer.
COPY azure/requirements.txt /app/azure/requirements.txt
RUN pip install --no-cache-dir -r /app/azure/requirements.txt

# Static Web Apps deploy CLI (publish target `swa`). Node is only needed for
# this, hence the separate layer and the build arg.
#
# The extra packages are NOT optional. Without them this step fails outright
# on any network where GitHub release downloads are slow or blocked:
#
#   * @azure/static-web-apps-cli hard-depends on keytar@7, a NATIVE module
#     whose install script is `prebuild-install || npm run build`. It first
#     tries to download a prebuilt binary from GitHub releases; when that
#     times out it falls back to compiling from source, which needs
#     g++ / make / libsecret-1-dev / pkg-config.
#   * gyp-next looks redundant but is the crux. Debian's `gyp` apt package
#     installs into /usr/lib/python3/dist-packages, while this image's Python
#     lives at /usr/local — so node-gyp, which shells out to `python3`, cannot
#     see it and dies with "ModuleNotFoundError: No module named 'gyp'".
#     Installing gyp-next with pip puts the module where that interpreter
#     actually looks.
#   * The prebuild download is still attempted and still times out on a
#     blocked network. It no longer matters, because the source build now
#     succeeds. Expect 30-60s wasted on the doomed attempt.
#
# Pass --build-arg INSTALL_SWA_CLI=0 to skip all of it and use the `blob` or
# `local` publish target instead (image drops from ~696 MB to ~286 MB).
#
# The purge/autoremove at the end is worth the extra lines: g++ and the dev
# headers add ~330 MB, and none of them are needed once keytar is compiled.
# libsecret-1-0 is reinstalled BEFORE the purge so apt marks it as manually
# installed and autoremove leaves it alone — keytar.node links
# libsecret-1.so.0 at runtime (verified with ldd), so removing it would break
# `swa deploy` with a dlopen error rather than a build error.
RUN if [ "$INSTALL_SWA_CLI" = "1" ]; then \
        apt-get update \
     && apt-get install -y --no-install-recommends \
            nodejs npm g++ make libsecret-1-dev pkg-config \
     && pip install --no-cache-dir gyp-next \
     && npm install -g @azure/static-web-apps-cli \
     && npm cache clean --force \
     && apt-get install -y --no-install-recommends libsecret-1-0 \
     && apt-get purge -y g++ make libsecret-1-dev pkg-config \
     && apt-get autoremove -y \
     && rm -rf /var/lib/apt/lists/* /root/.npm; \
    fi

COPY polar_plus/ /app/polar_plus/
COPY azure/ /app/azure/

RUN chmod +x /app/azure/entrypoint.sh \
 && mkdir -p /data/output

ENTRYPOINT ["/app/azure/entrypoint.sh"]
