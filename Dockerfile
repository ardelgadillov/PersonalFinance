# Home Assistant add-on image for Personal Finance.
# BUILD_FROM is supplied by the Supervisor builder (see build.yaml); the default lets you
# also build it by hand with `docker build .`.
ARG BUILD_FROM=python:3.13-slim
FROM ${BUILD_FROM}

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
