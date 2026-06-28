#!/usr/bin/env python3
"""
sync_from_drive.py — Sheet → Website

Pulls the Google Sheet into data.json:
  • NEW rows in the sheet that aren't on the website → added to data.json
  • Existing entries → call_number, genre, season updated from sheet

FIRST-TIME SETUP (one time only):
  pip3 install gspread google-auth --break-system-packages
  gcloud auth application-default login \
    --scopes=https://www.googleapis.com/auth/spreadsheets.readonly

Exit codes: 0 = no changes, 2 = data.json updated (caller should git commit), 1 = error
"""

import json, os, re, shutil, sys, time
from datetime import datetime

SHEET_ID  = '1LipZ8SiTWwhZl8UdIG56OgLw2oOtnxrPZme3xYn8Ijo'
TAB_NAME  = 'All Music'
DATA_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')

VOICING_MAP = {
    '3-part mixed':'3-Part Mixed','3 part mixed':'3-Part Mixed',
    '3-part':'3-Part','3 part':'3-Part',
    '2-part':'2-Part','2 part':'2-Part','two-part':'2-Part','two part':'2-Part',
    'unison':'Unison','unison/two part':'Unison',
    'sat(b)':'SATB',
    'masterwork':'Other','other':'Other','various':'Other','any':'Other',
}
VALID_VOICINGS = {
    'SATB','SSAATTBB','SSAA','SATTBB','SSATB','TTBB','TTBBB',
    'SSA','TTB','SAB','SA','TBB','3-Part Mixed','3-Part','2-Part','Unison','Other'
}

def norm_voicing(v):
    if not v: return 'Other'
    lo = v.lower().strip()
    if lo in VOICING_MAP: return VOICING_MAP[lo]
    up = v.upper().strip()
    for x in VALID_VOICINGS:
        if x.upper() == up: return x
    return 'Other'

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

def season_from_call(cn):
    if not cn: return ''
    return cn.split('-')[0].upper()

# ── Auth ──────────────────────────────────────────────────────────────────────

try:
    import gspread
    from google.auth import default
except ImportError:
    print("ERROR: pip3 install gspread google-auth --break-system-packages")
    sys.exit(1)

try:
    creds, _ = default(scopes=[
        'https://www.googleapis.com/auth/spreadsheets.readonly',
        'https://www.googleapis.com/auth/drive.readonly',
    ])
except Exception as e:
    print(f"ERROR: Auth failed: {e}")
    print("\nRun once to authenticate:")
    print("  gcloud auth application-default login \\")
    print("    --scopes=https://www.googleapis.com/auth/spreadsheets.readonly")
    print("\n(brew install --cask google-cloud-sdk  if gcloud isn't installed)")
    sys.exit(1)

