"""
CLI where you type a question and get an answer with cited source snippets.

Usage (Phase 1, vector-only retrieval -- still the default):
    EMBEDDER=tfidf GENERATOR=stub python3 cli.py "How do I add a request body?"

For real runs (on a machine with normal internet access):
    export GROQ_API_KEY=...
    EMBEDDER=bge GENERATOR=groq python3 cli.py "How do I add a request body?"

Phase 2 -- hybrid retrieval (BM25 + vector via RRF) and cross-encoder
reranking, both opt-in via env vars so Phase 1 behavior is unchanged by
default:
    RETRIEVAL=hybrid EMBEDDER=bge GENERATOR=groq python3 cli.py "..."
    RETRIEVAL=hybrid RERANK=true EMBEDDER=bge GENERATOR=groq python3 cli.py "..."
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import chromadb

from ingestion.embed_and_store import CHROMA_DIR, COLLECTION_NAME, build_embedder, load_chunks
from retrieval.bm25_index import build_bm25_index
from retrieval.hybrid import hybrid_retrieve
from generation.answer_generator import generate_answer
from generation.citation_enforcer import enforce_or_flag

TOP_K = 5


def retrieve_vector_only(question: str, embedder, collection) -> list[dict]:
    """Phase 1 retrieval: pure vector search. Kept as the default path."""
    q_emb = embedder.embed_query(question)
    results = collection.query(query_embeddings=[q_emb], n_results=TOP_K)

    chunks = []
    for doc_id, doc_text, meta in zip(
        results["ids"][0], results["documents"][0], results["metadatas"][0]
    ):
        chunks.append(
            {
                "chunk_id": doc_id,
                "text": doc_text,
                "source_file": meta["source_file"],
                "breadcrumb": meta["breadcrumb"],
            }
        )
    return chunks


def retrieve_hybrid(question: str, embedder, collection, all_chunks: list[dict], rerank: bool) -> list[dict]:
    """Phase 2 retrieval: BM25 + vector fused via RRF, optional cross-encoder rerank."""
    bm25_index = build_bm25_index()
    fused = hybrid_retrieve(
        question, embedder, collection, bm25_index,
        fused_top_k=(20 if rerank else TOP_K),  # wider net if reranking will narrow it
    )

    chunk_lookup = {c["chunk_id"]: c for c in all_chunks}

    if rerank:
        from retrieval.reranker import CrossEncoderReranker
        reranker = CrossEncoderReranker()
        return reranker.rerank(question, fused, chunk_lookup, top_k=TOP_K)

    # No reranking: just take the top RRF-fused results as-is.
    chunks = []
    for r in fused[:TOP_K]:
        chunk = chunk_lookup[r.chunk_id]
        chunks.append(
            {
                "chunk_id": chunk["chunk_id"],
                "text": chunk["text"],
                "source_file": chunk["source_file"],
                "breadcrumb": chunk["breadcrumb"],
                "rrf_score": r.rrf_score,
            }
        )
    return chunks


def main():
    if len(sys.argv) < 2:
        print('Usage: python3 cli.py "your question here"')
        sys.exit(1)
    question = sys.argv[1]

    embedder_kind = os.environ.get("EMBEDDER", "bge")
    retrieval_mode = os.environ.get("RETRIEVAL", "vector")  # "vector" | "hybrid"
    rerank = os.environ.get("RERANK", "false").lower() == "true"

    all_chunks = load_chunks()
    embedder = build_embedder(embedder_kind, [c["text"] for c in all_chunks])

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_collection(COLLECTION_NAME)

    print(f"\nQuestion: {question}\n")
    print(f"Retrieving... (mode={retrieval_mode}, rerank={rerank})")

    if retrieval_mode == "hybrid":
        retrieved = retrieve_hybrid(question, embedder, collection, all_chunks, rerank)
    else:
        retrieved = retrieve_vector_only(question, embedder, collection)

    print(f"Retrieved {len(retrieved)} chunks:")
    for c in retrieved:
        extra = ""
        if "rerank_score" in c:
            extra = f"  [rerank={c['rerank_score']:.3f} rrf={c['rrf_score']:.4f}]"
        elif "rrf_score" in c:
            extra = f"  [rrf={c['rrf_score']:.4f}]"
        print(f"  - {c['chunk_id']}  ({c['breadcrumb']}){extra}")

    print("\nGenerating answer...")
    raw_answer, retrieved_ids, was_truncated = generate_answer(question, retrieved)
    final_answer, check = enforce_or_flag(raw_answer, retrieved_ids)

    print("\n" + "=" * 70)
    print("ANSWER")
    print("=" * 70)
    print(final_answer)
    if was_truncated:
        print("\n[WARNING] This answer was CUT OFF (hit max_tokens before finishing). "
              "Do not trust any code example above as complete -- re-run with a "
              "narrower question or raise max_tokens in the prompt config.")
    print("=" * 70)
    print(f"\n[citation check] coverage={check.citation_coverage:.0%}, "
          f"fabricated={len(check.fabricated_chunk_ids)}, "
          f"declined={check.is_decline}, "
          f"truncated={was_truncated}")

if __name__ == "__main__":
    main()