// ── Storage layer ─────────────────────────────────────────────────────────────
// Uses PostgreSQL when DATABASE_URL is set (hosted deployment),
// otherwise falls back to local JSON files (identical to the original app).

const fs   = require('fs');
const path = require('path');

const DATA_FILE     = path.join(__dirname, 'data.json');
const REQUESTS_FILE = path.join(__dirname, 'requests.json');
const TOKEN_FILE    = path.join(__dirname, '.tokens.json');
const CONFIG_FILE   = path.join(__dirname, 'config.json');

const usePg = !!process.env.DATABASE_URL;

// ── JSON-file implementation (local mode) ─────────────────────────────────────

function readJSON(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
}
function writeJSON(file, obj) { fs.writeFileSync(file, JSON.stringify(obj, null, 2)); }

const jsonStore = {
  mode: 'json',
  async init() {
    if (!fs.existsSync(DATA_FILE))     writeJSON(DATA_FILE, { work: [], personal: [] });
    if (!fs.existsSync(REQUESTS_FILE)) writeJSON(REQUESTS_FILE, []);
  },
  async getData() { return readJSON(DATA_FILE, { work: [], personal: [] }); },
  async replaceData(data) { writeJSON(DATA_FILE, data); },
  async addEntry(tab, entry) {
    const data = await this.getData();
    if (!data[tab]) data[tab] = [];
    data[tab].unshift(entry);
    writeJSON(DATA_FILE, data);
    return entry;
  },
  async updateEntry(tab, id, entry) {
    const data = await this.getData();
    const i = (data[tab] || []).findIndex(e => e._id === id);
    if (i === -1) return null;
    data[tab][i] = { ...entry, _id: id };
    writeJSON(DATA_FILE, data);
    return data[tab][i];
  },
  async deleteEntries(tab, ids) {
    const data = await this.getData();
    const set = new Set(ids);
    const before = (data[tab] || []).length;
    data[tab] = (data[tab] || []).filter(e => !set.has(e._id));
    writeJSON(DATA_FILE, data);
    return before - data[tab].length;
  },
  async patchEntries(tab, ids, patch) {
    const data = await this.getData();
    const set = new Set(ids);
    let n = 0;
    (data[tab] || []).forEach(e => { if (set.has(e._id)) { Object.assign(e, patch); n++; } });
    writeJSON(DATA_FILE, data);
    return n;
  },
  async getRequests() { return readJSON(REQUESTS_FILE, []); },
  async addRequest(r) { const a = await this.getRequests(); a.unshift(r); writeJSON(REQUESTS_FILE, a); },
  async setRequestStatus(id, status) {
    const a = await this.getRequests();
    const r = a.find(x => x.id === id); if (r) r.status = status;
    writeJSON(REQUESTS_FILE, a);
  },
  async deleteRequest(id) {
    writeJSON(REQUESTS_FILE, (await this.getRequests()).filter(x => x.id !== id));
  },
  async getConfig() { return readJSON(CONFIG_FILE, {}); },
  async saveConfig(cfg) { writeJSON(CONFIG_FILE, cfg); },
  async getTokens() { return fs.existsSync(TOKEN_FILE) ? readJSON(TOKEN_FILE, null) : null; },
  async saveTokens(t) { writeJSON(TOKEN_FILE, t); },
  async deleteTokens() { if (fs.existsSync(TOKEN_FILE)) fs.unlinkSync(TOKEN_FILE); },
};

// ── PostgreSQL implementation (hosted mode) ───────────────────────────────────

