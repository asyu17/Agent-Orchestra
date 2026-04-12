from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import IsolatedAsyncioTestCase

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def _ensure_package(name: str, path: Path) -> ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        module.__path__ = [str(path)]
        module.__package__ = name
        sys.modules[name] = module
    return module


def _load_src_module(module_name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(SRC_ROOT / relative_path),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


agent_pkg = _ensure_package("agent_orchestra", SRC_ROOT / "agent_orchestra")
tools_pkg = _ensure_package("agent_orchestra.tools", SRC_ROOT / "agent_orchestra/tools")
runtime_pkg = _ensure_package("agent_orchestra.runtime", SRC_ROOT / "agent_orchestra/runtime")
setattr(agent_pkg, "tools", tools_pkg)
setattr(agent_pkg, "runtime", runtime_pkg)

mailbox_module = _load_src_module("agent_orchestra.tools.mailbox", "agent_orchestra/tools/mailbox.py")
setattr(tools_pkg, "mailbox", mailbox_module)
loop_module = _load_src_module(
    "agent_orchestra.runtime.teammate_online_loop",
    "agent_orchestra/runtime/teammate_online_loop.py",
)
setattr(runtime_pkg, "teammate_online_loop", loop_module)

MailboxEnvelope = mailbox_module.MailboxEnvelope
TeammateOnlineLoop = loop_module.TeammateOnlineLoop


class TeammateOnlineLoopTest(IsolatedAsyncioTestCase):
    async def test_prefers_mailbox_over_task_when_envelopes_exist(self) -> None:
        loop = TeammateOnlineLoop()
        envelopes = (
            MailboxEnvelope(sender="system", recipient="teammate:1", subject="init"),
            MailboxEnvelope(sender="system", recipient="teammate:1", subject="update"),
        )
        polls = [envelopes, (), ()]
        claims: list[str] = []
        processed_envelopes: list[MailboxEnvelope] = []
        idle_waits: int = 0

        async def poll_mailbox() -> tuple[MailboxEnvelope, ...]:
            if polls:
                return polls.pop(0)
            return ()

        async def claim_task() -> Any | None:
            claims.append("task-demand")
            return None

        async def handle_envelope(envelope: MailboxEnvelope) -> None:
            processed_envelopes.append(envelope)

        async def handle_task(claim: Any) -> None:  # pragma: no cover - claim not expected
            processed_envelopes.append(claim)

        async def idle_wait() -> None:
            nonlocal idle_waits
            idle_waits += 1
            await asyncio.sleep(0)

        result = await loop.run(
            poll_mailbox=poll_mailbox,
            claim_task=claim_task,
            on_mailbox_envelope=handle_envelope,
            on_task_claim=handle_task,
            idle_wait=idle_wait,
            iteration_limit=3,
        )

        self.assertEqual(processed_envelopes, list(envelopes))
        self.assertEqual(
            claims,
            ["task-demand", "task-demand"],
        )  # fallback claim check runs on each idle iteration
        self.assertEqual(idle_waits, 2)
        self.assertTrue(result.stopped_due_to_iteration_limit)
        self.assertFalse(result.stopped_due_to_stop_event)
        self.assertEqual(result.metrics.mailbox_polls, 3)
        self.assertEqual(result.metrics.task_claims, 0)
        self.assertEqual(result.metrics.idle_waits, 2)

    async def test_can_claim_multiple_tasks_in_isolation(self) -> None:
        loop = TeammateOnlineLoop()
        pending_tasks = ["task-a", "task-b"]
        claimed: list[str] = []
        idle_events: int = 0

        async def poll_mailbox() -> tuple[MailboxEnvelope, ...]:
            return ()

        async def claim_task() -> Any | None:
            if pending_tasks:
                return pending_tasks.pop(0)
            return None

        async def handle_envelope(envelope: MailboxEnvelope) -> None:  # pragma: no cover
            claimed.append("unexpected-envelope")

        async def handle_task(claim: Any) -> None:
            claimed.append(claim)

        async def idle_wait() -> None:
            nonlocal idle_events
            idle_events += 1
            await asyncio.sleep(0)

        result = await loop.run(
            poll_mailbox=poll_mailbox,
            claim_task=claim_task,
            on_mailbox_envelope=handle_envelope,
            on_task_claim=handle_task,
            idle_wait=idle_wait,
            iteration_limit=3,
        )

        self.assertEqual(claimed, ["task-a", "task-b"])
        self.assertEqual(result.metrics.task_claims, 2)
        self.assertEqual(idle_events, 1)
        self.assertTrue(result.stopped_due_to_iteration_limit)
        self.assertEqual(result.metrics.mailbox_envelopes_processed, 0)
