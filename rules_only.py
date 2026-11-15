#!/usr/bin/env python3
"""
--rules-only counterfactual (docs/judge-audit.md A4).

Runs the IDENTICAL invoices, deposits, and email as run_demo.py - same
customers, same amounts, same references, same D2 and D4 figures, nothing
about the hard case touched - but with the judgment layer removed. No
agent.propose_match call exists anywhere in this file. An ambiguous
deposit is instead resolved by one ordinary rule: close the single
candidate invoice whose amount falls within a conventional tolerance band
(config.rules_only_tolerance - 2% of the invoice, or the same $50 flat
floor the real pipeline uses, whichever is larger) of the deposit amount.

With that tolerance, D2's $2,310 is 1.7% off INV-108's $2,350 - inside the
band. This mode closes INV-108, writing off an invoice the customer is
actively disputing. That is the point of running this at all: it is what
the judgment layer actually prevents, demonstrated rather than argued.
2% is an ordinary reconciliation tolerance, not inflated to force this -
see README's note on it.

Uses its own Global Accounts (state.rules_only.json), separate from
run_demo.py's, so the two counterfactuals never share deposit history.
Writes rules_only_state.json as its own snapshot sequence, and - if
demo_state.json already exists from a run_demo.py run - regenerates
demo_view.html with a toggle between the two.

Usage:
    export AWX_CLIENT_ID="..."
    export AWX_API_KEY="..."
    python3 rules_only.py
    # or: python3 run_demo.py --rules-only
"""

import json
import os
import time
from datetime import datetime, timezone

import config
import spine
from awx_client import AwxClient

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.rules_only.json")
OUT_PATH = os.path.join(os.path.dirname(__file__), "rules_only_state.json")
DEMO_STATE_PATH = os.path.join(os.path.dirname(__file__), "demo_state.json")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "view_template.html")
HTML_OUT_PATH = os.path.join(os.path.dirname(__file__), "demo_view.html")

D4 = {"customer": "Cafe Brule", "amount": "2690.00", "payer_name": "CAFE BRULE",
      "reference": "BRULE/INV104 FINAL"}

DEPOSIT_LABELS = {
    "NORTHGATE/INV112": "D1",
    "BRULE/INV1": "D2",
    "INV106": "D3",
    "BRULE/INV104 FINAL": "D4",
}

STEP_DESCRIPTIONS = {
    "D1": "Northgate deposit lands, $12.85 short - within tolerance band, auto-closed.",
    "D2": "Café Brûlé deposit lands with a truncated reference - tolerance band closes INV-108.",
    "D3": "Café Brûlé deposit overpays INV-106 by $150 - auto-closed, refund flagged.",
    "D4": "A fourth deposit arrives - no candidate within tolerance, no model to ask. Unresolved.",
}


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def provision_accounts(client):
    """Separate accounts from run_demo.py's (state.json) - the two
    counterfactuals never share deposit history, so neither can leak into
    the other's ledger or discovery window."""
    state = load_state()
    if state:
        live_ids = {a.get("id") for a in client.list_global_accounts()}
        if all(acct["id"] in live_ids for acct in state["accounts"].values()):
            print("Reusing rules-only accounts from state.rules_only.json:")
            for customer, acct in state["accounts"].items():
                print(f"    {customer:<12} {acct['id']}  ({acct['currency']})")
            return state["accounts"]
        print("state.rules_only.json accounts no longer exist - provisioning fresh ones.")

    accounts = {}
    for customer, spec in spine.DEMO_ACCOUNTS.items():
        payload = client.create_global_account(spec["country_code"], spec["currency"])
        accounts[customer] = {"id": payload["id"], "currency": spec["currency"]}
        print(f"  provisioned {customer:<12} {payload['id']}  ({spec['currency']})")
    save_state({"accounts": accounts})
    return accounts


def deposit_label_for(reference):
    return DEPOSIT_LABELS.get(reference, "?")


