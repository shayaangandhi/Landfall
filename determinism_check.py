#!/usr/bin/env python3
"""
Run the model's judgment on the fixed D2 scenario several times at
temperature=0 and compare. If proposed_invoice_id and the tidy-match call
are stable across runs, the live call is safe for the demo. If not, use
agent.py's cache (use_cache=True) for the recording and say so on camera.

Does not touch the Airwallex sandbox - this is purely about the model.
"""

import json
from decimal import Decimal

from agent import propose_match, validate_proposal

EMAIL = (
    "Hi — we've queried INV-108 with your sales team, there's a line on it we don't "
    "recognise, so we're holding that one for now. Paying INV-106 in full and "
    "starting on INV-104 this week."
)

DEPOSIT_D2 = {
    "amount": Decimal("2310.00"), "currency": "USD",
    "payer_name": "CAFÉ BRÛLÉ", "reference": "BRULE/INV1",
}

CANDIDATES_3 = [
    {"id": "INV-104", "amount": Decimal("5000.00"), "status": "OPEN"},
    {"id": "INV-106", "amount": Decimal("1800.00"), "status": "OPEN"},
    {"id": "INV-108", "amount": Decimal("2350.00"), "status": "OPEN"},
]

CANDIDATES_2 = [
    {"id": "INV-104", "amount": Decimal("5000.00"), "status": "OPEN"},
    {"id": "INV-108", "amount": Decimal("2350.00"), "status": "OPEN"},
]


def run_scenario(label, candidates, n):
    print(f"\n{'=' * 70}\n{label}  ({n} runs)\n{'=' * 70}")
    results = []
    for i in range(n):
        proposal, from_cache = propose_match(DEPOSIT_D2, candidates, EMAIL, use_cache=False)
        ok, reason = validate_proposal(proposal, candidates)
        results.append(proposal)
        print(f"\n--- run {i + 1} ---")
        print(f"  tidy_match_id        = {proposal.get('tidy_match_id')!r}")
        print(f"  proposed_invoice_id  = {proposal.get('proposed_invoice_id')!r}")
        print(f"  proposed_summary     = {proposal.get('proposed_summary')!r}")
        print(f"  payment_type         = {proposal.get('payment_type')!r}")
        print(f"  confidence           = {proposal.get('confidence')!r}")
        print(f"  code validation      = {'OK' if ok else 'REFUSED: ' + reason}")
        print(f"  tidy_match_rejection = {proposal.get('tidy_match_rejection')}")
        print(f"  supporting_quote     = {proposal.get('supporting_quote')}")
        print(f"  unproven_reason      = {proposal.get('unproven_reason')}")

    proposed_ids = {r.get("proposed_invoice_id") for r in results}
    tidy_ids = {r.get("tidy_match_id") for r in results}
    confidences = {r.get("confidence") for r in results}
    stable = len(proposed_ids) == 1 and len(tidy_ids) == 1
    print(f"\n  STABLE (id-level): {stable}  (proposed_invoice_id set={proposed_ids}, "
          f"tidy_match_id set={tidy_ids})")
    print(f"  confidence set: {confidences}  "
          f"({'stable' if len(confidences) == 1 else 'VARIES across runs'})")
    return stable


def main():
    s1 = run_scenario("3-candidate scenario (Run 1, before D3 closes INV-106)", CANDIDATES_3, 3)
    s2 = run_scenario("2-candidate scenario (after D3 closes INV-106)", CANDIDATES_2, 3)

    print(f"\n{'=' * 70}\nVERDICT\n{'=' * 70}")
    if s1 and s2:
        print("  Stable across all runs at temperature=0. Safe to call the model live "
              "for the demo recording.")
    else:
        print("  NOT stable across runs. Use use_cache=True in run_demo.py for the "
              "recorded take, and say on camera that this is a recorded run.")


if __name__ == "__main__":
    main()
