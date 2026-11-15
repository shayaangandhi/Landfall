#!/usr/bin/env python3
"""
Airwallex sandbox probe.

Answers three questions, then prints a verdict:

  Q1  Can I create more than one Global Account in the SAME currency?
      (needed for the per-customer account idea)
  Q2  Can I create Global Accounts in different countries/currencies?
      (the fallback: one account per customer country)
  Q3  Do simulated deposits land attributable to a specific account?

Nothing here moves real money. Sandbox only.

Usage:
    export AWX_CLIENT_ID="your-sandbox-client-id"
    export AWX_API_KEY="your-sandbox-api-key"
    python3 awx_probe.py
"""

import json
import os
import sys
import uuid
import urllib.error
import urllib.request

BASE = "https://api.sandbox.airwallex.com"

CLIENT_ID = os.environ.get("AWX_CLIENT_ID", "").strip()
API_KEY = os.environ.get("AWX_API_KEY", "").strip()


# ---------------------------------------------------------------- plumbing

def call(method, path, token=None, body=None, extra_headers=None):
    """Return (status, parsed_json_or_raw_text). Never raises on HTTP errors."""
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if extra_headers:
        headers.update(extra_headers)

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        status = e.code
    except Exception as e:                      # network / DNS / TLS
        return 0, {"_error": str(e)}

    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def show(label, status, payload):
    print(f"\n--- {label}  [HTTP {status}]")
    text = json.dumps(payload, indent=2) if isinstance(payload, (dict, list)) else str(payload)
    if len(text) > 1800:
        text = text[:1800] + "\n   ... (truncated)"
    print(text)


def currency_of(account):
    """Global Account shapes vary; dig out a currency string."""
    if not isinstance(account, dict):
        return None
    if isinstance(account.get("currency"), str):
        return account["currency"]
    feats = account.get("required_features") or account.get("capabilities") or []
    if isinstance(feats, list):
        for f in feats:
            if isinstance(f, dict) and isinstance(f.get("currency"), str):
                return f["currency"]
    return None


def id_of(account):
    if not isinstance(account, dict):
        return None
    for key in ("id", "global_account_id", "account_id"):
        if isinstance(account.get(key), str):
            return account[key]
    return None


