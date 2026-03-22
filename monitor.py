"""
Thermal Camera Competitive Monitor
Brands: TOPDON, FLIR, FLUKE, HIKIMICRO, Seek
Monitors: new products, price changes, page content changes, blog updates
"""

import os
import json
import hashlib
import httpx
import smtplib
from datetime import datetime, timezone
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── Config ──────────────────────────────────────────────────────────────────

BRANDS = {
    "TOPDON": {
        "type": "shopify",
        "base": "https://www.topdon.com",
        "products_api": "https://www.topdon.com/products.json?limit=250",
        "blog_api": "https://www.topdon.com/blogs/news.json",
        "pages": {
            "about":   "https://www.topdon.com/pages/about-us",
            "faq":     "https://www.topdon.com/pages/faq",
            "refund":  "https://www.topdon.com/policies/refund-policy",
        },
    },
    "FLIR": {
        "type": "generic",
        "base": "https://www.flir.com",
        "products_url": "https://www.flir.com/browse/cameras/thermal-cameras/",
        "blog_url": "https://www.flir.com/discover/professional-tools/",
        "pages": {
            "about":  "https://www.flir.com/about/",
            "faq":    "https://www.flir.com/support/",
            "refund": "https://www.flir.com/support/return-policy/",
        },
    },
    "FLUKE": {
        "type": "generic",
        "base": "https://www.fluke.com",
        "products_url": "https://www.fluke.com/en-us/products/thermal-imagers",
        "blog_url": "https://www.fluke.com/en-us/learn/blog",
        "pages": {
            "about":  "https://www.fluke.com/en-us/about-fluke",
            "faq":    "https://www.fluke.com/en-us/support",
            "refund": "https://www.fluke.com/en-us/support/return-policy",
        },
    },
    "HIKIMICRO": {
        "type": "generic",
        "base": "https://www.hikimicrotech.com",
        "products_url": "https://www.hikimicrotech.com/en/products/",
        "blog_url": "https://www.hikimicrotech.com/en/news/",
        "pages": {
            "about":  "https://www.hikimicrotech.com/en/about/",
            "faq":    "https://www.hikimicrotech.com/en/support/",
            "refund": "https://www.hikimicrotech.com/en/policies/",
        },
    },
    "Seek": {
        "type": "generic",
        "base": "https://www.thermal.com",
        "products_url": "https://www.thermal.com/collections/all",
        "blog_url": "https://www.thermal.com/blogs/news",
        "pages": {
            "about":  "https://www.thermal.com/pages/about",
            "faq":    "https://www.thermal.com/pages/faq",
            "refund": "https://www.thermal.com/policies/refund-policy",
        },
    },
}

STATE_FILE = Path("monitor_state.json")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

# ─── State helpers ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

# ─── Shopify crawler ──────────────────────────────────────────────────────────

def fetch_shopify_products(url: str, client: httpx.Client) -> dict[str, dict]:
    """Returns {handle: {title, price, available, url}}"""
    products = {}
    page = 1
    while True:
        paged = url + f"&page={page}"
        try:
            r = client.get(paged, timeout=15)
            r.raise_for_status()
            data = r.json().get("products", [])
        except Exception as e:
            print(f"  [warn] Shopify fetch failed: {e}")
            break
        if not data:
            break
        for p in data:
            handle = p["handle"]
            variant = p["variants"][0] if p["variants"] else {}
            products[handle] = {
                "title":     p["title"],
                "price":     variant.get("price", "0.00"),
                "available": variant.get("available", False),
                "url":       f"https://www.topdon.com/products/{handle}",
                "updated":   p.get("updated_at", ""),
            }
        page += 1
        if len(data) < 250:
            break
    return products

def fetch_shopify_blogs(url: str, client: httpx.Client) -> dict[str, dict]:
    """Returns {handle: {title, published_at, url}}"""
    articles = {}
    try:
        r = client.get(url, timeout=15)
        r.raise_for_status()
        for a in r.json().get("articles", []):
            articles[str(a["id"])] = {
                "title":        a["title"],
                "published_at": a["published_at"],
                "url":          f"https://www.topdon.com/blogs/news/{a['handle']}",
            }
    except Exception as e:
        print(f"  [warn] Blog fetch failed: {e}")
    return articles