function makePgStore() {
  const { Pool } = require('pg');
  const cs = process.env.DATABASE_URL;
  // Railway internal hostnames don't use SSL; public proxies do.
  const needSsl = !/railway\.internal|localhost|127\.0\.0\.1/.test(cs) && process.env.PGSSL !== 'disable';
  const pool = new Pool({ connectionString: cs, ssl: needSsl ? { rejectUnauthorized: false } : false });

  async function q(text, params) { return pool.query(text, params); }

  return {
    mode: 'postgres',
    pool,
    async init() {
      await q(`CREATE TABLE IF NOT EXISTS entries (
        id BIGINT PRIMARY KEY,
        tab TEXT NOT NULL,
        pos DOUBLE PRECISION NOT NULL DEFAULT 0,
        payload JSONB NOT NULL
      )`);
      await q(`CREATE INDEX IF NOT EXISTS entries_tab_pos ON entries (tab, pos)`);
      await q(`CREATE TABLE IF NOT EXISTS requests (id BIGINT PRIMARY KEY, payload JSONB NOT NULL)`);
      await q(`CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value JSONB NOT NULL)`);

      // One-time seed from local JSON files if the database is empty.
      const { rows } = await q(`SELECT COUNT(*)::int AS n FROM entries`);
      if (rows[0].n === 0 && fs.existsSync(DATA_FILE)) {
        const data = readJSON(DATA_FILE, { work: [], personal: [] });
        let total = 0;
        for (const tab of Object.keys(data)) {
          for (let i = 0; i < data[tab].length; i++) {
            const e = data[tab][i];
            await q(`INSERT INTO entries (id, tab, pos, payload) VALUES ($1,$2,$3,$4)
                     ON CONFLICT (id) DO NOTHING`, [e._id, tab, i, e]);
            total++;
          }
        }
        console.log(`Seeded ${total} entries from data.json into PostgreSQL.`);
        if (fs.existsSync(REQUESTS_FILE)) {
          for (const r of readJSON(REQUESTS_FILE, []))
            await q(`INSERT INTO requests (id, payload) VALUES ($1,$2) ON CONFLICT (id) DO NOTHING`, [r.id, r]);
        }
        const cfgRow = await q(`SELECT 1 FROM app_config WHERE key='config'`);
        if (!cfgRow.rows.length && fs.existsSync(CONFIG_FILE))
          await q(`INSERT INTO app_config (key, value) VALUES ('config', $1)`, [readJSON(CONFIG_FILE, {})]);
      }
    },
    async getData() {
      const { rows } = await q(`SELECT tab, payload FROM entries ORDER BY tab, pos ASC, id DESC`);
      const data = { work: [], personal: [] };
      for (const r of rows) { (data[r.tab] = data[r.tab] || []).push(r.payload); }
      return data;
    },
    async replaceData(data) {
      const client = await pool.connect();
      try {
        await client.query('BEGIN');
        await client.query('DELETE FROM entries');
        for (const tab of Object.keys(data))
          for (let i = 0; i < data[tab].length; i++) {
            const e = data[tab][i];
            await client.query(`INSERT INTO entries (id, tab, pos, payload) VALUES ($1,$2,$3,$4)`, [e._id, tab, i, e]);
          }
        await client.query('COMMIT');
      } catch (e) { await client.query('ROLLBACK'); throw e; }
      finally { client.release(); }
    },
    async addEntry(tab, entry) {
      const { rows } = await q(`SELECT COALESCE(MIN(pos), 1) - 1 AS p FROM entries WHERE tab=$1`, [tab]);
      await q(`INSERT INTO entries (id, tab, pos, payload) VALUES ($1,$2,$3,$4)`, [entry._id, tab, rows[0].p, entry]);
      return entry;
    },
    async updateEntry(tab, id, entry) {
      const e = { ...entry, _id: id };
      const r = await q(`UPDATE entries SET payload=$3 WHERE tab=$1 AND id=$2`, [tab, id, e]);
      return r.rowCount ? e : null;
    },
    async deleteEntries(tab, ids) {
      const r = await q(`DELETE FROM entries WHERE tab=$1 AND id = ANY($2::bigint[])`, [tab, ids]);
      return r.rowCount;
    },
    async patchEntries(tab, ids, patch) {
      const r = await q(`UPDATE entries SET payload = payload || $3::jsonb WHERE tab=$1 AND id = ANY($2::bigint[])`,
        [tab, ids, JSON.stringify(patch)]);
      return r.rowCount;
    },
    async getRequests() {
      const { rows } = await q(`SELECT payload FROM requests ORDER BY id DESC`);
      return rows.map(r => r.payload);
    },
    async addRequest(r) { await q(`INSERT INTO requests (id, payload) VALUES ($1,$2)`, [r.id, r]); },
    async setRequestStatus(id, status) {
      await q(`UPDATE requests SET payload = payload || jsonb_build_object('status', $2::text) WHERE id=$1`, [id, status]);
    },
    async deleteRequest(id) { await q(`DELETE FROM requests WHERE id=$1`, [id]); },
    async getConfig() {
      const { rows } = await q(`SELECT value FROM app_config WHERE key='config'`);
      return rows.length ? rows[0].value : {};
    },
    async saveConfig(cfg) {
      await q(`INSERT INTO app_config (key, value) VALUES ('config', $1)
               ON CONFLICT (key) DO UPDATE SET value = $1`, [cfg]);
    },
    async getTokens() {
      const { rows } = await q(`SELECT value FROM app_config WHERE key='tokens'`);
      return rows.length ? rows[0].value : null;
    },
    async saveTokens(t) {
      await q(`INSERT INTO app_config (key, value) VALUES ('tokens', $1)
               ON CONFLICT (key) DO UPDATE SET value = $1`, [t]);
    },
    async deleteTokens() { await q(`DELETE FROM app_config WHERE key='tokens'`); },
  };
}

module.exports = usePg ? makePgStore() : jsonStore;
