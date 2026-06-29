#!/usr/bin/env python3
"""
sync_from_drive.py — Sheet → Website (no auth needed)

The Google Sheet is set to "Anyone with the link can view," so this script
downloads it as a CSV with zero authentication and zero extra packages.

Just run it:  python3 sync_from_drive.py

Exit codes: 0 = no changes, 2 = data.json was updated, 1 = error
"""

import csv, io, json, os, re, shutil, sys, time, urllib.request
from datetime import datetime

SHEET_ID  = '1LipZ8SiTWwhZl8UdIG56OgLw2oOtnxrPZme3xYn8Ijo'
# GID of the "All Music" tab (from the URL when that tab is active)
SHEET_GID = '2139739534'
DATA_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')

VOICING_MAP = {
    '3-part mixed':'3-Part Mixed','3 part mixed':'3-Part Mixed',
    '3-part':'3-Part','3 part':'3-Part',
    '2-part':'2-Part','2 part':'2-Part','two-part':'2-Part','two part':'2-Part',
    'unison':'Unison','unison/two part':'Unison',
    'sat(b)':'SATB','masterwork':'Other','other':'Other','various':'Other','any':'Other',
}
VALID = {'SATB','SSAATTBB','SSAA','SATTBB','SSATB','TTBB','SSA','TTB',
         'SAB','SA','TBB','3-Part Mixed','3-Part','2-Part','Unison','Other'}

def norm_voicing(v):
    if not v: return 'Other'
    lo = v.lower().strip()
    if lo in VOICING_MAP: return VOICING_MAP[lo]
    for x in VALID:
        if x.upper() == v.upper().strip(): return x
    return 'Other'

def normalise(s):
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def voicing_key(v):
    v = v.upper().strip()
    return {'3-PART MIXED':'3PT','3 PART MIXED':'3PT','3-PART':'3PT','3 PART':'3PT',
            '2-PART':'2PT','2 PART':'2PT','TWO-PART':'2PT','TWO PART':'2PT',
            'UNISON':'UNI','MASTERWORK':'OTHER','OTHER':'OTHER','SAT(B)':'SATB'}.get(v,v)

def season_from_call(cn):
    return cn.split('-')[0].upper() if cn else ''

# ── Download sheet as CSV ─────────────────────────────────────────────────────

url = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&id={SHEET_ID}&gid={SHEET_GID}'
print(f"Downloading sheet…")
try:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode('utf-8-sig')
except Exception as e:
    print(f"ERROR downloading sheet: {e}")
    print("Check your internet connection and that the sheet is still 'Anyone with the link can view'.")
    sys.exit(1)

rows = list(csv.reader(io.StringIO(raw)))
print(f"Got {len(rows)} rows from sheet")

# Find header
header = None
data_start = 0
for i, row in enumerate(rows):
    joined = ' '.join(row).lower()
    if 'call number' in joined or ('library' in joined and 'title' in joined):
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

all_entries = [e for s in ('work','personal') for e in data.get(s,[])]
lookup  = {}
lookup3 = {}
for entry in all_entries:
    tn = normalise(entry.get('title',''))
    vk = voicing_key(entry.get('voicing',''))
    cn = normalise(entry.get('composer', entry.get('author','')))
    lookup.setdefault((tn,vk),[]).append(entry)
    lookup3.setdefault((tn,vk,cn),[]).append(entry)

# ── Process ───────────────────────────────────────────────────────────────────

updated, added, no_call, no_match, same = [], [], 0, [], 0
_id = int(time.time()*1000)

for row in rows[data_start:]:
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

    tn, vk = normalise(title), voicing_key(voicing)
    cn      = normalise(composer)
    key, key3 = (tn,vk), (tn,vk,cn)

    matches = lookup.get(key,[])
    if not matches:
        title_only = [e for (t,v),es in lookup.items() if t==tn for e in es]
        if len(title_only)==1: matches = title_only
        elif len(title_only)>1:
            matches = lookup3.get(key3,[])
            if not matches: continue  # ambiguous
        else:
            # New entry — only add if it has a call number
            if not call_num:
                no_call += 1
                continue
            section = 'personal' if 'personal' in library else 'work'
            _type   = 'book' if 'book' in typ else 'octavo'
            _id += 1
            entry = {'_type':_type,'_id':_id,'title':title,'call_number':call_num,
                     'season':season_from_call(call_num),'genre':genre,
                     'voicing':norm_voicing(voicing),'copies':copies,
                     'lastPerformed':last,'link':link}
            if _type=='book':
                entry.update({'author':composer,'isbn':isbn,'description':''})
            else:
                entry['composer'] = composer
            data[section].insert(0, entry)
            lookup.setdefault((tn,voicing_key(norm_voicing(voicing))),[]).append(entry)
            added.append(f"  + [{call_num}] {title} ({voicing})")
            continue

    if len(matches)>1:
        m3 = lookup3.get(key3,[])
        matches = m3[:1] if m3 else matches[:1]

    entry = matches[0]
    season = season_from_call(call_num)
    changed = []
    if call_num and entry.get('call_number','') != call_num:
        changed.append(f"call# →{call_num!r}"); entry['call_number'] = call_num
    if genre and entry.get('genre','') != genre:
        changed.append(f"genre →{genre!r}"); entry['genre'] = genre
    if season and entry.get('season','') != season:
        changed.append(f"season →{season!r}"); entry['season'] = season
    if changed: updated.append(f"  ✓ {title}: {', '.join(changed)}")
    else: same += 1

# ── Report + write ────────────────────────────────────────────────────────────

print(f"\n{'─'*50}")
print(f"  {len(added)} new  |  {len(updated)} updated  |  {same} already current")
for l in added: print(l)
for l in updated: print(l)
if no_match: print(f"  {len(no_match)} not found in data.json")
print(f"{'─'*50}")

if not added and not updated:
    print("data.json is already up to date.")
    sys.exit(0)

ts = datetime.now().strftime('%Y%m%d-%H%M%S')
shutil.copy2(DATA_JSON, DATA_JSON.replace('data.json', f'data.backup.{ts}.json'))
with open(DATA_JSON, 'w') as f:
    json.dump(data, f, indent=2)
print(f"Saved. Run push-now.command to deploy.")
sys.exit(2)
