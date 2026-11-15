#!/usr/bin/env python3
"""
Spine - Reconciliation agent, Airwallex Agentic Banking Hackathon.

Build order step 1: provision per-customer accounts, seed the ledger, land
D1-D3, run the fixed-rule matcher, print terminal output. No model yet - D2
is deliberately left unresolved here; the agent layer (step 2) judges it.

Usage:
    export AWX_CLIENT_ID="..."
    export AWX_API_KEY="..."
    python3 spine.py
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from awx_client import AwxClient
from config import FEE_TOLERANCE  # noqa: F401 - re-exported as spine.FEE_TOLERANCE

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

DEMO_ACCOUNTS = {
    "Cafe Brule": {"country_code": "US", "currency": "USD"},
    "Northgate": {"country_code": "US", "currency": "USD"},
}

# Amounts are Decimal, built from strings - never a float literal
# (docs/judge-audit.md B1). Money is never binary floating point here.
INVOICES = [
    {"id": "INV-104", "customer": "Cafe Brule", "amount": Decimal("5000.00")},
    {"id": "INV-106", "customer": "Cafe Brule", "amount": Decimal("1800.00")},
    {"id": "INV-108", "customer": "Cafe Brule", "amount": Decimal("2350.00")},
    {"id": "INV-112", "customer": "Northgate", "amount": Decimal("3200.00")},
]

DEPOSITS_TO_LAND = [
    {"customer": "Northgate", "amount": "3187.15", "payer_name": "NORTHGATE COFFEE",
     "reference": "NORTHGATE/INV112"},
    {"customer": "Cafe Brule", "amount": "2310.00", "payer_name": "CAFE BRULE",
     "reference": "BRULE/INV1"},
    {"customer": "Cafe Brule", "amount": "1950.00", "payer_name": "CAFE BRULE",
     "reference": "INV106"},
]



# ---------------------------------------------------------------- state

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def provision_accounts(client):
    """Reuse accounts from state.json if they still exist; otherwise create
    fresh USD accounts and persist their IDs. Always look accounts up by
    stored ID, never by position in a list - the sandbox already has five
    accounts from probing, and list order isn't stable."""
    state = load_state()
    if state:
        live_ids = {a.get("id") for a in client.list_global_accounts()}
        if all(acct["id"] in live_ids for acct in state["accounts"].values()):
            print("Reusing accounts from state.json:")
            for customer, acct in state["accounts"].items():
                print(f"    {customer:<12} {acct['id']}  ({acct['currency']})")
            return state["accounts"]
        print("state.json accounts no longer exist in the sandbox - provisioning fresh ones.")

    accounts = {}
    for customer, spec in DEMO_ACCOUNTS.items():
        payload = client.create_global_account(spec["country_code"], spec["currency"])
        accounts[customer] = {"id": payload["id"], "currency": spec["currency"]}
        print(f"  provisioned {customer:<12} {payload['id']}  ({spec['currency']})")

    save_state({"accounts": accounts})
    return accounts


# ---------------------------------------------------------------- ledger

def seed_ledger():
    return [dict(inv, status="OPEN", refund_due=Decimal("0.00"), note="") for inv in INVOICES]


# ---------------------------------------------------------------- deposits

def land_deposits(client, accounts):
    """Returns the deposit ids in the exact order we submitted them. The
    sandbox's created_at has only 1-second resolution and these calls fire
    back-to-back, so two deposits landed in the same batch routinely tie on
    timestamp (observed: BRULE/INV1 and INV106 both at the same second) -
    the caller needs real submission order, not a coarse clock, to process
    them in the order they actually arrived."""
    print("\nLanding deposits...")
    landed_ids = []
    for d in DEPOSITS_TO_LAND:
        acct_id = accounts[d["customer"]]["id"]
        payload = client.simulate_deposit(acct_id, d["amount"], d["payer_name"], d["reference"])
        print(f"    {d['customer']:<12} ${d['amount']:<10} ref={d['reference']!r:<22} -> "
              f"id={payload.get('id')}  status={payload.get('status')}")
        landed_ids.append(payload.get("id"))
    return landed_ids


def _parse_timestamp(value):
    """Rows have been seen with both '+0000' and '+00:00' offsets - try
    both rather than trust one shape."""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def read_demo_deposits(client, accounts, run_start):
    """Filter to the two demo accounts and this run only - /deposits returns
    everything on the account, including every probe's test deposits.
    run_start is a tz-aware datetime; a small buffer absorbs clock skew
    between when we captured it and when the API stamped the deposit."""
    demo_ids = {a["id"] for a in accounts.values()}
    cutoff = run_start - timedelta(seconds=10)
    rows = client.list_deposits()
    kept = []
    for r in rows:
        if r.get("global_account_id") not in demo_ids:
            continue
        if r.get("status") not in ("PENDING", "SETTLED"):
            continue
        created = _parse_timestamp(r.get("created_at") or r.get("create_time") or "")
        if created and created < cutoff:
            continue
        kept.append(r)
    return kept


