
# Merchant Support Resolver — Improvement Findings

**Date:** 10 Nov 2026 (round one) · 11 Nov 2026 (round two: the top five improvements)
**Status:** All 10 findings from round one are **implemented** — findings 1-9 change behaviour, finding 10 fixes the setup path and the docs. All 5 round-two improvements are **implemented** as well.
**Scope:** Full read of `app.py`, `index.html`, `test_agent.py`, `README.md`; test suite run; live app exercised with the real Groq key; targeted behaviour probes.

The core architecture — `policy_gate()` as the single checkpoint for every action, code-injected `merchant_id`, audit log on every call, deny-then-escalate as a safety net — is genuinely solid and is the right spine to build on. The findings below are improvements on top of that, not a request to restructure it.

---

## Fixed

### 1. ~~The Groq path is dead with the default model~~ — DONE

`app.py` defaulted to `llama-3.3-70b-versatile`, which Groq shut down for free and developer tiers on 2026-08-16. The key in `.env` is valid, but the default model was not, so `app.py --check-groq` returned HTTP 404 and the LLM engine silently fell back to rules.

**Fixed:** the default is now `openai/gpt-oss-120b`, and `.env` was updated to match. Verified live against the real key — the key, the model and tool calling all work. The deprecation and the env-var override rule are documented in `README.md`, and `ModelConfigTests` fails if the retired id comes back.

### 2. ~~The keyword classifier misroutes real merchant phrasing~~ — DONE

`KEYWORDS` used first-match matching, and the bare phrase `"not working"` sat in the device list, so device was checked before settlement and settlement complaints were routed to the device flow.

**Fixed:** `KEYWORDS` is now a table of `(keyword, weight)` pairs and `classify_keywords()` takes the best-scoring category. Weights encode distinctiveness, so `"settlement"` (12) beats `"money"` (4) and `"refund"` (12) beats both. A new `refund` category routes refund requests to `flow_refund()`, which never auto-approves and escalates with the merchant's recent transactions as the case file.

### 3. ~~Dates are English inside Hindi and Tamil replies~~ — DONE

`fmt_date()` returned `"tomorrow (11 Nov)"` regardless of language, so the Hindi reply ended with an English date.

**Fixed:** `fmt_date()` is language-aware (`tomorrow (11 Nov)` / `कल (11 नव)` / `நாளை (11 நவ)`). The escalation reason is localized too, via an `ESCALATION_REASON` table, with the detailed English reason and case file kept in the audit trail for the human agent. `money()` now uses Indian digit grouping (`1,84,500`). The hi/ta `escalated` templates gained the `{reason}` slot they were missing.

**Test suite: 27 → 42 tests, all passing.**

### 4. ~~The human-handoff queue is invisible in the UI~~ — DONE

`/api/escalations` and `list_escalations()` existed and were covered by tests, but `index.html` never called the endpoint — `refresh()` only hit `/api/metrics` and `/api/tickets`.

**Fixed:** a **Human handoff queue** panel now sits between the chat and the Outcomes panel. Each case shows the case id, merchant and ticket number, the merchant's own words, why it was escalated, the full case file the agent prepared (settlement / dispute / device / transaction rows, rendered as indented key-value pairs with amounts grouped), and the reply the merchant was given with its language. A count badge and an empty state are included. `list_escalations()` was extended to return the reply and language so the human agent can see what the merchant was actually told. The panel refreshes with every message and clears on "Reset demo data".

**Test suite: 42 → 45 tests, all passing.**

---

### 5. ~~No way to compare the two engines after the fact~~ — DONE

The `tickets` table had no `engine` or `model` column, so the README's "uncheck the Groq box to compare with the rule-based agent" could not be shown in the UI or measured afterwards.

**Fixed:** each conversation records the engine that actually answered it (`engine`), the model used (`model`, Groq only), and whether the request fell back to the rule engine (`fallback`). `metrics()` returns `by_engine`, `avg_ms_by_engine`, `models` and `groq_fallbacks`. The Outcomes panel gained a second row of tiles — *Handled by rules*, *Handled by Groq*, *Avg time · rules*, *Avg time · Groq* — plus a note naming the model in use and counting fallbacks. The Recent tickets table and each handoff-queue card now carry an engine badge.

