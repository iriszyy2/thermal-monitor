"""
Thermal Camera Competitive Monitor v8
- TOPDON pages: full text diff (see exactly what changed)
- TOPDON TC/TS products: price/stock/new + product page text diff
- FLIR/FLUKE: hash-only
"""

import os, json, hashlib, re, httpx, smtplib, difflib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── Config ───────────────────────────────────────────────────────────────────

THERMAL_RE = re.compile(
    r'\b(tc\d|ts\d|tc0|ts0|thermal|infrared|imager|thermograph)',
    re.IGNORECASE
)
THERMAL_BLOG_KW = [
    "thermal", "infrared", "imager", "thermograph",
    "TC001", "TC002", "TC003", "TC004", "TC005",
    "TS001", "TS004", "TS005", "TopInfrared",
]

BRANDS = {
    "TOPDON": {
        "type": "shopify_filtered",
        "base": "https://www.topdon.com",
        "products_api": "https://www.topdon.com/products.json",
        "blog_api":     "https://www.topdon.com/blogs/news.json",
        "blog_keywords": THERMAL_BLOG_KW,
        "nav_url":      "https://www.topdon.com/",
        # Pages with full text diff
        "pages": {
            "Thermal Imagers":           "https://www.topdon.com/pages/tools/thermal-imagers",
            "Thermal Apps":              "https://www.topdon.com/pages/topdon-apps-thermal-imagers",
            "Solution: Home Inspection": "https://www.topdon.com/pages/solutions-home-inspection",
            "About Us":                  "https://www.topdon.com/pages/about-us",
            "Newsroom":                  "https://www.topdon.com/pages/news",
            "Program: TestLight":        "https://www.topdon.com/pages/topdon-testlight-program",
            "Program: TOP-UP":           "https://www.topdon.com/pages/top-up-program",
            "Program: My Story":         "https://www.topdon.com/pages/my-topdon-story",
            "Program: Partner":          "https://www.topdon.com/pages/become-a-topdon-partner",
            "Refund Policy":             "https://www.topdon.com/policies/refund-policy",
        },
    },
    "FLIR": {
        "type": "generic",
        "base": "https://www.flir.com",
        "products_url": "https://www.flir.com/browse/cameras/thermal-cameras/",
        "blog_url":     "https://www.flir.com/discover/professional-tools/",
        "pages": {
            "About Us":      "https://www.flir.com/about/",
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
            "Return Policy": "https://www.fluke.com/en-us/support/return-policy",
        },
    },
}

STATE_FILE          = Path("monitor_state.json")
DATA_DIR            = Path("docs")
DATA_FILE           = DATA_DIR / "data.json"
MAX_HISTORY         = 300
PAGE_COOLDOWN_HOURS = 6
MAX_DIFF_LINES      = 8   # max diff lines to show per page change

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control":   "no-cache",
}

TYPE_LABEL = {
    "new_product":     "NEW",
    "removed_product": "REMOVED",
    "price_change":    "PRICE",
    "stock_change":    "STOCK",
    "new_article":     "BLOG",
    "page_change":     "PAGE",
    "nav_change":      "NAV",
    "product_page":    "PRODUCT PAGE",
}
TAG_COLOR = {
    "NEW": "#16a34a", "REMOVED": "#dc2626", "PRICE": "#d97706",
    "STOCK": "#2563eb", "BLOG": "#7c3aed", "PAGE": "#0891b2",
    "NAV": "#be185d", "PRODUCT PAGE": "#0891b2",
}

# ─── State ────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: pass
    return {}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))

def load_dashboard():
    DATA_DIR.mkdir(exist_ok=True)
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text())
            if "history" in d and "changes" not in d:
                d["changes"] = d.pop("history")
                for c in d["changes"]:
                    if "label" not in c:
                        c["label"] = TYPE_LABEL.get(c.get("type", ""), "?")
            return d
        except: pass
    return {
        "generated_at": "", "brand_list": list(BRANDS.keys()),
        "stats": {"total": 0, "today": 0, "week": 0},
        "changes": [], "page_status": {}, "product_counts": {},
    }

def save_dashboard(d):
    DATA_DIR.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False))

# ─── Text extraction ──────────────────────────────────────────────────────────

