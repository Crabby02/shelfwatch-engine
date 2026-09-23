#!/usr/bin/env python3
"""Shelfwatch tracker: fetches product pages, extracts public product data,
and saves a dated snapshot. Run daily via cron.

Reads: watchlist.json  (list of {id, store, name, url})
Writes: data/snapshots/YYYY-MM-DD.json
Only reads publicly visible page content. Normal visitor User-Agent.
"""
import json
import re
import sys
import html as html_lib
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")


class LDJsonExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_ld = False
        self.blocks = []
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attrs = dict(attrs)
            if attrs.get("type") == "application/ld+json":
                self._in_ld = True
                self._buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.blocks.append("".join(self._buf))

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)


def find_product(nodes, want=("Product", "ProductGroup")):
    """Walk JSON-LD nodes, return first matching @type dict.
    Prefers Product over ProductGroup (variant-level precision)."""
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        return None
    fallback = None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" in types:
            return node
        if "ProductGroup" in types and fallback is None:
            fallback = node
        for key in ("@graph", "mainEntity", "itemListElement", "hasVariant"):
            found = find_product(node.get(key), want)
            if found:
                return found
    return fallback


def clean_price(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[\d,.]+", str(value))
    if not m:
        return None
    return float(m.group(0).replace(",", ""))


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def meta_og_price(html):
    """Fallback: og:price:amount / og:price:currency meta tags."""
    amt = re.search(r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([^"\']+)["\']', html)
    if not amt:
        amt = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:price:amount["\']', html)
    cur = re.search(r'<meta[^>]+property=["\']og:price:currency["\'][^>]+content=["\']([^"\']+)["\']', html)
    if not cur:
        cur = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:price:currency["\']', html)
    return (clean_price(amt.group(1)) if amt else None,
            cur.group(1) if cur else None)


def meta_og_title(html):
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html)
    return m.group(1) if m else None


def next_data_price(html):
    """Fallback for Next.js storefronts (e.g. Gymshark): price lives in
    the __NEXT_DATA__ blob under props.pageProps.productData.product."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    try:
        p = data["props"]["pageProps"]["productData"]["product"]
    except (KeyError, TypeError):
        return None
    price = clean_price(p.get("price"))
    if price is None:
        return None
    in_stock = p.get("inStock")
    if in_stock is None:
        sizes = p.get("sizesInStock") or []
        in_stock = bool(sizes)
    return {"name": p.get("title"), "price": price,
            "currency": p.get("currency") or None,
            "availability": "InStock" if in_stock else "OutOfStock",
            "rating": None, "review_count": None, "source": "next_data"}


def shopify_js(url):
    """Fallback for Shopify stores: /products/<handle>.js returns product JSON."""
    m = re.match(r"(https?://[^/]+)(/products/[^?#]+)", url)
    if not m:
        return None
    try:
        req = urllib.request.Request(m.group(1) + m.group(2) + ".js",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        variants = data.get("variants") or []
        prices = [clean_price(v.get("price")) for v in variants
                  if clean_price(v.get("price")) is not None]
        prices = [p / 100 for p in prices]
        avail = any(v.get("available") for v in variants) if variants else None
        return {
            "name": data.get("title"),
            "price": min(prices) if prices else None,
            "currency": None,
            "availability": "InStock" if avail else ("OutOfStock" if avail is False else None),
            "rating": None,
            "review_count": None,
        }
    except Exception:
        return None


def extract(html, url=""):
    result = None
    parser = LDJsonExtractor()
    parser.feed(html)
    for block in parser.blocks:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        product = find_product(data)
        if not product:
            continue
        offers = product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        result = {
            "name": product.get("name"),
            "price": clean_price(offers.get("price")),
            "currency": offers.get("priceCurrency"),
            "availability": (offers.get("availability") or "").split("/")[-1] or None,
            "rating": (product.get("aggregateRating") or {}).get("ratingValue"),
            "review_count": (product.get("aggregateRating") or {}).get("reviewCount"),
        }
        if result["price"] is not None:
            break
    if result is not None and result.get("price") is None:
        result = None
    if result is None:
        og_price, og_cur = meta_og_price(html)
        if og_price is not None:
            result = {"name": meta_og_title(html), "price": og_price,
                      "currency": og_cur, "availability": None,
                      "rating": None, "review_count": None, "source": "og_meta"}
    if result is None:
        result = next_data_price(html)
    if result is None and "/products/" in url:
        js = shopify_js(url)
        if js and js["price"] is not None:
            js["source"] = "shopify_js"
            result = js
    if result and not result.get("currency"):
        _, og_cur = meta_og_price(html)
        if og_cur:
            result["currency"] = og_cur
    if result and result.get("name"):
        result["name"] = html_lib.unescape(result["name"]).strip()
    return result


def main():
    watchlist_path = HERE / "watchlist.json"
    if not watchlist_path.exists():
        print("watchlist.json not found", file=sys.stderr)
        sys.exit(1)
    watchlist = json.loads(watchlist_path.read_text())
    snapshot = {"date": str(date.today()), "items": []}
    for entry in watchlist:
        item = {"id": entry["id"], "store": entry["store"], "url": entry["url"]}
        try:
            html = fetch(entry["url"])
            data = extract(html, entry["url"])
            if data:
                item.update(data)
                item["status"] = "ok"
            else:
                item["status"] = "no_product_data"
        except Exception as e:
            item["status"] = "error"
            item["error"] = f"{type(e).__name__}: {e}"
        snapshot["items"].append(item)
        print(f"{entry['id']}: {item.get('status')} {item.get('name') or ''} {item.get('price') or ''}")
    out_dir = HERE / "data" / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{snapshot['date']}.json"
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    print(f"saved {out_path}")


if __name__ == "__main__":
    main()
