"""
Citation enforcement.

The prompt (prompts/answer_prompt_v1.yaml) instructs the LLM to cite
[chunk_id] after every claim and to decline when context is insufficient.
But instructions in a prompt are a request, not a guarantee -- LLMs do
still hallucinate citations (citing a chunk_id that was never retrieved) or
skip citations for a sentence. This module is the enforcement layer that
sits between "the model said it cited its sources" and "we actually verified
it did."

This is deliberately NOT a faithfulness classifier (that's what Ragas does
offline in eval/). This is a fast, cheap, rule-based structural check that
runs on every single request:
  1. Does every [chunk_id] cited in the answer correspond to a chunk that
     was actually retrieved for this query? (Catches fabricated citations.)
  2. Does the answer contain substantive sentences with NO citation at all?
     (Flags likely-ungrounded claims for downstream logging/alerting.)

It does not verify that the citation semantically supports the claim next
to it -- that requires an LLM judge and belongs in the eval pipeline, not
inline in the request path where latency/cost matter.
"""

import re
from dataclasses import dataclass

CITATION_PATTERN = re.compile(r"\[(?:chunk_id:\s*)?([\w\-./]+::\d+)\]")

DECLINE_PHRASES = [
    "don't have enough information",
    "do not have enough information",
    "cannot answer",
    "can't answer",
]


@dataclass
class CitationCheckResult:
    cited_chunk_ids: set[str]
    fabricated_chunk_ids: set[str]  # cited but not in retrieved set
    uncited_sentence_count: int
    total_sentence_count: int
    is_decline: bool

    @property
    def has_fabricated_citations(self) -> bool:
        return len(self.fabricated_chunk_ids) > 0

    @property
    def citation_coverage(self) -> float:
        """Fraction of substantive sentences that carry at least one citation."""
        if self.total_sentence_count == 0:
            return 1.0
        cited = self.total_sentence_count - self.uncited_sentence_count
        return cited / self.total_sentence_count


def _split_sentences(text: str) -> list[str]:
    # Simple sentence splitter -- good enough for coverage estimation, not
    # meant to be linguistically perfect. Skips code blocks so that code
    # lines aren't counted as "uncited sentences" (code is quoted from
    # context directly, not a standalone factual claim needing its own cite).
    text_no_code = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    raw = re.split(r"(?<=[.!?])\s+", text_no_code.strip())
    return [s.strip() for s in raw if s.strip()]


def check_citations(answer: str, retrieved_chunk_ids: set[str]) -> CitationCheckResult:
    cited = set(CITATION_PATTERN.findall(answer))
    fabricated = cited - retrieved_chunk_ids

    is_decline = any(phrase in answer.lower() for phrase in DECLINE_PHRASES)

    sentences = _split_sentences(answer)
    uncited_count = sum(1 for s in sentences if not CITATION_PATTERN.search(s))

    return CitationCheckResult(
        cited_chunk_ids=cited,
        fabricated_chunk_ids=fabricated,
        uncited_sentence_count=uncited_count,
        total_sentence_count=len(sentences),
        is_decline=is_decline,
    )


def enforce_or_flag(answer: str, retrieved_chunk_ids: set[str], min_coverage: float = 0.6) -> tuple[str, CitationCheckResult]:
    """
    Run the citation check and, if it fails hard (fabricated citations),
    replace the answer with a safe decline rather than serving an answer
    that cites documentation that was never actually retrieved. Soft
    failures (low but nonzero coverage) are passed through with the check
    result attached so the caller can log/alert on them.
    """
    result = check_citations(answer, retrieved_chunk_ids)

    if result.has_fabricated_citations:
        safe_answer = (
            "I don't have enough information in the retrieved documentation "
            "to answer that confidently. (Internal note: the generated answer "
            "cited sources that were not part of the retrieved context, so it "
            "was suppressed rather than shown.)"
        )
        return safe_answer, result

    return answer, result