def resolve_rules_only(candidates, amount):
    """The one rule this mode has, in place of both spine.resolve_single
    and the model.

    Any overpayment on a SINGLE remaining candidate still auto-closes and
    flags a refund, regardless of tolerance width - that's not a judgment
    call, it's a code rule that would exist with or without a model
    (closing on overpayment never destroys information about which
    invoice was meant).

    Otherwise: close the one candidate whose amount falls within the
    conventional tolerance band of the deposit. If zero or more than one
    candidate qualifies, there is nothing here to resolve it with and no
    model to ask - it stays open.
    """
    if len(candidates) == 1:
        inv = candidates[0]
        diff = round(amount - inv["amount"], 2)
        if diff > 0:
            inv["status"] = "CLOSED"
            inv["refund_due"] = diff
            inv["closing_deposit_amount"] = amount
            inv["note"] = f"auto-closed, overpaid ${diff:.2f} - refund pending"
            return inv, f"[AUTO-CLOSE + REFUND FLAGGED] {inv['id']} - overpaid ${diff:.2f}"

    within = []
    for inv in candidates:
        diff = round(amount - inv["amount"], 2)
        tol = config.rules_only_tolerance(inv["amount"])
        if abs(diff) <= tol:
            within.append((inv, diff, tol))

    if len(within) == 1:
        inv, diff, tol = within[0]
        inv["status"] = "CLOSED"
        inv["closing_deposit_amount"] = amount
        inv["note"] = f"auto-closed by tolerance band (${abs(diff):.2f} off, band ${tol:.2f})"
        return inv, (f"[AUTO-CLOSE] {inv['id']} - ${abs(diff):.2f} off, within the "
                      f"${tol:.2f} tolerance band")

    names = ", ".join(i["id"] for i in candidates)
    if not within:
        return None, (f"[UNRESOLVED] no candidate within tolerance among ({names}) - "
                       f"no model available, flagging for a human")
    matched = ", ".join(i["id"] for i, _, _ in within)
    return None, (f"[UNRESOLVED] {len(within)} candidates within tolerance ({matched}) - "
                   f"no model available, flagging for a human")


def process_deposit(dep, accounts, invoices):
    customer = spine.account_customer(accounts, dep.get("global_account_id"))
    amount = dep.get("amount")
    reference = dep.get("reference") or ""
    digits = spine.match_reference(reference)

    if not digits:
        print(f"  [SKIP] {customer} ${amount:.2f} ref={reference!r} - no invoice number in reference")
        return
    candidates = spine.candidates_for(invoices, customer, digits)
    if not candidates:
        print(f"  [SKIP] {customer} ${amount:.2f} ref={reference!r} - no open invoice matches")
        return

    _, note = resolve_rules_only(candidates, amount)
    print(f"  {note} ({customer})")


def poll_and_process(client, accounts, invoices, run_start, seen_ids, snapshots,
                      known_order=None, seen_references=None):
    """Same discovery approach as run_demo.poll_and_process, including the
    two ordering fixes found while testing that file: known_order breaks
    the sandbox's 1-second created_at ties using our own submission order,
    and seen_references guards against a prior run's deposit re-entering
    scope through the run_start cutoff's clock-skew buffer."""
    known_order = known_order or {}
    seen_references = seen_references if seen_references is not None else set()
    deposits = spine.read_demo_deposits(client, accounts, run_start)
    new_deposits = [d for d in deposits if d.get("id") not in seen_ids]
    new_deposits.sort(key=lambda d: (
        known_order.get(d.get("id"), float("inf")),
        d.get("created_at") or d.get("create_time") or "",
        d.get("id") or "",
    ))

    processed = []
    for dep in new_deposits:
        seen_ids.add(dep.get("id"))
        reference = dep.get("reference")
        if reference in seen_references:
            print(f"\n--- [SKIP] reference {reference!r} already processed this run "
                  f"(deposit {dep.get('id')}) - likely a prior run's deposit ---")
            continue
        seen_references.add(reference)
        processed.append(dep)
        label = deposit_label_for(reference)
        print(f"\n--- {label} (discovered via GET /deposits) ---")
        process_deposit(dep, accounts, invoices)
        spine.print_ledger(invoices)
        snapshots.append(snapshot(label, invoices))

    return processed


