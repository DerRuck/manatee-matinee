"""
Parse the C-HAWQ Internal Proven Process Binder docx into binder_chunks.json.

Chunks at the Heading 2 level (all body text + H3 sub-sections rolled in).
Each "AUTOMATED —" H3 section also gets its own chunk (chunk_type: ai_prompt)
so the research agent can retrieve it directly later if needed.

Run from backend/:
    python scripts/chunk_binder.py --source "../Internal Proven Process Binder v2.docx"
    python scripts/chunk_binder.py --source "../Internal Proven Process Binder v2.docx" --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SOURCE_VERSION = "v4.0"
SOURCE_DOC = "proven_process_binder"


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s]", "", text.lower())
    s = re.sub(r"\s+", "_", s.strip())
    return s[:max_len].rstrip("_") or "untitled"


def heading_level(para) -> int:
    name = para.style.name
    if name.startswith("Heading "):
        try:
            return int(name.split()[-1])
        except ValueError:
            pass
    return 0


def chunk_type_for(level: int, title: str) -> str:
    t = title.lower()
    if "automated" in t:
        return "ai_prompt"
    if "✓" in title or "checklist" in t:
        return "checklist"
    if re.match(r"template\s+\d", t):
        return "template"
    if level == 1:
        return "section_intro"
    if level == 2:
        return "subsection_overview"
    return "subprocess"


def extract_step(text: str) -> int | None:
    m = re.search(r"\bSTEP\s+(\d+)\b", text, re.I)
    return int(m.group(1)) if m else None


def extract_phase(text: str) -> int | None:
    m = re.search(r"\bPHASE\s+(\d+)\b", text, re.I)
    return int(m.group(1)) if m else None


def make_chunk(idx: int, level: int, title: str, h1: str, h2: str, lines: list[str]) -> dict | None:
    text = "\n".join(l for l in lines if l.strip())
    if len(text.strip()) < 40:
        return None

    ctype = chunk_type_for(level, title)
    step = extract_step(h2) or extract_step(h1)
    phase = extract_phase(h1)

    return {
        "chunk_id":         f"ppb_GEN_{idx:03d}_{ctype}_{slugify(title)}",
        "chunk_type":       ctype,
        "source_doc":       SOURCE_DOC,
        "source_version":   SOURCE_VERSION,
        "section":          h1,
        "subsection":       h2 if level > 1 else None,
        "title":            title,
        "step":             step,
        "phase":            phase,
        "research_type_id": None,
        "text":             text.strip(),
        "char_count":       len(text.strip()),
    }


def parse(docx_path: Path) -> list[dict]:
    from docx import Document

    doc = Document(docx_path)

    chunks: list[dict] = []
    idx = 0

    h1 = h2 = title = ""
    level = 0
    buf: list[str] = []

    def flush() -> None:
        nonlocal idx
        chunk = make_chunk(idx + 1, level, title, h1, h2, buf)
        if chunk:
            idx += 1
            chunk["chunk_id"] = f"ppb_GEN_{idx:03d}_{chunk['chunk_type']}_{slugify(title)}"
            chunks.append(chunk)
        buf.clear()

    for para in doc.paragraphs:
        lvl = heading_level(para)
        text = para.text.strip()

        if lvl == 1:
            flush()
            h1, h2 = text, ""
            level = 1
            title = text
            if text:
                buf.append(f"**{text}**")

        elif lvl == 2:
            flush()
            h2 = text
            level = 2
            title = text
            if text:
                buf.append(f"**{text}**")

        elif lvl == 3:
            # AUTOMATED sections always get their own chunk
            if text and "AUTOMATED" in text.upper():
                flush()
                level = 3
                title = text
                buf.append(f"**{text}**")
            else:
                # Non-automated H3: include as a sub-header in the current H2 chunk
                if text:
                    buf.append(f"\n**{text}**")

        else:
            if text:
                buf.append(text)

    flush()
    return chunks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="Path to the .docx file")
    ap.add_argument("--output", default="binder_chunks.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    docx_path = Path(args.source).expanduser().resolve()
    if not docx_path.exists():
        raise SystemExit(f"File not found: {docx_path}")

    chunks = parse(docx_path)

    print(f"Source:  {docx_path.name}")
    print(f"Chunks:  {len(chunks)}")
    print(f"Types:   { {c['chunk_type'] for c in chunks} }")
    total_chars = sum(c["char_count"] for c in chunks)
    print(f"Chars:   {total_chars:,}")

    if args.dry_run:
        print("\nDRY RUN — first 5 chunks:")
        for c in chunks[:5]:
            print(f"  {c['chunk_id']}")
            print(f"    type={c['chunk_type']}  step={c['step']}  chars={c['char_count']}")
            print(f"    {c['text'][:120].replace(chr(10),' ')}...")
        return

    out = Path(args.output)
    out.write_text(json.dumps(chunks, indent=2, ensure_ascii=False))
    print(f"\nWritten to {out}")
    print("Next step:")
    print(f"  python scripts/ingest_binder.py --source {out} --version 3 --dry-run")
    print(f"  python scripts/ingest_binder.py --source {out} --version 3")


if __name__ == "__main__":
    main()
