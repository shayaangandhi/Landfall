#!/usr/bin/env python3
"""
Run demo - Reconciliation agent, Airwallex Agentic Banking Hackathon.

Runs the full demo arc end to end and writes two artifacts for the video:
  - demo_state.json  - an ordered list of snapshots, one after each deposit
    is processed (D1, D2, D3, D4) and one after the refund executes. Each
    snapshot carries the full invoice ledger and any open escalation card.
  - demo_view.html   - a single self-contained page (same template as
    view_template.html, with this run's snapshots embedded) that steps
    through them with prev/next, for filming.

Build order:
  - D2 is ambiguous by fixed rule alone -> the model is asked to judge it.
    The model is advisory only: it proposes, code validates the proposal
    against the candidates it was actually offered, and code is the only
    thing that can close an invoice or move money.
  - D3 closes INV-106, which narrows D2's candidates - the open escalation
    is re-evaluated immediately, not snapshotted from Run 1.
  - D4 lands mid-run. Its reference alone disambiguates to INV-104 (a fixed
    rule), and $2,310 + $2,690 = $5,000 exactly - also arithmetic, not
    judgment. Code closes INV-104 and withdraws the earlier escalation.
  - The $150 overpayment on INV-106 is then refunded, gated behind a
    console approval, with a duplicate-refund attempt proven rejected.

Usage:
    export AWX_CLIENT_ID="..."
    export AWX_API_KEY="..."
    export ANTHROPIC_API_KEY="..."
    python3 run_demo.py
"""

import json
import os
import time
from datetime import datetime, timezone

import agent
import refund as refund_mod
import spine
from awx_client import AwxClient

EMAIL_TEXT = (
    "Hi — we've queried INV-108 with your sales team, there's a line on it we don't "
    "recognise, so we're holding that one for now. Paying INV-106 in full and "
    "starting on INV-104 this week."
)

# D4 and DEPOSIT_LABELS live in spine.py now - shared with rules_only.py
# so the two modes can never drift on which reference means which step.

STEP_DESCRIPTIONS = {
    "D1": "Northgate deposit lands, $12.85 short - fee variance, auto-closed in code.",
    "D2": "Café Brûlé deposit lands with a truncated reference - ambiguous, escalated.",
    "D3": "Café Brûlé deposit overpays INV-106 by $150 - auto-closed, refund flagged.",
    "D4": "A fourth deposit confirms D2 was a partial payment - INV-104 closes, escalation withdrawn.",
    "D5": "A boring deposit lands - exact match, closes INV-220 instantly in code.",
    "D6": "Another boring deposit - exact match, closes INV-230 instantly in code.",
    "D7": "A boring Northgate deposit - exact match, closes INV-205 instantly in code.",
    "D8": "A single lumped payment lands - exact-sum match closes INV-221 and INV-222 together, in code, no model.",
    "REFUND": "The $150 overpayment on INV-106 is refunded.",
}

# determinism_check.py found live calls stable across runs at the time of
# writing - set True only if that stops being true, and say so on camera.
USE_CACHE = False

STATE_JSON_PATH = os.path.join(os.path.dirname(__file__), "demo_state.json")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "view_template.html")
HTML_OUT_PATH = os.path.join(os.path.dirname(__file__), "demo_view.html")


def payer_name_of(dep):
    payer = dep.get("payer")
    if isinstance(payer, dict):
        return payer.get("name", "")
    return dep.get("payer_name", "")


def to_model_deposit(dep):
    return {
        "amount": dep.get("amount"),  # already Decimal - parse_float=Decimal in awx_client
        "currency": dep.get("currency", "USD"),
        "payer_name": payer_name_of(dep),
        "reference": dep.get("reference"),
    }


def deposit_label_for(reference):
    return spine.DEPOSIT_LABELS.get(reference, "?")


