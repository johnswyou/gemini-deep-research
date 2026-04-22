#!/usr/bin/env bash
# 04_multimodal.sh — attach local files and URLs to a research run.
#
# `--file` attaches a local file (base64-encoded) as input. Mime
# type is guessed from the extension and mapped to the API's coarse
# media kind (image / document / audio / video).
#
# `--url` adds url_context as a grounding source — the agent fetches
# the page and reasons over it.
#
# Prereqs:
#   export GEMINI_API_KEY=AIza...
#   ~/Downloads/10k.pdf        # replace with any PDF you want to ask about
#
# Run:
#   bash examples/04_multimodal.sh
#
# Note: passing --file or --url flips the run into untrusted-input
# mode by default (config.safe_untrusted = true). That strips
# code_execution and mcp_server tools from the request as a safety
# precaution against prompt injection from the attached content.
# See docs/MCP.md for how to opt out.

set -euo pipefail

: "${GEMINI_API_KEY:?set GEMINI_API_KEY}"

PDF="${1:-$HOME/Downloads/10k.pdf}"
if [[ ! -f "$PDF" ]]; then
  echo "Set a real PDF path, e.g. bash examples/04_multimodal.sh /path/to/file.pdf" >&2
  exit 1
fi

gdr research \
  --file "$PDF" \
  --url "https://en.wikipedia.org/wiki/10-K" \
  --no-confirm \
  "What are the key risk factors in this 10-K compared to the typical public-company filing?"
