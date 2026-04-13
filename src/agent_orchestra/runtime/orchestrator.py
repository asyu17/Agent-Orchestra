from __future__ import annotations

from dataclasses import dataclass

from agent_orchestra.bus.base import EventBus
from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.execution import LaunchBackend, Planner
from agent_orchestra.planning.dynamic_superleader import DynamicSuperLeaderPlanner
from agent_orchestra.contracts.runner import AgentRunner
from agent_orchestra.runtime.backends import (
    BackendRegistry,
    CodexCliLaunchBackend,
    InProcessLaunchBackend,
    SubprocessLaunchBackend,
    TmuxLaunchBackend,
)
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.reducer import Reducer
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.base import OrchestrationStore
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore
from agent_orchestra.storage.postgres.store import PostgresOrchestrationStore


@dataclass(slots=True)
class AgentOrchestra:
    store: OrchestrationStore
    bus: EventBus
    reducer: Reducer
    runner: AgentRunner | None = None
    planner: Planner | None = None
    launch_backends: dict[str, LaunchBackend] | None = None
    supervisor: DefaultWorkerSupervisor | None = None

    def group_runtime(self) -> GroupRuntime:
        return GroupRuntime(
            store=self.store,
            bus=self.bus,
            reducer=self.reducer,
            launch_backends=self.launch_backends,
            supervisor=self.supervisor,
        )


def build_in_memory_orchestra(
    *,
    runner: AgentRunner | None = None,
    planner: Planner | None = None,
    launch_backends: dict[str, LaunchBackend] | None = None,
    supervisor: DefaultWorkerSupervisor | None = None,
) -> AgentOrchestra:
    store = InMemoryOrchestrationStore()
    return build_orchestra(
        store=store,
        runner=runner,
        planner=planner,
        launch_backends=launch_backends,
        supervisor=supervisor,
    )


def build_orchestra(
    *,
    store: OrchestrationStore,
    bus: EventBus | None = None,
    runner: AgentRunner | None = None,
    planner: Planner | None = None,
    launch_backends: dict[str, LaunchBackend] | None = None,
    supervisor: DefaultWorkerSupervisor | None = None,
) -> AgentOrchestra:
    active_planner = planner or DynamicSuperLeaderPlanner()
    backends = launch_backends or BackendRegistry(
        {
            "in_process": InProcessLaunchBackend(),
            "subprocess": SubprocessLaunchBackend(),
            "tmux": TmuxLaunchBackend(),
            "codex_cli": CodexCliLaunchBackend(),
        }
    ).as_mapping()
    active_supervisor = supervisor or DefaultWorkerSupervisor(
        store=store,
        launch_backends=backends,
        runner=runner,
    )
    return AgentOrchestra(
        store=store,
        bus=bus or InMemoryEventBus(),
        reducer=Reducer(),
        runner=runner,
        planner=active_planner,
        launch_backends=backends,
        supervisor=active_supervisor,
    )


def build_postgres_orchestra(
    *,
    dsn: str,
    schema: str = "agent_orchestra",
    bus: EventBus | None = None,
    runner: AgentRunner | None = None,
    planner: Planner | None = None,
    launch_backends: dict[str, LaunchBackend] | None = None,
    supervisor: DefaultWorkerSupervisor | None = None,
) -> AgentOrchestra:
    return build_orchestra(
        store=PostgresOrchestrationStore(dsn=dsn, schema=schema),
        bus=bus,
        runner=runner,
        planner=planner,
        launch_backends=launch_backends,
        supervisor=supervisor,
    )


def build_orchestra_for_store_backend(
    *,
    store_backend: str,
    dsn: str | None = None,
    schema: str = "agent_orchestra",
    bus: EventBus | None = None,
    runner: AgentRunner | None = None,
    planner: Planner | None = None,
    launch_backends: dict[str, LaunchBackend] | None = None,
    supervisor: DefaultWorkerSupervisor | None = None,
) -> AgentOrchestra:
    normalized_backend = store_backend.strip().lower()
    if normalized_backend == "in-memory":
        return build_in_memory_orchestra(
            runner=runner,
            planner=planner,
            launch_backends=launch_backends,
            supervisor=supervisor,
        )
    if normalized_backend == "postgres":
        if dsn is None or not dsn.strip():
            raise ValueError("`--dsn` is required when --store-backend=postgres.")
        return build_postgres_orchestra(
            dsn=dsn,
            schema=schema,
            bus=bus,
            runner=runner,
            planner=planner,
            launch_backends=launch_backends,
            supervisor=supervisor,
        )
    raise ValueError(f"Unsupported store backend: {store_backend}")
