import os
import unittest
from adapters.storage_adapter import LocalStorageAdapter


class TestStorageService(unittest.TestCase):
    def setUp(self):
        # ensure STORAGE_ROOT is isolated
        os.environ["STORAGE_ROOT"] = "local_test_store"
        self.adapter = LocalStorageAdapter(root="local_test_store")

    def test_gcs_write_json_and_read(self):
        from services.storage_service import gcs_write_json

        bucket = "test-bucket"
        path = "test/path/obj.json"
        obj = {"a": 1, "b": "x"}

        gcs_write_json(bucket, path, obj)

        # confirm file exists via adapter
        obj_name = f"{bucket}/{path}".lstrip("/")
        data = self.adapter.read_bytes(obj_name)
        self.assertIn(b'"a": 1', data)

    def test_extract_text_from_pdf_missing_raises(self):
        from services.storage_service import extract_text_from_pdf_gs_uri

        with self.assertRaises(RuntimeError):
            extract_text_from_pdf_gs_uri("gs://nonexistent-bucket/nope.pdf")


if __name__ == "__main__":
    unittest.main()
