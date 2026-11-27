import copy
import json
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from indexer import index_code as indexer
from investigator import retrieval
from tests.rag_compare import compare, estimate_tokens


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class MemoryConnection:
    def __init__(self):
        self.tables = {name: [] for name in ("rag_indexes", "rag_sources", "rag_chunks", "rag_embedding_cache", "rag_vectors")}
        self.fail_chunks = False

    @contextmanager
    def transaction(self):
        before = copy.deepcopy(self.tables)
        try:
            yield
        except Exception:
            self.tables = before
            raise

    def execute(self, sql, params=()):
        if sql.strip().lower().startswith(("create ", "alter ", "drop ")):
            raise AssertionError("Indexer must not execute runtime DDL")
        if sql.startswith("select distinct dimensions"):
            return Result(sorted({(row[3],) for row in self.tables["rag_embedding_cache"] if row[1:3] == params}))
        if sql.startswith("select embedding_json"):
            return Result([(row[4],) for row in self.tables["rag_embedding_cache"] if row[:4] == params])
        if sql.startswith("delete from"):
            table = sql.split()[2].split(".")[1]
            self.tables[table] = [row for row in self.tables[table] if row[:2] != params]
        if sql.startswith("insert into"):
            table = sql.split()[2].split(".")[1]
            if table == "rag_chunks" and self.fail_chunks:
                raise RuntimeError("injected membership write failure")
            if table in {"rag_vectors", "rag_embedding_cache"}:
                if any(row[:4] == params[:4] for row in self.tables[table]):
                    return Result()
            self.tables[table].append(params)
        return Result()


