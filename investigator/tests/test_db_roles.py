import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from investigator import config, store


class DatabaseRoleTests(unittest.TestCase):
    def tearDown(self):
        importlib.reload(config)

    def test_missing_readonly_password_never_falls_back(self):
        with patch.dict(os.environ, {"RO_PASSWORD": "", "DATABASE_URL": "postgresql://app@localhost/incidents",
                                     "READONLY_DATABASE_URL": "postgresql://app@localhost/incidents"}):
            importlib.reload(config)
            self.assertEqual(config.RO_DB_DSN, "")
            with patch.object(store.psycopg, "connect") as connect:
                with self.assertRaisesRegex(RuntimeError, "RO_PASSWORD is required"):
                    store.db(config.RO_DB_DSN)
                connect.assert_not_called()

    def test_readonly_password_uses_dedicated_role(self):
        with patch.dict(os.environ, {"RO_PASSWORD": "placeholder+/@"}):
            importlib.reload(config)
            self.assertEqual(config.RO_DB_DSN,
                             "postgresql://investigator_ro:placeholder%2B%2F%40@postgres:5432/incidents")

    def test_store_creates_only_investigator_tables(self):
        self.assertNotIn("create schema", store.SCHEMA.lower())
        self.assertNotIn("code_index", store.SCHEMA.lower())


if __name__ == "__main__":
    unittest.main()