from __future__ import annotations

import sys
from unittest import IsolatedAsyncioTestCase, TestCase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.planning_review import (
    LeaderDraftPlan,
    LeaderPeerReview,
    LeaderRevisedPlan,
    PlanningSlice,
    SuperLeaderGlobalReview,
)
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class PlanningReviewContractTest(TestCase):
    def test_leader_draft_plan_round_trips_with_sequential_slice(self) -> None:
        draft = LeaderDraftPlan.from_payload(
            {
                "objective_id": "obj-1",
                "planning_round_id": "round-1",
                "leader_id": "leader:runtime",
                "lane_id": "runtime",
                "team_id": "group-a:team:runtime",
                "summary": "Initial runtime draft.",
                "sequential_slices": [
                    {
                        "slice_id": "runtime-core",
                        "title": "Runtime core",
                        "goal": "Implement runtime core.",
                        "reason": "Needed before integration.",
                        "mode": "sequential",
                        "owned_paths": ["src/agent_orchestra/runtime/group_runtime.py"],
                    }
                ],
                "parallel_slices": [],
            }
        )

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft.leader_id, "leader:runtime")
        self.assertEqual(len(draft.sequential_slices), 1)
        self.assertEqual(draft.sequential_slices[0].slice_id, "runtime-core")
        self.assertEqual(
            draft.sequential_slices[0].owned_paths,
            ("src/agent_orchestra/runtime/group_runtime.py",),
        )


class PlanningReviewInMemoryStoreTest(IsolatedAsyncioTestCase):
    async def test_in_memory_store_round_trips_planning_review_artifacts(self) -> None:
        store = InMemoryOrchestrationStore()

        runtime_draft = LeaderDraftPlan(
            objective_id="obj-1",
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
            objective_id="obj-1",
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

        peer_review = LeaderPeerReview.from_payload(
            {
                "review_id": "peer-review-1",
                "objective_id": "obj-1",
                "planning_round_id": "round-1",
                "reviewer_leader_id": "leader:runtime",
                "reviewer_team_id": "group-a:team:runtime",
                "target_leader_id": "leader:infra",
                "target_team_id": "group-a:team:infra",
                "summary": "Potential shared test hotspot.",
                "conflict_type": "shared_hotspot_conflict",
                "severity": "high",
                "affected_paths": ["tests/test_runtime.py"],
                "suggested_change": "Split test edits into a follow-up slice.",
            }
        )
        self.assertIsNotNone(peer_review)
        assert peer_review is not None

        global_review = SuperLeaderGlobalReview.from_payload(
            {
                "objective_id": "obj-1",
                "planning_round_id": "round-1",
                "summary": "Reorder slices to avoid shared test contention.",
                "activation_blockers": ["shared_hotspot:tests/test_runtime.py"],
                "required_serialization": ["runtime-core -> infra-tests"],
            }
        )
        self.assertIsNotNone(global_review)
        assert global_review is not None

        revised_runtime = LeaderRevisedPlan(
            objective_id="obj-1",
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
        await store.save_leader_revised_plan(revised_runtime)

        loaded_drafts = await store.list_leader_draft_plans(
            "obj-1",
            planning_round_id="round-1",
        )
        loaded_reviews = await store.list_leader_peer_reviews(
            "obj-1",
            planning_round_id="round-1",
        )
        loaded_global = await store.get_superleader_global_review(
            "obj-1",
            planning_round_id="round-1",
        )
        loaded_revised = await store.list_leader_revised_plans(
            "obj-1",
            planning_round_id="round-1",
        )

        self.assertEqual(len(loaded_drafts), 2)
        self.assertEqual(len(loaded_reviews), 1)
        self.assertIsNotNone(loaded_global)
        assert loaded_global is not None
        self.assertEqual(loaded_global.planning_round_id, "round-1")
        self.assertEqual(len(loaded_revised), 1)
        self.assertEqual(loaded_revised[0].leader_id, "leader:runtime")
        self.assertEqual(loaded_revised[0].revision_bundle_ref, "bundle:runtime:round-1")
