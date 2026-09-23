"""
Cross-encoder reranking of hybrid-retrieved candidates.

WHY RERANK AT ALL, IF WE ALREADY HAVE HYBRID RETRIEVAL
========================================================
Both BM25 and the bi-encoder (bge) embed the QUERY and each DOCUMENT
independently, then compare vectors/scores. This is fast (embed once,
compare against a pre-built index) but structurally limited: the model
never actually looks at the query and a specific document TOGETHER, so it
can't reason about fine-grained interactions between them.

A cross-encoder does the opposite: it takes (query, document) as a single
joint input and outputs one relevance score for that specific pair. This
is far more accurate at judging "does this specific chunk actually answer
this specific question" -- but it's also much more expensive, since you
must run one full forward pass PER CANDIDATE DOCUMENT, with no
precomputable index. That cost is exactly why it's used as a second-stage
reranker over a small candidate set (10-20 from hybrid retrieval) rather
than as the primary retriever over the whole corpus (215+ chunks, or
millions in a real production corpus) -- running a cross-encoder over an
entire corpus per query would defeat the point of having an index at all.

MODEL CHOICE
============
cross-encoder/ms-marco-MiniLM-L-6-v2: a small (~80MB), fast, widely-used
cross-encoder fine-tuned specifically for passage reranking (trained on
MS MARCO, a large-scale passage-ranking dataset). Good relevance/latency
tradeoff for a CPU-only local setup; larger cross-encoders exist if
latency isn't a constraint.

NETWORK NOTE (same constraint as BGEEmbedder)
==============================================
Like the BGE embedder, this needs to download model weights from Hugging
Face on first use -- not available in the original sandbox build
environment, verified to work once run with normal internet access.
"""

from pathlib import Path

from retrieval.hybrid import HybridResult


class CrossEncoderReranker:
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_name)

    def rerank(
        self,
        question: str,
        candidates: list[HybridResult],
        chunk_lookup: dict[str, dict],
        top_k: int = 5,
    ) -> list[dict]:
        """
        chunk_lookup: chunk_id -> full chunk dict (from data/chunks.jsonl),
        so we can pass actual chunk text to the cross-encoder.

        Returns the top_k chunks (as dicts, same shape as retrieve() in
        cli.py) sorted by cross-encoder relevance score, with the score
        attached for transparency/debugging.
        """
        pairs = [(question, chunk_lookup[c.chunk_id]["text"]) for c in candidates]
        scores = self.model.predict(pairs)

        scored = list(zip(candidates, scores))
        scored.sort(key=lambda pair: pair[1], reverse=True)

        reranked = []
        for hybrid_result, score in scored[:top_k]:
            chunk = chunk_lookup[hybrid_result.chunk_id]
            reranked.append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "text": chunk["text"],
                    "source_file": chunk["source_file"],
                    "breadcrumb": chunk["breadcrumb"],
                    "rerank_score": float(score),
                    "rrf_score": hybrid_result.rrf_score,
                }
            )
        return reranked