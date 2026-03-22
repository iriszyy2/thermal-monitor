"""
Thermal Camera Competitive Monitor v3
- Monitors: TOPDON, FLIR, FLUKE, HIKIMICRO, Seek
- Outputs: monitor_state.json (internal) + docs/data.json (GitHub Pages Dashboard)
- First run: baseline only, no alerts
- Subsequent runs: diff only, email on real changes
"""

import os
import json
import hashlib
import re
import httpx
import smtplib
from datetime import datetime, timezone
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── Brand config ─────────────────────────────────────────────────────────────

BRANDS = {
    "TOPDON": {
        "type": "shopify",
        "base": "https://www.topdon.com",
        "products_api": "https://www.topdon.com/products.json",
        "blog_api": "https://www.topdon.com/blogs/news.json",
        "pages": {
            "About Us":      "https://www.topdon.com/pages/about-us",
            "FAQ":           "https://www.topdon.com/pages/faq",
            "Refund Policy": "https://www.topdon.com/policies/refund-policy",
        },
    },
    "Seek": {
        "type": "shopify",
        "base": "https://www.thermal.com",
        "products_api": "https://www.thermal.com/products.json",
        "blog_api": "https://www.thermal.com/blogs/news.json",
        "pages": {
            "About Us":      "https://www.thermal.com/pages/about",
            "FAQ":           "https://www.thermal.com/pages/faq",
            "Refund Policy": "https://www.thermal.com/policies/refund-policy",
        },
    },
    "FLIR": {
        "type": "generic",
        "base": "https://www.flir.com",
        "products_url": "https://www.flir.com/browse/cameras/thermal-cameras/",
        "blog_url":     "https://www.flir.com/discover/professional-tools/",
        "pages": {
            "About Us":      "https://www.flir.com/about/",
            "Support":       "https://www.flir.com/support/",
            "Return Policy": "https://www.flir.com/support/return-policy/",
        },
    },
    "FLUKE": {
        "type": "generic",
        "base": "https://www.fluke.com",
        "products_url": "https://www.fluke.com/en-us/products/thermal-imagers",
        "blog_url":     "https://www.fluke.com/en-us/learn/blog",
        "pages": {
            "About":         "https://www.fluke.com/en-us/about-fluke",
            "Support":       "https://www.fluke.com/en-us/support",
            "Return Policy": "https://www.fluke.com/en-us/support/return-policy",
        },
    },
    "HIKIMICRO": {
        "type": "generic",
        "base": "https://www.hikimicrotech.com",
        "products_url": "https://www.hikimicrotech.com/en/products/",
        "blog_url":     "https://www.hikimicrotech.com/en/news/",
        "pages": {
            "About":   "https://www.hikimicrotech.com/en/about/",
            "Support": "https://www.hikimicrotech.com/en/support/",
        },
    },
}

STATE_FILE   = Path("monitor_state.json")
DATA_DIR     = Path("docs")          # GitHub Pages serves from /docs
DATA_FILE    = DATA_DIR / "data.json"
MAX_HISTORY  = 200                   # keep last N change records in data.json

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─── State ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

def load_dashboard_data() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {
        "generated_at": "",
        "brands": list(BRANDS.keys()),
        "stats": {"total_changes": 0, "today": 0, "this_week": 0},
        "history": [],
        "page_status": {},
        "product_counts": {},
    }

def save_dashboard_data(data: dict):
    DATA_DIR.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

# ─── Hashing ──────────────────────────────────────────────────────────────────

def stable_page_hash(html: str) -> str:
    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<style[^>]*>.*?</style>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<!--.*?-->', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return hashlib.sha256(cleaned.encode()).hexdigest()[:20]

# ─── Fetchers ─────────────────────────────────────────────────────────────────

def fetch_shopify_products(api_url: str, base: str, client: httpx.Client) -> dict:
    products = {}
    page = 1
    while True:
        url = f"{api_url}?limit=250&page={page}"
        try:
            r = client.get(url, timeout=20)
            r.raise_for_status()
            data = r.json().get("products", [])
        except Exception as e:
            print(f"    [warn] products p{page}: {e}")
            break
        if not data:
            break
        for p in data:
            handle = p["handle"]
            variant = next(
                (v for v in p.get("variants", []) if v.get("available")),
                p["variants"][0] if p.get("variants") else {}
            )
            raw = variant.get("price", "")
            try:
                price = f"{float(raw):.2f}" if raw else "N/A"
            except ValueError:
                price = raw or "N/A"
            products[handle] = {
                "title":     p["title"],
                "price":     price,
                "available": any(v.get("available") for v in p.get("variants", [])),
                "url":       f"{base}/products/{handle}",
            }
        page += 1
        if len(data) < 250:
            break
    return products

