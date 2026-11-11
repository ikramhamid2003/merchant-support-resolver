# Next Steps — Merchant Support Resolver

**Date:** 11 Nov 2026
**Status:** All 15 findings from the two improvement rounds are closed. This is what comes next, beyond them.

The project is demo-ready: 115 tests pass, the Groq path runs live on `openai/gpt-oss-120b`, the demo server runs
from this directory, and the pitch deck matches the code. The items below are what would take it from a hackathon
demo to something a Paytm team could actually deploy.

Ordered by what unblocks the most value first. Nothing here is needed for the finals.

---

## Tier 1 — what a real pilot needs

### 1. Replace the SQLite stubs with Paytm's real APIs

Every `tool_*` method in `app.py` is a stub over a local database. The pilot version calls Paytm's actual settlement,
device, dispute and ticketing services.

The agent loop, the policy gate and the audit log do not change — that is the point of the current architecture. Only
the bodies of the nine `tool_*` methods get replaced.

**Why first:** it is the only item that makes the pitch true rather than a simulation.

### 2. Durable storage

SQLite is right for a demo and wrong for a pilot. Move to PostgreSQL for the ticket, audit and settlement tables.

Two things to carry over deliberately: the `migrate()` function already exists and knows how to add columns without
losing data, and the audit log is append-only — it should stay that way, because it is the record a support manager
will be asked to defend.

### 3. Authentication and merchant scoping

Today the merchant id arrives in the request body and the gate trusts it. In production the merchant id has to come
from the authenticated session, not the caller.

`policy_gate()` already refuses a cross-merchant read, so the enforcement point exists — what is missing is the
identity source. Derive the merchant from the session and treat the body value as untrusted input.

### 4. Rate-limit budgeting across users

The measured limit on the demo key is 1,000 requests/minute and 8,000 tokens/minute. One demo merchant is fine; a
pilot with several support agents sharing a key is not.

Add a token bucket in front of `groq_chat()` that accounts for all users of the key, not just one conversation. The
retry-and-backoff logic already in place is the right second line of defence, but it should not be the first.

---

## Tier 2 — makes the demo land harder

### 5. Voice channel

The deck says voice is the next step. The groundwork exists: replies are already localised and the agent already
turns a merchant's words into a category, which is exactly what speech-to-text feeds in.

A voice path means capturing audio, transcribing, feeding the transcript into the existing `process_message()`, and
speaking the reply back in the merchant's language.

### 6. More Indian languages

English, Hindi and Tamil are covered. The next most valuable for merchants are Bengali, Telugu, Marathi, Gujarati
and Kannada.

`fmt_date()` and the escalation reasons are already per-language tables, so a new language is a table plus a
classifier keyword set — not a rewrite. The classifier also needs the language weights checked: a word that is
distinctive in English may be a common word in one of the new languages, which is the same class of bug as finding 2.

### 7. A richer demo seed

The current seed is 8 merchants, 8 settlements, 8 devices and 2 disputes, deliberately small so the rules and the
guardrails are visible.

A demo seed with more variety would show the system handling edge cases: a merchant who asks twice, a case that
resolves and then reopens, a ticket that needs the conversation memory to make sense.

### 8. Analytics beyond the current tiles

The Outcomes panel shows handled, auto-resolved, escalated, average time and the rules-vs-Groq split. What is missing
is the trend view: auto-resolution rate over time, escalation rate by category, and which merchant types generate the
most tickets.

This is what turns the demo into something a support manager would actually keep open.

---

## Tier 3 — hardening

### 9. Structured logging

The audit log is a table, which is right for the demo and awkward for operations. Ship the same events to structured
logs with request ids, so a failure can be traced across a conversation, a Groq call and the tool it triggered.

### 10. A model-evaluation set

The safety net in finding 13 exists because the model sometimes narrates a case instead of acting on it. That is
model-side variance, so it will vary with the model version.

Build a small evaluation set of recorded merchant messages with the expected category, expected action and expected
guardrail outcome, and run it in CI. It turns "the model behaved well last week" into a check that fails loudly when
a model release changes behaviour.

### 11. Property-based tests for the policy gate

The gate is the safety story, and it is currently tested by example. A property-based test that generates random tool
calls and asserts the invariant — *no call outside policy ever takes effect* — covers combinations nobody wrote by
hand.

### 12. Secrets and key rotation

The key lives in `.env` and is never sent to the browser. For production it belongs in a secret manager, rotated on a
schedule, with usage tracked per merchant so one merchant's traffic cannot starve another's.

---

## Deliberately not on this list

- **More tools.** Nine tools cover the ticket types in scope. Adding tools raises the risk surface faster than it
  raises capability — and finding 12 showed that every extra tool costs tokens on every request.
- **A bigger model.** `openai/gpt-oss-120b` handles the tool calling reliably. A larger model costs more against a
  tokens-per-minute limit without fixing anything finding 13 did not already fix in code.
- **A rewrite of the agent loop.** The current spine — one policy gate for reads and actions, code-injected merchant
  id, an audit log on every call, deny-then-escalate — is the reason the safety claims can be made plainly. It should
  be extended, not replaced.
