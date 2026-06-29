#!/bin/bash
# push-to-sheet.command
# Double-click this to push new entries + copy counts from the website to Google Sheets.

set -e
cd "$(dirname "$0")"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Sheet Music Library — Push to Sheet"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Install gspread if needed (first run only)
if ! python3 -c "import gspread" 2>/dev/null; then
  echo "Installing gspread (one-time)…"
  pip3 install gspread google-auth --break-system-packages --quiet
fi

python3 sync_to_drive.py
EXIT=$?

if [ $EXIT -eq 2 ]; then
  echo ""
  echo "✓ Google Sheet updated successfully."
elif [ $EXIT -eq 0 ]; then
  echo ""
  echo "✓ Nothing to push — sheet is already up to date."
else
  echo ""
  echo "✗ Push failed. Check output above."
  exit 1
fi

echo ""
read -p "Press Enter to close…"
