"""
Fixed-window chunker (V1).

Public API:
    chunk_text(text, chunker_version=1) -> list[ChunkSpec]

Each ChunkSpec is a small dataclass: text, chunk_index, token_count, char_count.
The orchestrator wraps each into a full Chunk model with embedding + identity
metadata.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# V1 targets: ~800 tokens per chunk, ~100 overlap.
# 1 word ≈ 1.3 tokens for English -> 615 words per chunk, 77 overlap.
# Round to friendlier numbers; gives slightly larger chunks than 800 tokens
# in practice but well under text-embedding-005's 2048 token cap.
WORDS_PER_CHUNK = 600
WORD_OVERLAP = 75
TOKENS_PER_WORD = 1.3

# Whitespace splitter — keeps it simple. Newlines are also whitespace.
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ChunkSpec:
    """The chunker's per-chunk output. Pure data, no Firestore concerns."""

    text: str
    chunk_index: int
    token_count: int  # estimated
    char_count: int


def chunk_text(text: str) -> list[ChunkSpec]:
    """
    Split `text` into overlapping word-window chunks.

    Returns an empty list if the input is empty or whitespace-only.
    """
    if not text or not text.strip():
        return []

    words = _WHITESPACE.split(text.strip())
    n = len(words)

    # Single chunk if the doc is shorter than one window.
    if n <= WORDS_PER_CHUNK:
        chunk_str = " ".join(words)
        return [
            ChunkSpec(
                text=chunk_str,
                chunk_index=0,
                token_count=int(round(n * TOKENS_PER_WORD)),
                char_count=len(chunk_str),
            )
        ]

    chunks: list[ChunkSpec] = []
    step = WORDS_PER_CHUNK - WORD_OVERLAP  # how far we advance per window
    chunk_index = 0
    start = 0

    while start < n:
        end = min(start + WORDS_PER_CHUNK, n)
        window = words[start:end]
        chunk_str = " ".join(window)
        chunks.append(
            ChunkSpec(
                text=chunk_str,
                chunk_index=chunk_index,
                token_count=int(round(len(window) * TOKENS_PER_WORD)),
                char_count=len(chunk_str),
            )
        )
        chunk_index += 1

        # If this window reached the end, stop.
        if end >= n:
            break
        start += step

    return chunks
