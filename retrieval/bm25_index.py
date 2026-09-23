"""
BM25 keyword retrieval over the chunk corpus.

WHY BM25 ALONGSIDE VECTOR SEARCH
================================
Dense embeddings (bge) are excellent at semantic/paraphrase matching ("how
do I validate input" ~ "declare a Pydantic model") but are comparatively
weak at exact lexical matches on rare, specific tokens -- exact function
names, parameter names, error codes, class names (e.g. "APIRoute",
"Depends", "HTTPException"). BM25 is the opposite: it excels at exact
keyword/term overlap and is indifferent to semantic meaning. Combining
both (see hybrid.py) covers each other's blind spots, which is why hybrid
retrieval is close to a default expectation in production RAG systems,
not an optional nice-to-have.

TOKENIZATION
============
Deliberately simple: lowercase + split on non-alphanumeric boundaries, no
stemming. A real production system might add stemming (e.g. Porter
stemmer) or a proper tokenizer, but simple whitespace/punctuation
tokenization is what the original BM25 papers assume and is good enough
to demonstrate the retrieval-fusion architecture. Documented here rather
than hidden so the tradeoff is explicit, not accidental.
"""

import json
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

CHUNKS_PATH = Path(__file__).parent.parent / "data" / "chunks.jsonl"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class BM25Index:
    """Wraps rank_bm25's BM25Okapi with chunk_id-aware querying."""

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        self.chunk_ids = [c["chunk_id"] for c in chunks]
        tokenized_corpus = [tokenize(c["text"]) for c in chunks]
        self.bm25 = BM25Okapi(tokenized_corpus)

    def query(self, question: str, top_k: int = 10) -> list[tuple[str, float]]:
        """Returns [(chunk_id, bm25_score), ...] sorted by score desc."""
        tokenized_query = tokenize(question)
        scores = self.bm25.get_scores(tokenized_query)
        ranked = sorted(
            zip(self.chunk_ids, scores), key=lambda pair: pair[1], reverse=True
        )
        return ranked[:top_k]


def load_chunks() -> list[dict]:
    with CHUNKS_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def build_bm25_index() -> BM25Index:
    chunks = load_chunks()
    return BM25Index(chunks)


if __name__ == "__main__":
    # Quick manual smoke test.
    index = build_bm25_index()
    results = index.query("APIRoute custom request class", top_k=5)
    for chunk_id, score in results:
        print(f"{score:.3f}  {chunk_id}")