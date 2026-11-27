import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from investigator import api, fixes, tools


DEPLOYED = "a" * 40
BASE = "b" * 40
TREE = "c" * 40
BLOB = "d" * 40
SERVICE = {"repo_url": "https://github.com/example/lab", "repo_path": "services/orders",
           "prod_commit": DEPLOYED, "pr_target_branch": "release/stable"}


class FixTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"FIX_PR_ENABLED": "true", "GITHUB_WRITE_TOKEN": "mock-write",
                              "FIX_EVIDENCE_BASE_URL": "https://evidence.example/lab"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.service = patch.object(fixes.config, "service_by_name", return_value=SERVICE.copy())
        self.service_mock = self.service.start()
        self.addCleanup(self.service.stop)
        self.calls = []
        self.replies = {}
        real_client = httpx.Client
        self.client = patch.object(fixes.httpx, "Client", side_effect=lambda **kwargs: real_client(
            transport=httpx.MockTransport(self.http), **kwargs))
        self.client_mock = self.client.start()
        self.addCleanup(self.client.stop)

    def request(self, **kwargs):
        payload = {"reviewer_confirmed": True, "analysis_sha": DEPLOYED, "target_branch": "release/stable",
                   "files": [{"path": "services/orders/src/Main.java", "content": "class Main {}",
                              "expected_blob_sha": BLOB}]}
        payload.update(kwargs)
        return fixes.FixRequest(**payload)

    def http(self, request):
        import json
        payload = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, payload))
        path = request.url.path
        if path.endswith("/git/ref/heads/release/stable"):
            data = {"ref": "refs/heads/release/stable", "object": {"sha": BASE}}
        elif path.endswith("/git/commits/" + DEPLOYED):
            data = {"sha": DEPLOYED}
        elif "/compare/" in path:
            data = {"status": "ahead", "merge_base_commit": {"sha": DEPLOYED}}
        elif path.endswith("/git/commits/" + BASE):
            data = {"sha": BASE, "tree": {"sha": TREE}}
        elif path.endswith("/git/trees/" + TREE):
            data = {"truncated": False, "tree": [{"path": "services/orders/src/Main.java", "type": "blob",
                                                   "mode": "100644", "sha": BLOB}]}
        elif path.endswith("/pulls"):
            data = {"number": 17}
        else:
            data = {"sha": "e" * 40}
        for suffix, reply in self.replies.items():
            if path.endswith(suffix):
                if isinstance(reply, Exception):
                    raise reply
                if isinstance(reply, int):
                    return httpx.Response(reply, json={"message": "sensitive upstream response"})
                data = reply
        return httpx.Response(200, json=data)

    def test_draft_sequence_and_maintained_parent(self):
        result = fixes.propose_fix(4, "orders", self.request())
        writes = [call for call in self.calls if call[0] != "GET"]
        self.assertEqual([call[1].split("/git/")[-1] for call in writes[:4]],
                         ["refs", "blobs", "trees", "commits"])
        self.assertEqual(writes[0][2]["sha"], BASE)
        self.assertEqual(writes[2][2]["base_tree"], TREE)
        self.assertEqual(writes[3][2]["parents"], [BASE])
        self.assertEqual(writes[4][0], "PATCH")
        self.assertFalse(writes[4][2]["force"])
        self.assertTrue(writes[5][2]["draft"])
        self.assertEqual(writes[5][2]["base"], "release/stable")
        self.assertIn("https://evidence.example/lab/incidents/4/report", writes[5][2]["body"])
        self.assertEqual(result["number"], 17)

    def assert_no_writes(self):
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

    def test_path_traversal_and_outside_service_rejected_without_http(self):
        for path in ["../Main.java", "services/orders/../payment/Main.java", "services/orders//Main.java",
                     "services/orders\\Main.java", "/services/orders/Main.java", "services/orders/.env",
                     "services/orders/%2e%2e/Main.java", "services/payments/Main.java"]:
            with self.subTest(path=path), self.assertRaises(fixes.FixError):
                fixes.propose_fix(4, "orders", self.request(files=[{"path": path, "content": "safe"}]))
        self.client_mock.assert_not_called()

    def test_repository_allowlist_and_missing_config(self):
        for url in [None, "", "http://github.com/example/lab", "https://github.com.evil/example/lab",
                    "https://github.com/example/lab?token=private", "https://user@github.com/example/lab",
                    "https://api.github.com/repos/example/lab", "https://github.com/example/lab/tree/main"]:
            with self.subTest(url=url), self.assertRaises(fixes.FixError):
                self.service_mock.return_value = {**SERVICE, "repo_url": url}
                fixes.propose_fix(4, "orders", self.request())
        self.service_mock.return_value = None
        with self.assertRaises(fixes.FixError):
            fixes.propose_fix(4, "orders", self.request())
        self.client_mock.assert_not_called()

    def test_missing_invalid_or_mismatched_deployed_sha(self):
        for sha in [None, "", "main", "a" * 7, "a" * 39, "z" * 40, BASE]:
            with self.subTest(sha=sha), self.assertRaises(fixes.FixError):
                self.service_mock.return_value = {**SERVICE, "prod_commit": sha}
                fixes.propose_fix(4, "orders", self.request())
        self.service_mock.return_value = {**SERVICE, "commit_kind": "synthetic"}
        with self.assertRaises(fixes.FixError):
            fixes.propose_fix(4, "orders", self.request())
        self.client_mock.assert_not_called()

    def test_separate_token_required(self):
        with patch.object(fixes.config, "GITHUB_TOKEN", "mock-write"), self.assertRaises(fixes.FixError):
            fixes.propose_fix(4, "orders", self.request())
        self.client_mock.assert_not_called()

    def test_target_branch_must_be_configured_and_exact(self):
        for branch in ["main", "refs/heads/release/stable", "release/../stable", "release//stable", "release/stable.lock"]:
            with self.subTest(branch=branch), self.assertRaises(fixes.FixError):
                fixes.propose_fix(4, "orders", self.request(target_branch=branch))
        self.service_mock.return_value = {key: value for key, value in SERVICE.items() if key != "pr_target_branch"}
        with self.assertRaises(fixes.FixError):
            fixes.propose_fix(4, "orders", self.request())
        self.client_mock.assert_not_called()

    def test_explicit_reviewer_and_structured_payload_required(self):
        for confirmation in [False, 1, "true", None]:
            with self.subTest(value=confirmation), self.assertRaises(ValidationError):
                self.request(reviewer_confirmed=confirmation)
        for values in [{"files": []}, {"patch": "unstructured model diff"}, {"repo_url": "https://github.com/other/repo"}]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.request(**values)

    def test_evidence_url_and_credentials_are_refused_without_http(self):
        for url in ["", "http://evidence.example", "https://user:private@evidence.example",
                    "https://evidence.example?token=private", "https://evidence.example/#private", "https://[bad"]:
            with self.subTest(url=url), patch.dict(os.environ, {"FIX_EVIDENCE_BASE_URL": url}), self.assertRaises(fixes.FixError):
                fixes.propose_fix(4, "orders", self.request())
        with self.assertRaises(fixes.FixError):
            fixes.propose_fix(4, "orders", self.request(files=[{"path": "services/orders/config.properties",
                                                             "content": "password=not-a-real-credential"}]))
        self.client_mock.assert_not_called()

    def test_divergent_ancestry_and_wrong_merge_base_refuse_writes(self):
        for comparison in [{"status": "diverged", "merge_base_commit": {"sha": TREE}},
                           {"status": "behind", "merge_base_commit": {"sha": BASE}},
                           {"status": "identical", "merge_base_commit": {"sha": DEPLOYED}},
                           {"status": "ahead", "merge_base_commit": {"sha": TREE}}]:
            with self.subTest(comparison=comparison), self.assertRaises(fixes.FixError):
                self.replies = {f"/compare/{DEPLOYED}...{BASE}": comparison}
                fixes.propose_fix(4, "orders", self.request())
        self.assert_no_writes()

    def test_only_exact_branch_ref_is_accepted(self):
        self.replies = {"/git/ref/heads/release/stable": {"ref": "refs/tags/release/stable", "object": {"sha": BASE}}}
        with self.assertRaises(fixes.FixError):
            fixes.propose_fix(4, "orders", self.request())
        self.assert_no_writes()

    def test_stale_blob_refuses_writes(self):
        with self.assertRaises(fixes.FixError):
            fixes.propose_fix(4, "orders", self.request(files=[{"path": "services/orders/src/Main.java",
                            "content": "class Main {}", "expected_blob_sha": TREE}]))
        self.assert_no_writes()

    def test_symlinks_submodules_and_truncated_tree_refuse_writes(self):
        for tree in [{"truncated": True, "tree": []},
                     {"truncated": False, "tree": [{"path": "services/orders/src", "type": "blob", "mode": "120000", "sha": BLOB}]},
                     {"truncated": False, "tree": [{"path": "services/orders/src/Main.java", "type": "blob", "mode": "120000", "sha": BLOB}]},
                     {"truncated": False, "tree": [{"path": "services/orders/src/Main.java", "type": "commit", "mode": "160000", "sha": BLOB}]}]:
            with self.subTest(tree=tree), self.assertRaises(fixes.FixError):
                self.replies = {"/git/trees/" + TREE: tree}
                fixes.propose_fix(4, "orders", self.request())
        self.assert_no_writes()

    def test_new_files_are_supported(self):
        result = fixes.propose_fix(4, "orders", self.request(files=[{"path": "services/orders/src/New.java",
                                                                   "content": "class New {}"}]))
        self.assertTrue(result["draft"])
        tree_write = next(payload for method, path, payload in self.calls if method == "POST" and path.endswith("/git/trees"))
        self.assertEqual(tree_write["tree"][0]["mode"], "100644")

    def test_identical_branch_and_deployed_sha_are_supported(self):
        self.replies = {
            "/git/ref/heads/release/stable": {"ref": "refs/heads/release/stable", "object": {"sha": DEPLOYED}},
            "/git/commits/" + DEPLOYED: {"sha": DEPLOYED, "tree": {"sha": TREE}},
            f"/compare/{DEPLOYED}...{DEPLOYED}": {"status": "identical", "merge_base_commit": {"sha": DEPLOYED}},
        }
        self.assertEqual(fixes.propose_fix(4, "orders", self.request())["parent_sha"], DEPLOYED)

    def test_branch_movement_refuses_writes(self):
        original = self.http
        refs = []

        def moving(request):
            response = original(request)
            if request.url.path.endswith("/git/ref/heads/release/stable"):
                refs.append(request)
                if len(refs) == 2:
                    return httpx.Response(200, json={"object": {"sha": TREE}})
            return response

        with patch.object(self, "http", side_effect=moving), self.assertRaises(fixes.FixError):
            fixes.propose_fix(4, "orders", self.request())
        self.assert_no_writes()

    def test_http_failures_are_sanitized_and_not_retried(self):
        for failure in [403, httpx.ConnectError("sensitive upstream detail")]:
            with self.subTest(failure=failure), self.assertRaises(fixes.FixError) as caught:
                self.calls.clear()
                self.replies = {"/git/ref/heads/release/stable": failure}
                fixes.propose_fix(4, "orders", self.request())
            self.assertEqual(caught.exception.status, 502)
            self.assertNotIn("sensitive", caught.exception.detail)
            self.assertEqual(len(self.calls), 1)

    def test_duplicate_paths_and_byte_budget_refuse_without_http(self):
        replacement = {"path": "services/orders/src/New.java", "content": "class New {}"}
        for files in [[replacement, replacement], [{"path": f"services/orders/{index}.java", "content": "x" * 100_000} for index in range(3)]]:
            with self.subTest(count=len(files)), self.assertRaises(fixes.FixError):
                fixes.propose_fix(4, "orders", self.request(files=files))
        self.client_mock.assert_not_called()

    def test_api_success_uses_incident_service_and_does_not_publish_db_values(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchone.return_value = {"service": "orders", "status": "analyzed", "sensitive": "private-db-value"}
        with patch.object(api.store, "db", return_value=conn), patch.object(api.agent, "investigate") as autonomous:
            response = TestClient(api.app).post("/incidents/4/propose-fix", json=self.request().model_dump())
        self.assertEqual(response.status_code, 201)
        autonomous.assert_not_called()
        self.service_mock.assert_called_once_with("orders")
        self.assertNotIn("private-db-value", str(self.calls))
        conn.execute.assert_called_once_with("select service, status from investigator.incidents where id=%s", (4,))

    def test_api_disabled_missing_incident_unfinished_and_invalid_request(self):
        client = TestClient(api.app)
        with patch.dict(os.environ, {"FIX_PR_ENABLED": ""}), patch.object(api.store, "db") as database:
            self.assertEqual(client.post("/incidents/4/propose-fix", json=self.request().model_dump()).status_code, 403)
            database.assert_not_called()
        conn = MagicMock()
        conn.__enter__.return_value = conn
        for row, status in [(None, 404), ({"service": "orders", "status": "queued"}, 409)]:
            with self.subTest(row=row), patch.object(api.store, "db", return_value=conn):
                conn.execute.return_value.fetchone.return_value = row
                self.assertEqual(client.post("/incidents/4/propose-fix", json=self.request().model_dump()).status_code, status)
        payload = self.request().model_dump()
        payload["reviewer_confirmed"] = False
        self.assertEqual(client.post("/incidents/4/propose-fix", json=payload).status_code, 422)
        self.client_mock.assert_not_called()

    def test_write_operation_is_not_an_agent_tool(self):
        self.assertNotIn("propose_fix", tools.TOOLS)
        self.assertNotIn("propose_fix", [schema["name"] for schema in tools.TOOL_SCHEMAS])

    def test_disabled_missing_token_and_synthetic_sha_fail_closed(self):
        for values in [{"FIX_PR_ENABLED": ""}, {"GITHUB_WRITE_TOKEN": ""}]:
            with patch.dict(os.environ, values), self.assertRaises(fixes.FixError):
                fixes.propose_fix(4, "orders", self.request())
        self.service_mock.return_value["prod_commit"] = "local-demo"
        with self.assertRaises(fixes.FixError):
            fixes.propose_fix(4, "orders", self.request())
        self.client_mock.assert_not_called()

    def contents(self, text):
        import base64
        return {"type": "file", "encoding": "base64", "sha": BLOB, "content": base64.b64encode(text.encode()).decode()}

    def ai_proposal(self, old="int x = a;", new="int x = b;"):
        return [{"path": "src/Main.java", "edits": [{"old_text": old, "new_text": new}]}]

    def test_ai_proposal_is_applied_to_target_branch_file_and_labelled(self):
        self.replies = {"/contents/services/orders/src/Main.java": self.contents("class Main {\n  int x = a;\n}\n")}
        result = fixes.open_ai_pull_request(4, "orders", self.ai_proposal())
        self.assertTrue(result["draft"])
        read = next(call for call in self.calls if "/contents/" in call[1])
        self.assertEqual(read[0], "GET")
        blob = next(payload for method, path, payload in self.calls if method == "POST" and path.endswith("/git/blobs"))
        self.assertEqual(blob["content"], "class Main {\n  int x = b;\n}\n")
        pull = next(payload for method, path, payload in self.calls if path.endswith("/pulls"))
        self.assertTrue(pull["title"].startswith("AI-proposed fix for incident 4"))
        self.assertIn("No person has reviewed it yet", pull["body"])
        self.assertTrue(pull["draft"])
        self.assertEqual(pull["base"], "release/stable")

    def test_ai_proposal_keeps_crlf_files_and_refuses_stale_edits(self):
        self.replies = {"/contents/services/orders/src/Main.java": self.contents("class Main {\r\n  int x = a;\r\n}\r\n")}
        fixes.open_ai_pull_request(4, "orders", self.ai_proposal(old="class Main {\n  int x = a;", new="class Main {\n  int x = b;"))
        blob = next(payload for method, path, payload in self.calls if method == "POST" and path.endswith("/git/blobs"))
        self.assertEqual(blob["content"], "class Main {\r\n  int x = b;\r\n}\r\n")
        self.calls.clear()
        with self.assertRaises(fixes.FixError) as caught:
            fixes.open_ai_pull_request(4, "orders", self.ai_proposal(old="int y = a;"))
        self.assertEqual(caught.exception.status, 409)
        self.assert_no_writes()

    def test_ai_proposal_requires_enablement_and_real_commit(self):
        with patch.dict(os.environ, {"FIX_PR_ENABLED": ""}), self.assertRaises(fixes.FixError):
            fixes.open_ai_pull_request(4, "orders", self.ai_proposal())
        self.client_mock.assert_not_called()
        self.replies = {"/contents/services/orders/src/Main.java": self.contents("int x = a;")}
        self.service_mock.return_value = {**SERVICE, "prod_commit": "local-demo"}
        with self.assertRaises(fixes.FixError) as caught:
            fixes.open_ai_pull_request(4, "orders", self.ai_proposal())
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.calls, [])


