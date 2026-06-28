#!/bin/bash
# sync-sheet-now.command
# Double-click this to pull the latest call numbers + genres from Google Sheets
# and deploy to the live site.

set -e
cd "$(dirname "$0")"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Sheet Music Library — Sheet Sync"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

python3 sync_from_drive.py
EXIT=$?

if [ $EXIT -eq 2 ]; then
  echo ""
  echo "Committing changes…"
  git add data.json data.backup.*.json 2>/dev/null || true
  git add data.json
  git commit -m "Sync call numbers + genres from Google Sheet ($(date '+%Y-%m-%d %H:%M'))"
  echo ""
  echo "Pushing to GitHub → Render…"
  git push
  echo ""
  echo "✓ Done! Changes will be live on Render in ~1 minute."
elif [ $EXIT -eq 0 ]; then
  echo ""
  echo "✓ Nothing changed — data.json is already in sync."
else
  echo ""
  echo "✗ Sync failed. Check output above."
  exit 1
fi

echo ""
read -p "Press Enter to close…"
