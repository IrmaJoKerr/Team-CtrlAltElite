from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, Protocol
from datetime import datetime
from pydantic import BaseModel, Field


class DocumentReference(BaseModel):
    bucket: Optional[str] = Field(None, description="Storage bucket or namespace")
    path: str = Field(..., description="Object path within bucket or local adapter")
    uri: Optional[str] = Field(
        None, description="Optional fully-qualified URI (gs:// or file://)"
    )
    version: Optional[str] = None
    last_modified: Optional[datetime] = None


class Chunk(BaseModel):
    id: Optional[str]
    text: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[List[float]] = None


class EmbeddingRequest(BaseModel):
    texts: List[str]
    model: Optional[str] = None


class EmbeddingResponse(BaseModel):
    embeddings: List[List[float]]


class SearchQuery(BaseModel):
    query: str
    top_k: int = 5
    filters: Dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    id: Optional[str]
    score: float
    snippet: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DBRecord(BaseModel):
    id: Optional[str]
    sop_id: Optional[str]
    chunk_id: Optional[str]
    embedding: Optional[List[float]]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class OutboxItem(BaseModel):
    id: Optional[str]
    event_type: str
    payload: Dict[str, Any]
    created_at: Optional[datetime]
    processed_at: Optional[datetime]


class IngestResult(BaseModel):
    document_id: str
    chunks_created: int
    status: str


class RagResponse(BaseModel):
    prompt: str
    snippets: List[SearchResult]


class AnswerResponse(BaseModel):
    answer: str
    sources: List[str] = []
    metadata: Dict[str, Any] = Field(default_factory=dict)


class StorageServiceInterface(ABC):
    @abstractmethod
    def read_bytes(self, object_name: str) -> bytes:
        pass

    @abstractmethod
    def write_bytes(self, object_name: str, data: bytes) -> str:
        pass

    @abstractmethod
    def write_json(self, object_name: str, obj: Any) -> str:
        pass

    @abstractmethod
    def read_json(self, object_name: str) -> Optional[Any]:
        pass

    @abstractmethod
    def list_objects(self, prefix: Optional[str] = None) -> List[str]:
        pass

    @abstractmethod
    def get_latest_timestamp(self, prefix: Optional[str] = None) -> Optional[datetime]:
        pass


class DBServiceInterface(ABC):
    @abstractmethod
    def get_db_connection(self):
        """Return a live DB connection (psycopg2 connection expected).

        Implementations should raise on failure.
        """
        pass

    @abstractmethod
    def upsert_chunk_records(self, records: List[DBRecord]) -> None:
        pass

    @abstractmethod
    def search_vectors(
        self,
        embedding: List[float],
        top_k: int = 5,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[SearchResult]:
        pass


class EmbeddingServiceInterface(ABC):
    @abstractmethod
    async def get_text_embeddings(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[List[float]]:
        pass


class RAGServiceInterface(ABC):
    @abstractmethod
    async def run_rag_query(
        self, query: str, department: Optional[str] = None, top_k: int = 3
    ) -> RagResponse:
        pass

    @abstractmethod
    def upsert_embeddings(self, chunks: List[Chunk]) -> None:
        pass


class SyncOutboxServiceInterface(ABC):
    @abstractmethod
    def run_once(self) -> int:
        """Run a single outbox processing iteration. Return number processed."""
        pass


class GenerativeServiceInterface(ABC):
    @abstractmethod
    async def generate_answer(
        self, prompt: str, context_snippets: List[str]
    ) -> AnswerResponse:
        pass


# Note: this file is a contracts stub to guide the refactor. Implementations live in
# the existing modules under `services/` and `adapters/`. Update this file as contracts
# converge and add typed tests that import and assert behavior against these interfaces.
