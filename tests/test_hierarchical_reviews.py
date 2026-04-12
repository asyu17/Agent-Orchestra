from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.hierarchical_review import (
    CrossTeamLeaderReview,
    HierarchicalReviewDigestVisibility,
    HierarchicalReviewDigestSnapshot,
    HierarchicalReviewActor,
    HierarchicalReviewActorRole,
    HierarchicalReviewPhase,
    HierarchicalReviewPolicy,
    HierarchicalReviewReadMode,
    ReviewItemKind,
    ReviewFreshnessState,
    ReviewFreshnessStatus,
    ReviewPhaseTransition,
    ReviewItemRef,
    SuperLeaderSynthesis,
    TeamPositionReview,
    build_cross_team_leader_review_digest,
    build_hierarchical_review_digest_snapshot,
    build_superleader_synthesis_digest,
    build_team_position_review_digest,
)
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class HierarchicalReviewContractTest(TestCase):
    def test_review_subject_ref_distinguishes_task_and_project_items(self) -> None:
        task_item = ReviewItemRef(
            item_id="task-item-1",
            item_kind=ReviewItemKind.TASK_ITEM,
            objective_id="obj-1",
            lane_id="lane-a",
            team_id="team-a",
            source_task_id="task-1",
            title="Runtime task review item",
            summary="Local team-scoped execution item.",
        )
        project_item = ReviewItemRef.from_payload(
            {
                "item_id": "project-item-1",
                "item_kind": ReviewItemKind.PROJECT_ITEM.value,
                "objective_id": "obj-1",
                "title": "Cross-team interface review",
                "summary": "Shared module contract review item.",
                "metadata": {"shared_module": "src/shared/api.py"},
            }
        )

        self.assertEqual(task_item.item_kind, ReviewItemKind.TASK_ITEM)
        self.assertIsNotNone(project_item)
        assert project_item is not None
        self.assertEqual(project_item.item_kind, ReviewItemKind.PROJECT_ITEM)
        self.assertEqual(project_item.metadata["shared_module"], "src/shared/api.py")
        self.assertEqual(project_item.phase, HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW)
        self.assertEqual(project_item.phase_transition_count, 0)
        self.assertEqual(project_item.freshness.status, ReviewFreshnessStatus.UNKNOWN)

    def test_review_item_tracks_phase_transition_and_freshness_bookkeeping(self) -> None:
        item = ReviewItemRef.from_payload(
            {
                "item_id": "project-item-2",
                "item_kind": ReviewItemKind.PROJECT_ITEM.value,
                "objective_id": "obj-2",
                "title": "Runtime review phase truth",
                "summary": "Project item with explicit review phase state.",
                "phase": HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW.value,
                "phase_entered_at": "2026-04-07T12:05:00+00:00",
                "phase_transition_count": 2,
                "last_transition": {
                    "from_phase": HierarchicalReviewPhase.TEAM_SYNTHESIS.value,
                    "to_phase": HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW.value,
                    "transitioned_at": "2026-04-07T12:05:00+00:00",
                    "actor_id": "leader:team-b",
                    "trigger": "cross_team_position_published",
                    "source_artifact_id": "cross-1",
                    "metadata": {"target_team_id": "team-a"},
                },
                "freshness": {
                    "status": ReviewFreshnessStatus.STALE.value,
                    "last_evaluated_at": "2026-04-07T12:10:00+00:00",
                    "last_reviewed_at": "2026-04-07T12:05:00+00:00",
                    "stale_after_at": "2026-04-07T12:09:00+00:00",
                    "needs_refresh": True,
                    "freshness_token": "rev-2:teampos-1:cross-1",
                    "stale_reviewer_ids": ["team-b"],
                    "reason": "A new leader position landed after the last cross-team pass.",
                    "metadata": {"changed_understanding": True},
                },
            }
        )

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.phase, HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW)
        self.assertEqual(item.phase_transition_count, 2)
        self.assertEqual(item.phase_entered_at, "2026-04-07T12:05:00+00:00")
        self.assertIsNotNone(item.last_transition)
        assert item.last_transition is not None
        self.assertEqual(item.last_transition.from_phase, HierarchicalReviewPhase.TEAM_SYNTHESIS)
        self.assertEqual(
            item.last_transition.to_phase,
            HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW,
        )
        self.assertEqual(item.last_transition.source_artifact_id, "cross-1")
        self.assertEqual(item.freshness.status, ReviewFreshnessStatus.STALE)
        self.assertTrue(item.freshness.needs_refresh)
        self.assertEqual(item.freshness.stale_reviewer_ids, ("team-b",))
        self.assertEqual(item.freshness.metadata["changed_understanding"], True)

    def test_team_position_review_captures_team_level_summary(self) -> None:
        review = TeamPositionReview.from_payload(
            {
                "position_review_id": "teampos-1",
                "item_id": "task-item-1",
                "item_kind": ReviewItemKind.TASK_ITEM.value,
                "team_id": "team-a",
                "leader_id": "leader:team-a",
                "reviewed_at": "2026-04-07T12:00:00+00:00",
                "based_on_task_review_revision_ids": ["rev-1", "rev-2"],
                "team_stance": "implement_in_runtime",
                "summary": "Team A agrees the runtime path should own this item.",
                "key_risks": ["store coupling"],
                "key_dependencies": ["postgres schema"],
                "recommended_next_action": "Implement storage APIs first.",
                "confidence": 0.82,
                "evidence_refs": ["bb:entry:1"],
            }
        )

        self.assertIsNotNone(review)
        assert review is not None
        self.assertEqual(review.team_id, "team-a")
        self.assertEqual(review.based_on_task_review_revision_ids, ("rev-1", "rev-2"))
        self.assertEqual(review.key_risks, ("store coupling",))
        self.assertEqual(review.confidence, 0.82)

    def test_cross_team_leader_review_tracks_target_team_and_changed_understanding(self) -> None:
        review = CrossTeamLeaderReview.from_payload(
            {
                "cross_review_id": "cross-1",
                "item_id": "project-item-1",
                "item_kind": ReviewItemKind.PROJECT_ITEM.value,
                "reviewer_team_id": "team-b",
                "reviewer_leader_id": "leader:team-b",
                "target_team_id": "team-a",
                "target_position_review_id": "teampos-1",
                "reviewed_at": "2026-04-07T12:05:00+00:00",
                "stance": "support_with_adjustment",
                "agreement_level": "partial",
                "what_changed_in_my_understanding": "Team A exposed a dependency on schema rollout ordering.",
                "challenge_or_support": "support",
                "suggested_adjustment": "Publish a project item phase gate before integration.",
                "confidence": 0.73,
            }
        )

        self.assertIsNotNone(review)
        assert review is not None
        self.assertEqual(review.target_team_id, "team-a")
        self.assertIn("schema rollout ordering", review.what_changed_in_my_understanding)
        self.assertEqual(review.agreement_level, "partial")

    def test_superleader_synthesis_captures_upstream_review_ids(self) -> None:
        synthesis = SuperLeaderSynthesis.from_payload(
            {
                "synthesis_id": "synth-1",
                "item_id": "project-item-1",
                "item_kind": ReviewItemKind.PROJECT_ITEM.value,
                "superleader_id": "superleader:obj-1",
                "synthesized_at": "2026-04-07T12:10:00+00:00",
                "based_on_team_position_review_ids": ["teampos-1", "teampos-2"],
                "based_on_cross_team_review_ids": ["cross-1"],
                "final_position": "Proceed with runtime-owned schema integration after explicit project gate.",
                "accepted_risks": ["slower rollout"],
                "rejected_paths": ["raw cross-team teammate broadcast"],
                "open_questions": ["Do we need a second verification owner?"],
                "next_actions": ["add schema API", "publish leader review update"],
                "confidence": 0.79,
                "evidence_refs": ["bb:entry:2"],
            }
        )

        self.assertIsNotNone(synthesis)
        assert synthesis is not None
        self.assertEqual(synthesis.based_on_team_position_review_ids, ("teampos-1", "teampos-2"))
        self.assertEqual(synthesis.based_on_cross_team_review_ids, ("cross-1",))
        self.assertEqual(synthesis.next_actions, ("add schema API", "publish leader review update"))

    def test_digest_snapshot_tracks_phase_counts_and_latest_activity(self) -> None:
        item = ReviewItemRef(
            item_id="project-item-1",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            objective_id="obj-1",
            title="Cross-team interface review",
            summary="Shared module contract review item.",
            phase=HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW,
            freshness=ReviewFreshnessState(
                status=ReviewFreshnessStatus.STALE,
                last_evaluated_at="2026-04-07T12:11:00+00:00",
                reason="A newer review landed after the last digest snapshot.",
            ),
        )
        team_review = TeamPositionReview(
            position_review_id="teampos-1",
            item_id=item.item_id,
            item_kind=item.item_kind,
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T12:00:00+00:00",
            summary="Team A recommends runtime ownership.",
        )
        cross_review = CrossTeamLeaderReview(
            cross_review_id="cross-1",
            item_id=item.item_id,
            item_kind=item.item_kind,
            reviewer_team_id="team-b",
            reviewer_leader_id="leader:team-b",
            target_team_id="team-a",
            target_position_review_id="teampos-1",
            reviewed_at="2026-04-07T12:05:00+00:00",
            what_changed_in_my_understanding="Team A exposed a dependency on schema rollout ordering.",
        )
        synthesis = SuperLeaderSynthesis(
            synthesis_id="synth-1",
            item_id=item.item_id,
            item_kind=item.item_kind,
            superleader_id="superleader:obj-1",
            synthesized_at="2026-04-07T12:10:00+00:00",
            based_on_team_position_review_ids=("teampos-1",),
            based_on_cross_team_review_ids=("cross-1",),
            final_position="Proceed after adding the rollout gate.",
        )

        snapshot = build_hierarchical_review_digest_snapshot(
            item,
            team_position_reviews=(team_review,),
            cross_team_leader_reviews=(cross_review,),
            superleader_synthesis=synthesis,
        )

        self.assertIsInstance(snapshot, HierarchicalReviewDigestSnapshot)
        self.assertEqual(snapshot.current_phase, HierarchicalReviewPhase.SUPERLEADER_SYNTHESIS)
        self.assertEqual(snapshot.team_position_review_count, 1)
        self.assertEqual(snapshot.cross_team_leader_review_count, 1)
        self.assertTrue(snapshot.has_superleader_synthesis)
        self.assertEqual(snapshot.latest_activity_at, "2026-04-07T12:10:00+00:00")
        self.assertEqual(snapshot.freshness.status, ReviewFreshnessStatus.STALE)

    def test_team_position_digest_builder_attaches_summary_first_ref_metadata(self) -> None:
        item = ReviewItemRef(
            item_id="task-item-1",
            item_kind=ReviewItemKind.TASK_ITEM,
            objective_id="obj-1",
            team_id="team-a",
            title="Runtime task review item",
            summary="Local team-scoped execution item.",
        )
        older_review = TeamPositionReview(
            position_review_id="teampos-1",
            item_id=item.item_id,
            item_kind=item.item_kind,
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T12:00:00+00:00",
            based_on_task_review_revision_ids=("rev-1",),
            team_stance="implement_in_runtime",
            summary="Team A agrees the runtime path should own this item.",
            recommended_next_action="Implement storage APIs first.",
        )
        newer_review = TeamPositionReview(
            position_review_id="teampos-2",
            item_id=item.item_id,
            item_kind=item.item_kind,
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T12:05:00+00:00",
            based_on_task_review_revision_ids=("rev-1", "rev-2"),
            team_stance="implement_in_runtime",
            summary="Team A refreshed the team position after another review pass.",
        )
        snapshot = build_hierarchical_review_digest_snapshot(
            item,
            team_position_reviews=(older_review, newer_review),
        )
        digest = build_team_position_review_digest(
            item=item,
            review=older_review,
            snapshot=snapshot,
            team_position_reviews=(older_review, newer_review),
            visibility=HierarchicalReviewDigestVisibility(
                visibility_scope="control-private",
                read_mode=HierarchicalReviewReadMode.SUMMARY_PLUS_REF,
                ref_visible=True,
            ),
        )

        self.assertEqual(digest.summary, older_review.summary)
        self.assertEqual(digest.review_ref, older_review.position_review_id)
        self.assertEqual(digest.based_on_task_review_revision_count, 1)
        self.assertFalse(digest.is_latest_for_scope)
        self.assertEqual(
            digest.visibility.read_mode,
            HierarchicalReviewReadMode.SUMMARY_PLUS_REF,
        )

    def test_cross_review_and_synthesis_digest_builders_use_summary_plus_ref_contract(self) -> None:
        item = ReviewItemRef(
            item_id="project-item-1",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            objective_id="obj-1",
            title="Cross-team interface review",
            summary="Shared module contract review item.",
        )
        cross_review = CrossTeamLeaderReview(
            cross_review_id="cross-1",
            item_id=item.item_id,
            item_kind=item.item_kind,
            reviewer_team_id="team-b",
            reviewer_leader_id="leader:team-b",
            target_team_id="team-a",
            target_position_review_id="teampos-1",
            reviewed_at="2026-04-07T12:05:00+00:00",
            stance="support_with_adjustment",
            agreement_level="partial",
            what_changed_in_my_understanding="Team A exposed a dependency on schema rollout ordering.",
            suggested_adjustment="Publish a project item phase gate before integration.",
        )
        synthesis = SuperLeaderSynthesis(
            synthesis_id="synth-1",
            item_id=item.item_id,
            item_kind=item.item_kind,
            superleader_id="superleader:obj-1",
            synthesized_at="2026-04-07T12:10:00+00:00",
            based_on_team_position_review_ids=("teampos-1", "teampos-2"),
            based_on_cross_team_review_ids=("cross-1",),
            final_position="Proceed with runtime-owned schema integration after explicit project gate.",
            next_actions=("add schema API", "publish leader review update"),
        )
        snapshot = build_hierarchical_review_digest_snapshot(
            item,
            cross_team_leader_reviews=(cross_review,),
            superleader_synthesis=synthesis,
        )
        cross_digest = build_cross_team_leader_review_digest(
            item=item,
            review=cross_review,
            snapshot=snapshot,
            cross_team_leader_reviews=(cross_review,),
            visibility=HierarchicalReviewDigestVisibility(
                visibility_scope="shared",
                read_mode=HierarchicalReviewReadMode.SUMMARY_ONLY,
                ref_visible=False,
            ),
        )
        synthesis_digest = build_superleader_synthesis_digest(
            item=item,
            synthesis=synthesis,
            snapshot=snapshot,
            visibility=HierarchicalReviewDigestVisibility(
                visibility_scope="shared",
                read_mode=HierarchicalReviewReadMode.SUMMARY_PLUS_REF,
                ref_visible=True,
            ),
        )

        self.assertEqual(cross_digest.summary, cross_review.what_changed_in_my_understanding)
        self.assertIsNone(cross_digest.review_ref)
        self.assertEqual(synthesis_digest.summary, synthesis.final_position)
        self.assertEqual(synthesis_digest.review_ref, synthesis.synthesis_id)
        self.assertEqual(synthesis_digest.based_on_team_position_review_count, 2)
        self.assertEqual(synthesis_digest.based_on_cross_team_review_count, 1)

    def test_default_policy_keeps_cross_team_reads_summary_first(self) -> None:
        policy = HierarchicalReviewPolicy.default()
        foreign_leader = HierarchicalReviewActor(
            actor_id="leader:team-b",
            role=HierarchicalReviewActorRole.LEADER,
            team_id="team-b",
        )
        teammate = HierarchicalReviewActor(
            actor_id="teammate:team-a:1",
            role=HierarchicalReviewActorRole.TEAMMATE,
            team_id="team-a",
        )
        team_a_review = TeamPositionReview(
            position_review_id="teampos-1",
            item_id="project-item-1",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T13:00:00+00:00",
            based_on_task_review_revision_ids=("rev-1",),
            team_stance="runtime_owns_this",
            summary="Team A summary.",
            key_risks=("store coupling",),
            evidence_refs=("bb:entry:1",),
        )

        read_decision = policy.team_position_read_access(actor=foreign_leader, review=team_a_review)
        create_decision = policy.create_review_item_access(
            actor=teammate,
            item_kind=ReviewItemKind.PROJECT_ITEM,
            item_team_id="team-a",
        )

        self.assertTrue(read_decision.allowed)
        self.assertEqual(read_decision.read_mode, HierarchicalReviewReadMode.SUMMARY_PLUS_REF)
        self.assertFalse(create_decision.allowed)