try:
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(TAB_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.get_worksheet(0)
        print(f"Note: tab '{TAB_NAME}' not found — using '{ws.title}'")
    all_rows = ws.get_all_values()
    print(f"Fetched {len(all_rows)} rows from '{ws.title}'")
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
    header = ['library','type','sort #','call number','title',
              'composer / author','voicing','genre','copies',
              'last performed','isbn','publisher','year','description','tags','link']
    data_start = 0

def colidx(*names):
    for n in names:
        try: return header.index(n)
        except ValueError: pass
    return None

IDX_LIB   = colidx('library')
IDX_TYPE  = colidx('type')
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

# ── Load data.json ────────────────────────────────────────────────────────────

with open(DATA_JSON) as f:
    data = json.load(f)

all_entries = [e for s in ('work','personal') for e in data.get(s, [])]

lookup  = {}   # (title_n, vk) → [entries]
lookup3 = {}   # (title_n, vk, comp_n) → [entries]
for entry in all_entries:
    tn = normalise(entry.get('title',''))
    vk = voicing_key(entry.get('voicing',''))
    cn = normalise(entry.get('composer', entry.get('author','')))
    lookup.setdefault((tn,vk), []).append(entry)
    lookup3.setdefault((tn,vk,cn), []).append(entry)

# ── Process sheet rows ────────────────────────────────────────────────────────

updated   = []
added     = []
no_match  = []
ambiguous = []
same      = 0

_id_counter = int(time.time() * 1000)

for row in all_rows[data_start:]:
    if not any(c.strip() for c in row): continue

    title    = get(row, IDX_TITLE)
    composer = get(row, IDX_COMP)
    voicing  = get(row, IDX_VOICE)
    genre    = get(row, IDX_GENRE)
    call_num = get(row, IDX_CALL)
    copies   = get(row, IDX_COPY)
    last     = get(row, IDX_LAST)
    library  = get(row, IDX_LIB, 'work').lower()
    typ      = get(row, IDX_TYPE, 'octavo').lower()
    link     = get(row, IDX_LINK)
    isbn     = get(row, IDX_ISBN)

    if not title: continue

    tn = normalise(title)
    vk = voicing_key(voicing)
    cn = normalise(composer)
    key  = (tn, vk)
    key3 = (tn, vk, cn)

    # ── Find match in data.json ──
    matches = lookup.get(key, [])
    if not matches:
        title_only = [e for (t,v),es in lookup.items() if t == tn for e in es]
        if len(title_only) == 1:
            matches = title_only
        elif len(title_only) > 1:
            matches = lookup3.get(key3, [])
            if not matches:
                ambiguous.append(f"{title} ({voicing})")
                continue
        else:
            # ── NEW entry: add to data.json ──
            if not call_num:
                # Skip if no call number yet — not ready
                no_match.append(f"{title} [{voicing}] (no call# — not added yet)")
                continue

            section = 'personal' if 'personal' in library else 'work'
            _type   = 'book' if 'book' in typ else 'octavo'
            _id_counter += 1

            new_entry = {
                '_type': _type,
                '_id': _id_counter,
                'title': title,
                'call_number': call_num,
                'season': season_from_call(call_num),
                'genre': genre,
                'voicing': norm_voicing(voicing),
                'copies': copies,
                'lastPerformed': last,
                'link': link,
            }
            if _type == 'book':
                new_entry['author']      = composer
                new_entry['isbn']        = isbn
                new_entry['description'] = ''
            else:
                new_entry['composer'] = composer

            data[section].insert(0, new_entry)
            # Update lookup so duplicates in sheet don't re-add
            ntn = normalise(title)
            nvk = voicing_key(norm_voicing(voicing))
            lookup.setdefault((ntn, nvk), []).append(new_entry)
            lookup3.setdefault((ntn, nvk, normalise(composer)), []).append(new_entry)
            added.append(f"  + [{call_num}] {title} ({voicing}) → {section}")
            continue

    if len(matches) > 1:
        m3 = lookup3.get(key3, [])
        matches = m3[:1] if m3 else matches[:1]

    entry  = matches[0]
    season = season_from_call(call_num)
    changed = []

    if call_num and entry.get('call_number','') != call_num:
        changed.append(f"call# →{call_num!r}")
        entry['call_number'] = call_num
    if genre and entry.get('genre','') != genre:
        changed.append(f"genre →{genre!r}")
        entry['genre'] = genre
    if season and entry.get('season','') != season:
        changed.append(f"season →{season!r}")
        entry['season'] = season

    if changed:
        updated.append(f"  ✓ {title} ({voicing}): {', '.join(changed)}")
    else:
        same += 1

# ── Report ────────────────────────────────────────────────────────────────────

print(f"\n{'─'*55}")
print(f"  {len(added)} new  |  {len(updated)} updated  |  {same} already current")
if added:
    print("\nNEW entries added to data.json:")
    for l in added: print(l)
if updated:
    print("\nUPDATED entries:")
    for l in updated: print(l)
if no_match:
    print(f"\nSkipped {len(no_match)} sheet rows (no call# yet or unresolvable):")
    for l in no_match[:10]: print(f"  - {l}")
if ambiguous:
    print(f"\nAmbiguous (skipped): {len(ambiguous)}")
print(f"{'─'*55}")

if not added and not updated:
    print("data.json is already up to date.")
    sys.exit(0)

ts = datetime.now().strftime('%Y%m%d-%H%M%S')
backup = DATA_JSON.replace('data.json', f'data.backup.{ts}.json')
shutil.copy2(DATA_JSON, backup)
print(f"Backed up → {os.path.basename(backup)}")

with open(DATA_JSON, 'w') as f:
    json.dump(data, f, indent=2)
print(f"Wrote {os.path.basename(DATA_JSON)}")
sys.exit(2)
