"""
Runs every question in eval/golden_set.jsonl through the REAL pipeline
(same retrieval/generation code cli.py uses) and scores the results two
ways:

1. DETERMINISTIC checks (no LLM judge needed -- fully verifiable, no
   network dependency beyond the pipeline itself):
   - retrieval_recall: of the expected_chunk_ids for a question, what
     fraction actually got retrieved? (Did we even give the LLM a chance
     to answer correctly?)
   - decline_correct: for should_decline questions, did the system
     actually decline? For all other questions, did it NOT incorrectly
     decline?
   - citation_coverage / fabricated_count: reused directly from
     citation_enforcer, already proven to work in Phase 2.
   - truncated: reused from Phase 2's truncation detection.

2. RAGAS metrics (LLM-judged, only run on non-decline questions since
   "faithfulness to retrieved context" isn't a meaningful question for a
   query the system correctly refused to answer):
   - faithfulness: are the claims in the generated answer actually
     supported by the retrieved chunks? (independent of whether the
     answer matches the golden reference -- this catches hallucination
     even when the topic is right)
   - context_precision / context_recall: did retrieval pull the right
     chunks, scored against the golden expected_chunk_ids' content?
   - answer_relevancy: does the generated answer actually address the
     question asked (not a tangent)?

WHY SPLIT THIS WAY
===================
Deterministic checks can be fully verified without any LLM judge and are
cheap/fast/reproducible -- there's no reason to pay for an LLM call to
check something a regex or set-overlap can check exactly. Ragas metrics
are reserved for the genuinely fuzzy question of answer quality, where
an LLM judge is actually needed.

LLM/EMBEDDING BACKEND FOR RAGAS
=================================
Ragas needs its own LLM (as a judge) and embedding model (for semantic
similarity metrics like answer_relevancy). Rather than requiring a second
API key, this reuses your existing GROQ_API_KEY via Groq's OpenAI-
compatible endpoint (https://api.groq.com/openai/v1), and reuses the
already-downloaded BGE model for embeddings -- no new dependencies, no
new cost.

NETWORK / VERSION-DRIFT WARNING
=================================
This is the one piece of the whole project that was written without the
ability to run it end-to-end (this dev environment has no route to
api.groq.com or huggingface.co, and Ragas's exact dataset/API shape has
changed across versions). The deterministic half above needs nothing
external and should just work. If the Ragas section throws an import or
schema error on first run, that is expected -- paste the exact traceback
back for a targeted fix rather than guessing blind.
"""

import json
import os
import sys
import types
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).parent.parent))

# --- Workaround for a real ragas==0.4.3 / langchain-community==0.4.2 bug ---
# ragas/llms/base.py unconditionally does:
#     from langchain_community.chat_models.vertexai import ChatVertexAI
# but that submodule no longer exists in current langchain-community (it was
# split out during langchain-community's "sunset" migration). This breaks
# `import ragas` ENTIRELY, regardless of which LLM you actually want to use
# (we use Groq, never Vertex AI). Injecting a harmless stub module before
# ragas is imported anywhere satisfies that import without needing the real
# Vertex AI integration installed. Confirmed via direct reproduction against
# the exact installed versions -- this is not a guess.
_fake_vertexai = types.ModuleType("langchain_community.chat_models.vertexai")


class _StubChatVertexAI:
    """Placeholder only -- never instantiated, we always use Groq."""
    pass


_fake_vertexai.ChatVertexAI = _StubChatVertexAI
sys.modules["langchain_community.chat_models.vertexai"] = _fake_vertexai
# --- end workaround ---

import chromadb

from cli import retrieve_vector_only, retrieve_hybrid
from ingestion.embed_and_store import CHROMA_DIR, COLLECTION_NAME, build_embedder, load_chunks
from generation.answer_generator import generate_answer
from generation.citation_enforcer import enforce_or_flag

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.jsonl"
RESULTS_PATH = Path(__file__).parent / "results.jsonl"
RAGAS_RESULTS_PATH = Path(__file__).parent / "ragas_results.jsonl"


