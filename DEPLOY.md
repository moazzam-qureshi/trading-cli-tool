# Deployment Guide

**TL;DR:** Use Docker. It's one file to install, one command to run.

---

## Option A — Docker (Recommended)

### Local (your laptop, today)

```bash
cd "D:\Personal\Projects\trading-workspace"

# Pre-create state files so Docker bind-mounts them as files (not dirs)
type nul > trades.csv 2>nul || echo. > trades.csv
type nul > state.json 2>nul || echo {} > state.json

# Build + start in background
docker compose up -d --build

# Watch logs (tail -f equivalent)
docker compose logs -f

# Stop / restart / status
docker compose stop
docker compose restart
docker compose ps
```

You'll get a Discord ping within ~5 seconds confirming the daemon is online.

### VPS (Ubuntu/Debian/any Linux)

```bash
# 1. Install Docker (one-time setup)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out + back in for group to apply

# 2. Get the code on the VPS
mkdir -p ~/trading-workspace
cd ~/trading-workspace
# Either git clone, or rsync from your laptop:
# rsync -avz --exclude='venv' --exclude='__pycache__' --exclude='logs' \
#   "user@laptop:/path/to/trading-workspace/" ~/trading-workspace/

# 3. Configure secrets
cp .env.example .env
nano .env
# Fill in: BINANCE_API_KEY, BINANCE_API_SECRET, DISCORD_WEBHOOK_SIGNALS
chmod 600 .env

# 4. Pre-create state files
touch trades.csv state.json

# 5. Build + run
docker compose up -d --build

# 6. Verify
docker compose ps                    # should say "running (healthy)" after ~30s
docker compose logs -f --tail=50     # live tail
```

That's it. Restarts on crash, restarts on reboot (`unless-stopped` policy).

### Common Docker Commands

```bash
# Restart after code changes
docker compose up -d --build

# Run a one-off CLI command in the container
docker compose run --rm trade-cli python trade.py status
docker compose run --rm trade-cli python trade.py confluence SOLUSDT

# Tail logs (live)
docker compose logs -f

# Show last 100 log lines
docker compose logs --tail=100

# Shell into the container for debugging
docker compose exec trade-cli /bin/bash

# Update + rebuild after code changes
git pull   # or rsync
docker compose up -d --build

# Full reset (KEEPS your trades.csv + state.json)
docker compose down
docker compose up -d --build

# Nuclear option (DELETES the image, keeps your data)
docker compose down --rmi all
docker compose up -d --build
```

---

## Option B — systemd (No Docker)

Use this only if you can't run Docker on your VPS. See `trade-cli.service` and follow the older guide:

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
cd ~/trading-workspace
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env

# Edit trade-cli.service: set User and WorkingDirectory paths
sudo cp trade-cli.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trade-cli
sudo systemctl status trade-cli
sudo journalctl -u trade-cli -f
```

---

## What Gets Persisted

These live on the **host filesystem** (mounted as volumes), so they survive container rebuilds:

| Path | What |
|---|---|
| `./logs/daemon.log` | Rotating daemon logs (14 days) |
| `./trades.csv` | Trade journal (one row per trade) |
| `./trade_notes/*.md` | Per-trade post-mortems |
| `./state.json` | Daemon state (last alerts, tracked positions) |

Your `.env` is only read at start — never copied into the image.

---

## What the Daemon Does

| Job | Frequency (default) | Channel | Purpose |
|---|---|---|---|
| **PositionMonitor** | every 60s | `signals` | near-stop / near-target / structure flips / fill detection / heartbeats |
| **SetupScanner** | every 30 min | `scanner` (or `signals`) | A+ setups (≥ 8/10) with reasoning |
| **Startup** | once on boot | `signals` | "Daemon online" with USDT + open orders |

All intervals + thresholds are tunable via `.env` (`DAEMON_*` vars).

---

## Multiple Discord Channels

Create webhooks in Discord (Server Settings → Integrations → Webhooks) and add to `.env`:

```bash
DISCORD_WEBHOOK_SIGNALS=https://discord.com/api/webhooks/.../...   # active alerts
DISCORD_WEBHOOK_SCANNER=https://discord.com/api/webhooks/.../...   # A+ scan results
DISCORD_WEBHOOK_REPORTS=https://discord.com/api/webhooks/.../...   # daily reports (future)
```

If `SCANNER` isn't set, scanner alerts fall back to `SIGNALS`.

---

## Security Checklist

- [ ] Binance API: **trading enabled, withdrawals DISABLED, margin/futures DISABLED**
- [ ] Binance API: **whitelist your VPS IP** in API key restrictions
- [ ] `chmod 600 .env` on the VPS — only your user can read it
- [ ] Container runs as **non-root user** (`trader`, UID 1000) — already configured in Dockerfile
- [ ] `restart: unless-stopped` keeps the daemon up across host reboots
- [ ] Firewall: VPS exposes only SSH (port 22)
- [ ] SSH: key-based auth only, no passwords
- [ ] Pick a VPS region where Binance API is reachable

---

## Cheap VPS Picks (April 2026)

| Provider | Plan | $/mo | RAM | Notes |
|---|---|---|---|---|
| Hetzner | CAX11 | €3.79 | 4 GB | ARM, very cheap, EU |
| DigitalOcean | Basic Droplet | $6 | 1 GB | Easy, US/EU/SG regions |
| Vultr | Cloud Compute | $6 | 1 GB | Many regions |
| OVH | VPS Starter | €3.50 | 2 GB | EU, decent value |

The daemon uses ~80MB RAM and < 1% CPU. Smallest tier is fine.
