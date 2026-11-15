#!/usr/bin/env python3
"""
Airwallex sandbox probe 2 - make deposits readable and attributable.

Q3 came back UNCLEAR: deposits were created (HTTP 201) but GET /api/v1/deposits
returned no rows. The docs say each deposit object carries global_account_id,
payer.name and reference, so this is almost certainly a request problem.

This script:
  A. Re-reads /api/v1/deposits several ways (x-api-version variants, explicit
     date window, paging) until rows come back.
  B. Makes one fresh deposit that also tries to set `reference` - the remittance
     string a reconciliation agent reads - and checks whether it survives.
  C. Prints exactly which request shape worked, so we can hardcode it.

Usage:
    export AWX_CLIENT_ID="..."
    export AWX_API_KEY="..."
    python3 awx_probe2.py
"""

import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.sandbox.airwallex.com"
CLIENT_ID = os.environ.get("AWX_CLIENT_ID", "").strip()
API_KEY = os.environ.get("AWX_API_KEY", "").strip()

# Dated versions seen in Airwallex's published docs, newest first.
API_VERSIONS = ["2024-08-07", "2024-04-30", "2023-10-01", "2022-09-09"]


def call(method, path, token=None, body=None, extra_headers=None):
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
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, json.dumps({"_error": str(e)})


def jcall(*args, **kwargs):
    status, raw = call(*args, **kwargs)
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def rows_of(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("items", "data", "deposits", "results"):
            if isinstance(payload.get(k), list):
                return payload[k]
    return []


def login():
    status, payload = jcall(
        "POST", "/api/v1/authentication/login",
        extra_headers={"x-client-id": CLIENT_ID, "x-api-key": API_KEY},
    )
    # Airwallex returns 201 Created here, not 200.
    if status not in (200, 201) or not isinstance(payload, dict) or not payload.get("token"):
        print(f"Login failed [HTTP {status}]:")
        print(json.dumps(payload, indent=2) if isinstance(payload, dict) else payload)
        sys.exit(1)
    print(f"Logged in (HTTP {status}).")
    return payload["token"]


def first_global_account(token):
    status, payload = jcall("GET", "/api/v1/global_accounts", token=token)
    accounts = rows_of(payload) or (payload if isinstance(payload, list) else [])
    if not accounts:
        print(f"No global accounts found [HTTP {status}]. Run probe 1 first.")
        sys.exit(1)
    acct = accounts[0]
    acct_id = acct.get("id") or acct.get("global_account_id")
    print(f"Using global account {acct_id}  ({len(accounts)} total)")
    return acct_id, [a.get("id") or a.get("global_account_id") for a in accounts]


def try_reads(token, label):
    """Try the deposits list several ways. Return the first shape that has rows."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S%z")
    until = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
    window = f"?from_created_at={since}&to_created_at={until}&page_num=0&page_size=100"

    attempts = [("plain, no version", "/api/v1/deposits", None)]
    for v in API_VERSIONS:
        attempts.append((f"x-api-version {v}", "/api/v1/deposits", v))
    attempts.append(("date window, no version", "/api/v1/deposits" + window, None))
    for v in API_VERSIONS[:2]:
        attempts.append((f"date window + x-api-version {v}", "/api/v1/deposits" + window, v))
    attempts.append(("financial_transactions", "/api/v1/financial_transactions", None))

    print(f"\n==== {label} ====")
    winner = None
    for name, path, version in attempts:
        hdrs = {"x-api-version": version} if version else None
        status, payload = jcall("GET", path, token=token, extra_headers=hdrs)
        rows = rows_of(payload)
        print(f"  [{status}] {name:<34} rows={len(rows)}")
        if status != 200 and isinstance(payload, dict) and payload.get("code"):
            print(f"         -> {payload.get('code')}: {payload.get('message')}")
        if rows and winner is None:
            winner = (name, path, version, rows)
    return winner


def show_rows(rows, known_accounts):
    print("\n  Deposit rows:")
    for r in rows[:8]:
        if not isinstance(r, dict):
            print(f"    {r}")
            continue
        payer = r.get("payer") or {}
        payer_name = payer.get("name") if isinstance(payer, dict) else payer
        print(f"    id={r.get('id')}")
        print(f"      amount={r.get('amount')} {r.get('currency')}  status={r.get('status')}")
        print(f"      global_account_id={r.get('global_account_id')}")
        print(f"      payer.name={payer_name!r}  reference={r.get('reference')!r}")
    attributed = {r.get("global_account_id") for r in rows
                  if isinstance(r, dict) and r.get("global_account_id")}
    print(f"\n  Distinct global_account_id values seen: {len(attributed)}")
    overlap = attributed & set(known_accounts)
    print(f"  Of those, matching accounts we know about: {len(overlap)}")
    return len(attributed)


def main():
    if not CLIENT_ID or not API_KEY:
        print("Set AWX_CLIENT_ID and AWX_API_KEY first.")
        sys.exit(1)

    token = login()
    acct_id, all_accounts = first_global_account(token)

    # ---- A. can we read the deposits already made?
    winner = try_reads(token, "A. Reading deposits made earlier")
    if winner:
        name, path, version, rows = winner
        print(f"\n  WORKS: {name}")
        show_rows(rows, all_accounts)
    else:
        print("\n  No shape returned rows yet. Making a fresh one and retrying.")

    # ---- B. fresh deposit, and does `reference` survive?
    print("\n==== B. Fresh deposit, with a remittance reference ====")
    body = {
        "request_id": str(uuid.uuid4()),
        "global_account_id": acct_id,
        "amount": "2310.00",
        "payer_name": "CAFE BRULE SARL",
        "reference": "BRULE/INV1",          # may be ignored; we find out below
    }
    status, payload = jcall("POST", "/api/v1/simulation/deposit/create", token=token, body=body)
    print(f"  create -> HTTP {status}")
    print(json.dumps(payload, indent=2)[:1200] if isinstance(payload, dict) else payload)

    if status not in (200, 201):
        print("\n  Retrying without `reference` to see if that field is the problem.")
        body.pop("reference")
        body["request_id"] = str(uuid.uuid4())
        status, payload = jcall("POST", "/api/v1/simulation/deposit/create", token=token, body=body)
        print(f"  create (no reference) -> HTTP {status}")
        print(json.dumps(payload, indent=2)[:800] if isinstance(payload, dict) else payload)

    # ---- C. read back, with a short wait for indexing
    for wait in (0, 5, 15):
        if wait:
            print(f"\n  waiting {wait}s for indexing...")
            time.sleep(wait)
        winner = try_reads(token, f"C. Re-read after {wait}s")
        if winner:
            name, path, version, rows = winner
            print(f"\n  WORKS: {name}")
            distinct = show_rows(rows, all_accounts)
            print("\n" + "=" * 68)
            print("VERDICT")
            print("=" * 68)
            print(f"  Use this request shape: {name}")
            print(f"  Path: {path}")
            print(f"  Header: x-api-version={version}" if version else "  Header: none")
            print(f"  Deposits carry global_account_id: "
                  f"{'YES' if distinct else 'NO - see rows above'}")
            return

    print("\n" + "=" * 68)
    print("VERDICT")
    print("=" * 68)
    print("  Deposits create fine but never appear in any list shape tried.")
    print("  Fall back to: track attribution in your own ledger at the moment")
    print("  you simulate the deposit, and use GET /balances/current to prove")
    print("  the money arrived. The demo still works; you just can't read the")
    print("  arrival back out of Airwallex.")


if __name__ == "__main__":
    main()
