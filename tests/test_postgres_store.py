from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PACKAGE_ROOT = SRC / "agent_orchestra"
if "agent_orchestra" not in sys.modules:
    package = types.ModuleType("agent_orchestra")
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["agent_orchestra"] = package
if "agent_orchestra.contracts" not in sys.modules:
    contracts_package = types.ModuleType("agent_orchestra.contracts")
    contracts_package.__path__ = [str(PACKAGE_ROOT / "contracts")]
    sys.modules["agent_orchestra.contracts"] = contracts_package

from agent_orchestra.contracts.blackboard import BlackboardEntry, BlackboardSnapshot
from agent_orchestra.contracts.agent import AgentSession
from agent_orchestra.contracts.authority import AuthorityDecision, ScopeExtensionRequest
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.daemon import (
    AgentIncarnation,
    AgentIncarnationStatus,
    AgentSlot,
    AgentSlotStatus,
    ProviderRouteHealth,
    ProviderRouteStatus,
    SessionAttachment,
    SessionAttachmentStatus,
    SlotFailureClass,
    SlotHealthEvent,
)
from agent_orchestra.contracts.enums import BlackboardEntryKind, BlackboardKind, TaskScope, TaskStatus, WorkerStatus
from agent_orchestra.contracts.execution import (
    WorkerHandle,
    WorkerRecord,
    WorkerSession,
    WorkerSessionStatus,
    WorkerTransportLocator,
)
from agent_orchestra.contracts.hierarchical_review import (
    CrossTeamLeaderReview,
    HierarchicalReviewPhase,
    ReviewItemKind,
    ReviewFreshnessState,
    ReviewFreshnessStatus,
    ReviewPhaseTransition,
    ReviewItemRef,
    SuperLeaderSynthesis,
    TeamPositionReview,
)
from agent_orchestra.contracts.planning_review import (
    LeaderDraftPlan,
    LeaderPeerReview,
    LeaderRevisedPlan,
    PlanningReviewSeverity,
    PlanningSlice,
    SuperLeaderGlobalReview,
)
from agent_orchestra.contracts.session_continuity import (
    ConversationHead,
    ConversationHeadKind,
    ResidentTeamShell,
    ResidentTeamShellStatus,
    RuntimeGeneration,
    RuntimeGenerationContinuityMode,
    RuntimeGenerationStatus,
    SessionEvent,
    WorkSession,
    WorkSessionMessage,
)
from agent_orchestra.contracts.session_memory import (
    AgentTurnActorRole,
    AgentTurnKind,
    AgentTurnRecord,
    AgentTurnStatus,
    ArtifactRef,
    ArtifactRefKind,
    ArtifactStorageKind,
    SessionMemoryItem,
    SessionMemoryKind,
    ToolInvocationKind,
    ToolInvocationRecord,
)
from agent_orchestra.contracts.session_memory import (
    AgentTurnActorRole,
    AgentTurnKind,
    AgentTurnRecord,
    AgentTurnStatus,
    ArtifactRef,
    ArtifactRefKind,
    ArtifactStorageKind,
    SessionMemoryItem,
    SessionMemoryKind,
    ToolInvocationKind,
    ToolInvocationRecord,
)
from agent_orchestra.contracts.objective import ObjectiveSpec
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.contracts.task_review import (
    TaskReviewExperienceContext,
    TaskReviewRevision,
    TaskReviewSlot,
    TaskReviewStance,
    make_task_review_revision_id,
    make_task_review_slot_id,
)
from agent_orchestra.contracts.team import Group, Team
from agent_orchestra.storage.base import (
    AuthorityDecisionStoreCommit,
    AuthorityRequestStoreCommit,
    CoordinationTransactionStoreCommit,
    CoordinationOutboxRecord,
    DirectedTaskReceiptStoreCommit,
    MailboxConsumeStoreCommit,
    ProtocolBusCursorCommit,
    SessionTransactionStoreCommit,
    TeammateResultStoreCommit,
)
from agent_orchestra.storage.postgres.store import PostgresOrchestrationStore


def _normalize_sql(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip().rstrip(";")


class _FakeAsyncCursor:
    _INSERT_RE = re.compile(
        r"INSERT INTO (?P<schema>\w+)\.(?P<table>\w+) \((?P<columns>.+?)\) "
        r"VALUES \((?P<values>.+?)\) ON CONFLICT \((?P<pk>.+?)\) DO UPDATE SET .+",
        re.IGNORECASE,
    )
    _SELECT_RE = re.compile(
        r"SELECT (?P<columns>.+?) FROM (?P<schema>\w+)\.(?P<table>\w+)"
        r"(?: WHERE (?P<where>.+?))?"
        r"(?: ORDER BY (?P<order>.+))?$",
        re.IGNORECASE,
    )

    def __init__(self, connection: _FakeAsyncConnection) -> None:
        self.connection = connection
        self._results: list[tuple[object, ...]] = []

    async def __aenter__(self) -> _FakeAsyncCursor:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, query: str, params: tuple[object, ...] | list[object] | None = None) -> None:
        normalized = _normalize_sql(query)
        values = list(params or ())
        if normalized.upper() == "SELECT 1":
            self._results = [(1,)]
            return

        if (
            normalized.upper().startswith("UPDATE ")
            and ".worker_sessions" in normalized
            and "RETURNING payload" in normalized
        ):
            self._execute_worker_session_reclaim(normalized, values)
            return

        insert_match = self._INSERT_RE.match(normalized)
        if insert_match is not None:
            table = insert_match.group("table")
            columns = [column.strip() for column in insert_match.group("columns").split(",")]
            row = {column: values[index] for index, column in enumerate(columns)}
            pk_columns = [column.strip() for column in insert_match.group("pk").split(",")]
            key = tuple(row[column] for column in pk_columns)
            if len(key) == 1:
                key = key[0]
            self.connection.tables.setdefault(table, {})[key] = row
            self._results = []
            return

        select_match = self._SELECT_RE.match(normalized)
        if select_match is not None:
            table = select_match.group("table")
            requested_columns = [column.strip() for column in select_match.group("columns").split(",")]
            rows = list(self.connection.tables.get(table, {}).values())
            where_clause = select_match.group("where")
            if where_clause:
                rows = self._filter_rows(rows, where_clause, values)
            order_clause = select_match.group("order")
            if order_clause:
                order_columns = [column.strip() for column in order_clause.split(",")]
                for column in reversed(order_columns):
                    rows.sort(key=lambda row: row.get(column))
            self._results = [tuple(row.get(column) for column in requested_columns) for row in rows]
            return

        raise AssertionError(f"Unexpected SQL in fake Postgres cursor: {normalized}")

    def _execute_worker_session_reclaim(self, query: str, values: list[object]) -> None:
        if "status IN (%s, %s)" not in query:
            raise AssertionError(f"Unexpected worker session UPDATE shape: {query}")
        if len(values) not in (9, 10):
            raise AssertionError(f"Unexpected worker session UPDATE params: {len(values)}")

        session_id = values[5]
        session_rows = self.connection.tables.setdefault("worker_sessions", {})
        row = session_rows.get(session_id)
        if row is None:
            self._results = []
            return

        allowed_statuses = {values[6], values[7]}
        if row.get("status") not in allowed_statuses:
            self._results = []
            return

        now = values[8]
        expires_at = row.get("supervisor_lease_expires_at")
        if expires_at is not None and expires_at >= now:
            self._results = []
            return

        if "supervisor_lease_id IS NULL" in query:
            if row.get("supervisor_lease_id") is not None:
                self._results = []
                return
        elif "supervisor_lease_id = %s" in query:
            expected_previous = values[9]
            if row.get("supervisor_lease_id") != expected_previous:
                self._results = []
                return
        else:
            raise AssertionError(f"Unexpected lease reclaim predicate: {query}")

        row["supervisor_id"] = values[0]
        row["supervisor_lease_id"] = values[1]
        row["supervisor_lease_expires_at"] = values[2]
        row["last_active_at"] = values[3]

        payload_raw = row.get("payload")
        if isinstance(payload_raw, str):
            payload = json.loads(payload_raw)
        elif isinstance(payload_raw, dict):
            payload = dict(payload_raw)
        else:
            payload = {}
        payload_patch = values[4]
        if isinstance(payload_patch, str):
            payload.update(json.loads(payload_patch))
        elif isinstance(payload_patch, dict):
            payload.update(payload_patch)
        row["payload"] = payload
        self._results = [(payload,)]

    def _filter_rows(
        self,
        rows: list[dict[str, object]],
        where_clause: str,
        values: list[object],
    ) -> list[dict[str, object]]:
        filtered = rows
        params = iter(values)
        for raw_condition in where_clause.split(" AND "):
            condition = raw_condition.strip()
            if condition.endswith("= %s"):
                column = condition[:-4].strip()
            else:
                column, _separator, _rest = condition.partition("=")
                column = column.strip()
            expected = next(params)
            filtered = [row for row in filtered if row.get(column) == expected]
        return filtered

    async def fetchone(self) -> tuple[object, ...] | None:
        if not self._results:
            return None
        return self._results[0]

    async def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._results)


class _FakeAsyncConnection:
    def __init__(self) -> None:
        self.tables: dict[str, dict[object, dict[str, object]]] = {}
        self.commits = 0

    def cursor(self) -> _FakeAsyncCursor:
        return _FakeAsyncCursor(self)

    async def commit(self) -> None:
        self.commits += 1


class _NoRoundTripSavePostgresStore(PostgresOrchestrationStore):
    def __init__(self, dsn: str, *, connection_factory) -> None:
        super().__init__(dsn, connection_factory=connection_factory)
        self.disallow_save_worker_session = False

    async def save_worker_session(self, session: WorkerSession) -> None:
        if self.disallow_save_worker_session:
            raise AssertionError("reclaim should not call save_worker_session round-trip")
        await super().save_worker_session(session)


class _OpaqueRuntimeHandle:
    def __str__(self) -> str:
        return "opaque-runtime-handle"