**Fallback semantics:** a Groq request that fails is recorded as `engine="rules"` with `fallback=1`, so a degraded run is never counted as a Groq success and the rules total stays honest.

**Migration:** `init_db()` now runs `migrate()` when the database already exists, adding the three new columns with `ALTER TABLE` instead of dropping the demo data.

**Test suite: 45 → 53 tests, all passing.**

### 6. ~~No conversation memory~~ — DONE

A follow-up could not be resolved back to what the agent had just done. Verified before the fix:

```
M001: "My settlement has not arrived"   -> resolved, retrigger_settlement
M001: "yes please do that"              -> needs_info, category "unknown"
```

**Fixed — three parts:**

1. **Category inheritance.** `resolve_category(message, history)` falls back to the previous turn's category when the
   message has no keyword of its own and either contains a continuation cue (`CONTINUATION_CUES`: "yes", "also",
   "any update", "still", "do that" …) or is three words or fewer. A genuinely new question — "what is the weather
   today" — has neither, so it correctly starts a fresh conversation instead of inheriting.
2. **History in the model prompt.** `run_groq()` passes the last four turns to Groq and the system prompt tells it to
   use them, so the model answers "did that work?" about the thing the agent just did.
3. **One case per issue.** `primary_record()` identifies which record a case is about (`settlement:S1007`,
   `dispute:CB02`, `device:D04`, `merchant:M001`) and `Agent.open_case()` reuses an already-open case for the same
   record, so a follow-up about an escalated issue does not raise a second one. The merchant is told the case is
   already with the team, via the new `already_escalated` reply (en/hi/ta).

The `tickets` table doubles as the conversation log — no new table and no extra migration.

**Test suite: 53 → 61 tests, all passing.**

### 7. ~~`auto_resolution_rate` counts `needs_info` in the denominator~~ — DONE

One resolved + one escalated + one needs_info yielded `auto_resolution_rate: 33`. An unclassified message is not a
resolution failure — it is a clarification request — so it was dragging the headline number down.

**Fixed:** the rate is now measured over **decided** cases only: `resolved / (resolved + escalated)`. A clarification
never counts for or against the agent. The same conversation now reports **50%** instead of 33%. `metrics()` also
returns the new `decided` field so the denominator is visible rather than implied, and the Outcomes panel states
plainly how many messages were excluded and why.

Escalated cases deliberately stay in the denominator: handing a case to a human is not an auto-resolution, so counting
it against the rate is correct.

`avg_ms` is unchanged — it still averages over every ticket the agent handled, clarifications included, because it
measures turnaround time rather than success.

**Test suite: 61 → 67 tests, all passing.** Six regression tests cover the excluded denominator, the escalation case
that must stay in it, clarifications that must not move the rate, the empty-database and clarifications-only
edge cases, and the all-resolved 100%.

### 8. ~~The read path has a defence-in-depth gap~~ — DONE

`policy_gate()` correctly denies cross-merchant reads, and the Groq path was safe because `run_tool()` injects
`merchant_id` before gating. But `Agent.call()` only invoked the gate for action tools:

```python
if self.mid and name in ACTION_TOOLS:  # read tools never reached the gate
```

So `Agent.call("get_settlement_status", merchant_id="M007")` from M001 returned M007's settlement details, including
amount, UTR and failure reason.

**Fixed:** `Agent.call()` now passes **every** tool call through the gate, reads included, so the invariant lives in one
place and holds on both call paths. A read that names another merchant raises `PolicyDenied` and is written to the
audit trail as `BLOCKED <tool>: read access is limited to the merchant in this chat`. An agent with no merchant
context may not read at all. The rule engine reads with the merchant's own id, so no flow changes behaviour.

