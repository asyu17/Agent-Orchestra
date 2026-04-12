from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.enums import TaskScope
from agent_orchestra.contracts.task_review import (
    TaskReviewExperienceContext,
    TaskReviewStance,
    build_task_review_digest,
    make_task_review_revision_id,
    make_task_review_slot_id,
    reduce_task_review_slots,
)
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class TaskReviewContractTest(IsolatedAsyncioTestCase):
    def test_reduce_task_review_slots_and_digest_use_latest_revision_per_agent(self) -> None:
        from agent_orchestra.contracts.task_review import TaskReviewRevision

        task_id = "task-review-1"
        slot_id = make_task_review_slot_id(task_id=task_id, reviewer_agent_id="agent-a")
        old_revision = TaskReviewRevision(
            revision_id=make_task_review_revision_id(),
            slot_id=slot_id,
            task_id=task_id,
            reviewer_agent_id="agent-a",
            reviewer_role="teammate",
            created_at="2026-04-07T10:00:00+00:00",
            replaces_revision_id=None,
            based_on_task_version=1,
            based_on_knowledge_epoch=1,
            stance=TaskReviewStance.GOOD_FIT,
            summary="I can take this.",
            relation_to_my_work="Touches files I already modified.",
            confidence=0.8,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/runtime_a.py",),
            ),
        )
        new_revision = TaskReviewRevision(
            revision_id=make_task_review_revision_id(),
            slot_id=slot_id,
            task_id=task_id,
            reviewer_agent_id="agent-a",
            reviewer_role="teammate",
            created_at="2026-04-07T10:05:00+00:00",
            replaces_revision_id=old_revision.revision_id,
            based_on_task_version=2,
            based_on_knowledge_epoch=2,
            stance=TaskReviewStance.NEEDS_AUTHORITY,
            summary="Need extra authority before taking this.",
            relation_to_my_work="Blocked by files outside my current scope.",
            confidence=0.9,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/runtime_a.py",),
                observed_paths=("src/protected.py",),
            ),
        )
        other_revision = TaskReviewRevision(
            revision_id=make_task_review_revision_id(),
            slot_id=make_task_review_slot_id(task_id=task_id, reviewer_agent_id="agent-b"),
            task_id=task_id,
            reviewer_agent_id="agent-b",
            reviewer_role="teammate",
            created_at="2026-04-07T10:03:00+00:00",
            replaces_revision_id=None,
            based_on_task_version=2,
            based_on_knowledge_epoch=1,
            stance=TaskReviewStance.GOOD_FIT,
            summary="This matches my current workstream.",
            relation_to_my_work="Same lane and same owned paths.",
            confidence=0.7,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/runtime_b.py",),
            ),
        )

        slots = reduce_task_review_slots(task_id, (old_revision, new_revision, other_revision))

        self.assertEqual(len(slots), 2)
        latest_by_agent = {slot.reviewer_agent_id: slot for slot in slots}
        self.assertEqual(latest_by_agent["agent-a"].latest_revision_id, new_revision.revision_id)
        self.assertEqual(latest_by_agent["agent-a"].stance, TaskReviewStance.NEEDS_AUTHORITY)
        self.assertEqual(latest_by_agent["agent-b"].stance, TaskReviewStance.GOOD_FIT)

        digest = build_task_review_digest(task_id, slots)
        self.assertEqual(digest.slot_count, 2)
        self.assertEqual(digest.stance_counts[TaskReviewStance.NEEDS_AUTHORITY.value], 1)
        self.assertEqual(digest.stance_counts[TaskReviewStance.GOOD_FIT.value], 1)
        self.assertEqual(digest.needs_authority_agent_ids, ("agent-a",))
        self.assertEqual(digest.good_fit_agent_ids, ("agent-b",))
        self.assertTrue(any("agent-a" in line for line in digest.summary_lines))

    async def test_in_memory_store_upsert_task_review_slot_tracks_latest_slot_and_revisions(self) -> None:
        from agent_orchestra.contracts.task_review import TaskReviewRevision, TaskReviewSlot

        store = InMemoryOrchestrationStore()
        slot_id = make_task_review_slot_id(
            task_id="task-review-store",
            reviewer_agent_id="agent-a",
        )
        revision_1 = TaskReviewRevision(
            revision_id=make_task_review_revision_id(),
            slot_id=slot_id,
            task_id="task-review-store",
            reviewer_agent_id="agent-a",
            reviewer_role="teammate",
            created_at="2026-04-07T11:00:00+00:00",
            stance=TaskReviewStance.GOOD_FIT,
            summary="Initial review.",
            relation_to_my_work="I already touched adjacent files.",
            confidence=0.6,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/a.py",),
            ),
        )
        slot_1 = TaskReviewSlot.from_revision(revision_1)
        await store.upsert_task_review_slot(slot_1, revision_1)

        revision_2 = TaskReviewRevision(
            revision_id=make_task_review_revision_id(),
            slot_id=slot_id,
            task_id="task-review-store",
            reviewer_agent_id="agent-a",
            reviewer_role="teammate",
            created_at="2026-04-07T11:05:00+00:00",
            replaces_revision_id=revision_1.revision_id,
            stance=TaskReviewStance.NEEDS_SPLIT,
            summary="This should be split first.",
            relation_to_my_work="My earlier work showed hidden sub-steps.",
            confidence=0.75,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/a.py",),
                related_task_ids=("task-parent",),
            ),
        )
        slot_2 = TaskReviewSlot.from_revision(revision_2)
        await store.upsert_task_review_slot(slot_2, revision_2)

        slots = await store.list_task_review_slots("task-review-store")
        revisions = await store.list_task_review_revisions("task-review-store")

        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].latest_revision_id, revision_2.revision_id)
        self.assertEqual(slots[0].stance, TaskReviewStance.NEEDS_SPLIT)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0].revision_id, revision_1.revision_id)
        self.assertEqual(revisions[1].revision_id, revision_2.revision_id)

    async def test_group_runtime_returns_task_claim_context_with_digest_and_latest_reviews(self) -> None:
        runtime = GroupRuntime(
            store=InMemoryOrchestrationStore(),
            bus=InMemoryEventBus(),
        )
        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            lane_id="runtime",
            goal="Implement task review slots",
            scope=TaskScope.TEAM,
            created_by="leader-a",
        )

        await runtime.upsert_task_review(
            task_id=task.task_id,
            reviewer_agent_id="team-a:teammate:1",
            reviewer_role="teammate",
            based_on_task_version=1,
            based_on_knowledge_epoch=1,
            stance=TaskReviewStance.GOOD_FIT,
            summary="I can handle the runtime side.",
            relation_to_my_work="I already changed related runtime files.",
            confidence=0.8,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/agent_orchestra/runtime/group_runtime.py",),
            ),
            reviewed_at="2026-04-07T12:00:00+00:00",
        )
        await runtime.upsert_task_review(
            task_id=task.task_id,
            reviewer_agent_id="team-a:teammate:2",
            reviewer_role="teammate",
            based_on_task_version=1,
            based_on_knowledge_epoch=1,
            stance=TaskReviewStance.HIGH_RISK,
            summary="Need to watch store coupling.",
            relation_to_my_work="I worked on postgres persistence nearby.",
            confidence=0.7,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/agent_orchestra/storage/postgres/store.py",),
            ),
            reviewed_at="2026-04-07T12:01:00+00:00",
        )

        context = await runtime.get_task_claim_context(task.task_id)

        self.assertEqual(context.task.task_id, task.task_id)
        self.assertEqual(len(context.review_slots), 2)
        self.assertEqual(context.review_digest.slot_count, 2)
        self.assertEqual(context.review_digest.good_fit_agent_ids, ("team-a:teammate:1",))
        self.assertEqual(context.review_digest.high_risk_agent_ids, ("team-a:teammate:2",))
        self.assertTrue(any("high_risk" in line for line in context.review_digest.summary_lines))

    async def test_group_runtime_rejects_cross_agent_review_slot_update(self) -> None:
        runtime = GroupRuntime(
            store=InMemoryOrchestrationStore(),
            bus=InMemoryEventBus(),
        )
        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            lane_id="runtime",
            goal="Guard review slot ownership",
            scope=TaskScope.TEAM,
            created_by="leader-a",
        )

        with self.assertRaises(PermissionError):
            await runtime.upsert_task_review(
                task_id=task.task_id,
                actor_id="team-a:teammate:2",
                reviewer_agent_id="team-a:teammate:1",
                reviewer_role="teammate",
                based_on_task_version=1,
                based_on_knowledge_epoch=1,
                stance=TaskReviewStance.GOOD_FIT,
                summary="Improper overwrite attempt.",
                relation_to_my_work="Should be rejected.",
                confidence=0.1,
                experience_context=TaskReviewExperienceContext(),
                reviewed_at="2026-04-07T12:10:00+00:00",
            )
