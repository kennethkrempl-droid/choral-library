#!/usr/bin/env python3
"""
sync_to_drive.py — Website → Sheet

Pushes data.json into the Google Sheet:
  • NEW entries on the website that aren't in the sheet → added as new rows
  • Existing entries → copies and lastPerformed updated from website

(Call numbers and genres flow the OTHER direction: sheet → website.
 This script won't overwrite a call number that already exists in the sheet.)

FIRST-TIME SETUP (one time only):
  pip3 install gspread google-auth --break-system-packages
  gcloud auth application-default login \
    --scopes=https://www.googleapis.com/auth/spreadsheets

Exit codes: 0 = no changes, 2 = sheet updated, 1 = error
"""

import json, os, re, sys
from datetime import datetime

SHEET_ID  = '1LipZ8SiTWwhZl8UdIG56OgLw2oOtnxrPZme3xYn8Ijo'
TAB_NAME  = 'All Music'
DATA_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')

def normalise(s):
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def voicing_key(v):
    v = v.upper().strip()
    aliases = {
        '3-PART MIXED':'3PT','3 PART MIXED':'3PT','3-PART':'3PT','3 PART':'3PT',
        '2-PART':'2PT','2 PART':'2PT','TWO-PART':'2PT','TWO PART':'2PT',
        'UNISON':'UNI','UNISON/TWO PART':'UNI',
        'MASTERWORK':'OTHER','OTHER':'OTHER','SAT(B)':'SATB',
    }
    return aliases.get(v, v)

# ── Auth ──────────────────────────────────────────────────────────────────────

try:
    import gspread
    from google.auth import default
except ImportError:
    print("ERROR: pip3 install gspread google-auth --break-system-packages")
    sys.exit(1)

try:
    creds, _ = default(scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ])
except Exception as e:
    print(f"ERROR: Auth failed: {e}")
    print("\nRun once to set up write access:")
    print("  gcloud auth application-default login \\")
    print("    --scopes=https://www.googleapis.com/auth/spreadsheets")
    sys.exit(1)

