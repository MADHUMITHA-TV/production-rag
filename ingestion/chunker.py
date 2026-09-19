"""
Structure-aware chunker for the FastAPI docs corpus.

Design decisions (documented here because "why did you chunk it this way"
is a guaranteed interview question):

1. Naive fixed-window chunking (split every N tokens) would routinely slice
   a Python code block in half, or separate a heading from the paragraph
   that explains it. Both wreck retrieval quality: a half-a-function code
   chunk is useless, and an orphaned paragraph with no heading context loses
   the "what is this actually about" signal the embedding needs.

2. Instead we parse each doc into ordered ATOMIC UNITS (paragraph, code
   block, list, table) that are never split internally, each tagged with
   its heading breadcrumb (e.g. "Request Body > Import Pydantic's
   BaseModel"). We then greedily pack units into chunks targeting
   500-800 tokens, only ever breaking chunks at unit boundaries.

3. Overlap (~100 tokens) is implemented by carrying the trailing unit(s) of
   one chunk into the start of the next, rather than a raw character-offset
   overlap, so overlap content is always a complete, coherent unit.

4. Known limitation: a single code block or table larger than the max
   chunk size becomes its own oversized chunk rather than being split. This
   is a deliberate tradeoff -- a broken code example is worse than a
   slightly oversized chunk. Documented, not hidden.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PROCESSED_DIR = Path(__file__).parent.parent / "data" / "processed"
OUTPUT_PATH = Path(__file__).parent.parent / "data" / "chunks.jsonl"

TARGET_MIN_TOKENS = 500
TARGET_MAX_TOKENS = 800
OVERLAP_TOKENS = 100

# NOTE on token counting: tiktoken's real BPE vocab file is hosted on
# openaipublic.blob.core.windows.net, which isn't reachable from this
# sandboxed build environment (network allowlist doesn't include it). Rather
# than silently failing or requiring an internet-dependent setup step, we
# use a local approximation: ~4 chars/token for English prose, adjusted
# down slightly for code (denser symbols, shorter "words"). This is a
# well-known rough heuristic (GPT/Claude tokenizers both average close to
# this for English text) and is only used for *chunk sizing* -- the actual
# embedding model and LLM will each tokenize with their own real tokenizer
# at inference time regardless of how we sized chunks here. If exact token
# budgets matter later (e.g. hard context-window limits in production),
# swap this for the real tokenizer of whichever embedding/LLM model you end
# up serving with.
_WORD_RE = re.compile(r"\S+")


def count_tokens(text: str) -> int:
    chars = len(text)
    words = len(_WORD_RE.findall(text))
    # Blend char-based and word-based estimates; char/4 alone under-counts
    # code (lots of short symbol-heavy tokens), word count alone over-counts
    # prose (multi-token words). Average the two heuristics.
    char_estimate = chars / 4
    word_estimate = words * 1.3
    return round((char_estimate + word_estimate) / 2)


@dataclass
class Unit:
    text: str
    kind: str  # "heading" | "code" | "paragraph" | "list" | "table"
    heading_level: int = 0
    breadcrumb: str = ""
    tokens: int = field(init=False)

    def __post_init__(self):
        self.tokens = count_tokens(self.text)


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
CODE_FENCE_RE = re.compile(r"^```")
TABLE_ROW_RE = re.compile(r"^\|.*\|\s*$")


def parse_units(text: str) -> list[Unit]:
    """Parse markdown text into ordered atomic units, tracking heading breadcrumbs."""
    lines = text.split("\n")
    units: list[Unit] = []
    heading_stack: list[str] = []  # e.g. ["Request Body", "Import Pydantic's BaseModel"]

    i = 0
    buffer: list[str] = []

    def breadcrumb() -> str:
        return " > ".join(heading_stack)

    def flush_paragraph():
        nonlocal buffer
        joined = "\n".join(buffer).strip()
        if joined:
            kind = "table" if TABLE_ROW_RE.match(joined.split("\n")[0]) else "paragraph"
            units.append(Unit(text=joined, kind=kind, breadcrumb=breadcrumb()))
        buffer = []

    while i < len(lines):
        line = lines[i]

        heading_match = HEADING_RE.match(line)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            # Truncate heading_stack to this level, then append.
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(title)
            units.append(
                Unit(text=line, kind="heading", heading_level=level, breadcrumb=breadcrumb())
            )
            i += 1
            continue

        if CODE_FENCE_RE.match(line):
            flush_paragraph()
            code_lines = [line]
            i += 1
            while i < len(lines) and not CODE_FENCE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                code_lines.append(lines[i])  # closing fence
                i += 1
            units.append(Unit(text="\n".join(code_lines), kind="code", breadcrumb=breadcrumb()))
            continue

        if line.strip() == "":
            flush_paragraph()
            i += 1
            continue

        buffer.append(line)
        i += 1

    flush_paragraph()
    return units


@dataclass
class Chunk:
    text: str
    breadcrumb: str
    source_file: str
    chunk_index: int
    token_count: int


def pack_units_into_chunks(units: list[Unit], source_file: str) -> list[Chunk]:
    """Greedily pack atomic units into ~500-800 token chunks with unit-level overlap."""
    chunks: list[Chunk] = []
    current: list[Unit] = []
    current_tokens = 0
    chunk_index = 0

    def emit():
        nonlocal current, current_tokens, chunk_index
        if not current:
            return
        text = "\n\n".join(u.text for u in current)
        # Breadcrumb of the chunk = breadcrumb of its first substantive unit.
        breadcrumb = next((u.breadcrumb for u in current if u.kind != "heading"), current[0].breadcrumb)
        chunks.append(
            Chunk(
                text=text,
                breadcrumb=breadcrumb,
                source_file=source_file,
                chunk_index=chunk_index,
                token_count=count_tokens(text),
            )
        )
        chunk_index += 1

    for unit in units:
        # A single oversized unit (e.g. a huge code block) becomes its own chunk.
        if unit.tokens > TARGET_MAX_TOKENS:
            emit()
            current, current_tokens = [], 0
            chunks.append(
                Chunk(
                    text=unit.text,
                    breadcrumb=unit.breadcrumb,
                    source_file=source_file,
                    chunk_index=chunk_index,
                    token_count=unit.tokens,
                )
            )
            chunk_index += 1
            continue

        if current_tokens + unit.tokens > TARGET_MAX_TOKENS and current_tokens >= TARGET_MIN_TOKENS:
            emit()
            # Build overlap: carry trailing units totaling ~OVERLAP_TOKENS.
            overlap_units: list[Unit] = []
            overlap_tokens = 0
            for u in reversed(current):
                if overlap_tokens >= OVERLAP_TOKENS:
                    break
                overlap_units.insert(0, u)
                overlap_tokens += u.tokens
            current = overlap_units
            current_tokens = overlap_tokens

        current.append(unit)
        current_tokens += unit.tokens

    emit()
    return chunks


def run():
    md_files = sorted(PROCESSED_DIR.rglob("*.md"))
    all_chunks: list[Chunk] = []

    for md_path in md_files:
        rel = md_path.relative_to(PROCESSED_DIR).as_posix()
        text = md_path.read_text(encoding="utf-8")
        units = parse_units(text)
        chunks = pack_units_into_chunks(units, source_file=rel)
        all_chunks.extend(chunks)

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        for i, c in enumerate(all_chunks):
            record = {
                "chunk_id": f"{c.source_file}::{c.chunk_index}",
                "source_file": c.source_file,
                "breadcrumb": c.breadcrumb,
                "chunk_index": c.chunk_index,
                "token_count": c.token_count,
                "text": c.text,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    token_counts = [c.token_count for c in all_chunks]
    print(f"Files processed: {len(md_files)}")
    print(f"Total chunks:    {len(all_chunks)}")
    print(f"Token count  min={min(token_counts)}  max={max(token_counts)}  "
          f"avg={sum(token_counts)/len(token_counts):.0f}")
    print(f"Chunks over {TARGET_MAX_TOKENS} tokens (oversized units): "
          f"{sum(1 for t in token_counts if t > TARGET_MAX_TOKENS)}")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    run()
