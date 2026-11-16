# Reconciliation agent — Airwallex Agentic Banking Hackathon

Full Airwallex builder guide is at `docs/builder-guide.md`. Consult it before
guessing or probing for any payload shape, field name or state transition. Do
not discover by experiment what the guide already documents. Kit 1 and kit 3's
"Watch out for" sections and the "Sandbox constraints" section cover
beneficiaries, transfers and FX.

Solo build. **Already submitted** — the HackerEarth portal closed Sat 15 Nov
12:29 PM Central; submission went in at 12:28. The "Sun 15 Nov 23:59 PT"
deadline below was wrong (left over from an earlier read of the rules) and
is no longer live. Work from here on is not racing a clock: it's a better
video and a shareable link, post-submission.
Deliverables: working demo, video **under 5 min**, repo with setup instructions,
uploaded on the HackerEarth microsite. Repo may be private if shared with
GitHub user `danxkim`. Sandbox only, synthetic data, no real money.

## What this is

A small Chicago coffee roaster, **Ravenswood Roasters**, sells wholesale to cafés
abroad. Money arrives that never matches the invoices: fees come out in transit,
customers part-pay, overpay, and lump invoices together, and wire remittance text
gets truncated so you can't tell what a payment was for.

Two halves to the problem, and the build says so explicitly:

1. **Infrastructure half** — the agent mints one Airwallex Global Account per
   customer, so an incoming payment is pre-attributed to *who* sent it. Big
   corporates have done this for decades with virtual account ranges; it needed a
   banking relationship and a six-figure treasury setup. Airwallex made it one
   API call. (Say it this way — do not claim we invented it.)
2. **Judgment half** — the account says *who*, never *which invoice*. That part
   needs reading a customer email and weighing what is likely. That is the agent.

## The demo arc (this is the spec — build exactly this, nothing more)

Airwallex's own rubric: show the user's goal, the agent's initial plan, the new
information, the revised decision, and the resulting balance/payment/status.
Finish with a financial action or an escalation to a person.

**Open invoices** (all billed USD):

| Invoice | Customer    | Amount    | Note                          |
|---------|-------------|-----------|-------------------------------|
| INV-104 | Café Brûlé  | $5,000.00 |                               |
| INV-106 | Café Brûlé  | $1,800.00 |                               |
| INV-108 | Café Brûlé  | $2,350.00 | customer has queried this one |
| INV-112 | Northgate   | $3,200.00 |                               |

**Evidence in the pile** — email from Café Brûlé's accounts payable:

> Hi — we've queried INV-108 with your sales team, there's a line on it we don't
> recognise, so we're holding that one for now. Paying INV-106 in full and
> starting on INV-104 this week.

**Run 1 — three deposits land:**

- **D1** `$3,187.15` · Northgate · ref `NORTHGATE/INV112`
  → INV-112 less a $12.85 SWIFT fee. Inside tolerance. **Auto-close in code.**
  The model is not consulted; a fixed rule belongs in code.
- **D2** `$2,310.00` · Café Brûlé · ref `BRULE/INV1` (truncated)
  → **The trap.** Arithmetic offers a tidy answer: INV-108 ($2,350) minus ~$40 of
  correspondent-bank fees ≈ $2,310. Any rules engine takes it. It is wrong — the
  email says INV-108 is being held. It is really a part payment on INV-104.
  The agent cannot prove the split, so it **escalates with its reasoning**,
  naming both candidates and why it rejected the tidy one.
- **D3** `$1,950.00` · Café Brûlé · ref `INV106`
  → INV-106 is $1,800, so $150 overpaid. **Refund the $150** — money moves.

**Mid-run — a fourth deposit lands:**

- **D4** `$2,690.00` · Café Brûlé · ref `BRULE/INV104 FINAL`
  → $2,310 + $2,690 = $5,000 exactly. D2 is confirmed as a partial on INV-104.
  The agent **recalculates only INV-104**, closes it, and withdraws its own
  escalation. Everything else is left alone.

**End state:** INV-112 closed (fee variance booked), INV-106 closed ($150
refunded), INV-104 closed, INV-108 still open and correctly untouched.

**The line the video turns on:** the tidy arithmetic would have closed INV-108
and quietly written off $2,350 the customer is actively disputing.

## Architecture rule (straight from their rubric)

- **Code owns**: tolerance thresholds, the fee table, matching arithmetic,
  duplicate protection, state transitions, all amounts and limits.
- **The model owns**: reading the truncated reference and the customer email,
  judging whether a gap is explainable, proposing a match when arithmetic is
  ambiguous, writing the rationale a human reads.