def ledger_facts_for(invoices, customer, prior_candidate_ids, current_candidate_ids):
    """Facts about ledger state the model is never asked about and never
    asserts - code states them because code knows them for certain. Covers
    any invoice that dropped out of the candidate set because it closed."""
    dropped = set(prior_candidate_ids) - set(current_candidate_ids)
    facts = []
    for inv in invoices:
        if inv["customer"] != customer or inv["id"] not in dropped:
            continue
        if inv["status"] == "CLOSED" and inv.get("closed_by_label"):
            facts.append(f"{inv['id']} closed by deposit {inv['closed_by_label']} earlier in this run.")
    return facts


def new_escalation(dep, customer, candidates):
    return {
        "deposit_id": dep.get("id"),
        "label": deposit_label_for(dep.get("reference")),
        "amount": dep.get("amount"),  # already Decimal - parse_float=Decimal in awx_client
        "currency": dep.get("currency", "USD"),
        "counterparty": customer,
        "reference": dep.get("reference"),
        "candidates_considered": [c["id"] for c in candidates],
        "email_relied_on": EMAIL_TEXT,
        "model_recommendation": None,
        "card_fields": None,
        "code_validation": None,
        "status": "OPEN",
        "revisions": 0,
        "ledger_facts": [],
    }


def ask_agent_and_record(dep, customer, candidates):
    proposal, from_cache = agent.propose_match(
        to_model_deposit(dep), candidates, EMAIL_TEXT, use_cache=USE_CACHE
    )
    accepted, reason = agent.validate_proposal(proposal, candidates)
    record = new_escalation(dep, customer, candidates)
    record["code_validation"] = {"accepted": accepted, "reason": reason}

    if from_cache:
        print("  [CACHED RESPONSE - recorded run]")

    if not accepted:
        print(f"\n  [ESCALATE] code REFUSED the model's proposal: {reason}")
        print("  falling back to a plain escalation with no accepted recommendation.")
        return record

    record["model_recommendation"] = proposal
    record["card_fields"] = agent.escalation_card_fields(
        record["label"], record["amount"], record["currency"], customer, proposal,
    )
    print()
    print(agent.render_escalation_card(record["card_fields"]))
    return record


def reevaluate_single_escalation(rec, invoices):
    """Requirement: narrow candidates as the run progresses, don't
    snapshot. Called right after a deposit closes an invoice - if that
    changes an open escalation's candidate set, re-ask the model."""
    if rec["status"] != "OPEN":
        return

    digits = spine.match_reference(rec["reference"])
    current = spine.candidates_for(invoices, rec["counterparty"], digits)
    current_ids = [c["id"] for c in current]
    if sorted(current_ids) == sorted(rec["candidates_considered"]):
        return

    facts = ledger_facts_for(invoices, rec["counterparty"], rec["candidates_considered"], current_ids)
    print(f"\n  [RE-EVALUATE] narrowed from {rec['candidates_considered']} to "
          f"{current_ids} - {len(facts)} ledger fact(s) now certain")

    if len(current) == 1:
        action, message = spine.resolve_single(current[0], rec["amount"])
        current[0]["closed_by_label"] = rec["label"]
        print("    down to one candidate - this is a fixed rule now, not a judgment.")
        print(f"    {message}")
        rec["status"] = "RESOLVED_BY_ELIMINATION"
        rec["resolution"] = {"invoice_id": current[0]["id"], "action": action}
        rec["candidates_considered"] = current_ids
        rec["card_fields"] = None
        return

    proposal, from_cache = agent.propose_match(
        {"amount": rec["amount"], "currency": rec["currency"], "payer_name": "",
         "reference": rec["reference"]},
        current, EMAIL_TEXT, use_cache=USE_CACHE,
    )
    accepted, reason = agent.validate_proposal(proposal, current)
    if from_cache:
        print("    [CACHED RESPONSE - recorded run]")
    print("    re-asked the model with the narrowed set")

    rec["candidates_considered"] = current_ids
    rec["code_validation"] = {"accepted": accepted, "reason": reason}
    rec["ledger_facts"] = facts
    rec["revisions"] += 1

    if accepted:
        rec["model_recommendation"] = proposal
        rec["card_fields"] = agent.escalation_card_fields(
            rec["label"], rec["amount"], rec["currency"], rec["counterparty"],
            proposal, ledger_facts=facts,
        )
        print()
        print(agent.render_escalation_card(rec["card_fields"]))
    else:
        rec["model_recommendation"] = None
        rec["card_fields"] = None
        print(f"    code REFUSED the revised proposal: {reason}")