class PostgresOrchestrationStoreTest(IsolatedAsyncioTestCase):
    async def _save_work_session(
        self,
        store: PostgresOrchestrationStore,
        *,
        work_session_id: str,
        objective_id: str,
        runtime_generation_id: str | None = None,
        created_at: str = "2026-04-11T00:00:00+00:00",
    ) -> WorkSession:
        session = WorkSession(
            work_session_id=work_session_id,
            group_id="group-a",
            root_objective_id=objective_id,
            title=f"Session {work_session_id}",
            status="open",
            created_at=created_at,
            updated_at=created_at,
            current_runtime_generation_id=runtime_generation_id,
        )
        await store.save_work_session(session)
        return session

    async def _save_runtime_generation(
        self,
        store: PostgresOrchestrationStore,
        *,
        runtime_generation_id: str,
        work_session_id: str,
        objective_id: str,
        generation_index: int = 0,
        created_at: str = "2026-04-11T00:00:00+00:00",
        status: RuntimeGenerationStatus = RuntimeGenerationStatus.BOOTING,
        continuity_mode: RuntimeGenerationContinuityMode = RuntimeGenerationContinuityMode.FRESH,
    ) -> RuntimeGeneration:
        generation = RuntimeGeneration(
            runtime_generation_id=runtime_generation_id,
            work_session_id=work_session_id,
            generation_index=generation_index,
            status=status,
            continuity_mode=continuity_mode,
            created_at=created_at,
            group_id="group-a",
            objective_id=objective_id,
        )
        await store.save_runtime_generation(generation)
        return generation

    async def test_postgres_schema_includes_resident_team_shells_table(self) -> None:
        store = PostgresOrchestrationStore("postgresql://unused")
        schema_sql = "\n".join(store.get_schema_statements())

        self.assertIn("CREATE TABLE IF NOT EXISTS agent_orchestra.resident_team_shells", schema_sql)
        self.assertIn("resident_team_shell_id TEXT PRIMARY KEY", schema_sql)
        self.assertIn("work_session_id TEXT NOT NULL", schema_sql)
        self.assertIn("runtime_generation_id TEXT NOT NULL", schema_sql)
        self.assertIn("status TEXT NOT NULL", schema_sql)
        self.assertIn("leader_slot_session_id TEXT", schema_sql)
        self.assertIn("updated_at TEXT NOT NULL", schema_sql)
        self.assertIn("last_progress_at TEXT NOT NULL", schema_sql)
        self.assertIn("payload JSONB NOT NULL", schema_sql)

    async def test_postgres_schema_includes_daemon_slot_tables(self) -> None:
        store = PostgresOrchestrationStore("postgresql://unused")
        schema_sql = "\n".join(store.get_schema_statements())

        self.assertIn("CREATE TABLE IF NOT EXISTS agent_orchestra.agent_slots", schema_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS agent_orchestra.agent_incarnations", schema_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS agent_orchestra.slot_health_events", schema_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS agent_orchestra.session_attachments", schema_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS agent_orchestra.provider_route_health", schema_sql)
        self.assertIn("slot_id TEXT PRIMARY KEY", schema_sql)
        self.assertIn("incarnation_id TEXT PRIMARY KEY", schema_sql)
        self.assertIn("event_id TEXT PRIMARY KEY", schema_sql)
        self.assertIn("attachment_id TEXT PRIMARY KEY", schema_sql)
        self.assertIn("route_key TEXT PRIMARY KEY", schema_sql)

    async def test_postgres_store_round_trips_planning_review_artifacts(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )

        runtime_draft = LeaderDraftPlan(
            objective_id="obj-plan-review",
            planning_round_id="round-1",
            leader_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Runtime initial draft",
            sequential_slices=(
                PlanningSlice(
                    slice_id="runtime-core",
                    title="Runtime core",
                    goal="Implement runtime core",
                    reason="Needed first",
                    mode="sequential",
                    owned_paths=("src/agent_orchestra/runtime/group_runtime.py",),
                ),
            ),
            parallel_slices=(),
        )
        infra_draft = LeaderDraftPlan(
            objective_id="obj-plan-review",
            planning_round_id="round-1",
            leader_id="leader:infra",
            lane_id="infra",
            team_id="group-a:team:infra",
            summary="Infra initial draft",
            sequential_slices=(),
            parallel_slices=(
                PlanningSlice(
                    slice_id="infra-tests",
                    title="Infra tests",
                    goal="Add infra tests",
                    reason="Need verification",
                    mode="parallel",
                    parallel_group="infra-batch-1",
                    owned_paths=("tests/test_runtime.py",),
                ),
            ),
        )
        peer_review = LeaderPeerReview(
            review_id="peer-review-1",
            objective_id="obj-plan-review",
            planning_round_id="round-1",
            reviewer_leader_id="leader:runtime",
            reviewer_team_id="group-a:team:runtime",
            target_leader_id="leader:infra",
            target_team_id="group-a:team:infra",
            summary="Potential shared test hotspot.",
            conflict_type="shared_hotspot_conflict",
            severity=PlanningReviewSeverity.HIGH,
            affected_paths=("tests/test_runtime.py",),
            suggested_change="Split test edits into a follow-up slice.",
        )
        global_review = SuperLeaderGlobalReview(
            objective_id="obj-plan-review",
            planning_round_id="round-1",
            superleader_id="superleader:obj-plan-review",
            summary="Reorder slices to avoid shared test contention.",
            activation_blockers=("shared_hotspot:tests/test_runtime.py",),
            required_serialization=("runtime-core -> infra-tests",),
        )
        runtime_revised = LeaderRevisedPlan(
            objective_id="obj-plan-review",
            planning_round_id="round-1",
            leader_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Runtime revised draft",
            sequential_slices=runtime_draft.sequential_slices,
            parallel_slices=(),
            revision_bundle_ref="bundle:runtime:round-1",
        )

        await store.save_leader_draft_plan(runtime_draft)
        await store.save_leader_draft_plan(infra_draft)
        await store.save_leader_peer_review(peer_review)
        await store.save_superleader_global_review(global_review)
        await store.save_leader_revised_plan(runtime_revised)

        loaded_drafts = await store.list_leader_draft_plans(
            "obj-plan-review",
            planning_round_id="round-1",
        )
        loaded_reviews = await store.list_leader_peer_reviews(
            "obj-plan-review",
            planning_round_id="round-1",
        )
        loaded_global = await store.get_superleader_global_review(
            "obj-plan-review",
            planning_round_id="round-1",
        )
        loaded_revised = await store.list_leader_revised_plans(
            "obj-plan-review",
            planning_round_id="round-1",
        )

        self.assertEqual(len(loaded_drafts), 2)
        self.assertEqual(len(loaded_reviews), 1)
        self.assertIsNotNone(loaded_global)
        assert loaded_global is not None
        self.assertEqual(loaded_global.activation_blockers, ("shared_hotspot:tests/test_runtime.py",))
        self.assertEqual(len(loaded_revised), 1)
        self.assertEqual(loaded_revised[0].leader_id, "leader:runtime")
        self.assertEqual(loaded_revised[0].revision_bundle_ref, "bundle:runtime:round-1")

    async def test_postgres_store_persists_daemon_slot_entities(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_daemon",
            objective_id="obj-daemon",
            runtime_generation_id="runtimegen_daemon",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_daemon",
            work_session_id="worksession_daemon",
            objective_id="obj-daemon",
        )

        slot = AgentSlot(
            slot_id="leader:lane:runtime",
            role="leader",
            work_session_id="worksession_daemon",
            resident_team_shell_id="shell-daemon",
            status=AgentSlotStatus.ACTIVE,
            desired_state="active",
            preferred_backend="tmux",
            preferred_transport_class="full_resident_transport",
            current_incarnation_id="inc-daemon",
            current_lease_id="lease-daemon",
            restart_count=1,
            created_at="2026-04-12T10:00:00+00:00",
            updated_at="2026-04-12T10:00:01+00:00",
        )
        incarnation = AgentIncarnation(
            incarnation_id="inc-daemon",
            slot_id=slot.slot_id,
            work_session_id=slot.work_session_id,
            runtime_generation_id="runtimegen_daemon",
            status=AgentIncarnationStatus.ACTIVE,
            backend="tmux",
            transport_locator={"session_name": "ao-runtime"},
            lease_id="lease-daemon",
            restart_generation=1,
            started_at="2026-04-12T10:00:02+00:00",
        )
        health_event = SlotHealthEvent(
            event_id="slotevt-daemon",
            slot_id=slot.slot_id,
            incarnation_id=incarnation.incarnation_id,
            work_session_id=slot.work_session_id,
            event_kind="heartbeat",
            failure_class=None,
            observed_at="2026-04-12T10:00:03+00:00",
            detail="ok",
        )
        attachment = SessionAttachment(
            attachment_id="attach-daemon",
            work_session_id=slot.work_session_id,
            resident_team_shell_id="shell-daemon",
            slot_id=slot.slot_id,
            incarnation_id=incarnation.incarnation_id,
            client_id="cli-1",
            status=SessionAttachmentStatus.ATTACHED,
            attached_at="2026-04-12T10:00:04+00:00",
            detached_at=None,
            last_event_id="evt-1",
        )
        route = ProviderRouteHealth(
            route_key="leader/openai/gpt-5",
            role="leader",
            backend="openai",
            route_fingerprint="model:gpt-5",
            status=ProviderRouteStatus.HEALTHY,
            health_score=0.9,
            consecutive_failures=0,
            last_failure_class=None,
            cooldown_expires_at=None,
            preferred=True,
            updated_at="2026-04-12T10:00:05+00:00",
        )

        await store.save_agent_slot(slot)
        await store.save_agent_incarnation(incarnation)
        await store.append_slot_health_event(health_event)
        await store.save_session_attachment(attachment)
        await store.save_provider_route_health(route)

        loaded_slot = await store.get_agent_slot(slot.slot_id)
        loaded_incarnation = await store.get_agent_incarnation(incarnation.incarnation_id)
        loaded_route = await store.get_provider_route_health(route.route_key)
        self.assertEqual(loaded_slot, slot)
        self.assertEqual(loaded_incarnation, incarnation)
        self.assertEqual(loaded_route, route)
        self.assertEqual(
            [item.slot_id for item in await store.list_agent_slots(work_session_id=slot.work_session_id)],
            [slot.slot_id],
        )
        self.assertEqual(
            [item.incarnation_id for item in await store.list_agent_incarnations(slot_id=slot.slot_id)],
            [incarnation.incarnation_id],
        )
        self.assertEqual(
            [item.event_id for item in await store.list_slot_health_events(slot_id=slot.slot_id)],
            [health_event.event_id],
        )
        self.assertEqual(
            [item.attachment_id for item in await store.list_session_attachments(slot.work_session_id)],
            [attachment.attachment_id],
        )
        self.assertEqual(
            [item.route_key for item in await store.list_provider_route_health(role="leader")],
            [route.route_key],
        )

    async def test_postgres_store_rejects_agent_slot_without_persisted_work_session(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        slot = AgentSlot(
            slot_id="leader:lane:runtime",
            role="leader",
            work_session_id="worksession-missing",
            resident_team_shell_id="shell-missing",
            status=AgentSlotStatus.BOOTING,
            desired_state="active",
            created_at="2026-04-12T10:00:00+00:00",
            updated_at="2026-04-12T10:00:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "AgentSlot work_session_id must reference an existing WorkSession",
        ):
            await store.save_agent_slot(slot)

    async def test_postgres_store_round_trips_review_item_ref(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-review",
                group_id="group-a",
                title="Review objective",
                description="Support review item persistence.",
            )
        )
        await store.save_task(
            TaskCard(
                task_id="task-a",
                goal="Source task for review item.",
                lane="runtime",
                group_id="group-a",
                team_id="team-a",
            )
        )
        item = ReviewItemRef(
            item_id="project-item-1",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            objective_id="obj-review",
            lane_id="runtime",
            team_id="team-a",
            source_task_id="task-a",
            title="Shared interface decision",
            summary="Cross-team review surface.",
            phase=HierarchicalReviewPhase.TEAM_SYNTHESIS,
            phase_entered_at="2026-04-07T12:30:00+00:00",
            phase_transition_count=1,
            last_transition=ReviewPhaseTransition(
                from_phase=HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW,
                to_phase=HierarchicalReviewPhase.TEAM_SYNTHESIS,
                transitioned_at="2026-04-07T12:30:00+00:00",
                actor_id="leader:team-a",
                trigger="team_position_published",
                source_artifact_id="teampos-1",
                metadata={"based_on_task_review_revision_ids": ["rev-1", "rev-2"]},
            ),
            freshness=ReviewFreshnessState(
                status=ReviewFreshnessStatus.STALE,
                last_evaluated_at="2026-04-07T12:45:00+00:00",
                last_reviewed_at="2026-04-07T12:30:00+00:00",
                stale_after_at="2026-04-07T12:40:00+00:00",
                needs_refresh=True,
                freshness_token="rev-2:teampos-1",
                stale_reviewer_ids=("group-a:team:runtime:teammate:2",),
                reason="A newer task-review revision landed after team synthesis.",
            ),
            metadata={"shared_module": "src/agent_orchestra/runtime/group_runtime.py"},
        )

        await store.save_review_item(item)
        loaded = await store.get_review_item(item.item_id)
        listed = await store.list_review_items("obj-review")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.item_kind, ReviewItemKind.PROJECT_ITEM)
        self.assertEqual(loaded.phase, HierarchicalReviewPhase.TEAM_SYNTHESIS)
        self.assertEqual(loaded.phase_transition_count, 1)
        self.assertIsNotNone(loaded.last_transition)
        assert loaded.last_transition is not None
        self.assertEqual(loaded.last_transition.source_artifact_id, "teampos-1")
        self.assertEqual(loaded.freshness.status, ReviewFreshnessStatus.STALE)
        self.assertTrue(loaded.freshness.needs_refresh)
        self.assertEqual(loaded.metadata["shared_module"], "src/agent_orchestra/runtime/group_runtime.py")
        self.assertEqual([review_item.item_id for review_item in listed], ["project-item-1"])

    async def test_postgres_store_persists_team_position_review(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-review",
                group_id="group-a",
                title="Review objective",
                description="Support team position review persistence.",
            )
        )
        await store.save_review_item(
            ReviewItemRef(
                item_id="project-item-1",
                item_kind=ReviewItemKind.PROJECT_ITEM,
                objective_id="obj-review",
                lane_id="runtime",
                team_id="team-a",
                title="Shared interface decision",
                summary="Cross-team review surface.",
            )
        )
        review = TeamPositionReview(
            position_review_id="teampos-1",
            item_id="project-item-1",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T13:00:00+00:00",
            based_on_task_review_revision_ids=("rev-1", "rev-2"),
            team_stance="runtime_owns_this",
            summary="Team A recommends runtime ownership.",
            key_risks=("store coupling",),
        )

        await store.save_team_position_review(review)
        reviews = await store.list_team_position_reviews("project-item-1")

        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].position_review_id, "teampos-1")
        self.assertEqual(reviews[0].key_risks, ("store coupling",))

    async def test_postgres_store_persists_cross_team_leader_review(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-review",
                group_id="group-a",
                title="Review objective",
                description="Support cross-team review persistence.",
            )
        )
        await store.save_review_item(
            ReviewItemRef(
                item_id="project-item-1",
                item_kind=ReviewItemKind.PROJECT_ITEM,
                objective_id="obj-review",
                lane_id="runtime",
                team_id="team-a",
                title="Shared interface decision",
                summary="Cross-team review surface.",
            )
        )
        review = CrossTeamLeaderReview(
            cross_review_id="cross-1",
            item_id="project-item-1",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            reviewer_team_id="team-b",
            reviewer_leader_id="leader:team-b",
            target_team_id="team-a",
            target_position_review_id="teampos-1",
            reviewed_at="2026-04-07T13:05:00+00:00",
            stance="support_with_adjustment",
            agreement_level="partial",
            what_changed_in_my_understanding="Team A exposed rollout ordering risk.",
            challenge_or_support="support",
            suggested_adjustment="Add a project gate.",
        )

        await store.save_cross_team_leader_review(review)
        reviews = await store.list_cross_team_leader_reviews("project-item-1")

        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].cross_review_id, "cross-1")
        self.assertEqual(reviews[0].target_team_id, "team-a")

    async def test_postgres_store_persists_superleader_synthesis(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-review",
                group_id="group-a",
                title="Review objective",
                description="Support synthesis persistence.",
            )
        )
        await store.save_review_item(
            ReviewItemRef(
                item_id="project-item-1",
                item_kind=ReviewItemKind.PROJECT_ITEM,
                objective_id="obj-review",
                lane_id="runtime",
                team_id="team-a",
                title="Shared interface decision",
                summary="Cross-team review surface.",
            )
        )
        synthesis = SuperLeaderSynthesis(
            synthesis_id="synth-1",
            item_id="project-item-1",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            superleader_id="superleader:obj-review",
            synthesized_at="2026-04-07T13:10:00+00:00",
            based_on_team_position_review_ids=("teampos-1",),
            based_on_cross_team_review_ids=("cross-1",),
            final_position="Proceed with runtime ownership after project gate.",
            next_actions=("implement store API",),
        )

        await store.save_superleader_synthesis(synthesis)
        loaded = await store.get_superleader_synthesis("project-item-1")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.synthesis_id, "synth-1")
        self.assertEqual(loaded.next_actions, ("implement store API",))

    async def test_postgres_store_rejects_review_item_without_persisted_objective(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        item = ReviewItemRef(
            item_id="project-item-missing-objective",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            objective_id="obj-missing",
            lane_id="runtime",
            team_id="team-a",
            title="Missing objective",
            summary="Should fail before DB write.",
        )

        with self.assertRaisesRegex(
            ValueError,
            "ReviewItemRef objective_id must reference an existing ObjectiveSpec",
        ):
            await store.save_review_item(item)

    async def test_postgres_store_rejects_team_position_review_without_persisted_review_item(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        review = TeamPositionReview(
            position_review_id="teampos-missing-item",
            item_id="project-item-missing",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T13:00:00+00:00",
            based_on_task_review_revision_ids=("rev-1",),
            team_stance="runtime_owns_this",
            summary="Should fail before DB write.",
        )

        with self.assertRaisesRegex(
            ValueError,
            "TeamPositionReview item_id must reference an existing ReviewItemRef",
        ):
            await store.save_team_position_review(review)

    async def test_postgres_store_upserts_task_review_slot_and_persists_revision_log(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_task(
            TaskCard(
                task_id="task-review-postgres-1",
                goal="Persist review slots.",
                lane="runtime",
                group_id="group-a",
                team_id="group-a:team:runtime",
            )
        )

        slot_id = make_task_review_slot_id(
            task_id="task-review-postgres-1",
            reviewer_agent_id="group-a:team:runtime:teammate:1",
        )
        revision_1 = TaskReviewRevision(
            revision_id=make_task_review_revision_id(),
            slot_id=slot_id,
            task_id="task-review-postgres-1",
            reviewer_agent_id="group-a:team:runtime:teammate:1",
            reviewer_role="teammate",
            created_at="2026-04-07T13:00:00+00:00",
            stance=TaskReviewStance.GOOD_FIT,
            summary="Initial review",
            relation_to_my_work="I touched the runtime path.",
            confidence=0.8,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/agent_orchestra/runtime/group_runtime.py",),
            ),
        )
        await store.upsert_task_review_slot(
            TaskReviewSlot.from_revision(revision_1),
            revision_1,
        )
        revision_2 = TaskReviewRevision(
            revision_id=make_task_review_revision_id(),
            slot_id=slot_id,
            task_id="task-review-postgres-1",
            reviewer_agent_id="group-a:team:runtime:teammate:1",
            reviewer_role="teammate",
            created_at="2026-04-07T13:05:00+00:00",
            replaces_revision_id=revision_1.revision_id,
            stance=TaskReviewStance.NEEDS_AUTHORITY,
            summary="Updated review",
            relation_to_my_work="Blocked on protected files.",
            confidence=0.9,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/agent_orchestra/runtime/group_runtime.py",),
                observed_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            ),
        )
        await store.upsert_task_review_slot(
            TaskReviewSlot.from_revision(revision_2),
            revision_2,
        )

        slots = await store.list_task_review_slots("task-review-postgres-1")
        revisions = await store.list_task_review_revisions("task-review-postgres-1")

        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].latest_revision_id, revision_2.revision_id)
        self.assertEqual(slots[0].stance, TaskReviewStance.NEEDS_AUTHORITY)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0].revision_id, revision_1.revision_id)
        self.assertEqual(revisions[1].revision_id, revision_2.revision_id)

    async def test_postgres_store_rejects_task_review_slot_without_persisted_task(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        revision = TaskReviewRevision(
            revision_id="taskreview_missing_task",
            slot_id="task-missing:review-slot:group-a:team:runtime:teammate:1",
            task_id="task-missing",
            reviewer_agent_id="group-a:team:runtime:teammate:1",
            reviewer_role="teammate",
            created_at="2026-04-07T14:00:00+00:00",
            stance=TaskReviewStance.GOOD_FIT,
            summary="Should fail before DB write.",
            confidence=0.6,
        )
        slot = TaskReviewSlot(
            slot_id="task-missing:review-slot:group-a:team:runtime:teammate:1",
            task_id="task-missing",
            reviewer_agent_id="group-a:team:runtime:teammate:1",
            reviewed_at="2026-04-07T14:00:00+00:00",
            latest_revision_id="rev-missing-task",
            stance=TaskReviewStance.GOOD_FIT,
            summary="Should fail before DB write.",
            confidence=0.6,
        )

        with self.assertRaisesRegex(
            ValueError,
            "TaskReview(?:Slot|Revision) task_id must reference an existing TaskCard",
        ):
            await store.upsert_task_review_slot(slot, revision)

    async def test_postgres_store_persists_runtime_entities(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        group = Group(group_id="group-a", display_name="Group A", metadata={"stage": "phase-2"})
        team = Team(
            team_id="group-a:team:runtime",
            group_id="group-a",
            name="Runtime",
            member_ids=("leader:runtime", "group-a:team:runtime:teammate:1"),
            metadata={"lane": "runtime"},
        )
        objective = ObjectiveSpec(
            objective_id="obj-1",
            group_id="group-a",
            title="Promote persistence",
            description="Persist the covered orchestration entities.",
            success_metrics=("state persisted",),
            hard_constraints=("keep leader loop green",),
            budget={"max_iterations": 2},
            metadata={"slice": "persistence"},
        )
        team_task = TaskCard(
            task_id="task-team-1",
            goal="Persist runtime delivery state.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            scope=TaskScope.TEAM,
            owned_paths=("src/agent_orchestra/storage/postgres/store.py",),
            verification_commands=("python3 -m unittest tests.test_postgres_store -v",),
            created_by="leader:runtime",
            reason="Need durable delivery-state storage.",
            status=TaskStatus.IN_PROGRESS,
        )
        lane_task = TaskCard(
            task_id="task-lane-1",
            goal="Keep the lane coordination task visible.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            scope=TaskScope.LEADER_LANE,
            created_by="superleader.runtime",
        )
        entry = BlackboardEntry(
            entry_id="entry-1",
            blackboard_id="group-a:team:group-a:team:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
            author_id="group-a:team:runtime:teammate:1",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-team-1",
            summary="Persisted task metadata.",
            payload={"artifact": "store.py"},
            created_at="2026-04-04T08:00:00+00:00",
        )
        snapshot = BlackboardSnapshot(
            blackboard_id="group-a:team:group-a:team:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            version=2,
            summary="Persisted team blackboard snapshot.",
            latest_entry_ids=("entry-1",),
            open_blockers=(),
            open_proposals=("proposal-1",),
        )

        await store.save_group(group)
        await store.save_team(team)
        await store.save_objective(objective)
        await store.save_task(team_task)
        await store.save_task(lane_task)
        await store.save_blackboard_entry(entry)
        await store.save_blackboard_snapshot(snapshot)

        loaded_group = await store.get_group("group-a")
        loaded_team = await store.get_team("group-a:team:runtime")
        teams = await store.list_teams("group-a")
        loaded_objective = await store.get_objective("obj-1")
        loaded_task = await store.get_task("task-team-1")
        filtered_tasks = await store.list_tasks(
            "group-a",
            team_id="group-a:team:runtime",
            lane_id="runtime",
            scope=TaskScope.TEAM.value,
        )
        loaded_entries = await store.list_blackboard_entries("group-a:team:group-a:team:runtime")
        loaded_snapshot = await store.get_blackboard_snapshot("group-a:team:group-a:team:runtime")

        self.assertEqual(loaded_group, group)
        self.assertEqual(loaded_team, team)
        self.assertEqual(teams, [team])
        self.assertEqual(loaded_objective, objective)
        self.assertEqual(loaded_task, team_task)
        self.assertEqual(filtered_tasks, [team_task])
        self.assertEqual(loaded_entries, [entry])
        self.assertEqual(loaded_snapshot, snapshot)

    async def test_postgres_store_defaults_blackboard_entry_created_at_when_missing(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        entry = BlackboardEntry(
            entry_id="entry-created-at-default",
            blackboard_id="group-a:team:group-a:team:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.DIRECTIVE,
            author_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Runtime directive without explicit timestamp.",
        )

        await store.save_blackboard_entry(entry)

        loaded = await store.list_blackboard_entries("group-a:team:group-a:team:runtime")
        stored_row = connection.tables["blackboard_entries"]["entry-created-at-default"]

        self.assertEqual(len(loaded), 1)
        self.assertIsNotNone(loaded[0].created_at)
        assert loaded[0].created_at is not None
        self.assertRegex(loaded[0].created_at, r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(stored_row["created_at"], loaded[0].created_at)

    async def test_postgres_store_normalizes_blank_work_session_timestamps_before_ordering(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        explicit = WorkSession(
            work_session_id="worksession_explicit",
            group_id="group-a",
            root_objective_id="obj-1",
            title="Explicit timestamp",
            status="open",
            created_at="2026-04-10T10:00:00+00:00",
            updated_at="2026-04-10T10:00:00+00:00",
        )
        blank = WorkSession(
            work_session_id="worksession_blank",
            group_id="group-a",
            root_objective_id="obj-1",
            title="Blank timestamp",
            status="open",
        )

        await store.save_work_session(blank)
        await store.save_work_session(explicit)

        listed = await store.list_work_sessions("group-a", root_objective_id="obj-1")
        loaded_blank = await store.get_work_session("worksession_blank")

        self.assertEqual([session.work_session_id for session in listed][0], "worksession_explicit")
        self.assertIsNotNone(loaded_blank)
        assert loaded_blank is not None
        self.assertRegex(loaded_blank.created_at, r"^\d{4}-\d{2}-\d{2}T")
        self.assertRegex(loaded_blank.updated_at, r"^\d{4}-\d{2}-\d{2}T")

    async def test_postgres_store_commits_session_transaction(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        work_session = WorkSession(
            work_session_id="worksession_txn",
            group_id="group-a",
            root_objective_id="obj-1",
            title="Transactional session",
            status="open",
            created_at="2026-04-12T09:00:00+00:00",
            updated_at="2026-04-12T09:00:00+00:00",
            current_runtime_generation_id="runtimegen_txn",
        )
        generation = RuntimeGeneration(
            runtime_generation_id="runtimegen_txn",
            work_session_id="worksession_txn",
            generation_index=0,
            status=RuntimeGenerationStatus.BOOTING,
            continuity_mode=RuntimeGenerationContinuityMode.FRESH,
            created_at="2026-04-12T09:00:00+00:00",
            group_id="group-a",
            objective_id="obj-1",
        )
        message = WorkSessionMessage(
            message_id="wsmsg_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            role="system",
            content="Transactional session created.",
            content_kind="summary",
            created_at="2026-04-12T09:00:01+00:00",
        )
        head = ConversationHead(
            conversation_head_id="convhead_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="runtime",
            backend="codex_cli",
            model="gpt-5",
            provider="openai",
            last_response_id="resp_txn",
            checkpoint_summary="Checkpoint",
            updated_at="2026-04-12T09:00:02+00:00",
        )
        event = SessionEvent(
            session_event_id="sevt_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            event_kind="new_session_created",
            payload={"objective_id": "obj-1"},
            created_at="2026-04-12T09:00:03+00:00",
        )
        turn_record = AgentTurnRecord(
            turn_record_id="turnrec_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="runtime",
            actor_role=AgentTurnActorRole.WORKER,
            assignment_id="assignment_txn",
            turn_kind=AgentTurnKind.WORKER_RESULT,
            input_summary="Run worker",
            output_summary="Worker completed",
            response_id="resp_txn",
            status=AgentTurnStatus.COMPLETED,
            created_at="2026-04-12T09:00:04+00:00",
        )
        tool_record = ToolInvocationRecord(
            tool_invocation_id="toolinv_txn",
            turn_record_id="turnrec_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            tool_name="pytest",
            tool_kind=ToolInvocationKind.LOCAL_COMMAND,
            input_summary="pytest -q",
            output_summary="passed",
            status="completed",
            started_at="2026-04-12T09:00:05+00:00",
            completed_at="2026-04-12T09:00:06+00:00",
        )
        artifact_ref = ArtifactRef(
            artifact_ref_id="artifactref_txn",
            turn_record_id="turnrec_txn",
            tool_invocation_id="toolinv_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            artifact_kind=ArtifactRefKind.FINAL_REPORT,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path="worker-record:worker-alpha:final-report",
            content_hash="hash-1",
            size_bytes=64,
        )
        memory_item = SessionMemoryItem(
            memory_item_id="memitem_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="runtime",
            memory_kind=SessionMemoryKind.HANDOFF,
            importance=7,
            summary="Worker can resume from final report.",
            source_turn_record_ids=("turnrec_txn",),
            source_artifact_ref_ids=("artifactref_txn",),
            created_at="2026-04-12T09:00:07+00:00",
        )

        await store.commit_session_transaction(
            SessionTransactionStoreCommit(
                work_sessions=(work_session,),
                runtime_generations=(generation,),
                work_session_messages=(message,),
                conversation_heads=(head,),
                session_events=(event,),
                turn_records=(turn_record,),
                tool_invocation_records=(tool_record,),
                artifact_refs=(artifact_ref,),
                session_memory_items=(memory_item,),
            )
        )

        self.assertEqual(await store.get_work_session("worksession_txn"), work_session)
        self.assertEqual(await store.get_runtime_generation("runtimegen_txn"), generation)
        self.assertEqual(
            await store.list_work_session_messages("worksession_txn"),
            [message],
        )
        self.assertEqual(await store.get_conversation_head("convhead_txn"), head)
        self.assertEqual(
            await store.list_session_events("worksession_txn"),
            [event],
        )
        self.assertEqual(await store.list_turn_records("worksession_txn"), [turn_record])
        self.assertEqual(
            await store.list_tool_invocation_records("worksession_txn"),
            [tool_record],
        )
        self.assertEqual(await store.list_artifact_refs("worksession_txn"), [artifact_ref])
        self.assertEqual(
            await store.list_session_memory_items("worksession_txn"),
            [memory_item],
        )

    async def test_postgres_store_persists_session_continuity_entities(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        work_session = WorkSession(
            work_session_id="worksession_1",
            group_id="group-a",
            root_objective_id="obj-1",
            title="Session continuity",
            status="open",
            created_at="2026-04-10T10:00:00+00:00",
            updated_at="2026-04-10T10:00:00+00:00",
            current_runtime_generation_id="runtimegen_2",
            metadata={"entry_mode": "continue"},
        )
        generation_1 = RuntimeGeneration(
            runtime_generation_id="runtimegen_1",
            work_session_id="worksession_1",
            generation_index=0,
            status=RuntimeGenerationStatus.CLOSED,
            continuity_mode=RuntimeGenerationContinuityMode.FRESH,
            created_at="2026-04-10T10:00:00+00:00",
            closed_at="2026-04-10T10:01:00+00:00",
            group_id="group-a",
            objective_id="obj-1",
        )
        generation_2 = RuntimeGeneration(
            runtime_generation_id="runtimegen_2",
            work_session_id="worksession_1",
            generation_index=1,
            status=RuntimeGenerationStatus.DETACHED,
            continuity_mode=RuntimeGenerationContinuityMode.WARM_RESUME,
            created_at="2026-04-10T10:02:00+00:00",
            source_runtime_generation_id="runtimegen_1",
            group_id="group-a",
            objective_id="obj-1",
        )
        message = WorkSessionMessage(
            message_id="wsmsg_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            role="assistant",
            scope_kind="session",
            content="Detached runtime is resumable via warm resume.",
            content_kind="summary",
            created_at="2026-04-10T10:03:00+00:00",
        )
        head = ConversationHead(
            conversation_head_id="convhead_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="runtime",
            backend="codex_cli",
            model="gpt-5",
            provider="openai",
            last_response_id="resp_200",
            checkpoint_summary="Leader checkpoint for detached runtime.",
            checkpoint_metadata={"iteration": 2},
            source_agent_session_id="agent-session-1",
            source_worker_session_id="worker-session-1",
            updated_at="2026-04-10T10:03:30+00:00",
        )
        event = SessionEvent(
            session_event_id="sevt_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            event_kind="warm_resume_started",
            payload={"source_runtime_generation_id": "runtimegen_1"},
            created_at="2026-04-10T10:03:45+00:00",
        )

        await store.save_work_session(work_session)
        await store.save_runtime_generation(generation_1)
        await store.save_runtime_generation(generation_2)
        await store.append_work_session_message(message)
        await store.save_conversation_head(head)
        await store.append_session_event(event)

        loaded_session = await store.get_work_session("worksession_1")
        loaded_generations = await store.list_runtime_generations("worksession_1")
        loaded_messages = await store.list_work_session_messages("worksession_1")
        loaded_head = await store.get_conversation_head("convhead_1")
        loaded_heads = await store.list_conversation_heads("worksession_1")
        loaded_events = await store.list_session_events("worksession_1")
        latest_resumable = await store.find_latest_resumable_runtime_generation("worksession_1")

        self.assertEqual(loaded_session, work_session)
        self.assertEqual(len(loaded_generations), 2)
        self.assertEqual(loaded_generations[1].runtime_generation_id, "runtimegen_2")
        self.assertEqual(loaded_messages, [message])
        self.assertEqual(loaded_head, head)
        self.assertEqual(loaded_heads, [head])
        self.assertEqual(loaded_events, [event])
        self.assertEqual(latest_resumable, generation_2)

    async def test_postgres_store_normalizes_blank_session_event_timestamp_before_ordering(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_1",
        )
        explicit = SessionEvent(
            session_event_id="sevt_explicit",
            work_session_id="worksession_1",
            runtime_generation_id=None,
            event_kind="explicit",
            payload={"kind": "explicit"},
            created_at="2026-04-10T10:03:45+00:00",
        )
        blank = SessionEvent(
            session_event_id="sevt_blank",
            work_session_id="worksession_1",
            runtime_generation_id=None,
            event_kind="blank",
            payload={"kind": "blank"},
        )

        await store.append_session_event(blank)
        await store.append_session_event(explicit)

        loaded = await store.list_session_events("worksession_1")

        self.assertEqual([event.session_event_id for event in loaded][0], "sevt_explicit")
        self.assertRegex(loaded[1].created_at, r"^\d{4}-\d{2}-\d{2}T")

    async def test_postgres_store_rejects_runtime_generation_without_persisted_work_session(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        generation = RuntimeGeneration(
            runtime_generation_id="runtimegen_missing_ws",
            work_session_id="worksession_missing",
            generation_index=0,
            status=RuntimeGenerationStatus.BOOTING,
            continuity_mode=RuntimeGenerationContinuityMode.FRESH,
            created_at="2026-04-12T10:00:00+00:00",
            group_id="group-a",
            objective_id="obj-1",
        )

        with self.assertRaisesRegex(
            ValueError,
            "RuntimeGeneration work_session_id must reference an existing WorkSession",
        ):
            await store.save_runtime_generation(generation)

    async def test_postgres_store_persists_session_memory_entities(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_2",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_2",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=1,
        )
        turn = AgentTurnRecord(
            turn_record_id="turnrec_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            actor_role=AgentTurnActorRole.WORKER,
            assignment_id="assignment-1",
            turn_kind=AgentTurnKind.WORKER_RESULT,
            input_summary="Run worker",
            output_summary="Worker completed",
            response_id="resp_1",
            status=AgentTurnStatus.COMPLETED,
            created_at="2026-04-11T12:00:00+00:00",
        )
        tool = ToolInvocationRecord(
            tool_invocation_id="toolinv_1",
            turn_record_id="turnrec_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            tool_name="pytest",
            tool_kind=ToolInvocationKind.LOCAL_COMMAND,
            input_summary="pytest -q",
            output_summary="passed",
            status="completed",
            started_at="2026-04-11T12:00:01+00:00",
            completed_at="2026-04-11T12:00:02+00:00",
        )
        artifact = ArtifactRef(
            artifact_ref_id="artifactref_1",
            turn_record_id="turnrec_1",
            tool_invocation_id="toolinv_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            artifact_kind=ArtifactRefKind.FINAL_REPORT,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path="worker-record:worker-alpha:final-report",
            content_hash="hash-1",
            size_bytes=64,
        )
        memory = SessionMemoryItem(
            memory_item_id="memitem_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            memory_kind=SessionMemoryKind.HANDOFF,
            importance=6,
            summary="Resume can start from final report.",
            source_turn_record_ids=("turnrec_1",),
            source_artifact_ref_ids=("artifactref_1",),
            created_at="2026-04-11T12:00:03+00:00",
        )

        await store.append_turn_record(turn)
        await store.append_tool_invocation_record(tool)
        await store.save_artifact_ref(artifact)
        await store.save_session_memory_item(memory)

        loaded_turns = await store.list_turn_records(
            "worksession_1",
            runtime_generation_id="runtimegen_2",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
        )
        loaded_tools = await store.list_tool_invocation_records(
            "worksession_1",
            runtime_generation_id="runtimegen_2",
            turn_record_id="turnrec_1",
        )
        loaded_artifacts = await store.list_artifact_refs(
            "worksession_1",
            runtime_generation_id="runtimegen_2",
            turn_record_id="turnrec_1",
        )
        loaded_memory = await store.list_session_memory_items(
            "worksession_1",
            runtime_generation_id="runtimegen_2",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
        )

        self.assertEqual(loaded_turns, [turn])
        self.assertEqual(loaded_tools, [tool])
        self.assertEqual(loaded_artifacts, [artifact])
        self.assertEqual(loaded_memory, [memory])

    async def test_postgres_store_rejects_turn_record_without_persisted_work_session(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        turn = AgentTurnRecord(
            turn_record_id="turnrec_missing_ws",
            work_session_id="worksession_missing",
            runtime_generation_id="runtimegen_missing",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            actor_role=AgentTurnActorRole.WORKER,
            assignment_id="assignment-missing",
            turn_kind=AgentTurnKind.WORKER_RESULT,
            input_summary="Run worker",
            output_summary="Worker completed",
            response_id="resp_missing",
            status=AgentTurnStatus.COMPLETED,
            created_at="2026-04-12T10:05:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "AgentTurnRecord work_session_id must reference an existing WorkSession",
        ):
            await store.append_turn_record(turn)

    async def test_postgres_store_rejects_turn_record_without_persisted_runtime_generation(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_missing",
        )
        turn = AgentTurnRecord(
            turn_record_id="turnrec_missing_gen",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_missing",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            actor_role=AgentTurnActorRole.WORKER,
            assignment_id="assignment-missing-gen",
            turn_kind=AgentTurnKind.WORKER_RESULT,
            input_summary="Run worker",
            output_summary="Worker completed",
            response_id="resp_missing_gen",
            status=AgentTurnStatus.COMPLETED,
            created_at="2026-04-12T10:06:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "AgentTurnRecord runtime_generation_id must reference an existing RuntimeGeneration",
        ):
            await store.append_turn_record(turn)

    async def test_postgres_store_rejects_tool_invocation_without_persisted_turn_record(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_2",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_2",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=1,
        )
        tool = ToolInvocationRecord(
            tool_invocation_id="toolinv_missing_turn",
            turn_record_id="turnrec_missing",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            tool_name="pytest",
            tool_kind=ToolInvocationKind.LOCAL_COMMAND,
            input_summary="pytest -q",
            output_summary="passed",
            status="completed",
            started_at="2026-04-12T10:07:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "ToolInvocationRecord turn_record_id must reference an existing AgentTurnRecord",
        ):
            await store.append_tool_invocation_record(tool)

    async def test_postgres_store_rejects_artifact_ref_without_persisted_tool_invocation(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_2",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_2",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=1,
        )
        await store.append_turn_record(
            AgentTurnRecord(
                turn_record_id="turnrec_1",
                work_session_id="worksession_1",
                runtime_generation_id="runtimegen_2",
                head_kind=ConversationHeadKind.WORKER,
                scope_id="worker-alpha",
                actor_role=AgentTurnActorRole.WORKER,
                assignment_id="assignment-1",
                turn_kind=AgentTurnKind.WORKER_RESULT,
                input_summary="Run worker",
                output_summary="Worker completed",
                response_id="resp_1",
                status=AgentTurnStatus.COMPLETED,
                created_at="2026-04-12T10:08:00+00:00",
            )
        )
        artifact = ArtifactRef(
            artifact_ref_id="artifactref_missing_tool",
            turn_record_id="turnrec_1",
            tool_invocation_id="toolinv_missing",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            artifact_kind=ArtifactRefKind.FINAL_REPORT,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path="worker-record:worker-alpha:final-report",
            content_hash="hash-missing-tool",
            size_bytes=32,
        )

        with self.assertRaisesRegex(
            ValueError,
            "ArtifactRef tool_invocation_id must reference an existing ToolInvocationRecord",
        ):
            await store.save_artifact_ref(artifact)

    async def test_postgres_store_persists_and_queries_resident_team_shells(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_2",
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_2",
            objective_id="obj-2",
            runtime_generation_id="runtimegen_9",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_1",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=0,
            created_at="2026-04-11T08:00:00+00:00",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_2",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=1,
            created_at="2026-04-11T09:00:00+00:00",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_9",
            work_session_id="worksession_2",
            objective_id="obj-2",
            generation_index=0,
            created_at="2026-04-11T10:00:00+00:00",
        )
        shell_1 = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.IDLE,
            leader_slot_session_id="leader-session-1",
            teammate_slot_session_ids=["tm-session-1"],
            attach_state={"mode": "attached"},
            created_at="2026-04-11T08:00:00+00:00",
            updated_at="2026-04-11T08:01:00+00:00",
            last_progress_at="2026-04-11T08:01:00+00:00",
            metadata={"phase": "boot"},
        )
        shell_2 = ResidentTeamShell(
            resident_team_shell_id="shell_002",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_2",
            status=ResidentTeamShellStatus.ATTACHED,
            leader_slot_session_id="leader-session-1",
            teammate_slot_session_ids=["tm-session-1", "tm-session-2"],
            attach_state={"mode": "woken"},
            created_at="2026-04-11T09:00:00+00:00",
            updated_at="2026-04-11T09:02:00+00:00",
            last_progress_at="2026-04-11T09:02:00+00:00",
            metadata={"phase": "steady"},
        )
        shell_3 = ResidentTeamShell(
            resident_team_shell_id="shell_900",
            work_session_id="worksession_2",
            group_id="group-a",
            objective_id="obj-2",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_9",
            status=ResidentTeamShellStatus.RECOVERING,
            created_at="2026-04-11T10:00:00+00:00",
            updated_at="2026-04-11T10:01:00+00:00",
            last_progress_at="2026-04-11T10:01:00+00:00",
        )

        await store.save_resident_team_shell(shell_2)
        await store.save_resident_team_shell(shell_1)
        await store.save_resident_team_shell(shell_3)

        loaded_shell_1 = await store.get_resident_team_shell("shell_001")
        listed_shells = await store.list_resident_team_shells("worksession_1")
        latest_shell = await store.find_latest_resident_team_shell("worksession_1")

        self.assertEqual(loaded_shell_1, shell_1)
        self.assertEqual(
            [shell.resident_team_shell_id for shell in listed_shells],
            ["shell_001", "shell_002"],
        )
        self.assertEqual(latest_shell, shell_2)

    async def test_postgres_store_rejects_resident_team_shell_without_persisted_work_session(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        shell = ResidentTeamShell(
            resident_team_shell_id="shell_missing_work_session",
            work_session_id="worksession_missing",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_missing",
            status=ResidentTeamShellStatus.BOOTING,
            created_at="2026-04-11T08:00:00+00:00",
            updated_at="2026-04-11T08:01:00+00:00",
            last_progress_at="2026-04-11T08:01:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "ResidentTeamShell work_session_id must reference an existing WorkSession",
        ):
            await store.save_resident_team_shell(shell)

    async def test_postgres_store_rejects_resident_team_shell_without_persisted_runtime_generation(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_missing",
        )
        shell = ResidentTeamShell(
            resident_team_shell_id="shell_missing_runtime_generation",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_missing",
            status=ResidentTeamShellStatus.BOOTING,
            created_at="2026-04-11T08:00:00+00:00",
            updated_at="2026-04-11T08:01:00+00:00",
            last_progress_at="2026-04-11T08:01:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "ResidentTeamShell runtime_generation_id must reference an existing RuntimeGeneration",
        ):
            await store.save_resident_team_shell(shell)

    async def test_postgres_store_prefers_latest_resident_shell_by_progress_and_preserves_created_at(
        self,
    ) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_2",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_1",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=0,
            created_at="2026-04-11T08:00:00+00:00",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_2",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=1,
            created_at="2026-04-11T09:00:00+00:00",
        )
        original = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.IDLE,
            created_at="2026-04-11T08:00:00+00:00",
            updated_at="2026-04-11T08:05:00+00:00",
            last_progress_at="2026-04-11T08:05:00+00:00",
        )
        newer_created_stale = ResidentTeamShell(
            resident_team_shell_id="shell_002",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_2",
            status=ResidentTeamShellStatus.ATTACHED,
            created_at="2026-04-11T09:00:00+00:00",
            updated_at="2026-04-11T09:10:00+00:00",
            last_progress_at="2026-04-11T09:10:00+00:00",
        )
        updated_original = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.ATTACHED,
            created_at="2026-04-11T12:00:00+00:00",
            updated_at="2026-04-11T12:10:00+00:00",
            last_progress_at="2026-04-11T12:10:00+00:00",
            metadata={"phase": "steady"},
        )

        await store.save_resident_team_shell(original)
        await store.save_resident_team_shell(newer_created_stale)
        await store.save_resident_team_shell(updated_original)

        loaded = await store.get_resident_team_shell("shell_001")
        latest = await store.find_latest_resident_team_shell("worksession_1")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.created_at, "2026-04-11T08:00:00+00:00")
        self.assertEqual(loaded.updated_at, "2026-04-11T12:10:00+00:00")
        self.assertEqual(loaded.last_progress_at, "2026-04-11T12:10:00+00:00")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.resident_team_shell_id, "shell_001")

    async def test_postgres_store_prefers_latest_resident_shell_by_progress_and_update_time(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_2",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_1",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=0,
            created_at="2026-04-11T08:00:00+00:00",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_2",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=1,
            created_at="2026-04-11T09:00:00+00:00",
        )
        shell_old_created_recent_progress = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.IDLE,
            created_at="2026-04-11T08:00:00+00:00",
            updated_at="2026-04-11T12:05:00+00:00",
            last_progress_at="2026-04-11T12:10:00+00:00",
        )
        shell_newer_created_stale_progress = ResidentTeamShell(
            resident_team_shell_id="shell_002",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_2",
            status=ResidentTeamShellStatus.ATTACHED,
            created_at="2026-04-11T09:00:00+00:00",
            updated_at="2026-04-11T09:10:00+00:00",
            last_progress_at="2026-04-11T09:10:00+00:00",
        )

        await store.save_resident_team_shell(shell_old_created_recent_progress)
        await store.save_resident_team_shell(shell_newer_created_stale_progress)

        latest = await store.find_latest_resident_team_shell("worksession_1")

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.resident_team_shell_id, "shell_001")

    async def test_postgres_store_resave_resident_shell_preserves_created_at(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await self._save_work_session(
            store,
            work_session_id="worksession_1",
            objective_id="obj-1",
            runtime_generation_id="runtimegen_2",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_1",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=0,
            created_at="2026-04-11T08:00:00+00:00",
        )
        await self._save_runtime_generation(
            store,
            runtime_generation_id="runtimegen_2",
            work_session_id="worksession_1",
            objective_id="obj-1",
            generation_index=1,
            created_at="2026-04-11T09:00:00+00:00",
        )
        original = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.IDLE,
            created_at="2026-04-11T08:00:00+00:00",
            updated_at="2026-04-11T08:01:00+00:00",
            last_progress_at="2026-04-11T08:01:00+00:00",
        )
        updated = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_2",
            status=ResidentTeamShellStatus.ATTACHED,
            created_at="2026-04-11T09:30:00+00:00",
            updated_at="2026-04-11T09:31:00+00:00",
            last_progress_at="2026-04-11T09:31:00+00:00",
        )

        await store.save_resident_team_shell(original)
        await store.save_resident_team_shell(updated)

        loaded = await store.get_resident_team_shell("shell_001")
        latest = await store.find_latest_resident_team_shell("worksession_1")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.created_at, "2026-04-11T08:00:00+00:00")
        self.assertEqual(loaded.updated_at, "2026-04-11T09:31:00+00:00")
        self.assertEqual(loaded.last_progress_at, "2026-04-11T09:31:00+00:00")
        self.assertEqual(loaded.runtime_generation_id, "runtimegen_2")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.resident_team_shell_id, "shell_001")

    async def test_postgres_store_round_trips_task_surface_contract_fields(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        task = TaskCard(
            task_id="task-surface-contract-1",
            goal="Persist task mutation contract fields.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            created_by="leader:runtime",
            derived_from="task-parent-1",
            reason="Need a child task for follow-up coverage.",
            authority_decision_payload={
                "request_id": "auth-req-surface-1",
                "decision": "reroute",
                "reroute_task_id": "task-canonical-1",
            },
            superseded_by_task_id="task-canonical-1",
            blocked_by=("task-parent-1",),
            status=TaskStatus.CANCELLED,
        )

        await store.save_task(task)
        loaded = await store.get_task("task-surface-contract-1")

        self.assertEqual(loaded, task)

    async def test_postgres_store_persists_worker_records_and_delivery_states(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )

        await store.save_group(Group(group_id="group-a"))
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-1",
                group_id="group-a",
                title="Runtime objective",
                description="Support durable worker and delivery state.",
            )
        )

        record = WorkerRecord(
            worker_id="leader:runtime",
            assignment_id="task-1:leader-turn-2",
            backend="in_process",
            role="leader",
            status=WorkerStatus.COMPLETED,
            handle=WorkerHandle(
                worker_id="leader:runtime",
                role="leader",
                backend="in_process",
                run_id="run-2",
                process_id=42,
                metadata={"transport": "local"},
            ),
            output_text="Persisted a completed leader turn.",
            response_id="resp-2",
            metadata={"attempts": [{"operation": "reactivate"}]},
            session=WorkerSession(
                session_id="session-runtime",
                worker_id="leader:runtime",
                backend="in_process",
                role="leader",
                status=WorkerSessionStatus.IDLE,
                last_assignment_id="task-1:leader-turn-2",
                last_response_id="resp-2",
                reactivation_count=1,
                metadata={"lane_id": "runtime"},
            ),
        )
        lane_state = DeliveryState(
            delivery_id="obj-1:lane:runtime",
            objective_id="obj-1",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            iteration=2,
            summary="Waiting for the leader to consume teammate results.",
            latest_worker_ids=("leader:runtime", "group-a:team:runtime:teammate:1"),
            mailbox_cursor="env-3",
            metadata={"pending_mailbox_count": 1},
        )
        objective_state = DeliveryState(
            delivery_id="obj-1:objective",
            objective_id="obj-1",
            kind=DeliveryStateKind.OBJECTIVE,
            status=DeliveryStatus.PENDING,
            summary="Objective still has a running lane.",
        )

        await store.save_worker_record(record)
        await store.save_delivery_state(lane_state)
        await store.save_delivery_state(objective_state)

        loaded_record = await store.get_worker_record("leader:runtime")
        records = await store.list_worker_records()
        loaded_state = await store.get_delivery_state("obj-1:lane:runtime")
        delivery_states = await store.list_delivery_states("obj-1")

        self.assertEqual(loaded_record, record)
        self.assertEqual(records, [record])
        self.assertEqual(loaded_state, lane_state)
        self.assertEqual(
            {state.delivery_id for state in delivery_states},
            {"obj-1:lane:runtime", "obj-1:objective"},
        )

    async def test_postgres_store_serializes_unjsonable_worker_handle_metadata(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )

        record = WorkerRecord(
            worker_id="leader:runtime:opaque",
            assignment_id="task-opaque:leader-turn-1",
            backend="subprocess",
            role="leader",
            status=WorkerStatus.RUNNING,
            handle=WorkerHandle(
                worker_id="leader:runtime:opaque",
                role="leader",
                backend="subprocess",
                transport_ref="/tmp/opaque.result.json",
                metadata={
                    "_process": _OpaqueRuntimeHandle(),
                    "transport_class": "ephemeral_worker_transport",
                },
            ),
        )

        await store.save_worker_record(record)

        loaded = await store.get_worker_record("leader:runtime:opaque")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertIsNotNone(loaded.handle)
        assert loaded.handle is not None
        self.assertEqual(loaded.handle.metadata["transport_class"], "ephemeral_worker_transport")
        self.assertEqual(loaded.handle.metadata["_process"], "opaque-runtime-handle")

    async def test_postgres_store_round_trips_structured_delivery_mailbox_cursor(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        lane_state = DeliveryState(
            delivery_id="obj-structured:lane:runtime",
            objective_id="obj-structured",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            iteration=4,
            summary="Resume from a structured mailbox cursor.",
            mailbox_cursor={
                "stream": "mailbox",
                "offset": "4-0",
                "event_id": "env-4",
                "last_envelope_id": "env-4",
            },
            metadata={"pending_mailbox_count": 1},
        )

        await store.save_delivery_state(lane_state)
        loaded_state = await store.get_delivery_state("obj-structured:lane:runtime")

        self.assertIsNotNone(loaded_state)
        assert loaded_state is not None
        self.assertEqual(loaded_state.mailbox_cursor["offset"], "4-0")
        self.assertEqual(loaded_state.mailbox_cursor["event_id"], "env-4")
        self.assertEqual(loaded_state.mailbox_cursor["last_envelope_id"], "env-4")

    async def test_postgres_store_persists_active_session_reclaims_lease_and_tracks_bus_cursor(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        locator = WorkerTransportLocator(
            backend="subprocess",
            working_dir="/tmp/ao",
            spool_dir="/tmp/ao/spool",
            protocol_state_file="/tmp/ao/protocol.json",
            result_file="/tmp/ao/result.json",
            stdout_file="/tmp/ao/stdout.log",
            stderr_file="/tmp/ao/stderr.log",
            last_message_file="/tmp/ao/last_message.txt",
            pid=1234,
            command_fingerprint="fp-1",
        )
        session = WorkerSession(
            session_id="session-1",
            worker_id="worker-1",
            assignment_id="assignment-1",
            backend="subprocess",
            role="teammate",
            status=WorkerSessionStatus.ACTIVE,
            lifecycle_status="running",
            transport_locator=locator,
            protocol_cursor={"stream": "lifecycle", "offset": "10-0", "meta": {"lag": 2}},
            mailbox_cursor={"last_envelope_id": "env-1", "pending": ["env-2"]},
            supervisor_id="supervisor-a",
            supervisor_lease_id="lease-old",
            supervisor_lease_expires_at="2026-04-05T00:00:30+00:00",
        )

        await store.save_worker_session(session)
        session.protocol_cursor["meta"]["lag"] = 99
        session.mailbox_cursor["pending"].append("env-3")
        saved = await store.get_worker_session("session-1")

        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.status, WorkerSessionStatus.ACTIVE)
        self.assertEqual(saved.supervisor_id, "supervisor-a")
        self.assertIsNotNone(saved.transport_locator)
        assert saved.transport_locator is not None
        self.assertEqual(saved.transport_locator.pid, 1234)
        self.assertEqual(saved.protocol_cursor["meta"]["lag"], 2)
        self.assertEqual(saved.mailbox_cursor["pending"], ["env-2"])

        reclaimable = await store.list_reclaimable_worker_sessions(
            now="2026-04-05T00:01:00+00:00",
            statuses=(WorkerSessionStatus.ASSIGNED.value, WorkerSessionStatus.ACTIVE.value),
        )
        self.assertEqual([item.session_id for item in reclaimable], ["session-1"])

        reclaimed = await store.reclaim_worker_session_lease(
            session_id="session-1",
            previous_lease_id="lease-old",
            new_supervisor_id="supervisor-b",
            new_lease_id="lease-new",
            now="2026-04-05T00:01:00+00:00",
            new_expires_at="2026-04-05T00:01:30+00:00",
        )
        self.assertIsNotNone(reclaimed)
        assert reclaimed is not None
        self.assertEqual(reclaimed.supervisor_id, "supervisor-b")
        self.assertEqual(reclaimed.supervisor_lease_id, "lease-new")
        self.assertEqual(reclaimed.supervisor_lease_expires_at, "2026-04-05T00:01:30+00:00")

        await store.save_protocol_bus_cursor(
            stream="lifecycle",
            consumer="supervisor-b",
            cursor={
                "offset": "10-0",
                "checkpoint": {"assignment_id": "assignment-1", "retry": None},
            },
        )
        cursor = await store.get_protocol_bus_cursor(stream="lifecycle", consumer="supervisor-b")
        self.assertEqual(
            cursor,
            {
                "offset": "10-0",
                "checkpoint": {"assignment_id": "assignment-1", "retry": None},
            },
        )

    async def test_postgres_store_normalizes_legacy_closed_status_to_abandoned(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )

        await store.save_worker_session(
            WorkerSession(
                session_id="session-legacy",
                worker_id="worker-legacy",
                assignment_id="assignment-legacy",
                backend="subprocess",
                role="teammate",
                status=WorkerSessionStatus.CLOSED,
            )
        )
        saved = await store.get_worker_session("session-legacy")
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.status, WorkerSessionStatus.ABANDONED)

    async def test_postgres_store_reclaim_is_atomic_without_save_roundtrip(self) -> None:
        connection = _FakeAsyncConnection()
        store = _NoRoundTripSavePostgresStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_worker_session(
            WorkerSession(
                session_id="session-atomic",
                worker_id="worker-atomic",
                assignment_id="assignment-atomic",
                backend="subprocess",
                role="teammate",
                status=WorkerSessionStatus.ACTIVE,
                supervisor_id="supervisor-a",
                supervisor_lease_id="lease-old",
                supervisor_lease_expires_at="2026-04-05T00:00:30+00:00",
            )
        )

        store.disallow_save_worker_session = True
        reclaimed = await store.reclaim_worker_session_lease(
            session_id="session-atomic",
            previous_lease_id="lease-old",
            new_supervisor_id="supervisor-b",
            new_lease_id="lease-new",
            now="2026-04-05T00:01:00+00:00",
            new_expires_at="2026-04-05T00:01:30+00:00",
        )
        self.assertIsNotNone(reclaimed)
        assert reclaimed is not None
        self.assertEqual(reclaimed.supervisor_id, "supervisor-b")
        self.assertEqual(reclaimed.supervisor_lease_id, "lease-new")

    async def test_postgres_store_commits_directed_task_receipt_state_in_single_transaction(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-coordination",
                group_id="group-a",
                title="Receipt commit",
                description="Persist receipt state behind one store transaction.",
            )
        )
        cursor = {
            "offset": "7-0",
            "event_id": "env-7",
            "last_envelope_id": "env-7",
        }
        receipt_task = TaskCard(
            task_id="task-receipt-1",
            goal="Persist the directed claim receipt.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            owner_id="group-a:team:runtime:teammate:1",
            claim_session_id="claim-7",
            claimed_at="2026-04-05T03:00:00+00:00",
            claim_source="teammate.directed",
            status=TaskStatus.IN_PROGRESS,
        )
        receipt_entry = BlackboardEntry(
            entry_id="entry-receipt-7",
            blackboard_id="group-a:blackboard:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
            author_id="group-a:team:runtime:teammate:1",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-receipt-1",
            summary="Receipt committed.",
            payload={
                "event": "task.receipt",
                "coordination_outbox": [
                    {
                        "subject": "task.receipt",
                        "recipient": "leader:runtime",
                        "sender": "group-a:team:runtime:teammate:1",
                        "payload": {"task_id": "task-receipt-1"},
                        "metadata": {"lane_id": "runtime"},
                    }
                ],
            },
            created_at="2026-04-05T03:00:01+00:00",
        )
        receipt_delivery_state = DeliveryState(
            delivery_id="obj-coordination:lane:runtime",
            objective_id="obj-coordination",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            iteration=1,
            summary="Receipt coordination snapshot.",
            active_task_ids=("task-receipt-1",),
            mailbox_cursor=cursor,
        )
        receipt_session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            objective_id="obj-coordination",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            mailbox_cursor={"stream": "mailbox", **cursor},
            current_directive_ids=("task-receipt-1",),
            metadata={"current_claim_session_id": "claim-7"},
        )

        baseline_commits = connection.commits
        await store.commit_directed_task_receipt(
            DirectedTaskReceiptStoreCommit(
                task=receipt_task,
                blackboard_entry=receipt_entry,
                delivery_state=receipt_delivery_state,
                protocol_bus_cursor=ProtocolBusCursorCommit(
                    stream="mailbox",
                    consumer="group-a:team:runtime:teammate:1",
                    cursor=cursor,
                ),
                agent_session=receipt_session,
                post_commit_outbox=(
                    CoordinationOutboxRecord(
                        subject="task.receipt",
                        recipient="leader:runtime",
                        sender="group-a:team:runtime:teammate:1",
                        payload={"task_id": "task-receipt-1"},
                        metadata={"lane_id": "runtime"},
                    ),
                ),
            )
        )

        self.assertEqual(connection.commits - baseline_commits, 1)
        loaded_task = await store.get_task("task-receipt-1")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:runtime")
        loaded_delivery = await store.get_delivery_state("obj-coordination:lane:runtime")
        loaded_cursor = await store.get_protocol_bus_cursor(
            stream="mailbox",
            consumer="group-a:team:runtime:teammate:1",
        )
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")
        loaded_outbox = await store.list_coordination_outbox_records()

        self.assertEqual(loaded_task, receipt_task)
        self.assertEqual(loaded_entries, [receipt_entry])
        self.assertEqual(loaded_delivery, receipt_delivery_state)
        self.assertEqual(loaded_cursor, cursor)
        self.assertEqual(loaded_session, receipt_session)
        self.assertEqual(len(loaded_outbox), 1)
        self.assertEqual(loaded_outbox[0].subject, "task.receipt")
        self.assertEqual(loaded_outbox[0].recipient, "leader:runtime")

    async def test_postgres_store_commits_teammate_result_state_in_single_transaction(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-result",
                group_id="group-a",
                title="Result commit",
                description="Persist result state behind one store transaction.",
            )
        )
        result_task = TaskCard(
            task_id="task-result-1",
            goal="Persist the teammate result.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            owner_id="group-a:team:runtime:teammate:1",
            claim_session_id="claim-8",
            claimed_at="2026-04-05T03:10:00+00:00",
            claim_source="teammate.directed",
            status=TaskStatus.COMPLETED,
        )
        result_entry = BlackboardEntry(
            entry_id="entry-result-8",
            blackboard_id="group-a:blackboard:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
            author_id="group-a:team:runtime:teammate:1",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-result-1",
            summary="Result committed.",
            payload={
                "event": "task.result",
                "coordination_outbox": [
                    {
                        "subject": "task.result",
                        "recipient": "leader:runtime",
                        "sender": "group-a:team:runtime:teammate:1",
                        "payload": {"task_id": "task-result-1", "status": "completed"},
                        "metadata": {"lane_id": "runtime"},
                    }
                ],
            },
            created_at="2026-04-05T03:10:01+00:00",
        )
        result_delivery_state = DeliveryState(
            delivery_id="obj-result:lane:runtime",
            objective_id="obj-result",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            iteration=2,
            summary="Result coordination snapshot.",
            completed_task_ids=("task-result-1",),
            latest_worker_ids=("group-a:team:runtime:teammate:1",),
        )
        result_session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            objective_id="obj-result",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            metadata={"last_worker_session_id": "worker-session-8"},
        )

        baseline_commits = connection.commits
        await store.commit_teammate_result(
            TeammateResultStoreCommit(
                task=result_task,
                blackboard_entry=result_entry,
                delivery_state=result_delivery_state,
                agent_session=result_session,
                post_commit_outbox=(
                    CoordinationOutboxRecord(
                        subject="task.result",
                        recipient="leader:runtime",
                        sender="group-a:team:runtime:teammate:1",
                        payload={"task_id": "task-result-1", "status": "completed"},
                        metadata={"lane_id": "runtime"},
                    ),
                ),
            )
        )

        self.assertEqual(connection.commits - baseline_commits, 1)
        loaded_task = await store.get_task("task-result-1")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:runtime")
        loaded_delivery = await store.get_delivery_state("obj-result:lane:runtime")
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")
        loaded_outbox = await store.list_coordination_outbox_records()

        self.assertEqual(loaded_task, result_task)
        self.assertEqual(loaded_entries, [result_entry])
        self.assertEqual(loaded_delivery, result_delivery_state)
        self.assertEqual(loaded_session, result_session)
        self.assertEqual(len(loaded_outbox), 1)
        self.assertEqual(loaded_outbox[0].subject, "task.result")
        self.assertEqual(loaded_outbox[0].recipient, "leader:runtime")

    async def test_postgres_store_commits_generalized_coordination_transaction(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-coordination-transaction",
                group_id="group-a",
                title="Generalized coordination transaction",
                description="Persist every coordination mutation through one store contract.",
            )
        )
        superseded_task = TaskCard(
            task_id="task-coordination-transaction-1",
            goal="Persist a generalized coordination transaction.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            owner_id="group-a:team:runtime:teammate:1",
            claim_session_id="claim-coordination-1",
            claimed_at="2026-04-09T03:00:00+00:00",
            claim_source="teammate.directed",
            status=TaskStatus.CANCELLED,
            authority_decision_payload={"decision": "reroute"},
            superseded_by_task_id="task-coordination-transaction-1-replacement",
        )
        replacement_task = TaskCard(
            task_id="task-coordination-transaction-1-replacement",
            goal="Continue the rerouted repair.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            status=TaskStatus.PENDING,
            derived_from="task-coordination-transaction-1",
            authority_decision_payload={"decision": "reroute"},
        )
        execution_entry = BlackboardEntry(
            entry_id="entry-coordination-transaction-1",
            blackboard_id="group-a:blackboard:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
            author_id="group-a:team:runtime:teammate:1",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-coordination-transaction-1",
            summary="Generalized transaction committed task mutation.",
            payload={"event": "task.result"},
            created_at="2026-04-09T03:00:01+00:00",
        )
        decision_entry = BlackboardEntry(
            entry_id="entry-coordination-transaction-2",
            blackboard_id="group-a:blackboard:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.DECISION,
            author_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-coordination-transaction-1",
            summary="Generalized transaction committed reroute decision.",
            payload={"event": "authority.decision"},
            created_at="2026-04-09T03:00:02+00:00",
        )
        delivery_state = DeliveryState(
            delivery_id="obj-coordination-transaction:lane:runtime",
            objective_id="obj-coordination-transaction",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Generalized coordination snapshot.",
            pending_task_ids=("task-coordination-transaction-1-replacement",),
            metadata={
                "reroute_links": [
                    {
                        "superseded_task_id": "task-coordination-transaction-1",
                        "replacement_task_id": "task-coordination-transaction-1-replacement",
                    }
                ]
            },
        )
        mailbox_cursor = ProtocolBusCursorCommit(
            stream="mailbox",
            consumer="group-a:team:runtime:teammate:1",
            cursor={
                "stream": "mailbox",
                "event_id": "env-transaction-3",
                "last_envelope_id": "env-transaction-3",
            },
        )
        session_snapshot = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            objective_id="obj-coordination-transaction",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            mailbox_cursor=dict(mailbox_cursor.cursor),
            metadata={"last_authority_decision": "reroute"},
        )
        worker_session = WorkerSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            worker_id="group-a:team:runtime:teammate:1",
            assignment_id="assign-coordination-transaction-1",
            backend="scripted",
            role="teammate",
            status=WorkerSessionStatus.FAILED,
            lifecycle_status="blocked",
            mailbox_cursor=dict(mailbox_cursor.cursor),
            metadata={"last_authority_decision": "reroute"},
        )
        durable_outbox = (
            CoordinationOutboxRecord(
                subject="authority.decision",
                recipient="group-a:team:runtime:teammate:1",
                sender="leader:runtime",
                payload={"task_id": "task-coordination-transaction-1"},
                metadata={"lane_id": "runtime"},
            ),
            CoordinationOutboxRecord(
                subject="task.directive",
                recipient="group-a:team:runtime:teammate:1",
                sender="leader:runtime",
                payload={"task_id": "task-coordination-transaction-1-replacement"},
                metadata={"lane_id": "runtime"},
            ),
        )

        baseline_commits = connection.commits
        await store.commit_coordination_transaction(
            CoordinationTransactionStoreCommit(
                task_mutations=(superseded_task,),
                replacement_tasks=(replacement_task,),
                blackboard_entries=(execution_entry, decision_entry),
                delivery_snapshots=(delivery_state,),
                mailbox_cursors=(mailbox_cursor,),
                session_snapshots=(session_snapshot,),
                worker_session_snapshots=(worker_session,),
                durable_outbox_records=durable_outbox,
                outbox_scope_id="coordination-transaction-postgres-1",
            )
        )

        self.assertEqual(connection.commits - baseline_commits, 1)
        loaded_superseded = await store.get_task("task-coordination-transaction-1")
        loaded_replacement = await store.get_task("task-coordination-transaction-1-replacement")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:runtime")
        loaded_delivery = await store.get_delivery_state("obj-coordination-transaction:lane:runtime")
        loaded_cursor = await store.get_protocol_bus_cursor(
            stream="mailbox",
            consumer="group-a:team:runtime:teammate:1",
        )
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")
        loaded_worker_session = await store.get_worker_session(
            "group-a:team:runtime:teammate:1:resident"
        )
        loaded_outbox = await store.list_coordination_outbox_records()

        self.assertEqual(loaded_superseded, superseded_task)
        self.assertEqual(loaded_replacement, replacement_task)
        self.assertEqual(loaded_entries, [execution_entry, decision_entry])
        self.assertEqual(loaded_delivery, delivery_state)
        self.assertEqual(loaded_cursor, dict(mailbox_cursor.cursor))
        self.assertEqual(loaded_session, session_snapshot)
        self.assertEqual(loaded_worker_session, worker_session)
        self.assertEqual(loaded_outbox, list(durable_outbox))

    async def test_postgres_store_commits_authority_request_state_with_durable_outbox(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-authority-request",
                group_id="group-a",
                title="Authority request outbox commit",
                description="Persist authority request coordination outbox in one transaction.",
            )
        )
        authority_task = TaskCard(
            task_id="task-authority-request-1",
            goal="Persist authority request and durable outbox.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            owner_id="group-a:team:runtime:teammate:1",
            claim_session_id="claim-auth-request-1",
            claimed_at="2026-04-06T03:00:00+00:00",
            claim_source="teammate.directed",
            status=TaskStatus.WAITING_FOR_AUTHORITY,
            authority_request_id="auth-req-postgres-1",
            authority_request_payload={"request_id": "auth-req-postgres-1"},
            authority_boundary_class="protected_runtime",
            authority_waiting_since="2026-04-06T03:00:01+00:00",
            authority_resume_target="group-a:team:runtime:teammate:1",
        )
        authority_request = ScopeExtensionRequest(
            request_id="auth-req-postgres-1",
            assignment_id="task-authority-request-1:assignment",
            worker_id="group-a:team:runtime:teammate:1",
            task_id="task-authority-request-1",
            requested_paths=("src/agent_orchestra/runtime/session_host.py",),
            reason="Need protected runtime authority.",
            evidence="runtime boundary hit",
            retry_hint="wait authority decision",
        )
        authority_entry = BlackboardEntry(
            entry_id="entry-authority-request-postgres-1",
            blackboard_id="group-a:blackboard:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.PROPOSAL,
            author_id="group-a:team:runtime:teammate:1",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-authority-request-1",
            summary="Authority request committed with durable outbox.",
            payload={
                "event": "authority.request",
                "authority_request": authority_request.to_dict(),
                "coordination_outbox": [
                    {
                        "subject": "authority.request",
                        "recipient": "leader:runtime",
                        "sender": "group-a:team:runtime:teammate:1",
                        "payload": {"task_id": "task-authority-request-1"},
                        "metadata": {"lane_id": "runtime"},
                    }
                ],
            },
            created_at="2026-04-06T03:00:02+00:00",
        )
        authority_delivery = DeliveryState(
            delivery_id="obj-authority-request:lane:runtime",
            objective_id="obj-authority-request",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.WAITING_FOR_AUTHORITY,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Waiting for authority decision.",
            metadata={"authority_waiting": True},
        )
        authority_session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            objective_id="obj-authority-request",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            metadata={"authority_request_id": "auth-req-postgres-1"},
        )

        baseline_commits = connection.commits
        await store.commit_authority_request(
            AuthorityRequestStoreCommit(
                task=authority_task,
                authority_request=authority_request,
                blackboard_entry=authority_entry,
                delivery_state=authority_delivery,
                agent_session=authority_session,
                post_commit_outbox=(
                    CoordinationOutboxRecord(
                        subject="authority.request",
                        recipient="leader:runtime",
                        sender="group-a:team:runtime:teammate:1",
                        payload={"task_id": "task-authority-request-1"},
                        metadata={"lane_id": "runtime"},
                    ),
                ),
            )
        )

        self.assertEqual(connection.commits - baseline_commits, 1)
        loaded_task = await store.get_task("task-authority-request-1")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:runtime")
        loaded_delivery = await store.get_delivery_state("obj-authority-request:lane:runtime")
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")
        loaded_outbox = await store.list_coordination_outbox_records()

        self.assertEqual(loaded_task, authority_task)
        self.assertEqual(loaded_entries, [authority_entry])
        self.assertEqual(loaded_delivery, authority_delivery)
        self.assertEqual(loaded_session, authority_session)
        self.assertEqual(len(loaded_outbox), 1)
        self.assertEqual(loaded_outbox[0].subject, "authority.request")
        self.assertEqual(loaded_outbox[0].recipient, "leader:runtime")

    async def test_postgres_store_commits_authority_decision_state_with_durable_outbox(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-authority-decision",
                group_id="group-a",
                title="Authority decision outbox commit",
                description="Persist authority decision coordination outbox in one transaction.",
            )
        )
        authority_task = TaskCard(
            task_id="task-authority-decision-1",
            goal="Persist authority decision and durable outbox.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            status=TaskStatus.PENDING,
            authority_decision_payload={"request_id": "auth-req-postgres-2", "decision": "grant"},
        )
        authority_decision = AuthorityDecision(
            request_id="auth-req-postgres-2",
            decision="grant",
            actor_id="leader:runtime",
            scope_class="soft_scope",
            granted_paths=("src/agent_orchestra/runtime/session_host.py",),
            reason="approved",
            summary="Authority granted.",
        )
        authority_entry = BlackboardEntry(
            entry_id="entry-authority-decision-postgres-1",
            blackboard_id="group-a:blackboard:leader",
            group_id="group-a",
            kind=BlackboardKind.LEADER_LANE,
            entry_kind=BlackboardEntryKind.DECISION,
            author_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-authority-decision-1",
            summary="Authority decision committed with durable outbox.",
            payload={
                "event": "authority.decision",
                "authority_decision": authority_decision.to_dict(),
                "coordination_outbox": [
                    {
                        "subject": "authority.decision",
                        "recipient": "group-a:team:runtime:teammate:1",
                        "sender": "leader:runtime",
                        "payload": {"task_id": "task-authority-decision-1"},
                        "metadata": {"lane_id": "runtime"},
                    }
                ],
            },
            created_at="2026-04-06T03:10:02+00:00",
        )
        authority_delivery = DeliveryState(
            delivery_id="obj-authority-decision:lane:runtime",
            objective_id="obj-authority-decision",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Authority decision committed.",
            metadata={"authority_waiting": False},
        )
        authority_session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            objective_id="obj-authority-decision",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            metadata={"authority_last_decision": "grant"},
        )

        baseline_commits = connection.commits
        await store.commit_authority_decision(
            AuthorityDecisionStoreCommit(
                task=authority_task,
                authority_decision=authority_decision,
                blackboard_entry=authority_entry,
                delivery_state=authority_delivery,
                agent_session=authority_session,
                post_commit_outbox=(
                    CoordinationOutboxRecord(
                        subject="authority.decision",
                        recipient="group-a:team:runtime:teammate:1",
                        sender="leader:runtime",
                        payload={"task_id": "task-authority-decision-1"},
                        metadata={"lane_id": "runtime"},
                    ),
                ),
            )
        )

        self.assertEqual(connection.commits - baseline_commits, 1)
        loaded_task = await store.get_task("task-authority-decision-1")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:leader")
        loaded_delivery = await store.get_delivery_state("obj-authority-decision:lane:runtime")
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")
        loaded_outbox = await store.list_coordination_outbox_records()

        self.assertEqual(loaded_task, authority_task)
        self.assertEqual(loaded_entries, [authority_entry])
        self.assertEqual(loaded_delivery, authority_delivery)
        self.assertEqual(loaded_session, authority_session)
        self.assertEqual(len(loaded_outbox), 1)
        self.assertEqual(loaded_outbox[0].subject, "authority.decision")
        self.assertEqual(loaded_outbox[0].recipient, "group-a:team:runtime:teammate:1")

    async def test_postgres_store_commits_task_mutation_state_in_single_transaction(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        await store.save_objective(
            ObjectiveSpec(
                objective_id="obj-task-mutation",
                group_id="group-a",
                title="Task mutation commit",
                description="Persist task mutation coordination in one transaction.",
            )
        )
        mutated_task = TaskCard(
            task_id="task-mutation-postgres-1",
            goal="Mark duplicate runtime task as not needed.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            status=TaskStatus.CANCELLED,
            reason="Covered by another runtime slice.",
            authority_decision_payload={
                "decision": "deny",
                "actor_id": "leader:runtime",
                "reason": "Covered by another runtime slice.",
            },
            blocked_by=("leader.marked_not_needed",),
        )
        mutation_entry = BlackboardEntry(
            entry_id="entry-task-mutation-postgres-1",
            blackboard_id="group-a:blackboard:leader",
            group_id="group-a",
            kind=BlackboardKind.LEADER_LANE,
            entry_kind=BlackboardEntryKind.DECISION,
            author_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-mutation-postgres-1",
            summary="Task mutation committed with durable coordination truth.",
            payload={
                "event": "task.mutation",
                "mutation": {
                    "kind": "not_needed",
                    "actor_id": "leader:runtime",
                    "reason": "Covered by another runtime slice.",
                },
            },
            created_at="2026-04-09T03:10:02+00:00",
        )
        mutation_delivery = DeliveryState(
            delivery_id="obj-task-mutation:lane:runtime",
            objective_id="obj-task-mutation",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Task mutation committed.",
            pending_task_ids=(),
        )

        baseline_commits = connection.commits
        await store.commit_coordination_transaction(
            CoordinationTransactionStoreCommit(
                task_mutations=(mutated_task,),
                blackboard_entries=(mutation_entry,),
                delivery_snapshots=(mutation_delivery,),
                outbox_scope_id=mutation_entry.entry_id,
            )
        )

        self.assertEqual(connection.commits - baseline_commits, 1)
        loaded_task = await store.get_task("task-mutation-postgres-1")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:leader")
        loaded_delivery = await store.get_delivery_state("obj-task-mutation:lane:runtime")

        self.assertEqual(loaded_task, mutated_task)
        self.assertEqual(loaded_entries, [mutation_entry])
        self.assertEqual(loaded_delivery, mutation_delivery)

    async def test_postgres_store_commits_mailbox_consume_state_in_single_transaction(self) -> None:
        connection = _FakeAsyncConnection()
        store = PostgresOrchestrationStore(
            "postgresql://unused",
            connection_factory=lambda: connection,
        )
        cursor = {
            "stream": "mailbox",
            "event_id": "env-11",
            "last_envelope_id": "env-11",
        }
        session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            objective_id="obj-mailbox",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            mailbox_cursor=cursor,
            current_directive_ids=("task-11",),
            last_reason="Committed teammate mailbox consume.",
        )
        worker_session = WorkerSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            worker_id="group-a:team:runtime:teammate:1",
            assignment_id="assign-mailbox-11",
            backend="scripted",
            role="teammate",
            status=WorkerSessionStatus.ACTIVE,
            lifecycle_status="running",
            mailbox_cursor={
                "stream": "mailbox",
                "event_id": "env-9",
                "last_envelope_id": "env-9",
            },
        )

        baseline_commits = connection.commits
        await store.commit_mailbox_consume(
            MailboxConsumeStoreCommit(
                recipient="group-a:team:runtime:teammate:1",
                envelope_ids=("env-10", "env-11"),
                agent_session=session,
                worker_session=worker_session,
                protocol_bus_cursor=ProtocolBusCursorCommit(
                    stream="mailbox",
                    consumer="group-a:team:runtime:teammate:1",
                    cursor=cursor,
                ),
            )
        )

        self.assertEqual(connection.commits - baseline_commits, 1)
        loaded_cursor = await store.get_protocol_bus_cursor(
            stream="mailbox",
            consumer="group-a:team:runtime:teammate:1",
        )
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")
        loaded_worker_session = await store.get_worker_session(
            "group-a:team:runtime:teammate:1:resident"
        )

        self.assertEqual(loaded_cursor, cursor)
        self.assertEqual(loaded_session, session)
        self.assertIsNotNone(loaded_worker_session)
        assert loaded_worker_session is not None
        self.assertEqual(loaded_worker_session.mailbox_cursor, cursor)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
