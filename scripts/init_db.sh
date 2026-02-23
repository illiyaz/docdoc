#!/usr/bin/env bash
set -euo pipefail

echo "Running Alembic migrations..."
alembic upgrade head

echo "Loading protocols from config/protocols/..."
ls config/protocols/*.yaml 2>/dev/null && echo "Protocol files found." || echo "No protocol YAML files found (ok for dev)."

echo "Database ready."
