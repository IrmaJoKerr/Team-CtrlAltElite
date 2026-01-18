import os
import asyncio
import unittest


class TestEmbeddingService(unittest.TestCase):
    def test_get_text_embeddings_stub(self):
        # Ensure stub embedder is used for deterministic output
        os.environ['EMBEDDING_PROVIDER'] = 'stub'
        from services.embedding_service import get_text_embeddings

        vecs = asyncio.run(get_text_embeddings(['hello world', 'another text']))
        self.assertIsInstance(vecs, list)
        self.assertEqual(len(vecs), 2)
        self.assertTrue(all(isinstance(v, list) for v in vecs))


if __name__ == '__main__':
    unittest.main()
