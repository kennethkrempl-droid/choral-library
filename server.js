const express    = require('express');
const fs         = require('fs');
const path       = require('path');
const https      = require('https');
const crypto     = require('crypto');
const os         = require('os');
const nodemailer = require('nodemailer');
const { google } = require('googleapis');
const db         = require('./db');

const app  = express();
const PORT = process.env.PORT || 3000;

app.set('trust proxy', true);
app.use(express.json({ limit: '10mb' }));
app.use(express.static(path.join(__dirname, 'public')));

// ── Helpers ───────────────────────────────────────────────────────────────────

function getLocalIPs() {
  const nets = os.networkInterfaces(); const ips = [];
  for (const n of Object.keys(nets))
    for (const net of nets[n])
      if (net.family === 'IPv4' && !net.internal) ips.push(net.address);
  return ips;
}

function httpsGet(url, headers = {}) {
  return new Promise((resolve, reject) => {
    https.get(url, { headers: { 'User-Agent': 'Mozilla/5.0 (SheetMusicCatalog/2.0)', 'Accept': 'application/json,text/html', ...headers } }, res => {
      if (res.statusCode >= 301 && res.statusCode <= 308 && res.headers.location) {
        const next = new URL(res.headers.location, url).href;
        res.resume();
        return httpsGet(next, headers).then(resolve, reject);
      }
      let body = ''; res.on('data', c => body += c); res.on('end', () => resolve({ status: res.statusCode, body }));
    }).on('error', reject);
  });
}

function httpsPost(url, bodyObj, headers = {}) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(bodyObj);
    const u = new URL(url);
    const req = https.request({
      hostname: u.hostname, path: u.pathname + u.search, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data), ...headers },
    }, res => { let b = ''; res.on('data', c => b += c); res.on('end', () => resolve({ status: res.statusCode, body: b })); });
    req.on('error', reject);
    req.write(data); req.end();
  });
}

function baseUrl(req) {
  if (process.env.APP_URL) return process.env.APP_URL.replace(/\/$/, '');
  return `${req.protocol}://${req.get('host')}`;
}

// Effective config = stored config, with environment variables taking precedence
// (so secrets never need to live in the database/repo for hosted deployments).
async function effectiveConfig() {
  const cfg = await db.getConfig();
  return {
    ...cfg,
    clientId:      process.env.GOOGLE_CLIENT_ID     || cfg.clientId,
    clientSecret:  process.env.GOOGLE_CLIENT_SECRET || cfg.clientSecret,
    adminPassword: process.env.ADMIN_PASSWORD       || cfg.adminPassword,
    geminiApiKey:  process.env.GEMINI_API_KEY       || cfg.geminiApiKey,
    notifyEmail:   process.env.NOTIFY_EMAIL         || cfg.notifyEmail,
    smtpUser:      process.env.SMTP_USER            || cfg.smtpUser,
    smtpPass:      process.env.SMTP_PASS            || cfg.smtpPass,
    smtpHost:      process.env.SMTP_HOST            || cfg.smtpHost,
    smtpPort:      parseInt(process.env.SMTP_PORT)  || cfg.smtpPort,
  };
}

// ── Admin auth ────────────────────────────────────────────────────────────────

let _secretCache = null;
async function getServerSecret() {
  if (process.env.SECRET) return process.env.SECRET;
  if (_secretCache) return _secretCache;
  const cfg = await db.getConfig();
  if (!cfg._secret) { cfg._secret = crypto.randomBytes(32).toString('hex'); await db.saveConfig(cfg); }
  _secretCache = cfg._secret;
  return _secretCache;
}
async function makeAdminToken(password) {
  return crypto.createHmac('sha256', await getServerSecret()).update(password).digest('hex');
}
async function isAdminRequest(req) {
  const token = (req.headers.authorization || '').replace('Bearer ', '').trim();
  if (!token) return false;
  const cfg = await effectiveConfig();
  if (!cfg.adminPassword) return true;
  const expected = await makeAdminToken(cfg.adminPassword);
  return token.length === expected.length &&
    crypto.timingSafeEqual(Buffer.from(token), Buffer.from(expected));
}
function adminRequired(req, res, next) {
  isAdminRequest(req).then(ok => ok ? next() : res.status(403).json({ error: 'Admin access required.' }))
    .catch(e => res.status(500).json({ error: e.message }));
}

// ── Data ──────────────────────────────────────────────────────────────────────

