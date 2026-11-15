#!/usr/bin/env python3
"""
Audit tests - docs/judge-audit.md section A. Stdlib asserts, no pytest.
Hits the live Anthropic API for the A1 tests (that's the point - we're
testing the model's actual behavior, not a mock of it). Everything else is
pure in-memory logic, no network.

Usage:
    export ANTHROPIC_API_KEY="..."
    python3 tests.py
"""

import sys
from decimal import Decimal

import agent
import spine

# ---------------------------------------------------------------- fixtures

EMAIL_BASELINE = (
    "Hi — we've queried INV-108 with your sales team, there's a line on it we don't "
    "recognise, so we're holding that one for now. Paying INV-106 in full and "
    "starting on INV-104 this week."
)

# A1: the exact same email with ONLY the hold sentence removed. Nothing else
# changed - if the model's answer doesn't move, the "judgment" is decoration.
EMAIL_NO_HOLD = (
    "Hi — Paying INV-106 in full and starting on INV-104 this week."
)

DEPOSIT_D2 = {
    "amount": Decimal("2310.00"), "currency": "USD",
    "payer_name": "CAFE BRULE", "reference": "BRULE/INV1",
}

CANDIDATES_3 = [
    {"id": "INV-104", "amount": Decimal("5000.00"), "status": "OPEN"},
    {"id": "INV-106", "amount": Decimal("1800.00"), "status": "OPEN"},
    {"id": "INV-108", "amount": Decimal("2350.00"), "status": "OPEN"},
]


def _check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail else ""))
    return condition


# ---------------------------------------------------------------- A1

def test_a1_evidence_sensitivity():
    """docs/judge-audit.md A1. Change the evidence, change the decision."""
    print("\n=== A1: evidence sensitivity ===")
    all_ok = True

    baseline, _ = agent.propose_match(DEPOSIT_D2, CANDIDATES_3, EMAIL_BASELINE, use_cache=False)
    print(f"  baseline email  -> proposed_invoice_id={baseline.get('proposed_invoice_id')!r}  "
          f"tidy_match_id={baseline.get('tidy_match_id')!r}  "
          f"tidy_match_rejection={baseline.get('tidy_match_rejection')!r}")
    all_ok &= _check(
        "baseline proposes INV-104 (the documented demo answer)",
        baseline.get("proposed_invoice_id") == "INV-104",
    )

    flipped, _ = agent.propose_match(DEPOSIT_D2, CANDIDATES_3, EMAIL_NO_HOLD, use_cache=False)
    print(f"  hold sentence removed -> proposed_invoice_id={flipped.get('proposed_invoice_id')!r}  "
          f"tidy_match_id={flipped.get('tidy_match_id')!r}  "
          f"tidy_match_rejection={flipped.get('tidy_match_rejection')!r}")
    all_ok &= _check(
        "removing the hold sentence flips the answer to INV-108 "
        "(the tidy arithmetic match, no longer contradicted)",
        flipped.get("proposed_invoice_id") == "INV-108",
        f"got {flipped.get('proposed_invoice_id')!r} - "
        f"if this is still INV-104, the answer is baked in somewhere and the "
        f"model is decoration, per the audit's own wording",
    )

    return all_ok


# ---------------------------------------------------------------- C3

def test_c3_typed_tool_refuses_out_of_bounds():
    """docs/judge-audit.md C3. Out-of-bounds proposals are refused by code,
    not trusted. No network call - validate_proposal() is a pure function."""
    print("\n=== C3: typed tool refuses an invoice it wasn't offered ===")
    fake_proposal = {
        "proposed_invoice_id": "INV-999",
        "tidy_match_id": None,
        "payment_type": "partial",
        "confidence": "high",
    }
    accepted, reason = agent.validate_proposal(fake_proposal, CANDIDATES_3)
    ok = _check(
        "INV-999 (never offered as a candidate) is refused",
        accepted is False,
        reason or "",
    )

    valid_proposal = {
        "proposed_invoice_id": "INV-104",
        "tidy_match_id": "INV-108",
        "payment_type": "partial",
        "confidence": "medium",
    }
    accepted2, reason2 = agent.validate_proposal(valid_proposal, CANDIDATES_3)
    ok &= _check("a genuinely-offered candidate (INV-104) is accepted", accepted2 is True)
    return ok


# ---------------------------------------------------------------- A4

def test_a4_fallback_is_scoped_not_universal():
    """docs/judge-audit.md A4. The combined-sum close in
    run_demo.try_combined_close DOES reach the same answer as the model
    without consulting it, when a second deposit happens to reconcile the
    invoice exactly - that is a real FAIL against A4's literal test, not
    argued away here. What this test shows is narrower: that fallback is
    scoped to the reconciling case. Absent a deposit that reconciles, the
    escalation stays OPEN - there is no universal code bypass that would
    resolve every ambiguous case without the model."""
    print("\n=== A4: the code fallback is scoped, not universal (does not excuse the FAIL) ===")
    all_ok = True

    # Case 1: a second deposit DOES reconcile the invoice exactly (D4 in the
    # real demo). The combined-sum rule fires without the model's input.
    invoices = [dict(c, customer="Cafe Brule") for c in CANDIDATES_3]
    rec = {"amount": Decimal("2310.00"), "currency": "USD", "counterparty": "Cafe Brule",
           "candidates_considered": ["INV-104", "INV-108"], "status": "OPEN",
           "deposit_id": "dep-2", "label": "D2"}
    inv104 = next(i for i in invoices if i["id"] == "INV-104")
    inv104["status"] = "OPEN"

    second_deposit_amount = Decimal("2690.00")  # 2310 + 2690 = 5000.00 exactly
    combined = round(rec["amount"] + second_deposit_amount, 2)
    combined_diff = round(combined - inv104["amount"], 2)
    reconciles = abs(combined_diff) <= spine.FEE_TOLERANCE
    all_ok &= _check(
        "a reconciling second deposit closes the invoice via pure arithmetic "
        "alone (rec['model_recommendation'] never read) - confirms the A4 FAIL",
        reconciles,
        f"${rec['amount']:.2f} + ${second_deposit_amount:.2f} = ${combined:.2f} vs "
        f"invoice ${inv104['amount']:.2f}, diff ${combined_diff:+.2f}",
    )

    # Case 2: a second deposit that does NOT reconcile. Confirms the fallback
    # does not fire universally - no amount, no close, escalation stays OPEN.
    non_reconciling_amount = Decimal("100.00")  # 2310 + 100 = 2410, nowhere near 5000
    combined2 = round(rec["amount"] + non_reconciling_amount, 2)
    combined_diff2 = round(combined2 - inv104["amount"], 2)
    does_not_reconcile = abs(combined_diff2) > spine.FEE_TOLERANCE
    all_ok &= _check(
        "a non-reconciling second deposit does NOT close the invoice - "
        "the escalation would stay OPEN, dependent on the model's card",
        does_not_reconcile,
        f"${rec['amount']:.2f} + ${non_reconciling_amount:.2f} = ${combined2:.2f} vs "
        f"invoice ${inv104['amount']:.2f}, diff ${combined_diff2:+.2f}",
    )

    return all_ok


def main():
    results = {
        "A1": test_a1_evidence_sensitivity(),
        "C3": test_c3_typed_tool_refuses_out_of_bounds(),
        "A4": test_a4_fallback_is_scoped_not_universal(),
    }
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
