import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from src.workflows.lifecycle import register_lifecycle_execution

with workflow.unsafe.imports_passed_through():
    from src.workflows.activities import (
        llm_call,
        record_langfuse_score,
        record_message_score,
    )

logger = logging.getLogger(__name__)

# See conversation.py's _ACTIVITY_RETRY_POLICY — same rationale: without a
# bound, a persistently-failing activity retries forever and the enclosing
# try/except below never gets a chance to run.
_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass
class EvaluationInput:
    conversation_id: str
    turn_id: str
    query: str
    context_used: str
    agent_answer: str
    model: str
    # Multi-agent turns judge the answer against the intermediate node outputs,
    # which are admin-only by design.  record_message_score's reason is rendered
    # to the user (AssistantBlock's tooltip), and a judge asked to justify its
    # score can quote that context verbatim — so those turns keep the score and
    # drop the user-visible explanation.  Langfuse still gets the full reason:
    # it is an operator surface, governed by the existing capture settings.
    reveal_reason: bool = True


@workflow.defn(name="EvaluationWorkflow")
class EvaluationWorkflow:
    @workflow.run
    async def run(self, input: EvaluationInput) -> None:
        await register_lifecycle_execution()
        prompt = f"""You are a quality evaluation expert. Assess the faithfulness of the agent's answer strictly against the provided context.
If the answer contains information that is not in the context, it is a hallucination (score 0).
If the answer is fully supported by the context, it is faithful (score 1).

Context:
{input.context_used}

Question:
{input.query}

Answer:
{input.agent_answer}

Respond only with a JSON object with the fields "score" (0 or 1) and "reason" (string)."""

        request = {
            "model": input.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "temperature": 0.0
        }

        try:
            # publish_events=False: this is an internal judge call, not part of
            # the user-facing turn — must not leak onto the conversation's SSE
            # channel (its content-delta/thinking-delta carry no turn_id, so
            # they'd otherwise pass the frontend's "untagged = broadcast" filter
            # and get appended to whatever answer is currently on screen).
            raw = await workflow.execute_activity(
                llm_call,
                args=(request, None, False),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            
            # Clean up markdown JSON blocks if the model wrapped it
            raw_clean = raw.strip()
            if raw_clean.startswith("```json"):
                raw_clean = raw_clean[7:]
            if raw_clean.startswith("```"):
                raw_clean = raw_clean[3:]
            if raw_clean.endswith("```"):
                raw_clean = raw_clean[:-3]
            raw_clean = raw_clean.strip()
            
            data = json.loads(raw_clean)
            score = float(data.get("score", 0))
            reason = data.get("reason", "")

            await workflow.execute_activity(
                record_langfuse_score,
                args=(input.conversation_id, "faithfulness", score, reason),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            await workflow.execute_activity(
                record_message_score,
                args=(input.turn_id, score, reason if input.reveal_reason else None),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
        except Exception as e:
            workflow.logger.warning(f"Hallucination evaluation failed: {e}")
            # Still notify the backend (score=None) so an SSE connection kept
            # open waiting for this turn's evaluation (see business-backend's
            # chat_stream) closes promptly instead of idling out.
            try:
                await workflow.execute_activity(
                    record_message_score,
                    args=(
                        input.turn_id,
                        None,
                        f"Evaluation failed: {e}" if input.reveal_reason else None,
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_ACTIVITY_RETRY_POLICY,
                )
            except Exception as notify_exc:
                workflow.logger.warning(
                    f"Could not notify the backend about the failed evaluation: {notify_exc}"
                )
