"""Isolated teammate online loop for mailbox polling and task claiming."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Tuple

from agent_orchestra.tools.mailbox import MailboxEnvelope


MailboxPollCallback = Callable[[], Awaitable[Tuple[MailboxEnvelope, ...]]]
TaskClaimCallback = Callable[[], Awaitable[Optional[Any]]]
IdleWaitCallback = Callable[[], Awaitable[None]]
MailboxEnvelopeHandler = Callable[[MailboxEnvelope], Awaitable[None]]
TaskClaimHandler = Callable[[Any], Awaitable[None]]


@dataclass(slots=True)
class TeammateOnlineLoopMetrics:
    iterations: int = 0
    mailbox_polls: int = 0
    mailbox_envelopes_processed: int = 0
    task_claims: int = 0
    idle_waits: int = 0


@dataclass(slots=True)
class TeammateOnlineLoopRunResult:
    metrics: TeammateOnlineLoopMetrics = field(default_factory=TeammateOnlineLoopMetrics)
    stopped_due_to_iteration_limit: bool = False
    stopped_due_to_stop_event: bool = False


class TeammateOnlineLoop:
    def __init__(self, *, idle_wait: IdleWaitCallback | None = None) -> None:
        self._idle_wait = idle_wait or self._default_idle_wait

    async def run(
        self,
        *,
        poll_mailbox: MailboxPollCallback,
        claim_task: TaskClaimCallback,
        on_mailbox_envelope: MailboxEnvelopeHandler,
        on_task_claim: TaskClaimHandler,
        idle_wait: IdleWaitCallback | None = None,
        iteration_limit: int | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> TeammateOnlineLoopRunResult:
        """Run the online loop until stopped, yielding mailbox envelopes and claimed tasks."""

        idle_wait_callable = idle_wait or self._idle_wait
        metrics = TeammateOnlineLoopMetrics()
        iterations = 0
        result = TeammateOnlineLoopRunResult(metrics=metrics)

        while True:
            if stop_event is not None and stop_event.is_set():
                result.stopped_due_to_stop_event = True
                break
            if iteration_limit is not None and iterations >= iteration_limit:
                result.stopped_due_to_iteration_limit = True
                break

            progress_made = False

            envelopes = await poll_mailbox()
            metrics.mailbox_polls += 1
            if envelopes:
                progress_made = True
                for envelope in envelopes:
                    await on_mailbox_envelope(envelope)
                    metrics.mailbox_envelopes_processed += 1
            else:
                claim = await claim_task()
                if claim is not None:
                    progress_made = True
                    metrics.task_claims += 1
                    await on_task_claim(claim)

            if not progress_made:
                metrics.idle_waits += 1
                await idle_wait_callable()

            iterations += 1
            metrics.iterations = iterations

        return result

    @staticmethod
    async def _default_idle_wait() -> None:
        await asyncio.sleep(0)