Verified with the real key: the ungated tool still returns M007's row (`S1007`, Rs 9,800, failed) — which is exactly
what the gate now prevents — while `Agent.call` refuses it and M001's own read still returns `S1001`. Both engines
were exercised live: rules resolved the settlement, restarted the device and escalated the chargeback; Groq read the
settlement through the gate (`allowed get_settlement_status: read-only lookup for this merchant`) and ordered the
replacement.

**Test suite: 67 → 75 tests, all passing.** Eight regression tests cover the cross-merchant read on all three read
tools, a read with no merchant named, an agent with no merchant context, the merchant's own read still working, the
denial reaching the audit log, and the normal flows reading their own data.

### 9. ~~No cap on bank tickets or device restarts~~ — DONE

`POLICY` capped settlement retriggers via `max_retriggers`, but `raise_bank_ticket` and `remote_restart_device` had
no equivalent limit. Verified before the fix: four consecutive `raise_bank_ticket` calls were all allowed. The tool is
close to idempotent (it reuses an existing ticket reference), so the impact was low, but the asymmetry with
`max_retriggers` left the guardrail story incomplete.

**Fixed:** `POLICY` now carries **`max_bank_tickets: 2`** and **`max_device_restarts: 2`**, and the counters live on
the record — `settlements.bank_ticket_count` and `devices.restart_count` — so the cap holds across turns, not just
within one. The gate refuses the action once the counter is reached ("bank ticket limit reached (2 of 2)"), and the
tools increment the counter only when the action really happened.

**A cap must not crash the turn.** The rule flows now catch the refusal and escalate instead of letting
`PolicyDenied` escape: a third request hands the case to a human with the count in the case file
(`bank_ticket_count: 2`) and tells the merchant why, in their language. The Groq path already had this safety net,
so the denial travels back to the model as an error and the model escalates.

Verified live: M002 asked three times in Hindi and got two bank tickets, then the third turn escalated with *"हमारे
नियमों के अनुसार जितने बैंक टिकट बन सकते थे, हम बना चुके हैं"*; M003's device dropped off the network three times and
the third restart escalated with *"we have already restarted the device as many times as our rules allow"*, the fourth
turn reusing the same case rather than opening a second one.

**Migration:** the two counters are added with `ALTER TABLE` by `migrate()`, so an existing demo database keeps its
data. The seed inserts now name their columns, so adding a column can no longer silently break them.

**Test suite: 75 → 87 tests, all passing.** Twelve regression tests cover the declared limits, the first action still
being allowed, both counters counting, both caps stopping the action, the normal flows being unaffected, the cap
holding across turns, the denial reaching the audit log, the escalation instead of a crash, and the migration.

### 10. ~~Scaffolding and documentation inconsistencies~~ — DONE

Six loose ends that made the setup path fail or the docs wrong:

- **`.env.example` did not exist**, though the README lists it and instructs `cp .env.example .env`. It now exists and
  carries the two keys the app actually reads (`GROQ_API_KEY`, `GROQ_MODEL`), with the model id the app defaults to and
  a note about the retired one. The documented setup path now works.
- **`requires-python = ">=3.14"`** contradicted the README's "3.9+". The code is 3.9-compatible, so the constraint was
  wrong and needlessly strict; it now reads `">=3.9"`.
- **`main.py`** was the `uv init` leftover, a stub printing "Hello from hackathon!". It is gone.
- **`merchant_support.db` was not in `.gitignore`.** `*.db` is now ignored, with a comment pointing at `--reset`.
- **`python` is not on `PATH`** on this machine. The README now documents the Windows invocation
  (`.\.venv\Scripts\python.exe app.py`) for the app, the tests and the reset.
- **`pyproject.toml` read** `description = "Add your description here"`. It now describes the project.

**`avg_ms`** mixes Groq API latency with rule execution time, so one number cannot compare the engines. It is left as
the overall turnaround number and the README now says so plainly, pointing at the per-engine tiles — *Avg time · rules*
and *Avg time · Groq* — as the apples-to-apples comparison.

