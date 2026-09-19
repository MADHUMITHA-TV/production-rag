"""
Phase 1 deliverable: a CLI where you type a question and get an answer with
cited source snippets.

Usage:
    EMBEDDER=tfidf GENERATOR=stub python3 cli.py "How do I add a request body?"

For real runs (on a machine with normal internet access):
    export GROQ_API_KEY=...
    EMBEDDER=bge GENERATOR=groq python3 cli.py "How do I add a request body?"
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import chromadb

from ingestion.embed_and_store import CHROMA_DIR, COLLECTION_NAME, build_embedder, load_chunks
from generation.answer_generator import generate_answer
from generation.citation_enforcer import enforce_or_flag

TOP_K = 5


def retrieve(question: str, embedder, collection) -> list[dict]:
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


def main():
    if len(sys.argv) < 2:
        print('Usage: python3 cli.py "your question here"')
        sys.exit(1)
    question = sys.argv[1]

    embedder_kind = os.environ.get("EMBEDDER", "bge")
    all_chunks = load_chunks()
    embedder = build_embedder(embedder_kind, [c["text"] for c in all_chunks])

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_collection(COLLECTION_NAME)

    print(f"\nQuestion: {question}\n")
    print("Retrieving...")
    retrieved = retrieve(question, embedder, collection)

    print(f"Retrieved {len(retrieved)} chunks:")
    for c in retrieved:
        print(f"  - {c['chunk_id']}  ({c['breadcrumb']})")

    print("\nGenerating answer...")
    raw_answer, retrieved_ids = generate_answer(question, retrieved)
    final_answer, check = enforce_or_flag(raw_answer, retrieved_ids)

    print("\n" + "=" * 70)
    print("ANSWER")
    print("=" * 70)
    print(final_answer)
    print("=" * 70)
    print(f"\n[citation check] coverage={check.citation_coverage:.0%}  "
          f"fabricated={len(check.fabricated_chunk_ids)}  "
          f"declined={check.is_decline}")


if __name__ == "__main__":
    main()
