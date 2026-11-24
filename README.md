# PS Medico — Weekly Auto-Update Pipeline (free, GitHub Actions)

This is the scheduled runner from the 24 Nov 2026 spec. It lives in your
GitHub repo and runs **every Monday 06:30 IST**, plus any time you click
**Run workflow**. Everything it does was already proven live on 24 Nov
(265 queries, 1,122 fresh prices, zero failures).

## What it does, honestly

| Layer | Source | What the weekly run does |
|---|---|---|
| 1 | NPPA (nppa.gov.in) | fetches the publications page, downloads new dated worksheet PDFs (last 21 days), parses the revised MRPs into the price channel |
| 2 | Jan Aushadhi / PMBJP (pmbi.co.in) | tries the live product API; when it 500s (it does, intermittently) it falls back to the dated baseline CSV checked in here — the app labels whichever it got |
| 3 | Live re-check (PharmEasy) | re-verifies **every** medicine already in the published channel + the tier-1 list + 54 famous brands, through your worker's own `/live` endpoint |
| 4 | CDSCO NSQ | checks the site is reachable and reports; new monthly lists are parsed by the builder AI in a session (formats vary) |
| 5 | Publish | merges everything with the same rules the worker uses (name+pack key, freshest 6,000, honest source strings) and POSTs it to `/publish` — every copy of the app picks it up on next open |

**Failure behavior:** a government site being down is a ⚠️ warning in the run
report — the app keeps its last published data. A ❌ (nothing published) opens
a GitHub issue automatically. Success is silent: the run report appears in
the Actions job summary, nothing else.

## Setup — 4 steps, ~7 minutes, once

### 1. Upload these files into your GitHub repo

Open https://github.com/Pranay9977/ps-medico → **Add file → Upload files**.
From this folder (`ps-medico-gha`) drag:

- `weekly_update.py` (repo root)
- `data/` folder with `pmbjp_prices.csv` + `verify_extra.txt` (repo root, so
  the path is `data/pmbjp_prices.csv`)
- `.github/workflows/weekly-update.yml` — this one needs the folder structure:
  on a computer, drag the `.github` folder in; on a phone, first create
  `.github/workflows` via **Add file → Create new file** (name it
  `.github/workflows/weekly-update.yml`), paste the yml's content, commit,
  then upload the rest.

Commit. The **Actions** tab now shows "PS Medico weekly update".

### 2. Add the admin key as a repository secret

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

- Name: `PSM_ADMIN_KEY`
- Secret: the admin key from `download/ADMIN_KEY.txt` (the `PSM-…` line)

### 3. Test it right now

Actions tab → **PS Medico weekly update → Run workflow** → Run. Watch the
run: the summary shows the report (✅/⚠️ per layer). Then open
https://ps-medico.psc-core.workers.dev/ping — data flows to every app copy
automatically.

### 4. (Recommended, when convenient) rotate the admin key

The key is a GitHub secret now — good. The reason to rotate eventually: very
old app builds (before build 63) still carry the old key inside them, so
anyone with an old copy knows it. To rotate: Cloudflare dashboard → your
worker → **Settings → Variables and Secrets** → `ADMIN_KEY` → edit → new
value → Deploy. Then update the GitHub secret (step 2) and
`download/ADMIN_KEY.txt`. Tell the builder AI — the app is already
key-free since build 63, so nothing else changes.

## The second, lighter automation: the worker's own cron (2 minutes)

The worker (v15) can also refresh prices **every 6 hours by itself** — this
catches daily price moves between the weekly runs. In the Cloudflare
dashboard: your worker → **Settings → Trigger Events (or "Cron Triggers")**
→ **Add** → `0 */6 * * *` → Save. Nothing else. The `#admin` → Run-update
button does the same thing on demand.

Both automations are visible, not a claim: `GET /sweep` on your worker shows
the last run (manual or cron) with its rotation stats — `via: "cron"` vs
`via: "manual"` — and the GitHub Actions history is the weekly audit trail.

## Updating the baseline lists

- `data/pmbjp_prices.csv` — when PMBI publishes a new price list and the API
  is down, replace this file (the report's ⚠️ tells you when the API fails).
- `data/verify_extra.txt` — tier-1 + famous brands; add names you want
  re-checked weekly (one per line).
