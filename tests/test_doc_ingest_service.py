from services.doc_ingest_service import chunk_text


def test_chunk_text_basic():
    text = "0123456789" * 200
    chunks = chunk_text(text, chunk_size=100, overlap=10)
    assert len(chunks) > 0
    # ensure chunks reconstructible (approx)
    assert text[:100] == chunks[0]
