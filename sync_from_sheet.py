#!/usr/bin/env python3
"""
sync_from_sheet.py — Backwards sync: Google Sheet → data.json

Usage:
  1. In your Google Sheet → File → Download → Comma Separated Values (.csv)
     (This exports the FIRST/active sheet. Make sure "All Music" is the active tab.)
  2. python3 sync_from_sheet.py ~/Downloads/Sheet\ Music\ Library\ -\ All\ Music.csv

The script updates call_number, season, and genre for matched entries.
It prints a summary of every match, skip, and miss.
"""

import csv
import json
import shutil
import sys
import re
import os
from datetime import datetime

DATA_JSON = os.path.join(os.path.dirname(__file__), 'data.json')

# Maps call number prefix → season code stored on the entry.
# These are the codes the web app recognises in SEASON_COLORS.
# Any prefix not listed here is stored as-is (the card just won't be tinted).
KNOWN_PREFIXES = {
    'MT', 'POP', 'CON', 'HLD', 'EAS', 'SAC', 'PAT', 'FOL', 'SEA', 'MSC',
    # Sheet also uses these — we store them; cards won't be tinted for now
    'CLS', 'SPR', 'JAZ', 'SPC',
}

def normalise(s):
    """Lowercase, collapse whitespace, strip non-alphanumeric for fuzzy matching."""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def voicing_key(v):
    """Collapse common voicing aliases so they match across sheet and data.json."""
    v = v.upper().strip()
    aliases = {
        '3-PART MIXED': '3PT', '3 PART MIXED': '3PT',
        '3-PART': '3PT', '3 PART': '3PT',
        '2-PART': '2PT', '2 PART': '2PT', 'TWO-PART': '2PT', 'TWO PART': '2PT',
        'UNISON': 'UNI', 'UNISON/TWO PART': 'UNI',
        'MASTERWORK': 'OTHER', 'OTHER': 'OTHER',
        'SAT(B)': 'SATB',
    }
    return aliases.get(v, v)

def season_from_call(call_number):
    """Extract season prefix from call number like MT-SSA-0042 → 'MT'."""
    if not call_number:
        return ''
    parts = call_number.split('-')
    if parts:
        return parts[0].upper()
    return ''

# ── Load data.json ──────────────────────────────────────────────────────────

with open(DATA_JSON) as f:
    data = json.load(f)

# Build a lookup: (normalised_title, voicing_key) → list of entries
# We keep lists in case two pieces share a title+voicing (e.g., two SATB "Gloria").
lookup: dict[tuple, list] = {}
all_entries = []
for section in ('work', 'personal'):
    for entry in data.get(section, []):
        all_entries.append(entry)
        title_n = normalise(entry.get('title', ''))
        v_key   = voicing_key(entry.get('voicing', ''))
        key = (title_n, v_key)
        lookup.setdefault(key, []).append(entry)

# Also build composer-disambiguated index for duplicates
lookup_with_composer: dict[tuple, list] = {}
for entry in all_entries:
    title_n   = normalise(entry.get('title', ''))
    v_key     = voicing_key(entry.get('voicing', ''))
    comp_n    = normalise(entry.get('composer', entry.get('author', '')))
    key3 = (title_n, v_key, comp_n)
    lookup_with_composer.setdefault(key3, []).append(entry)

# ── Parse CSV ───────────────────────────────────────────────────────────────

if len(sys.argv) < 2:
    print("Usage: python3 sync_from_sheet.py <path-to-csv>")
    print()
    print("Export from Google Sheets: File → Download → Comma Separated Values (.csv)")
    sys.exit(1)

csv_path = os.path.expanduser(sys.argv[1])
if not os.path.exists(csv_path):
    print(f"ERROR: File not found: {csv_path}")
    sys.exit(1)

sheet_rows = []
with open(csv_path, newline='', encoding='utf-8-sig') as f:
    reader = csv.reader(f)
    header = None
    for row in reader:
        if not any(c.strip() for c in row):
            continue  # blank row
        if header is None:
            # Detect header row
            joined = ','.join(row).lower()
            if 'call number' in joined or 'library' in joined:
                header = [c.strip().lower() for c in row]
                print(f"Header detected: {header}")
                continue
            else:
                # No header yet — use positional defaults
                header = ['library','type','sort #','call number','title',
                          'composer / author','voicing','genre','copies',
                          'last performed','isbn','publisher','year',
                          'description','tags','link']
        sheet_rows.append(row)

print(f"\nParsed {len(sheet_rows)} data rows from {os.path.basename(csv_path)}")

# Column indices (robust to header variations)
def col(hdr, *names):
    for name in names:
        try: return hdr.index(name)
        except ValueError: pass
    return None