def extract_text(html: str) -> str:
    """Extract clean visible text from HTML for diffing."""
    # Remove scripts (all types)
    t = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
    # Remove Shopify <template> tags - contain obfuscated JSON that causes garbled text
    t = re.sub(r'<template[^>]*>.*?</template>', '', t, flags=re.DOTALL|re.IGNORECASE)
    t = re.sub(r'<style[^>]*>.*?</style>', '', t, flags=re.DOTALL|re.IGNORECASE)
    t = re.sub(r'<!--.*?-->', '', t, flags=re.DOTALL)
    # Strip tag attributes to remove data-* encoded content
    t = re.sub(r'<([a-zA-Z][a-zA-Z0-9]*)[^>]*>', r'<\1>', t)
    # Line breaks at block elements
    t = re.sub(r'</(p|div|li|h[1-6]|section|article|td|tr)>', '\n', t, flags=re.IGNORECASE)
    t = re.sub(r'<br>', '\n', t, flags=re.IGNORECASE)
    t = re.sub(r'<[^>]+>', ' ', t)
    lines = []
    for line in t.split('\n'):
        line = re.sub(r'\s+', ' ', line).strip()
        if len(line) < 25:
            continue
        special = sum(1 for c in line if c in '{}[]\\^$*+=|@#%&;:<>')
        if len(line) > 0 and special / len(line) > 0.12:
            continue
        lines.append(line)
    return '\n'.join(lines)

def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]

