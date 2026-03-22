# Thermal Camera Brand Monitor

Monitors **TOPDON · FLIR · FLUKE · HIKIMICRO · Seek** for:
- 🆕 New product launches
- 📉📈 Price changes (with % delta)
- ⚠️ Stock status changes (in stock ↔ out of stock)
- 🔄 Page content changes (About / FAQ / Refund policy)
- 📝 New blog / marketing articles

Runs automatically via **GitHub Actions** — free, no server needed.

---

## Quick Setup (10 minutes)

### 1. Fork / create this repo on GitHub

Push all files to a new GitHub repository (can be private).

### 2. Add Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**

| Secret name         | What it is                                    | Required |
|---------------------|-----------------------------------------------|----------|
| `SLACK_WEBHOOK`     | Slack incoming webhook URL                    | Optional |
| `WECHAT_WEBHOOK_KEY`| 企业微信机器人 webhook key                     | Optional |
| `SMTP_HOST`         | e.g. `smtp.gmail.com`                         | Optional |
| `SMTP_PORT`         | e.g. `587`                                    | Optional |
| `SMTP_USER`         | Your Gmail address                            | Optional |
| `SMTP_PASS`         | Gmail App Password (not your login password)  | Optional |
| `EMAIL_TO`          | Where to send alerts                          | Optional |

You only need to set up at least **one** notification channel.

**Gmail App Password**: Go to Google Account → Security → 2-Step Verification →
App passwords → Generate one for "Mail".

**Slack Webhook**: Go to api.slack.com/apps → Create app → Incoming Webhooks → Add.

**企业微信**: 群设置 → 群机器人 → 添加机器人 → 复制 Webhook URL 中的 key 参数。

### 3. Enable Actions

Go to **Actions** tab in your repo → click **"I understand my workflows, go ahead and enable them"**.

### 4. Run manually first

Actions → "Thermal Camera Brand Monitor" → **Run workflow**.

Check the logs — it will initialize the state baseline on the first run (no alerts sent).
From the second run onward, it will alert on any detected changes.

---

## How It Works

```
Every 30 min (GitHub cron)
       ↓
For each brand:
  TOPDON      → Shopify JSON API   → exact price/stock/new product diff
  FLIR/FLUKE  → page content hash  → detects any HTML change
  HIKIMICRO   → page content hash
  Seek        → Shopify-like URLs  → page hash (no public API)
       ↓
Compare against saved state (monitor_state.json, cached between runs)
       ↓
Changes found → Slack + 企业微信 + Email
No changes   → silent
```

TOPDON and Seek use Shopify's public `/products.json` endpoint, giving you
**exact prices, availability, and product handles** — no scraping needed.

For FLIR, FLUKE, and HIKIMICRO (not Shopify), the monitor hashes the full
page content and alerts when it changes. This is reliable for detecting
updates but won't give you the exact price number — you'll need to visit
the page to see what changed.

---

## Customize

### Add / remove brands

Edit the `BRANDS` dict in `monitor.py`. For Shopify stores, set `"type": "shopify"`.
For any other store, set `"type": "generic"`.

### Change check frequency

Edit the `cron` line in `.github/workflows/monitor.yml`.

```yaml
- cron: '*/30 * * * *'   # every 30 min
- cron: '0 * * * *'      # every hour
- cron: '0 9 * * *'      # once a day at 9am UTC
```

### Run locally

```bash
pip install httpx
# Set env vars or create a .env file
export SLACK_WEBHOOK=https://hooks.slack.com/...
python monitor.py
```

---

## Free Tier Limits

GitHub Actions free tier: **2,000 minutes/month** for private repos,
**unlimited** for public repos.

Running every 30 min = ~1,440 runs/month × ~1 min each = **~1,440 minutes/month**.
Well within the free limit even on private repos.
