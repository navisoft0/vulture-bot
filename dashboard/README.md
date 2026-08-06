# Vulture Dashboard — setup

Cloudflare-hosted community dashboard (see `../DASHBOARD_PLAN.md` for design).
One Worker serves the API + static frontend; D1 stores data; R2 holds monthly
archives; Cloudflare Access provides logins.

## 1. One-time Cloudflare setup

```bash
cd dashboard
npm install -g wrangler          # or use npx wrangler
wrangler login

# Database
wrangler d1 create vulture       # paste the printed database_id into wrangler.toml
wrangler d1 execute vulture --remote --file=schema.sql

# Archive bucket
wrangler r2 bucket create vulture-archive

# Secrets
wrangler secret put INGEST_TOKEN     # long random string; also set on Railway
wrangler secret put RUN_TOKEN        # long random string; also set on Railway
wrangler secret put RUN_URL          # https://<railway-app>.up.railway.app/run
wrangler secret put DISCORD_ARCHIVE_WEBHOOK   # optional: archive links channel

# Edit [vars] in wrangler.toml: ADMIN_EMAILS = "you@example.com"

wrangler deploy
```

Then attach your domain: Cloudflare dashboard → Workers & Pages →
vulture-dashboard → Settings → Domains & Routes → add `vulture.<your-domain>`.

## 2. Cloudflare Access (logins)

Zero Trust dashboard → Access → Applications → Add application → Self-hosted:

- Application domain: `vulture.<your-domain>`
- Policy "members": Allow → Include → Emails → the 5 member addresses
- Identity: One-time PIN (no IdP setup needed) and/or Google

**Important:** add a second policy or bypass rule for the engine paths — the
Railway engine can't do Access logins. Easiest: Access → your application →
add a **Service Auth / Bypass** policy for paths `/api/ingest/*`,
`/api/plays/due`, `/api/runs/pending` (these endpoints enforce their own
bearer token). Alternatively scope the Access app to exclude `/api/ingest`.

## 3. Railway (engine) env vars

```
DASHBOARD_API_URL=https://vulture.<your-domain>
DASHBOARD_INGEST_TOKEN=<same INGEST_TOKEN as above>
RUN_TOKEN=<same RUN_TOKEN as above>
```

Railway must expose the service publicly (Settings → Networking → Generate
Domain) so the Worker can reach `POST /run`. The daemon serves `/health` and
the token-protected `/run` on `$PORT`.

## 4. Local development

```bash
cd dashboard
DEV_MODE=1 wrangler dev          # local D1 + assets; auth bypassed as admin
wrangler d1 execute vulture --local --file=schema.sql
```

## Defaults chosen (change anytime)

- Play grading: contract EOD premium return, HIT ≥ +25% / MISS ≤ −25%
  (`PLAY_HIT_PCT` on the engine); ITM/OTM fallback; ±2% band for strike-less
  plays.
- Members are read-only; admin = `ADMIN_EMAILS`.
- Retention 90 days (`RETENTION_DAYS`), monthly cron, open plays survive,
  archives to R2 with a Discord link.
- Sheets dual-write stays on until cutover (D7).
