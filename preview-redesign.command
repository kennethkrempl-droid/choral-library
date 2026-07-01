#!/bin/bash
# Preview the redesigned Choral Catalog locally (does NOT touch the live site).
cd "$(dirname "$0")"
git checkout redesign 2>/dev/null
echo ""
echo "  Starting local preview of the redesign…"
echo "  Opening http://localhost:3000 — press Ctrl+C here to stop."
echo ""
(sleep 2 && open http://localhost:3000) &
node server.js