# ─── Generic page crawler ─────────────────────────────────────────────────────

def fetch_page_hash(url: str, client: httpx.Client) -> str | None:
    try:
        r = client.get(url, timeout=20)
        r.raise_for_status()
        # Strip whitespace variations to reduce false positives
        normalized = " ".join(r.text.split())
        return content_hash(normalized)
    except Exception as e:
        print(f"  [warn] Page fetch failed {url}: {e}")
        return None

def fetch_generic_products_hash(url: str, client: httpx.Client) -> str | None:
    """For non-Shopify brands, just hash the product listing page."""
    return fetch_page_hash(url, client)

# ─── Diff engine ─────────────────────────────────────────────────────────────

def diff_products(brand: str, old: dict, new: dict) -> list[dict]:
    changes = []
    old_keys, new_keys = set(old), set(new)

    for handle in new_keys - old_keys:
        p = new[handle]
        changes.append({
            "type":  "new_product",
            "brand": brand,
            "title": p["title"],
            "price": p["price"],
            "url":   p.get("url", ""),
            "msg":   f"🆕 New product: {p['title']} @ ${p['price']}",
        })

    for handle in old_keys - new_keys:
        p = old[handle]
        changes.append({
            "type":  "removed_product",
            "brand": brand,
            "title": p["title"],
            "url":   p.get("url", ""),
            "msg":   f"❌ Removed: {p['title']}",
        })

    for handle in old_keys & new_keys:
        o, n = old[handle], new[handle]
        if o["price"] != n["price"]:
            old_p, new_p = float(o["price"]), float(n["price"])
            pct = (new_p - old_p) / old_p * 100
            arrow = "📈" if new_p > old_p else "📉"
            changes.append({
                "type":  "price_change",
                "brand": brand,
                "title": n["title"],
                "url":   n.get("url", ""),
                "msg":   f"{arrow} Price change: {n['title']} ${o['price']} → ${n['price']} ({pct:+.1f}%)",
            })
        if o["available"] != n["available"]:
            status = "back in stock ✅" if n["available"] else "out of stock ⚠️"
            changes.append({
                "type":  "stock_change",
                "brand": brand,
                "title": n["title"],
                "url":   n.get("url", ""),
                "msg":   f"Stock: {n['title']} is now {status}",
            })

    return changes

def diff_blogs(brand: str, old: dict, new: dict) -> list[dict]:
    changes = []
    for aid in set(new) - set(old):
        a = new[aid]
        changes.append({
            "type":  "new_article",
            "brand": brand,
            "title": a["title"],
            "url":   a.get("url", ""),
            "msg":   f"📝 New article ({brand}): {a['title']}",
        })
    return changes

def diff_pages(brand: str, page_name: str, old_hash: str | None, new_hash: str | None, url: str) -> list[dict]:
    if old_hash is None or new_hash is None:
        return []
    if old_hash != new_hash:
        return [{
            "type":  "page_change",
            "brand": brand,
            "title": page_name,
            "url":   url,
            "msg":   f"🔄 Page changed ({brand}): {page_name} — {url}",
        }]
    return []

# ─── Notifications ────────────────────────────────────────────────────────────

def send_slack(changes: list[dict]):
    webhook = os.getenv("SLACK_WEBHOOK")
    if not webhook or not changes:
        return
    lines = [f"*Competitive Monitor — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*"]
    for c in changes:
        lines.append(f"• {c['msg']}")
        if c.get("url"):
            lines.append(f"  <{c['url']}|View>")
    payload = {"text": "\n".join(lines)}
    try:
        httpx.post(webhook, json=payload, timeout=10)
        print(f"  [slack] Sent {len(changes)} changes")
    except Exception as e:
        print(f"  [slack] Failed: {e}")

def send_wechat(changes: list[dict]):
    key = os.getenv("WECHAT_WEBHOOK_KEY")
    if not key or not changes:
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    lines = [f"**竞品监控 {datetime.now(timezone.utc).strftime('%m-%d %H:%M')}**"]
    for c in changes:
        lines.append(f"> {c['msg']}")
    payload = {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}
    try:
        httpx.post(url, json=payload, timeout=10)
        print(f"  [wechat] Sent {len(changes)} changes")
    except Exception as e:
        print(f"  [wechat] Failed: {e}")

