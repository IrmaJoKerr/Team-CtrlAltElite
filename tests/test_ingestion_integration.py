import unittest
from unittest.mock import patch
import importlib.util
from pathlib import Path
from adapters.storage_adapter import get_storage_adapter

# Load the ingestion pipeline module directly
spec = importlib.util.spec_from_file_location(
    "ingestion_pipeline",
    str(Path.cwd() / "docintel-ingestion-service" / "ingestion_pipeline.py"),
)
pipeline_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline_mod)


class TestIngestionIntegration(unittest.TestCase):
    def test_upload_and_pipeline_end_to_end(self):
        # Use the local adapter to upload a file (simulate /upload)
        adapter = get_storage_adapter()
        object_name = "itest_upload_test.txt"
        adapter.upload_bytes(object_name, b"integration content", "text/plain")

        # Prepare fake pipeline components
        sample_text = "This is the uploaded content." * 5
        chunks = [sample_text[:40], sample_text[40:80]]
        embeddings = [[0.1, 0.2], [0.2, 0.3]]

        async def _fake_embed(texts):
            return embeddings

        with (
            patch(
                "services.storage_service.extract_text_from_pdf_gs_uri",
                return_value=sample_text,
            ) as mock_extract,
            patch(
                "services.doc_ingest_service.chunk_text", return_value=chunks
            ) as mock_chunk,
            patch(
                "services.embedding_service.get_text_embeddings", new=_fake_embed
            ) as mock_embed,
            patch(
                "services.db_service.upsert_chunk_records", return_value=2
            ) as mock_upsert,
            patch(
                "services.storage_service.append_audit", return_value=None
            ) as mock_audit,
        ):
            # Run pipeline directly and wait for completion
            import asyncio

            res = asyncio.run(
                pipeline_mod.run_ingest_pipeline(
                    None,
                    object_name,
                    session_meta={"title": "IT"},
                    enqueue_outbox=False,
                )
            )

            self.assertEqual(res.get("status"), "ok")
            self.assertEqual(res.get("chunks"), len(chunks))
            mock_extract.assert_called()
            mock_chunk.assert_called()
            mock_upsert.assert_called()


if __name__ == "__main__":
    unittest.main()