def account_customer(accounts, global_account_id):
    for customer, acct in accounts.items():
        if acct["id"] == global_account_id:
            return customer
    return None


# ---------------------------------------------------------------- matching

def match_reference(reference):
    """Pull the invoice-number digits out of a remittance reference, or
    None if nothing invoice-shaped is in it."""
    m = re.search(r"INV-?(\d+)", reference or "", re.IGNORECASE)
    return m.group(1) if m else None


def candidates_for(invoices, customer, digits):
    return [
        inv for inv in invoices
        if inv["customer"] == customer
        and inv["status"] == "OPEN"
        and inv["id"].replace("INV-", "").startswith(digits)
    ]


def resolve_single(inv, amount):
    """The fixed-rule resolution for a deposit matched to exactly one
    candidate invoice. Mutates inv in place. Returns (action, message) -
    action is one of 'auto_close', 'auto_close_refund', 'outside_tolerance'.
    Pure code, no model involved - this is a rule, not a judgment."""
    inv["closing_deposit_amount"] = amount  # the real evidence, not a guess later
    diff = round(amount - inv["amount"], 2)

    if abs(diff) <= FEE_TOLERANCE:
        inv["status"] = "CLOSED"
        inv["note"] = (f"auto-closed, ${abs(diff):.2f} fee variance" if diff
                        else "auto-closed, exact")
        return "auto_close", (
            f"[AUTO-CLOSE] {inv['id']} - deposit ${amount:.2f} vs invoice "
            f"${inv['amount']:.2f}, diff ${diff:+.2f} within tolerance"
        )
    elif diff > 0:
        inv["status"] = "CLOSED"
        inv["refund_due"] = diff
        inv["note"] = f"overpaid by ${diff:.2f} - refund pending (not yet executed)"
        return "auto_close_refund", (
            f"[AUTO-CLOSE + REFUND FLAGGED] {inv['id']} - overpaid by ${diff:.2f}"
        )
    else:
        return "outside_tolerance", (
            f"[ESCALATE] {inv['id']} - deposit ${amount:.2f} is ${abs(diff):.2f} "
            f"short of invoice ${inv['amount']:.2f}, outside tolerance - needs agent"
        )


def apply_matching(invoices, deposits, accounts):
    print("\nMatching (fixed rules only - ambiguous cases wait for the agent layer):")
    for dep in deposits:
        customer = account_customer(accounts, dep.get("global_account_id"))
        amount = dep.get("amount")  # already Decimal - parse_float=Decimal in awx_client
        reference = dep.get("reference") or ""
        digits = match_reference(reference)

        if not digits:
            print(f"  [SKIP] {customer} ${amount:.2f} ref={reference!r} - "
                  f"no invoice number in reference, needs agent")
            continue

        matches = candidates_for(invoices, customer, digits)
        if len(matches) != 1:
            names = ", ".join(m["id"] for m in matches) or "none"
            print(f"  [ESCALATE] {customer} ${amount:.2f} ref={reference!r} -> "
                  f"{len(matches)} candidate invoice(s) ({names}) - ambiguous, needs agent judgment")
            continue

        _, message = resolve_single(matches[0], amount)
        print(f"  {message} ({customer})")


def print_ledger(invoices):
    print("\n" + "=" * 70)
    print("LEDGER STATE")
    print("=" * 70)
    for inv in invoices:
        print(f"  {inv['id']:<9} {inv['customer']:<12} ${inv['amount']:>8,.2f}  "
              f"{inv['status']:<7} {inv['note']}")


# ---------------------------------------------------------------- main

def main():
    client = AwxClient()
    client.login()
    print("Authenticated.")

    print("\nProvisioning per-customer accounts...")
    accounts = provision_accounts(client)

    invoices = seed_ledger()

    run_start = datetime.now(timezone.utc)
    land_deposits(client, accounts)

    print("\nWaiting for deposits to settle, then reading them back "
          "(filtered to demo accounts + this run)...")
    time.sleep(5)
    deposits = read_demo_deposits(client, accounts, run_start)
    print(f"  found {len(deposits)} deposit(s) in scope")

    apply_matching(invoices, deposits, accounts)
    print_ledger(invoices)


if __name__ == "__main__":
    main()