class ProposePatchToolTests(unittest.TestCase):
    SOURCE = "class Main {\r\n  int x = a;\r\n  int y = a;\r\n}\r\n"

    def setUp(self):
        from investigator import retrieval
        svc = {"name": "orders", "repo_path": "services/orders", "prod_commit": DEPLOYED}
        self.enterContext(patch.object(tools.config, "service_by_name", return_value=svc))
        self.source = self.enterContext(patch.object(retrieval, "source_text", return_value=self.SOURCE))

    def propose(self, edits, path="src/Main.java"):
        return tools.propose_patch("orders", path, edits, "summary")

    def test_exact_unique_edit_is_accepted_and_repo_prefix_is_optional(self):
        for path in ["src/Main.java", "services/orders/src/Main.java"]:
            with self.subTest(path=path):
                result = self.propose([{"old_text": "int x = a;", "new_text": "int x = b;"}], path)
                self.assertTrue(result["accepted"])
                self.assertEqual((result["path"], result["repo_path"]), ("src/Main.java", "services/orders/src/Main.java"))
        self.assertTrue(self.propose([{"old_text": "{\n  int x = a;", "new_text": "{\n  int x = b;"}])["accepted"])

    def test_invalid_proposals_are_rejected_with_reasons(self):
        cases = [
            ([{"old_text": "int z;", "new_text": "x"}], "found 0"),
            ([{"old_text": "= a;", "new_text": "= b;"}], "found 2"),
            ([{"old_text": "int x = a;", "new_text": "String password = \"hunter22\";"}], "credential"),
            ([{"old_text": "int x = a;", "new_text": "int x = a;"}], "do not change"),
            ([{"old_text": "", "new_text": "x"}], "must not be empty"),
            ([], "1 to"),
        ]
        for edits, reason in cases:
            with self.subTest(reason=reason):
                result = self.propose(edits)
                self.assertNotIn("accepted", result)
                self.assertIn(reason, result["error"])
        for path in ["../Main.java", "src/.env", "/etc/passwd"]:
            with self.subTest(path=path):
                self.assertIn("error", self.propose([{"old_text": "int x = a;", "new_text": "b"}], path))
        self.source.return_value = None
        self.assertIn("not found", self.propose([{"old_text": "int x = a;", "new_text": "b"}])["error"])

    def test_tool_never_contacts_github(self):
        with patch.object(fixes.httpx, "Client") as client, patch.object(fixes.httpx, "get") as get:
            self.propose([{"old_text": "int x = a;", "new_text": "int x = b;"}])
        client.assert_not_called()
        get.assert_not_called()
        self.assertIn("propose_patch", tools.TOOLS_BY_CATEGORY["code"])
        self.assertNotIn("propose_patch", tools.TOOLS_BY_CATEGORY["infra"])


if __name__ == "__main__":
    unittest.main()