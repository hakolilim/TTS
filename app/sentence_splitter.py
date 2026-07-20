"""Split text into sentences with character offsets for highlighting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass
class Sentence:
    """A single sentence with position info in the original text."""

    index: int
    text: str
    start_char: int
    end_char: int

    @property
    def preview(self) -> str:
        t = self.text.strip()
        return t if len(t) <= 60 else t[:57] + "..."


# Split on sentence-ending punctuation followed by whitespace or end of string.
# Supports Vietnamese / English ellipsis and common terminators.
_SENTENCE_END = re.compile(
    r"(?<=[.!?…。！？])\s+|(?<=[.!?…。！？])$"
)


def split_sentences(text: str) -> List[Sentence]:
    """
    Split *text* into sentences, preserving character offsets.

    Empty / whitespace-only segments are skipped.
    If no terminator is found, the whole (stripped) text is one sentence.
    """
    if not text or not text.strip():
        return []

    # Work on the original string so offsets stay valid for the Text widget.
    parts: List[tuple[str, int, int]] = []
    last = 0

    for match in re.finditer(r".+?(?:[.!?…。！？]+(?=\s|$)|$)", text, re.DOTALL):
        segment = match.group(0)
        start = match.start()
        end = match.end()
        # Trim trailing whitespace from segment but keep start offset on real content
        stripped = segment.strip()
        if not stripped:
            last = end
            continue
        # Adjust start/end to the stripped content inside original text
        leading = len(segment) - len(segment.lstrip())
        trailing = len(segment) - len(segment.rstrip())
        real_start = start + leading
        real_end = end - trailing
        parts.append((stripped, real_start, real_end))
        last = end

    # Fallback: entire text as one sentence
    if not parts:
        stripped = text.strip()
        if not stripped:
            return []
        start = text.find(stripped)
        return [Sentence(0, stripped, start, start + len(stripped))]

    sentences: List[Sentence] = []
    for i, (seg, start, end) in enumerate(parts):
        sentences.append(Sentence(index=i, text=seg, start_char=start, end_char=end))
    return sentences


def sentence_index_at_offset(sentences: List[Sentence], offset: int) -> int:
    """Return sentence index that contains *offset*, or nearest following, or -1."""
    if not sentences:
        return -1
    for s in sentences:
        if s.start_char <= offset < s.end_char:
            return s.index
    # Before first
    if offset < sentences[0].start_char:
        return sentences[0].index
    # After last or in gaps — pick nearest next, else last
    for s in sentences:
        if s.start_char >= offset:
            return s.index
    return sentences[-1].index
