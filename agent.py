#!/usr/bin/env python3
"""
The judgment half. The model reads a truncated remittance reference and a
customer email and PROPOSES a match - it never closes an invoice itself.

Typed tool in, typed tool out: the model only ever sees candidate invoices
(id, amount, status) and free text (reference, email) - never account IDs,
API keys, or anything it could use to touch money. Code validates the
proposal (the invoice id must be one it was actually offered) before using
it for anything, and the state transition - if any - happens in code.

Note: Claude Sonnet 5 / Opus 5 removed temperature/top_p/top_k entirely (400
if sent) - there is no sampling knob to pin on these models. Determinism is
instead verified empirically by determinism_check.py, with a response cache
here as the documented fallback for the recorded demo take.
"""

import hashlib
import json
import os
import textwrap

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
CACHE_PATH = os.path.join(os.path.dirname(__file__), "agent_cache.json")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


PROPOSE_MATCH_TOOL = {
    "name": "propose_match",
    "description": (
        "Report your reconciliation judgment for one ambiguous deposit, as "
        "short filmable fields - this renders directly on screen, not as a "
        "paragraph someone reads. You are advisory only - you do not close "
        "invoices or move money. Code validates your proposed_invoice_id "
        "against the candidates you were given and decides what, if "
        "anything, happens next. Do not hedge or speculate about facts you "
        "were not given - any invoice not in the candidate list has already "
        "been resolved for a reason you do not need to guess at."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tidy_match_id": {
                "type": ["string", "null"],
                "description": "The invoice id that fits by arithmetic alone "
                                "(amount minus a plausible fee), if any. Null if none.",
            },
            "tidy_match_rejection": {
                "type": ["string", "null"],
                "description": (
                    "Under 25 words, one or two short clauses. State the arithmetic "
                    "(invoice amount less a plausible fee equals the deposit amount) "
                    "and the one concrete reason from the email it's rejected for. "
                    "No hedge words (likely, possibly, may). Null if there is no tidy "
                    "match to reject. Shape only, use the real dollar amounts in front "
                    "of you with a '$' prefix: "
                    "'$<amount> less ~$<fee> is $<deposit amount>, the closest "
                    "arithmetic fit. <one concrete reason from the email>.'"
                ),
            },
            "proposed_invoice_id": {
                "type": ["string", "null"],
                "description": "The invoice you judge this deposit is most likely for. "
                                "Must be one of the candidate ids you were given, or null "
                                "if you genuinely cannot determine one.",
            },
            "proposed_summary": {
                "type": ["string", "null"],
                "description": "Under 8 words, not a full sentence - e.g. "
                                "'<payment type> payment against <invoice id>'. "
                                "Null if proposed_invoice_id is null.",
            },
            "supporting_quote": {
                "type": ["string", "null"],
                "description": "Under 20 words, a direct quote lifted verbatim from "
                                "the customer email - not a paraphrase - that supports "
                                "the proposed invoice. Null if nothing in the email "
                                "supports a proposal.",
            },
            "unproven_reason": {
                "type": ["string", "null"],
                "description": "Under 15 words, one short clause stating why the "
                                "match can't be proven outright. Null if you are "
                                "fully confident (confidence: high).",
            },
            "payment_type": {
                "type": "string",
                "enum": ["full", "partial", "overpayment", "unknown"],
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "A level, not a number. Every escalation still goes "
                                "to a person regardless of this value.",
            },
        },
        "required": ["proposed_invoice_id", "payment_type", "confidence"],
    },
}


class AgentError(RuntimeError):
    pass


def _call_anthropic(prompt):
    client = _get_client()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"effort": "high"},
            tools=[PROPOSE_MATCH_TOOL],
            tool_choice={"type": "tool", "name": "propose_match"},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        raise AgentError(f"Anthropic API error [HTTP {e.status_code}]: {e.message}")

    for block in response.content:
        if block.type == "tool_use" and block.name == "propose_match":
            return block.input
    raise AgentError(f"No propose_match tool_use block in response: {response.to_dict()}")


def _prompt_for(deposit, candidates, email_text):
    candidate_lines = "\n".join(
        f"  - {c['id']}: ${c['amount']:,.2f}, status {c['status']}" for c in candidates
    )
    return (
        "You are the reconciliation judgment step for a coffee wholesaler's AR agent. "
        "A deposit landed that does not cleanly match one invoice. Decide, as far as "
        "the evidence allows, which open invoice it is most likely for. You do not "
        "move money or close anything - you only report your judgment via the "
        "propose_match tool.\n\n"
        f"Deposit: ${deposit['amount']:,.2f} {deposit.get('currency', 'USD')} "
        f"from counterparty '{deposit['payer_name']}', remittance reference "
        f"{deposit['reference']!r} (bank references are often truncated).\n\n"
        f"Open candidate invoices for this customer - this is the complete list, and "
        f"it is current as of right now:\n{candidate_lines}\n\n"
        "Any invoice for this customer that is NOT in that list has already been "
        "closed or is otherwise resolved. Treat that as a certain fact, not something "
        "to speculate about - do not mention or guess at why an invoice is absent from "
        "the list; it simply is not a candidate.\n\n"
        f"Customer email on file (accounts payable):\n\"\"\"\n{email_text}\n\"\"\"\n\n"
        "If an invoice amount minus a plausible transit/correspondent fee lands close "
        "to the deposit amount, that is the tidy arithmetic answer - but check it "
        "against the email before trusting it.\n\n"
        "Every field you return is displayed directly on screen to a person deciding "
        "whether to approve your judgment - not read as prose. Keep every field to its "
        "stated word limit, state facts plainly, and do not hedge with words like "
        "'likely', 'possibly', or 'may' - if you are not sure, that is what the "
        "confidence field is for."
    )


