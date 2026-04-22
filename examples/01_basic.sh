#!/usr/bin/env bash
# 01_basic.sh — the shortest path to a Deep Research report.
#
# Prereqs:
#   export GEMINI_API_KEY=AIza...
#
# Run:
#   bash examples/01_basic.sh
#
# Expected: live thought summaries stream to your terminal, then a
# report.md path is printed. Total runtime: ~2-5 minutes with the
# fast agent.

set -euo pipefail

: "${GEMINI_API_KEY:?set GEMINI_API_KEY before running — see https://aistudio.google.com/apikey}"

QUERY="Latest trends in RISC-V adoption for server workloads"

# --no-confirm lets this run headless; drop it for interactive use.
gdr research "$QUERY" --no-confirm
