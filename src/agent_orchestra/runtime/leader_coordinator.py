from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from agent_orchestra.tools.mailbox import MailboxEnvelope


@dataclass(slots=True)
class LeaderCoordinator:
    allow_promptless_convergence: bool = True
    _pending_continuous_convergence: bool = False

    def register_prompt_turn(
        self,
        *,
        produced_mailbox: tuple[MailboxEnvelope, ...],
        has_open_team_tasks: bool = False,
    ) -> None:
        if not self.allow_promptless_convergence:
            self._pending_continuous_convergence = False
            return
        self._pending_continuous_convergence = bool(
            produced_mailbox or has_open_team_tasks
        )

    def should_run_prompt_turn(
        self,
        *,
        mailbox_messages: tuple[MailboxEnvelope, ...],
        has_open_team_tasks: bool,
    ) -> bool:
        if not self.allow_promptless_convergence:
            return True
        if mailbox_messages:
            return False
        if self._pending_continuous_convergence and has_open_team_tasks:
            return False
        return True

    def should_enter_promptless_convergence(
        self,
        *,
        mailbox_messages: tuple[MailboxEnvelope, ...],
        has_open_team_tasks: bool,
        resident_state_available: bool,
        routine_mailbox_only: bool,
        host_owned_open_team_work: bool,
    ) -> bool:
        if not self.allow_promptless_convergence or not resident_state_available:
            return False
        if self._pending_continuous_convergence and (
            mailbox_messages or has_open_team_tasks
        ):
            return True
        if routine_mailbox_only and mailbox_messages:
            return True
        if has_open_team_tasks and host_owned_open_team_work:
            return True
        return False

    def clear_convergence(self) -> None:
        self._pending_continuous_convergence = False
