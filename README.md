# Production RAG — Phase 1 (Local Setup Guide)

Ask-my-docs RAG system over FastAPI's real documentation (72 curated pages:
tutorial, how-to guides, deployment). This guide gets Phase 1 running for
real on your own machine — real embeddings, real LLM answers, real
citations.

## Why run this locally instead of where it was built

This project was built inside a sandboxed environment that couldn't reach
Hugging Face (for embedding model downloads) or Groq's API (for
generation), and didn't have disk space for PyTorch. So it was built and
tested using lightweight stand-ins (TF-IDF instead of real embeddings, a
non-LLM stub instead of a real generator) purely to prove the pipeline
wiring was correct. All the actual engineering — chunking logic, prompt
design, citation enforcement — is real and finished. The two model-backed
pieces just need to run somewhere with normal internet access: your
machine.

---

## 1. Prerequisites

- Python 3.10+
- ~3GB free disk space (sentence-transformers pulls in PyTorch)
- A free Groq API key: sign up at https://console.groq.com → API Keys → Create Key

## 2. Setup

```bash
# From the project root (where this README lives)
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

This installs: chromadb, pyyaml, fastapi/uvicorn (for later), rank_bm25
(Phase 2), sentence-transformers, scikit-learn, groq, ragas, langfuse.
The sentence-transformers install pulls in PyTorch — this step takes a
few minutes and ~2GB.

Set your Groq key so the generation step can call it:

```bash
export GROQ_API_KEY=your_key_here        # Windows: set GROQ_API_KEY=your_key_here
```

## 3. Run the pipeline, step by step

`data/raw/` (the 72 FastAPI doc pages + their referenced code examples) is
present on the original development machine but is intentionally excluded
from version control (see `.gitignore`) to keep the repo lean — it's ~140
third-party doc files, not project code. If you're continuing from this
same machine, it's already there. Setting up fully fresh elsewhere? You'll
need to supply `data/raw/fastapi_docs/` and `data/raw/fastapi_docs_src/`
yourself (a fresh checkout of FastAPI's `docs/en/docs/` and `docs_src/`
folders works). Note that `data/chunks.jsonl` (the post-chunking output)
IS committed, so you can skip straight to Step 3 (embedding) without the
raw corpus if you just want to see retrieval + generation working.

### Step 1 — Preprocess (resolve code macros, clean markdown)

```bash
python3 ingestion/preprocess.py
```

Expected output: `Preprocessed 72 files -> data/processed`

### Step 2 — Chunk

```bash
python3 ingestion/chunker.py
```

Expected output: `Total chunks: 215` (writes `data/chunks.jsonl`)

### Step 3 — Embed and store (this is the one that needs HF access)

```bash
EMBEDDER=bge python3 ingestion/embed_and_store.py
```

First run downloads `BAAI/bge-small-en-v1.5` (~130MB) from Hugging Face,
then embeds all 215 chunks and stores them in a local ChromaDB at
`data/chroma_db/`. Takes a minute or two on CPU.

### Step 4 — Ask a question (this is the one that needs your Groq key)

```bash
GENERATOR=groq EMBEDDER=bge python3 cli.py "How do I add a request body to a path operation?"
```

You should see: retrieved chunk IDs, a real generated answer with inline
`[chunk_id]` citations, and a citation-check summary (coverage %,
fabricated-citation count, whether the model declined).

Try a few more to get a feel for it:

```bash
GENERATOR=groq EMBEDDER=bge python3 cli.py "How do I set up OAuth2 with JWT tokens?"
GENERATOR=groq EMBEDDER=bge python3 cli.py "How do I deploy FastAPI with Docker?"
GENERATOR=groq EMBEDDER=bge python3 cli.py "What's the capital of France?"   # should decline
```

That last one is a good sanity check for the citation-enforcement /
decline behavior — it's not in the FastAPI docs, so it should say it
doesn't have enough information rather than making something up.

## 4. What to look at while testing

- **`data/chunks.jsonl`** — every chunk with its breadcrumb and token count.
  Worth skimming to see how chunking handled real headings/code blocks.
- **`prompts/answer_prompt_v1.yaml`** — the versioned prompt. Try editing
  it (e.g. loosen or tighten the decline rule) and re-running the same
  question to see the effect — this is the "prompts as architecture"
  practice from the spec.
- **`generation/citation_enforcer.py`** — if you want to see fabrication
  get caught, you could temporarily feed it a fake answer with a made-up
  chunk_id and confirm it gets suppressed.

## 5. Re-running after changes

If you edit the corpus or chunker, redo steps 1–3 (embedding is
idempotent — it drops and recreates the ChromaDB collection each run, so
it's always safe to re-run).

## 6. Project layout

```
production-rag/
├── data/
│   ├── raw/fastapi_docs/        # 72 curated source markdown files
│   ├── raw/fastapi_docs_src/    # referenced code examples
│   ├── processed/               # after preprocess.py
│   ├── chunks.jsonl             # after chunker.py
│   └── chroma_db/               # after embed_and_store.py
├── ingestion/
│   ├── preprocess.py
│   ├── chunker.py
│   └── embed_and_store.py
├── generation/
│   ├── answer_generator.py
│   └── citation_enforcer.py
├── prompts/
│   └── answer_prompt_v1.yaml
├── cli.py
└── requirements.txt
```

## 7. Next: Phase 2

Once this is running and you've kicked the tires on a handful of
questions, we'll move to Phase 2: hybrid retrieval (BM25 + vector),
cross-encoder reranking, and (stretch) role-based access control. Come
back to the chat when you're ready.

## 8. Real-run notes

Two issues only surfaced when running Steps 3–4 for real (not in the
original sandbox with stub embedders/generators):

- **Windows path separators broke citation checking.** `chunk_id`s were
  built with `str(path.relative_to(...))`, which uses backslashes on
  Windows. The citation regex only matched forward slashes, so the
  fabrication check silently never matched *any* citation on Windows.
  Fixed by using `.as_posix()` when building chunk_ids.
- **The LLM didn't always cite in the exact bracket format the prompt
  asked for.** It sometimes echoed the context block's own
  `[chunk_id: ...]` label style instead of the bare `[chunk_id]` format
  the system prompt specified. Fixed by making the citation regex accept
  both forms, and by removing the ambiguous bracket example from the
  context template.
