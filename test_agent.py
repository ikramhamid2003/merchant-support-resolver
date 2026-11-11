"""Tests for the Merchant Support Resolver. Run: python test_agent.py"""
import os
import tempfile
import json
import unittest
import urllib.error
import email.message
import io
import threading
from http.server import ThreadingHTTPServer
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["MSR_DB"] = os.path.join(_tmp, "test.db")
os.environ["GROQ_API_KEY"] = ""   # ...and never call the real Groq API

import app  # noqa: E402


class AgentTests(unittest.TestCase):
    def setUp(self):
        app.init_db(reset=True)

    def run_case(self, mid, text):
        return app.process_message(mid, text)

    def test_failed_settlement_is_retriggered(self):
        r = self.run_case("M001", "My settlement has not arrived")
        self.assertEqual(r["status"], "resolved")
        self.assertIn("retrigger_settlement", r["actions"])
        # idempotent: asking again takes no new action
        r2 = self.run_case("M001", "My settlement has not arrived")
        self.assertEqual(r2["actions"], [])

    def test_pending_at_bank_raises_bank_ticket(self):
        r = self.run_case("M002", "मेरा सेटलमेंट नहीं आया")
        self.assertEqual(r["language"], "hi")
        self.assertIn("raise_bank_ticket", r["actions"])
        self.assertIn("बैंक", r["reply"])

    def test_offline_device_gets_remote_restart(self):
        r = self.run_case("M003", "Soundbox is not working")
        self.assertIn("remote_restart_device", r["actions"])

    def test_faulty_device_gets_replacement(self):
        r = self.run_case("M004", "QR is not working")
        self.assertIn("request_replacement", r["actions"])

    def test_small_dispute_is_auto_handled(self):
        r = self.run_case("M005", "A chargeback was raised")
        self.assertIn("submit_dispute_evidence", r["actions"])
        self.assertEqual(r["status"], "resolved")

    def test_large_dispute_is_escalated(self):
        r = self.run_case("M006", "chargeback raised on my account")
        self.assertEqual(r["status"], "escalated")
        self.assertNotIn("submit_dispute_evidence", r["actions"])

    def test_account_mismatch_is_escalated_not_retried(self):
        r = self.run_case("M007", "settlement not received")
        self.assertEqual(r["status"], "escalated")
        self.assertNotIn("retrigger_settlement", r["actions"])

    def test_healthy_merchant_needs_no_action(self):
        r = self.run_case("M008", "where is my settlement")
        self.assertEqual((r["status"], r["actions"]), ("resolved", []))

    def test_tamil_script_and_romanised_tamil(self):
        r = self.run_case("M001", "என் செட்டில்மென்ட் வரவில்லை")
        self.assertEqual(r["language"], "ta")
        r = self.run_case("M003", "enakku panam varavillai")
        self.assertEqual(r["language"], "ta")

    def test_unknown_message_asks_for_clarification(self):
        r = self.run_case("M008", "hello there")
        self.assertEqual(r["status"], "needs_info")

    def test_audit_log_and_metrics(self):
        self.run_case("M001", "settlement not received")
        self.run_case("M006", "chargeback")
        m = app.metrics()
        self.assertEqual((m["total"], m["resolved"], m["escalated"]), (2, 1, 1))
        db = app.conn_db()
        n = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        db.close()
        self.assertGreater(n, 4)


class ClassifierTests(unittest.TestCase):
    """A generic phrase must not hijack a settlement request, and a refund must never be
    mistaken for a settlement that can be auto-retriggered."""

    def test_settlement_wording_is_not_routed_to_device(self):
        for text in ("my settlement is not working", "my money is not working"):
            self.assertEqual(app.classify_keywords(text), "settlement", text)

    def test_device_wording_still_routes_to_device(self):
        for text in ("QR is not working", "Soundbox is not working", "my speaker is dead"):
            self.assertEqual(app.classify_keywords(text), "device", text)

    def test_refund_is_its_own_category(self):
        self.assertEqual(app.classify_keywords("I want a refund of my money"), "refund")
        self.assertEqual(app.classify_keywords("please refund my payment"), "refund")

    def test_chargeback_still_wins(self):
        self.assertEqual(app.classify_keywords("A chargeback was raised on my account"), "chargeback")

    def test_unrelated_message_has_no_category(self):
        self.assertIsNone(app.classify_keywords("hello there"))

    def test_settlement_complaint_actually_retriggers(self):
        app.init_db(reset=True)
        r = app.process_message("M001", "my settlement is not working")
        self.assertEqual(r["category"], "settlement")
        self.assertIn("retrigger_settlement", r["actions"])

    def test_refund_request_is_escalated_not_retriggered(self):
        app.init_db(reset=True)
        r = app.process_message("M001", "I want a refund of my money")
        self.assertEqual(r["category"], "refund")
        self.assertEqual(r["status"], "escalated")
        self.assertNotIn("retrigger_settlement", r["actions"])
        self.assertIn("escalate_to_human", r["actions"])


