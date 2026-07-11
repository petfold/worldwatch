# Operator Setup Guide — Step-by-step with recommendations

This walks you through every item in `OPERATOR-TODO.md` with exact instructions and best-practice recommendations.

---

## 1️⃣ Internet Health — Cloudflare Radar (🔴 blocks alerts)

**What it does**: Detects internet outages, BGP hijacks, DNS anomalies globally. Adds a `physical` modality that correlates with real-world Internet disruptions.

**Best option**: Cloudflare Radar (free, global coverage, no setup).

### Setup:

1. Go to **https://dash.cloudflare.com/** and create a free account (if needed)
2. Navigate to **Radar → API** (or https://developers.cloudflare.com/radar/get-started/authentication/)
3. Click **Create token**, name it `worldwatch`
4. Copy the token (long string like `abc123xyz...`)
5. Save it somewhere safe

### Test locally:

```bash
export WW_CLOUDFLARE_TOKEN="<your-token>"
curl -H "Authorization: Bearer $WW_CLOUDFLARE_TOKEN" \
  "https://api.cloudflare.com/client/v4/radar/http/summary?limit=1"
```

If you get JSON data (not an error), it works.

### For the VPS:

Add to `/etc/worldwatch/worldwatch.env`:
```
WW_CLOUDFLARE_TOKEN=<your-token>
```

---

## 2️⃣ Night Lights — NASA Earthdata (🔴 blocks night-lights source)

**What it does**: VIIRS satellite night-time radiance shows economic activity, power outages, conflict zones. Excellent signal for anomaly detection.

**Best option**: Free NASA Earthdata + VIIRS Black Marble product (regional 500m resolution).

### Setup:

1. Go to **https://urs.earthdata.nasa.gov/users/new** and create a free account
   - Username, email, password
2. Confirm your email
3. Log in → **User Profile → My Applications → Approve more applications**
4. Search for **`LAADS DAAC`** and approve it
5. You now have credentials (username + password)

### For the VPS:

Add to `/etc/worldwatch/worldwatch.env`:
```
WW_EARTHDATA_USER=<your-nasa-username>
WW_EARTHDATA_PASS=<your-nasa-password>
```

---

## 3️⃣ GDELT News (🟡 adds corroboration; not blocking)

**What it does**: Tracks global news events, protests, conflicts. Adds an `informational` modality so multi-source alerts can fire (e.g., quake + news corroboration).

**You choose the access mode** — I'll build the parser once you decide:

### Option A: Raw 15-min event files (recommended)
- **Pros**: No quota, simple, ~1 MB/day
- **Cons**: Less structured
- **Best if**: You want raw mention counts per region

### Option B: GEO 2.0 query API
- **Pros**: Richer, queryable by event type
- **Cons**: 100 free queries/day, more complex
- **Best if**: You want specific event filtering (violence, protests, etc.)

**Recommendation**: **Start with Option A** (no setup needed, just a URL). Switch to Option B later if you need finer control.

Once you decide, tell Claude and the parser will be added.

---

## 4️⃣ FX Markets (🟡 optional; crypto already live)

**Current state**: BTC/ETH are already live (no key needed).

**If you want fiat FX pairs** (USD/EUR, etc.):
- Pick a free provider (e.g., Fixer, Alpha Vantage, or CoinGecko for crypto pairs)
- Most need an API key
- Add to env file as `WW_FIXER_TOKEN=...` etc.

**Recommendation**: Skip for now. Crypto is the most interesting signal. Add fiat FX later if needed.

---

## 5️⃣ Push Notifications — ntfy (🔴 needed for phone alerts)

**What it does**: Sends alert notifications to your phone when corroborated anomalies open.

**Two options:**

### Option A: Public ntfy.sh (simplest, recommended)

**Zero setup, free, works immediately.**

1. Pick a topic name: `worldwatch-alerts-<initials>` (e.g., `worldwatch-alerts-pf`)
   - This is *not* a password; it's a URL path. Make it unique-ish.
2. On your phone, download the **ntfy app**:
   - iOS: App Store → search "ntfy"
   - Android: Google Play → search "ntfy"
3. In the app, subscribe to your topic (e.g., `worldwatch-alerts-pf`)
4. Test: open https://ntfy.sh/worldwatch-alerts-pf in a browser, click **Publish a message**, see it on your phone

### Option B: Self-hosted ntfy (if you want full control)

Run on your VPS after deploy:

```bash
docker run -d -p 8080:80 --name ntfy heckel/ntfy:latest serve
```

Access at `http://<vps-ip>:8080`.

### For the VPS:

**If using public ntfy.sh:**
```
WW_NTFY_SERVER=https://ntfy.sh
WW_NTFY_TOPIC=worldwatch-alerts-pf
WW_NTFY_TOKEN=
```

**If self-hosting:**
```
WW_NTFY_SERVER=http://127.0.0.1:8080
WW_NTFY_TOPIC=worldwatch-alerts
WW_NTFY_TOKEN=
```

**Recommendation**: Start with **public ntfy.sh**. Zero friction, works on day 1. Self-host later if you want it.

---

## 6️⃣ VPS Provisioning (⚪ deployment phase)

**When ready**, pick a small VPS:

| Provider | Cost | Specs | Region |
|----------|------|-------|--------|
| **Hetzner Cloud** | €3/mo | 2 vCPU, 4GB RAM, 40GB SSD | Germany (or US) |
| **DigitalOcean** | $4/mo | 1 vCPU, 1GB RAM, 25GB | Multiple |
| **Linode** | $5/mo | 1 vCPU, 1GB RAM, 25GB | Multiple |

**Recommendation**: **Hetzner** (€3 ≈ $3.25/mo, best specs, reliable).

**When you spin it up:**
1. Choose **Ubuntu 24.04 LTS** as the image — it ships Python 3.12, which
   Worldwatch requires (22.04 ships 3.10 and the install would fail)
2. Size: use **4GB RAM** — gives headroom for Layer-1 work later
3. Add your SSH public key (`~/.ssh/id_ed25519.pub`) in the provider console so
   Claude can drive the deploy from your machine
4. Tell Claude the IP — the rest (`ops/deploy.sh`, env file, verification) is
   driven over SSH

---

## 7️⃣ Restic/B2 Backup (⚪ deployment phase)

You already have restic + B2 configured. When deploying:

1. The deploy script creates `/var/lib/worldwatch/worldwatch.db`
2. Add it to your existing restic backup schedule:
   ```bash
   RESTIC_REPOSITORY=... RESTIC_PASSWORD_FILE=... \
     /opt/worldwatch/ops/backup/restic-backup.sh
   ```
3. Schedule via cron (e.g., hourly):
   ```
   0 * * * * RESTIC_REPOSITORY=... /opt/worldwatch/ops/backup/restic-backup.sh
   ```

That's it — piggyback on your existing setup.

---

## ✅ What to do right now (updated 2026-07-11)

Done and verified: Cloudflare Radar token, NASA Earthdata login (both live in
the local `.env`), GDELT (raw files, live), and all 8 Tier-1 sources.

Remaining — all deployment-phase:

1. **ntfy**: pick a topic, subscribe on your phone (§5; public ntfy.sh
   recommended to start)
2. **VPS**: provision per §6 (Hetzner, **Ubuntu 24.04**, 4 GB, your SSH key)
3. **Deploy**: give Claude the IP; it runs `ops/deploy.sh`, copies the three
   keys from your local `.env` plus the ntfy settings into
   `/etc/worldwatch/worldwatch.env`, and verifies all sources are polling
4. **Backup**: point your existing restic/B2 at the DB (§7)
5. **Soak**: 14 days unattended (`ops/README.md` for what to check)

---

## How secrets are handled

1. You set env vars on the VPS (in `/etc/worldwatch/worldwatch.env`)
2. The systemd units read them via `EnvironmentFile=`
3. The pollers fetch them at runtime via `SourceConfig.auth_token()`
4. Values never touch the repo or the database

This is secure and allows you to rotate keys without redeploying code.
