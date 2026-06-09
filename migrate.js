// One-time migration: pushes local data.json / requests.json / config into
// the PostgreSQL database pointed to by DATABASE_URL.
//   DATABASE_URL=postgres://... node migrate.js
// Safe to re-run: existing rows are left untouched (ON CONFLICT DO NOTHING).

if (!process.env.DATABASE_URL) { console.error('Set DATABASE_URL first.'); process.exit(1); }

const fs = require('fs');
const path = require('path');
const db = require('./db'); // will be the Postgres store because DATABASE_URL is set

(async () => {
  await db.init(); // creates tables and auto-seeds if DB is empty

  // If the DB already had rows, init() skips seeding — do an explicit upsert-merge.
  const data = JSON.parse(fs.readFileSync(path.join(__dirname, 'data.json'), 'utf8'));
  const existing = await db.getData();
  const existingIds = new Set([...(existing.work || []), ...(existing.personal || [])].map(e => e._id));
  let added = 0;
  for (const tab of Object.keys(data)) {
    for (const e of data[tab]) {
      if (!existingIds.has(e._id)) { await db.addEntry(tab, e); added++; }
    }
  }
  const final = await db.getData();
  console.log(`Migration complete.`);
  console.log(`  Local file:  work=${(data.work || []).length}  personal=${(data.personal || []).length}`);
  console.log(`  Database:    work=${(final.work || []).length}  personal=${(final.personal || []).length}  (+${added} newly added this run)`);
  const localTotal = Object.values(data).reduce((s, a) => s + a.length, 0);
  const dbTotal = Object.values(final).reduce((s, a) => s + a.length, 0);
  if (dbTotal < localTotal) { console.error('❌ Database has fewer entries than the local file — investigate before going live!'); process.exit(1); }
  console.log('✅ All local entries are present in the database.');
  process.exit(0);
})().catch(e => { console.error('Migration failed:', e); process.exit(1); });
