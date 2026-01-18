import unittest
from unittest.mock import MagicMock
from services.db_sync_service import (
    _parse_vector_text,
    fetch_batch_from_db,
    mark_synced,
)


class TestDBSyncService(unittest.TestCase):
    def test_parse_vector_json(self):
        s = "[0.1, 0.2, 0.3]"
        v = _parse_vector_text(s)
        self.assertEqual(v, [0.1, 0.2, 0.3])

    def test_parse_vector_literal(self):
        s = "(0.1, 0.2, 0.3)"
        v = _parse_vector_text(s)
        self.assertEqual(v, [0.1, 0.2, 0.3])

    def test_fetch_batch_parsing(self):
        # prepare mock connection/cursor
        class MockCursor:
            def __init__(self):
                self._rows = [(1, "c", "[0.1,0.2]", "t", "d", "p", "2026-01-01")]

            def execute(self, *a, **k):
                pass

            def fetchall(self):
                return self._rows

            def close(self):
                pass

        class MockConn:
            def cursor(self):
                return MockCursor()

        rows = fetch_batch_from_db(MockConn(), 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[0]["vector"], [0.1, 0.2])

    def test_mark_synced_executes(self):
        executed = []

        class MockCursor:
            def execute(self, sql, params=None):
                executed.append((sql, params))

            def close(self):
                pass

        class MockConn:
            def __init__(self):
                self._cur = MockCursor()

            def cursor(self):
                return self._cur

            def commit(self):
                executed.append(("commit", None))

        conn = MockConn()
        mark_synced(conn, [5, 6])
        # expect CREATE TABLE + two inserts + commit
        self.assertTrue(
            any(
                "CREATE TABLE IF NOT EXISTS qdrant_sync_state" in e[0] for e in executed
            )
        )
        self.assertTrue(("commit", None) in executed)


if __name__ == "__main__":
    unittest.main()
