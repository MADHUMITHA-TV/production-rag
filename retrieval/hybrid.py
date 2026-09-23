"""
Hybrid retrieval: fuse BM25 (keyword) and vector (semantic) results.

WHY RECIPROCAL RANK FUSION (RRF), NOT A WEIGHTED SCORE AVERAGE
================================================================
BM25 scores are unbounded and corpus-dependent (can be 0-30+ depending on
term rarity). Vector cosine similarity is bounded [-1, 1] (typically
0.3-0.9 in practice). Averaging these two raw scores directly is a classic
mistake: whichever score happens to have a larger numeric range silently
dominates the "combination" regardless of which retriever is actually more
relevant for a given query. You'd need to normalize both to a comparable
scale first, and that normalization is itself fragile (depends on the
score distribution of THIS query, not a fixed range).

RRF sidesteps the whole problem by throwing away raw scores entirely and
using only each result's RANK POSITION in its own list:

    RRF_score(doc) = sum over each retriever of  1 / (k + rank_in_that_list)

A document that ranks #1 in both lists gets a high combined score. A
document that ranks #1 in one list but doesn't appear in the other still
scores reasonably (its missing-list contribution is just 0). No score
normalization needed, no tuning of relative weights required to get a
reasonable result. This is why RRF is the standard first choice for hybrid
retrieval in production systems (Elasticsearch, Azure AI Search, and
Weaviate all ship it as their default hybrid fusion method).

k=60 is the constant used in the original RRF paper (Cormack et al., 2009)
and is a widely-used default; it dampens the impact of very low ranks
without needing per-corpus tuning.
"""

from dataclasses import dataclass

RRF_K = 60


@dataclass
class HybridResult:
    chunk_id: str
    rrf_score: float
    vector_rank: int | None  # None if not in vector top-k at all
    bm25_rank: int | None  # None if not in BM25 top-k at all


def reciprocal_rank_fusion(
    vector_ranked_ids: list[str],
    bm25_ranked_ids: list[str],
    k: int = RRF_K,
) -> list[HybridResult]:
    """
    vector_ranked_ids / bm25_ranked_ids: chunk_ids in rank order (best first)
    from each retriever, independently.
    """
    scores: dict[str, float] = {}
    vector_rank_map: dict[str, int] = {}
    bm25_rank_map: dict[str, int] = {}

    for rank, chunk_id in enumerate(vector_ranked_ids):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        vector_rank_map[chunk_id] = rank + 1  # 1-indexed for readability

    for rank, chunk_id in enumerate(bm25_ranked_ids):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        bm25_rank_map[chunk_id] = rank + 1

    results = [
        HybridResult(
            chunk_id=chunk_id,
            rrf_score=score,
            vector_rank=vector_rank_map.get(chunk_id),
            bm25_rank=bm25_rank_map.get(chunk_id),
        )
        for chunk_id, score in scores.items()
    ]
    results.sort(key=lambda r: r.rrf_score, reverse=True)
    return results


def hybrid_retrieve(
    question: str,
    embedder,
    collection,
    bm25_index,
    vector_top_k: int = 20,
    bm25_top_k: int = 20,
    fused_top_k: int = 10,
) -> list[HybridResult]:
    """
    Runs both retrievers independently (wider net than final top_k, since
    fusion needs candidates from both sides to combine), fuses via RRF, and
    returns the fused_top_k best candidates.
    """
    q_emb = embedder.embed_query(question)
    vector_results = collection.query(query_embeddings=[q_emb], n_results=vector_top_k)
    vector_ranked_ids = vector_results["ids"][0]

    bm25_results = bm25_index.query(question, top_k=bm25_top_k)
    bm25_ranked_ids = [chunk_id for chunk_id, _score in bm25_results]

    fused = reciprocal_rank_fusion(vector_ranked_ids, bm25_ranked_ids)
    return fused[:fused_top_k]