---

## Recommended order of work

| # | Change | Effort | Impact | Status |
| --- | --- | --- | --- | --- |
| 1 | Fix the default Groq model ID (app.py:50, README.md:66) | 1 line | Unblocks the whole Groq demo | **Done** |
| 2 | Fix classifier ordering + keywords + add refund category | ~15 lines | Fixes 3 wrong-answer cases | **Done** |
| 3 | Localize `fmt_date()`, add hi/ta `{reason}`, Indian grouping in `money()` | ~30 lines | Highest visible quality payoff | **Done** |
| 4 | Add an escalation-queue panel to `index.html` | ~40 lines | Shows the safety story in the demo | **Done** |
| 5 | Persist `engine`/`model`/`fallback` on tickets; show rules-vs-Groq split | ~20 lines | Makes the comparison measurable | **Done** |
| 6 | Multi-turn conversation memory: category inheritance, model history, no duplicate cases | ~40 lines | Enables follow-up requests | **Done** |
| 7 | Exclude `needs_info` from the `auto_resolution_rate` denominator | ~5 lines | Makes the headline metric honest | **Done** |
| 8 | Gate read tools in `Agent.call()` as well | ~10 lines | Closes the defence-in-depth gap | **Done** |
| 9 | Add `max_bank_tickets` and `max_device_restarts` to POLICY | ~10 lines | Completes the guardrail story | **Done** |
| 10 | Scaffolding: `.env.example`, delete `main.py`, fix pyproject, `.gitignore` | ~10 min | Removes confusion | **Done** |

All 10 findings are done. Findings 1-9 change how the agent behaves; finding 10 makes the documented setup path work.

---

## Round two — the top five improvements

After the first 10 findings, the project was reviewed again against the one constraint that actually bites at
demo time: **a shared, rate-limited API key**. The measured limits on this key are 1,000 requests/minute and
8,000 tokens/minute. The five changes below make the demo survive that, and make the safety story harder to argue
with.

### 11. ~~A 429 ends the turn immediately~~ — DONE

`groq_chat()` made a single request. A single HTTP 429 dropped straight to the rule engine, so a throttled key
turned a Groq-quality answer into a rules-quality answer — and the audit trail recorded it as a fallback, which
looked like a failure even though nothing had broken.

**Fixed:** `groq_chat()` now retries. A request that comes back HTTP 429 or 5xx is retried up to
`GROQ_ATTEMPTS = 3` times with a growing pause between attempts (`GROQ_BACKOFF = (2, 6)` seconds, capped at
`GROQ_MAX_WAIT = 12`). The wait is chosen in order of preference: a `Retry-After` header if the server sent one,
then the "try again in 5.8725s" hint Groq puts in the error body, and only then the fixed backoff. A request that
was worth making gets a second and third chance instead of falling over on the first refusal.

### 12. ~~Every request carries all 9 tools~~ — DONE

The agent had 9 tools and offered all of them on every request, so a settlement question shipped the
device-replacement and dispute-proof tools to the model too. That costs tokens against a tokens-per-minute limit
and invites the model to reach for an action that does not belong to the category.

**Fixed:** `CATEGORY_ACTION_TOOLS` maps each category to the actions that make sense for it, and
`tools_for_category()` builds the per-request tool list — all the read tools (gated, harmless, and needed to
diagnose) plus only that category's action tools, plus `escalate_to_human`.

Measured on the live prompt: **672 → 517 tokens, 21% fewer.**

**Enforced in code, not in the prompt.** `run_tool(allowed=...)` refuses any tool that was not offered for that
request, so a model that asks for an unoffered tool anyway is stopped by the program. The refusal is written to
the audit trail as `… was not offered for this request`. It deliberately does *not* trigger the deny-then-escalate
net: the model may still read and answer, but the action cannot happen.

### 13. ~~The model can describe a case instead of handling it~~ — DONE