def fetch_shopify_blog(api_url: str, base: str, client: httpx.Client) -> dict:
    articles = {}
    try:
        r = client.get(api_url, timeout=20)
        r.raise_for_status()
        for a in r.json().get("articles", []):
            articles[str(a["id"])] = {
                "title":        a["title"],
                "published_at": a.get("published_at", ""),
                "url":          f"{base}/blogs/news/{a['handle']}",
            }
    except Exception as e:
        print(f"    [warn] blog: {e}")
    return articles

def fetch_page_hash(url: str, client: httpx.Client) -> str | None:
    try:
        r = client.get(url, timeout=25)
        r.raise_for_status()
        return stable_page_hash(r.text)
    except Exception as e:
        print(f"    [warn] page {url}: {e}")
        return None

# ─── Diff ─────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def diff_products(brand: str, old: dict, new: dict) -> list[dict]:
    changes, ts = [], now_iso()
    for h in sorted(set(new) - set(old)):
        p = new[h]
        price_str = f"${p['price']}" if p['price'] != 'N/A' else "(price TBD)"
        changes.append({
            "id": f"{ts}-{brand}-new-{h}",
            "type": "new_product", "brand": brand, "ts": ts,
            "title": p["title"], "url": p["url"],
            "detail": f"{price_str} · {'In stock' if p['available'] else 'Out of stock'}",
            "price": p["price"],
        })
    for h in sorted(set(old) - set(new)):
        p = old[h]
        changes.append({
            "id": f"{ts}-{brand}-removed-{h}",
            "type": "removed_product", "brand": brand, "ts": ts,
            "title": p["title"], "url": p.get("url", ""),
            "detail": "Removed from store", "price": "",
        })
    for h in sorted(set(old) & set(new)):
        o, n = old[h], new[h]
        if o["price"] != n["price"] and n["price"] != "N/A" and o["price"] != "N/A":
            try:
                op, np_ = float(o["price"]), float(n["price"])
                pct = (np_ - op) / op * 100
                changes.append({
                    "id": f"{ts}-{brand}-price-{h}",
                    "type": "price_change", "brand": brand, "ts": ts,
                    "title": n["title"], "url": n["url"],
                    "detail": f"${o['price']} → ${n['price']} ({pct:+.1f}%)",
                    "price": n["price"],
                })
            except ValueError:
                pass
        if o["available"] != n["available"]:
            changes.append({
                "id": f"{ts}-{brand}-stock-{h}",
                "type": "stock_change", "brand": brand, "ts": ts,
                "title": n["title"], "url": n["url"],
                "detail": "Back in stock" if n["available"] else "Out of stock",
                "price": n["price"],
            })
    return changes

def diff_blog(brand: str, old: dict, new: dict) -> list[dict]:
    ts = now_iso()
    return [
        {
            "id": f"{ts}-{brand}-blog-{aid}",
            "type": "new_article", "brand": brand, "ts": ts,
            "title": new[aid]["title"], "url": new[aid].get("url", ""),
            "detail": f"Published {new[aid].get('published_at','')[:10]}",
            "price": "",
        }
        for aid in sorted(set(new) - set(old))
    ]

def diff_page(brand: str, name: str, old_h: str | None, new_h: str | None, url: str) -> list[dict]:
    if not old_h or not new_h or old_h == new_h:
        return []
    ts = now_iso()
    return [{
        "id": f"{ts}-{brand}-page-{name}",
        "type": "page_change", "brand": brand, "ts": ts,
        "title": name, "url": url, "detail": "Content updated", "price": "",
    }]

# ─── Dashboard data update ────────────────────────────────────────────────────

