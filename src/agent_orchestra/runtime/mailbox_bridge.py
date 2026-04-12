from __future__ import annotations

from dataclasses import replace

from agent_orchestra.tools.mailbox import MailboxBridge, MailboxCursor, MailboxEnvelope


class InMemoryMailboxBridge(MailboxBridge):
    def __init__(self) -> None:
        self._messages: dict[str, list[MailboxEnvelope]] = {}
        self._cursors: dict[str, MailboxCursor] = {}
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"env-{self._counter}"

    async def send(self, envelope: MailboxEnvelope) -> MailboxEnvelope:
        if envelope.envelope_id is None:
            envelope = replace(envelope, envelope_id=self._next_id())
        if envelope.mailbox_id is None:
            envelope = replace(envelope, mailbox_id=envelope.recipient)
        self._messages.setdefault(envelope.recipient, []).append(envelope)
        self._cursors.setdefault(envelope.recipient, MailboxCursor(recipient=envelope.recipient))
        return envelope

    async def list_for_recipient(
        self,
        recipient: str,
        *,
        after_envelope_id: str | None = None,
    ) -> list[MailboxEnvelope]:
        messages = list(self._messages.get(recipient, []))
        if after_envelope_id is None:
            return messages
        if all(item.envelope_id != after_envelope_id for item in messages):
            return messages
        seen = False
        filtered: list[MailboxEnvelope] = []
        for item in messages:
            if seen:
                filtered.append(item)
            if item.envelope_id == after_envelope_id:
                seen = True
        return filtered

    async def acknowledge(self, recipient: str, envelope_ids: tuple[str, ...]) -> MailboxCursor:
        cursor = self._cursors.get(recipient, MailboxCursor(recipient=recipient))
        acknowledged = tuple(dict.fromkeys(cursor.acknowledged_ids + envelope_ids))
        last_envelope_id = envelope_ids[-1] if envelope_ids else cursor.last_envelope_id
        updated = MailboxCursor(
            recipient=recipient,
            last_envelope_id=last_envelope_id,
            acknowledged_ids=acknowledged,
        )
        self._cursors[recipient] = updated
        return updated

    async def get_cursor(self, recipient: str) -> MailboxCursor:
        return self._cursors.get(recipient, MailboxCursor(recipient=recipient))

    async def poll(self, recipient: str, *, limit: int = 100) -> tuple[MailboxEnvelope, ...]:
        return tuple((await self.list_for_recipient(recipient))[:limit])

    async def ack(self, recipient: str, envelope_id: str) -> MailboxCursor:
        return await self.acknowledge(recipient, (envelope_id,))
