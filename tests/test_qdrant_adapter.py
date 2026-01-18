import unittest
from unittest.mock import patch, MagicMock
import adapters.qdrant_adapter as qa


class TestQdrantAdapter(unittest.TestCase):
    @patch('adapters.qdrant_adapter.requests.put')
    def test_upsert_retries_and_success(self, mock_put):
        # First two attempts raise exception, third returns success
        mock_put.side_effect = [Exception('net'), Exception('net2'), MagicMock(status_code=200, text='ok')]
        points = [{'id': 1, 'vector': [0.1,0.2], 'payload': {'title':'t'}}]
        ok = qa.upsert_points(points, 'col', 'http://q', dry_run=False, retries=3, backoff=0.01)
        self.assertTrue(ok)

    @patch('adapters.qdrant_adapter.requests.put')
    def test_upsert_failure_after_retries(self, mock_put):
        mock_put.side_effect = Exception('net')
        points = [{'id': 1, 'vector': [0.1,0.2], 'payload': {'title':'t'}}]
        ok = qa.upsert_points(points, 'col', 'http://q', dry_run=False, retries=2, backoff=0.01)
        self.assertFalse(ok)

    def test_dry_run(self):
        points = [{'id': 1, 'vector': [0.1,0.2], 'payload': {'title':'t'}}]
        ok = qa.upsert_points(points, 'col', 'http://q', dry_run=True)
        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