def update_dashboard(dash: dict, new_changes: list[dict], page_status: dict, product_counts: dict):
    now = datetime.now(timezone.utc)
    today_str  = now.strftime("%Y-%m-%d")
    week_start = now.timestamp() - 7 * 86400

    # Prepend new changes
    dash["history"] = (new_changes + dash.get("history", []))[:MAX_HISTORY]
    dash["generated_at"] = now.isoformat()
    dash["brands"] = list(BRANDS.keys())
    dash["page_status"] = page_status
    dash["product_counts"] = product_counts

    # Recompute stats from history
    total = len(dash["history"])
    today_count = sum(1 for c in dash["history"] if c.get("ts", "")[:10] == today_str)
    week_count  = sum(
        1 for c in dash["history"]
        if datetime.fromisoformat(c["ts"].replace("Z", "+00:00")).timestamp() >= week_start
        if c.get("ts")
    )
    dash["stats"] = {"total_changes": total, "today": today_count, "this_week": week_count}

# ─── Email ────────────────────────────────────────────────────────────────────

TYPE_LABEL = {
    "new_product": "NEW", "removed_product": "REMOVED",
    "price_change": "PRICE", "stock_change": "STOCK",
    "new_article": "BLOG", "page_change": "PAGE",
}
TAG_COLOR = {
    "NEW": "#16a34a", "REMOVED": "#dc2626", "PRICE": "#d97706",
    "STOCK": "#2563eb", "BLOG": "#7c3aed", "PAGE": "#0891b2",
}