Found while testing the trimming: on some chargeback turns the model wrote a correct-sounding explanation and
never called a tool. The turn ended with the merchant told what would happen, while nothing had actually
happened and no case existed. This was proven to be **model-side variance, not caused by trimming** — the same
behaviour reproduced with the full 9-tool list.

**Fixed:** the rule engine's verdict now decides. If the model only describes a case, `Agent.rule_flow()` runs
and its outcome is authoritative, reusing the same single implementation of policy rather than adding a second
one. A dispute over the limit still reaches a human even when the model rambles. When the rule engine acted or
escalated, the merchant's reply is rebuilt from the template, so the message always matches what actually
happened. `RULE_CATEGORIES = ("settlement", "device", "chargeback", "refund")` bounds where this applies.

### 14. ~~The HTTP layer was never tested~~ — DONE

`app.py` exposes eight endpoints and the test suite exercised none of them over HTTP. Every previous check was a
manual `curl` against a running server, which is exactly the check that stops being run.

**Fixed:** a new `HttpApiTests` class boots a real `ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)` on a random
port and exercises every endpoint, plus the error paths: an empty message, malformed JSON, a non-UTF-8 body, a
404, and the reset endpoint.

### 15. ~~Nothing runs the tests automatically~~ — DONE

The suite only ran when someone remembered to run it, on one interpreter.

**Fixed:** `.github/workflows/tests.yml` runs `python test_agent.py` on a matrix of Python **3.9, 3.12 and 3.13**,
then starts the server and smoke-tests it (server output redirected, then the process killed). The 3.9 leg matters
because `pyproject.toml` claims `requires-python = ">=3.9"` — now that claim is enforced on every push.

