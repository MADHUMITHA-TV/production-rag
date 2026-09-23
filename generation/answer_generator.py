"""
Answer generation: retrieved chunks + question -> cited answer.

LLM BACKEND CHOICE
==================
Production: Groq's free-tier API (e.g. llama-3.1-8b-instant or
llama-3.3-70b-versatile). Free, fast, no local compute needed. Requires a
GROQ_API_KEY (get one at console.groq.com) and normal internet access.

SANDBOX FALLBACK
=================
This build sandbox's network allowlist includes neither api.groq.com nor
any way to authenticate to api.anthropic.com from bash (no key injected
here). Rather than fake a working LLM call, StubGenerator below does NOT
call any LLM -- it deterministically formats the top retrieved chunk as an
"answer" purely to prove that retrieval -> prompt assembly -> citation
tagging -> citation_enforcer wiring is correct end-to-end. It is explicitly
not a quality signal and should never be used to evaluate faithfulness.

Swap via the GENERATOR env var: "groq" (default, production) or "stub"
(sandbox wiring test only).
"""

import os
import textwrap
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "answer_prompt_v1.yaml"


def load_prompt_config() -> dict:
    with PROMPT_PATH.open() as f:
        return yaml.safe_load(f)


def build_context_block(chunks: list[dict], template: str) -> str:
    blocks = []
    for c in chunks:
        blocks.append(
            template.format(
                chunk_id=c["chunk_id"],
                source_file=c["source_file"],
                breadcrumb=c["breadcrumb"],
                text=c["text"],
            )
        )
    return "\n\n---\n\n".join(blocks)


class Generator(ABC):
    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str, params: dict) -> str:
        ...


class GroqGenerator(Generator):
    """Production generator using Groq's free-tier hosted inference."""

    def __init__(self, model: str = "openai/gpt-oss-20b"):
        # Imported lazily so this module doesn't hard-require the `groq`
        # package (or an API key) when running in stub mode.
        from groq import Groq

        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY not set. Get a free key at console.groq.com "
                "and export it before running with GENERATOR=groq."
            )
        self.client = Groq(api_key=api_key)
        self.model = model

    def generate(self, system_prompt: str, user_prompt: str, params: dict) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=params.get("temperature", 0.1),
            max_tokens=params.get("max_tokens", 800),
        )
        # Recorded so callers can detect truncation (finish_reason == "length")
        # rather than silently showing a cut-off answer as if it were complete.
        self.last_finish_reason = response.choices[0].finish_reason
        return response.choices[0].message.content


class StubGenerator(Generator):
    """
    Sandbox-only. No LLM call. Deterministically builds a fake 'answer' by
    quoting the top retrieved chunk with a citation, so the surrounding
    pipeline (retrieval -> prompt -> citation check) can be tested without
    network access to a real LLM provider.
    """

    def generate(self, system_prompt: str, user_prompt: str, params: dict) -> str:
        # Parse the chunk_id and a short excerpt back out of the user_prompt
        # so the stub "answer" cites something that was actually retrieved.
        import re

        match = re.search(r"Source ID: ([\w\-./]+::\d+)[^\n]*\n(.+?)(?=\n\n---|\Z)", user_prompt, re.DOTALL)
        if not match:
            return "I don't have enough information in the retrieved documentation to answer that."
        chunk_id = match.group(1)
        raw_text = match.group(2).strip()
        # Prefer the first readable prose line over a code fence marker.
        prose_lines = [ln for ln in raw_text.split("\n") if ln.strip() and not ln.strip().startswith("```")]
        excerpt_source = prose_lines[0] if prose_lines else raw_text
        excerpt = textwrap.shorten(excerpt_source.strip(), width=200)
        return (
            f"[STUB ANSWER -- no LLM called] Based on the top retrieved chunk: "
            f"{excerpt} [{chunk_id}]"
        )


def build_generator(kind: str) -> Generator:
    if kind == "groq":
        return GroqGenerator()
    elif kind == "stub":
        return StubGenerator()
    else:
        raise ValueError(f"Unknown generator kind: {kind}")


def generate_answer(question: str, retrieved_chunks: list[dict]) -> tuple[str, set[str], bool]:
    """
    Returns (raw_answer, retrieved_chunk_ids, was_truncated). Caller is
    responsible for passing raw_answer through citation_enforcer.enforce_or_flag
    before showing it to a user. was_truncated is True when the LLM hit
    max_tokens mid-answer (finish_reason == "length") -- callers should
    treat a truncated answer as untrustworthy even if it looks complete,
    since a cut-off code example is worse than an explicit decline.
    """
    config = load_prompt_config()
    context_block = build_context_block(retrieved_chunks, config["context_chunk_template"])
    user_prompt = config["user_prompt_template"].format(
        context_block=context_block, question=question
    )

    generator_kind = os.environ.get("GENERATOR", "groq")
    generator = build_generator(generator_kind)
    answer = generator.generate(
        system_prompt=config["system_prompt"],
        user_prompt=user_prompt,
        params=config["generation_params"],
    )
    was_truncated = getattr(generator, "last_finish_reason", None) == "length"

    retrieved_ids = {c["chunk_id"] for c in retrieved_chunks}
    return answer, retrieved_ids, was_truncated