class HierarchicalReviewInMemoryStoreTest(IsolatedAsyncioTestCase):
    async def test_in_memory_store_round_trips_review_item_phase_truth(self) -> None:
        store = InMemoryOrchestrationStore()
        item = ReviewItemRef(
            item_id="project-item-1",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            objective_id="obj-review",
            lane_id="runtime",
            team_id="team-a",
            source_task_id="task-a",
            title="Shared interface decision",
            summary="Cross-team review surface.",
            phase=HierarchicalReviewPhase.SUPERLEADER_SYNTHESIS,
            phase_entered_at="2026-04-07T13:10:00+00:00",
            phase_transition_count=3,
            last_transition=ReviewPhaseTransition(
                from_phase=HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW,
                to_phase=HierarchicalReviewPhase.SUPERLEADER_SYNTHESIS,
                transitioned_at="2026-04-07T13:10:00+00:00",
                actor_id="superleader:obj-review",
                trigger="superleader_synthesis_published",
                source_artifact_id="synth-1",
                metadata={"based_on_cross_team_review_ids": ["cross-1"]},
            ),
            freshness=ReviewFreshnessState(
                status=ReviewFreshnessStatus.FRESH,
                last_evaluated_at="2026-04-07T13:11:00+00:00",
                last_reviewed_at="2026-04-07T13:10:00+00:00",
                stale_after_at="2026-04-07T14:10:00+00:00",
                freshness_token="teampos-1:cross-1:synth-1",
                stale_reviewer_ids=("team-b",),
            ),
            metadata={"shared_module": "src/agent_orchestra/runtime/group_runtime.py"},
        )

        await store.save_review_item(item)

        loaded = await store.get_review_item(item.item_id)
        listed = await store.list_review_items("obj-review")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.phase, HierarchicalReviewPhase.SUPERLEADER_SYNTHESIS)
        self.assertEqual(loaded.phase_transition_count, 3)
        self.assertIsNotNone(loaded.last_transition)
        assert loaded.last_transition is not None
        self.assertEqual(loaded.last_transition.source_artifact_id, "synth-1")
        self.assertEqual(loaded.freshness.status, ReviewFreshnessStatus.FRESH)
        self.assertEqual(loaded.freshness.freshness_token, "teampos-1:cross-1:synth-1")
        self.assertEqual([review_item.item_id for review_item in listed], ["project-item-1"])