def parse_range(argv: list[str]) -> tuple[int, int] | None:
    """
    Looks for --range START:END (1-indexed, inclusive, matching the
    [i/N] numbers printed during a run) in argv. Returns (start, end) or
    None if not present. Lets a long golden set be worked through across
    multiple shorter sessions instead of needing one fragile end-to-end
    run against a free-tier API with real rate limits.
    """
    for arg in argv:
        if arg.startswith("--range="):
            start_str, end_str = arg.split("=", 1)[1].split(":")
            return int(start_str), int(end_str)
    if "--range" in argv:
        idx = argv.index("--range")
        start_str, end_str = argv[idx + 1].split(":")
        return int(start_str), int(end_str)
    return None


def load_jsonl_by_id(path: Path) -> dict[str, dict]:
    """Loads a jsonl file into {id: record}, or {} if it doesn't exist yet."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    return {r["id"]: r for r in records if "id" in r}


def merge_and_save_jsonl(path: Path, new_records: list[dict], order: list[str]):
    """
    Merges new_records into whatever's already on disk at `path`, keyed by
    `id` (new entries overwrite old ones for the same id -- a re-run of a
    question replaces its prior result). Writes back in `order` (the
    golden set's original order) for readability, skipping ids not present
    in either the existing file or new_records.
    """
    existing = load_jsonl_by_id(path)
    for r in new_records:
        existing[r["id"]] = r

    with path.open("w", encoding="utf-8") as f:
        for id_ in order:
            if id_ in existing:
                f.write(json.dumps(existing[id_], ensure_ascii=False) + "\n")
    return existing


def load_golden_set() -> list[dict]:
    with GOLDEN_SET_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def run_pipeline_for_question(question: str, embedder, collection, all_chunks: list[dict]):
    """Same retrieval + generation path cli.py uses, respecting the same env vars."""
    retrieval_mode = os.environ.get("RETRIEVAL", "vector")
    rerank = os.environ.get("RERANK", "false").lower() == "true"

    if retrieval_mode == "hybrid":
        retrieved = retrieve_hybrid(question, embedder, collection, all_chunks, rerank)
    else:
        retrieved = retrieve_vector_only(question, embedder, collection)

    raw_answer, retrieved_ids, was_truncated = generate_answer(question, retrieved)
    final_answer, check = enforce_or_flag(raw_answer, retrieved_ids)

    return {
        "retrieved": retrieved,
        "retrieved_ids": retrieved_ids,
        "final_answer": final_answer,
        "was_truncated": was_truncated,
        "citation_coverage": check.citation_coverage,
        "fabricated_count": len(check.fabricated_chunk_ids),
        "declined": check.is_decline,
    }


def score_deterministic(pair: dict, run_result: dict) -> dict:
    expected_ids = set(pair["expected_chunk_ids"])
    retrieved_ids = run_result["retrieved_ids"]

    if pair["category"] == "should_decline":
        retrieval_recall = None  # not meaningful -- there's nothing to retrieve
        decline_correct = run_result["declined"] is True
    else:
        retrieval_recall = (
            len(expected_ids & retrieved_ids) / len(expected_ids) if expected_ids else None
        )
        decline_correct = run_result["declined"] is False  # should NOT have declined

    return {
        "retrieval_recall": retrieval_recall,
        "decline_correct": decline_correct,
        "citation_coverage": run_result["citation_coverage"],
        "fabricated_count": run_result["fabricated_count"],
        "truncated": run_result["was_truncated"],
    }


def build_ragas_dataset(records: list[dict]):
    """
    records: list of {question, contexts (list[str]), answer, reference}
    for non-decline pairs only.

    Ragas's exact dataset class/field names have shifted across versions.
    This tries the current (0.2+) EvaluationDataset/SingleTurnSample shape
    first. If your installed ragas version differs, this is the first
    place to look -- the field names below (user_input, retrieved_contexts,
    response, reference) are what needs to match your ragas version's
    schema.
    """
    from ragas.dataset_schema import EvaluationDataset, SingleTurnSample

    samples = [
        SingleTurnSample(
            user_input=r["question"],
            retrieved_contexts=r["contexts"],
            response=r["answer"],
            reference=r["reference"],
        )
        for r in records
    ]
    return EvaluationDataset(samples=samples)


def run_ragas_eval(records: list[dict]):
    """Returns a dict of {metric_name: per_sample_scores list}, or None if
    Ragas evaluation couldn't run (missing key, import/schema mismatch)."""
    if not records:
        print("No non-decline records to score with Ragas -- skipping.")
        return None

    if not os.environ.get("GROQ_API_KEY"):
        print("GROQ_API_KEY not set -- skipping Ragas metrics (deterministic "
              "results above are still valid).")
        return None

    try:
        from langchain_openai import ChatOpenAI
        from ragas import evaluate
        from ragas.llms import LangchainLLMWrapper
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.metrics import Faithfulness, ContextPrecision, ContextRecall, AnswerRelevancy
        from ragas.run_config import RunConfig

        class _DirectSentenceTransformerEmbeddings:
            """
            Minimal embeddings adapter bypassing langchain_community entirely --
            that package's HuggingFaceEmbeddings import triggers a lazy-load
            chain that references optional integrations (e.g. Vertex AI) which
            aren't installed here and break the import. This talks to
            sentence-transformers directly instead, needing nothing else.
            LangchainEmbeddingsWrapper just needs an object with
            embed_documents(list[str]) and embed_query(str) -- it doesn't
            require a real langchain Embeddings subclass.

            NOTE: the attribute name `self.model` is intentionally a STRING
            (the model name), not the SentenceTransformer instance itself.
            Ragas's internal usage-telemetry (EmbeddingUsageEvent) introspects
            `embeddings.model` and validates it as a string -- storing the
            actual model object under that name causes a pydantic
            ValidationError on every single call. The real model instance is
            kept under `self._st_model` instead.
            """

            def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
                from sentence_transformers import SentenceTransformer
                self.model = model_name  # string, for ragas's telemetry
                self._st_model = SentenceTransformer(model_name)

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return self._st_model.encode(texts, normalize_embeddings=True).tolist()

            def embed_query(self, text: str) -> list[float]:
                return self._st_model.encode(text, normalize_embeddings=True).tolist()

        judge_llm = LangchainLLMWrapper(
            ChatOpenAI(
                model="openai/gpt-oss-20b",
                api_key=os.environ["GROQ_API_KEY"],
                base_url="https://api.groq.com/openai/v1",
                temperature=0,
                max_tokens=1024,  # proven sufficient for context_precision,
                # context_recall, and answer_relevancy -- these already
                # scored cleanly (0.99, 1.00, 0.76) at this budget. Kept
                # deliberately lean since Groq's free tier caps a SINGLE
                # request's (prompt + max_tokens) at 8000 tokens (TPM),
                # independent of concurrency -- a 2000-token budget already
                # got a request rejected as "too large" once. faithfulness
                # gets its own separate, larger-budget client below instead
                # of raising this shared one and risking the metrics that
                # already work.
            ),
            bypass_n=True,  # Groq rejects n>1 in a single request; this
            # makes N sequential n=1 calls instead of one n>1 call.
        )

        # faithfulness's NLI verification step returns one verdict object
        # (statement + reason + 0/1 verdict) PER individual factual claim
        # in the answer, all packed into a single JSON response -- and
        # this project's generated answers are often long, multi-section,
        # multi-claim (confirmed by inspecting ragas's own
        # NLIStatementPrompt structure, not a guess). At 1024 tokens that
        # combined output was truncating (LLMDidNotFinishException) on
        # 4/5 real questions. Rather than raising the shared client's
        # budget (which risks breaking context_precision/context_recall/
        # answer_relevancy -- already proven working at 1024, and Groq's
        # free tier already rejected one request as "too large" at just
        # 2000), faithfulness gets its own isolated, larger-budget client.
        faithfulness_llm = LangchainLLMWrapper(
            ChatOpenAI(
                model="openai/gpt-oss-20b",
                api_key=os.environ["GROQ_API_KEY"],
                base_url="https://api.groq.com/openai/v1",
                temperature=0,
                max_tokens=3000,
            ),
            bypass_n=True,
        )
        judge_embeddings = LangchainEmbeddingsWrapper(_DirectSentenceTransformerEmbeddings())

        # ragas.run_config.RunConfig defaults to max_workers=16 -- 16
        # concurrent requests instantly exceed an 8000 TPM free-tier budget,
        # which is what caused the earlier cascade of 413 rate-limit errors
        # followed by a wave of TimeoutErrors (retries waiting on a budget
        # that never had room to recover). max_workers=1 serializes calls
        # so Groq's per-minute budget actually has a chance to refill
        # between requests. This makes the eval run noticeably slower but
        # reliable -- a genuine, real cost/throughput tradeoff worth being
        # able to explain, not a bug to hide.
        throttled_run_config = RunConfig(max_workers=1, timeout=300)

        dataset = build_ragas_dataset(records)
        result = evaluate(
            dataset=dataset,
            metrics=[
                Faithfulness(llm=faithfulness_llm),  # per-metric override --
                # isolated from the shared judge_llm used below.
                ContextPrecision(),
                ContextRecall(),
                AnswerRelevancy(strictness=1),  # default is 3 self-consistency
                # samples; with bypass_n forcing sequential n=1 calls instead
                # of one n=3 call, strictness=3 would triple the API calls
                # for this metric alone. 1 is less robust but keeps eval
                # runs fast and cheap on a free-tier key.
            ],
            llm=judge_llm,
            embeddings=judge_embeddings,
            run_config=throttled_run_config,
        )
        df = result.to_pandas()
        # ragas's own schema has no `id` field, but result rows preserve
        # the input dataset's order -- attach the golden set id back on
        # so per-question ragas scores can be merged/traced across
        # separate --range sessions instead of being anonymous rows.
        df["id"] = [r["id"] for r in records]
        return df

    except Exception as e:
        print(f"\n[Ragas evaluation failed -- see below. Deterministic results "
              f"above are unaffected and still valid.]")
        print(f"Error: {type(e).__name__}: {e}")
        print("If this is an import/schema error, paste this traceback back "
              "for a targeted fix -- ragas's API shape varies by version.")
        return None


def main():
    quick = "--quick" in sys.argv  # shorthand for --range 1:5
    deterministic_only = "--deterministic-only" in sys.argv  # skip Ragas entirely
    # (no LLM-judge multi-sampling cost) -- for fast, cheap CI runs on every
    # push. The full Ragas suite is reserved for manual/scheduled runs, not
    # every commit, given the real rate-limit constraints this project hit.
    allow_partial = "--allow-partial" in sys.argv  # don't require all 25 ids
    # to be present to pass the gate -- for a deliberately partial CI
    # smoke-test subset (e.g. --range 1:5) that's never meant to cover the
    # full golden set on its own. Without this, results.jsonl/
    # ragas_results.jsonl must NOT be committed to the repo (see .gitignore)
    # -- otherwise a smoke test's gate could pass by silently relying on
    # stale committed results for ids it never actually re-verified.
    range_arg = parse_range(sys.argv)

    full_golden_set = load_golden_set()
    full_order = [p["id"] for p in full_golden_set]
    if not all(p["verified"] for p in full_golden_set):
        unverified = [p["id"] for p in full_golden_set if not p["verified"]]
        print(f"WARNING: {len(unverified)} unverified pairs found: {unverified}")
        print("Running anyway, but treat their scores as provisional.\n")

    if quick:
        golden_set = full_golden_set[:5]
        print(f"--quick mode: running only the first {len(golden_set)} pairs\n")
    elif range_arg:
        start, end = range_arg  # 1-indexed, inclusive
        golden_set = full_golden_set[start - 1:end]
        print(f"--range {start}:{end}: running {len(golden_set)} pairs "
              f"({golden_set[0]['id']} .. {golden_set[-1]['id']})\n")
    else:
        golden_set = full_golden_set

    all_chunks = load_chunks()
    embedder_kind = os.environ.get("EMBEDDER", "bge")
    embedder = build_embedder(embedder_kind, [c["text"] for c in all_chunks])
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_collection(COLLECTION_NAME)

    session_results = []
    session_ragas_records = []
    stopped_early = False

    for i, pair in enumerate(golden_set, 1):
        print(f"[{i}/{len(golden_set)}] {pair['id']}: {pair['question'][:60]}...")
        try:
            run_result = run_pipeline_for_question(pair["question"], embedder, collection, all_chunks)
        except Exception as e:
            # A rate-limit error (or any other API failure) partway through
            # a run used to crash the whole script uncaught, silently
            # discarding every result computed so far -- results were only
            # ever written to disk at the very end of main(). For a script
            # that legitimately runs for minutes against an external API
            # that can rate-limit mid-run, losing all prior work on one
            # failure is a real robustness gap, not just bad luck.
            #
            # A RateLimitError specifically means the WHOLE account is out
            # of budget right now -- retrying the next question would just
            # fail identically. So: log it clearly, keep whatever succeeded
            # before this point, merge and save it, and stop early rather
            # than burning through remaining questions on guaranteed
            # failures.
            print(f"    FAILED: {type(e).__name__}: {e}")
            print(f"    Stopping early -- {len(session_results)}/{len(golden_set)} "
                  f"questions in this session completed before this failure. "
                  f"Their results are still merged and saved below.")
            stopped_early = True
            break

        det_scores = score_deterministic(pair, run_result)

        record = {**pair, **det_scores, "generated_answer": run_result["final_answer"]}
        session_results.append(record)

        if pair["category"] != "should_decline":
            session_ragas_records.append(
                {
                    "id": pair["id"],
                    "question": pair["question"],
                    "contexts": [c["text"] for c in run_result["retrieved"]],
                    "answer": run_result["final_answer"],
                    "reference": pair["expected_answer"],
                }
            )

        flag = "OK" if det_scores["decline_correct"] else "MISMATCH"
        recall_str = (
            f"recall={det_scores['retrieval_recall']:.0%}"
            if det_scores["retrieval_recall"] is not None
            else "recall=n/a"
        )
        print(f"    decline_correct={flag}  {recall_str}  "
              f"coverage={det_scores['citation_coverage']:.0%}  "
              f"fabricated={det_scores['fabricated_count']}  "
              f"truncated={det_scores['truncated']}")

    # ---- Ragas for this session's questions only ----
    ragas_df = None
    if deterministic_only:
        print("\n--deterministic-only: skipping Ragas metrics entirely "
              "(no LLM-judge API calls made).")
    else:
        print("\n" + "=" * 70)
        print("RAGAS RESULTS (this session, LLM-judged, non-decline pairs only)")
        print("=" * 70)
        ragas_df = run_ragas_eval(session_ragas_records)
        if ragas_df is not None:
            for metric in ["faithfulness", "context_precision", "context_recall", "answer_relevancy"]:
                if metric in ragas_df.columns:
                    print(f"Mean {metric} (this session): {ragas_df[metric].mean():.2f}")

    # ---- Merge this session's results into the accumulated files ----
    # (rather than overwriting -- lets a 25-question set be worked through
    # across multiple shorter --range sessions instead of needing one
    # fragile end-to-end run against a free-tier API's real rate limits)
    all_det_results = merge_and_save_jsonl(RESULTS_PATH, session_results, full_order)
    print(f"\nDeterministic results merged and saved to {RESULTS_PATH} "
          f"({len(all_det_results)}/{len(full_golden_set)} total questions covered so far)")

    all_ragas_results = {}
    if ragas_df is not None:
        ragas_records_list = ragas_df.to_dict(orient="records")
        all_ragas_results = merge_and_save_jsonl(RAGAS_RESULTS_PATH, ragas_records_list, full_order)
        print(f"Ragas results merged and saved to {RAGAS_RESULTS_PATH} "
              f"({len(all_ragas_results)}/{len(full_golden_set)} total questions covered so far)")
    elif RAGAS_RESULTS_PATH.exists():
        all_ragas_results = load_jsonl_by_id(RAGAS_RESULTS_PATH)

    # ---- Quality gate: evaluated against the FULL accumulated picture, ----
    # ---- not just this session's slice.                                ----
    print("\n" + "=" * 70)
    print("CUMULATIVE STATUS (across all sessions so far)")
    print("=" * 70)

    is_complete = len(all_det_results) == len(full_golden_set)
    missing_ids = [id_ for id_ in full_order if id_ not in all_det_results]
    if missing_ids:
        print(f"INCOMPLETE: {len(all_det_results)}/{len(full_golden_set)} questions "
              f"evaluated so far. Still missing: {missing_ids}")
        print("Run again with --range covering the missing ids to complete the set.")
    else:
        print(f"COMPLETE: all {len(full_golden_set)} questions have been evaluated "
              f"(possibly across multiple sessions).")

    all_results_list = list(all_det_results.values())
    decline_correct_rate = mean(1 if r["decline_correct"] else 0 for r in all_results_list)
    print(f"Decline correctness (cumulative, n={len(all_results_list)}): {decline_correct_rate:.0%}")

    recalls = [r["retrieval_recall"] for r in all_results_list if r["retrieval_recall"] is not None]
    if recalls:
        print(f"Mean retrieval recall (cumulative, n={len(recalls)}): {mean(recalls):.0%}")

    coverages = [r["citation_coverage"] for r in all_results_list]
    total_fabricated = sum(r["fabricated_count"] for r in all_results_list)
    truncated_count = sum(1 for r in all_results_list if r["truncated"])
    print(f"Mean citation coverage (cumulative): {mean(coverages):.0%}")
    print(f"Total fabricated citations (cumulative): {total_fabricated}")
    print(f"Truncated answers (cumulative): {truncated_count}/{len(all_results_list)}")

    non_decline_ids = [p["id"] for p in full_golden_set if p["category"] != "should_decline"]
    ragas_complete = all(id_ in all_ragas_results for id_ in non_decline_ids)
    mean_faithfulness = None
    if all_ragas_results:
        faith_scores = [
            all_ragas_results[id_]["faithfulness"]
            for id_ in non_decline_ids
            if id_ in all_ragas_results and "faithfulness" in all_ragas_results[id_]
        ]
        if faith_scores:
            mean_faithfulness = mean(faith_scores)
            label = "cumulative" if ragas_complete else f"partial, {len(faith_scores)}/{len(non_decline_ids)}"
            print(f"Mean faithfulness ({label}): {mean_faithfulness:.2f}")

    # ---- Failure conditions ----
    # Thresholds below are deliberately strict for decline-correctness and
    # fabrication (these are correctness bugs, not fuzzy quality -- zero
    # tolerance is the right bar), and slightly looser for recall/faithfulness
    # (genuinely fuzzy quality signals where some run-to-run LLM variance is
    # expected and shouldn't fail a build over noise).
    failures = []

    if stopped_early:
        failures.append(
            f"This session stopped early after {len(session_results)}/{len(golden_set)} "
            f"questions due to an API failure."
        )

    if not is_complete and not allow_partial:
        failures.append(
            f"Only {len(all_det_results)}/{len(full_golden_set)} questions evaluated "
            f"so far -- an incomplete set cannot be treated as a full pass."
        )

    if decline_correct_rate < 1.0:
        failures.append(
            f"Decline correctness {decline_correct_rate:.0%} < 100% required "
            f"-- the system answered when it should have declined, or vice versa."
        )

    if total_fabricated > 0:
        failures.append(
            f"{total_fabricated} fabricated citation(s) detected -- zero tolerance."
        )

    if recalls and mean(recalls) < 0.80:
        failures.append(
            f"Mean retrieval recall {mean(recalls):.0%} < 80% threshold -- "
            f"retrieval is missing chunks it should be finding."
        )

    if not deterministic_only and mean_faithfulness is not None and ragas_complete and mean_faithfulness < 0.80:
        failures.append(
            f"Mean faithfulness {mean_faithfulness:.2f} < 0.80 threshold -- "
            f"generated answers are drifting from what retrieved context supports."
        )

    print("\n" + "=" * 70)
    if failures:
        print("QUALITY GATE: FAILED")
        print("=" * 70)
        for f_msg in failures:
            print(f"  - {f_msg}")
        sys.exit(1)
    else:
        print("QUALITY GATE: PASSED")
        print("=" * 70)
        sys.exit(0)


if __name__ == "__main__":
    main()