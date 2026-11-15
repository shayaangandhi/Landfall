# Reconciliation agent — Airwallex Agentic Banking Hackathon

A small Chicago coffee roaster, Ravenswood Roasters, sells wholesale to cafés
abroad. Money arrives that never matches the invoices: fees come out in
transit, customers part-pay, overpay, and lump invoices together, and wire
remittance text gets truncated so you can't tell what a payment was for.

Two halves to the problem:

1. **Infrastructure** — one Airwallex Global Account per customer, so an
   incoming payment is pre-attributed to *who* sent it.
2. **Judgment** — the account says *who*, never *which invoice*. That part
   needs reading a customer email and weighing what is likely. That is the
   agent (Claude, via the Anthropic API) — advisory only. It proposes a
   match; code validates the proposal and is the only thing that can close
   an invoice or move money.

Sandbox only. Synthetic data. No real money moves.

## Setup

```bash
pip3 install -r requirements.txt   # plain `pip` isn't guaranteed on PATH
```

Create `.env` in this directory:

```
AWX_CLIENT_ID=<your Airwallex sandbox client id>
AWX_API_KEY=<your Airwallex sandbox api key>
ANTHROPIC_API_KEY=<your Anthropic api key>
```

Your Airwallex sandbox business must be named `Sandbox Business` (or `New
Business Sandbox`) to be auto-approved — see `docs/builder-guide.md`.

## Running it

`set -a` must come *before* `source .env`, not after — if sourced first,
the variables become shell locals but are never exported, and
`run_demo.py` sees them as empty (which gets a bare 403 from Airwallex's
edge, not even a JSON error - worth knowing if you ever see that).

```bash
set -a && source .env && set +a   # or export each var yourself
python3 run_demo.py
```

This provisions two USD Global Accounts (Café Brûlé, Northgate), lands three
deposits, discovers and processes them by polling `GET /deposits`, lands a
fourth deposit mid-run, discovers and processes that too, then finds and
executes any overpayment refund it detects — prompting for a console
approval first. It writes two files:

- `demo_state.json` — an ordered list of snapshots, one per deposit
  discovered plus one after the refund, each with the full invoice ledger
  and any open escalation card.
- `demo_view.html` — a single self-contained page (open it directly in a
  browser, no server needed) that steps through those snapshots with
  prev/next buttons, for recording the demo video.

Other entry points:

- `spine.py` — the fixed-rule matcher alone, no model. Useful for seeing
  what code can resolve on its own before the judgment layer is added.
- `refund.py` — the refund path standalone, against a fixture invoice (not
  a live ledger result — see the module docstring).
- `determinism_check.py` — calls the model 6 times on the same ambiguous
  scenario and checks whether its answer is stable (Claude Sonnet 5 / Opus 5
  have no temperature parameter to pin — see `agent.py`).
- `tests.py` — the adversarial checks from `docs/judge-audit.md` section A:
  does the model's judgment actually change when the evidence changes
  (A1), does the typed tool refuse an invoice it wasn't offered (C3), and
  is the code-only arithmetic fallback scoped to the case it's meant for,
  not a universal bypass (A4).

```bash
python3 tests.py              # hits the live Anthropic API for A1
python3 determinism_check.py  # also live
python3 spine.py              # Airwallex only, no model
```

### The `--rules-only` counterfactual

```bash
python3 run_demo.py --rules-only   # or: python3 rules_only.py
```

Runs the identical invoices, deposits, and email as `run_demo.py` - same
dollar figures, same references, nothing about the hard case touched - but
with the judgment layer removed entirely. No call to Claude exists anywhere
in `rules_only.py`. In place of the model, an ambiguous deposit is resolved
by one ordinary rule: close the single candidate invoice whose amount falls
within a conventional tolerance band of the deposit (`config.py`'s
`rules_only_tolerance` - **2% of the invoice, or the same $50 flat floor the
real pipeline uses for fee variance, whichever is larger**).

2% is an ordinary reconciliation tolerance band, not inflated to produce a
particular answer - it's the kind of number a conventional auto-match rule
would actually ship with. Under it, the D2 deposit ($2,310) lands 1.7% off
INV-108's $2,350, which is inside the band. Rules-only closes INV-108 and
writes off an invoice the customer is actively disputing. Run it and see
for yourself; the verdict it prints names exactly that failure. It also
never recognizes that D2 + D4 ($2,310 + $2,690) is an exact, full payment
of INV-104 - that invoice is left open and apparently unpaid, since
spotting a split across two deposits using the email as a tiebreaker is
exactly what this file has no way to do.

Writes `rules_only_state.json` as its own snapshot sequence, separate from
`demo_state.json`, and - if `demo_state.json` already exists - regenerates
`demo_view.html` with a toggle between the two (run `run_demo.py` first,
then this, to see both). Uses its own Global Accounts
(`state.rules_only.json`) so the two runs never share deposit history.

## Architecture

- **Code owns**: tolerance thresholds, the fee table, matching arithmetic,
  duplicate protection (via Airwallex's own idempotent `request_id`), state
  transitions, every amount that reaches an API call. Money is `Decimal`
  throughout — never a `float`. Tolerance policy is centralized in
  `config.py` as named constants (`FEE_TOLERANCE`,
  `RULES_ONLY_TOLERANCE_PERCENT`) rather than literals scattered through
  matching logic.
- **The model owns**: reading a truncated remittance reference and the
  customer email, judging whether a gap is explainable, proposing a match
  when arithmetic is genuinely ambiguous, writing the short, fixed-field
  rationale a human reviewer reads. It is given a typed tool with a
  constrained schema and never sees account IDs, API keys, or anything
  beyond the deposit/candidate/email data needed to judge one case.
- Every escalation is bound to amount, currency, counterparty, the
  candidates actually offered, and the email relied on. A proposal naming
  an invoice it wasn't offered is refused by code, not trusted.
- All Airwallex sandbox calls live behind one interface (`awx_client.py`)
  so the decision logic never talks HTTP directly.

## Known limitations

- Deposits are simulated via Airwallex's sandbox simulation endpoint, not
  real bank transfers.
- The demo discovers new deposits by polling `GET /deposits` on a fixed
  schedule, not a webhook.
- Two customers, four invoices, one beneficiary bank account — not a
  production ledger. `refund.py`'s beneficiary bank details are a fixture
  for the one counterparty this demo ever refunds.
- `agent_cache.json` (a response cache for a recorded take, in case the
  model's live output ever proves unstable at camera time) is not currently
  in use — `determinism_check.py` found live calls stable.
- The code-only "combined deposits reconcile an invoice" rule (the D4 step)
  resolves an ambiguous case without re-consulting the model when a later
  deposit happens to confirm the earlier one exactly. `tests.py` shows this
  fallback only fires on an exact reconciliation, not universally — a
  non-reconciling second deposit leaves the escalation open, dependent on
  the model's card. See `docs/judge-audit.md` A4 for the full discussion;
  this is reported there as a literal FAIL against that check's test, with
  the scoping evidence as mitigation, not an argument that it isn't one.
