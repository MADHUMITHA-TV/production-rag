"""
Embed chunks and store them in a local ChromaDB collection.

EMBEDDER CHOICE
================
Production embedder: BAAI/bge-small-en-v1.5 via sentence-transformers.
  - Free, runs locally, no per-call cost.
  - bge models are trained specifically for asymmetric retrieval (short
    query vs. longer passage), and expect a query-side instruction prefix
    ("Represent this sentence for searching relevant passages:") that is
    NOT applied to the documents being indexed. Get this backwards and
    retrieval quality drops -- this is a common real-world bug, and a good
    interview talking point in itself.

DEV/SANDBOX FALLBACK
=====================
This build environment's network allowlist does not include
huggingface.co, so `sentence-transformers` can't download model weights
here (confirmed: HTTP 403, host_not_allowed). Rather than leave the
pipeline untestable, this module exposes an Embedder interface with two
implementations:
  - BGEEmbedder      -> the real one, for you to run anywhere with normal
                        internet access (e.g. your own machine).
  - TfidfEmbedder     -> a pure scikit-learn fallback with zero external
                        downloads, used ONLY to validate that chunking ->
                        embedding -> vector store -> retrieval works
                        end-to-end in this sandbox. It is not a serious
                        semantic embedder (no notion of synonyms/meaning,
                        just weighted term overlap) and should not be used
                        for your actual evaluation numbers.

Switch which one is active via the EMBEDDER env var: "bge" (default,
production) or "tfidf" (sandbox testing only).
"""

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

import chromadb

CHUNKS_PATH = Path(__file__).parent.parent / "data" / "chunks.jsonl"
CHROMA_DIR = Path(__file__).parent.parent / "data" / "chroma_db"
COLLECTION_NAME = "fastapi_docs"

BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(ABC):
    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passages for storage. No query prefix applied."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a single query. Prefix applied where the model expects it."""


class BGEEmbedder(Embedder):
    """Production embedder. Requires sentence-transformers + HF network access."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        prefixed = BGE_QUERY_PREFIX + text
        return self.model.encode([prefixed], normalize_embeddings=True)[0].tolist()


class TfidfEmbedder(Embedder):
    """
    Sandbox-only fallback. Fits a TF-IDF vectorizer over the corpus so we
    can exercise the full retrieval pipeline without any model download.
    NOT a semantic embedder -- purely for plumbing validation.
    """

    def __init__(self, corpus_texts: list[str], max_features: int = 2048):
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.vectorizer = TfidfVectorizer(max_features=max_features, stop_words="english")
        self.vectorizer.fit(corpus_texts)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.vectorizer.transform(texts).toarray().tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.vectorizer.transform([text]).toarray()[0].tolist()


def load_chunks() -> list[dict]:
    with CHUNKS_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def build_embedder(kind: str, chunk_texts: list[str]) -> Embedder:
    if kind == "bge":
        return BGEEmbedder()
    elif kind == "tfidf":
        return TfidfEmbedder(chunk_texts)
    else:
        raise ValueError(f"Unknown embedder kind: {kind}")


def run():
    embedder_kind = os.environ.get("EMBEDDER", "bge")
    chunks = load_chunks()
    texts = [c["text"] for c in chunks]

    print(f"Loaded {len(chunks)} chunks. Using embedder: {embedder_kind}")
    if embedder_kind == "tfidf":
        print("WARNING: TF-IDF fallback is for sandbox pipeline validation only. "
              "Set EMBEDDER=bge on a machine with HF network access for real runs.")

    embedder = build_embedder(embedder_kind, texts)

    print("Embedding chunks...")
    embeddings = embedder.embed_documents(texts)

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    # Drop and recreate for idempotent re-runs during development.
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"embedder": embedder_kind},
    )

    collection.add(
        ids=[c["chunk_id"] for c in chunks],
        embeddings=embeddings,
        documents=texts,
        metadatas=[
            {
                "source_file": c["source_file"],
                "breadcrumb": c["breadcrumb"],
                "chunk_index": c["chunk_index"],
                "token_count": c["token_count"],
            }
            for c in chunks
        ],
    )

    print(f"Stored {collection.count()} chunks in ChromaDB at {CHROMA_DIR}")

    # Persist which embedder kind was used, so retrieval code can build a
    # matching query embedder without guessing.
    (CHROMA_DIR / "embedder_kind.txt").write_text(embedder_kind)


if __name__ == "__main__":
    run()
