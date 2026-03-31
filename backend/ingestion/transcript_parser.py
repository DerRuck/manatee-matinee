import re
from dataclasses import dataclass


# Filler words/phrases to strip from spoken transcripts
_FILLERS = re.compile(
    r"\b(um+|uh+|like|you know|i mean|sort of|kind of|basically|literally|actually|"
    r"right\??|okay\??|so+|well+|just|anyway|anyways|honestly)\b",
    re.IGNORECASE,
)

# Collapse 3+ whitespace/newline chars into one
_WHITESPACE = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n{3,}")

# Speaker label patterns: "Speaker 1:", "LUIS:", "[00:01:23] John:"
_SPEAKER_LABEL = re.compile(r"^(\[[\d:]+\]\s*)?[\w\s]+:\s*", re.MULTILINE)


@dataclass
class TranscriptChunk:
    index: int
    text: str
    char_start: int
    char_end: int
    token_estimate: int  # rough word count, useful for embedding cost estimates


def clean(raw: str) -> str:
    """
    Remove filler words and normalize whitespace from a raw transcript.
    Preserves speaker labels and sentence structure.
    """
    text = _FILLERS.sub("", raw)
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    # Clean up spaces that appear right before punctuation after filler removal
    text = re.sub(r" +([,\.!?])", r"\1", text)
    # Collapse multiple spaces within a line
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def chunk(
    text: str,
    chunk_size: int = 400,
    overlap: int = 50,
) -> list[TranscriptChunk]:
    """
    Split cleaned transcript text into overlapping word-based chunks for embedding.

    Args:
        text:       Cleaned transcript string.
        chunk_size: Target chunk size in words.
        overlap:    Number of words to repeat at the start of each new chunk
                    to preserve context across boundaries.

    Returns:
        List of TranscriptChunk objects in order.
    """
    words = text.split()
    chunks: list[TranscriptChunk] = []

    step = chunk_size - overlap
    start_word = 0
    index = 0

    while start_word < len(words):
        end_word = min(start_word + chunk_size, len(words))
        chunk_words = words[start_word:end_word]
        chunk_text = " ".join(chunk_words)

        # Map word offsets back to character offsets in the original text
        char_start = len(" ".join(words[:start_word])) + (1 if start_word > 0 else 0)
        char_end = char_start + len(chunk_text)

        chunks.append(
            TranscriptChunk(
                index=index,
                text=chunk_text,
                char_start=char_start,
                char_end=char_end,
                token_estimate=len(chunk_words),
            )
        )

        if end_word == len(words):
            break

        start_word += step
        index += 1

    return chunks


def parse(raw: str, chunk_size: int = 400, overlap: int = 50) -> list[TranscriptChunk]:
    """
    Full pipeline: clean a raw transcript then chunk it for embedding.

    Args:
        raw:        Raw transcript text (from Drive, audio-to-text, etc.).
        chunk_size: Target words per chunk.
        overlap:    Overlap words between consecutive chunks.

    Returns:
        List of TranscriptChunk objects ready for vectorization.
    """
    cleaned = clean(raw)
    return chunk(cleaned, chunk_size=chunk_size, overlap=overlap)
