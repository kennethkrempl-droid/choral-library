#!/usr/bin/env python3
"""
sync_to_drive.py — Website → Sheet

Uses the OAuth credentials already stored in config.json + .tokens.json
(same credentials the web server uses). No gcloud or extra setup needed.

What it pushes:
  • New entries on the website not yet in the sheet → appended as new rows
  • copies / lastPerformed from website → updated in sheet cells
  (call numbers and genres flow the OTHER direction — sheet wins for those)

Exit codes: 0 = no changes, 2 = sheet updated, 1 = error
"""

import json, os, re, sys
from datetime import datetime

PROJ      = os.path.dirname(os.path.abspath(__file__))
DATA_JSON = os.path.join(PROJ, 'data.json')
CFG_PATH  = os.path.join(PROJ, 'config.json')
TOK_PATH  = os.path.join(PROJ, '.tokens.json')
TAB_NAME  = 'All Music'

# ── Auth ──────────────────────────────────────────────────────────────────────

try:
    import gspread
    import google.oauth2.credentials
    from google.auth.transport.requests import Request
except ImportError:
    print("ERROR: pip3 install gspread google-auth --break-system-packages")
    sys.exit(1)

try:
    with open(CFG_PATH) as f: cfg = json.load(f)
    with open(TOK_PATH) as f: tok = json.load(f)
except FileNotFoundError as e:
    print(f"ERROR: {e}")
    print("config.json and .tokens.json must be present in the project folder.")
    sys.exit(1)

creds = google.oauth2.credentials.Credentials(
    token=tok.get('access_token'),
    refresh_token=tok.get('refresh_token'),
    client_id=cfg['clientId'],
    client_secret=cfg['clientSecret'],
    token_uri='https://oauth2.googleapis.com/token',
    scopes=['https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive.file'],
)

# Refresh if expired
if creds.expired or not creds.valid:
    try:
        creds.refresh(Request())
        tok['access_token'] = creds.token
        with open(TOK_PATH, 'w') as f:
            json.dump(tok, f, indent=2)
        print("Token refreshed.")
    except Exception as e:
        print(f"ERROR refreshing token: {e}")
        print("Try re-authorizing via the website's /api/auth/google endpoint.")
        sys.exit(1)

