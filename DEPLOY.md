# Deploying the Choral Library to Railway

The app auto-detects its environment: with no `DATABASE_URL` it runs locally off
`data.json` exactly as before; with `DATABASE_URL` set it uses PostgreSQL and
**auto-seeds the database from `data.json` on first boot** — so deployment migrates
your data automatically. `node migrate.js` exists as a manual/repair tool.

## One-time setup (manual)

1. Create a Railway account at https://railway.com (sign up with GitHub or email).
2. Install the Railway CLI on your Mac:
   ```
   brew install railway
   ```
   (or `npm i -g @railway/cli`)

## Deploy

From the project folder (`~/Code/sheet-music-catalog`):

```bash
railway login                  # opens browser to authenticate
railway init                   # create a new project, e.g. "choral-library"
railway add --database postgres  # provision the PostgreSQL database
railway up                     # deploy the app code
```

Then link the database and set the app's variables (replace the password!):

```bash
railway variables --set 'DATABASE_URL=${{Postgres.DATABASE_URL}}' \
  --set 'ADMIN_PASSWORD=CHOOSE-A-STRONG-PASSWORD' \
  --set "SECRET=$(openssl rand -hex 32)"
railway domain                 # generates the public URL
```

After `railway domain` prints your URL (e.g. `https://choral-library-production.up.railway.app`):

```bash
railway variables --set 'APP_URL=https://YOUR-URL-HERE'
railway up                     # redeploy so APP_URL takes effect
```

## Google Sheets sync on the hosted site (optional)

The Google OAuth credentials are no longer shipped in the repo (`config.json` is
gitignored / not uploaded). To enable Sheets sync on the hosted site:

1. In [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials),
   edit your OAuth client and add the redirect URI:
   `https://YOUR-URL-HERE/auth/callback`
2. Set the credentials as Railway variables:
   ```bash
   railway variables --set 'GOOGLE_CLIENT_ID=...' --set 'GOOGLE_CLIENT_SECRET=...'
   ```

## Environment variables reference

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes (hosted) | PostgreSQL connection; switches storage to Postgres |
| `ADMIN_PASSWORD` | yes (hosted) | Your librarian login; visitors are read-only |
| `SECRET` | recommended | HMAC secret for login tokens |
| `APP_URL` | recommended | Public URL (used for OAuth redirects + share box) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | optional | Sheets sync |
| `NOTIFY_EMAIL`, `SMTP_USER`, `SMTP_PASS`, `SMTP_HOST`, `SMTP_PORT` | optional | Request email notifications (can also be set in Settings UI) |

## Verifying after deploy

- `https://YOUR-URL/healthz` should return `{"ok":true,"storage":"postgres"}`
- The Work tab should show **396** titles and Personal **8** (plus anything added since).
- Visitors see read-only mode; the 🔓 Sign In button + your `ADMIN_PASSWORD` unlocks editing.

## Local use (unchanged)

```bash
npm start          # http://localhost:3000, stores in data.json
```

Backups of all pre-upgrade data are in `backups/`.
