"""
Preprocessing step for the FastAPI docs corpus.

FastAPI's markdown source doesn't inline code examples directly. Instead it
references them via a macro like:

    {* ../../docs_src/body/tutorial001_py310.py hl[2] *}

This resolves against docs_src/ at build time (via a mkdocs plugin) into an
actual fenced Python code block. Since we're not running mkdocs, we replicate
that resolution ourselves so the chunks we embed actually contain the code,
not just a dangling reference to a file path that means nothing outside the
mkdocs build.

Why this matters for RAG quality: a chunk that says "see the example below"
with no code is nearly useless for retrieval-augmented answers about *how*
to do something. Losing this would quietly gut a big fraction of the
corpus's value.
"""

import re
from pathlib import Path

RAW_DOCS_DIR = Path(__file__).parent.parent / "data" / "raw" / "fastapi_docs"
DOCS_SRC_DIR = Path(__file__).parent.parent / "data" / "raw" / "fastapi_docs_src"
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "processed"

# Matches: {* ../../docs_src/body/tutorial001_py310.py hl[2,5:9] *}
# or without the highlight suffix.
MACRO_PATTERN = re.compile(r"\{\*\s*(\S+?\.py)\s*(?:hl\[[\d,:]+\])?\s*\*\}")

# Matches mkdocs-material heading anchor syntax: "## Title { #anchor-id }"
HEADING_ANCHOR_PATTERN = re.compile(r"\s*\{\s*#[\w\-]+\s*\}\s*$")

# Matches mkdocs-material admonition blocks: /// note ... ///
ADMONITION_OPEN = re.compile(r"^///\s*(\w+)(?:\s*\|\s*(.*))?\s*$")
ADMONITION_CLOSE = re.compile(r"^///\s*$")


def resolve_code_macros(text: str) -> str:
    """Replace {* path/to/file.py *} references with real fenced code blocks."""

    def replacer(match: re.Match) -> str:
        rel_path = match.group(1)
        # Paths in the docs are relative to docs/en/docs/, e.g. ../../docs_src/x.py
        # Strip leading ../ segments and resolve against DOCS_SRC_DIR's parent.
        cleaned = rel_path.replace("../../docs_src/", "").replace("../docs_src/", "")
        code_path = DOCS_SRC_DIR / cleaned
        if not code_path.exists():
            # Fall back to searching by filename if the relative path doesn't
            # resolve cleanly (some docs use slightly different nesting).
            candidates = list(DOCS_SRC_DIR.rglob(Path(cleaned).name))
            if candidates:
                code_path = candidates[0]
            else:
                return f"[code example unavailable: {rel_path}]"
        code = code_path.read_text(encoding="utf-8").strip()
        return f"```python\n{code}\n```"

    return MACRO_PATTERN.sub(replacer, text)


def clean_heading_anchors(text: str) -> str:
    """Strip mkdocs' ' { #anchor-id }' suffix from headings for clean text."""
    lines = text.split("\n")
    cleaned = [HEADING_ANCHOR_PATTERN.sub("", line) for line in lines]
    return "\n".join(cleaned)


def convert_admonitions(text: str) -> str:
    """
    Convert mkdocs-material '/// note ... ///' blocks into a plain-text
    labeled paragraph, e.g. 'Note: ...'. Keeps the semantic signal (this is
    a warning/tip/note) without leaving syntax that means nothing outside
    mkdocs.
    """
    lines = text.split("\n")
    out = []
    label = None
    for line in lines:
        open_match = ADMONITION_OPEN.match(line.strip())
        if open_match:
            label = open_match.group(1).capitalize()
            out.append(f"**{label}:**")
            continue
        if ADMONITION_CLOSE.match(line.strip()):
            label = None
            continue
        out.append(line)
    return "\n".join(out)


def preprocess_file(md_path: Path) -> str:
    text = md_path.read_text(encoding="utf-8")
    text = resolve_code_macros(text)
    text = clean_heading_anchors(text)
    text = convert_admonitions(text)
    return text


def run():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    md_files = sorted(RAW_DOCS_DIR.rglob("*.md"))
    print(f"Found {len(md_files)} markdown files to preprocess.")

    n_ok = 0
    for md_path in md_files:
        rel = md_path.relative_to(RAW_DOCS_DIR)
        out_path = OUTPUT_DIR / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        processed = preprocess_file(md_path)
        out_path.write_text(processed, encoding="utf-8")
        n_ok += 1

    print(f"Preprocessed {n_ok} files -> {OUTPUT_DIR}")


if __name__ == "__main__":
    run()