**Test suite: 87 → 115 tests, all passing** (`Ran 115 tests in 18.210s / OK`). The 28 new tests cover the retry
and backoff behaviour (429, 5xx, `Retry-After`, the body hint, giving up), the per-category trimming (the right
tools offered, the wrong ones refused, the refusal logged), the safety net (a narrating model still reaches the
rule engine's verdict) and the whole HTTP surface.

---

## Verification notes

- Test suite: **87 tests, all passing** (`Ran 87 tests in 3.487s / OK`). Was 27 before this work; the 60 new tests cover the classifier routing, the localized output, the model id, the handoff queue, the engine tracking, the conversation memory, the auto-resolution rate, the read-path gate and the action caps.
- Groq engine exercised live with the key from `.env` against `openai/gpt-oss-120b`: `app.py --check-groq` reports the key, the model and tool calling all work; settlement retrigger, bank ticket, dispute escalation and the Hindi reply all produced correct actions and correct status.
- Rule-based engine verified separately (the `.env` key makes Groq the default): "my settlement is not working" now retriggers, a refund request escalates with `refund` code, and the Hindi/Tamil replies carry `कल (11 नव)` / `நாளை (11 நव)` with no English date left.
- HTTP smoke test: `/api/merchants` (8 rows), `/api/metrics`, `/api/tickets`, `/api/escalations` all responded correctly.
- Engine tracking verified against a live server: `by_engine {"rules": 4, "groq": 1}`, `models {"openai/gpt-oss-120b": 1}`, `groq_fallbacks 0`; a deliberate Groq failure produced `engine="rules"` with `fallback=1`. The Outcomes panel was screenshotted in the real page showing the split, the model note, the engine badges on the tickets table and the handoff cards.
- Migration verified against an old-shape `tickets` table: the three new columns are added with `ALTER TABLE` and the existing row survives.
- Conversation memory verified against a live server with a seeded two-turn conversation: M001 "My settlement has not arrived" (settlement, resolved) followed by "yes please do that" (settlement again, **inherited**, resolved — previously `needs_info` / `unknown`); M006 "chargeback raised on my account" (escalated) followed by "any update on this?" (chargeback, **inherited**, escalated — **no second case**, the handoff queue still held one case). The UI was screenshotted showing the queue and the tickets table with the inherited turns.
- Auto-resolution rate verified against a live server: one resolved, one escalated and two clarifications reported `total 4, resolved 1, escalated 1, needs_info 2, decided 2, rate 50%` — the old denominator would have said 25%. The Outcomes panel was screenshotted showing the "Auto-resolved 1 (50%)" tile and the note stating that the clarification is not counted in the rate.
- Read-path gate verified live: the ungated tool still returns M007's settlement (`S1007`, Rs 9,800, failed), which is the leak the gate now stops; `Agent.call("get_settlement_status", merchant_id="M007")` from M001 raises `PolicyDenied` and logs `BLOCKED get_settlement_status: read access is limited to the merchant in this chat`, while M001's own read still returns `S1001`. Both engines were exercised against the live key: rules resolved the settlement, restarted the device and escalated the chargeback; Groq read the settlement through the gate (`allowed get_settlement_status: read-only lookup for this merchant`) and ordered the replacement, with no fallbacks.
- Action caps verified live: M002 asked three times in Hindi and got two bank tickets (`bank_ticket_count` 2), then the third turn escalated with the Hindi reason *"हमारे नियमों के अनुसार जितने बैंक टिकट बन सकते थे, हम बना चुके हैं"*; M003's device dropped off the network three times, so the third restart escalated with *"we have already restarted the device as many times as our rules allow"* and the fourth turn reused the same case (ESC-6) instead of opening a second one. The handoff case files carried `bank_ticket_count: 2` and `restart_count: 2` for the human agent.
- Scaffolding verified: `.env.example` now exists with the two keys `app.py` reads, `pyproject.toml` describes the project and asks for Python 3.9+, `*.db` is ignored by git, `main.py` is gone, and the README documents the Windows invocation. `git status` shows the local demo database no longer tracked.
- **Note on the environment:** a `GROQ_MODEL=llama-3.3-70b-versatile` environment variable was set in the shell session, and environment variables win over `.env`. It was cleared for the verification runs. If the app still reports the old model, check `echo $env:GROQ_MODEL`.
- **Note on a stale server:** a server started at 16:57 from `C:\Users\ifels\Downloads\app.py` was still holding port 8000 and serving pre-fix code (it 404s on `/api/escalations`). It has since been stopped and replaced with a server started from `E:\hackathon`.
- Probe scripts used for the behaviour checks were temporary and have been removed; the working tree contains only the project files.

### Round-two verification

- Test suite: **115 tests, all passing** (`Ran 115 tests in 18.210s / OK`). Was 87 after round one.
- Retry verified with a stubbed transport: a 429 is retried and eventually succeeds; a 500 is retried; a
  `Retry-After: 7` header is honoured ahead of the fixed backoff; the "try again in 5.8725s" hint in Groq's body is
  parsed and used; a request that keeps failing gives up after `GROQ_ATTEMPTS` and falls back to the rule engine
  with `engine="rules"` and `fallback=1`.
- Tool trimming measured against the live prompt: 672 tokens with all 9 tools, 517 with `tools_for_category()` —
  a 21% reduction. The unoffered-tool refusal was verified in code and in the audit log.
- Safety net verified by forcing the model to narrate a chargeback without calling a tool: the rule engine's verdict
  decided, the dispute above the limit still escalated to a human with a case file, and the merchant's reply matched
  what happened.
- HTTP layer verified against a live `ThreadingHTTPServer` on a random port: every endpoint answers, and the error
  paths (empty message, malformed JSON, non-UTF-8 body, 404, reset) all behave.
- CI: `.github/workflows/tests.yml` runs the suite and a server smoke test on Python 3.9, 3.12 and 3.13.
- The demo server was restarted from `E:\hackathon` (the earlier one was serving stale code from a different
  directory) and the demo beats were run live against it. `/api/reset` leaves the database pristine: 0 tickets,
  0 escalations.
- **Note on the environment:** a `GROQ_MODEL=llama-3.3-70b-versatile` environment variable and a shell-level
  `GROQ_API_KEY` were both set, and both override `.env`. They were cleared for the verification runs. If the app
  reports the old model, check `echo $env:GROQ_MODEL`.
