"""Opt-in integration tests against a disposable pgvector database only."""
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

try:
    from indexer import index_code as indexer
    from investigator import retrieval
except ModuleNotFoundError:
    import index_code as indexer
    import retrieval


@unittest.skipUnless(os.getenv("RAG_TEST_DATABASE_URL"), "disposable pgvector database not configured")
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.conn = psycopg.connect(os.environ["RAG_TEST_DATABASE_URL"], autocommit=True)
        indexer.check_storage(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def prepared(self):
        text = "package demo;\r\nclass Test {\r\n void reserve() {}\r\n void charge() {}\r\n}\r\n"
        chunks = [("Test.java", 3, 3, "Test.reserve", "method_declaration", " void reserve() {}\r\n", indexer.digest("reserve")),
                  ("Test.java", 4, 4, "Test.charge", "method_declaration", " void charge() {}\r\n", indexer.digest("charge"))]
        return None, "synthetic-local-demo", [("Test.java", text, indexer.digest(text))], chunks

    def test_vector_order_cache_and_sha_memberships(self):
        import psycopg
        from psycopg.rows import dict_row

        with patch.object(indexer, "prepare_service", side_effect=lambda svc: self.prepared()), patch.object(indexer, "PROVIDER", "openai"), patch.object(indexer, "MODEL", "offline-test-v1"):
            with patch.object(indexer, "embed", side_effect=[[[1.0, 0.0]], [[0.0, 1.0]]]):
                indexer.index_service(self.conn, {"name": "vector-test", "prod_commit": "first"})
            with patch.object(indexer, "embed") as embed:
                indexer.index_service(self.conn, {"name": "vector-test", "prod_commit": "second"})
                embed.assert_not_called()
        self.assertEqual(self.conn.execute("select count(*) from code_index.rag_chunks where service='vector-test'").fetchone()[0], 4)
        settings = SimpleNamespace(EMBEDDING_PROVIDER="openai", EMBEDDING_MODEL="offline-test-v1")
        with psycopg.connect(os.environ["RAG_TEST_DATABASE_URL"], autocommit=True, row_factory=dict_row) as reader:
            with patch.object(retrieval, "query_embedding", return_value=[1.0, 0.0]):
                result = retrieval.search_in_connection(reader, "vector-test", "second", "reserve", settings, 1)
            self.assertEqual(result["chunks"][0]["symbol"], "Test.reserve")
            self.assertAlmostEqual(result["chunks"][0]["score"], 1)
            settings.EMBEDDING_PROVIDER = "none"
            result = retrieval.search_in_connection(reader, "vector-test", "first", "charge", settings, 1)
            self.assertEqual(result["chunks"][0]["symbol"], "Test.charge")
            text = reader.execute("select content from code_index.rag_sources where service='vector-test' and commit_sha='first'").fetchone()["content"]
            self.assertEqual(retrieval.source_range(text, 1, 1)["content"], "package demo;\r\n")

    def test_actual_postgres_rollback_keeps_ready_snapshot(self):
        svc = {"name": "atomic-test", "prod_commit": "local-demo"}
        conn = self.conn

        class FailingConnection:
            def transaction(self):
                return conn.transaction()

            def execute(self, sql, params=()):
                if sql.startswith("insert into code_index.rag_chunks"):
                    raise RuntimeError("injected write failure")
                return conn.execute(sql, params)

        with patch.object(indexer, "prepare_service", side_effect=lambda svc: self.prepared()), patch.object(indexer, "PROVIDER", "none"):
            indexer.index_service(conn, svc)
            before = conn.execute("select ready_at from code_index.rag_indexes where service=%s and commit_sha=%s", (svc["name"], svc["prod_commit"])).fetchone()
            with self.assertRaises(RuntimeError):
                indexer.index_service(FailingConnection(), svc)
            after = conn.execute("select ready_at from code_index.rag_indexes where service=%s and commit_sha=%s", (svc["name"], svc["prod_commit"])).fetchone()
            self.assertEqual(before, after)
            self.assertEqual(conn.execute("select count(*) from code_index.rag_chunks where service='atomic-test'").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)