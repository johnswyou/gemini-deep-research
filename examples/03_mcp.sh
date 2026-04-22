#!/usr/bin/env bash
# 03_mcp.sh — attach an MCP server for a single research run.
#
# MCP (Model Context Protocol) servers let Deep Research call your own
# tools. This example wires up an Authorization header via env var.
# See docs/MCP.md for the full security model.
#
# Prereqs:
#   export GEMINI_API_KEY=AIza...
#   export DEPLOY_TOKEN=...            # your MCP server's bearer token
#
# Run:
#   bash examples/03_mcp.sh
#
# Expected: the request to Gemini includes an mcp_server tool with the
# Authorization header. In transcript.json that header shows up as
# [REDACTED] — the live wire value is never written to disk.

set -euo pipefail

: "${GEMINI_API_KEY:?set GEMINI_API_KEY}"
: "${DEPLOY_TOKEN:?set DEPLOY_TOKEN — the bearer token for your MCP endpoint}"

MCP_URL="https://mcp.example.com"   # replace with your server
MCP_NAME="deploys"

gdr research \
  --mcp "${MCP_NAME}=${MCP_URL}" \
  --mcp-header "${MCP_NAME}=Authorization:Bearer ${DEPLOY_TOKEN}" \
  --no-confirm \
  "Summarize our last 10 production deploys and flag any rollbacks"

# Dry-run variant — prints the full request JSON without calling the API.
# Useful to verify header assembly and check for injection-protection
# rejections before spending tokens.
#
#   gdr research --dry-run \
#     --api-key "$GEMINI_API_KEY" \
#     --mcp "${MCP_NAME}=${MCP_URL}" \
#     --mcp-header "${MCP_NAME}=Authorization:Bearer ${DEPLOY_TOKEN}" \
#     "Any query"
