import asyncio
import os
import unittest
from unittest.mock import patch, MagicMock


class TestRagService(unittest.TestCase):
    def test_run_rag_query_pgvector(self):
        # Mock embedding to return a simple vector
        async def _fake_embed(texts):
            return [[0.1, 0.2, 0.3]]

        class MockCursor:
            def __init__(self):
                self._rows = [('snippet1', 'path1')]
            def execute(self, sql, params=None):
                pass
            def fetchall(self):
                return self._rows
            def close(self):
                pass

        class MockConn:
            def cursor(self):
                return MockCursor()
            def close(self):
                pass

        with patch('services.embedding_service.get_text_embeddings', new=_fake_embed):
            with patch('services.db_service.get_db_connection', return_value=MockConn()):
                from services.rag_service import run_rag_query
                res = asyncio.run(run_rag_query('hello', None, 3))
                self.assertIn('prompt', res)
                self.assertIsInstance(res['snippets'], list)
                self.assertEqual(len(res['snippets']), 1)

    def test_run_rag_query_fallback_text(self):
        async def _fake_embed(texts):
            return [[0.1, 0.2, 0.3]]

        class MockCursor:
            def __init__(self):
                self._called = 0
            def execute(self, sql, params=None):
                if 'embedding_vector' in sql:
                    raise Exception('pgvector failure')
                self._rows = [('snippet2', 'path2')]
            def fetchall(self):
                return getattr(self, '_rows', [])
            def close(self):
                pass

        class MockConn:
            def cursor(self):
                return MockCursor()
            def close(self):
                pass

        with patch('services.embedding_service.get_text_embeddings', new=_fake_embed):
            with patch('services.db_service.get_db_connection', return_value=MockConn()):
                from services.rag_service import run_rag_query
                res = asyncio.run(run_rag_query('hello', 'dept', 2))
                self.assertIsInstance(res['snippets'], list)
                self.assertEqual(res['snippets'][0]['path'], 'path2')


if __name__ == '__main__':
    unittest.main()
