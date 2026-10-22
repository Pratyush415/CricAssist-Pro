"""The ChromaDB vector database of context passages."""

import hashlib
from pathlib import Path

from cricassist.context.passages import split_passages
from cricassist.sources.parsers import Document

COLLECTION = "written_context"
BATCH = 100


def default_embedding_function():
    """all-MiniLM-L6-v2 run locally through ONNX: no API key, works offline
    once the model (about 80 MB) has been downloaded on first use."""
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    return DefaultEmbeddingFunction()


class ContextStore:
    def __init__(self, path: Path, embedding_function=None):
        import chromadb

        Path(path).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(path))
        self.embedding_function = embedding_function or default_embedding_function()
        self.collection = self._collection()

    def _collection(self):
        return self.client.get_or_create_collection(
            COLLECTION, embedding_function=self.embedding_function, metadata={"hnsw:space": "cosine"}
        )

    def count(self) -> int:
        return self.collection.count()

    def clear(self) -> None:
        self.client.delete_collection(COLLECTION)
        self.collection = self._collection()

    def add(self, documents: list[Document]) -> int:
        """Split each document into passages and store them. Re-adding a
        document replaces its passages, so builds can be repeated."""
        ids, texts, metadatas = [], [], []
        for doc in documents:
            key = hashlib.sha1(doc.url.encode()).hexdigest()[:16]
            self.collection.delete(where={"doc_key": key})
            for i, passage in enumerate(split_passages(doc.text)):
                ids.append(f"{key}-{i}")
                texts.append(passage)
                metadatas.append({
                    "doc_key": key, "url": doc.url, "title": doc.title, "kind": doc.kind,
                    "date": doc.date or "", "source": doc.source, "ref": doc.ref or "", "passage": i,
                })
        for start in range(0, len(ids), BATCH):
            self.collection.upsert(
                ids=ids[start:start + BATCH], documents=texts[start:start + BATCH], metadatas=metadatas[start:start + BATCH]
            )
        return len(ids)

    def search(self, query: str, n: int = 5, kind: str | None = None) -> list[dict]:
        """The passages closest in meaning to the query, best first."""
        if self.count() == 0:
            return []
        result = self.collection.query(
            query_texts=[query], n_results=min(n, self.count()), where={"kind": kind} if kind else None
        )
        hits = []
        for text, meta, distance in zip(result["documents"][0], result["metadatas"][0], result["distances"][0]):
            hits.append({
                "text": text, "url": meta["url"], "title": meta["title"], "date": meta["date"] or None,
                "kind": meta["kind"], "source": meta["source"], "relevance": round(1 - distance, 3),
            })
        return hits
