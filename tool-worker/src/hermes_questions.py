"""Questions Hermes asks the user through its clarify tool (docs/HERMES_AGENT.md,
phase 4c).

The run activity forwards each `clarify.request` and `clarify.settled` to the
conversation workflow, which asks through the platform's question cards and
sends the answer back through the session workflow. Everything in the event
comes from the sandbox, so only a bounded question is forwarded: an id of a
fixed shape, the text without Hermes's own reply hint, and at most eight
short choices. A replay after a reattach forwards the same prompts again;
the conversation workflow keeps each prompt once.
"""

import logging
import re
from typing import Any

from temporalio import activity

logger = logging.getLogger(__name__)

QUESTION_SIGNAL = "hermes_question"
SETTLED_SIGNAL = "hermes_question_settled"
_PROMPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Hermes appends this to every question for its own chat surfaces.
_SKIP_HINT = '\nReply "skip" to skip this question.'
_RECOMMENDED = " (Recommended)"
_MAX_TEXT = 4000
_MAX_CHOICES = 8
_MAX_CHOICE = 200


def question(event: dict[str, Any], run_id: str, turn_id: str) -> dict[str, Any] | None:
    """The bounded question of a `clarify.request`, or None for anything else."""
    prompt_id, generation, text = event.get("prompt_id"), event.get("execution_generation"), event.get("question")
    if not (isinstance(prompt_id, str) and _PROMPT_ID.match(prompt_id)):
        return None
    if type(generation) is not int or not isinstance(text, str):
        return None
    text = text.removesuffix(_SKIP_HINT).strip()[:_MAX_TEXT]
    if not text:
        return None
    raw = event.get("choices")
    choices = [
        # Hermes marks its first choice; the answer is the choice itself.
        choice.removesuffix(_RECOMMENDED).strip()[:_MAX_CHOICE]
        for choice in (raw if isinstance(raw, list) else [])[:_MAX_CHOICES]
        if isinstance(choice, str) and choice.strip()
    ]
    return {
        "turn_id": turn_id,
        "run_id": run_id,
        "prompt_id": prompt_id,
        "execution_generation": generation,
        "text": text,
        "choices": list(dict.fromkeys(choices)),
        "multi_select": event.get("multi_select") is True,
    }


class ParentQuestions:
    """Signals the waiting conversation workflow about the run's questions."""

    def __init__(self, parent_workflow_id: str, turn_id: str) -> None:
        self._parent = parent_workflow_id
        self._turn_id = turn_id

    async def event(self, event: dict[str, Any], run_id: str | None) -> None:
        if not self._parent or run_id is None:
            return
        kind = event.get("event")
        if kind == "clarify.request":
            asked = question(event, run_id, self._turn_id)
            if asked is not None:
                await self._signal(QUESTION_SIGNAL, asked)
        elif kind == "clarify.settled":
            prompt_id = event.get("prompt_id")
            if isinstance(prompt_id, str) and _PROMPT_ID.match(prompt_id):
                await self._signal(SETTLED_SIGNAL, {"turn_id": self._turn_id, "prompt_id": prompt_id})

    async def _signal(self, name: str, payload: dict[str, Any]) -> None:
        try:
            await activity.client().get_workflow_handle(self._parent).signal(name, payload)
        except Exception:
            # The conversation workflow is gone; Hermes's own timeout ends the wait.
            logger.warning("Could not forward a Hermes question")