IDX_CALL   = col(header, 'call number', 'call #', 'callnumber')
IDX_TITLE  = col(header, 'title')
IDX_COMP   = col(header, 'composer / author', 'composer', 'author', 'composer/author')
IDX_VOICE  = col(header, 'voicing')
IDX_GENRE  = col(header, 'genre')
IDX_SORT   = col(header, 'sort #', 'sort#', 'sort')

def get(row, idx, default=''):
    if idx is None or idx >= len(row):
        return default
    return row[idx].strip()

# ── Sync ────────────────────────────────────────────────────────────────────

updated = []
skipped_no_call = []
skipped_no_match = []
skipped_ambiguous = []
skipped_already_same = []

for row in sheet_rows:
    call_number = get(row, IDX_CALL)
    title       = get(row, IDX_TITLE)
    composer    = get(row, IDX_COMP)
    voicing     = get(row, IDX_VOICE)
    genre       = get(row, IDX_GENRE)
    sort_num    = get(row, IDX_SORT)

    # Skip rows without a call number assigned
    if not call_number or not call_number.strip():
        skipped_no_call.append(title or f'(row: {row[:4]})')
        continue

    # Skip header-ish rows
    if call_number.lower() in ('call number', 'call #'):
        continue

    title_n   = normalise(title)
    v_key     = voicing_key(voicing)
    comp_n    = normalise(composer)
    key       = (title_n, v_key)
    key3      = (title_n, v_key, comp_n)

    # Try match
    matches = lookup.get(key, [])

    if not matches:
        # Try without voicing (fuzzy — title only)
        title_only = [e for k, v in lookup.items() if k[0] == title_n for e in v]
        if len(title_only) == 1:
            matches = title_only
        elif len(title_only) > 1:
            # Try disambiguating by composer
            matches = lookup_with_composer.get(key3, [])
            if not matches:
                skipped_ambiguous.append(f"{title} ({voicing}) — {len(title_only)} title-only matches, composer unclear")
                continue
        else:
            skipped_no_match.append(f"{title} [{call_number}]")
            continue

    if len(matches) > 1:
        # Disambiguate by composer
        comp_matches = lookup_with_composer.get(key3, [])
        if len(comp_matches) == 1:
            matches = comp_matches
        elif len(comp_matches) > 1:
            skipped_ambiguous.append(f"{title} ({voicing}) — {len(matches)} identical entries")
            continue
        else:
            # Just take the first one
            matches = matches[:1]

    entry = matches[0]
    season = season_from_call(call_number)

    changed = []
    if entry.get('call_number', '') != call_number:
        changed.append(f"call_number: {entry.get('call_number','')!r} → {call_number!r}")
        entry['call_number'] = call_number
    if genre and entry.get('genre', '') != genre:
        changed.append(f"genre: {entry.get('genre','')!r} → {genre!r}")
        entry['genre'] = genre
    if season and entry.get('season', '') != season:
        changed.append(f"season: {entry.get('season','')!r} → {season!r}")
        entry['season'] = season

    if changed:
        updated.append(f"  ✓ [{call_number}] {title} ({voicing}) — {', '.join(changed)}")
    else:
        skipped_already_same.append(f"{title} [{call_number}]")

# ── Report ──────────────────────────────────────────────────────────────────

print(f"\n{'═'*60}")
print(f"  SYNC RESULTS")
print(f"{'═'*60}")
print(f"\n✓ UPDATED ({len(updated)}):")
for line in updated:
    print(line)

print(f"\n⚡ ALREADY UP TO DATE ({len(skipped_already_same)}): (not shown)")

if skipped_no_call:
    print(f"\n○ NO CALL NUMBER YET ({len(skipped_no_call)}) — skipped:")
    for line in skipped_no_call[:20]:
        print(f"    {line}")
    if len(skipped_no_call) > 20:
        print(f"    ... and {len(skipped_no_call)-20} more")

if skipped_no_match:
    print(f"\n✗ NOT FOUND IN DATA.JSON ({len(skipped_no_match)}):")
    for line in skipped_no_match:
        print(f"    {line}")

if skipped_ambiguous:
    print(f"\n? AMBIGUOUS ({len(skipped_ambiguous)}):")
    for line in skipped_ambiguous:
        print(f"    {line}")

# ── Write ───────────────────────────────────────────────────────────────────

if not updated:
    print("\nNo changes to write.")
    sys.exit(0)

# Back up first
timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
backup_path = DATA_JSON.replace('data.json', f'data.backup.{timestamp}.json')
shutil.copy2(DATA_JSON, backup_path)
print(f"\nBacked up to: {os.path.basename(backup_path)}")

with open(DATA_JSON, 'w') as f:
    json.dump(data, f, indent=2)

print(f"Wrote {DATA_JSON}")
print(f"\nDone! {len(updated)} entries updated.")
print("Run push-now.command to deploy.")
