"""Split documents into passages small enough to embed and quote."""

import re

MAX_WORDS = 180
OVERLAP_WORDS = 40


def split_passages(text: str, max_words: int = MAX_WORDS, overlap: int = OVERLAP_WORDS) -> list[str]:
    """Passages of up to `max_words`, built from whole paragraphs where possible.

    Each passage after the first starts with the last `overlap` words of the
    one before, so a sentence that straddles a boundary is found either way.
    A paragraph longer than `max_words` is split at sentence ends.
    """
    units: list[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n|\n", text)):
        if not para:
            continue
        if len(para.split()) <= max_words:
            units.append(para)
            continue
        # Long paragraph: sentences, and words if a single sentence is still too long.
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            words = sentence.split()
            for i in range(0, len(words), max_words):
                units.append(" ".join(words[i:i + max_words]))

    passages: list[str] = []
    current: list[str] = []
    for unit in units:
        if current and len(current) + len(unit.split()) > max_words:
            passages.append(" ".join(current))
            current = current[-overlap:] if overlap else []
        current += unit.split()
    if current and (not passages or len(current) > overlap):
        passages.append(" ".join(current))
    return passages
