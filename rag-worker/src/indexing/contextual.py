"""Contextual Retrieval: enrich chunks with document-level context via LLM."""

from __future__ import annotations
from typing import Optional

import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

CONTEXTUAL_PROMPT = """\
Document: "{title}"
Chunk: "{chunk}"

Briefly (1-2 sentences) describe the context of this chunk within the whole document.
Reply with the context only, without repeating the chunk itself.
Write the context in the same language as the document."""


async def generate_chunk_context(
    *,
    title: str,
    chunk: str,
    document_excerpt:Optional[ str] = None,
) -> str:
    if not settings.contextual_retrieval_enabled:
        return ""

    if settings.contextual_retrieval_mock:
        return f"Context of document \"{title}\"."

    prompt = CONTEXTUAL_PROMPT.format(title=title, chunk=chunk[:800])
    if document_excerpt:
        prompt += f"\n\nDocument beginning:\n{document_excerpt[:1200]}"

    request = {
        "model": settings.contextual_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 120,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if settings.openai_api_key:
        headers["Authorization"] = f"Bearer {settings.openai_api_key}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                settings.llm_gateway_url,
                json=request,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            content: str = data["choices"][0]["message"]["content"].strip()
            return content
    except Exception as exc:
        logger.warning("Contextual retrieval failed: %s", exc)
        return ""


def build_contextual_content(context: str, sentence: str) -> str:
    context = context.strip()
    if not context:
        return sentence
    return f"{context}\n\n{sentence}"
