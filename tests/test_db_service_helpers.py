import unittest
from unittest.mock import MagicMock, patch

import services.db_service as db_service


class TestDBServiceHelpers(unittest.TestCase):
    def make_conn_cursor(self, rowcount=1, fetchone_value=None, fetchall_value=None):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = rowcount
        mock_cursor.fetchone = MagicMock(return_value=fetchone_value)
        mock_cursor.fetchall = MagicMock(return_value=fetchall_value)
        mock_cursor.execute = MagicMock()

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.commit.return_value = None
        mock_conn.rollback.return_value = None
        mock_conn.close.return_value = None
        return mock_conn, mock_cursor

    @patch("services.db_service.get_db_connection")
    def test_update_upload_metadata_updates(self, mock_get_conn):
        conn, cursor = self.make_conn_cursor(rowcount=1)
        mock_get_conn.return_value = conn
        affected = db_service.update_upload_metadata("sess-1", {"title": "T", "department": "D"})
        self.assertEqual(affected, 1)
        cursor.execute.assert_called()

    @patch("services.db_service.get_db_connection")
    def test_update_document_metadata_updates(self, mock_get_conn):
        conn, cursor = self.make_conn_cursor(rowcount=2)
        mock_get_conn.return_value = conn
        affected = db_service.update_document_metadata("path/1", {"final_title": "New", "final_status": "approved"})
        self.assertEqual(affected, 2)
        cursor.execute.assert_called()

    @patch("services.db_service.get_db_connection")
    def test_confirm_document_metadata_returns_id(self, mock_get_conn):
        conn, cursor = self.make_conn_cursor(rowcount=1, fetchone_value=(42,))
        mock_get_conn.return_value = conn
        result = db_service.confirm_document_metadata("path/1", "user1")
        self.assertEqual(result, 42)
        cursor.execute.assert_called()

    @patch("services.db_service.get_db_connection")
    def test_discard_upload_session(self, mock_get_conn):
        conn, cursor = self.make_conn_cursor(rowcount=1)
        mock_get_conn.return_value = conn
        affected = db_service.discard_upload_session("sess-2")
        self.assertEqual(affected, 1)

    @patch("services.db_service.get_db_connection")
    def test_delete_document_record(self, mock_get_conn):
        conn, cursor = self.make_conn_cursor(rowcount=1)
        mock_get_conn.return_value = conn
        deleted = db_service.delete_document_record("path/1")
        self.assertEqual(deleted, 1)


if __name__ == "__main__":
    unittest.main()
