# syntax=docker/dockerfile:1
#
# polar_plus schedule trigger.
#
# Deliberately NOT built from azure/Dockerfile. That image carries h5py,
# Pillow and (optionally) Node + the Static Web Apps CLI to run the pipeline,
# which is ~696 MB. This one makes a single HTTPS POST, so it is the standard
# library and nothing else.
#
# Build from the REPO ROOT:
#     podman build -f azure/trigger/Dockerfile -t polar-plus-trigger:v1 .
#
# BASE_IMAGE is overridable for the same reason as the pipeline image: Docker
# Hub is unreachable from some networks, and the mirror turns a 5s timeout
# into a working build without touching the host's registries.conf.
#     podman build -f azure/trigger/Dockerfile -t polar-plus-trigger:v1 \
#       --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

COPY azure/trigger/trigger.py /app/trigger.py

# Non-root on purpose, unlike the pipeline image (which runs as root because it
# bind-mounts a host output directory). Nothing here needs to write to disk,
# and this process holds a credential with write access to the repository.
#
# The chmod is load-bearing, not tidiness. COPY preserves the source file's
# mode, and a checkout with a restrictive umask (0600) lands as root:root 0600
# — unreadable by uid 10001, which fails at runtime with a bare
# "can't open file '/app/trigger.py': [Errno 13] Permission denied".
RUN useradd --uid 10001 --create-home trigger \
 && chmod 0644 /app/trigger.py
USER 10001

ENTRYPOINT ["python", "/app/trigger.py"]
