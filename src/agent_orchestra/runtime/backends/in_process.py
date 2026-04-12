from __future__ import annotations

from agent_orchestra.contracts.execution import (
    LaunchBackend,
    WorkerAssignment,
    WorkerBackendCapabilities,
    WorkerHandle,
    WorkerTransportClass,
)
from agent_orchestra.runtime.backends.base import backend_capability_hints


class InProcessLaunchBackend(LaunchBackend):
    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            transport_class=WorkerTransportClass.FULL_RESIDENT_TRANSPORT,
            supports_protocol_contract=True,
            supports_protocol_final_report=True,
            supports_resume=True,
            supports_reactivate=True,
            supports_artifact_progress=False,
            supports_verification_in_working_dir=True,
        )

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        capabilities = self.describe_capabilities()
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend="in_process",
            run_id=assignment.assignment_id,
            metadata={
                "assignment_id": assignment.assignment_id,
                **backend_capability_hints(capabilities),
            },
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        handle.metadata["cancelled"] = True

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        if assignment is not None:
            handle.run_id = assignment.assignment_id
            handle.metadata["assignment_id"] = assignment.assignment_id
        handle.metadata.update(backend_capability_hints(self.describe_capabilities()))
        return handle
