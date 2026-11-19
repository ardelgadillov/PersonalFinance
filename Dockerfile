# Home Assistant add-on image for Personal Finance.
# We pin a Debian base (python:3.13-slim) directly instead of honouring the builder's
# BUILD_FROM. The new HA "Apps" builder (HA 2026.2+) ignores build.yaml's build_from and
# injects BUILD_FROM=ghcr.io/home-assistant/base:latest, which is Alpine/musl -- there
# apt-get is missing and pandas/numpy/scipy would compile from source (slow / failing).
# python:3.13-slim is multi-arch (aarch64/amd64/armv7), so this works on every HA host.
# Any BUILD_FROM the builder passes is accepted as a harmless unused build arg.
ARG BUILD_FROM
FROM python:3.13-slim

ENV FINANCE_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata: honour the configured timezone; ca-certificates: outbound HTTPS (SimpleFIN, Yahoo).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so they are cached across code changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Then the application code (.dockerignore keeps data/, .env, .git etc. out).
COPY . /app

EXPOSE 8099
CMD ["bash", "/app/run.sh"]