class LocalisationTests(unittest.TestCase):
    """Dates, money and the escalation reason must come back in the merchant's language."""

    def setUp(self):
        app.init_db(reset=True)

    def test_dates_are_localised(self):
        tomorrow = app.day(1)
        self.assertTrue(app.fmt_date(tomorrow, "en").startswith("tomorrow"))
        self.assertTrue(app.fmt_date(tomorrow, "hi").startswith("कल"))
        self.assertTrue(app.fmt_date(tomorrow, "ta").startswith("நாளை"))
        # the three languages never share a date string
        self.assertNotEqual(app.fmt_date(tomorrow, "en"), app.fmt_date(tomorrow, "hi"))

    def test_hindi_reply_has_no_english_date(self):
        r = app.process_message("M002", "मेरा सेटलमेंट नहीं आया")
        self.assertEqual(r["language"], "hi")
        self.assertNotIn("tomorrow", r["reply"])
        self.assertNotIn("Nov", r["reply"])

    def test_tamil_reply_has_no_english_date(self):
        r = app.process_message("M001", "என் செட்டில்மென்ட் வரவில்லை")
        self.assertEqual(r["language"], "ta")
        self.assertNotIn("tomorrow", r["reply"])
        self.assertNotIn("Nov", r["reply"])

    def test_money_uses_indian_digit_grouping(self):
        self.assertEqual(app.money(184500), "1,84,500")
        self.assertEqual(app.money(3200), "3,200")
        self.assertEqual(app.money(45000), "45,000")

    def test_escalation_reason_is_localised(self):
        r = app.process_message("M006", "chargeback raised on my account")
        self.assertEqual(r["status"], "escalated")
        self.assertIn("chargeback is above", r["reply"])
        # the internal English reason stays in the audit trail for the human agent
        q = app.list_escalations()
        self.assertTrue(any("auto-handling limit" in e["reason"] for e in q))

    def test_escalation_reason_in_hindi(self):
        r = app.process_message("M006", "मेरे खाते पर चार्जबैक लगा है")
        self.assertEqual(r["language"], "hi")
        self.assertEqual(r["status"], "escalated")
        self.assertIn("कारण:", r["reply"])
        self.assertNotIn("chargeback is above", r["reply"])


class EngineTrackingTests(unittest.TestCase):
    """Each conversation records which engine answered it, so rules and Groq can be compared."""

    def setUp(self):
        app.init_db(reset=True)

    def groq_script(self):
        """A Groq reply that calls one tool and then answers."""
        def chat(messages, tools=None):
            if not any(m.get("role") == "tool" for m in messages):
                return tool_call("get_settlement_status")
            return {"content": "Your settlement is on schedule."}
        return chat

    def test_rule_based_ticket_records_the_rules_engine(self):
        r = app.process_message("M001", "settlement not received", use_groq=False)
        self.assertEqual(r["engine"], "rules")
        self.assertEqual(r["model"], "")
        self.assertEqual(r["fallback"], 0)

    def test_groq_ticket_records_the_model(self):
        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", self.groq_script()):
            r = app.process_message("M001", "settlement not received", use_groq=True)
        self.assertEqual(r["engine"], "groq")
        self.assertEqual(r["model"], app.GROQ_MODEL)
        self.assertEqual(r["fallback"], 0)

    def test_failed_groq_is_recorded_as_a_fallback_not_as_groq(self):
        def boom(messages, tools=None):
            raise urllib.error.URLError("offline")
        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", boom):
            r = app.process_message("M001", "settlement not received", use_groq=True)
        self.assertEqual(r["engine"], "rules")
        self.assertEqual(r["fallback"], 1)

    def test_engine_is_persisted_on_the_ticket(self):
        app.process_message("M001", "settlement not received", use_groq=False)
        t = app.list_tickets()[0]
        self.assertEqual(t["engine"], "rules")

    def test_metrics_split_the_two_engines(self):
        app.process_message("M001", "settlement not received", use_groq=False)
        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", self.groq_script()):
            app.process_message("M002", "settlement not received", use_groq=True)
        m = app.metrics()
        self.assertEqual(m["by_engine"], {"rules": 1, "groq": 1})
        self.assertIn("rules", m["avg_ms_by_engine"])
        self.assertIn("groq", m["avg_ms_by_engine"])
        self.assertEqual(m["groq_fallbacks"], 0)

    def test_metrics_count_fallbacks_separately(self):
        def boom(messages, tools=None):
            raise urllib.error.URLError("offline")
        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", boom):
            app.process_message("M001", "settlement not received", use_groq=True)
        m = app.metrics()
        self.assertEqual(m["groq_fallbacks"], 1)
        self.assertEqual(m["by_engine"].get("groq", 0), 0)

    def test_escalation_case_records_the_engine(self):
        app.process_message("M006", "chargeback raised on my account", use_groq=False)
        e = app.list_escalations()[0]
        self.assertEqual(e["engine"], "rules")

    def test_existing_database_gains_the_new_columns_without_losing_data(self):
        """An older demo database is migrated in place, not reset."""
        db = app.conn_db()
        db.execute("DROP TABLE tickets")
        db.execute("CREATE TABLE tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id TEXT, message TEXT,"
                   " language TEXT, category TEXT, status TEXT, reply TEXT, actions TEXT, created_at TEXT,"
                   " duration_ms INTEGER)")
        db.execute("INSERT INTO tickets(id,merchant_id,message,status) VALUES(99,'M001','old row','resolved')")
        db.commit()
        db.close()
        app.init_db(reset=False)
        db = app.conn_db()
        cols = [r[1] for r in db.execute("PRAGMA table_info(tickets)")]
        row = db.execute("SELECT * FROM tickets WHERE id=99").fetchone()
        db.close()
        self.assertIn("engine", cols)
        self.assertIn("model", cols)
        self.assertIn("fallback", cols)
        self.assertEqual(row["message"], "old row")