def send_email(changes: list[dict]):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    email_to  = os.getenv("EMAIL_TO")
    if not all([smtp_user, smtp_pass, email_to]) or not changes:
        return

    body_lines = [f"Competitive Monitor Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"]
    by_brand: dict[str, list] = {}
    for c in changes:
        by_brand.setdefault(c["brand"], []).append(c)
    for brand, items in by_brand.items():
        body_lines.append(f"\n── {brand} ──")
        for item in items:
            body_lines.append(f"  {item['msg']}")
            if item.get("url"):
                body_lines.append(f"  {item['url']}")

    msg = MIMEMultipart()
    msg["Subject"] = f"[Monitor] {len(changes)} changes detected"
    msg["From"]    = smtp_user
    msg["To"]      = email_to
    msg.attach(MIMEText("\n".join(body_lines), "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, email_to, msg.as_string())
        print(f"  [email] Sent to {email_to}")
    except Exception as e:
        print(f"  [email] Failed: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def run():
    print(f"\n{'='*60}")
    print(f"Thermal Monitor run @ {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")

    state   = load_state()
    all_changes: list[dict] = []

    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for brand_name, cfg in BRANDS.items():
            print(f"\n[{brand_name}]")
            brand_state = state.setdefault(brand_name, {})

            # ── Products ──────────────────────────────────────────────
            if cfg["type"] == "shopify":
                print("  Fetching Shopify products...")
                new_products = fetch_shopify_products(cfg["products_api"], client)
                old_products = brand_state.get("products", {})
                changes = diff_products(brand_name, old_products, new_products)
                if changes:
                    print(f"  → {len(changes)} product change(s)")
                    all_changes.extend(changes)
                brand_state["products"] = new_products

                # Shopify blog
                print("  Fetching Shopify blog...")
                new_blog = fetch_shopify_blogs(cfg["blog_api"], client)
                old_blog = brand_state.get("blog", {})
                changes = diff_blogs(brand_name, old_blog, new_blog)
                if changes:
                    print(f"  → {len(changes)} new article(s)")
                    all_changes.extend(changes)
                brand_state["blog"] = new_blog

            else:
                # Generic brand: hash-based product page monitor
                print("  Fetching product page hash...")
                new_hash = fetch_generic_products_hash(cfg["products_url"], client)
                old_hash = brand_state.get("products_hash")
                changes  = diff_pages(brand_name, "Products listing", old_hash, new_hash, cfg["products_url"])
                if changes:
                    print(f"  → Product page changed")
                    all_changes.extend(changes)
                brand_state["products_hash"] = new_hash

                # Generic blog page hash
                print("  Fetching blog page hash...")
                new_blog_hash = fetch_page_hash(cfg["blog_url"], client)
                old_blog_hash = brand_state.get("blog_hash")
                changes = diff_pages(brand_name, "Blog", old_blog_hash, new_blog_hash, cfg["blog_url"])
                if changes:
                    print(f"  → Blog page changed")
                    all_changes.extend(changes)
                brand_state["blog_hash"] = new_blog_hash

            # ── Key pages ─────────────────────────────────────────────
            page_state = brand_state.setdefault("pages", {})
            for page_name, url in cfg["pages"].items():
                print(f"  Checking page: {page_name}...")
                new_hash = fetch_page_hash(url, client)
                old_hash = page_state.get(page_name)
                changes  = diff_pages(brand_name, page_name, old_hash, new_hash, url)
                if changes:
                    print(f"  → {page_name} changed!")
                    all_changes.extend(changes)
                if new_hash:
                    page_state[page_name] = new_hash

    # ── Save state & notify ───────────────────────────────────────────
    save_state(state)

    print(f"\n{'─'*60}")
    print(f"Total changes: {len(all_changes)}")

    if all_changes:
        for c in all_changes:
            print(f"  {c['msg']}")
        send_slack(all_changes)
        send_wechat(all_changes)
        send_email(all_changes)
    else:
        print("  No changes detected.")

    print(f"{'─'*60}\n")

if __name__ == "__main__":
    run()
