#!/usr/bin/env bash
# 02_planning.sh — collaborative planning flow.
#
# `gdr research --plan` shows the agent's plan before spending tokens on
# execution. You approve / refine / cancel from an interactive prompt.
#
# Prereqs:
#   export GEMINI_API_KEY=AIza...
#
# Run:
#   bash examples/02_planning.sh
#
# Expected: plan appears in your terminal → you type A/R/C. On A, the
# execution phase runs and writes a report.

set -euo pipefail

: "${GEMINI_API_KEY:?set GEMINI_API_KEY before running}"

gdr research --plan "Competitive landscape of EV batteries: incumbents vs. new entrants"

# --- Cross-session variant ---
#
# You can also iterate on a plan across terminal sessions. Start a
# planned run, review the plan, and answer C (cancel) at the prompt —
# gdr prints the plan id so you can come back to it:
#
#   gdr research --plan "EV batteries"
#   # ... review plan, answer C ...
#   # Plan kept for later:  gdr plan approve <plan_id>
#
# Then refine later:
#
#   new_id=$(gdr plan refine "$plan_id" "focus on 2024 data" | tail -n 1)
#
# And approve when ready:
#
#   gdr plan approve "$new_id"