try:
    gc = gspread.Client(auth=creds)
    spreadsheet = gc.open_by_key(cfg['spreadsheetId'])
    try:
        ws = spreadsheet.worksheet(TAB_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.get_worksheet(0)
        print(f"Note: tab '{TAB_NAME}' not found, using '{ws.title}'")
    all_rows = ws.get_all_values()
    print(f"Sheet loaded: {len(all_rows)} rows")
except Exception as e:
    print(f"ERROR connecting to sheet: {e}")
    sys.exit(1)

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalise(s):
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def voicing_key(v):
    v = v.upper().strip()
    return {'3-PART MIXED':'3PT','3 PART MIXED':'3PT','3-PART':'3PT','3 PART':'3PT',
            '2-PART':'2PT','2 PART':'2PT','TWO-PART':'2PT','TWO PART':'2PT',
            'UNISON':'UNI','UNISON/TWO PART':'UNI','MASTERWORK':'OTHER',
            'OTHER':'OTHER','SAT(B)':'SATB'}.get(v, v)

# ── Find header row ───────────────────────────────────────────────────────────

header, data_start = None, 0
for i, row in enumerate(all_rows):
    joined = ' '.join(row).lower()
    if 'call number' in joined or ('library' in joined and 'type' in joined):
        header = [c.strip().lower() for c in row]
        data_start = i + 1
        break
if not header:
    print("ERROR: could not find header row in sheet.")
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

sheet_lookup, sheet_lookup3 = {}, {}
for i, row in enumerate(all_rows[data_start:], start=data_start + 1):
    title = get(row, IDX_TITLE)
    if not title: continue
    key  = (normalise(title), voicing_key(get(row, IDX_VOICE)))
    key3 = (normalise(title), voicing_key(get(row, IDX_VOICE)), normalise(get(row, IDX_COMP)))
    sheet_lookup[key]   = (i, row)
    sheet_lookup3[key3] = (i, row)

# ── Load data.json ────────────────────────────────────────────────────────────

with open(DATA_JSON) as f:
    data = json.load(f)

# ── Compare ───────────────────────────────────────────────────────────────────

cell_updates = []   # (sheet_row, col_1based, value, label)
new_rows     = []   # (row_array, label)

def make_sheet_row(entry, section):
    row = [''] * len(header)
    def s(idx, val):
        if idx is not None and idx < len(row): row[idx] = val
    _type = entry.get('_type','octavo')
    s(IDX_LIB,   'Work' if section == 'work' else 'Personal')
    s(IDX_TYPE,  'Book' if _type == 'book' else 'Octavo')
    s(IDX_CALL,  entry.get('call_number',''))
    s(IDX_TITLE, entry.get('title',''))
    s(IDX_COMP,  entry.get('composer', entry.get('author','')))
    s(IDX_VOICE, entry.get('voicing',''))
    s(IDX_GENRE, entry.get('genre',''))
    s(IDX_COPY,  str(entry.get('copies','')))
    s(IDX_LAST,  entry.get('lastPerformed',''))
    s(IDX_LINK,  entry.get('link',''))
    s(IDX_ISBN,  entry.get('isbn',''))
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
        if not title: continue

        tn, vk, cn = normalise(title), voicing_key(voice), normalise(composer)
        key, key3  = (tn, vk), (tn, vk, cn)

        match = sheet_lookup.get(key) or sheet_lookup3.get(key3)
        if not match:
            title_only = [(rn,rd) for (t,v),(rn,rd) in sheet_lookup.items() if t == tn]
            if len(title_only) == 1: match = title_only[0]

        if not match:
            new_rows.append((make_sheet_row(entry, section), f"+ {title} ({voice})"))
            continue

        rn, sheet_row = match

        # copies: website → sheet
        if IDX_COPY is not None and copies:
            sc = get(sheet_row, IDX_COPY)
            if sc != copies:
                cell_updates.append((rn, IDX_COPY+1, copies,
                                     f"[r{rn}] {title}: copies {sc!r}→{copies!r}"))

        # lastPerformed: website → sheet
        if IDX_LAST is not None and last:
            sl = get(sheet_row, IDX_LAST)
            if sl != last:
                cell_updates.append((rn, IDX_LAST+1, last,
                                     f"[r{rn}] {title}: lastPerformed {sl!r}→{last!r}"))

        # call_number: push only if sheet is blank (sheet wins)
        if IDX_CALL is not None and call_num and not get(sheet_row, IDX_CALL):
            cell_updates.append((rn, IDX_CALL+1, call_num,
                                 f"[r{rn}] {title}: call# (blank)→{call_num!r}"))

        # genre: push only if sheet is blank
        if IDX_GENRE is not None and genre and not get(sheet_row, IDX_GENRE):
            cell_updates.append((rn, IDX_GENRE+1, genre,
                                 f"[r{rn}] {title}: genre (blank)→{genre!r}"))

# ── Report ────────────────────────────────────────────────────────────────────

print(f"\n{'─'*55}")
print(f"  {len(new_rows)} new rows  |  {len(cell_updates)} cell updates")
if new_rows:
    print("\nNew entries to add:")
    for _, lbl in new_rows: print(f"  {lbl}")
if cell_updates:
    print("\nCell updates:")
    for _,_,_,lbl in cell_updates[:20]: print(f"  ✓ {lbl}")
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
    ws.append_rows([r for r,_ in new_rows], value_input_option='USER_ENTERED')

print("\nDone — Google Sheet updated.")
sys.exit(2)
