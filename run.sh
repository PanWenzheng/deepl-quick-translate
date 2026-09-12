#!/usr/bin/env bash
# 开发直跑：./run.sh [--toggle|--background|--settings|--quit|--verbose]
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m app "$@"
