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
    "Café Brûlé": {"country_code": "US", "currency": "USD"},
    "Northgate": {"country_code": "US", "currency": "USD"},
}

# Café Brûlé is in Paris and holds euros; Northgate is in London and holds
# pounds. Both are invoiced in USD - their own banks convert and send
# dollars, and correspondent banks deduct on the way, which is the whole
# reason nothing lands at the billed amount. Display-only: never read by
# any matching function. See deposit_card_info()'s sent_as field.
CUSTOMER_HOME_CURRENCY = {
    "Café Brûlé": "EUR",
    "Northgate": "GBP",
}

# Amounts are Decimal, built from strings - never a float literal
# (docs/judge-audit.md B1). Money is never binary floating point here.
#
# The hard case (INV-104/106/108/112, D1-D4 below) is untouched - same
# ids, same amounts, same positions in these lists. The 220/221/222/230/205
# block is volume added around it so most matches are boring and the one
# escalation stands out. Deliberately numbered in the 2xx range: D2's trap
# works by matching any OPEN Café Brûlé invoice whose number starts with
# "1" (104, 106, 108 all do) - a new invoice starting with "1" would
# silently join that candidate set and corrupt the hard case. 2xx can't.
INVOICES = [
    {"id": "INV-104", "customer": "Café Brûlé", "amount": Decimal("5000.00")},
    {"id": "INV-106", "customer": "Café Brûlé", "amount": Decimal("1800.00")},
    {"id": "INV-108", "customer": "Café Brûlé", "amount": Decimal("2350.00")},
    {"id": "INV-112", "customer": "Northgate", "amount": Decimal("3200.00")},
    {"id": "INV-220", "customer": "Café Brûlé", "amount": Decimal("4200.00")},
    {"id": "INV-221", "customer": "Café Brûlé", "amount": Decimal("990.00")},
    {"id": "INV-222", "customer": "Café Brûlé", "amount": Decimal("1510.00")},
    {"id": "INV-230", "customer": "Café Brûlé", "amount": Decimal("3400.00")},
    {"id": "INV-205", "customer": "Northgate", "amount": Decimal("2750.00")},
]

DEPOSITS_TO_LAND = [
    # --- the hard case: untouched, same order, same content ---
    {"customer": "Northgate", "amount": "3187.15", "payer_name": "NORTHGATE COFFEE",
     "reference": "NORTHGATE/INV112"},
    {"customer": "Café Brûlé", "amount": "2310.00", "payer_name": "CAFÉ BRÛLÉ",
     "reference": "BRULE/INV1"},
    {"customer": "Café Brûlé", "amount": "1950.00", "payer_name": "CAFÉ BRÛLÉ",
     "reference": "INV106"},
    # --- boring volume: each a few dollars off (crossed a border, got
    # converted, correspondent banks took their cut) but inside tolerance -
    # close instantly in code regardless. An exact match would be the
    # implausible one once money has actually crossed a border. ---
    {"customer": "Café Brûlé", "amount": "4193.60", "payer_name": "CAFÉ BRÛLÉ",
     "reference": "BRULE/INV220"},
    {"customer": "Café Brûlé", "amount": "3381.25", "payer_name": "CAFÉ BRÛLÉ",
     "reference": "BRULE/INV230"},
    {"customer": "Northgate", "amount": "2740.80", "payer_name": "NORTHGATE COFFEE",
     "reference": "NORTHGATE/INV205"},
    # --- the one lumped payment: exact-sum match, resolved in code, no model ---
    {"customer": "Café Brûlé", "amount": "2500.00", "payer_name": "CAFÉ BRÛLÉ",
     "reference": "BRULE/INV221+INV222"},
]

# Mid-run: a fourth deposit lands after Run 1 settles. Untouched - same
# amount, same reference, same customer as the original hard case.
D4 = {"customer": "Café Brûlé", "amount": "2690.00", "payer_name": "CAFÉ BRÛLÉ",
      "reference": "BRULE/INV104 FINAL"}

# Shared between run_demo.py and rules_only.py so the two modes can never
# drift on which reference means which step.
DEPOSIT_LABELS = {
    "NORTHGATE/INV112": "D1",
    "BRULE/INV1": "D2",
    "INV106": "D3",
    "BRULE/INV104 FINAL": "D4",
    "BRULE/INV220": "D5",
    "BRULE/INV230": "D6",
    "NORTHGATE/INV205": "D7",
    "BRULE/INV221+INV222": "D8",
}



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


