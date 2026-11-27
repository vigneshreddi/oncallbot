import copy
import json
import os
import sys
import threading
import unittest
from collections import OrderedDict
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as Object
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from investigator import agent, api, config, llm, sources, store, triage


NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
SERVICES = [{"name": name, "pod_selector": f"app={name}", "prod_commit": "deployed"}
            for name in ("order-service", "inventory-service", "payment-service")]


def connection():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    return conn


def openai_call(identifier, name="search_logs", arguments="{}"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


class AgentTests(unittest.TestCase):
    def harness(self, client, message="ExampleException at dev.orders.Handler.run(Handler.java:42)", raw=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        conn = connection()
        conn.execute.return_value.fetchone.side_effect = [
            {"first_seen": NOW, "title": "alert"},
            {"message": message, "event_at": NOW, "observed_at": NOW, "raw": raw or {}},
        ]
        stack.enter_context(patch.object(store, "db", return_value=conn))
        stack.enter_context(patch.object(store, "now", return_value=NOW))
        stack.enter_context(patch.object(store, "set_status"))
        self.add_evidence = stack.enter_context(patch.object(store, "add_evidence"))
        self.finish = stack.enter_context(patch.object(store, "finish_incident"))
        self.snapshot = stack.enter_context(patch.object(triage, "snapshot", return_value={
            "service": "order-service", "pods": [], "events": [], "ready_endpoints": None,
        }))
        stack.enter_context(patch.object(config, "services", return_value=SERVICES))
        stack.enter_context(patch.object(agent, "make_client", return_value=client))
        stack.enter_context(patch.dict(agent.TOOLS, {
            "search_logs": lambda **kwargs: [{"event_at": NOW, "observed_at": NOW, "message": "healthy trace context"}],
            "search_code": lambda **kwargs: {"chunks": [], "commit": "deployed"},
            "find_rows_by_ids": lambda **kwargs: {"rows": [{"created_at": NOW, "updated_at": NOW, "padding": "x" * 5000}], "observed_at": NOW},
        }))
        return stack

    def run_agent(self):
        agent.investigate(1, "order-service", "fingerprint", None)

    def test_openai_happy_path_all_calls_and_native_history(self):
        client = llm.OpenAICompat()
        self.harness(client)
        calls = [openai_call("first"), openai_call("second", "find_rows_by_ids", '{"schema":"orders","table":"orders","id_column":"id","ids":["1"]}')]
        responses = [
            {"choices": [{"message": {"role": "assistant", "content": "Checking", "tool_calls": calls}}], "usage": {"prompt_tokens": 12, "completion_tokens": 5}},
            {"choices": [{"message": {"role": "assistant", "content": "Evidence reviewed"}}], "usage": {"prompt_tokens": 8, "completion_tokens": 3}},
        ]
        payloads = []

        def post(url, **kwargs):
            payloads.append(copy.deepcopy(kwargs["json"]))
            return httpx.Response(200, json=responses.pop(0), request=httpx.Request("POST", url))

        with patch.object(llm.httpx, "post", side_effect=post):
            self.run_agent()
        history = payloads[1]["messages"]
        self.assertEqual(history[-3]["tool_calls"], calls)
        self.assertEqual([item["tool_call_id"] for item in history[-2:]], ["first", "second"])
        self.assertEqual([item["role"] for item in history[-2:]], ["tool", "tool"])
        usage = self.finish.call_args.args[4]
        self.assertEqual((usage["in"], usage["out"], usage["tool_calls"]), (20, 8, 2))
        report = self.finish.call_args.args[3]
        self.assertIn('"created_at"', report)
        self.assertIn('"updated_at"', report)
        self.assertIn(NOW.isoformat(), report)
        self.assertIsNone(self.finish.call_args.args[5])
        self.assertEqual(len(self.add_evidence.call_args_list), 4)

    def test_anthropic_happy_path_preserves_text_all_blocks_and_results(self):
        client = llm.Anthropic()
        self.harness(client)
        content = [{"type": "text", "text": "Checking"},
                   {"type": "tool_use", "id": "first", "name": "search_logs", "input": {}},
                   {"type": "tool_use", "id": "second", "name": "search_code", "input": {"service": "order-service", "query": "Handler"}}]
        responses = [{"content": content, "usage": {"input_tokens": 7, "output_tokens": 4}},
                     {"content": [{"type": "text", "text": "Evidence reviewed"}], "usage": {"input_tokens": 6, "output_tokens": 2}}]
        payloads = []

        def post(url, **kwargs):
            payloads.append(copy.deepcopy(kwargs["json"]))
            return httpx.Response(200, json=responses.pop(0), request=httpx.Request("POST", url))

        with patch.object(llm.httpx, "post", side_effect=post):
            self.run_agent()
        history = payloads[1]["messages"]
        self.assertEqual(history[-2], {"role": "assistant", "content": content})
        self.assertEqual(history[-1]["role"], "user")
        self.assertEqual([block["tool_use_id"] for block in history[-1]["content"]], ["first", "second"])
        self.assertTrue(all(block["type"] == "tool_result" and not block["is_error"] for block in history[-1]["content"]))
        self.assertEqual(self.finish.call_args.args[4]["in"], 13)

    def test_errors_are_results_and_later_calls_still_execute(self):
        for provider in (llm.OpenAICompat, llm.Anthropic):
            with self.subTest(provider=provider.__name__):
                client = provider()
                stack = self.harness(client)
                stack.enter_context(patch.dict(agent.TOOLS, {"search_logs": MagicMock(side_effect=RuntimeError("unavailable"))}))
                turn = llm.Turn(tool_calls=[llm.ToolCall("search_logs", {}, "throw"), llm.ToolCall("not_allowed", {}, "denied"),
                                           llm.ToolCall("search_code", {}, "malformed", "invalid JSON"),
                                           llm.ToolCall("search_code", {"service": "order-service", "query": "Handler"}, "good")],
                                assistant_message={"role": "assistant", "content": []})
                with patch.object(client, "step", side_effect=[turn, llm.Turn(text="Reviewed")]) as step:
                    self.run_agent()
                history = step.call_args.args[1]
                results = history[-1]["content"] if provider is llm.Anthropic else history[-4:]
                self.assertEqual(len(results), 4)
                self.assertIn("error", results[0]["content"])
                self.assertNotIn('"error"', results[3]["content"])
                if provider is llm.Anthropic:
                    self.assertEqual([result["is_error"] for result in results], [True, True, True, False])
                self.assertEqual(len(self.add_evidence.call_args_list), 6)
                stack.close()

    def test_provider_failure_persists_usage_partial_evidence_and_failure(self):
        client = llm.OpenAICompat()
        self.harness(client)
        turn = llm.Turn(tool_calls=[llm.ToolCall("search_logs", {}, "first")],
                        assistant_message={"role": "assistant", "content": None}, input_tokens=9)
        with patch.object(client, "step", side_effect=[turn, httpx.ConnectError("offline")]):
            self.run_agent()
        self.assertEqual(self.finish.call_args.args[4]["in"], 9)
        self.assertIn("ConnectError", self.finish.call_args.args[5])
        self.assertIn("Incomplete investigation", self.finish.call_args.args[3])
        self.assertGreaterEqual(len(self.add_evidence.call_args_list), 3)

    def test_fallback_retrieval_is_evidence_derived_and_recorded(self):
        self.harness(llm.NoLLM())
        with patch.dict(agent.TOOLS, {"search_code": MagicMock(return_value={"chunks": []})}):
            self.run_agent()
            agent.TOOLS["search_code"].assert_called_once_with(service="order-service", query="dev.orders.Handler.run", limit=3)
        self.assertEqual([call.args[2] for call in self.add_evidence.call_args_list],
                         ["incident_logs", "triage_snapshot", "search_logs", "search_code"])
        self.assertIn("not a proven root cause", self.finish.call_args.args[3])

    def test_structured_stack_is_available_to_model_and_fallback(self):
        client = llm.OpenAICompat()
        raw = {"error.stack_trace": "ExampleException at dev.orders.Handler.run(Handler.java:42)"}
        self.harness(client, "Request failed", raw)
        with patch.object(client, "step", return_value=llm.Turn()) as step, patch.dict(agent.TOOLS, {"search_code": MagicMock(return_value={"chunks": []})}):
            self.run_agent()
            agent.TOOLS["search_code"].assert_called_once_with(service="order-service", query="dev.orders.Handler.run", limit=3)
        payload = json.loads(step.call_args.args[1][0]["content"])
        self.assertEqual(payload["first_error_record"]["raw"], raw)
        self.assertEqual(self.finish.call_args.args[1], "code")

    def test_fallback_has_no_catalog_queries_for_ambiguous_log(self):
        self.harness(llm.NoLLM(), "reserved warehouse authorized duplicate")
        with patch.dict(agent.TOOLS, {"search_code": MagicMock()}) as tools:
            self.run_agent()
            tools["search_code"].assert_not_called()

    def test_call_and_step_budgets_persist_failure_and_all_results(self):
        client = llm.OpenAICompat()
        self.harness(client)
        turn = llm.Turn(tool_calls=[llm.ToolCall("search_logs", {}, identifier) for identifier in ("first", "second")],
                        assistant_message={"role": "assistant", "content": None})
        with patch.object(config, "LLM_MAX_STEPS", 1), patch.object(config, "LLM_MAX_TOOL_CALLS", 1), patch.object(client, "step", return_value=turn):
            self.run_agent()
        self.assertEqual(self.finish.call_args.args[4]["tool_calls"], 1)
        self.assertIn("step budget exhausted", self.finish.call_args.args[5])
        self.assertIn("tool call budget exhausted", self.finish.call_args.args[3])

    def test_audit_records_requests_responses_chunks_and_final(self):
        client = llm.OpenAICompat()
        stack = self.harness(client)
        chunk = {"path": "src/Handler.java", "symbol": "Handler.run", "start_line": 40, "end_line": 44, "score": 0.9, "content": "code"}
        stack.enter_context(patch.dict(agent.TOOLS, {"search_code": lambda **kwargs: {"chunks": [chunk], "commit": "deployed", "mode": "semantic"}}))
        exchanges = stack.enter_context(patch.object(store, "add_llm_exchange"))
        turn = llm.Turn(tool_calls=[llm.ToolCall("search_code", {"service": "order-service", "query": "Handler"}, "call")],
                        assistant_message={"role": "assistant", "content": None}, input_tokens=11, output_tokens=2)
        with patch.object(client, "step", side_effect=[turn, llm.Turn(text="Root cause", input_tokens=5, output_tokens=7)]), \
                patch("builtins.print") as printed:
            self.run_agent()
        kinds = [call.args[2] for call in exchanges.call_args_list]
        self.assertEqual(kinds.count("request"), 2)
        self.assertEqual(kinds.count("response"), 2)
        self.assertEqual(kinds[-1], "final")
        first_request = next(call.args[3] for call in exchanges.call_args_list if call.args[2] == "request")
        self.assertIn("system", first_request)
        self.assertIn("first_error", first_request["new_messages"][0]["content"])
        second_request = [call.args[3] for call in exchanges.call_args_list if call.args[2] == "request"][1]
        self.assertNotIn("system", second_request)
        self.assertEqual(second_request["new_messages"][-1]["tool_call_id"], "call")
        tool = next(call.args[3] for call in exchanges.call_args_list
                    if call.args[2] == "tool_result" and call.args[3]["tool"] == "search_code")
        self.assertEqual(tool["chunks"][0]["path"], "src/Handler.java")
        self.assertEqual(tool["mode"], "semantic")
        responses = [call for call in exchanges.call_args_list if call.args[2] == "response"]
        self.assertEqual((responses[0].args[4], responses[0].args[5]), (11, 2))
        lines = [json.loads(call.args[0]) for call in printed.call_args_list if call.args and str(call.args[0]).startswith("{")]
        self.assertTrue(all(line["event"] == "investigation_audit" and line["incident_id"] == 1 for line in lines))
        self.assertFalse(any("content" in chunk for line in lines for chunk in line.get("chunks") or []))

    def run_with_proposal(self, mode):
        client = llm.OpenAICompat()
        stack = self.harness(client)
        accepted = {"accepted": True, "service": "order-service", "path": "src/Handler.java",
                    "edits": [{"old_text": "a", "new_text": "b"}], "summary": "guard null"}
        stack.enter_context(patch.dict(agent.TOOLS, {"propose_patch": MagicMock(return_value=accepted)}))
        stack.enter_context(patch.object(store, "add_llm_exchange"))
        saved = stack.enter_context(patch.object(store, "save_fix_proposals"))
        stored = [{"id": 5, "service": "order-service", "path": "src/Handler.java", "edits": accepted["edits"], "summary": "guard null"}]
        stack.enter_context(patch.object(store, "fix_proposals", return_value=stored))
        marked = stack.enter_context(patch.object(store, "mark_fix_proposals"))
        opened = stack.enter_context(patch.object(agent.fixes, "open_ai_pull_request", return_value={
            "url": "https://github.com/example/lab/pull/9", "branch": "incident-fix/1-x"}))
        stack.enter_context(patch.dict(os.environ, {"FIX_PR_MODE": mode}))
        call = llm.ToolCall("propose_patch", {"service": "order-service", "path": "src/Handler.java", "edits": accepted["edits"]}, "fix")
        turn = llm.Turn(tool_calls=[call], assistant_message={"role": "assistant", "content": None})
        with patch.object(client, "step", side_effect=[turn, llm.Turn(text="Root cause and fix")]), patch("builtins.print"):
            self.run_agent()
        self.assertEqual(saved.call_args.args[1][0]["path"], "src/Handler.java")
        return opened, marked, self.finish.call_args.args[3]

    def test_ai_proposal_is_opened_as_draft_pr_by_investigator_in_auto_mode(self):
        opened, marked, report = self.run_with_proposal("auto")
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[:2], (1, "order-service"))
        marked.assert_called_once_with([5], "pr_opened", pr_url="https://github.com/example/lab/pull/9")
        self.assertIn("## AI-proposed fix and draft pull requests", report)
        self.assertIn("https://github.com/example/lab/pull/9", report)

    def test_ai_proposal_waits_for_approval_in_approve_mode(self):
        opened, marked, report = self.run_with_proposal("approve")
        opened.assert_not_called()
        marked.assert_not_called()
        self.assertIn("POST /incidents/1/open-pr", report)

    def test_failed_pr_is_reported_not_raised(self):
        with patch.object(agent.fixes, "pr_mode", return_value="auto"):
            with patch.object(store, "save_fix_proposals"), \
                    patch.object(store, "fix_proposals", return_value=[{"id": 1, "service": "s", "path": "p", "edits": [], "summary": ""}]), \
                    patch.object(store, "mark_fix_proposals") as marked, \
                    patch.object(agent.fixes, "open_ai_pull_request", side_effect=agent.fixes.FixError(403, "Draft PR generation is disabled")):
                lines = agent.open_fix_pull_requests(1, [{"service": "s", "path": "p", "edits": []}])
        self.assertIn("No pull request for `s`: Draft PR generation is disabled", lines[-1])
        marked.assert_called_once_with([1], "pr_failed", error="Draft PR generation is disabled")

    def test_incident_audit_links_chunks_to_error(self):
        conn = connection()
        incident = {"id": 1, "fingerprint": "fp", "title": "NullPointerException", "service": "order-service", "category": "code",
                    "status": "analyzed", "first_seen": NOW, "last_seen": NOW, "occurrences": 1, "usage": {}, "failure": None}
        search = {"chunks": [{"path": "A.java", "symbol": "A.run", "start_line": 1, "end_line": 9, "score": 0.8, "content": "abc"}],
                  "commit": "sha", "mode": "semantic"}
        conn.execute.return_value.fetchone.return_value = incident
        conn.execute.return_value.fetchall.side_effect = [
            [{"step": 1, "tool": "search_code", "arguments": {"query": "null"}, "result_summary": json.dumps(search), "observed_at": NOW}],
            [{"round": 1, "kind": "request", "payload": {"new_messages": []}, "input_tokens": 0, "output_tokens": 0,
              "created_at": NOW + timedelta(seconds=1)}],
            [{"service": "order-service", "path": "A.java", "summary": "fix", "edits": [], "status": "pr_opened",
              "pr_url": "https://github.com/example/lab/pull/1", "error": None, "created_at": NOW, "updated_at": NOW + timedelta(seconds=2)}],
            [{"source": "datadog", "external_id": "evt-9", "service": "order-service", "pod": None, "event_at": NOW,
              "received_at": NOW + timedelta(seconds=3), "trace_id": "t-9", "span_id": "s-9", "correlation_id": None,
              "request_id": None, "message": "NPE", "details": {}}],
        ]
        with patch.object(store, "db", return_value=conn):
            records = store.incident_audit(1)
        self.assertEqual([record["type"] for record in records],
                         ["incident", "tool_result", "code_chunk_sent", "llm_request", "fix_proposal", "occurrence"])
        self.assertEqual((records[-1]["trace_id"], records[-1]["span_id"]), ("t-9", "s-9"))
        self.assertEqual(records[-2]["pr_url"], "https://github.com/example/lab/pull/1")
        sent = records[2]
        self.assertEqual((sent["error_title"], sent["query"], sent["path"], sent["mode"], sent["characters"]),
                         ("NullPointerException", "null", "A.java", "semantic", 3))
        conn.execute.return_value.fetchone.return_value = None
        with patch.object(store, "db", return_value=conn):
            self.assertIsNone(store.incident_audit(2))

    def test_handoff_brief_contains_everything_needed_to_continue(self):
        records = [
            {"type": "incident", "id": 1, "fingerprint": "fp", "title": "NPE", "service": "order-service", "category": "code",
             "status": "analyzed", "first_seen": NOW, "last_seen": NOW, "occurrences": 2, "usage": {}, "failure": None},
            {"type": "tool_result", "step": 1, "tool": "incident_logs", "arguments": {},
             "result": {"message": "NullPointerException at place", "raw": {"error.stack_trace": "java.lang.NPE\n\tat A.place(A.java:48)"}}},
            {"type": "tool_result", "step": 2, "tool": "triage_snapshot", "arguments": {},
             "result": {"pods": [{"pod": "order-1", "ready": True, "restarts": 0}], "events": [], "db_health": {"connections": 3}}},
            {"type": "tool_result", "step": 3, "tool": "run_readonly_sql", "arguments": {"sql": "select 1"}, "result": {"rows": [{"reserved_qty": None}]}},
            {"type": "code_chunk_sent", "step": 4, "path": "src/A.java", "symbol": "A.place", "start_line": 37, "end_line": 57,
             "score": 0.53, "mode": "semantic", "query": "reservedQty null"},
            {"type": "llm_response", "round": 1, "input_tokens": 10, "output_tokens": 2,
             "payload": {"tool_calls": [{"name": "search_code", "arguments": {"query": "reservedQty null"}}]}},
            {"type": "fix_proposal", "service": "order-service", "path": "src/A.java", "summary": "guard null",
             "edits": [{"old_text": "int r = q();", "new_text": "int r = q() == null ? 0 : q();"}],
             "status": "pr_opened", "pr_url": "https://github.com/example/lab/pull/24", "error": None},
            {"type": "llm_final", "round": 2, "input_tokens": 0, "output_tokens": 0, "payload": {"findings": "Root cause: null reserved_qty."}},
        ]
        with patch.object(store, "incident_audit", return_value=records), \
                patch.object(config, "service_by_name", return_value={"repo_url": "https://github.com/example/lab",
                                                                       "repo_path": "services/order-service", "prod_commit": "a" * 40}):
            brief = store.incident_handoff(1)
        for expected in ["# Incident 1 handoff", "a" * 40, "NullPointerException at place", "at A.place(A.java:48)",
                         "Root cause: null reserved_qty.", "https://github.com/example/lab/pull/24", "- int r = q();",
                         "+ int r = q() == null ? 0 : q();", "`src/A.java` lines 37\u201357", "reservedQty null",
                         "run_readonly_sql", "\"reserved_qty\": null", "order-1 ready=True", "round 1: search_code",
                         "Suggested prompts"]:
            self.assertIn(expected, brief)
        with patch.object(store, "incident_audit", return_value=None):
            self.assertIsNone(store.incident_handoff(9))
        with patch.object(store, "incident_handoff", return_value="# brief\n"):
            response = TestClient(api.app).get("/incidents/1/handoff")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/markdown"))


class ProviderTests(unittest.TestCase):
    def test_malformed_openai_arguments_keep_ids(self):
        data = {"choices": [{"message": {"role": "assistant", "tool_calls": [openai_call("bad", arguments="{"), openai_call("array", arguments="[]"), openai_call("good")]}}]}
        with patch.object(llm, "post", return_value=data):
            turn = llm.OpenAICompat().step("system", [], [])
        self.assertEqual([call.id for call in turn.tool_calls], ["bad", "array", "good"])
        self.assertTrue(turn.tool_calls[0].error)
        self.assertTrue(turn.tool_calls[1].error)
        self.assertIsNone(turn.tool_calls[2].error)

    def test_retries_are_bounded_and_nonretryable_status_is_not_retried(self):
        request = httpx.Request("POST", "https://example.invalid")
        for status, count in ((429, 3), (503, 3), (401, 1)):
            with self.subTest(status=status), patch.object(config, "LLM_RETRIES", 2), patch.object(llm.time, "sleep"), patch.object(llm.httpx, "post", return_value=httpx.Response(status, request=request)) as post:
                with self.assertRaises(httpx.HTTPStatusError):
                    llm.post(str(request.url))
                self.assertEqual(post.call_count, count)

    def test_retry_success_and_transport_failure(self):
        request = httpx.Request("POST", "https://example.invalid")
        with patch.object(config, "LLM_RETRIES", 2), patch.object(llm.time, "sleep"), patch.object(llm.httpx, "post", side_effect=[httpx.ConnectError("offline"), httpx.Response(200, json={"ok": True}, request=request)]) as post:
            self.assertEqual(llm.post(str(request.url)), {"ok": True})
            self.assertEqual(post.call_count, 2)


class LogTests(unittest.TestCase):
    def test_search_and_trace_context_return_record_times_and_raw(self):
        conn = connection()
        row = {"event_at": NOW, "observed_at": NOW, "raw": {"error.stack_trace": "ExampleException"}}
        conn.execute.return_value.fetchall.return_value = [row]
        with patch.object(store, "db", return_value=conn):
            source = sources.KubernetesLogSource()
            self.assertEqual(source.search("svc", None), [row])
            self.assertIn("raw", conn.execute.call_args.args[0])
            self.assertEqual(source.get_context("trace"), [row])
            self.assertIn("event_at", conn.execute.call_args.args[0])
            self.assertIn("raw", conn.execute.call_args.args[0])

    def test_same_message_different_records_and_cache_bounds(self):
        cache = OrderedDict()
        conn = connection()
        conn.execute.return_value.fetchone.return_value = {"id": 1}
        with patch.object(store, "db", return_value=conn), patch.object(store, "now", return_value=NOW), patch.object(config, "LOG_CACHE_SIZE", 2):
            def insert(timestamp, uid="pod-uid", occurrence=1):
                return store.insert_log("fp", "svc", "pod", "INFO", "trace", "same", {"@timestamp": timestamp, "_pod_uid": uid, "_record_occurrence": occurrence}, cache)
            self.assertTrue(insert(NOW.isoformat()))
            self.assertFalse(insert(NOW.isoformat()))
            self.assertTrue(insert((NOW + timedelta(seconds=1)).isoformat()))
            self.assertFalse(insert((NOW + timedelta(seconds=1)).isoformat()))
            self.assertTrue(insert(NOW.isoformat(), occurrence=2))
            self.assertTrue(insert(NOW.isoformat(), uid="replacement"))
            self.assertEqual(len(cache), 2)

    def test_failed_insert_not_cached_and_durable_duplicates_rejected(self):
        cache = OrderedDict()
        conn = connection()
        with patch.object(store, "db", return_value=conn):
            conn.execute.side_effect = RuntimeError("db offline")
            with self.assertRaises(RuntimeError):
                store.insert_log("fp", "svc", "pod", "INFO", None, "message", {}, cache)
            self.assertFalse(cache)
            conn.execute.side_effect = None
            conn.execute.return_value.fetchone.return_value = None
            self.assertFalse(store.insert_log("fp", "svc", "pod", "INFO", None, "message", {}, cache))

    def test_cache_expiry_and_log_retention(self):
        cache = OrderedDict({"expired": NOW - timedelta(days=2)})
        conn = connection()
        conn.execute.return_value.fetchone.return_value = {"id": 1}
        with patch.object(store, "db", return_value=conn), patch.object(store, "now", return_value=NOW):
            store.insert_log("fp", "svc", "pod", "INFO", None, "message", {}, cache)
            store.prune_logs()
        self.assertNotIn("expired", cache)
        self.assertIn("coalesce(event_at, observed_at)", conn.execute.call_args.args[0])

    def test_collection_keeps_normal_logs_and_only_errors_trigger(self):
        kube = MagicMock()
        kube.list_namespaced_pod.return_value.items = [Object(metadata=Object(name="pod", uid="uid"))]
        kube.read_namespaced_pod_log.return_value = '\n'.join([
            '2026-10-03T12:00:00Z {"log.level":"INFO","message":"normal","trace":{"id":"shared"}}',
            '2026-10-03T12:00:01Z {"log.level":"ERROR","message":"ExampleException","trace.id":"shared"}',
            '2026-10-03T12:00:02Z {"log.level":"ERROR","message":"ExampleException","trace.id":"shared"}',
        ])
        callback = MagicMock()
        with patch.object(config, "services", return_value=SERVICES[:1]), patch.object(sources.client, "CoreV1Api", return_value=kube), patch.object(store, "prune_logs"), patch.object(store, "insert_log", return_value=True) as insert, patch.object(store, "upsert_incident", side_effect=[(1, True), (1, False)]) as upsert, patch.object(store, "add_occurrence") as occurrence:
            sources.KubernetesLogSource().collect_once(callback)
        self.assertEqual(occurrence.call_count, 2)
        self.assertEqual(occurrence.call_args.kwargs["ids"], {"trace_id": "shared"})
        self.assertEqual(occurrence.call_args.kwargs["pod"], "pod")
        self.assertEqual(insert.call_count, 3)
        self.assertEqual(upsert.call_count, 2)
        self.assertEqual(insert.call_args_list[0].args[4], "shared")
        self.assertEqual(insert.call_args_list[0].args[6]["_k8s_timestamp"], "2026-10-03T12:00:00Z")
        callback.assert_called_once()
        self.assertTrue(kube.read_namespaced_pod_log.call_args.kwargs["timestamps"])

    def test_evidence_and_usage_are_not_truncated(self):
        conn = connection()
        result = json.dumps({"rows": [{"padding": "x" * 5000, "updated_at": NOW.isoformat()}], "observed_at": NOW.isoformat()})
        with patch.object(store, "db", return_value=conn):
            store.add_evidence(1, 1, "rows", {}, result)
            self.assertEqual(conn.execute.call_args.args[1][-1], result)
            store.finish_incident(1, "unknown", {}, "report", {"in": 3}, "offline")
            self.assertEqual(conn.execute.call_args.args[1][-2], "failed")
            self.assertIn('"in": 3', conn.execute.call_args.args[1][3])


class TriageTests(unittest.TestCase):
    def test_historical_termination_and_missing_evidence_stay_unknown(self):
        snap = {"pods": [{"containers": [{"last_termination_reason": "OOMKilled", "last_termination_in_window": False}]}], "events": [], "ready_endpoints": None}
        self.assertEqual(triage.classify(snap, ""), "unknown")
        self.assertEqual(triage.classify(snap, "order 1503"), "unknown")
        snap["pods"][0]["containers"][0]["last_termination_in_window"] = True
        self.assertEqual(triage.classify(snap, ""), "infra")

    def test_all_services_event_window_and_missing_nodes_resources(self):
        core = MagicMock()
        old = Object(event_time=NOW - timedelta(days=2), last_timestamp=None, first_timestamp=None, series=None,
                     metadata=Object(creation_timestamp=NOW - timedelta(days=2)), involved_object=Object(name="order-service-pod"), reason="BackOff", message="old failure", type="Warning")
        recent = copy.copy(old)
        recent.event_time = NOW
        recent.message = "current failure"
        core.list_namespaced_event.return_value.items = [old, recent]
        status = Object(name="app", ready=True, restart_count=1, state=Object(waiting=None, terminated=None),
                        last_state=Object(terminated=Object(reason="OOMKilled", finished_at=NOW - timedelta(days=2))))
        pod = Object(metadata=Object(name="order-service-pod", annotations={}, uid="uid"),
                     spec=Object(node_name="node", containers=[Object(name="app", resources=None)]),
                     status=Object(phase="Running", container_statuses=[status]))
        core.list_namespaced_pod.return_value.items = [pod]
        core.read_node.side_effect = PermissionError("node RBAC")
        core.read_namespaced_endpoints.side_effect = PermissionError("endpoints RBAC")
        metrics = MagicMock()
        metrics.list_namespaced_custom_object.side_effect = RuntimeError("metrics unavailable")
        metrics.list_cluster_custom_object.side_effect = PermissionError("metrics RBAC")
        apps = MagicMock()
        apps.read_namespaced_deployment.side_effect = PermissionError("rollout RBAC")
        with patch.object(config, "services", return_value=SERVICES), patch.object(store, "now", return_value=NOW), patch.object(store, "db", side_effect=RuntimeError("db offline")), patch.object(triage.client, "CoreV1Api", return_value=core), patch.object(triage.client, "CustomObjectsApi", return_value=metrics), patch.object(triage.client, "AppsV1Api", return_value=apps):
            snap = triage.snapshot("order-service")
        self.assertEqual(set(snap["services"]), {svc["name"] for svc in SERVICES})
        self.assertEqual(core.list_namespaced_pod.call_count, 3)
        self.assertEqual(len(snap["events"]), 1)
        self.assertEqual(snap["events"][0]["message"], "current failure")
        self.assertFalse(snap["pods"][0]["containers"][0]["last_termination_in_window"])
        self.assertIsNone(snap["ready_endpoints"])
        self.assertIn("error", snap["nodes"]["node"])
        self.assertIn("error", snap["resource_usage"]["pods"])
        self.assertTrue(any(error["source"] == "resource_limits" for error in snap["errors"]))
        self.assertTrue(any(error["source"] == "pod_usage" for error in snap["errors"]))
        self.assertEqual(snap["nodes"]["node"]["usage_error"], "unavailable")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.occurrence = self.enterContext(patch.object(store, "add_occurrence", return_value=True))

    def test_real_semaphore_bounds_admission_and_recovers_after_release(self):
        with patch.object(api, "slots", threading.BoundedSemaphore(2)), patch.object(store, "set_status"), patch.object(api.workers, "submit") as submit, patch.object(agent, "investigate"):
            self.assertTrue(api.on_new_incident(1, "svc", "fp1", None))
            self.assertTrue(api.on_new_incident(2, "svc", "fp2", None))
            self.assertFalse(api.on_new_incident(3, "svc", "fp3", None))
            self.assertEqual(submit.call_count, 2)
            api.run_investigation(1, "svc", "fp1", None)
            self.assertTrue(api.on_new_incident(4, "svc", "fp4", None))

    def test_webhook_validation_happens_before_storage(self):
        client = TestClient(api.app)
        with patch.object(config, "service_by_name", return_value=None), patch.object(store, "upsert_incident") as upsert:
            for payload in ({}, {"service": None}, {"service": "unknown"}, {"service": "svc", "title": " "}, {"service": ["svc"]}):
                self.assertEqual(client.post("/webhooks/incident", json=payload).status_code, 422)
            upsert.assert_not_called()

    def test_webhook_happy_path_and_long_titles_do_not_collide(self):
        client = TestClient(api.app)
        with patch.object(config, "service_by_name", return_value=SERVICES[0]), patch.object(store, "upsert_incident", return_value=(1, True)) as upsert, patch.object(api, "on_new_incident", return_value=True) as schedule:
            for suffix in ("first", "second"):
                response = client.post("/webhooks/incident", json={"service": "order-service", "title": "prefix" * 40 + suffix, "trace_id": "trace"})
                self.assertEqual(response.status_code, 200)
            self.assertNotEqual(upsert.call_args_list[0].args[0], upsert.call_args_list[1].args[0])
            self.assertEqual(schedule.call_args.args[-1], "trace")

    def test_saturation_and_submission_failure_are_persisted(self):
        slots = MagicMock()
        with patch.object(api, "slots", slots), patch.object(store, "set_status") as status, patch.object(api.workers, "submit", side_effect=RuntimeError("offline")) as submit:
            slots.acquire.return_value = False
            self.assertFalse(api.on_new_incident(1, "svc", "fp", None))
            submit.assert_not_called()
            self.assertEqual(status.call_args.args[1], "failed")
            slots.acquire.return_value = True
            self.assertFalse(api.on_new_incident(1, "svc", "fp", None))
            slots.release.assert_called_once()
            self.assertEqual(status.call_args.args[1], "failed")

    def test_worker_failure_releases_slot_and_records_status(self):
        with patch.object(api, "slots") as slots, patch.object(agent, "investigate", side_effect=RuntimeError("offline")), patch.object(store, "set_status") as status:
            api.run_investigation(1, "svc", "fp", None)
        slots.release.assert_called_once()
        self.assertEqual(status.call_args.args[1], "failed")

    def test_webhook_capacity_failure_returns_503(self):
        client = TestClient(api.app)
        with patch.object(config, "service_by_name", return_value=SERVICES[0]), patch.object(store, "upsert_incident", return_value=(1, True)), patch.object(api, "on_new_incident", return_value=False):
            self.assertEqual(client.post("/webhooks/incident", json={"service": "order-service"}).status_code, 503)

    def test_open_pr_approval_endpoint(self):
        client = TestClient(api.app)
        conn = connection()
        pending = [{"id": 3, "service": "order-service", "path": "p", "edits": [], "status": "proposed"}]
        with patch.object(api, "ensure_enabled"), patch.object(store, "db", return_value=conn), \
                patch.object(store, "fix_proposals", return_value=pending), \
                patch.object(agent, "open_stored_proposals", return_value=["- Draft pull request"]) as opened:
            for row, status in [(None, 404), ({"status": "running"}, 409), ({"status": "analyzed"}, 200)]:
                with self.subTest(row=row):
                    conn.execute.return_value.fetchone.return_value = row
                    self.assertEqual(client.post("/incidents/1/open-pr").status_code, status)
            opened.assert_called_once_with(1, pending)
        with patch.object(api, "ensure_enabled", side_effect=api.FixError(403, "disabled")):
            self.assertEqual(client.post("/incidents/1/open-pr").status_code, 403)

    def test_reinvestigate_resets_and_requeues(self):
        client = TestClient(api.app)
        conn = connection()
        with patch.object(store, "db", return_value=conn), patch.object(store, "reset_investigation") as reset, \
                patch.object(api, "on_new_incident", return_value=True) as queue:
            conn.execute.return_value.fetchone.return_value = {"service": "order-service", "fingerprint": "fp", "status": "running"}
            self.assertEqual(client.post("/incidents/1/reinvestigate").status_code, 409)
            reset.assert_not_called()
            conn.execute.return_value.fetchone.return_value = {"service": "order-service", "fingerprint": "fp", "status": "analyzed"}
            self.assertEqual(client.post("/incidents/1/reinvestigate").status_code, 202)
            reset.assert_called_once_with(1)
            queue.assert_called_once_with(1, "order-service", "fp", None)


if __name__ == "__main__":
    unittest.main()