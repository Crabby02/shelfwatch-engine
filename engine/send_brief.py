#!/usr/bin/env python3
"""Shelfwatch weekly brief sender: builds each customer's Friday brief from
the last 8 days of snapshots and emails it via Resend. Runs on GitHub
Actions (Friday cron).

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, RESEND_API_KEY
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "")
FROM = "Shelfwatch <onboarding@resend.dev>"
WINDOW_DAYS = 8


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


def money(value, currency):
    if value is None:
        return "n/a"
    symbols = {"USD": "$", "GBP": "GBP ", "EUR": "EUR ", "INR": "Rs "}
    s = symbols.get(currency, (currency or "") + " ")
    try:
        return f"{s}{float(value):,.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "n/a"


def pct(a, b):
    try:
        return round((float(b) - float(a)) / float(a) * 100, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0


def render_email(customer_email, week_label, sections, summary):
    def sec(title, items):
        lis = "".join(f"<li style='margin:6px 0;'>{i}</li>" for i in items)
        return (f"<h3 style='font-size:14px;text-transform:uppercase;letter-spacing:1px;"
                f"color:#1e6f4e;margin:24px 0 8px;'>{title}</h3>"
                f"<ul style='padding-left:20px;margin:0;color:#1b1712;'>{lis}</ul>")
    body = "".join(sec(t, items) for t, items in sections)
    if not sections:
        body = ("<p style='color:#1b1712;'>No moves this week. "
                "Your competitors sat still.</p>")
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f3efe7;">
<div style="max-width:600px;margin:0 auto;background:#faf8f4;padding:32px;font-family:Georgia,serif;">
<p style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#1e6f4e;margin:0 0 4px;">Shelfwatch &middot; Weekly brief</p>
<h1 style="font-size:26px;color:#1b1712;margin:0 0 4px;">Your competitors, this week.</h1>
<p style="color:#6f665a;font-size:14px;margin:0 0 16px;">{week_label} &middot; {summary}</p>
<hr style="border:none;border-top:1px solid #e4ddd0;margin:16px 0;">
{body}
<hr style="border:none;border-top:1px solid #e4ddd0;margin:24px 0 16px;">
<p style="color:#6f665a;font-size:12px;">You are getting this because you joined Shelfwatch early access.
Manage emails anytime in your <a href="https://shelfwatch-livid.vercel.app/app.html" style="color:#1e6f4e;">dashboard</a>.</p>
</div></body></html>"""


def send(to, subject, html):
    r = requests.post("https://api.resend.com/emails",
                      headers={"Authorization": f"Bearer {RESEND_KEY}",
                               "Content-Type": "application/json"},
                      json={"from": FROM, "to": [to],
                            "subject": subject, "html": html},
                      timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Resend: {r.status_code} {r.text[:200]}")
    return r.json().get("id")


def main():
    if not (SUPABASE_URL and SERVICE_KEY and RESEND_KEY):
        print("SUPABASE_URL, SUPABASE_SERVICE_KEY, RESEND_API_KEY required",
              file=sys.stderr)
        sys.exit(1)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    customers = sb("customers", params={"select": "id,email"})
    print(f"{len(customers)} customers")
    sent = 0
    for c in customers:
        existing = sb("briefs", params={
            "select": "id", "customer_id": f"eq.{c['id']}",
            "week_start": f"eq.{week_start.isoformat()}"})
        if existing:
            print(f"skip {c['email']}: already sent")
            continue
        products = sb("products", params={
            "select": "id,url,name", "customer_id": f"eq.{c['id']}"})
        if not products:
            print(f"skip {c['email']}: no products")
            continue
        pids = ",".join(p["id"] for p in products)
        snaps = sb("snapshots", params={
            "select": "product_id,checked_at,price,currency,availability,name,raw",
            "product_id": f"in.({pids})",
            "checked_at": f"gte.{since}",
            "order": "checked_at.asc",
            "limit": "5000",
        })
        by_product = {}
        for s in snaps:
            by_product.setdefault(s["product_id"], []).append(s)
        price_moves, new_arrivals, stock = [], [], []
        for p in products:
            hist = by_product.get(p["id"], [])
            if not hist:
                continue
            first, last = hist[0], hist[-1]
            label = last.get("name") or p.get("name") or p["url"]
            cur = last.get("currency")
            if len(hist) == 1 or first["checked_at"] == last["checked_at"]:
                if first.get("price") is not None:
                    new_arrivals.append(f"{label} &mdash; {money(first['price'], cur)}")
                continue
            if last.get("price") != first.get("price"):
                d = pct(first.get("price"), last.get("price"))
                price_moves.append(
                    f"{label}: {money(first.get('price'), cur)} &rarr; "
                    f"<b>{money(last.get('price'), cur)}</b> ({d:+}%)")
            if last.get("availability") != first.get("availability"):
                stock.append(f"{label}: {first.get('availability') or '?'} &rarr; "
                             f"<b>{last.get('availability') or '?'}</b>")
        sections = []
        if price_moves:
            sections.append(("Price moves", price_moves))
        if new_arrivals:
            sections.append(("New arrivals", new_arrivals))
        if stock:
            sections.append(("Stock changes", stock))
        summary = (f"{len(price_moves)} price moves, {len(new_arrivals)} new, "
                   f"{len(stock)} stock changes")
        week_label = f"Week of {week_start.isoformat()}"
        html = render_email(c["email"], week_label, sections, summary)
        subject = f"Shelfwatch brief: {len(price_moves)} price moves this week"
        try:
            msg_id = send(c["email"], subject, html)
            sb("briefs", "POST", json_body={
                "customer_id": c["id"],
                "week_start": week_start.isoformat(),
                "subject": subject,
                "html": html,
                "summary": {"price_moves": len(price_moves),
                            "new_arrivals": len(new_arrivals),
                            "stock_changes": len(stock)},
                "sent_at": datetime.now(timezone.utc).isoformat(),
            })
            sent += 1
            print(f"sent {c['email']} ({msg_id})")
        except Exception as e:
            print(f"FAILED {c['email']}: {e}")
    print(f"done: {sent} briefs sent")


if __name__ == "__main__":
    main()
