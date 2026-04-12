from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from agent_orchestra.contracts.agent import SessionBinding
from agent_orchestra.contracts.execution import (
    LaunchBackend,
    WorkerHandle,
    WorkerSession,
    WorkerTransportLocator,
)
from agent_orchestra.runtime.backends.base import describe_backend_capability_hints


class TransportAdapter(ABC):
    @abstractmethod
    def set_launch_backends(self, launch_backends: Mapping[str, LaunchBackend]) -> None:
        raise NotImplementedError

    @abstractmethod
    def snapshot_handle(self, handle: WorkerHandle) -> dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def locator_from_handle(
        self,
        handle: WorkerHandle | None,
        *,
        backend: str,
    ) -> WorkerTransportLocator:
        raise NotImplementedError

    @abstractmethod
    def binding_from_handle(
        self,
        *,
        session_id: str,
        backend: str,
        handle: WorkerHandle | None,
        binding_type: str,
        supervisor_id: str | None,
        lease_id: str | None,
        lease_expires_at: str | None,
        metadata: Mapping[str, object] | None = None,
    ) -> SessionBinding:
        raise NotImplementedError

    @abstractmethod
    def handle_from_worker_session(
        self,
        session: WorkerSession,
        *,
        backend: str,
    ) -> WorkerHandle:
        raise NotImplementedError

    @abstractmethod
    def locator_from_worker_session(self, session: WorkerSession) -> WorkerTransportLocator | None:
        raise NotImplementedError


class DefaultTransportAdapter(TransportAdapter):
    def __init__(
        self,
        *,
        launch_backends: Mapping[str, LaunchBackend] | None = None,
    ) -> None:
        self._launch_backends: dict[str, LaunchBackend] = dict(launch_backends or {})

    def set_launch_backends(self, launch_backends: Mapping[str, LaunchBackend]) -> None:
        self._launch_backends = dict(launch_backends)

    def snapshot_handle(self, handle: WorkerHandle) -> dict[str, object]:
        metadata = {
            key: value
            for key, value in handle.metadata.items()
            if isinstance(key, str) and not key.startswith("_")
        }
        payload: dict[str, object] = {"backend": handle.backend}
        if handle.transport_ref is not None:
            payload["transport_ref"] = handle.transport_ref
        if handle.process_id is not None:
            payload["process_id"] = handle.process_id
        if handle.session_name is not None:
            payload["session_name"] = handle.session_name
        if metadata:
            payload["metadata"] = metadata
        return payload

    def locator_from_handle(
        self,
        handle: WorkerHandle | None,
        *,
        backend: str,
    ) -> WorkerTransportLocator:
        payload: dict[str, object] = {"backend": backend}
        if handle is not None:
            payload.update(self.snapshot_handle(handle))
        return WorkerTransportLocator.from_dict(payload)

    def binding_from_handle(
        self,
        *,
        session_id: str,
        backend: str,
        handle: WorkerHandle | None,
        binding_type: str,
        supervisor_id: str | None,
        lease_id: str | None,
        lease_expires_at: str | None,
        metadata: Mapping[str, object] | None = None,
    ) -> SessionBinding:
        handle_snapshot = self.snapshot_handle(handle) if handle is not None else {}
        locator = self.locator_from_handle(handle, backend=backend)
        return SessionBinding(
            session_id=session_id,
            backend=backend,
            binding_type=binding_type,
            transport_locator=locator.to_dict(),
            supervisor_id=supervisor_id,
            lease_id=lease_id,
            lease_expires_at=lease_expires_at,
            handle_snapshot=handle_snapshot,
            metadata={str(key): value for key, value in (metadata or {}).items()},
        )

    def handle_from_worker_session(
        self,
        session: WorkerSession,
        *,
        backend: str,
    ) -> WorkerHandle:
        snapshot = dict(session.handle_snapshot)
        metadata_payload = snapshot.get("metadata", {})
        if not isinstance(metadata_payload, Mapping):
            metadata_payload = {}
        metadata: dict[str, Any] = {str(key): value for key, value in metadata_payload.items()}
        handle = WorkerHandle(
            worker_id=session.worker_id,
            role=session.role,
            backend=str(snapshot.get("backend", backend)),
            run_id=session.last_assignment_id or session.assignment_id,
            process_id=(
                int(snapshot["process_id"])
                if isinstance(snapshot.get("process_id"), int)
                else None
            ),
            session_name=(
                str(snapshot["session_name"])
                if isinstance(snapshot.get("session_name"), str)
                else None
            ),
            transport_ref=(
                str(snapshot["transport_ref"])
                if isinstance(snapshot.get("transport_ref"), str)
                else None
            ),
            metadata=metadata,
        )
        capability_hints = self._capability_hints(handle.backend)
        for key, value in capability_hints.items():
            handle.metadata.setdefault(key, value)
        return handle

    def locator_from_worker_session(self, session: WorkerSession) -> WorkerTransportLocator | None:
        if session.transport_locator is not None:
            return WorkerTransportLocator.from_dict(session.transport_locator.to_dict())
        if not session.handle_snapshot:
            return None
        payload: dict[str, object] = {"backend": session.backend}
        payload.update(session.handle_snapshot)
        return WorkerTransportLocator.from_dict(payload)

    def _capability_hints(self, backend_name: str) -> dict[str, bool]:
        backend = self._launch_backends.get(backend_name)
        return dict(describe_backend_capability_hints(backend))