class ConversationMemoryTests(unittest.TestCase):
    """A follow-up message keeps the context of the earlier turns."""

    def setUp(self):
        app.init_db(reset=True)

    def test_follow_up_inherits_the_previous_category(self):
        app.process_message("M001", "My settlement has not arrived", use_groq=False)
        r = app.process_message("M001", "yes please do that", use_groq=False)
        self.assertEqual(r["category"], "settlement")
        self.assertEqual(r["status"], "resolved")
        self.assertTrue(any("Follow-up" in t["title"] for t in r["trace"]))

    def test_an_unrelated_message_does_not_inherit(self):
        app.process_message("M001", "My settlement has not arrived", use_groq=False)
        r = app.process_message("M001", "what is the weather today", use_groq=False)
        self.assertEqual(r["category"], "unknown")
        self.assertEqual(r["status"], "needs_info")

    def test_follow_up_on_an_open_case_does_not_open_a_second_one(self):
        app.process_message("M006", "chargeback raised on my account", use_groq=False)
        r = app.process_message("M006", "any update on this?", use_groq=False)
        self.assertEqual(r["status"], "escalated")
        self.assertEqual(r["actions"], [])            # no new case was raised
        self.assertIn("already with our support team", r["reply"])
        self.assertEqual(len(app.list_escalations()), 1)

    def test_a_different_case_still_gets_its_own_escalation(self):
        app.process_message("M006", "chargeback raised on my account", use_groq=False)
        app.process_message("M001", "I want a refund of my money", use_groq=False)
        self.assertEqual(len(app.list_escalations()), 2)

    def test_history_is_passed_to_the_model(self):
        app.process_message("M001", "My settlement has not arrived", use_groq=False)
        seen = []

        def chat(messages, tools=None):
            seen.append(messages)
            return {"content": "ok"}

        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", chat):
            app.process_message("M001", "and what about the refund?", use_groq=True)
        roles = [m["role"] for m in seen[-1]]
        self.assertIn("assistant", roles)              # the earlier reply is in the context
        self.assertEqual(roles[-1], "user")
        self.assertIn("My settlement has not arrived", seen[-1][1]["content"])

    def test_resolve_category_helper(self):
        history = [{"category": "settlement"}, {"category": "unknown"}]
        self.assertEqual(app.resolve_category("my settlement is late", history), ("settlement", False))
        self.assertEqual(app.resolve_category("yes please do that", history), ("settlement", True))
        self.assertEqual(app.resolve_category("what is the weather", history), (None, False))

    def test_conversation_returns_the_earlier_turns(self):
        app.process_message("M001", "My settlement has not arrived", use_groq=False)
        app.process_message("M001", "still not credited", use_groq=False)
        db = app.conn_db()
        turns = app.conversation(db, "M001")
        db.close()
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["message"], "My settlement has not arrived")
        self.assertEqual(turns[1]["message"], "still not credited")

    def test_conversation_is_scoped_to_the_merchant(self):
        app.process_message("M001", "My settlement has not arrived", use_groq=False)
        db = app.conn_db()
        self.assertEqual(app.conversation(db, "M008"), [])
        db.close()


class AutoResolutionRateTests(unittest.TestCase):
    """The auto-resolution rate must measure the cases the agent actually decided.
    A message it could not classify is a clarification request, not a resolution failure."""

    def setUp(self):
        app.init_db(reset=True)

    def run_case(self, mid, text):
        return app.process_message(mid, text, use_groq=False)

    def test_needs_info_is_excluded_from_the_denominator(self):
        """One resolved + one escalated + one needs_info used to report 33%.
        It is 50%: asking the merchant to clarify is not a case the agent could have won."""
        self.run_case("M001", "settlement not received")   # resolved
        self.run_case("M006", "chargeback")                # escalated
        self.run_case("M008", "hello there")               # needs_info
        m = app.metrics()
        self.assertEqual((m["total"], m["resolved"], m["escalated"], m["needs_info"]), (3, 1, 1, 1))
        self.assertEqual(m["decided"], 2)
        self.assertEqual(m["auto_resolution_rate"], 50)
        self.assertNotEqual(m["auto_resolution_rate"], 33)   # the old, wrong denominator

    def test_escalated_cases_stay_in_the_denominator(self):
        """Handing a case to a human is not an auto-resolution, so it still counts against the rate."""
        self.run_case("M001", "settlement not received")
        self.run_case("M006", "chargeback")
        m = app.metrics()
        self.assertEqual(m["decided"], 2)
        self.assertEqual(m["auto_resolution_rate"], 50)

    def test_clarifications_do_not_change_the_rate(self):
        self.run_case("M001", "settlement not received")
        before = app.metrics()["auto_resolution_rate"]
        self.run_case("M008", "hello there")
        self.run_case("M007", "what is this")
        after = app.metrics()
        self.assertEqual(after["auto_resolution_rate"], before)
        self.assertEqual((after["total"], after["needs_info"], after["decided"]), (3, 2, 1))

    def test_rate_is_zero_when_only_clarifications_happened(self):
        self.run_case("M008", "hello there")
        m = app.metrics()
        self.assertEqual(m["decided"], 0)
        self.assertEqual(m["auto_resolution_rate"], 0)

    def test_rate_is_zero_with_no_tickets(self):
        m = app.metrics()
        self.assertEqual((m["total"], m["decided"], m["auto_resolution_rate"]), (0, 0, 0))

    def test_everything_resolved_is_a_hundred_percent(self):
        self.run_case("M001", "settlement not received")
        self.run_case("M005", "chargeback")
        m = app.metrics()
        self.assertEqual((m["resolved"], m["decided"]), (2, 2))
        self.assertEqual(m["auto_resolution_rate"], 100)