try:
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(TAB_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.get_worksheet(0)
        print(f"Note: using sheet '{ws.title}'")
    all_rows = ws.get_all_values()
    print(f"Sheet loaded: {len(all_rows)} rows")
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

# ── Find header ───────────────────────────────────────────────────────────────

header = None
data_start = 0
for i, row in enumerate(all_rows):
    joined = ' '.join(row).lower()
    if 'call number' in joined or ('library' in joined and 'type' in joined):
        header = [c.strip().lower() for c in row]
        data_start = i + 1
        break

if not header:
    print("ERROR: Could not find header row in sheet.")
    sys.exit(1)

def colidx(*names):
    for n in names:
        try: return header.index(n)
        except ValueError: pass
    return None

IDX_LIB   = colidx('library')
IDX_TYPE  = colidx('type')
IDX_SORT  = colidx('sort #','sort#')
IDX_CALL  = colidx('call number','call #')
IDX_TITLE = colidx('title')
IDX_COMP  = colidx('composer / author','composer','author')
IDX_VOICE = colidx('voicing')
IDX_GENRE = colidx('genre')
IDX_COPY  = colidx('copies','# of copies','# copies')
IDX_LAST  = colidx('last performed','last year performed')
IDX_LINK  = colidx('link')
IDX_ISBN  = colidx('isbn')

def get(row, idx, default=''):
    if idx is None or idx >= len(row): return default
    return row[idx].strip()

# ── Build sheet lookup ────────────────────────────────────────────────────────

sheet_lookup  = {}  # (title_n, vk) → (1-based row #, row data)
sheet_lookup3 = {}  # (title_n, vk, comp_n) → (1-based row #, row data)

for i, row in enumerate(all_rows[data_start:], start=data_start + 1):
    title = get(row, IDX_TITLE)
    voice = get(row, IDX_VOICE)
    comp  = get(row, IDX_COMP)
    if not title: continue
    key  = (normalise(title), voicing_key(voice))
    key3 = (normalise(title), voicing_key(voice), normalise(comp))
    sheet_lookup[key]  = (i, row)
    sheet_lookup3[key3] = (i, row)

# ── Load data.json ────────────────────────────────────────────────────────────

with open(DATA_JSON) as f:
    data = json.load(f)

# ── Compare ───────────────────────────────────────────────────────────────────

cell_updates = []   # (row, col_1based, value, label)
new_rows     = []   # list of row arrays to append
no_match     = []

def make_sheet_row(entry, section):
    """Build a full row array matching the sheet's column layout."""
    _type    = entry.get('_type','octavo')
    title    = entry.get('title','')
    composer = entry.get('composer', entry.get('author',''))
    voicing  = entry.get('voicing','')
    genre    = entry.get('genre','')
    copies   = str(entry.get('copies',''))
    last     = entry.get('lastPerformed','')
    call_num = entry.get('call_number','')
    link     = entry.get('link','')
    isbn     = entry.get('isbn','')

    # Build row as long as the header
    row = [''] * len(header)
    def setcol(idx, val):
        if idx is not None and idx < len(row):
            row[idx] = val

    setcol(IDX_LIB,   'Work' if section == 'work' else 'Personal')
    setcol(IDX_TYPE,  'Book' if _type == 'book' else 'Octavo')
    setcol(IDX_CALL,  call_num)
    setcol(IDX_TITLE, title)
    setcol(IDX_COMP,  composer)
    setcol(IDX_VOICE, voicing)
    setcol(IDX_GENRE, genre)
    setcol(IDX_COPY,  copies)
    setcol(IDX_LAST,  last)
    setcol(IDX_LINK,  link)
    setcol(IDX_ISBN,  isbn)
    return row

for section in ('work', 'personal'):
    for entry in data.get(section, []):
        title    = entry.get('title','')
        voice    = entry.get('voicing','')
        composer = entry.get('composer', entry.get('author',''))
        copies   = str(entry.get('copies',''))
        last     = entry.get('lastPerformed','')
        call_num = entry.get('call_number','')
        genre    = entry.get('genre','')

        tn  = normalise(title)
        vk  = voicing_key(voice)
        cn  = normalise(composer)
        key  = (tn, vk)
        key3 = (tn, vk, cn)

        # Find in sheet
        match = sheet_lookup.get(key) or sheet_lookup3.get(key3)
        if not match:
            # Title-only fallback
            title_only = [(rn, rd) for (t,v),(rn,rd) in sheet_lookup.items() if t == tn]
            if len(title_only) == 1:
                match = title_only[0]

        if not match:
            # NEW entry: add as new sheet row
            new_rows.append((make_sheet_row(entry, section),
                             f"+ {title} ({voice})"))
            continue

        sheet_row_num, sheet_row = match

        # Push copies if website differs from sheet
        if IDX_COPY is not None and copies:
            sc = get(sheet_row, IDX_COPY)
            if sc != copies:
                cell_updates.append((sheet_row_num, IDX_COPY + 1, copies,
                                     f"[r{sheet_row_num}] {title}: copies {sc!r}→{copies!r}"))

        # Push lastPerformed
        if IDX_LAST is not None and last:
            sl = get(sheet_row, IDX_LAST)
            if sl != last:
                cell_updates.append((sheet_row_num, IDX_LAST + 1, last,
                                     f"[r{sheet_row_num}] {title}: lastPerformed {sl!r}→{last!r}"))

        # Push call_number ONLY if sheet cell is blank (sheet wins if it has one)
        if IDX_CALL is not None and call_num:
            sc = get(sheet_row, IDX_CALL)
            if not sc:
                cell_updates.append((sheet_row_num, IDX_CALL + 1, call_num,
                                     f"[r{sheet_row_num}] {title}: call# (blank)→{call_num!r}"))

        # Push genre ONLY if sheet cell is blank
        if IDX_GENRE is not None and genre:
            sg = get(sheet_row, IDX_GENRE)
            if not sg:
                cell_updates.append((sheet_row_num, IDX_GENRE + 1, genre,
                                     f"[r{sheet_row_num}] {title}: genre (blank)→{genre!r}"))

# ── Report ────────────────────────────────────────────────────────────────────

print(f"\n{'─'*55}")
print(f"  {len(new_rows)} new rows  |  {len(cell_updates)} cell updates")
if new_rows:
    print("\nNEW entries to add to sheet:")
    for _, label in new_rows: print(f"  {label}")
if cell_updates:
    print("\nUpdates:")
    for _,_,_,label in cell_updates[:20]: print(f"  ✓ {label}")
    if len(cell_updates) > 20: print(f"  … and {len(cell_updates)-20} more")
print(f"{'─'*55}")

if not new_rows and not cell_updates:
    print("Sheet is already up to date with the website.")
    sys.exit(0)

# ── Write ─────────────────────────────────────────────────────────────────────

if cell_updates:
    print(f"\nUpdating {len(cell_updates)} cells…")
    for rn, cn, val, _ in cell_updates:
        ws.update_cell(rn, cn, val)

if new_rows:
    print(f"\nAppending {len(new_rows)} new rows…")
    rows_to_append = [r for r, _ in new_rows]
    ws.append_rows(rows_to_append, value_input_option='USER_ENTERED')

print(f"\nDone — Google Sheet updated.")
sys.exit(2)
