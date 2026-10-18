"""Sentence Window / Small2Big chunking (~512 tokens per window)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class SentenceWindowChunk:
    """Small chunk for search; parent window for LLM context."""

    chunk_index: int
    sentence: str
    parent_window: str
    token_estimate: int


def estimate_tokens(text: str) -> int:
    """Heuristic token count (~4 chars/token for RU/EN mixed text)."""
    normalized = WHITESPACE_RE.sub(" ", text.strip())
    if not normalized:
        return 0
    return max(1, len(normalized) // 4)


def split_sentences(text: str) -> list[str]:
    text = WHITESPACE_RE.sub(" ", text.strip())
    if not text:
        return []
    parts = SENTENCE_SPLIT_RE.split(text)
    return [part.strip() for part in parts if part.strip()]


def _group_sentences(
    sentences: list[str],
    *,
    max_tokens: int,
    overlap_sentences: int,
) -> Iterable[list[str]]:
    if not sentences:
        return

    start = 0
    while start < len(sentences):
        window: list[str] = []
        tokens = 0
        idx = start
        while idx < len(sentences):
            candidate = sentences[idx]
            candidate_tokens = estimate_tokens(candidate)
            if window and tokens + candidate_tokens > max_tokens:
                break
            window.append(candidate)
            tokens += candidate_tokens
            idx += 1

        if not window:
            window = [sentences[start]]
            idx = start + 1

        yield window

        if idx >= len(sentences):
            break
        start = max(start + 1, idx - overlap_sentences)


def chunk_text_sentence_window(
    text: str,
    *,
    max_tokens: int = 512,
    overlap_sentences: int = 1,
) -> list[SentenceWindowChunk]:
    """
    Small2Big pattern:
    - index/search by individual sentences (high precision)
    - return parent_window to LLM (broader context)
    """
    sentences = split_sentences(text)
    if not sentences:
        stripped = text.strip()
        if not stripped:
            return []
        return [
            SentenceWindowChunk(
                chunk_index=0,
                sentence=stripped,
                parent_window=stripped,
                token_estimate=estimate_tokens(stripped),
            )
        ]

    chunks: list[SentenceWindowChunk] = []
    chunk_index = 0
    for window in _group_sentences(
        sentences,
        max_tokens=max_tokens,
        overlap_sentences=overlap_sentences,
    ):
        parent_window = " ".join(window)
        for sentence in window:
            chunks.append(
                SentenceWindowChunk(
                    chunk_index=chunk_index,
                    sentence=sentence,
                    parent_window=parent_window,
                    token_estimate=estimate_tokens(sentence),
                )
            )
            chunk_index += 1
    return chunks
