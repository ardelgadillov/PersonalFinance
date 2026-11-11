#!/usr/bin/env bash
# Add-on entrypoint. Configuration (SimpleFIN URL, timezone, daily sync hour) is read by the
# app itself from /data/options.json, and the ingress path from the Supervisor API, so this
# script just starts the web app bound to the ingress port.
set -euo pipefail

export FINANCE_DATA_DIR=/data
cd /app

exec python -m finance app --host 0.0.0.0 --port 8099