def try_combined_close(inv, amount, customer, label, escalations, dep_id):
    """A deposit came in short of an invoice beyond fee tolerance on its
    own - check whether it plus an existing open escalation's deposit
    reconciles the invoice exactly. Pure arithmetic: if the agent would
    make the same call every time, it's a rule, not a judgment
    (docs/judge-audit.md A4 - this is the one path where that rule reaches
    the same invoice the model's card was advising on, without consulting
    the model again. tests.py:test_a4 shows this is scoped to the
    reconciling case, not a universal bypass - a non-reconciling second
    deposit leaves the escalation OPEN, dependent on the model's card)."""
    for rec in escalations.values():
        if rec["status"] != "OPEN" or rec["counterparty"] != customer:
            continue
        if inv["id"] not in rec["candidates_considered"]:
            continue
        combined = round(rec["amount"] + amount, 2)
        combined_diff = round(combined - inv["amount"], 2)
        if abs(combined_diff) <= spine.FEE_TOLERANCE:
            inv["status"] = "CLOSED"
            inv["closing_deposit_amount"] = amount
            inv["note"] = (f"closed via {rec['label']}+{label}: "
                            f"${rec['amount']:.2f} + ${amount:.2f} = ${combined:.2f}")
            inv["closed_by_label"] = f"{rec['label']}+{label}"
            rec["status"] = "WITHDRAWN"
            rec["card_fields"] = None
            rec["resolution"] = {
                "confirmed_by_deposit_id": dep_id,
                "combined_amount": combined,
                "note": f"${rec['amount']:.2f} + ${amount:.2f} = ${combined:.2f} "
                        f"matches {inv['id']} (${inv['amount']:.2f}) within tolerance",
            }
            outcome = (f"Confirms {rec['label']}'s partial payment - closes {inv['id']} "
                       f"(${rec['amount']:.2f} + ${amount:.2f} = ${combined:.2f}), withdraws escalation")
            print(f"  [WITHDRAW ESCALATION + AUTO-CLOSE] {inv['id']} ({customer}) - "
                  f"{label} confirms {rec['label']} was a partial payment on {inv['id']}.")
            print(f"    ${rec['amount']:.2f} ({rec['label']}) + ${amount:.2f} ({label}) = "
                  f"${combined:.2f} ≈ {inv['id']} (${inv['amount']:.2f}). "
                  f"Recalculated in code, not re-judged.")
            print(f"    escalation for deposit {rec['deposit_id']} is now WITHDRAWN.")
            return True, outcome
    return False, None


