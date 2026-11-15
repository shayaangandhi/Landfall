# Judge audit — adversarial checklist

Work through these in order. Highest risk first. For each: state PASS or FAIL
with the file and line as evidence, then fix the FAILs. Don't rationalise a
FAIL into a PASS — the point of this list is to find things before a judge does.

---

## A. Is the judgment real, or is it theatre?

These are the ones that destroy credibility if a judge pokes at them.

**A1. Change the evidence, change the decision.**
Delete the "we're holding INV-108" sentence from the customer email and re-run.
*Pass:* the agent now proposes INV-108, because the tidy arithmetic is no longer
contradicted. *Fail:* it still proposes INV-104, which means the answer is baked
in somewhere and the model is decoration.
This is the single most likely thing a sharp judge asks you to do live. Make it
a test in the repo.

**A2. No invoice IDs hardcoded in the decision path.**
Grep for `INV-104`, `INV-108` etc. outside fixtures and tests.
*Pass:* they appear only in seed data. *Fail:* any appear in agent or matching logic.

**A3. D4 is discovered, not scripted.**
*Pass:* the run loop re-reads `GET /api/v1/deposits` and acts on anything it
hasn't processed. *Fail:* a hardcoded "now process deposit 4" step.

**A4. The model can't be bypassed by an if-statement.**
*Pass:* removing the Anthropic call makes the ambiguous case unresolvable.
*Fail:* there's a fallback heuristic that would have reached the same answer —
in which case the honest thing is to say so, or delete it.

**A5. Re-runnable.**
Run the full demo three times in a row from a clean start.
*Pass:* identical end state each time. *Fail:* accumulated deposits leak in,
or the second run errors instead of recognising the refund is already done.

---

## B. Money safety — what a payments judge checks

**B1. Floats.**
Grep for `float(` and any arithmetic on money.
*Pass:* amounts are `Decimal` or integer minor units throughout. *Fail:* any
float. `0.1 + 0.2` is the oldest tell in fintech and a judge from this world
will look.

**B2. The model never moves money.**
*Pass:* no code path lets a model output reach a transfer, capture or state
change without code validating it first. *Fail:* anything else.

**B3. Amounts are never model-generated.**
*Pass:* every figure that reaches an API call is computed in code. The model
proposes *which invoice*, never *how much*.

**B4. Credentials never enter model context.**
Grep the prompt construction for anything from `.env`, tokens, account IDs that
aren't needed. *Pass:* the model sees invoice data, the reference, and the email.

**B5. Approval is bound, not a bare yes.**
*Pass:* the approval record names amount, currency, counterparty and the
evidence, and the executed transfer is checked against that record before
sending. *Fail:* approval is a boolean and the transfer is built separately —
then approving one thing and sending another is possible.

**B6. Timeout handling is stated.**
Per the guide, a timeout is unconfirmed, not failed.
*Pass:* on an ambiguous response the code queries by `request_id` before
retrying. At minimum, a comment saying so and the idempotent request_id making
it safe.

**B7. Transfer state vocabulary is right.**
*Pass:* code treats CANCELLED as the failure terminal state and reads
`failure_type`; SENT is never treated as final.

---

## C. Rubric compliance (their words, their criteria)

**C1. Simulation calls behind one interface.**
*Pass:* every `/simulation/` call lives in one module that decision logic calls
through. *Fail:* simulation calls scattered through matching code.

**C2. Fixed rules live in code, not prompts.**
*Pass:* the tolerance threshold, the fee table and the matching arithmetic are
code. The model is never asked to decide something it would always decide the
same way.

**C3. Typed tools.**
*Pass:* the model is given a tool with a constrained schema, and out-of-bounds
proposals are refused. Already tested with INV-999 — keep that test.

**C4. The demo arc is visible in output.**
*Pass:* goal, initial plan, new information, revised decision, and resulting
status are each identifiable on screen.

---

## D. Provenance — "what did you build during the hackathon?"

**D1. Every file is yours and recent.**
*Pass:* nothing vendored or copied in that you'd struggle to explain. If
anything is, say so in the README. Blur on this question reads as hiding.

**D2. The README claims nothing the code doesn't do.**
Read the README against the code line by line. Overclaiming is worse than a
smaller claim, because a judge who finds one inflated line distrusts all of it.

**D3. Known limitations are stated.**
Deposits are simulated. Polling stands in for a webhook. Two customers, not a
production ledger. Say all of it. Judges rated honesty above polish.

---

## E. Repo hygiene

**E1. No secrets.** `.env`, `state.json`, any key, anywhere in git history —
not just the working tree. Check `git log -p | grep -i "api.key"`.

**E2. Cold clone runs.** Clone to a fresh directory, follow only the README,
and see if it works. No step you "just know."

**E3. requirements.txt is complete and pinned.**

**E4. Repo shared with `danxkim` if private.**

---

## F. The demo itself

**F1. Escalation card legible at 1080p**, judged by looking at it on a phone.

**F2. Final frame shows INV-108 still open** beside the closed ones.

**F3. Video under 5:00.** Not 5:01.

**F4. Runs offline** or with the network you'll actually have while recording.

---

## How to report

For each check: ID, PASS/FAIL, evidence (file:line or command output), and for
FAILs what you changed. Do A and B first; if time runs short, C through F can
be partially done, but A1, B1, B2 and E1 are not optional.