def snapshot(step, invoices):
    return {
        "step": step,
        "description": STEP_DESCRIPTIONS.get(step, ""),
        "ledger": [
            {"id": inv["id"], "customer": inv["customer"], "amount": inv["amount"],
             "status": inv["status"], "note": inv.get("note", "")}
            for inv in invoices
        ],
        "escalations": [],  # this mode has no escalation concept - nothing to show
        "refund": None,  # matching only - no transfer is executed in this mode
    }


def write_outputs(snapshots):
    with open(OUT_PATH, "w") as f:
        json.dump({"snapshots": snapshots}, f, indent=2, default=str)
    print(f"\nWrote {OUT_PATH}")

    if not os.path.exists(DEMO_STATE_PATH):
        print(f"{DEMO_STATE_PATH} doesn't exist yet - run run_demo.py first "
              f"to get the toggle-enabled demo_view.html. Skipping HTML regeneration.")
        return

    with open(DEMO_STATE_PATH) as f:
        agent_snapshots = json.load(f)["snapshots"]

    with open(TEMPLATE_PATH) as f:
        template = f.read()
    html = template.replace(
        "/*__SNAPSHOTS_AGENT_JSON__*/[]", json.dumps(agent_snapshots, default=str)
    ).replace(
        "/*__SNAPSHOTS_RULES_ONLY_JSON__*/[]", json.dumps(snapshots, default=str)
    )
    with open(HTML_OUT_PATH, "w") as f:
        f.write(html)
    print(f"Wrote {HTML_OUT_PATH} with both modes (agent judgment / rules-only toggle)")


def main():
    client = AwxClient()
    client.login()
    print("Authenticated.")
    print("\n*** --rules-only: the model is disabled. No agent.propose_match call ***")
    print("*** exists anywhere in this file - see the module docstring.         ***\n")

    print("Provisioning rules-only accounts...")
    accounts = provision_accounts(client)
    invoices = spine.seed_ledger()  # IDENTICAL fixtures to run_demo.py
    snapshots = []

    print("\n" + "=" * 70)
    print("RUN 1  D1-D3 land")
    print("=" * 70)
    run_start = datetime.now(timezone.utc)
    seen_ids = set()
    seen_references = set()
    landed_ids = spine.land_deposits(client, accounts)  # IDENTICAL deposits to run_demo.py
    known_order = {dep_id: i for i, dep_id in enumerate(landed_ids)}

    print("\nWaiting for deposits to settle, then polling GET /deposits...")
    time.sleep(5)
    poll_and_process(client, accounts, invoices, run_start, seen_ids, snapshots,
                      known_order=known_order, seen_references=seen_references)

    print("\n" + "=" * 70)
    print("MID-RUN  a fourth deposit lands")
    print("=" * 70)
    acct_id = accounts[D4["customer"]]["id"]
    payload = client.simulate_deposit(acct_id, D4["amount"], D4["payer_name"], D4["reference"])
    print(f"  landed: {D4['customer']} ${D4['amount']} ref={D4['reference']!r} -> id={payload.get('id')}")
    known_order[payload.get("id")] = len(known_order)

    print("\nWaiting for it to settle, then polling GET /deposits again...")
    time.sleep(5)
    poll_and_process(client, accounts, invoices, run_start, seen_ids, snapshots,
                      known_order=known_order, seen_references=seen_references)

    spine.print_ledger(invoices)
    write_outputs(snapshots)

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    inv108 = next(i for i in invoices if i["id"] == "INV-108")
    if inv108["status"] == "CLOSED":
        print(f"  INV-108 is CLOSED - {inv108['note']}")
        print("  That invoice is actively disputed per the customer's email. Nothing in")
        print("  this file ever read that email - there is no judgment layer here to.")
    else:
        print(f"  INV-108 is {inv108['status']} - did not reproduce the expected failure "
              f"mode this run; investigate before using this as evidence.")


if __name__ == "__main__":
    main()