def process_deposit(dep, accounts, invoices, escalations):
    """The one path every deposit goes through - D1, D2, D3, D4, or any
    deposit this run discovers. Nothing here is specific to which deposit
    this is (docs/judge-audit.md A3). Returns a one-line outcome string
    for the deposit card (view_template.html)."""
    customer = spine.account_customer(accounts, dep.get("global_account_id"))
    amount = dep.get("amount")  # already Decimal - parse_float=Decimal in awx_client
    reference = dep.get("reference") or ""
    label = deposit_label_for(reference)

    lumped = spine.match_lumped_payment(invoices, customer, reference, amount)
    if lumped:
        names = " + ".join(i["id"] for i in lumped)
        total = sum(i["amount"] for i in lumped)
        for inv in lumped:
            other = next(i for i in lumped if i is not inv)
            inv["status"] = "CLOSED"
            inv["closing_deposit_amount"] = amount
            inv["closed_by_label"] = label
            inv["note"] = f"auto-closed, lumped payment with {other['id']} (combined ${total:.2f})"
        print(f"  [AUTO-CLOSE LUMPED] {names} - ${amount:.2f} covers both exactly ({customer})")
        for rec in escalations.values():
            reevaluate_single_escalation(rec, invoices)
        return f"Lumped payment - covers {names} exactly (${total:.2f}), closed in code"

    digits = spine.match_reference(reference)

    if not digits:
        outcome = "No invoice number in the reference - skipped"
        print(f"  [SKIP] {customer} ${amount:.2f} ref={reference!r} - no invoice number in reference")
        return outcome

    candidates = spine.candidates_for(invoices, customer, digits)

    if not candidates:
        outcome = f"No open invoice matches digits {digits!r} - skipped"
        print(f"  [SKIP] {customer} ${amount:.2f} ref={reference!r} - no open invoice matches digits {digits!r}")
        return outcome

    if len(candidates) > 1:
        escalations[dep.get("id")] = ask_agent_and_record(dep, customer, candidates)
        for rec in escalations.values():
            reevaluate_single_escalation(rec, invoices)
        rec = escalations[dep.get("id")]
        if rec.get("card_fields"):
            return f"Ambiguous - escalated: {rec['card_fields']['proposed']} (advisory, needs a person)"
        return "Ambiguous - escalated, no accepted proposal"

    inv = candidates[0]
    action, message = spine.resolve_single(inv, amount)

    combined_ok, combined_outcome = (
        try_combined_close(inv, amount, customer, label, escalations, dep.get("id"))
        if action == "outside_tolerance" else (False, None)
    )

    if combined_ok:
        outcome = combined_outcome
    else:
        if inv["status"] == "CLOSED":
            inv["closed_by_label"] = label
        outcome = message.split("] ", 1)[-1]  # drop the [TAG] prefix, keep the rest
        print(f"  {message} ({customer})")

    for rec in escalations.values():
        reevaluate_single_escalation(rec, invoices)

    return outcome


def poll_and_process(client, accounts, invoices, escalations, run_start, seen_ids, snapshots,
                      known_order=None, seen_references=None):
    """A3: discovery, not a script. Re-reads GET /api/v1/deposits and
    processes anything not already seen (by id), through process_deposit()
    - the SAME function regardless of which deposit this turns out to be.
    There is no 'now handle deposit N' step anywhere in this file.

    known_order is an optional {deposit_id: sequence_number} map for
    deposits WE simulated ourselves this run - the sandbox's created_at has
    only 1-second resolution, and deposits landed back-to-back (no delay
    between simulate_deposit calls) routinely tie on it. Observed directly:
    BRULE/INV1 and INV106 landed in the same second and sorted on their
    random id instead, discovering D3 before D2 and silently skipping the
    candidate-narrowing step. known_order breaks that tie with the one
    thing more reliable than the sandbox's clock for a deposit we created
    ourselves: the order we actually called simulate_deposit in. Anything
    not in known_order (a deposit we didn't initiate) falls back to
    created_at, same as before.

    seen_references guards a different, also-observed failure: run_start's
    10-second cutoff buffer (spine.read_demo_deposits) can let a PRIOR
    run's own deposit back into scope if the two runs are close enough in
    time - it then reads as a second, duplicate landing of the same
    reference this run. A deposit whose reference this run has already
    fully processed is treated as that artifact and skipped, not reprocessed."""
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
                  f"(deposit {dep.get('id')}) - likely a prior run's deposit caught by "
                  f"the scoping window's clock-skew buffer, not a genuine second landing ---")
            continue
        seen_references.add(reference)
        processed.append(dep)
        label = deposit_label_for(reference)
        print(f"\n--- {label} (discovered via GET /deposits) ---")
        outcome = process_deposit(dep, accounts, invoices, escalations)
        spine.print_ledger(invoices)
        dep_card = deposit_card_info(dep, accounts, reference, outcome)
        snapshots.append(snapshot(label, invoices, escalations, deposit=dep_card))

    return processed


