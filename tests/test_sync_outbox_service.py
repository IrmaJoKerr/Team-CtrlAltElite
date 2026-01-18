import unittest
from unittest.mock import patch, MagicMock


class TestSyncOutboxService(unittest.TestCase):
    @patch("services.db_sync_service.acquire_advisory_lock")
    @patch("services.db_sync_service.release_advisory_lock")
    @patch("services.db_sync_service.fetch_batch_from_db")
    @patch("services.db_sync_service.mark_synced")
    @patch("adapters.qdrant_adapter.upsert_points")
    def test_run_once_success(
        self, mock_upsert, mock_mark, mock_fetch, mock_release, mock_acquire
    ):
        mock_acquire.return_value = True
        mock_fetch.return_value = [
            {
                "id": 1,
                "vector": [0.1, 0.2],
                "title": "T",
                "department": "D",
                "gcs_path": "p",
            }
        ]
        mock_upsert.return_value = True

        from services.sync_outbox_service import SyncOutboxService

        svc = SyncOutboxService("col", "http://q")
        conn = MagicMock()
        ok = svc.run_once(conn, batch_size=10, dry_run=True)
        self.assertTrue(ok)
        mock_acquire.assert_called()
        mock_fetch.assert_called_with(conn, 10)
        mock_upsert.assert_called()
        mock_mark.assert_called_with(conn, [1])

    @patch("services.db_sync_service.acquire_advisory_lock")
    @patch("services.db_sync_service.release_advisory_lock")
    @patch("services.db_sync_service.fetch_batch_from_db")
    @patch("adapters.qdrant_adapter.upsert_points")
    def test_run_once_no_rows(
        self, mock_upsert, mock_fetch, mock_release, mock_acquire
    ):
        mock_acquire.return_value = True
        mock_fetch.return_value = []

        from services.sync_outbox_service import SyncOutboxService

        svc = SyncOutboxService("col", "http://q")
        conn = MagicMock()
        ok = svc.run_once(conn, batch_size=10, dry_run=True)
        self.assertTrue(ok)
        mock_upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