def send_email(changes: list[dict], run_time: str):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    email_to  = os.getenv("EMAIL_TO", "")
    pages_url = os.getenv("PAGES_URL", "")   # optional: your GitHub Pages URL

    if not all([smtp_user, smtp_pass, email_to]):
        print("  [email] skipped — SMTP not configured")
        return

    by_brand: dict[str, list] = {}
    for c in changes:
        by_brand.setdefault(c["brand"], []).append(c)

    # Plain text
    lines = [f"Competitive Monitor — {run_time}", "=" * 56]
    for brand, items in by_brand.items():
        lines += [f"\n{brand} ({len(items)} change{'s' if len(items)>1 else ''})", "-" * 36]
        for c in items:
            lines += [f"  [{TYPE_LABEL.get(c['type'],'?')}] {c['title']}", f"         {c['detail']}"]
            if c.get("url"):
                lines.append(f"         {c['url']}")
    if pages_url:
        lines += [f"\nView dashboard: {pages_url}"]
    plain = "\n".join(lines)

    # HTML
    rows = ""
    for brand, items in by_brand.items():
        rows += (f'<tr><td colspan="3" style="padding:14px 0 6px;font-weight:600;'
                 f'font-size:15px;border-bottom:2px solid #e5e7eb">{brand} '
                 f'<span style="font-weight:400;font-size:12px;color:#6b7280">'
                 f'{len(items)} change{"s" if len(items)>1 else ""}</span></td></tr>')
        for c in items:
            tag   = TYPE_LABEL.get(c["type"], "?")
            color = TAG_COLOR.get(tag, "#6b7280")
            title = (f'<a href="{c["url"]}" style="color:#1d4ed8;text-decoration:none">'
                     f'{c["title"]}</a>') if c.get("url") else c["title"]
            rows += (f'<tr style="border-bottom:1px solid #f3f4f6">'
                     f'<td style="padding:9px 8px 9px 0;width:64px;vertical-align:top">'
                     f'<span style="background:{color}20;color:{color};font-size:11px;'
                     f'font-weight:600;padding:3px 7px;border-radius:4px">{tag}</span></td>'
                     f'<td style="padding:9px 8px;font-size:14px;vertical-align:top">{title}</td>'
                     f'<td style="padding:9px 0 9px 8px;font-size:12px;color:#6b7280;'
                     f'vertical-align:top;white-space:nowrap">{c["detail"]}</td></tr>')

    dashboard_link = (f'<p style="margin-top:20px"><a href="{pages_url}" '
                      f'style="color:#1d4ed8">View full dashboard →</a></p>') if pages_url else ""
    html = f"""<!DOCTYPE html><html><body style="font-family:-apple-system,sans-serif;
max-width:660px;margin:0 auto;padding:24px;color:#111">
<div style="margin-bottom:18px">
  <div style="font-size:20px;font-weight:700">Competitive Monitor</div>
  <div style="font-size:12px;color:#9ca3af;margin-top:3px">{run_time} · {len(changes)} changes</div>
</div>
<table style="width:100%;border-collapse:collapse">{rows}</table>
{dashboard_link}
<div style="margin-top:20px;font-size:11px;color:#9ca3af;border-top:1px solid #e5e7eb;padding-top:10px">
TOPDON · FLIR · FLUKE · HIKIMICRO · Seek</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Monitor] {len(changes)} change{'s' if len(changes)>1 else ''} — {run_time[:10]}"
    msg["From"] = smtp_user
    msg["To"]   = email_to
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, email_to, msg.as_string())
        print(f"  [email] sent to {email_to}")
    except Exception as e:
        print(f"  [email] FAILED: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def run():
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}\nThermal Monitor  {run_time}\n{'='*60}")

    state     = load_state()
    dash      = load_dashboard_data()
    is_first  = len(state) == 0

    if is_first:
        print("\n>>> FIRST RUN — building baseline. No alerts sent.\n")

    all_changes: list[dict] = []
    page_status:    dict[str, dict] = {}
    product_counts: dict[str, int]  = {}

    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for brand, cfg in BRANDS.items():
            print(f"\n[{brand}]")
            bs = state.setdefault(brand, {})
            page_status[brand] = {}

            # Products
            if cfg["type"] == "shopify":
                print("  products...")
                new_p = fetch_shopify_products(cfg["products_api"], cfg["base"], client)
                old_p = bs.get("products", {})
                product_counts[brand] = len(new_p)
                if not is_first:
                    ch = diff_products(brand, old_p, new_p)
                    if ch:
                        print(f"  → {len(ch)} product change(s)")
                    all_changes.extend(ch)
                else:
                    print(f"  → baseline: {len(new_p)} products")
                bs["products"] = new_p

                print("  blog...")
                new_b = fetch_shopify_blog(cfg["blog_api"], cfg["base"], client)
                old_b = bs.get("blog", {})
                if not is_first:
                    ch = diff_blog(brand, old_b, new_b)
                    if ch:
                        print(f"  → {len(ch)} new article(s)")
                    all_changes.extend(ch)
                bs["blog"] = new_b

            else:
                print("  products page...")
                new_h = fetch_page_hash(cfg["products_url"], client)
                if not is_first:
                    ch = diff_page(brand, "Products", bs.get("products_hash"), new_h, cfg["products_url"])
                    all_changes.extend(ch)
                bs["products_hash"] = new_h

                print("  blog page...")
                new_bh = fetch_page_hash(cfg["blog_url"], client)
                if not is_first:
                    ch = diff_page(brand, "Blog", bs.get("blog_hash"), new_bh, cfg["blog_url"])
                    all_changes.extend(ch)
                bs["blog_hash"] = new_bh

            # Key pages
            ps = bs.setdefault("pages", {})
            for pname, url in cfg["pages"].items():
                print(f"  {pname}...")
                new_h = fetch_page_hash(url, client)
                changed = False
                if not is_first and new_h:
                    ch = diff_page(brand, pname, ps.get(pname), new_h, url)
                    if ch:
                        changed = True
                        print(f"  → {pname} changed!")
                    all_changes.extend(ch)
                page_status[brand][pname] = {
                    "url": url,
                    "status": "changed" if changed else ("ok" if new_h else "error"),
                    "checked_at": now_iso(),
                }
                if new_h:
                    ps[pname] = new_h

    # Save state
    save_state(state)

    # Update & save dashboard data
    update_dashboard(dash, all_changes, page_status, product_counts)
    save_dashboard_data(dash)
    print(f"\n  [dashboard] data.json updated → {DATA_FILE}")

    print(f"\n{'─'*60}")
    if is_first:
        print("Baseline saved. Dashboard data initialised.")
    elif all_changes:
        print(f"Changes: {len(all_changes)}")
        for c in all_changes:
            print(f"  [{TYPE_LABEL.get(c['type'],'?')}] {c['brand']} · {c['title']} — {c['detail']}")
        send_email(all_changes, run_time)
    else:
        print("No changes. No email sent.")
    print(f"{'─'*60}\n")

if __name__ == "__main__":
    run()
