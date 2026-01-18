import unittest
from unittest.mock import patch, MagicMock
import asyncio
import importlib.util
from pathlib import Path

# Load the ingestion pipeline module by path (service folder name contains a hyphen)
spec = importlib.util.spec_from_file_location(
    "ingestion_pipeline",
    str(Path.cwd() / "docintel-ingestion-service" / "ingestion_pipeline.py"),
)
pipeline_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline_mod)


class TestIngestionPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_run_ingest_pipeline_success(self):
        sample_text = "This is a test document. " * 10
        # Simulate three chunks
        chunks = [sample_text[:50], sample_text[50:100], sample_text[100:150]]
        embeddings = [[0.1, 0.2], [0.2, 0.3], [0.3, 0.4]]

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
                "services.db_service.upsert_chunk_records", return_value=3
            ) as mock_upsert,
            patch(
                "services.storage_service.append_audit", return_value=None
            ) as mock_audit,
        ):
            res = await pipeline_mod.run_ingest_pipeline(
                "test-bucket",
                "test-object.pdf",
                session_meta={"title": "T"},
                enqueue_outbox=False,
            )

            self.assertEqual(res.get("status"), "ok")
            self.assertEqual(res.get("chunks"), len(chunks))
            mock_extract.assert_called()
            mock_chunk.assert_called()
            mock_upsert.assert_called()


if __name__ == "__main__":
    unittest.main()