def _cache_key(prompt):
    raw = json.dumps(
        {"model": MODEL, "prompt": prompt, "tool": PROPOSE_MATCH_TOOL},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_cache(cache):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def propose_match(deposit, candidates, email_text, use_cache=False):
    """Ask the model to judge one ambiguous deposit against its candidate
    invoices. Returns (proposal, from_cache) - proposal is UNVALIDATED.
    Callers must run it through validate_proposal() before trusting any field.

    use_cache=True reuses a prior response for the exact same inputs - for
    the recorded demo run only, after a determinism check. It is never the
    default, and callers should say plainly when a cached response was used.
    """
    prompt = _prompt_for(deposit, candidates, email_text)
    key = _cache_key(prompt)

    if use_cache:
        cache = _load_cache()
        if key in cache:
            return cache[key], True

    proposal = _call_anthropic(prompt)

    if use_cache:
        cache = _load_cache()
        cache[key] = proposal
        _save_cache(cache)

    return proposal, False


def validate_proposal(proposal, candidates):
    """Code, not the model, decides whether a proposal is usable. Refuses
    anything that names an invoice it wasn't offered - the one thing a
    model must never be trusted to get right on its own."""
    candidate_ids = {c["id"] for c in candidates}
    proposed = proposal.get("proposed_invoice_id")

    if proposed is not None and proposed not in candidate_ids:
        return False, f"model proposed {proposed!r}, which is not among the candidates offered"

    tidy = proposal.get("tidy_match_id")
    if tidy is not None and tidy not in candidate_ids:
        return False, f"model's tidy_match_id {tidy!r} is not among the candidates offered"

    return True, None


def escalation_card_fields(deposit_label, amount, currency, counterparty,
                            proposal, ledger_facts=None, also_possible=None, ruled_out=None):
    """The single source of truth for an escalation card's content - a
    plain dict, reused by the terminal renderer and the JSON snapshot
    export so both show exactly the same fields in exactly the same order.
    Every fact about ledger state (what's already closed, and why) comes
    from ledger_facts, which the caller derives from actual invoice state -
    never from the model, which is never asked about it.

    also_possible/ruled_out are likewise code-computed (run_demo.candidate_readings,
    pure arithmetic on the candidates and the deposit amount) - never asked
    of the model. When a live alternate reading exists, the unproven line
    is replaced with a plain statement of that fact rather than whatever
    the model wrote, because at that point it's something code knows for
    certain, not something anyone had to judge."""
    rejected = None
    tidy_id = proposal.get("tidy_match_id")
    # Only a genuine rejection - if the tidy match IS the final proposal
    # (evidence didn't contradict it), there's nothing to label "Rejected".
    if tidy_id and tidy_id != proposal.get("proposed_invoice_id"):
        rejected = f"{tidy_id} — {proposal.get('tidy_match_rejection', '')}"

    supporting = []
    if proposal.get("supporting_quote"):
        supporting.append(f'Email: "{proposal["supporting_quote"]}"')
    supporting.extend(ledger_facts or [])

    related = [i for i in (proposal.get("proposed_invoice_id"), proposal.get("tidy_match_id")) if i]

    also_possible = also_possible or []
    unproven = proposal.get("unproven_reason")
    if also_possible:
        unproven = "The evidence supports both live readings. Only the customer can separate them."

    return {
        "deposit_label": deposit_label,
        "amount": amount,
        "currency": currency,
        "counterparty": counterparty,
        "proposed": proposal.get("proposed_summary") or "No proposal - escalate as-is",
        "also_possible": also_possible,
        "rejected": rejected,
        "ruled_out": ruled_out or [],
        "supporting": supporting,
        "unproven": unproven,
        "confidence": proposal.get("confidence", "unknown"),
        # code-known, not parsed from text - the UI highlights exactly these
        # invoices as "under escalation" without guessing from prose.
        "related_invoice_ids": sorted(set(related)),
    }


def render_escalation_card(fields, width=58):
    """Render escalation_card_fields() as the fixed-shape, filmable card -
    not prose."""

    def field(label, text_lines):
        prefix = f"  {label:<12}  "
        pad = " " * len(prefix)
        out = []
        for i, line in enumerate(text_lines):
            out.append((prefix if i == 0 else pad) + line)
        return out

    def wrapped(label, text):
        return field(label, textwrap.wrap(text, width=width) or [""])

    lines = [
        f"ESCALATION · Deposit {fields['deposit_label']} · "
        f"${fields['amount']:,.2f} {fields['currency']} · {fields['counterparty']}",
        "",
    ]

    lines += field("Proposed", [fields["proposed"]])

    for i, alt in enumerate(fields.get("also_possible") or []):
        lines += field("Also possible" if i == 0 else "", textwrap.wrap(alt, width=width) or [""])

    if fields["rejected"]:
        lines += wrapped("Rejected", fields["rejected"])

    for i, ro in enumerate(fields.get("ruled_out") or []):
        lines += field("Ruled out" if i == 0 else "", textwrap.wrap(ro, width=width) or [""])

    for i, fact in enumerate(fields["supporting"]):
        wrapped_lines = textwrap.wrap(fact, width=width) or [""]
        lines += field("Supporting" if i == 0 else "", wrapped_lines)

    if fields["unproven"]:
        lines += wrapped("Unproven", fields["unproven"])

    lines += field("Confidence", [f"{fields['confidence'].capitalize()} — needs a person."])

    return "\n".join(lines)