def compute_diff(old_text: str, new_text: str) -> list[dict]:
    """Return list of {type, old, new} change objects, max MAX_DIFF_LINES items."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    changes = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=True)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == 'equal':
            continue
        old_chunk = old_lines[i1:i2]
        new_chunk = new_lines[j1:j2]
        if op == 'insert':
            for line in new_chunk:
                if line.strip():
                    changes.append({"type": "added",   "text": line.strip()})
        elif op == 'delete':
            for line in old_chunk:
                if line.strip():
                    changes.append({"type": "removed", "text": line.strip()})
        elif op == 'replace':
            # Pair up old/new lines
            for i in range(max(len(old_chunk), len(new_chunk))):
                old_l = old_chunk[i].strip() if i < len(old_chunk) else ""
                new_l = new_chunk[i].strip() if i < len(new_chunk) else ""
                if old_l and new_l:
                    changes.append({"type": "modified", "old": old_l, "new": new_l})
                elif new_l:
                    changes.append({"type": "added",   "text": new_l})
                elif old_l:
                    changes.append({"type": "removed", "text": old_l})
        if len(changes) >= MAX_DIFF_LINES:
            break
    return changes[:MAX_DIFF_LINES]

def format_diff_detail(diffs: list[dict], total_changes: int) -> str:
    """Format diff list into a readable string for email/dashboard."""
    lines = []
    for d in diffs:
        if d["type"] == "added":
            lines.append(f"+ {d['text'][:120]}")
        elif d["type"] == "removed":
            lines.append(f"- {d['text'][:120]}")
        elif d["type"] == "modified":
            lines.append(f"~ {d['old'][:80]} → {d['new'][:80]}")
    if total_changes > MAX_DIFF_LINES:
        lines.append(f"... and {total_changes - MAX_DIFF_LINES} more changes")
    return "\n".join(lines) if lines else "Content updated (diff unavailable)"

# ─── Fetchers ─────────────────────────────────────────────────────────────────

def fetch_page(url: str, client: httpx.Client) -> tuple[str|None, str|None]:
    """Returns (raw_html, extracted_text) or (None, None) on error."""
    try:
        r = client.get(url, timeout=25); r.raise_for_status()
        text = extract_text(r.text)
        return r.text, text
    except Exception as e:
        print(f"    [warn] {url}: {e}"); return None, None

def fetch_shopify_products(api_url, base, client):
    products, page = {}, 1
    while True:
        try:
            r = client.get(f"{api_url}?limit=250&page={page}", timeout=20)
            r.raise_for_status()
            data = r.json().get("products", [])
        except Exception as e:
            print(f"    [warn] products p{page}: {e}"); break
        if not data: break
        for p in data:
            h     = p["handle"]
            title = p.get("title", "")
            tags  = " ".join(p.get("tags", []))
            if not THERMAL_RE.search(f"{h} {title} {tags}"):
                continue
            v = next((x for x in p.get("variants", []) if x.get("available")),
                     p["variants"][0] if p.get("variants") else {})
            raw = v.get("price", "")
            try: price = f"{float(raw):.2f}" if raw else "N/A"
            except: price = raw or "N/A"
            products[h] = {
                "title":     title,
                "price":     price,
                "available": any(x.get("available") for x in p.get("variants", [])),
                "url":       f"{base}/products/{h}",
            }
        page += 1
        if len(data) < 250: break
    return products

def fetch_shopify_blog(api_url, base, keywords, client):
    try:
        r = client.get(api_url, timeout=20); r.raise_for_status()
        out = {}
        for a in r.json().get("articles", []):
            text = f"{a.get('title', '')} {a.get('body_html', '')}"
            if any(k.lower() in text.lower() for k in keywords):
                out[str(a["id"])] = {
                    "title":        a["title"],
                    "published_at": a.get("published_at", ""),
                    "url":          f"{base}/blogs/news/{a['handle']}",
                }
        return out
    except Exception as e:
        print(f"    [warn] blog: {e}"); return {}

# ─── Diff helpers ─────────────────────────────────────────────────────────────

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def mk(type_, brand, title, url, detail, price="", diffs=None):
    return {
        "id":     f"{now_iso()}-{brand}-{type_}-{hashlib.md5(title.encode()).hexdigest()[:6]}",
        "type":   type_, "label": TYPE_LABEL.get(type_, "?"),
        "brand":  brand, "ts":    now_iso(),
        "title":  title, "url":   url,
        "detail": detail, "price": price,
        "diffs":  diffs or [],   # list of {type, text/old/new}
    }

def diff_products(brand, old, new):
    out = []
    for h in sorted(set(new) - set(old)):
        p = new[h]
        ps = f"${p['price']}" if p["price"] != "N/A" else "Price TBD"
        out.append(mk("new_product", brand, p["title"], p["url"],
                      f"{ps} · {'In stock' if p['available'] else 'Out of stock'}", p["price"]))
    for h in sorted(set(old) - set(new)):
        p = old[h]
        out.append(mk("removed_product", brand, p["title"], p.get("url",""), "Removed from store"))
    for h in sorted(set(old) & set(new)):
        o, n = old[h], new[h]
        if o["price"] != n["price"] and "N/A" not in (o["price"], n["price"]):
            try:
                op, np_ = float(o["price"]), float(n["price"])
                pct = (np_ - op) / op * 100
                out.append(mk("price_change", brand, n["title"], n["url"],
                              f"${o['price']} → ${n['price']} ({pct:+.1f}%)", n["price"]))
            except: pass
        if o["available"] != n["available"]:
            out.append(mk("stock_change", brand, n["title"], n["url"],
                          "Back in stock" if n["available"] else "Out of stock", n["price"]))
    return out

def diff_blog(brand, old, new):
    return [mk("new_article", brand, new[aid]["title"], new[aid].get("url",""),
               f"Published {new[aid].get('published_at','')[:10]}")
            for aid in sorted(set(new) - set(old))]

def diff_page_text(brand, name, old_text, new_text, url, last_alerted,
                   cooldown=PAGE_COOLDOWN_HOURS):
    """Full text diff for TOPDON pages."""
    if not old_text or not new_text: return []
    old_h = text_hash(old_text)
    new_h = text_hash(new_text)
    if old_h == new_h: return []
    if last_alerted:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(last_alerted) \
               < timedelta(hours=cooldown):
                print(f"    [cooldown] {brand} · {name}"); return []
        except: pass
    # Compute diff
    all_diffs = compute_diff(old_text, new_text)
    total_changes = sum(1 for op, i1, i2, j1, j2 in
                        difflib.SequenceMatcher(None, old_text.splitlines(),
                                                new_text.splitlines()).get_opcodes()
                        if op != 'equal')
    detail = format_diff_detail(all_diffs, total_changes)
    return [mk("page_change", brand, name, url, detail, diffs=all_diffs)]

def diff_page_hash(brand, name, old_h, new_h, url, last_alerted,
                   cooldown=PAGE_COOLDOWN_HOURS):
    """Hash-only diff for FLIR/FLUKE."""
    if not old_h or not new_h or old_h == new_h: return []
    if last_alerted:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(last_alerted) \
               < timedelta(hours=cooldown):
                print(f"    [cooldown] {brand} · {name}"); return []
        except: pass
    return [mk("page_change", brand, name, url, "Hash-only: page changed (diff unavailable)")]

def diff_nav(brand, old_h, new_h, url, last_alerted):
    if not old_h or not new_h or old_h == new_h: return []
    if last_alerted:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(last_alerted) \
               < timedelta(hours=12):
                print(f"    [cooldown] {brand} nav"); return []
        except: pass
    return [mk("nav_change", brand, "Navigation menu", url, "Menu structure changed")]

# ─── Dashboard ────────────────────────────────────────────────────────────────

def update_dashboard(dash, new_changes, page_status, product_counts):
    now     = datetime.now(timezone.utc)
    today   = now.strftime("%Y-%m-%d")
    week_ts = (now - timedelta(days=7)).timestamp()
    dash["changes"] = (new_changes + dash.get("changes", []))[:MAX_HISTORY]
    dash.update({
        "generated_at":   now.isoformat(),
        "brand_list":     list(BRANDS.keys()),
        "page_status":    page_status,
        "product_counts": product_counts,
    })
    all_c = dash["changes"]
    dash["stats"] = {
        "total": len(all_c),
        "today": sum(1 for c in all_c if c.get("ts","")[:10] == today),
        "week":  sum(1 for c in all_c if c.get("ts") and
                     datetime.fromisoformat(c["ts"].replace("Z","+00:00")).timestamp() >= week_ts),
    }

# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(changes, run_time):
    h  = os.getenv("SMTP_HOST", "smtp.gmail.com")
    p  = int(os.getenv("SMTP_PORT", "587"))
    u  = os.getenv("SMTP_USER", "")
    pw = os.getenv("SMTP_PASS", "")
    to = os.getenv("EMAIL_TO", "")
    pu = os.getenv("PAGES_URL", "")
    if not all([u, pw, to]): print("  [email] skipped"); return

    by_brand = {}
    for c in changes: by_brand.setdefault(c["brand"], []).append(c)

    # Plain text
    lines = [f"Competitive Monitor — {run_time}", "="*56]
    for brand, items in by_brand.items():
        lines += [f"\n{brand} ({len(items)} change{'s' if len(items)>1 else ''})", "─"*36]
        for c in items:
            lines += [f"  [{c['label']}] {c['title']}", f"  {c['detail']}"]
            if c.get("url"): lines.append(f"  {c['url']}")
    if pu: lines.append(f"\nDashboard: {pu}")

    # HTML
    rows = ""
    for brand, items in by_brand.items():
        rows += (f'<tr><td colspan="2" style="padding:14px 0 6px;font-size:15px;font-weight:600;'
                 f'border-bottom:2px solid #e5e7eb">{brand} '
                 f'<span style="font-weight:400;font-size:12px;color:#6b7280">'
                 f'{len(items)} change{"s" if len(items)>1 else ""}</span></td></tr>')
        for c in items:
            col = TAG_COLOR.get(c["label"], "#6b7280")
            tit = (f'<a href="{c["url"]}" style="color:#1d4ed8;text-decoration:none">'
                   f'{c["title"]}</a>') if c.get("url") else c["title"]
            # Build diff block if available
            diff_html = ""
            if c.get("diffs"):
                diff_lines = []
                for d in c["diffs"]:
                    if d["type"] == "added":
                        diff_lines.append(
                            f'<div style="background:#f0fdf4;color:#15803d;padding:2px 6px;'
                            f'border-left:3px solid #86efac;margin:1px 0;font-size:12px;'
                            f'font-family:monospace">+ {d["text"][:120]}</div>')
                    elif d["type"] == "removed":
                        diff_lines.append(
                            f'<div style="background:#fef2f2;color:#b91c1c;padding:2px 6px;'
                            f'border-left:3px solid #fca5a5;margin:1px 0;font-size:12px;'
                            f'font-family:monospace">- {d["text"][:120]}</div>')
                    elif d["type"] == "modified":
                        diff_lines.append(
                            f'<div style="background:#fffbeb;color:#92400e;padding:2px 6px;'
                            f'border-left:3px solid #fcd34d;margin:1px 0;font-size:12px;'
                            f'font-family:monospace">'
                            f'~ {d["old"][:80]}<br>'
                            f'&nbsp;&nbsp;→ {d["new"][:80]}</div>')
                diff_html = "".join(diff_lines)
            elif "Hash-only" in c.get("detail", ""):
                diff_html = (f'<div style="color:#6b7280;font-size:12px;font-style:italic;'
                             f'margin-top:4px">Hash-only: diff unavailable</div>')

            rows += (f'<tr style="border-bottom:1px solid #f3f4f6">'
                     f'<td style="padding:9px 8px 9px 0;width:100px;vertical-align:top">'
                     f'<span style="background:{col}20;color:{col};font-size:11px;'
                     f'font-weight:700;padding:3px 7px;border-radius:4px">{c["label"]}</span>'
                     f'</td>'
                     f'<td style="padding:9px 0;font-size:14px;vertical-align:top">'
                     f'{tit}'
                     f'<div style="font-size:12px;color:#6b7280;margin-top:2px">{c["detail"][:200]}</div>'
                     f'{diff_html}'
                     f'</td></tr>')

    dl = (f'<p style="margin-top:20px"><a href="{pu}" style="color:#1d4ed8">'
          f'View dashboard →</a></p>') if pu else ""
    html = (f'<!DOCTYPE html><html><body style="font-family:-apple-system,sans-serif;'
            f'max-width:680px;margin:0 auto;padding:24px;color:#111">'
            f'<div style="margin-bottom:18px">'
            f'<div style="font-size:20px;font-weight:700">Competitive Monitor</div>'
            f'<div style="font-size:12px;color:#9ca3af;margin-top:3px">'
            f'{run_time} · {len(changes)} changes</div></div>'
            f'<table style="width:100%;border-collapse:collapse">{rows}</table>{dl}'
            f'<div style="margin-top:20px;font-size:11px;color:#9ca3af;'
            f'border-top:1px solid #e5e7eb;padding-top:10px">TOPDON · FLIR · FLUKE</div>'
            f'</body></html>')

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"[Monitor] {len(changes)} change{'s' if len(changes)>1 else ''} "
                      f"— {run_time[:10]}")
    msg["From"] = u; msg["To"] = to
    msg.attach(MIMEText("\n".join(lines), "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(h, p) as s:
            s.ehlo(); s.starttls(); s.ehlo(); s.login(u, pw)
            s.sendmail(u, to, msg.as_string())
        print(f"  [email] sent → {to}")
    except Exception as e:
        print(f"  [email] FAILED: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def run():
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}\nMonitor v8  {run_time}\n{'='*60}")

    state    = load_state()
    dash     = load_dashboard()
    is_first = len(state) == 0
    if is_first: print("\n>>> FIRST RUN — building baseline only.\n")

    all_changes, page_status, product_counts = [], {}, {}

    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for brand, cfg in BRANDS.items():
            print(f"\n[{brand}]")
            bs = state.setdefault(brand, {})
            page_status[brand] = {}

            if cfg["type"] == "shopify_filtered":
                # TC/TS products: price/stock/new
                print("  products (TC/TS)...")
                new_p = fetch_shopify_products(cfg["products_api"], cfg["base"], client)
                old_p = bs.get("products", {})
                product_counts[brand] = len(new_p)
                if not is_first:
                    ch = diff_products(brand, old_p, new_p)
                    if ch: print(f"  → {len(ch)} product data change(s)")
                    all_changes.extend(ch)
                else:
                    print(f"  → baseline: {len(new_p)} thermal products")
                bs["products"] = new_p

                # TC/TS product pages: text diff on description/specs
                print("  product pages (text diff)...")
                prod_snapshots = bs.setdefault("product_snapshots", {})
                prod_alerted   = bs.setdefault("product_page_alerted", {})
                for handle, prod in new_p.items():
                    url = prod["url"]
                    _, new_text = fetch_page(url, client)
                    if new_text is None: continue
                    old_text = prod_snapshots.get(handle)
                    if not is_first and old_text:
                        ch = diff_page_text(
                            brand, f"{prod['title']} (product page)",
                            old_text, new_text, url, prod_alerted.get(handle)
                        )
                        if ch:
                            all_changes.extend(ch)
                            prod_alerted[handle] = now_iso()
                            print(f"  → {prod['title']} page changed")
                    prod_snapshots[handle] = new_text

                # Blog
                print("  blog...")
                new_b = fetch_shopify_blog(
                    cfg["blog_api"], cfg["base"], cfg["blog_keywords"], client)
                if not is_first:
                    ch = diff_blog(brand, bs.get("blog", {}), new_b)
                    if ch: print(f"  → {len(ch)} new article(s)")
                    all_changes.extend(ch)
                bs["blog"] = new_b

                # Nav structure
                print("  nav...")
                _, nav_text = fetch_page(cfg["nav_url"], client)
                new_nh = text_hash(nav_text) if nav_text else None
                if not is_first:
                    ch = diff_nav(brand, bs.get("nav_hash"), new_nh,
                                  cfg["nav_url"], bs.get("nav_alerted"))
                    if ch:
                        all_changes.extend(ch)
                        bs["nav_alerted"] = now_iso()
                bs["nav_hash"] = new_nh

                # Key pages: full text diff
                ps = bs.setdefault("page_snapshots", {})
                pa = bs.setdefault("page_alerted", {})
                for pname, url in cfg["pages"].items():
                    print(f"  {pname}...")
                    _, new_text = fetch_page(url, client)
                    changed = False
                    if not is_first and new_text:
                        old_text = ps.get(pname)
                        ch = diff_page_text(brand, pname, old_text, new_text,
                                            url, pa.get(pname))
                        if ch:
                            changed = True
                            all_changes.extend(ch)
                            pa[pname] = now_iso()
                            print(f"  → {pname} changed!")
                    page_status[brand][pname] = {
                        "url":        url,
                        "status":     "changed" if changed else ("ok" if new_text else "error"),
                        "checked_at": now_iso(),
                    }
                    if new_text: ps[pname] = new_text

            else:
                # FLIR/FLUKE: hash-only
                hs = bs.setdefault("page_hashes", {})
                ha = bs.setdefault("page_alerted", {})

                for key, url, label in [
                    ("products_hash", cfg["products_url"], "Products page"),
                    ("blog_hash",     cfg["blog_url"],     "Blog page"),
                ]:
                    print(f"  {label}...")
                    _, text = fetch_page(url, client)
                    new_h = text_hash(text) if text else None
                    if not is_first:
                        ch = diff_page_hash(brand, label, hs.get(key), new_h,
                                            url, ha.get(key))
                        if ch:
                            all_changes.extend(ch)
                            ha[key] = now_iso()
                    hs[key] = new_h

                for pname, url in cfg["pages"].items():
                    print(f"  {pname}...")
                    _, text = fetch_page(url, client)
                    new_h = text_hash(text) if text else None
                    changed = False
                    if not is_first:
                        ch = diff_page_hash(brand, pname, hs.get(pname), new_h,
                                            url, ha.get(pname))
                        if ch:
                            changed = True
                            all_changes.extend(ch)
                            ha[pname] = now_iso()
                    page_status[brand][pname] = {
                        "url":        url,
                        "status":     "changed" if changed else ("ok" if new_h else "error"),
                        "checked_at": now_iso(),
                    }
                    hs[pname] = new_h

    save_state(state)
    update_dashboard(dash, all_changes, page_status, product_counts)
    save_dashboard(dash)
    print(f"\n  [data] {len(dash['changes'])} records → docs/data.json")

    print(f"\n{'─'*60}")
    if is_first:
        print("Baseline saved. Alerts active from next run.")
    elif all_changes:
        print(f"{len(all_changes)} change(s):")
        for c in all_changes:
            print(f"  [{c['label']}] {c['brand']} · {c['title']} — {c['detail'][:80]}")
        send_email(all_changes, run_time)
    else:
        print("No changes. No email sent.")
    print(f"{'─'*60}\n")

if __name__ == "__main__":
    run()
