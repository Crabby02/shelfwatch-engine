#!/usr/bin/env python3
"""Sync Stripe subscriptions -> Supabase customer plans.
Runs daily on GitHub Actions. No webhook server needed; plan changes
take effect within 24h, which is fine for launch.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, STRIPE_SECRET_KEY,
     STRIPE_PRICE_PRO, STRIPE_PRICE_SCALE
"""
import os
import sys

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
PRICE_PRO = os.environ.get("STRIPE_PRICE_PRO", "")
PRICE_SCALE = os.environ.get("STRIPE_PRICE_SCALE", "")

PRICE_TO_PLAN = {PRICE_PRO: "pro", PRICE_SCALE: "scale"}


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
    return r.json() if method == "GET" else None


def stripe_get(path, params=None):
    r = requests.get(f"https://api.stripe.com/v1/{path}",
                     auth=(STRIPE_KEY, ""), params=params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Stripe {path}: {r.status_code} {r.text[:200]}")
    return r.json()


def main():
    if not all([SUPABASE_URL, SERVICE_KEY, STRIPE_KEY, PRICE_PRO, PRICE_SCALE]):
        print("missing env", file=sys.stderr)
        sys.exit(1)
    subs = []
    starting_after = None
    while True:
        params = {"status": "active", "limit": "100", "expand[]": "data.customer"}
        if starting_after:
            params["starting_after"] = starting_after
        page = stripe_get("subscriptions", params)
        subs.extend(page["data"])
        if not page.get("has_more"):
            break
        starting_after = page["data"][-1]["id"]
    page = stripe_get("subscriptions", {"status": "trialing", "limit": "100",
                                        "expand[]": "data.customer"})
    subs.extend(page["data"])
    email_to_plan = {}
    for s in subs:
        price_id = (s["items"]["data"][0]["price"]["id"]
                    if s["items"]["data"] else None)
        plan = PRICE_TO_PLAN.get(price_id)
        cust = s.get("customer") or {}
        email = cust.get("email") if isinstance(cust, dict) else None
        if plan and email:
            if email_to_plan.get(email) != "scale":
                email_to_plan[email] = plan
    print(f"{len(subs)} subscriptions, {len(email_to_plan)} paying emails")
    customers = sb("customers", params={"select": "id,email,plan"})
    updated = 0
    for c in customers:
        want = email_to_plan.get(c["email"], "scout")
        if want != c["plan"]:
            sb(f"customers?id=eq.{c['id']}", "PATCH", json_body={"plan": want})
            updated += 1
            print(f"{c['email']}: {c['plan']} -> {want}")
    print(f"done: {updated} plans updated")


if __name__ == "__main__":
    main()
