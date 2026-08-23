#!/usr/bin/env bash
set -Eeuo pipefail

# Ensure report output directory exists inside container
REPORTS_DIR="${SSD_INTAKE_ROOT_DIR:-/app/reports}"
mkdir -p "$REPORTS_DIR"

BIND_HOST="${SSD_INTAKE_HOST:-127.0.0.1}"
BIND_PORT="${SSD_INTAKE_PORT:-7492}"
LOG_LEVEL="${SSD_INTAKE_LOG_LEVEL:-info}"

echo "=========================================================="
echo " Starting Proxmox SSD Intake Station (Web GUI)"
echo " Version: 1.1.0"
echo " Reports Directory: $REPORTS_DIR"
echo " Listening on:      http://$BIND_HOST:$BIND_PORT"
echo "=========================================================="

exec uvicorn app.main:app \
    --host "$BIND_HOST" \
    --port "$BIND_PORT" \
    --log-level "$LOG_LEVEL" \
    --no-access-log