app.get('/api/data', async (req, res) => {
  try { res.json(await db.getData()); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

// Legacy whole-document replace (kept for compatibility)
app.post('/api/data', adminRequired, async (req, res) => {
  try { await db.replaceData(req.body); res.json({ success: true }); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

// Granular entry endpoints
app.post('/api/entries/:tab', adminRequired, async (req, res) => {
  try {
    const tab = req.params.tab;
    if (!['work', 'personal'].includes(tab)) return res.status(400).json({ error: 'Invalid tab' });
    const entry = { ...req.body, _id: req.body._id || Date.now() };
    if (!entry.title || !String(entry.title).trim()) return res.status(400).json({ error: 'Title is required' });
    // Guard against column-shift corruption: composer should never equal title exactly
    if (entry.composer && entry.title && entry.composer.trim() === entry.title.trim()) {
      return res.status(400).json({ error: `Data integrity error: composer ("${entry.composer}") equals title — check column mapping in your import.` });
    }
    await db.addEntry(tab, entry);
    res.json({ success: true, entry });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.put('/api/entries/:tab/:id', adminRequired, async (req, res) => {
  try {
    const body = req.body;
    // Guard against column-shift corruption: composer should never equal title exactly
    if (body.composer && body.title && body.composer.trim() === body.title.trim()) {
      return res.status(400).json({ error: `Data integrity error: composer ("${body.composer}") equals title — check column mapping in your import.` });
    }
    const updated = await db.updateEntry(req.params.tab, parseInt(req.params.id), body);
    if (!updated) return res.status(404).json({ error: 'Entry not found' });
    res.json({ success: true, entry: updated });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/api/entries/:tab/bulk', adminRequired, async (req, res) => {
  try {
    const { action, ids, patch } = req.body;
    if (!Array.isArray(ids) || !ids.length) return res.status(400).json({ error: 'ids required' });
    let n = 0;
    if (action === 'delete')     n = await db.deleteEntries(req.params.tab, ids);
    else if (action === 'patch') n = await db.patchEntries(req.params.tab, ids, patch || {});
    else return res.status(400).json({ error: 'Unknown action' });
    res.json({ success: true, count: n });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.delete('/api/entries/:tab/:id', adminRequired, async (req, res) => {
  try {
    const n = await db.deleteEntries(req.params.tab, [parseInt(req.params.id)]);
    res.json({ success: true, count: n });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ── Admin ─────────────────────────────────────────────────────────────────────

app.get('/api/admin-status', async (req, res) => {
  try {
    const cfg = await effectiveConfig();
    res.json({ hasPassword: !!cfg.adminPassword, isAdmin: await isAdminRequest(req) });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/api/admin-login', async (req, res) => {
  try {
    const { password } = req.body; const cfg = await effectiveConfig();
    if (!cfg.adminPassword) return res.json({ token: 'open', open: true });
    if (password !== cfg.adminPassword) return res.status(401).json({ error: 'Incorrect password.' });
    res.json({ token: await makeAdminToken(password) });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/api/admin-set-password', adminRequired, async (req, res) => {
  try {
    if (process.env.ADMIN_PASSWORD)
      return res.status(400).json({ error: 'Password is managed by the ADMIN_PASSWORD environment variable on the server.' });
    const cfg = await db.getConfig();
    if (req.body.password) cfg.adminPassword = req.body.password;
    else delete cfg.adminPassword;
    await db.saveConfig(cfg);
    res.json({ success: true });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ── ISBN Lookup (Google Books primary, Open Library fallback) ─────────────────

app.get('/api/isbn-lookup', async (req, res) => {
  const isbn = (req.query.isbn || '').replace(/[-\s]/g, '');
  if (!isbn) return res.status(400).json({ error: 'ISBN required' });
  try {
    // 1) Google Books
    try {
      const { body } = await httpsGet(`https://www.googleapis.com/books/v1/volumes?q=isbn:${isbn}`);
      const j = JSON.parse(body);
      if (j.totalItems > 0 && j.items?.length) {
        const v = j.items[0].volumeInfo || {};
        return res.json({
          source: 'Google Books',
          title: [v.title, v.subtitle].filter(Boolean).join(': '),
          author: (v.authors || []).join(', '),
          description: v.description || '',
          publisher: v.publisher || '',
          year: (v.publishedDate || '').slice(0, 4),
          pageCount: v.pageCount || '',
          link: v.infoLink || v.canonicalVolumeLink || '',
          thumbnail: (v.imageLinks?.thumbnail || '').replace(/^http:/, 'https:'),
        });
      }
    } catch (e) { console.error('Google Books lookup failed:', e.message); }

    // 2) Open Library fallback
    const { body: olBody } = await httpsGet(`https://openlibrary.org/api/books?bibkeys=ISBN:${isbn}&format=json&jscmd=data`);
    const olJson = JSON.parse(olBody); const key = `ISBN:${isbn}`;
    if (olJson[key]) {
      const b = olJson[key];
      return res.json({
        source: 'Open Library',
        title: b.title || '', author: (b.authors || []).map(a => a.name).join(', '),
        description: typeof b.notes === 'string' ? b.notes : (b.notes?.value || ''),
        publisher: (b.publishers || []).map(p => p.name).join(', '),
        year: (b.publish_date || '').slice(-4),
        link: b.url || '', thumbnail: b.cover?.medium || '',
      });
    }
    const { body: srchBody } = await httpsGet(`https://openlibrary.org/search.json?isbn=${isbn}&limit=1`);
    const srchJson = JSON.parse(srchBody);
    if (srchJson.docs?.length) {
      const d = srchJson.docs[0];
      return res.json({
        source: 'Open Library',
        title: d.title || '', author: (d.author_name || []).join(', '),
        description: '', publisher: (d.publisher || []).slice(0, 2).join(', '),
        year: String(d.first_publish_year || ''), link: '', thumbnail: '',
      });
    }
    res.status(404).json({ error: 'No book found for that ISBN.' });
  } catch (e) { res.status(500).json({ error: 'Lookup failed: ' + e.message }); }
});

// ── JW Pepper Lookup (title + composer) ───────────────────────────────────────

const VOICING_RE = /\b(SSAATTBB|SSATB|SATTBB|SSAA|SATB|SSAB|SSA|SAB|SA|TTBB|TTB|TBB|TB|Unison|2-Part|3-Part Mixed|3-Part|Two-Part)\b/i;

function parsePepperProducts(arr) {
  return (arr || []).slice(0, 8).map(p => {
    const props = {};
    (p.properties || []).forEach(x => { props[x.name] = (x.values || []).join(', '); });
    const itemNames = (p.items || []).map(i => i.name || i.nameComplete || '').filter(Boolean);
    const voicings = [...new Set(itemNames.map(n => (n.match(VOICING_RE) || [])[1]).filter(Boolean)
      .map(v => v.toUpperCase().replace('TWO-PART', '2-Part')))];
    const img = p.items?.[0]?.images?.[0]?.imageUrl || '';
    return {
      title: p.productName || '',
      composer: props.Composer || props.Arranger || props['Composer/Arranger'] || p[`Composer`]?.[0] || '',
      publisher: p.brand || '',
      voicings,
      description: (p.description || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 400),
      url: p.linkText ? `https://www.jwpepper.com/${p.linkText}/p` : (p.link || ''),
      image: img,
    };
  }).filter(r => r.title && r.url);
}

app.get('/api/pepper-lookup', async (req, res) => {
  const title = (req.query.title || '').trim();
  const composer = (req.query.composer || '').trim();
  if (!title) return res.status(400).json({ error: 'Title required' });
  const query = encodeURIComponent([title, composer].filter(Boolean).join(' '));
  try {
    // 1) VTEX intelligent search API
    try {
      const { status, body } = await httpsGet(`https://www.jwpepper.com/api/io/_v/api/intelligent-search/product_search/?query=${query}&count=8`);
      if (status === 200) {
        const j = JSON.parse(body);
        const results = parsePepperProducts(j.products);
        if (results.length) return res.json({ results });
      }
    } catch (e) { console.error('Pepper intelligent-search failed:', e.message); }

    // 2) VTEX legacy catalog search API
    try {
      const { status, body } = await httpsGet(`https://www.jwpepper.com/api/catalog_system/pub/products/search/?ft=${query}&_from=0&_to=7`);
      if (status === 200 || status === 206) {
        const arr = JSON.parse(body);
        const results = (arr || []).slice(0, 8).map(p => {
          const voicings = [...new Set((p.items || []).map(i => (String(i.name || '').match(VOICING_RE) || [])[1])
            .filter(Boolean).map(v => v.toUpperCase().replace('TWO-PART', '2-Part')))];
          return {
            title: p.productName || '',
            composer: (p.Composer || p.Arranger || [])[0] || '',
            publisher: p.brand || '',
            voicings,
            description: (p.description || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 400),
            url: p.linkText ? `https://www.jwpepper.com/${p.linkText}/p` : '',
            image: p.items?.[0]?.images?.[0]?.imageUrl || '',
          };
        }).filter(r => r.title && r.url);
        if (results.length) return res.json({ results });
      }
    } catch (e) { console.error('Pepper catalog search failed:', e.message); }

    res.status(404).json({ error: 'No JW Pepper results found. Try fewer words or check spelling.' });
  } catch (e) { res.status(500).json({ error: 'JW Pepper lookup failed: ' + e.message }); }
});

// ── Multi-source link finder (Pepper → CPDL → search URLs) ──────────────────
app.get('/api/find-links', async (req, res) => {
  const title    = (req.query.title    || '').trim();
  const composer = (req.query.composer || '').trim();
  if (!title) return res.status(400).json({ error: 'Title required' });

  const query = [title, composer].filter(Boolean).join(' ');
  const qEnc  = encodeURIComponent(query);
  const results = []; // [{ source, items }]

  // 1. JW Pepper (primary)
  try {
    let pepperItems = [];
    try {
      const { status, body } = await httpsGet(
        `https://www.jwpepper.com/api/io/_v/api/intelligent-search/product_search/?query=${qEnc}&count=6`
      );
      if (status === 200) pepperItems = parsePepperProducts(JSON.parse(body).products);
    } catch (e) { /* fall through to legacy */ }
    if (!pepperItems.length) {
      const { status, body } = await httpsGet(
        `https://www.jwpepper.com/api/catalog_system/pub/products/search/?ft=${qEnc}&_from=0&_to=5`
      );
      if (status === 200 || status === 206) {
        const arr = JSON.parse(body);
        pepperItems = (arr || []).slice(0, 6).map(p => {
          const voicings = [...new Set((p.items || []).map(i => (String(i.name || '').match(VOICING_RE) || [])[1])
            .filter(Boolean).map(v => v.toUpperCase().replace('TWO-PART', '2-Part')))];
          return { title: p.productName || '', composer: (p.Composer || p.Arranger || [])[0] || '',
            publisher: p.brand || '', voicings, description: (p.description || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 400),
            url: p.linkText ? `https://www.jwpepper.com/${p.linkText}/p` : '', image: p.items?.[0]?.images?.[0]?.imageUrl || '' };
        }).filter(r => r.title && r.url);
      }
    }
    if (pepperItems.length) results.push({ source: 'JW Pepper', items: pepperItems });
  } catch (e) { console.error('find-links Pepper:', e.message); }

  // 2. CPDL – Choral Public Domain Library (only when Pepper found nothing)
  if (results.length === 0) {
    try {
      const { status, body } = await httpsGet(
        `https://cpdl.org/wiki/api.php?action=query&list=search&srsearch=${qEnc}&srlimit=6&format=json`
      );
      if (status === 200) {
        const j = JSON.parse(body);
        const items = (j.query?.search || []).map(s => ({
          title: s.title, composer: '', publisher: 'CPDL', voicings: [],
          description: (s.snippet || '').replace(/<[^>]+>/g, '').trim().slice(0, 300),
          url: `https://cpdl.org/wiki/index.php?curid=${s.pageid}`, image: '',
        })).filter(r => r.title && r.url);
        if (items.length) results.push({ source: 'CPDL (Free / Public Domain)', items });
      }
    } catch (e) { console.error('find-links CPDL:', e.message); }
  }

  // Always return manual search URLs as a last resort
  const searchUrls = {
    'Sheet Music Plus': `https://www.sheetmusicplus.com/search?Ntt=${qEnc}`,
    'Hal Leonard':      `https://www.halleonard.com/search.action?keywords=${qEnc}`,
    'GIA Publications': `https://www.giamusic.com/search/products?q=${qEnc}`,
    'Alfred Music':     `https://www.alfred.com/search/?q=${qEnc}`,
    'MusicNotes':       `https://www.musicnotes.com/search/go?w=${qEnc}`,
    'CPDL':             `https://cpdl.org/wiki/index.php/Special:Search?search=${qEnc}`,
  };

  res.json({ results, searchUrls });
});

// ── Cover photo scan (Google AI free tier) ────────────────────────────────────

const SCAN_VOICINGS = ['SATB','SAB','SSA','SSAA','SSATB','SATTBB','SSAATTBB','TTB','TTBB','TBB','SA','2-Part','3-Part','3-Part Mixed','Unison'];

app.post('/api/scan-cover', adminRequired, async (req, res) => {
  try {
    const cfg = await effectiveConfig();
    const key = cfg.geminiApiKey;
    if (!key) return res.status(400).json({ error: 'No Google AI key configured. Add your free key in the Import tab.' });
    const { image, mimeType } = req.body;
    if (!image) return res.status(400).json({ error: 'No image provided.' });

    const prompt = `You are reading photo(s) of printed choral sheet music covers (octavos). One photo may show one or several distinct covers.
For EACH distinct piece visible, extract:
- "title": the piece title as printed (not the series/publisher name)
- "composer": composer and/or arranger as printed, e.g. "Mac Huff" or "arr. Roger Emerson" or "Handel, arr. Hopson"
- "voicing": the voicing if printed, normalized to exactly one of: ${SCAN_VOICINGS.join(', ')} — or "" if not visible
Respond with ONLY a strict JSON array (no markdown, no commentary): [{"title":"...","composer":"...","voicing":"..."}]
If you cannot read anything, return [].`;

    const payload = {
      contents: [{ parts: [{ text: prompt }, { inline_data: { mime_type: mimeType || 'image/jpeg', data: image } }] }],
      generationConfig: { temperature: 0 },
    };
    const models = ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-flash-lite-latest'];
    let status, body;
    for (const model of models) {
      ({ status, body } = await httpsPost(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${encodeURIComponent(key)}`,
        payload
      ));
      if (status !== 429 && status !== 404) break; // try the next model on quota/availability problems
    }
    if (status === 429) return res.status(429).json({ error: 'Free-tier rate limit hit — wait a minute and continue.' });
    const j = JSON.parse(body);
    if (status !== 200) return res.status(status).json({ error: j.error?.message || `Scan failed (HTTP ${status})` });
    let text = j.candidates?.[0]?.content?.parts?.map(p => p.text || '').join('') || '';
    text = text.replace(/```json/gi, '').replace(/```/g, '').trim();
    const start = text.indexOf('['), end = text.lastIndexOf(']');
    if (start === -1 || end === -1) return res.json({ pieces: [] });
    let pieces;
    try { pieces = JSON.parse(text.slice(start, end + 1)); } catch { return res.json({ pieces: [] }); }
    if (!Array.isArray(pieces)) pieces = [];
    pieces = pieces.filter(p => p && p.title).map(p => ({
      title: String(p.title).trim().slice(0, 200),
      composer: String(p.composer || '').trim().slice(0, 200),
      voicing: SCAN_VOICINGS.find(v => v.toLowerCase() === String(p.voicing || '').trim().toLowerCase()) || '',
    }));
    res.json({ pieces });
  } catch (e) { res.status(500).json({ error: 'Scan failed: ' + e.message }); }
});

// ── Genre suggestions (Google AI free tier) ───────────────────────────────────

const SCAN_GENRES = ['Pop','Spiritual','Classical','Folk','Holiday','Musical Theater','Jazz','Sacred','Other'];

app.post('/api/suggest-genres', adminRequired, async (req, res) => {
  try {
    const cfg = await effectiveConfig();
    const key = cfg.geminiApiKey;
    if (!key) return res.status(400).json({ error: 'No Google AI key configured.' });
    const pieces = req.body.pieces;
    if (!Array.isArray(pieces) || !pieces.length || pieces.length > 60)
      return res.status(400).json({ error: 'Send 1-60 pieces per request.' });

    const list = pieces.map((p, i) => `${i + 1}. "${String(p.title || '').slice(0, 120)}" — ${String(p.composer || 'unknown').slice(0, 80)}`).join('\n');
    const prompt = `These are choral sheet music pieces. Classify EACH into exactly one genre from this list: ${SCAN_GENRES.join(', ')}.
Guidelines: arrangements of pop/rock/Motown songs = Pop. Christmas/Hanukkah/winter-holiday = Holiday. African-American spirituals = Spiritual. Sacred Latin/hymn/anthem texts = Sacred. Show tunes/movie musicals = Musical Theater. Art music/classical composers = Classical. Traditional folk songs = Folk. Jazz standards/vocal jazz = Jazz. Unsure = Other.
Pieces:
${list}
Respond with ONLY a strict JSON array of ${pieces.length} strings in the same order, e.g. ["Pop","Holiday",...]. No markdown.`;

    const models = ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-flash-lite-latest'];
    let status, body;
    for (const model of models) {
      ({ status, body } = await httpsPost(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${encodeURIComponent(key)}`,
        { contents: [{ parts: [{ text: prompt }] }], generationConfig: { temperature: 0 } }
      ));
      if (status !== 429 && status !== 404) break;
    }
    if (status === 429) return res.status(429).json({ error: 'Free-tier rate limit hit — wait a minute and retry.' });
    const j = JSON.parse(body);
    if (status !== 200) return res.status(status).json({ error: j.error?.message || `Failed (HTTP ${status})` });
    let text = (j.candidates?.[0]?.content?.parts?.map(p => p.text || '').join('') || '').replace(/```json/gi, '').replace(/```/g, '').trim();
    const start = text.indexOf('['), end = text.lastIndexOf(']');
    if (start === -1 || end === -1) return res.json({ genres: pieces.map(() => '') });
    let genres;
    try { genres = JSON.parse(text.slice(start, end + 1)); } catch { genres = []; }
    if (!Array.isArray(genres)) genres = [];
    genres = pieces.map((_, i) => SCAN_GENRES.find(g => g.toLowerCase() === String(genres[i] || '').trim().toLowerCase()) || '');
    res.json({ genres });
  } catch (e) { res.status(500).json({ error: 'Genre suggestion failed: ' + e.message }); }
});

// ── Requests (Library system) ─────────────────────────────────────────────────

app.post('/api/request', async (req, res) => {
  try {
    const { entryTitle, entryType, entryComposer, entryVoicing, tab,
            name, email, school, district, copies, message } = req.body;
    if (!name || !name.trim()) return res.status(400).json({ error: 'Name is required.' });

    const request = {
      id: Date.now(), timestamp: new Date().toISOString(),
      tab, entryTitle, entryType, entryComposer: entryComposer || '', entryVoicing: entryVoicing || '',
      name: name.trim(), email: (email || '').trim(), school: (school || '').trim(),
      district: (district || '').trim(), copies: (copies || '').trim(), message: (message || '').trim(),
      status: 'pending'
    };
    await db.addRequest(request);

    const cfg = await effectiveConfig();
    if (cfg.notifyEmail && cfg.smtpUser && cfg.smtpPass) {
      try {
        const transporter = nodemailer.createTransport({
          host: cfg.smtpHost || 'smtp.gmail.com',
          port: cfg.smtpPort || 587,
          secure: cfg.smtpPort === 465,
          auth: { user: cfg.smtpUser, pass: cfg.smtpPass },
        });
        const typeLabel = entryType === 'octavo' ? 'Octavo' : 'Book';
        const tabLabel  = tab === 'work' ? 'Work' : 'Personal';
        const esc = s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        await transporter.sendMail({
          from: `"Sheet Music Catalog" <${cfg.smtpUser}>`,
          to: cfg.notifyEmail,
          subject: `📚 New Request: "${entryTitle}" (${typeLabel})`,
          html: `
<div style="font-family:sans-serif;max-width:540px;margin:0 auto;color:#1a1917;">
  <div style="background:#2d5a8e;padding:20px 24px;border-radius:8px 8px 0 0;">
    <h2 style="margin:0;color:#fff;font-size:18px;">New Sheet Music Request</h2>
    <p style="margin:4px 0 0;color:#c8d9ef;font-size:13px;">from your catalog</p>
  </div>
  <div style="border:1px solid #e2e0db;border-top:none;border-radius:0 0 8px 8px;padding:24px;">
    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
      <tr><td style="padding:8px 0;border-bottom:1px solid #f0eeea;font-weight:600;color:#2d5a8e;width:36%;">Item</td>
          <td style="padding:8px 0;border-bottom:1px solid #f0eeea;">${esc(entryTitle) || '—'}</td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #f0eeea;color:#6b6860;">Type</td>
          <td style="padding:8px 0;border-bottom:1px solid #f0eeea;">${typeLabel} · ${tabLabel} collection</td></tr>
      ${entryComposer ? `<tr><td style="padding:8px 0;border-bottom:1px solid #f0eeea;color:#6b6860;">Composer</td><td style="padding:8px 0;border-bottom:1px solid #f0eeea;">${esc(entryComposer)}</td></tr>` : ''}
      ${entryVoicing ? `<tr><td style="padding:8px 0;border-bottom:1px solid #f0eeea;color:#6b6860;">Voicing</td><td style="padding:8px 0;border-bottom:1px solid #f0eeea;">${esc(entryVoicing)}</td></tr>` : ''}
    </table>
    <h3 style="font-size:14px;color:#6b6860;text-transform:uppercase;letter-spacing:.5px;margin:0 0 12px;">Requester</h3>
    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
      <tr><td style="padding:6px 0;color:#6b6860;width:36%;">Name</td><td style="padding:6px 0;font-weight:500;">${esc(name)}</td></tr>
      ${email ? `<tr><td style="padding:6px 0;color:#6b6860;">Email</td><td style="padding:6px 0;"><a href="mailto:${esc(email)}">${esc(email)}</a></td></tr>` : ''}
      ${school ? `<tr><td style="padding:6px 0;color:#6b6860;">School</td><td style="padding:6px 0;">${esc(school)}</td></tr>` : ''}
      ${district ? `<tr><td style="padding:6px 0;color:#6b6860;">District</td><td style="padding:6px 0;">${esc(district)}</td></tr>` : ''}
      ${copies ? `<tr><td style="padding:6px 0;color:#6b6860;">Copies needed</td><td style="padding:6px 0;">${esc(copies)}</td></tr>` : ''}
    </table>
    ${message ? `<div style="background:#f8f7f4;border-radius:6px;padding:14px;font-size:13px;color:#1a1917;line-height:1.6;"><strong>Message:</strong><br>${esc(message)}</div>` : ''}
    <p style="margin:20px 0 0;font-size:12px;color:#6b6860;">
      Received ${new Date().toLocaleString()} · View all requests in your catalog under ⚙ Settings → Requests.
    </p>
  </div>
</div>`,
        });
      } catch (e) { console.error('Email error:', e.message); }
    }
    res.json({ success: true });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/api/requests', adminRequired, async (req, res) => {
  try { res.json(await db.getRequests()); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/api/requests/:id/status', adminRequired, async (req, res) => {
  try { await db.setRequestStatus(parseInt(req.params.id), req.body.status); res.json({ success: true }); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.delete('/api/requests/:id', adminRequired, async (req, res) => {
  try { await db.deleteRequest(parseInt(req.params.id)); res.json({ success: true }); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

// ── Share info / health ───────────────────────────────────────────────────────

app.get('/api/share-info', (req, res) => res.json({ ips: getLocalIPs(), port: PORT, appUrl: process.env.APP_URL || null, hosted: db.mode === 'postgres' }));
app.get('/healthz', (req, res) => res.json({ ok: true, storage: db.mode }));

// ── Config ────────────────────────────────────────────────────────────────────

app.get('/api/auth-status', async (req, res) => {
  try {
    const cfg = await effectiveConfig();
    const tokens = await db.getTokens();
    res.json({ configured: !!(cfg.clientId && cfg.clientSecret), authenticated: !!(cfg.clientId && cfg.clientSecret && tokens), spreadsheetId: cfg.spreadsheetId || null });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// Admin-only: previously this leaked the Google client secret to all visitors.
app.get('/api/config-read', adminRequired, async (req, res) => {
  try {
    const c = await effectiveConfig();
    res.json({
      clientId: c.clientId || '', clientSecretSet: !!c.clientSecret, hasAdminPassword: !!c.adminPassword,
      geminiKeySet: !!c.geminiApiKey,
      notifyEmail: c.notifyEmail || '', smtpUser: c.smtpUser || '', smtpHost: c.smtpHost || '', smtpPort: c.smtpPort || '',
      envManaged: { adminPassword: !!process.env.ADMIN_PASSWORD, google: !!process.env.GOOGLE_CLIENT_ID },
    });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/api/config', adminRequired, async (req, res) => {
  try {
    const allowed = ['clientId', 'clientSecret', 'notifyEmail', 'smtpUser', 'smtpPass', 'smtpHost', 'smtpPort', 'geminiApiKey'];
    const cfg = await db.getConfig();
    for (const k of allowed) if (k in req.body) cfg[k] = req.body[k];
    await db.saveConfig(cfg);
    if (req.body.clientId || req.body.clientSecret) await db.deleteTokens();
    res.json({ success: true });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ── Google OAuth ──────────────────────────────────────────────────────────────

async function getOAuthClient(req) {
  const cfg = await effectiveConfig();
  if (!cfg.clientId || !cfg.clientSecret) return null;
  return new google.auth.OAuth2(cfg.clientId, cfg.clientSecret, `${baseUrl(req)}/auth/callback`);
}

app.get('/auth/google', async (req, res) => {
  const c = await getOAuthClient(req); if (!c) return res.redirect('/?error=no-config');
  res.redirect(c.generateAuthUrl({ access_type: 'offline', prompt: 'consent', scope: ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file'] }));
});

app.get('/auth/callback', async (req, res) => {
  try {
    const c = await getOAuthClient(req);
    const { tokens } = await c.getToken(req.query.code);
    await db.saveTokens(tokens);
    res.redirect('/?auth=success');
  } catch (e) { res.redirect(`/?error=${encodeURIComponent(e.message)}`); }
});

app.post('/auth/revoke', adminRequired, async (req, res) => {
  await db.deleteTokens();
  const cfg = await db.getConfig(); delete cfg.spreadsheetId; await db.saveConfig(cfg);
  res.json({ success: true });
});

// ── Google Sheets Sync ────────────────────────────────────────────────────────

app.post('/api/sync-sheets', adminRequired, async (req, res) => {
  const { tab } = req.body;
  try {
    const c = await getOAuthClient(req); if (!c) return res.status(400).json({ error: 'Google credentials not configured.' });
    const tokens = await db.getTokens();
    if (!tokens) return res.status(401).json({ error: 'Not authenticated with Google.' });
    c.setCredentials(tokens);
    c.on('tokens', t => db.saveTokens({ ...tokens, ...t }).catch(() => {}));
    const sheetsApi = google.sheets({ version: 'v4', auth: c });
    const cfg = await db.getConfig();
    let spreadsheetId = cfg.spreadsheetId;
    if (!spreadsheetId) {
      const created = await sheetsApi.spreadsheets.create({ requestBody: { properties: { title: 'Sheet Music Library' }, sheets: [{ properties: { title: 'Work', index: 0 } }, { properties: { title: 'Personal', index: 1 } }] } });
      spreadsheetId = created.data.spreadsheetId; cfg.spreadsheetId = spreadsheetId; await db.saveConfig(cfg);
    }

    const tabTitle = tab === 'work' ? 'Work' : 'Personal';
    // Payloads: either the new multi-sheet format, or legacy single-sheet {rows, headers}
    let payloads = Array.isArray(req.body.sheets) && req.body.sheets.length
      ? req.body.sheets
      : [{ title: tabTitle, headers: req.body.headers, rows: req.body.rows }];
    payloads = payloads.filter(p => p.title && Array.isArray(p.headers) && Array.isArray(p.rows));

    // Group sheets we manage (and may delete when empty/stale)
    // Include both legacy prefixed titles ("Work – Voicing – ...") and current unprefixed titles ("Voicing – ...")
    const managedPrefixes = [`${tabTitle} – Voicing – `, `${tabTitle} – Genre – `, `${tabTitle} – Octavos – `, `${tabTitle} – Books – `, 'Voicing – ', 'Octavos – ', 'Books – '];

    // 1. Add missing sheets, delete stale managed ones — single batch
    const ss = await sheetsApi.spreadsheets.get({ spreadsheetId });
    const existing = ss.data.sheets.map(s => s.properties);
    const wantTitles = new Set(payloads.map(p => p.title));
    const structure = [];
    for (const p of payloads)
      if (!existing.some(e => e.title === p.title)) structure.push({ addSheet: { properties: { title: p.title } } });
    for (const e of existing)
      if (!wantTitles.has(e.title) && managedPrefixes.some(pre => e.title.startsWith(pre)))
        structure.push({ deleteSheet: { sheetId: e.sheetId } });
    if (structure.length) await sheetsApi.spreadsheets.batchUpdate({ spreadsheetId, requestBody: { requests: structure } });

    // 2. Clear + write all sheets in two batched calls
    const idByTitle = {};
    (await sheetsApi.spreadsheets.get({ spreadsheetId })).data.sheets
      .forEach(s => { idByTitle[s.properties.title] = s.properties.sheetId; });
    const quote = t => `'${String(t).replace(/'/g, "''")}'`;
    await sheetsApi.spreadsheets.values.batchClear({ spreadsheetId, requestBody: { ranges: payloads.map(p => `${quote(p.title)}!A:Z`) } });
    await sheetsApi.spreadsheets.values.batchUpdate({ spreadsheetId, requestBody: {
      valueInputOption: 'RAW',
      data: payloads.map(p => ({ range: `${quote(p.title)}!A1`, values: [p.headers, ...p.rows] })),
    } });

    // 3. Bold headers + autosize columns — single batch
    const fmt = [];
    for (const p of payloads) {
      const sheetId = idByTitle[p.title];
      if (sheetId === undefined) continue;
      fmt.push({ repeatCell: { range: { sheetId, startRowIndex: 0, endRowIndex: 1 }, cell: { userEnteredFormat: { textFormat: { bold: true } } }, fields: 'userEnteredFormat.textFormat.bold' } });
      fmt.push({ autoResizeDimensions: { dimensions: { sheetId, dimension: 'COLUMNS', startIndex: 0, endIndex: p.headers.length } } });
    }
    if (fmt.length) await sheetsApi.spreadsheets.batchUpdate({ spreadsheetId, requestBody: { requests: fmt } });

    res.json({ success: true, spreadsheetId, sheetCount: payloads.length, url: `https://docs.google.com/spreadsheets/d/${spreadsheetId}` });
  } catch (e) {
    console.error('Sync error:', e.message);
    const isAuth = e.code === 401 || (e.message && e.message.includes('invalid_grant'));
    if (isAuth) await db.deleteTokens();
    res.status(isAuth ? 401 : 500).json({ error: e.message, reauth: isAuth });
  }
});

// ── Boot ──────────────────────────────────────────────────────────────────────

db.init().then(() => {
  app.listen(PORT, '0.0.0.0', () => {
    console.log(`\n🎵  Sheet Music Catalog  (storage: ${db.mode})`);
    console.log(`   Local:   http://localhost:${PORT}`);
    if (db.mode === 'json') getLocalIPs().forEach(ip => console.log(`   Network: http://${ip}:${PORT}`));
    console.log('');
  });
}).catch(e => { console.error('Failed to initialize storage:', e); process.exit(1); });
