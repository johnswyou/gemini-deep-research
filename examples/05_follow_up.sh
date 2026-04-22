#!/usr/bin/env bash
# 05_follow_up.sh — ask a follow-up question using a prior interaction
# as context.
#
# `gdr follow-up <id> <question>` creates a new interaction with
# previous_interaction_id=<id>. The agent inherits the parent run's
# context so you don't have to re-state everything.
#
# Prereqs:
#   export GEMINI_API_KEY=AIza...
#
# Run:
#   bash examples/05_follow_up.sh [interaction_id]
#
# If no id is passed, we grab the most recent run from `gdr ls`.

set -euo pipefail

: "${GEMINI_API_KEY:?set GEMINI_API_KEY}"

INTERACTION_ID="${1:-}"

if [[ -z "$INTERACTION_ID" ]]; then
  # Pull the most recent completed interaction from local history.
  INTERACTION_ID=$(gdr ls --status completed --limit 1 --full-id \
    | awk 'NR > 2 {print $1; exit}')
  if [[ -z "$INTERACTION_ID" ]]; then
    echo "No prior interactions found. Run examples/01_basic.sh first." >&2
    exit 1
  fi
  echo "Using most recent completed interaction: $INTERACTION_ID"
fi

gdr follow-up "$INTERACTION_ID" \
  "Elaborate on section 3 with additional citations and a comparison table" \
  --no-confirm
