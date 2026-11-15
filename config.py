#!/usr/bin/env python3
"""
Named policy constants - never a literal buried inside matching logic.
Both tolerances here are code's own policy; the model is never told a
dollar figure and never asked about either of these (docs/judge-audit.md
B3) - it only ever sees whether an invoice is open, never what tolerance
would close it.
"""

from decimal import Decimal

# The real (agent) pipeline's flat tolerance for in-transit fee variance -
# used by spine.resolve_single(). The sandbox's own observed SWIFT fee is
# a flat $12.85; $50 gives headroom without being generous enough to
# swallow an actual dispute.
FEE_TOLERANCE = Decimal("50.00")

# --rules-only counterfactual's tolerance band (docs/judge-audit.md A4,
# rules_only.py). An ordinary reconciliation policy - 2% of the invoice,
# or the same $50 flat floor the real pipeline uses, whichever is larger.
# Not tuned to produce a particular outcome: it is what a conventional
# auto-match rule looks like with no judgment layer behind it, and the
# README states that plainly rather than letting the number speak for
# itself.
RULES_ONLY_TOLERANCE_PERCENT = Decimal("0.02")


def rules_only_tolerance(invoice_amount):
    return max(invoice_amount * RULES_ONLY_TOLERANCE_PERCENT, FEE_TOLERANCE)