def match_references(reference):
    """Every invoice-number digit group found in a reference, not just
    the first - used for a lumped payment that explicitly names more than
    one invoice."""
    return re.findall(r"INV-?(\d+)", reference or "", re.IGNORECASE)


def match_lumped_payment(invoices, customer, reference, amount):
    """A single deposit whose reference names exactly two open invoices
    for this customer by their full number, and whose amount sums exactly
    (within fee tolerance) to those two invoices. Matched by full number
    equality, not the prefix-startswith rule candidates_for uses - the
    reference states both invoice numbers outright, so there is nothing
    ambiguous to weigh. A fixed rule, like resolve_single: shared by both
    run_demo.py and rules_only.py, since whether a model exists has no
    bearing on an exact-sum arithmetic check. Returns the two matched
    invoices, or None if the reference/amount don't cleanly qualify."""
    nums = match_references(reference)
    distinct = list(dict.fromkeys(nums))  # de-dupe, keep first-seen order
    if len(distinct) != 2:
        return None

    matched = []
    for num in distinct:
        found = [
            inv for inv in invoices
            if inv["customer"] == customer and inv["status"] == "OPEN"
            and inv["id"].replace("INV-", "") == num
        ]
        if len(found) != 1:
            return None
        matched.append(found[0])

    total = matched[0]["amount"] + matched[1]["amount"]
    if abs(round(amount - total, 2)) <= FEE_TOLERANCE:
        return matched
    return None


def resolve_single(inv, amount):
    """The fixed-rule resolution for a deposit matched to exactly one
    candidate invoice. Mutates inv in place. Returns (action, message) -
    action is one of 'auto_close', 'auto_close_refund', 'outside_tolerance'.
    Pure code, no model involved - this is a rule, not a judgment."""
    inv["closing_deposit_amount"] = amount  # the real evidence, not a guess later
    diff = round(amount - inv["amount"], 2)

    if abs(diff) <= FEE_TOLERANCE:
        inv["status"] = "CLOSED"
        # Honest gap labelling: say what's actually known (the gap and that
        # it's inside tolerance), never what caused it. Code cannot tell a
        # correspondent fee from FX drift from a genuine short payment -
        # that inability is the project's own thesis, so a confident
        # breakdown here would contradict it. No word "exactly" either:
        # even a zero-diff match is just "no gap", not a claim about why.
        if diff < 0:
            inv["note"] = f"${abs(diff):.2f} short - correspondent fees and FX drift, inside tolerance"
        elif diff > 0:
            inv["note"] = f"${diff:.2f} over - inside tolerance, no refund needed"
        else:
            inv["note"] = "auto-closed, no gap"
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


def candidate_arithmetic(candidates, amount):
    """Every reading of a deposit against its candidate invoices that
    arithmetic alone can produce - no email, no model. Two shapes:

    singles: for every candidate, the deposit-minus-invoice diff, so a
    caller can spot an overpayment that's too large to be ordinary fee/FX
    noise (same FEE_TOLERANCE used everywhere else - no new threshold).

    splits: every pair where one candidate's full amount fits inside the
    deposit with a genuine (not tolerance-sized) remainder left over, and
    that remainder is itself a valid partial (less than its full amount)
    toward a different open candidate. This is how a single deposit can
    plausibly be "invoice A in full, plus a partial start on invoice B" -
    the two-invoice reading docs/judge-audit.md's D2 escalation was
    silently not naming.

    A caller still needs the email to decide which candidate is ruled out
    entirely (as D2's email rules out INV-108) - any single or split
    reading touching that candidate stops being live once the email does
    that. This function only enumerates what the numbers alone allow.
    """
    singles = [{"invoice": c, "diff": round(amount - c["amount"], 2)} for c in candidates]

    splits = []
    for full in candidates:
        remainder = round(amount - full["amount"], 2)
        if remainder <= FEE_TOLERANCE:
            continue  # doesn't even cover `full`, or is itself the tidy single-invoice fit
        for partial in candidates:
            if partial is full:
                continue
            if 0 < remainder < partial["amount"]:
                splits.append({"full": full, "partial": partial, "remainder": remainder})

    return singles, splits


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
