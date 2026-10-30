#!/usr/bin/env bash
# Offline tests for hsp_qa (no network; the mock MCP server binds 127.0.0.1 only).
set -euo pipefail
cd "$(dirname "$0")"
exec "${PYTHON:-python3}" -I test_hsp_qa.py "$@"