- Give the model **typed tools**, never raw credentials.
- Bind every approval to amount, currency, counterparty and the evidence shown.
- If the agent would make the same choice every time, it is a rule — put it in
  code and do not ask a model to narrate it.
- Put sandbox-only simulation calls behind **one interface** so they could be
  swapped without touching decision logic.

## Build order (tripwire, not a cut)

1. **[DONE]** Spine (`spine.py`): provision per-customer accounts → seed
   ledger → land D1–D3 → match → terminal output. Runs end to end.
2. **[DONE]** Agent layer (`agent.py`, `run_demo.py`): the D2 judgment,
   escalation (with evidence binding), re-evaluation as candidates narrow,
   D4 revision and withdrawal. Determinism checked empirically
   (`determinism_check.py`) since Sonnet 5/Opus 5 have no temperature knob.
3. Refund path for D3 (beneficiary → transfer). Only once 1 and 2 run.
4. Static HTML view reading the agent's JSON output.
5. Video, README, submit.

If the spine is not running, stop extending and make it run.

## Airwallex sandbox facts

Base `https://api.sandbox.airwallex.com`, paths start `/api/v1`.

- **Login**: `POST /api/v1/authentication/login`, headers `x-client-id` and
  `x-api-key`, **no body**. Returns **HTTP 201** (not 200) and a token valid
  30 minutes. Accept 200 *and* 201.
- **Amounts are major units.** `100` is one hundred dollars.
- **`request_id`** must be a UUID. Reuse it only to retry the *same* operation;
  reusing it for a different one inside 7 days returns `duplicate_request_id`.
  Transfers reject ids under 10 or over 50 chars. Derive the refund's request_id
  from the invoice id so Airwallex's own idempotency prevents a double refund —
  do not hand-roll a lock.
- **Global accounts**: `POST /api/v1/global_accounts/create` with `country_code`
  and `required_features: [{currency, transfer_method}]`. Several in the same
  currency is allowed — verified.
- **Deposit simulation**: `POST /api/v1/simulation/deposit/create` with
  `global_account_id`, `amount`, `payer_name`. Reports PENDING but posts
  immediately; re-read balances before recalculating.
- **Deposits list**: `GET /api/v1/deposits`, plain — no `x-api-version`, no
  date params. Verified working. Each row carries `global_account_id`,
  `payer.name`, `reference`, `amount`, `status`. Date-window params rejected a
  malformed timezone offset in testing; not needed. Returns all deposits on
  the account, so filter by the demo account IDs and a run-start timestamp.
  Treat PENDING and SETTLED alike.
- **FX**: leave `x-api-version` **off** FX calls. A rate is not a quote —
  `rate` on a rate check, `client_rate` on a quote. Quotes are single-use.
  Conversions settle immediately. No MCP tool; use REST.
- **Transfers**: send `transfer_currency` / `transfer_amount` (not
  `payment_currency`). A failed transfer ends **CANCELLED**, never FAILED, and
  CANCELLED does not mean a human cancelled it — read `failure_type`.
  SWIFT costs a flat fee per payout; LOCAL is free. Transfers need a
  `simulation/transfers/{id}/transition` call to reach a terminal state.
- **Beneficiaries** (refund path): call `POST /api/v1/beneficiaries/schema`
  first. Wrap validation as `{"beneficiary": {...}, "transfer_methods": ["LOCAL"]}`
  — `transfer_methods`, not `payment_methods`. For **US / USD / LOCAL**: flat
  `bank_details` with `account_routing_type1: "aba"` and `bank_account_category`
  exactly `"Checking"` or `"Savings"` (other casing rejected, code 016); address
  needs `street_address`, `city`, `state`, `postcode`, `country_code`.
  Use a real routing number with a synthetic account number.
  **Use US/USD/LOCAL for the refund** — no SWIFT fee arithmetic.
- **Rate limits**: 20 req/s global, 10 per endpoint, 10 concurrent. Back off on
  429; no bursts of parallel requests during the demo.
- Docs MCP (no login): `https://mcp.sandbox.airwallex.com/docs`

## Credentials

`AWX_CLIENT_ID` and `AWX_API_KEY` in `.env`, never committed. Quote values that
contain spaces. No IP allowlist on the key.

## Judging context (why choices were made)

Judges are from fintech and venture; Demo Day is 45 min of finalist demos, so
3–5 min live to a room holding pizza. What scored highest in every account we
found: the demo actually running, one complete path over several partial ones,
and whether a judge can repeat your project to someone else afterwards. Be
explicit in the video about what is real and what is stubbed — judges can tell,
and blur reads as hiding.
