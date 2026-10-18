import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from src.workflows.lifecycle import register_lifecycle_execution

with workflow.unsafe.imports_passed_through():
    from src.workflows.activities import llm_call, publish_event
    from src.workflows.tools import extract_run_commands


CODE_SKILL_SYSTEM_PROMPT_TEMPLATE = (
    "You are running a code skill inside an isolated sandbox container. Its "
    "working directory is /skill, which already contains the skill's bundled "
    "files (scripts, references, resources). These are its instructions:\n\n"
    "{skill_md}\n\n"
    "To run a shell command in the sandbox (cwd=/skill), emit exactly:\n"
    '<run_command>{{"command": "<shell command>"}}</run_command>\n'
    "Run one command at a time — you will see its stdout, stderr, and exit "
    "code before deciding what to do next. The sandbox has no network access "
    "unless the skill was explicitly configured for it. When the task is "
    "complete, respond with your final answer as plain text (no <run_command> "
    "tags) summarizing what was done and any output the user should see."
)


@dataclass
class CodeSkillSessionInput:
    skill_id: str
    task: str
    conversation_id: str
    turn_id: str | None = None
    session_token: str | None = None
    model: str = "gpt-4o-mini"
    max_rounds: int = 8
    round_timeout_seconds: float = 60.0


@workflow.defn(name="CodeSkillSessionWorkflow")
class CodeSkillSessionWorkflow:
    def __init__(self) -> None:
        self.round = 0

    @workflow.run
    async def run(self, input: CodeSkillSessionInput) -> str:
        await register_lifecycle_execution()
        session = await workflow.execute_activity(
            "open_skill_session",
            args=(input.skill_id, input.conversation_id, input.session_token),
            task_queue="tools",
            start_to_close_timeout=timedelta(seconds=90),
            # Bounded everywhere on the tools queue: SkillExecutorError is
            # non-retryable (marked in the activity), and anything else that
            # keeps failing must not retry forever and hang the session.
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        session_id = session["session_id"]
        skill_md = session.get("skill_md") or ""

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": CODE_SKILL_SYSTEM_PROMPT_TEMPLATE.format(skill_md=skill_md),
            },
            {"role": "user", "content": input.task},
        ]

        max_rounds = input.max_rounds
        round_timeout = input.round_timeout_seconds

        final_answer = ""
        try:
            for self.round in range(1, max_rounds + 1):
                request = {"model": input.model, "messages": messages, "stream": True}
                raw = await workflow.execute_activity(
                    llm_call,
                    args=(request, None, False),
                    start_to_close_timeout=timedelta(minutes=2),
                )

                commands = extract_run_commands(raw)
                if not commands:
                    final_answer = raw
                    break

                messages.append({"role": "assistant", "content": raw})
                for call in commands:
                    command = str(call.get("command", ""))

                    await workflow.execute_activity(
                        publish_event,
                        args=(
                            "run-command",
                            {
                                "skill_id": input.skill_id,
                                "round": self.round,
                                "command": command,
                                "turn_id": input.turn_id,
                            },
                        ),
                        start_to_close_timeout=timedelta(seconds=30),
                    )

                    exec_result = await workflow.execute_activity(
                        "exec_in_skill_session",
                        args=(session_id, command, round_timeout),
                        task_queue="tools",
                        start_to_close_timeout=timedelta(seconds=round_timeout + 30),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )

                    await workflow.execute_activity(
                        publish_event,
                        args=(
                            "run-result",
                            {
                                "skill_id": input.skill_id,
                                "round": self.round,
                                "result": exec_result,
                                "turn_id": input.turn_id,
                            },
                        ),
                        start_to_close_timeout=timedelta(seconds=30),
                    )

                    status = " (TIMED OUT)" if exec_result.get("timed_out") else ""
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Command: {command}\n"
                                f"stdout: {exec_result.get('stdout', '')}\n"
                                f"stderr: {exec_result.get('stderr', '')}\n"
                                f"exit_code: {exec_result.get('exit_code')}{status}"
                            ),
                        }
                    )
            else:
                # Round budget exhausted without a plain-text final answer —
                # ask for a concise summary, same fallback shape as AgentWorkflow.
                messages.append(
                    {
                        "role": "user",
                        "content": "You've reached the round limit. Provide a concise "
                        "final answer based on the work above.",
                    }
                )
                request = {"model": input.model, "messages": messages, "stream": True}
                final_answer = await workflow.execute_activity(
                    llm_call,
                    args=(request, None, False),
                    start_to_close_timeout=timedelta(minutes=2),
                )
        except ActivityError as exc:
            # A dead sandbox session (reaped, executor restart, ...) is
            # non-retryable — surface it as the skill's answer instead of
            # failing this workflow and hanging the parent conversation turn.
            cause = exc.cause or exc
            final_answer = (
                f"Code skill execution failed in round {self.round}: {cause}"
            )
        finally:
            # Second half of the double teardown guarantee — the first half
            # is skill-executor's own TTL reaper, which fires independently
            # even if this workflow crashes before reaching here. shield()
            # because a cancellation landing on the workflow while this
            # finally is running would otherwise cancel this activity call
            # too — the whole point of this call is to
            # run precisely *during* teardown, cancelled or not.
            await asyncio.shield(
                workflow.execute_activity(
                    "close_skill_session",
                    args=(session_id,),
                    task_queue="tools",
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            )

        return final_answer