class ModelConfigTests(unittest.TestCase):
    """The default Groq model must be one that still exists on Groq."""

    def test_default_model_is_not_a_retired_one(self):
        """The model id baked into the code must be one Groq still serves. A GROQ_MODEL
        environment variable may override it at runtime, so check the default in the source."""
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8") as f:
            source = f.read()
        default = source.split('os.environ.get("GROQ_MODEL", "')[1].split('"')[0]
        self.assertEqual(default, "openai/gpt-oss-120b")
        self.assertNotIn("llama-3.3-70b-versatile", default)

    def test_env_file_points_at_the_live_model(self):
        env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env):
            with open(env, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("GROQ_MODEL="):
                        self.assertEqual(line.split("=", 1)[1].strip(), "openai/gpt-oss-120b")


def tool_call(name, **args):
    return {"tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class HandoffQueueTests(unittest.TestCase):
    def setUp(self):
        app.init_db(reset=True)

    def test_escalations_listed_with_case_file(self):
        app.process_message("M006", "chargeback raised on my account")
        app.process_message("M007", "settlement not received")
        app.process_message("M001", "My settlement has not arrived")   # resolved, must not appear
        q = app.list_escalations()
        self.assertEqual({e["merchant_id"] for e in q}, {"M006", "M007"})
        e6 = next(e for e in q if e["merchant_id"] == "M006")
        self.assertEqual(e6["case_file"]["dispute"]["amount"], 25000)
        self.assertTrue(e6["case"].startswith("ESC-"))

    def test_escalation_carries_what_the_merchant_was_told(self):
        """The handoff panel shows the human agent the reply, the language and the ticket."""
        app.process_message("M006", "chargeback raised on my account")
        e = app.list_escalations()[0]
        self.assertEqual(e["language"], "en")
        self.assertIn("support team", e["reply"])
        self.assertTrue(e["ticket_id"])
        self.assertIn("chargeback", e["message"])

    def test_escalation_reply_is_in_the_merchants_language(self):
        r = app.process_message("M006", "मेरे खाते पर चार्जबैक लगा है")
        self.assertEqual(r["language"], "hi")
        e = app.list_escalations()[0]
        self.assertEqual(e["language"], "hi")
        self.assertIn("कारण", e["reply"])

    def test_handoff_queue_is_empty_when_nothing_escalated(self):
        app.process_message("M001", "My settlement has not arrived")
        self.assertEqual(app.list_escalations(), [])


class GroqTests(unittest.TestCase):
    """Groq is mocked: a scripted list of model replies. Proves the guardrails hold even if the model misbehaves."""

    def setUp(self):
        app.init_db(reset=True)
        self.seen = []                     # every message list sent to the (fake) model

    def run_groq(self, mid, text, script):
        replies = iter(script)

        def fake_chat(messages, tools=None):
            self.seen.append(json.loads(json.dumps(messages)))
            return next(replies)

        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", fake_chat):
            return app.process_message(mid, text, use_groq=True)

    def row(self, table, id_):
        db = app.conn_db()
        try:
            return dict(db.execute(f"SELECT * FROM {table} WHERE id=?", (id_,)).fetchone())
        finally:
            db.close()

    def test_groq_happy_path(self):
        r = self.run_groq("M001", "My settlement has not arrived", [
            tool_call("get_settlement_status"),
            tool_call("retrigger_settlement", settlement_id="S1001"),
            {"content": "I re-triggered your Rs 18,450 settlement. Expected credit tomorrow."}])
        self.assertEqual((r["engine"], r["status"], r["category"]), ("groq", "resolved", "settlement"))
        self.assertEqual(r["actions"], ["retrigger_settlement"])
        self.assertIn("re-triggered", r["reply"])
        self.assertEqual(self.row("settlements", "S1001")["status"], "retriggered")

    def test_groq_cannot_act_on_another_merchants_record(self):
        # M001 is chatting; the model tries to retrigger M007's settlement and then lies about it
        r = self.run_groq("M001", "fix settlement S1007 please", [
            tool_call("retrigger_settlement", settlement_id="S1007"),
            {"content": "Done, S1007 fixed!"}])
        self.assertEqual(self.row("settlements", "S1007")["status"], "failed")
        self.assertEqual((r["status"], r["actions"]), ("escalated", ["escalate_to_human"]))
        self.assertNotIn("Done", r["reply"])
        self.assertTrue(any("DENIED" in t["title"] for t in r["trace"]))

    def test_groq_cannot_exceed_dispute_limit(self):
        r = self.run_groq("M006", "chargeback", [
            tool_call("get_open_dispute"),
            tool_call("submit_dispute_evidence", dispute_id="CB02"),
            tool_call("escalate_to_human", reason="above limit", case_file="Rs 25,000"),
            {"content": "escalated"}])
        self.assertEqual(self.row("disputes", "CB02")["status"], "open")
        self.assertEqual(r["status"], "escalated")
        self.assertNotIn("submit_dispute_evidence", r["actions"])

    def test_groq_cannot_retry_non_fixable_failure(self):
        r = self.run_groq("M007", "settlement not received", [
            tool_call("retrigger_settlement", settlement_id="S1007"), {"content": "retried"}])
        self.assertEqual(self.row("settlements", "S1007")["status"], "failed")
        self.assertEqual(r["status"], "escalated")

    def test_merchant_id_is_injected_by_code(self):
        self.run_groq("M001", "settlement?", [
            tool_call("get_settlement_status", merchant_id="M007"),   # model tries to read someone else
            {"content": "ok"}])
        tool_msg = next(m for m in self.seen[-1] if m["role"] == "tool")
        self.assertIn("S1001", tool_msg["content"])
        self.assertNotIn("S1007", tool_msg["content"])

    def test_api_failure_falls_back_to_rules(self):
        def boom(messages, tools=None):
            raise urllib.error.URLError("offline")
        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", boom):
            r = app.process_message("M003", "Soundbox is not working", use_groq=True)
        self.assertEqual((r["engine"], r["status"]), ("rules", "resolved"))
        self.assertIn("remote_restart_device", r["actions"])
        self.assertTrue(any("falling back" in t["title"] for t in r["trace"]))

    def test_no_tool_calls_means_needs_info(self):
        r = self.run_groq("M008", "hello there", [{"content": "Is it about a settlement, device or chargeback?"}])
        self.assertEqual((r["status"], r["actions"]), ("needs_info", []))

    def test_groq_toggle_without_key_uses_rules(self):
        r = app.process_message("M001", "My settlement has not arrived", use_groq=True)
        self.assertEqual((r["engine"], r["status"]), ("rules", "resolved"))


class CheckGroqTests(unittest.TestCase):
    def test_no_key_fails_clearly(self):
        with mock.patch.object(app, "GROQ_API_KEY", ""):
            self.assertEqual(app.check_groq(), 1)

    def test_working_key_with_tool_call_passes(self):
        with mock.patch.object(app, "GROQ_API_KEY", "gsk_abcdefgh1234"), \
                mock.patch.object(app, "groq_chat", lambda m, tools=None: tool_call("get_settlement_status")):
            self.assertEqual(app.check_groq(), 0)

    def test_bad_key_reports_failure(self):
        def bad(messages, tools=None):
            raise RuntimeError("Groq API HTTP 401: invalid key")
        with mock.patch.object(app, "GROQ_API_KEY", "gsk_abcdefgh1234"), mock.patch.object(app, "groq_chat", bad):
            self.assertEqual(app.check_groq(), 1)


class PolicyGateTests(unittest.TestCase):
    def setUp(self):
        app.init_db(reset=True)
        self.db = app.conn_db()

    def tearDown(self):
        self.db.close()

    def test_retrigger_limit(self):
        self.db.execute("UPDATE settlements SET retrigger_count=2 WHERE id='S1001'")
        ok, why = app.policy_gate(self.db, "M001", "retrigger_settlement", {"settlement_id": "S1001"})
        self.assertFalse(ok)
        self.assertIn("limit", why)

    def test_wrong_merchant_is_denied(self):
        ok, _ = app.policy_gate(self.db, "M001", "request_replacement", {"device_id": "D04"})
        self.assertFalse(ok)
        ok, _ = app.policy_gate(self.db, "M004", "request_replacement", {"device_id": "D04"})
        self.assertTrue(ok)

    def test_unknown_action_is_denied(self):
        ok, _ = app.policy_gate(self.db, "M001", "refund_everything", {})
        self.assertFalse(ok)

    def test_rule_based_path_cannot_bypass_gate(self):
        ag = app.Agent(self.db, 1, "M001")
        with self.assertRaises(app.PolicyDenied):
            ag.call("retrigger_settlement", settlement_id="S1007")


class ReadGateTests(unittest.TestCase):
    """A read tool must not return another merchant's records, however the call was made.
    The gate already refuses a cross-merchant read; Agent.call() has to ask it."""

    def setUp(self):
        app.init_db(reset=True)
        self.db = app.conn_db()

    def tearDown(self):
        self.db.close()

    def test_read_tool_cannot_read_another_merchants_data(self):
        ag = app.Agent(self.db, 1, "M001")
        with self.assertRaises(app.PolicyDenied):
            ag.call("get_settlement_status", merchant_id="M007")

    def test_device_read_is_gated(self):
        ag = app.Agent(self.db, 1, "M001")
        with self.assertRaises(app.PolicyDenied):
            ag.call("get_device_status", merchant_id="M006")

    def test_dispute_read_is_gated(self):
        ag = app.Agent(self.db, 1, "M001")
        with self.assertRaises(app.PolicyDenied):
            ag.call("get_open_dispute", merchant_id="M005")

    def test_read_without_a_merchant_is_denied(self):
        """A read with no merchant named returns nobody's data."""
        ag = app.Agent(self.db, 1, "M001")
        with self.assertRaises(app.PolicyDenied):
            ag.call("get_settlement_status")

    def test_read_without_a_merchant_context_is_denied(self):
        """An agent with no merchant at all may not read."""
        ag = app.Agent(self.db, 1, None)
        with self.assertRaises(app.PolicyDenied):
            ag.call("get_settlement_status", merchant_id="M001")

    def test_the_merchants_own_read_still_works(self):
        ag = app.Agent(self.db, 1, "M001")
        row = ag.call("get_settlement_status", merchant_id="M001")
        self.assertEqual(row["merchant_id"], "M001")

    def test_denied_read_is_written_to_the_audit_log(self):
        ag = app.Agent(self.db, 1, "M001")
        with self.assertRaises(app.PolicyDenied):
            ag.call("get_settlement_status", merchant_id="M007")
        self.assertTrue(any(t["kind"] == "policy" and "BLOCKED" in t["title"] for t in ag.trace))
        n = self.db.execute("SELECT COUNT(*) FROM audit_log WHERE kind='policy'").fetchone()[0]
        self.assertGreater(n, 0)

    def test_the_normal_flows_still_read_their_own_data(self):
        """The rule engine reads with the merchant's own id, so nothing is blocked in use."""
        r = app.process_message("M001", "My settlement has not arrived", use_groq=False)
        self.assertEqual(r["status"], "resolved")
        r = app.process_message("M006", "chargeback raised on my account", use_groq=False)
        self.assertEqual(r["status"], "escalated")


class PolicyCapsTests(unittest.TestCase):
    """Bank tickets and device restarts are capped, exactly like settlement retriggers.
    Hitting the cap is not a crash: the case goes to a human."""

    def setUp(self):
        app.init_db(reset=True)
        self.db = app.conn_db()

    def tearDown(self):
        self.db.close()

    def test_the_limits_are_declared(self):
        """The caps live in POLICY, so nobody can quietly drop one."""
        self.assertIn("max_bank_tickets", app.POLICY)
        self.assertIn("max_device_restarts", app.POLICY)
        self.assertGreater(app.POLICY["max_bank_tickets"], 0)
        self.assertGreater(app.POLICY["max_device_restarts"], 0)

    def test_the_first_bank_ticket_is_allowed(self):
        ok, _ = app.policy_gate(self.db, "M002", "raise_bank_ticket", {"settlement_id": "S1002"})
        self.assertTrue(ok)

    def test_bank_tickets_stop_at_the_cap(self):
        ag = app.Agent(self.db, 1, "M002")
        for _ in range(app.POLICY["max_bank_tickets"]):
            ag.call("raise_bank_ticket", settlement_id="S1002")
        ok, why = app.policy_gate(self.db, "M002", "raise_bank_ticket", {"settlement_id": "S1002"})
        self.assertFalse(ok)
        self.assertIn("limit", why)

    def test_the_bank_ticket_counter_counts_the_tickets(self):
        ag = app.Agent(self.db, 1, "M002")
        ag.call("raise_bank_ticket", settlement_id="S1002")
        ag.call("raise_bank_ticket", settlement_id="S1002")
        n = self.db.execute("SELECT bank_ticket_count FROM settlements WHERE id='S1002'").fetchone()[0]
        self.assertEqual(n, 2)

    def test_device_restarts_stop_at_the_cap(self):
        ag = app.Agent(self.db, 1, "M003")
        for _ in range(app.POLICY["max_device_restarts"]):
            self.db.execute("UPDATE devices SET status='offline', issue='network' WHERE id='D03'")
            ag.call("remote_restart_device", device_id="D03")
        ok, why = app.policy_gate(self.db, "M003", "remote_restart_device", {"device_id": "D03"})
        self.assertFalse(ok)
        self.assertIn("limit", why)

    def test_the_device_restart_counter_counts_the_restarts(self):
        ag = app.Agent(self.db, 1, "M003")
        self.db.execute("UPDATE devices SET status='offline', issue='network' WHERE id='D03'")
        ag.call("remote_restart_device", device_id="D03")
        n = self.db.execute("SELECT restart_count FROM devices WHERE id='D03'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_the_normal_flows_are_unaffected(self):
        """The first ticket and the first restart still happen, as before."""
        r = app.process_message("M002", "settlement not credited yet", use_groq=False)
        self.assertEqual(r["status"], "resolved")
        self.assertIn("raise_bank_ticket", r["actions"])
        r = app.process_message("M003", "soundbox is not working", use_groq=False)
        self.assertEqual(r["status"], "resolved")
        self.assertIn("remote_restart_device", r["actions"])

    def test_hitting_the_bank_ticket_cap_escalates_to_a_human(self):
        """A refusal must not crash the turn: the case is handed over instead."""
        self.db.execute("UPDATE settlements SET bank_ticket_count=2 WHERE id='S1002'")
        self.db.commit()
        r = app.process_message("M002", "settlement not credited yet", use_groq=False)
        self.assertEqual(r["status"], "escalated")
        self.assertIn("escalate_to_human", r["actions"])

    def test_hitting_the_device_restart_cap_escalates_to_a_human(self):
        self.db.execute("UPDATE devices SET restart_count=2 WHERE id='D03'")
        self.db.commit()
        r = app.process_message("M003", "soundbox is not working", use_groq=False)
        self.assertEqual(r["status"], "escalated")
        self.assertIn("escalate_to_human", r["actions"])

    def test_the_cap_keeps_counting_across_turns(self):
        """Two turns raise two tickets; the third turn is refused and escalated."""
        app.process_message("M002", "settlement not credited yet", use_groq=False)
        app.process_message("M002", "any update on this?", use_groq=False)
        n = self.db.execute("SELECT bank_ticket_count FROM settlements WHERE id='S1002'").fetchone()[0]
        self.assertEqual(n, 2)
        r = app.process_message("M002", "still waiting for the money", use_groq=False)
        self.assertEqual(r["status"], "escalated")

    def test_the_cap_reaches_the_audit_log(self):
        self.db.execute("UPDATE settlements SET bank_ticket_count=2 WHERE id='S1002'")
        self.db.commit()
        r = app.process_message("M002", "settlement not credited yet", use_groq=False)
        self.assertTrue(any("BLOCKED" in t["title"] for t in r["trace"]))

    def test_an_old_database_gains_the_counters(self):
        """The counters are added in place, so an existing demo database keeps its data."""
        db = app.conn_db()
        db.execute("DROP TABLE settlements")
        db.execute("CREATE TABLE settlements(id TEXT PRIMARY KEY, merchant_id TEXT, amount REAL, status TEXT,"
                   " utr TEXT, expected_date TEXT, failure_reason TEXT, retrigger_count INTEGER DEFAULT 0,"
                   " bank_ticket TEXT)")
        db.execute("INSERT INTO settlements VALUES('S1002','M002',32100,'pending_at_bank','UTR1','2026-11-11',"
                   "NULL,0,NULL)")
        db.commit()
        db.close()
        app.init_db(reset=False)
        db = app.conn_db()
        cols = [r[1] for r in db.execute("PRAGMA table_info(settlements)")]
        row = db.execute("SELECT * FROM settlements WHERE id='S1002'").fetchone()
        db.close()
        self.assertIn("bank_ticket_count", cols)
        self.assertEqual(row["merchant_id"], "M002")
        self.assertEqual(row["bank_ticket_count"], 0)


class FakeResponse:
    """Stands in for the object urllib.request.urlopen returns."""

    def __init__(self, obj):
        self.raw = json.dumps(obj).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.raw


class GroqRetryTests(unittest.TestCase):
    """A rate limit is the API saying "not now", not "no". It is retried, not treated as a failure."""

    def setUp(self):
        app.init_db(reset=True)

    def http_error(self, code, body=b'{"error":{"message":"nope"}}', headers=None):
        hdrs = email.message.Message()
        for k, v in (headers or {}).items():
            hdrs[k] = v
        return urllib.error.HTTPError("https://api.groq.com", code, "err", hdrs, io.BytesIO(body))

    def test_a_429_is_retried_and_then_succeeds(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if len(calls) < 3:
                raise self.http_error(429, b'{"error":{"message":"Rate limit reached. Please try again in 5.8725s."}}')
            return FakeResponse({"choices": [{"message": {"content": "hi"}}]})

        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), \
                mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch.object(app.time, "sleep") as slept:
            out = app.groq_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(out["content"], "hi")
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept.call_count, 2)

    def test_the_wait_comes_from_the_hints_in_the_error(self):
        err = self.http_error(429)
        self.assertAlmostEqual(app.retry_delay(err, "Please try again in 5.8725s.", 0), 5.87, places=1)
        self.assertEqual(app.retry_delay(err, "no hint here", 0), app.GROQ_BACKOFF[0])
        self.assertEqual(app.retry_delay(self.http_error(429, b"", {"Retry-After": "3"}), "", 0), 3.0)
        self.assertLessEqual(app.retry_delay(err, "try again in 900s", 0), app.GROQ_MAX_WAIT)

    def test_a_client_error_is_not_retried(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise self.http_error(401, b'{"error":{"message":"invalid key"}}')

        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), \
                mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch.object(app.time, "sleep") as slept:
            with self.assertRaises(RuntimeError) as ctx:
                app.groq_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(calls), 1)
        self.assertFalse(slept.called)
        self.assertIn("401", str(ctx.exception))

    def test_a_persistent_rate_limit_gives_up_and_falls_back(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise self.http_error(429, b'{"error":{"message":"Rate limit reached"}}')

        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), \
                mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch.object(app.time, "sleep"):
            r = app.process_message("M001", "settlement not received", use_groq=True)
        self.assertEqual(len(calls), app.GROQ_ATTEMPTS)
        self.assertEqual((r["engine"], r["fallback"], r["status"]), ("rules", 1, "resolved"))
        self.assertIn("retrigger_settlement", r["actions"])

    def test_a_server_error_is_retried(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if len(calls) < 2:
                raise self.http_error(503, b'{"error":{"message":"upstream"}}')
            return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), \
                mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch.object(app.time, "sleep"):
            out = app.groq_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(out["content"], "ok")
        self.assertEqual(len(calls), 2)


class ToolTrimmingTests(unittest.TestCase):
    """The model is offered only the tools its category may use: a shorter prompt and a
    narrower reach. Reading is always allowed; acting is not."""

    def setUp(self):
        app.init_db(reset=True)

    def captured_tools(self, mid, text):
        seen = []

        def chat(messages, tools=None):
            seen.append([t["function"]["name"] for t in (tools or [])])
            return {"content": "done"}

        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", chat):
            app.process_message(mid, text, use_groq=True)
        return seen[0]

    def test_a_settlement_message_gets_the_settlement_tools(self):
        names = self.captured_tools("M001", "my settlement has not arrived")
        self.assertIn("get_settlement_status", names)
        self.assertIn("retrigger_settlement", names)
        self.assertIn("raise_bank_ticket", names)
        self.assertIn("escalate_to_human", names)
        self.assertNotIn("request_replacement", names)
        self.assertNotIn("submit_dispute_evidence", names)

    def test_a_device_message_gets_the_device_tools(self):
        names = self.captured_tools("M003", "soundbox is not working")
        self.assertIn("remote_restart_device", names)
        self.assertIn("request_replacement", names)
        self.assertNotIn("retrigger_settlement", names)

    def test_a_dispute_message_gets_the_dispute_tools(self):
        names = self.captured_tools("M006", "a chargeback was raised on my account")
        self.assertIn("submit_dispute_evidence", names)
        self.assertNotIn("remote_restart_device", names)

    def test_the_read_tools_are_always_offered(self):
        """Reading is safe (the gate checks it), so the model may always look."""
        for text in ("my settlement has not arrived", "soundbox is not working",
                     "a chargeback was raised on my account", "what is the weather today"):
            names = self.captured_tools("M001", text)
            for read in app.READ_TOOLS:
                self.assertIn(read, names, text)

    def test_escalation_is_always_offered(self):
        for text in ("my settlement has not arrived", "soundbox is not working", "what is the weather today"):
            self.assertIn("escalate_to_human", self.captured_tools("M001", text))

    def test_the_prompt_is_shorter_than_the_full_tool_list(self):
        names = self.captured_tools("M001", "my settlement has not arrived")
        self.assertLess(len(names), len(app.TOOL_SPECS))

    def test_a_tool_the_model_was_not_offered_is_refused(self):
        """The trim is enforced in code, not only in the prompt."""
        replies = iter([tool_call("request_replacement", device_id="D04"),
                         {"content": "Your settlement is on its way."}])

        def fake_chat(messages, tools=None):
            return next(replies)

        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", fake_chat):
            r = app.process_message("M001", "my settlement has not arrived", use_groq=True)
        self.assertNotIn("request_replacement", r["actions"])
        self.assertTrue(any("request_replacement" in t["title"] and "not offered" in t["title"]
                             for t in r["trace"]))


class HttpApiTests(unittest.TestCase):
    """The HTTP layer itself: routing, status codes, JSON encoding and error handling.
    The other tests call app.py directly; these drive the real server over a socket."""

    def setUp(self):
        app.init_db(reset=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)   # port 0: let the OS pick
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def post(self, path, raw):
        req = urllib.request.Request(self.base + path, data=raw,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_the_page_is_served(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("Merchant chat", body)

    def test_merchants_are_listed(self):
        code, body = self.get("/api/merchants")
        self.assertEqual(code, 200)
        rows = json.loads(body)
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]["id"], "M001")

    def test_config_says_there_is_no_key(self):
        """The test suite never has a key, so the app must report the rule-based engine."""
        code, body = self.get("/api/config")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["groq_available"], False)

    def test_a_chat_message_is_answered(self):
        code, body = self.post("/api/chat", json.dumps(
            {"merchant_id": "M001", "message": "My settlement has not arrived", "use_groq": False}).encode())
        self.assertEqual(code, 200)
        out = json.loads(body)
        self.assertEqual((out["status"], out["engine"]), ("resolved", "rules"))
        self.assertIn("retrigger_settlement", out["actions"])
        self.assertTrue(out["trace"])

    def test_a_hindi_message_survives_the_round_trip(self):
        code, body = self.post("/api/chat", json.dumps(
            {"merchant_id": "M002", "message": "मेरा सेटलमेंट अभी तक नहीं आया", "use_groq": False},
            ensure_ascii=False).encode("utf-8"))
        self.assertEqual(code, 200)
        out = json.loads(body)
        self.assertEqual(out["language"], "hi")
        self.assertIn("बैंक टिकट", out["reply"])

    def test_an_empty_message_is_rejected(self):
        code, body = self.post("/api/chat", json.dumps(
            {"merchant_id": "M001", "message": "   "}).encode())
        self.assertEqual(code, 400)
        self.assertIn("empty message", body)

    def test_a_malformed_body_is_rejected(self):
        code, body = self.post("/api/chat", b"{not json")
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_a_non_utf8_body_is_rejected(self):
        """A body that is not UTF-8 is a client error, not a crash."""
        code, body = self.post("/api/chat", b'{"merchant_id":"M001","message":"caf\xe9"}')
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_metrics_tickets_and_escalations_follow_the_conversation(self):
        self.post("/api/chat", json.dumps(
            {"merchant_id": "M001", "message": "My settlement has not arrived", "use_groq": False}).encode())
        code, body = self.get("/api/metrics")
        self.assertEqual(code, 200)
        m = json.loads(body)
        self.assertEqual((m["total"], m["resolved"], m["decided"]), (1, 1, 1))
        code, body = self.get("/api/tickets")
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)), 1)
        code, body = self.get("/api/escalations")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), [])

    def test_an_escalation_shows_up_in_the_handoff_queue(self):
        self.post("/api/chat", json.dumps(
            {"merchant_id": "M006", "message": "A chargeback was raised on my account", "use_groq": False}).encode())
        code, body = self.get("/api/escalations")
        self.assertEqual(code, 200)
        rows = json.loads(body)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["merchant_id"], "M006")
        self.assertTrue(rows[0]["reason"])

    def test_an_unknown_path_is_a_404(self):
        self.assertEqual(self.get("/api/nope")[0], 404)
        self.assertEqual(self.post("/api/nope", b"{}")[0], 404)

    def test_reset_clears_the_demo_data(self):
        self.post("/api/chat", json.dumps(
            {"merchant_id": "M001", "message": "My settlement has not arrived", "use_groq": False}).encode())
        code, body = self.post("/api/reset", b"{}")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["ok"], True)
        self.assertEqual(json.loads(self.get("/api/metrics")[1])["total"], 0)
        self.assertEqual(json.loads(self.get("/api/tickets")[1]), [])


