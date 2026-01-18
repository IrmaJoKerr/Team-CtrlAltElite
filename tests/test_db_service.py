import os
import unittest
from unittest.mock import patch, MagicMock


class TestDBService(unittest.TestCase):
    @patch("psycopg2.connect")
    def test_env_password_used(self, mock_connect):
        os.environ["DB_PASSWORD"] = "envpw"
        os.environ["DB_USER"] = "u"
        os.environ["DB_NAME"] = "n"
        os.environ.pop("MODE", None)

        from services.db_service import get_db_connection

        conn = get_db_connection()
        mock_connect.assert_called_once()
        args, kwargs = mock_connect.call_args
        self.assertEqual(kwargs.get("password"), "envpw")

    @patch("psycopg2.connect")
    def test_cloud_mode_uses_secrets_adapter(self, mock_connect):
        # Remove env password
        os.environ.pop("DB_PASSWORD", None)
        os.environ["MODE"] = "cloud"
        os.environ["DB_USER"] = "u"
        os.environ["DB_NAME"] = "n"

        with patch(
            "adapters.secrets_adapter.get_db_password", return_value="secretpw"
        ) as mock_get_pw:
            from services.db_service import get_db_connection

            conn = get_db_connection()
            mock_get_pw.assert_called_once()
            args, kwargs = mock_connect.call_args
            self.assertEqual(kwargs.get("password"), "secretpw")

    def test_missing_password_raises(self):
        os.environ.pop("DB_PASSWORD", None)
        os.environ.pop("MODE", None)
        from services.db_service import get_db_connection

        with self.assertRaises(RuntimeError):
            get_db_connection()


if __name__ == "__main__":
    unittest.main()