def deposit_card_info(dep, accounts, reference, outcome):
    """The per-customer account is the project's actual novelty and
    previously appeared nowhere on screen - this is what the deposit card
    (view_template.html) renders at the top of each step."""
    customer = spine.account_customer(accounts, dep.get("global_account_id"))
    acct = accounts.get(customer, {})
    acct_id = acct.get("id", "")
    return {
        "amount": dep.get("amount"),
        "currency": dep.get("currency", "USD"),
        "payer": payer_name_of(dep),
        "reference": reference,
        "account_customer": customer,
        "account_id": acct_id,
        "account_id_last5": acct_id[-5:] if acct_id else "",
        "outcome": outcome,
    }


def invoices_needing_refund(invoices):
    """Find overpayments from the live ledger - never assume which invoice
    (docs/judge-audit.md A2). refund_due is set by spine.resolve_single()
    only when a deposit actually overpaid something, this run."""
    return [inv for inv in invoices if inv.get("refund_due") and inv["refund_due"] > 0
            and not inv.get("refund_executed")]


def execute_refund_step(client, invoice):
    """Returns a refund_result dict, or None if not approved. `invoice` is
    a live ledger entry, not a hardcoded constant."""
    beneficiary_id = refund_mod.get_or_create_beneficiary(client)
    record = refund_mod.approval_record(invoice)
    refund_mod.print_approval(record)

    approved_record = refund_mod.request_approval(record)
    if approved_record is None:
        print("\nRefund NOT approved. Exiting without moving money.")
        return None

    print("\nExecuting refund...")
    status, payload, request_id = refund_mod.execute_refund(client, beneficiary_id, approved_record)

    if status in (200, 201):
        transfer_id = payload["id"]
        print(f"  created transfer {transfer_id}  status={payload.get('status')}  request_id={request_id}")
        final = refund_mod.advance_to_terminal(client, transfer_id)
    elif status == 400 and payload.get("code") == "duplicate_request_id":
        transfer_id = payload["details"]["id"]
        print(f"  already executed in a prior run - reusing transfer {transfer_id}")
        final = client.get_transfer(transfer_id)
    else:
        raise RuntimeError(f"refund transfer create failed [HTTP {status}]: {payload}")

    print(f"\n  FINAL STATUS: {final.get('status')}")
    if final.get("status") == "CANCELLED":
        print(f"  failure_type: {final.get('failure_type')}  failure_reason: {final.get('failure_reason')}")

    print("\nDUPLICATE-REFUND PROOF - sending the identical refund request again")
    status2, payload2, _ = refund_mod.execute_refund(client, beneficiary_id, approved_record)
    duplicate_rejected = status2 == 400 and payload2.get("code") == "duplicate_request_id"
    print(f"  second attempt -> HTTP {status2}  rejected={duplicate_rejected}")

    invoice["refund_executed"] = True
    return {
        "invoice_id": approved_record["invoice_id"],
        "amount": approved_record["amount"],
        "currency": approved_record["currency"],
        "transfer_id": transfer_id,
        "status": final.get("status"),
        "duplicate_attempt": {"rejected": duplicate_rejected, "code": payload2.get("code")},
    }


def snapshot(step, invoices, escalations, refund_result=None, deposit=None):
    return {
        "step": step,
        "description": STEP_DESCRIPTIONS.get(step, ""),
        "deposit": deposit,
        "ledger": [
            {"id": inv["id"], "customer": inv["customer"], "amount": inv["amount"],
             "status": inv["status"], "note": inv.get("note", "")}
            for inv in invoices
        ],
        "escalations": [
            rec["card_fields"] for rec in escalations.values()
            if rec["status"] == "OPEN" and rec.get("card_fields")
        ],
        "refund": refund_result,
    }


def print_escalation_registry(escalations):
    if not escalations:
        return
    print("\n" + "=" * 70)
    print("ESCALATION REGISTRY")
    print("=" * 70)
    for dep_id, rec in escalations.items():
        print(f"  deposit {dep_id}  {rec['counterparty']}  ${rec['amount']:.2f}  "
              f"ref={rec['reference']!r}  status={rec['status']}  revisions={rec['revisions']}")
        if rec["status"] == "WITHDRAWN":
            print(f"    resolution: {rec['resolution']['note']}")