class SafetyNetTests(unittest.TestCase):
    """The model may not leave a case nobody handles. If it reads the case and then only
    describes it, the rule engine decides — and a case that needs a human gets one."""

    def setUp(self):
        app.init_db(reset=True)

    def run_groq(self, mid, text, script):
        replies = iter(script)

        def fake_chat(messages, tools=None):
            return next(replies)

        with mock.patch.object(app, "GROQ_API_KEY", "test-key"), mock.patch.object(app, "groq_chat", fake_chat):
            return app.process_message(mid, text, use_groq=True)

    def test_a_case_the_model_only_describes_still_goes_to_a_human(self):
        """A chargeback above the auto limit must reach a human even if the model forgets."""
        r = self.run_groq("M006", "a chargeback was raised on my account", [
            tool_call("get_open_dispute"),
            {"content": "Your chargeback is open and needs attention."}])
        self.assertEqual(r["status"], "escalated")
        self.assertIn("escalate_to_human", r["actions"])
        self.assertIn("support team", r["reply"])

    def test_a_case_that_needs_nobody_keeps_the_models_own_answer(self):
        """Nothing was wrong, so the model's answer stands."""
        r = self.run_groq("M008", "settlement status?", [
            tool_call("get_settlement_status"),
            {"content": "Your settlement is already credited. Nothing to do."}])
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["actions"], [])
        self.assertIn("Nothing to do", r["reply"])

    def test_the_safety_net_is_visible_in_the_trace(self):
        r = self.run_groq("M006", "a chargeback was raised on my account", [
            tool_call("get_open_dispute"),
            {"content": "Your chargeback is open."}])
        self.assertTrue(any("rule engine decides" in t["title"] for t in r["trace"]))

    def test_a_model_that_escalates_itself_is_left_alone(self):
        r = self.run_groq("M006", "a chargeback was raised on my account", [
            tool_call("get_open_dispute"),
            tool_call("escalate_to_human", reason="over the limit", case_file="Rs 25,000"),
            {"content": "sent to a human"}])
        self.assertEqual(r["status"], "escalated")
        self.assertFalse(any("rule engine decides" in t["title"] for t in r["trace"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
