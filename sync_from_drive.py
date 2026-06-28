#!/usr/bin/env python3
"""
sync_from_drive.py — Automated backwards sync: Google Sheet → data.json

Reads the "All Music" tab of the Sheet Music Library spreadsheet directly
via the Google Sheets API and updates call_number, genre, and season in
data.json for all matched entries.

FIRST-TIME SETUP (one time only):
  pip3 install gspread google-auth --break-system-packages
  gcloud auth application-default login \
    --scopes=https://www.googleapis.com/auth/spreadsheets.readonly

  (If gcloud isn't installed: brew install --cask google-cloud-sdk)

AFTER SETUP — run any time:
  python3 sync_from_drive.py

Exit codes:
  0 — ran successfully, no changes needed
  2 — ran successfully, data.json was updated (caller should git commit)
  1 — error
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime

SHEET_ID  = '1LipZ8SiTWwhZl8UdIG56OgLw2oOtnxrPZme3xYn8Ijo'
TAB_NAME  = 'All Music'
DATA_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')

# ── Auth + fetch ─────────────────────────────────────────────────────────────

def fetch_rows():
    try:
        import gspread
        from google.auth import default
    except ImportError:
        print("ERROR: Missing dependencies. Run:")
        print("  pip3 install gspread google-auth --break-system-packages")
        sys.exit(1)

    try:
        creds, _ = default(scopes=[
            'https://www.googleapis.com/auth/spreadsheets.readonly',
            'https://www.googleapis.com/auth/drive.readonly',
        ])
    except Exception as e:
        print(f"ERROR: Google auth failed: {e}")
        print()
        print("Run this once to authenticate:")
        print("  gcloud auth application-default login \\")
        print("    --scopes=https://www.googleapis.com/auth/spreadsheets.readonly")
        print()
        print("(Install gcloud if needed: brew install --cask google-cloud-sdk)")
        sys.exit(1)

    try:
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(SHEET_ID)
        try:
            ws = spreadsheet.worksheet(TAB_NAME)
        except gspread.WorksheetNotFound:
            # Fall back to first sheet
            ws = spreadsheet.get_worksheet(0)
            print(f"Warning: tab '{TAB_NAME}' not found — using first sheet: {ws.title}")
        rows = ws.get_all_values()
        print(f"Fetched {len(rows)} rows from '{ws.title}'")
        return rows
    except Exception as e:
        print(f"ERROR: Could not read sheet: {e}")
        sys.exit(1)

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalise(s):
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def voicing_key(v):
    v = v.upper().strip()
    aliases = {
        '3-PART MIXED':'3PT','3 PART MIXED':'3PT',
        '3-PART':'3PT','3 PART':'3PT',
        '2-PART':'2PT','2 PART':'2PT','TWO-PART':'2PT','TWO PART':'2PT',
        'UNISON':'UNI','UNISON/TWO PART':'UNI',
        'MASTERWORK':'OTHER','OTHER':'OTHER','SAT(B)':'SATB',
    }
    return aliases.get(v, v)

def season_from_call(cn):
    if not cn:
        return ''
    parts = cn.split('-')
    return parts[0].upper() if parts else ''

# ── Load data.json ────────────────────────────────────────────────────────────

with open(DATA_JSON) as f:
    data = json.load(f)

all_entries = [e for section in ('work','personal') for e in data.get(section, [])]

lookup: dict[tuple, list] = {}
lookup3: dict[tuple, list] = {}
for entry in all_entries:
    tn = normalise(entry.get('title',''))
    vk = voicing_key(entry.get('voicing',''))
    cn = normalise(entry.get('composer', entry.get('author','')))
    lookup.setdefault((tn, vk), []).append(entry)
    lookup3.setdefault((tn, vk, cn), []).append(entry)

# ── Main ──────────────────────────────────────────────────────────────────────

rows = fetch_rows()
if not rows:
    print("Sheet appears empty.")
    sys.exit(1)

# Find header row (first row containing "call number" or "library")
header = None
data_rows = []
for i, row in enumerate(rows):
    joined = ' '.join(row).lower()
    if 'call number' in joined or ('library' in joined and 'type' in joined):
        header = [c.strip().lower() for c in row]
        data_rows = rows[i+1:]
        break

if header is None:
    # Assume positional defaults
    header = ['library','type','sort #','call number','title',
              'composer / author','voicing','genre','copies',
              'last performed','isbn','publisher','year','description','tags','link']
    data_rows = rows

def colidx(*names):
    for n in names:
        try: return header.index(n)
        except ValueError: pass
    return None

IDX_CALL  = colidx('call number','call #','callnumber')
IDX_TITLE = colidx('title')
IDX_COMP  = colidx('composer / author','composer','author','composer/author')
IDX_VOICE = colidx('voicing')
IDX_GENRE = colidx('genre')

def get(row, idx, default=''):
    if idx is None or idx >= len(row): return default
    return row[idx].strip()

print(f"Processing {len(data_rows)} data rows…")

updated = []
no_call = 0
no_match = []
ambiguous = []
already_same = 0

for row in data_rows:
    if not any(c.strip() for c in row):
        continue

    call_number = get(row, IDX_CALL)
    if not call_number:
        no_call += 1
        continue

    title    = get(row, IDX_TITLE)
    composer = get(row, IDX_COMP)
    voicing  = get(row, IDX_VOICE)
    genre    = get(row, IDX_GENRE)

    tn = normalise(title)
    vk = voicing_key(voicing)
    cn = normalise(composer)

    matches = lookup.get((tn, vk), [])

    if not matches:
        title_only = [e for (t,v), es in lookup.items() if t == tn for e in es]
        if len(title_only) == 1:
            matches = title_only
        elif len(title_only) > 1:
            matches = lookup3.get((tn, vk, cn), [])
            if not matches:
                ambiguous.append(f"{title} ({voicing})")
                continue
        else:
            no_match.append(f"{title} [{call_number}]")
            continue

    if len(matches) > 1:
        m3 = lookup3.get((tn, vk, cn), [])
        matches = m3[:1] if m3 else matches[:1]

    entry  = matches[0]
    season = season_from_call(call_number)
    changed = []

    if entry.get('call_number','') != call_number:
        changed.append(f"call# {entry.get('call_number','(none)')!r}→{call_number!r}")
        entry['call_number'] = call_number
    if genre and entry.get('genre','') != genre:
        changed.append(f"genre {entry.get('genre','(none)')!r}→{genre!r}")
        entry['genre'] = genre
    if season and entry.get('season','') != season:
        changed.append(f"season {entry.get('season','(none)')!r}→{season!r}")
        entry['season'] = season

    if changed:
        updated.append(f"  [{call_number}] {title} ({voicing}): {', '.join(changed)}")
    else:
        already_same += 1

# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'─'*55}")
print(f"  {len(updated)} updated  |  {already_same} already current  |  {no_call} no call# yet")
if no_match:
    print(f"  {len(no_match)} not found in data.json:")
    for m in no_match[:10]: print(f"    ✗ {m}")
    if len(no_match) > 10: print(f"    … and {len(no_match)-10} more")
if ambiguous:
    print(f"  {len(ambiguous)} ambiguous (skipped):")
    for a in ambiguous[:5]: print(f"    ? {a}")
for line in updated:
    print(line)
print(f"{'─'*55}")

if not updated:
    print("data.json is already up to date.")
    sys.exit(0)

# ── Write ─────────────────────────────────────────────────────────────────────

ts = datetime.now().strftime('%Y%m%d-%H%M%S')
backup = DATA_JSON.replace('data.json', f'data.backup.{ts}.json')
shutil.copy2(DATA_JSON, backup)
print(f"Backed up → {os.path.basename(backup)}")

with open(DATA_JSON, 'w') as f:
    json.dump(data, f, indent=2)

print(f"Wrote {os.path.basename(DATA_JSON)}")
sys.exit(2)   # exit code 2 = changes were made, caller should git commit