def as_list(payload):
    """List endpoints return either a bare list or {items: [...]}."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "global_accounts", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


# ---------------------------------------------------------------- steps

def login():
    print("=" * 70)
    print("STEP 1  Authenticate")
    print("=" * 70)
    status, payload = call(
        "POST", "/api/v1/authentication/login",
        extra_headers={"x-client-id": CLIENT_ID, "x-api-key": API_KEY},
    )
    if status not in (200, 201) or not isinstance(payload, dict) or not payload.get("token"):
        show("login failed", status, payload)
        print("\nSTOP. Check the Client ID and API key, and that the sandbox")
        print("business is approved (it must be named 'Sandbox Business').")
        sys.exit(1)
    print("  OK - got a bearer token (valid 30 minutes)")
    return payload["token"]


def list_accounts(token, label):
    status, payload = call("GET", "/api/v1/global_accounts", token=token)
    accounts = as_list(payload)
    print(f"\n{label}: {len(accounts)} Global Account(s)")
    for a in accounts:
        print(f"    {id_of(a)}   currency={currency_of(a)}   status={a.get('status')}")
    if not accounts and status != 200:
        show("list returned non-200", status, payload)
    return accounts, status, payload


def create_account(token, country_code, currency, transfer_method="LOCAL"):
    body = {
        "request_id": str(uuid.uuid4()),
        "country_code": country_code,
        "required_features": [{"currency": currency, "transfer_method": transfer_method}],
    }
    status, payload = call("POST", "/api/v1/global_accounts/create", token=token, body=body)
    ok = status in (200, 201) and id_of(payload) is not None
    tag = "OK " if ok else "FAIL"
    print(f"  [{tag}] create {currency} in {country_code} ({transfer_method}) -> HTTP {status}", end="")
    if ok:
        print(f"  id={id_of(payload)}")
    else:
        print()
        show(f"create {currency}/{country_code} response", status, payload)
    return (id_of(payload) if ok else None), status, payload


def deposit(token, account_id, amount, payer_name):
    body = {
        "request_id": str(uuid.uuid4()),
        "global_account_id": account_id,
        "amount": amount,
        "payer_name": payer_name,
    }
    status, payload = call("POST", "/api/v1/simulation/deposit/create", token=token, body=body)
    ok = status in (200, 201)
    print(f"  [{'OK ' if ok else 'FAIL'}] deposit {amount} from '{payer_name}' "
          f"into {account_id} -> HTTP {status}")
    if not ok:
        show("deposit response", status, payload)
    return ok, payload


def read_deposits(token):
    for path in ("/api/v1/deposits", "/api/v1/financial_transactions"):
        status, payload = call("GET", path, token=token)
        if status == 200:
            return path, as_list(payload), payload
    return None, [], None


# ---------------------------------------------------------------- main

def main():
    if not CLIENT_ID or not API_KEY:
        print("Set AWX_CLIENT_ID and AWX_API_KEY first. See the header of this file.")
        sys.exit(1)

    token = login()

    print("\n" + "=" * 70)
    print("STEP 2  What already exists")
    print("=" * 70)
    existing, _, raw = list_accounts(token, "Before")
    if existing:
        show("full shape of the first account (for reference)", 200, existing[0])

    usd_before = [a for a in existing if currency_of(a) == "USD"]

    print("\n" + "=" * 70)
    print("STEP 3  Q1 - two Global Accounts in the SAME currency (USD)?")
    print("=" * 70)
    made_usd = []
    for i in range(2):
        acct_id, _, _ = create_account(token, "US", "USD", "LOCAL")
        if acct_id:
            made_usd.append(acct_id)

    after_usd, _, _ = list_accounts(token, "After USD attempts")
    distinct_usd = {id_of(a) for a in after_usd if currency_of(a) == "USD" and id_of(a)}
    q1 = len(distinct_usd) >= 2
    print(f"\n  Q1 RESULT: {len(distinct_usd)} distinct USD account(s) -> "
          f"{'YES, same-currency duplicates allowed' if q1 else 'NO'}")

    print("\n" + "=" * 70)
    print("STEP 4  Q2 - accounts in other countries / currencies?")
    print("=" * 70)
    others = {}
    for country, currency, method in (("GB", "GBP", "LOCAL"), ("NL", "EUR", "LOCAL")):
        acct_id, _, _ = create_account(token, country, currency, method)
        if acct_id:
            others[currency] = acct_id
    q2 = len(others) >= 1
    print(f"\n  Q2 RESULT: created {sorted(others)} -> {'YES' if q2 else 'NO'}")

    print("\n" + "=" * 70)
    print("STEP 5  Q3 - are deposits attributable per account?")
    print("=" * 70)
    targets = sorted(distinct_usd)[:2] or ([id_of(a) for a in after_usd if id_of(a)][:1])
    marks = [("4961.15", "CAFE BRULE"), ("2310.00", "NORTHGATE COFFEE")]
    landed = 0
    for acct_id, (amount, payer) in zip(targets, marks):
        ok, _ = deposit(token, acct_id, amount, payer)
        landed += 1 if ok else 0

    path, rows, raw_rows = read_deposits(token)
    q3 = False
    if path:
        print(f"\n  Read back from {path}: {len(rows)} row(s)")
        for r in rows[:6]:
            if isinstance(r, dict):
                keys = {k: r.get(k) for k in
                        ("amount", "currency", "payer_name", "global_account_id",
                         "account_id", "status") if k in r}
                print(f"    {keys}")
        attributed = {r.get("global_account_id") for r in rows
                      if isinstance(r, dict) and r.get("global_account_id")}
        q3 = len(attributed) >= 2 or (len(targets) == 1 and len(attributed) == 1)
        if not attributed and rows:
            print("    (no global_account_id field on these rows - see shape above)")
            show("first row, full shape", 200, rows[0])
    else:
        print("  Could not read deposits back from /deposits or /financial_transactions.")

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    print(f"  Q1 several accounts, same currency : {'YES' if q1 else 'NO'}")
    print(f"  Q2 accounts in other currencies    : {'YES' if q2 else 'NO'}")
    print(f"  Q3 deposits attributable per acct  : {'YES' if q3 else 'UNCLEAR - check rows above'}")
    print()
    if q1:
        print("  -> STRONG version is on: one receiving account per customer,")
        print("     even when two customers share a currency.")
    elif q2:
        print("  -> FALLBACK version: one account per customer COUNTRY.")
        print("     Works as long as the demo's customers are in different countries.")
    else:
        print("  -> Drop the per-customer account angle. Build straight reconciliation")
        print("     against the single funded account.")
    print()


if __name__ == "__main__":
    main()