def write_outputs(snapshots):
    with open(STATE_JSON_PATH, "w") as f:
        # default=str: amounts are Decimal (B1) - serialize as plain numeric
        # strings. The HTML's Number(n) parses those back fine for display.
        json.dump({"snapshots": snapshots}, f, indent=2, default=str)

    with open(TEMPLATE_PATH) as f:
        template = f.read()
    html = template.replace("/*__SNAPSHOTS_AGENT_JSON__*/[]", json.dumps(snapshots, default=str))
    with open(HTML_OUT_PATH, "w") as f:
        f.write(html)

    print(f"\nWrote {STATE_JSON_PATH}")
    print(f"Wrote {HTML_OUT_PATH}  ({len(snapshots)} snapshot(s))")


def main():
    print("=" * 70)
    print("LANDFALL — Ravenswood Roasters reconciliation agent")
    print("=" * 70)

    client = AwxClient()
    client.login()
    print("Authenticated.")

    print("\nProvisioning per-customer accounts...")
    accounts = spine.provision_accounts(client)
    invoices = spine.seed_ledger()
    escalations = {}
    snapshots = []

    print("\n" + "=" * 70)
    print("RUN 1  D1-D3 land")
    print("=" * 70)
    run_start = datetime.now(timezone.utc)
    seen_ids = set()
    seen_references = set()
    landed_ids = spine.land_deposits(client, accounts)  # the stimulus - three deposits actually arriving
    known_order = {dep_id: i for i, dep_id in enumerate(landed_ids)}

    print("\nWaiting for deposits to settle, then polling GET /deposits...")
    time.sleep(5)
    found = poll_and_process(client, accounts, invoices, escalations, run_start, seen_ids,
                              snapshots, known_order=known_order, seen_references=seen_references)
    print(f"\n  discovered and processed {len(found)} deposit(s)")

    print("\n" + "=" * 70)
    print("MID-RUN  a fourth deposit lands")
    print("=" * 70)
    acct_id = accounts[spine.D4["customer"]]["id"]
    payload = client.simulate_deposit(acct_id, spine.D4["amount"], spine.D4["payer_name"], spine.D4["reference"])
    print(f"  landed: {spine.D4['customer']} ${spine.D4['amount']} ref={spine.D4['reference']!r} -> id={payload.get('id')}")
    known_order[payload.get("id")] = len(known_order)

    print("\nWaiting for it to settle, then polling GET /deposits again...")
    time.sleep(5)
    found = poll_and_process(client, accounts, invoices, escalations, run_start, seen_ids,
                              snapshots, known_order=known_order, seen_references=seen_references)
    if not found:
        print("  could not read the new deposit back yet - nothing new discovered this poll.")

    refund_candidates = invoices_needing_refund(invoices)
    refund_result = None
    if not refund_candidates:
        print("\nNo overpayments detected this run - nothing to refund.")
    else:
        for inv in refund_candidates:
            print("\n" + "=" * 70)
            print(f"REFUND  ${inv['refund_due']:.2f} overpayment on {inv['id']}")
            print("=" * 70)
            refund_result = execute_refund_step(client, inv)
            if refund_result:
                inv["note"] = (f"overpaid by ${refund_result['amount']:.2f} - refund EXECUTED "
                                f"(transfer {refund_result['transfer_id']}, {refund_result['status']})")
    spine.print_ledger(invoices)
    snapshots.append(snapshot("REFUND", invoices, escalations, refund_result))

    print_escalation_registry(escalations)
    write_outputs(snapshots)


if __name__ == "__main__":
    import sys

    if "--rules-only" in sys.argv:
        # The counterfactual lives in its own module (rules_only.py) rather
        # than branching this file's agent-calling functions - cleaner than
        # threading an if-rules-only flag through ask_agent_and_record,
        # reevaluate_single_escalation, and try_combined_close.
        import rules_only
        rules_only.main()
    else:
        main()
