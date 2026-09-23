#!/usr/bin/env python3
"""Shelfwatch hosted tracker: checks every watched product and stores
snapshots in Supabase. Runs on GitHub Actions (daily cron).

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY
Only reads publicly visible page content. Normal visitor User-Agent.
Polite: 2s between requests, 8s per-domain throttle, 2000 product cap.
"""
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

import track  # local extraction engine (fetch + extract)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
PER_REQUEST_DELAY = 2.0
PER_DOMAIN_DELAY = 8.0
MAX_PRODUCTS = 2000


def sb(path, method="GET", params=None, json_body=None):
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    r = requests.request(method, f"{SUPABASE_URL}/rest/v1/{path}",
                         headers=headers, params=params, json=json_body,
                         timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Supabase {method} {path}: {r.status_code} {r.text[:200]}")
    if method == "GET":
        return r.json()
    return None


def main():
    if not SUPABASE_URL or not SERVICE_KEY:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY required", file=sys.stderr)
        sys.exit(1)
    products = []
    offset = 0
    while True:
        batch = sb("products", params={
            "select": "id,url,name",
            "order": "last_checked_at.asc.nullsfirst",
            "limit": "500",
            "offset": str(offset),
        })
        products.extend(batch)
        if len(batch) < 500 or len(products) >= MAX_PRODUCTS:
            break
        offset += 500
    products = products[:MAX_PRODUCTS]
    print(f"{len(products)} products to check")
    now = datetime.now(timezone.utc).isoformat()
    domain_last = {}
    ok = err = nodata = 0
    for p in products:
        domain = urlparse(p["url"]).netloc
        wait = PER_DOMAIN_DELAY - (time.time() - domain_last.get(domain, 0))
        if wait > 0:
            time.sleep(wait)
        try:
            html = track.fetch(p["url"])
            data = track.extract(html, p["url"])
            domain_last[domain] = time.time()
            if data and data.get("price") is not None:
                sb("snapshots", "POST", json_body={
                    "product_id": p["id"],
                    "price": data["price"],
                    "currency": data.get("currency"),
                    "availability": data.get("availability"),
                    "name": data.get("name"),
                    "raw": {"rating": data.get("rating"),
                            "review_count": data.get("review_count")},
                })
                sb(f"products?id=eq.{p['id']}", "PATCH", json_body={
                    "name": data.get("name") or p.get("name"),
                    "last_price": data["price"],
                    "last_currency": data.get("currency"),
                    "last_availability": data.get("availability"),
                    "last_checked_at": now,
                })
                ok += 1
                print(f"ok {p['id']} {data.get('name')} {data['price']}")
            else:
                sb(f"products?id=eq.{p['id']}", "PATCH",
                   json_body={"last_checked_at": now})
                nodata += 1
                print(f"no_product_data {p['id']} {p['url']}")
        except Exception as e:
            err += 1
            print(f"error {p['id']} {type(e).__name__}: {e}")
        time.sleep(PER_REQUEST_DELAY)
    print(f"done: {ok} ok, {nodata} no_product_data, {err} error")


if __name__ == "__main__":
    main()