class IndexTests(unittest.TestCase):
    def test_storage_preflight_is_read_only_and_requires_bootstrap(self):
        calls = []

        def execute(sql, params=()):
            calls.append((sql, params))
            return Result([(True,)])

        indexer.check_storage(SimpleNamespace(execute=execute))
        self.assertEqual(len(calls), len(indexer.RAG_TABLES) + 1)
        self.assertTrue(all(sql.startswith("select ") for sql, _ in calls))
        with self.assertRaisesRegex(RuntimeError, "Bootstrap"):
            indexer.check_storage(SimpleNamespace(execute=lambda *args: Result([(None,)])))
        with self.assertRaisesRegex(RuntimeError, "pgvector"):
            indexer.check_storage(SimpleNamespace(execute=lambda sql, *args: Result([
                (False if "pg_extension" in sql else True,)])))

    def test_ast_boundaries_and_original_lines(self):
        text = ('package demo;\n@Deprecated\nclass Outer {\n Outer() {}\n'
                ' String multiline(\n int value) { return "}"; }\n'
                ' class Inner { void nested() {} }\n}\nrecord Item(int value) { Item {} }\n')
        chunks = list(indexer.chunk_java(Path("Outer.java"), text))
        self.assertTrue(any(row[2] == "Outer.multiline" and row[:2] == (5, 6) for row in chunks))
        self.assertTrue(any(row[2] == "Outer.Inner.nested" for row in chunks))
        self.assertTrue(any(row[3] == "compact_constructor_declaration" for row in chunks))
        for start, end, _, _, content in chunks:
            self.assertEqual(content, "".join(text.splitlines(keepends=True)[start - 1:end]))

    def test_invalid_java_fails_closed(self):
        with self.assertRaises(ValueError):
            list(indexer.chunk_java(Path("Bad.java"), "class Bad { void broken( {"))

    def test_synthetic_commit_is_explicit(self):
        with self.assertRaises(ValueError):
            indexer.verify_checkout({"prod_commit": "local-demo"}, Path("."))
        self.assertEqual(indexer.verify_checkout({"prod_commit": "local-demo", "commit_kind": "synthetic"}, Path(".")),
                         (None, "synthetic-local-demo"))

    def test_git_sha_must_match_checkout_and_be_clean(self):
        svc = {"prod_commit": "a" * 40, "commit_kind": "git", "repo_path": "services/test"}
        with patch.object(indexer, "MANIFEST_PATH", Path("missing-rag-manifest.json")), patch.dict(indexer.os.environ, {"SOURCE_MANIFEST_B64": ""}):
            with patch.object(indexer.subprocess, "check_output", return_value="b" * 40):
                with self.assertRaisesRegex(ValueError, "HEAD"):
                    indexer.verify_checkout(svc, Path("."))
            with patch.object(indexer.subprocess, "check_output", side_effect=["a" * 40, " M source.java"]):
                with self.assertRaisesRegex(ValueError, "uncommitted"):
                    indexer.verify_checkout(svc, Path("."))

    def test_baked_manifest_rejects_wrong_source(self):
        import base64

        svc = {"name": "test", "prod_commit": "a" * 40, "commit_kind": "git"}
        encoded = base64.b64encode(json.dumps({"commit": "a" * 40, "services": {"test": {}}}).encode()).decode()
        with patch.dict(indexer.os.environ, {"SOURCE_MANIFEST_B64": encoded}), patch.object(indexer, "source_snapshot", return_value={"Test.java": "changed"}):
            with self.assertRaisesRegex(ValueError, "manifest"):
                indexer.verify_checkout(svc, Path("."))

    def test_cache_reuse_creates_every_commit_membership(self):
        content = "void reserve() {}"
        chunk = ("Test.java", 2, 2, "Test.reserve", "method_declaration", content, indexer.digest(content))
        prepared = (None, "synthetic-local-demo", [("Test.java", content, indexer.digest(content))], [chunk, chunk])
        conn = MemoryConnection()
        with patch.object(indexer, "prepare_service", return_value=prepared), patch.object(indexer, "PROVIDER", "openai"), patch.object(indexer, "MODEL", "versioned-model"), patch.object(indexer, "embed", return_value=[[1.0, 0.0]]) as embed:
            indexer.index_service(conn, {"name": "test", "prod_commit": "first"})
            indexer.index_service(conn, {"name": "test", "prod_commit": "second"})
            self.assertEqual(embed.call_count, 1)
        self.assertEqual(len(conn.tables["rag_chunks"]), 4)
        self.assertEqual(len(conn.tables["rag_embedding_cache"]), 1)
        self.assertEqual(conn.tables["rag_indexes"][-1][4:7], ("openai", "versioned-model", 2))

    def test_failure_keeps_prior_ready_snapshot_and_removes_stale_members(self):
        content = "class Test {}"
        chunk = ("Test.java", 1, 1, "Test", "class_declaration", content, indexer.digest(content))
        prepared = (None, "synthetic-local-demo", [("Test.java", content, indexer.digest(content))], [chunk])
        svc = {"name": "test", "prod_commit": "local-demo"}
        conn = MemoryConnection()
        with patch.object(indexer, "prepare_service", return_value=prepared), patch.object(indexer, "PROVIDER", "none"):
            indexer.index_service(conn, svc)
            before = copy.deepcopy(conn.tables)
            conn.fail_chunks = True
            with self.assertRaises(RuntimeError):
                indexer.index_service(conn, svc)
            self.assertEqual(conn.tables, before)
            conn.fail_chunks = False
            conn.tables["rag_chunks"].append(("test", "local-demo", 99, "Deleted.java"))
            indexer.index_service(conn, svc)
            self.assertEqual(len(conn.tables["rag_chunks"]), 1)

    def test_full_source_includes_imports_and_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "package demo;\r\n\r\nclass Test { void run() {} }\r\n"
            (root / "Test.java").write_bytes(text.encode())
            svc = {"name": "test", "repo_path": ".", "prod_commit": "local-demo", "commit_kind": "synthetic"}
            with patch.object(indexer, "SOURCE_ROOT", root):
                _, _, sources, _ = indexer.prepare_service(svc)
            self.assertEqual(sources[0][1], text)

    def test_provider_response_order_and_validation(self):
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": [
            {"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]})
        with patch.object(indexer, "PROVIDER", "openai"), patch.object(indexer, "MODEL", "test"), patch.object(indexer.httpx, "post", return_value=response):
            self.assertEqual(indexer.embed(["first", "second"]), [[1.0, 0.0], [0.0, 1.0]])
            with self.assertRaises(ValueError):
                indexer.embed(["only one"])


class ComparisonTests(unittest.TestCase):
    def test_estimates_count_characters_not_bytes_and_empty_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcde"), 2)
        self.assertEqual(estimate_tokens("\u00e9" * 4), 1)

    def test_comparison_is_offline_and_labels_estimates(self):
        with patch.object(indexer.httpx, "post") as post:
            report = compare(Path(__file__).resolve().parents[1], 5)
        post.assert_not_called()
        self.assertEqual(len(report["cases"]), 4)
        self.assertIn("not a model tokenizer", report["token_usage"])
        for case in report["cases"]:
            self.assertGreater(case["full_java_source_estimated_tokens"], 0)
            self.assertGreater(case["metrics"]["lexical_rag"]["context_estimated_tokens"], 0)
            baseline = case["metrics"]["literal_baseline"]
            if not baseline["returned_chunks"]:
                self.assertEqual(baseline["context_estimated_tokens"], 0)
                self.assertFalse(baseline["hit_at_k"])


class RetrievalConnection:
    def __init__(self, info, rows=()):
        self.info, self.rows, self.calls = info, rows, []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "to_regclass" in sql:
            return Result([{"relation": "code_index.rag_indexes"}])
        if "select * from code_index.rag_indexes" in sql:
            return Result([self.info] if self.info else [])
        return Result(self.rows)

    def transaction(self):
        return nullcontext()


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.info = dict(provider="openai", model="test", dimensions=2, provenance="verified-git-checkout")
        self.settings = SimpleNamespace(EMBEDDING_PROVIDER="none", EMBEDDING_MODEL="test", EMBEDDING_API_KEY="", EMBEDDING_BASE_URL="")

    def test_exact_original_ranges_and_cap(self):
        self.assertEqual(retrieval.source_range("import test;\r\n\r\nclass Test {}\r\n", 1, 2)["content"], "import test;\r\n\r\n")
        self.assertEqual(retrieval.source_range("line\n" * 500, 1, 999)["end_line"], 240)
        self.assertIn("error", retrieval.source_range("one\n", 5, 8))

    def test_lexical_multi_terms_no_provider(self):
        conn = RetrievalConnection(self.info)
        result = retrieval.search_in_connection(conn, "service", "sha", "reserve null quantity", self.settings, -10)
        self.assertEqual(result["mode"], "lexical")
        self.assertEqual(conn.calls[-1][1], (["reserve", "null", "quantity"], "service", "sha", 1))

    def test_missing_index_never_serves_partial_chunks(self):
        conn = RetrievalConnection(None)
        self.assertEqual(retrieval.search_in_connection(conn, "service", "sha", "query", self.settings)["mode"], "missing")
        self.assertEqual(len(conn.calls), 2)

    def test_semantic_is_pinned_to_sha_model_and_dimensions(self):
        self.settings.EMBEDDING_PROVIDER = "openai"
        conn = RetrievalConnection(self.info)
        with patch.object(retrieval, "query_embedding", return_value=[1.0, 0.0]):
            result = retrieval.search_in_connection(conn, "service", "sha", "query", self.settings, 100)
        self.assertEqual(result["mode"], "semantic")
        sql, params = conn.calls[-1]
        self.assertIn("<=>", sql)
        self.assertEqual(params[1:6], ("openai", "test", 2, "service", "sha"))
        self.assertEqual(params[-1], 10)

    def test_dimension_mismatch_falls_back(self):
        self.settings.EMBEDDING_PROVIDER = "openai"
        conn = RetrievalConnection(self.info)
        with patch.object(retrieval, "query_embedding", side_effect=ValueError("dimensions")):
            result = retrieval.search_in_connection(conn, "service", "sha", "query", self.settings)
        self.assertEqual(result["mode"], "lexical")
        self.assertIn("fallback", result["finding"])

    def test_query_vector_validation(self):
        self.settings.EMBEDDING_PROVIDER = "openai"
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": [{"index": 0, "embedding": [float("nan"), 0.0]}]})
        with patch.object(retrieval.httpx, "post", return_value=response), self.assertRaises(ValueError):
            retrieval.query_embedding("query", self.info, self.settings)

    def test_public_read_file_uses_full_source_and_synthetic_never_fetches_github(self):
        svc = {"name": "test", "prod_commit": "local-demo", "commit_kind": "synthetic", "repo_url": "https://github.com/example/repo"}
        config = SimpleNamespace(service_by_name=lambda name: svc, GITHUB_TOKEN="")
        conn = RetrievalConnection(self.info, [{"content": "package demo;\r\n\r\nclass Test {}\r\n"}])
        store = SimpleNamespace(db=lambda: nullcontext(conn))
        with patch("investigator.config", config, create=True), patch("investigator.store", store, create=True), patch.object(retrieval.httpx, "get") as get:
            result = retrieval.read_file("test", "Test.java", 1, 2)
            self.assertEqual(result["content"], "package demo;\r\n\r\n")
            self.assertEqual(result["source"], "code_index")
            conn.info = None
            self.assertIn("error", retrieval.read_file("test", "Test.java"))
            self.assertIn("error", retrieval.read_file("test", "../secret"))
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()