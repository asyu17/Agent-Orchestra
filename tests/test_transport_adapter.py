from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.execution import WorkerSession, WorkerSessionStatus
from agent_orchestra.runtime.backends import (
    CodexCliLaunchBackend,
    InProcessLaunchBackend,
    SubprocessLaunchBackend,
    TmuxLaunchBackend,
)
from agent_orchestra.runtime.transport_adapter import DefaultTransportAdapter


class TransportAdapterTest(TestCase):
    def test_hydrates_transport_class_hints_from_registered_backend_capabilities(self) -> None:
        adapter = DefaultTransportAdapter(
            launch_backends={
                "in_process": InProcessLaunchBackend(),
                "subprocess": SubprocessLaunchBackend(),
                "tmux": TmuxLaunchBackend(),
                "codex_cli": CodexCliLaunchBackend(codex_command=("codex",)),
            }
        )

        in_process_handle = adapter.handle_from_worker_session(
            WorkerSession(
                session_id="session-in-process",
                worker_id="worker-in-process",
                assignment_id="assign-in-process",
                backend="in_process",
                role="leader",
                status=WorkerSessionStatus.ACTIVE,
                handle_snapshot={"backend": "in_process", "metadata": {}},
            ),
            backend="in_process",
        )
        subprocess_handle = adapter.handle_from_worker_session(
            WorkerSession(
                session_id="session-subprocess",
                worker_id="worker-subprocess",
                assignment_id="assign-subprocess",
                backend="subprocess",
                role="teammate",
                status=WorkerSessionStatus.ACTIVE,
                handle_snapshot={"backend": "subprocess", "metadata": {}},
            ),
            backend="subprocess",
        )
        tmux_handle = adapter.handle_from_worker_session(
            WorkerSession(
                session_id="session-tmux",
                worker_id="worker-tmux",
                assignment_id="assign-tmux",
                backend="tmux",
                role="leader",
                status=WorkerSessionStatus.ACTIVE,
                handle_snapshot={"backend": "tmux", "metadata": {}},
            ),
            backend="tmux",
        )
        codex_handle = adapter.handle_from_worker_session(
            WorkerSession(
                session_id="session-codex",
                worker_id="worker-codex",
                assignment_id="assign-codex",
                backend="codex_cli",
                role="teammate",
                status=WorkerSessionStatus.ACTIVE,
                handle_snapshot={"backend": "codex_cli", "metadata": {}},
            ),
            backend="codex_cli",
        )

        self.assertEqual(in_process_handle.metadata.get("transport_class"), "full_resident_transport")
        self.assertEqual(tmux_handle.metadata.get("transport_class"), "full_resident_transport")
        self.assertEqual(
            subprocess_handle.metadata.get("transport_class"),
            "ephemeral_worker_transport",
        )
        self.assertEqual(
            codex_handle.metadata.get("transport_class"),
            "ephemeral_worker_transport",
        )
