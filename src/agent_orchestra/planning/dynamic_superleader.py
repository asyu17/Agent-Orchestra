from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent_orchestra.contracts.execution import Planner
from agent_orchestra.planning.template import ObjectiveTemplate, PlanningContext, PlanningResult, WorkstreamTemplate
from agent_orchestra.planning.template_planner import TemplatePlanner


def _slugify(text: str) -> str:
    value = text.lower()
    value = re.sub(r"[`*_]+", "", value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "workstream"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return tuple(result)


def _int_value(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _brief_clauses(text: str) -> list[str]:
    normalized = text.replace("\n", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return []
    raw_parts = re.split(r"(?:\s*[.;:]\s+|\s*,\s+|\s+\band\b\s+|\s*[，、；]\s*)", normalized)
    clauses: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        cleaned = part.strip(" -")
        if len(cleaned) < 4:
            continue
        key = _normalize_text(cleaned)
        if key in seen:
            continue
        seen.add(key)
        clauses.append(cleaned)
    return clauses


def _team_name_for_text(text: str, index: int) -> str:
    normalized = _normalize_text(text)
    team_keywords: tuple[tuple[tuple[str, ...], str], ...] = (
        (("worker", "runtime", "retry", "resume", "backend", "supervisor"), "Runtime"),
        (("verify", "verification", "guard", "owned_paths", "test", "qa"), "QA"),
        (("authority", "handoff", "reducer"), "Authority"),
        (("postgres", "persistence", "database", "storage"), "Persistence"),
        (("protocol", "redis", "mailbox", "permission", "control"), "Control"),
        (("plan", "planner", "planning", "dynamic"), "Planning"),
        (("docs", "knowledge", "documentation"), "Docs"),
    )
    for keywords, team_name in team_keywords:
        if any(keyword in normalized for keyword in keywords):
            return team_name
    return f"Team {index}"


@dataclass(slots=True)
class DynamicPlanningConfig:
    max_workstreams: int = 3
    max_replans: int = 1
    default_budget_max_teammates: int = 1
    default_budget_max_iterations: int = 2
    allow_dependency_edges: bool = True

    def __post_init__(self) -> None:
        if self.max_workstreams < 1:
            raise ValueError("max_workstreams must be at least 1")
        if self.max_replans < 0:
            raise ValueError("max_replans must be non-negative")
        if self.default_budget_max_teammates < 0:
            raise ValueError("default_budget_max_teammates must be non-negative")
        if self.default_budget_max_iterations < 0:
            raise ValueError("default_budget_max_iterations must be non-negative")


class DynamicSuperLeaderPlanner(Planner):
    def __init__(
        self,
        config: DynamicPlanningConfig | None = None,
        *,
        template_planner: TemplatePlanner | None = None,
    ) -> None:
        self.config = config or DynamicPlanningConfig()
        self.template_planner = template_planner or TemplatePlanner()

    async def build_initial_plan(self, objective: ObjectiveTemplate) -> PlanningResult:
        if self._should_passthrough(objective, context=None):
            return await self.template_planner.build_initial_plan(objective)
        workstreams = self._synthesize_workstreams(
            objective=objective,
            context=None,
            existing_ids=set(),
            limit=self.config.max_workstreams,
        )
        if not workstreams:
            raise ValueError("Dynamic planner could not synthesize any workstreams from the objective brief")
        dynamic_template = self._compile_dynamic_template(
            objective=objective,
            workstreams=workstreams,
            replan_count=0,
            source=self._planning_source(objective, context=None),
        )
        return await self.template_planner.build_initial_plan(dynamic_template)

    async def replan(self, objective: ObjectiveTemplate, context: PlanningContext) -> PlanningResult:
        if self._should_passthrough(objective, context=context):
            return await self.template_planner.replan(objective, context)

        current_replans = _int_value(objective.metadata.get("dynamic_replan_count"), 0)
        if current_replans >= self.config.max_replans:
            raise ValueError("Dynamic planner exhausted max_replans")

        base_workstreams = list(objective.workstreams)
        if not base_workstreams:
            base_workstreams = self._synthesize_workstreams(
                objective=objective,
                context=None,
                existing_ids=set(),
                limit=self.config.max_workstreams,
            )
        if not base_workstreams:
            raise ValueError("Dynamic planner could not synthesize a base plan before replanning")

        remaining_capacity = max(self.config.max_workstreams - len(base_workstreams), 0)
        additional_candidates = list(context.additional_workstreams)
        synthesized_followups = self._synthesize_workstreams(
            objective=objective,
            context=context,
            existing_ids={item.workstream_id for item in base_workstreams} | {item.workstream_id for item in additional_candidates},
            limit=remaining_capacity,
        )
        additional_candidates.extend(synthesized_followups)
        additional_workstreams = tuple(additional_candidates[:remaining_capacity])
        if not additional_workstreams:
            raise ValueError("Dynamic replanning requires at least one additional bounded workstream")

        dynamic_template = self._compile_dynamic_template(
            objective=objective,
            workstreams=base_workstreams,
            replan_count=current_replans + 1,
            source=self._planning_source(objective, context=context),
        )
        return await self.template_planner.replan(
            dynamic_template,
            PlanningContext(
                reason=context.reason,
                additional_workstreams=additional_workstreams,
                supersede_node_ids=context.supersede_node_ids,
                metadata=dict(context.metadata),
            ),
        )

    def _should_passthrough(self, objective: ObjectiveTemplate, context: PlanningContext | None) -> bool:
        if not objective.workstreams:
            return False
        if self._seed_payloads(objective.metadata):
            return False
        if context is not None and self._seed_payloads(context.metadata):
            return False
        return True

    def _planning_source(self, objective: ObjectiveTemplate, context: PlanningContext | None) -> str:
        if context is not None and self._seed_payloads(context.metadata):
            return "context.dynamic_workstream_seeds"
        if self._seed_payloads(objective.metadata):
            return "objective.dynamic_workstream_seeds"
        return "objective.brief"

    def _compile_dynamic_template(
        self,
        *,
        objective: ObjectiveTemplate,
        workstreams: list[WorkstreamTemplate],
        replan_count: int,
        source: str,
    ) -> ObjectiveTemplate:
        metadata = dict(objective.metadata)
        metadata.update(
            {
                "planner_kind": "dynamic_superleader",
                "planning_mode": "dynamic_superleader",
                "selected_workstream_ids": [item.workstream_id for item in workstreams],
                "dynamic_replan_count": replan_count,
                "dynamic_source": source,
            }
        )
        global_budget = dict(objective.global_budget)
        global_budget.setdefault("max_teams", len(workstreams))
        return ObjectiveTemplate(
            objective_id=objective.objective_id,
            group_id=objective.group_id,
            title=objective.title,
            description=objective.description,
            success_metrics=objective.success_metrics,
            hard_constraints=objective.hard_constraints,
            global_budget=global_budget,
            workstreams=tuple(workstreams),
            metadata=metadata,
        )

    def _synthesize_workstreams(
        self,
        *,
        objective: ObjectiveTemplate,
        context: PlanningContext | None,
        existing_ids: set[str],
        limit: int,
    ) -> list[WorkstreamTemplate]:
        if limit <= 0:
            return []
        workstreams: list[WorkstreamTemplate] = []
        seen = set(existing_ids)
        for index, seed in enumerate(self._iter_seed_candidates(objective, context=context), start=1):
            workstream = self._seed_to_workstream(seed, index=index)
            if workstream.workstream_id in seen:
                continue
            seen.add(workstream.workstream_id)
            workstreams.append(workstream)
            if len(workstreams) >= limit:
                break
        return workstreams

    def _iter_seed_candidates(self, objective: ObjectiveTemplate, context: PlanningContext | None) -> list[object]:
        if context is not None:
            context_seeds = self._seed_payloads(context.metadata)
            if context_seeds:
                return context_seeds
            if context.reason.strip():
                return [context.reason]

        objective_seeds = self._seed_payloads(objective.metadata)
        if objective_seeds:
            return objective_seeds

        clauses = _brief_clauses(objective.description)
        if clauses:
            return clauses
        clauses = _brief_clauses(objective.title)
        if clauses:
            return clauses
        return [objective.title]

    def _seed_payloads(self, metadata: dict[str, Any]) -> list[object]:
        raw = metadata.get("dynamic_workstream_seeds")
        if not isinstance(raw, (list, tuple)):
            return []
        return list(raw)

    def _seed_to_workstream(self, seed: object, *, index: int) -> WorkstreamTemplate:
        if isinstance(seed, str):
            title = seed.strip().rstrip(".")
            summary = title
            seed_metadata: dict[str, Any] = {}
            workstream_id = _slugify(title)
            team_name = _team_name_for_text(summary, index)
            acceptance_checks: tuple[str, ...] = ()
            depends_on: tuple[str, ...] = ()
            budget: dict[str, Any] = {}
        elif isinstance(seed, dict):
            seed_metadata = dict(seed.get("metadata", {})) if isinstance(seed.get("metadata"), dict) else {}
            title_value = seed.get("title") or seed.get("name") or seed.get("summary")
            summary_value = seed.get("summary") or title_value
            if not isinstance(title_value, str) or not title_value.strip():
                raise ValueError("Dynamic workstream seeds must include a non-empty title/summary")
            title = title_value.strip().rstrip(".")
            summary = title
            if isinstance(summary_value, str) and summary_value.strip():
                summary = summary_value.strip()
            workstream_id = str(seed.get("workstream_id") or seed.get("gap_id") or _slugify(title))
            team_name_value = seed.get("team_name")
            if isinstance(team_name_value, str) and team_name_value.strip():
                team_name = team_name_value.strip()
            else:
                team_name = _team_name_for_text(summary, index)
            acceptance_checks = _string_tuple(seed.get("acceptance_checks"))
            depends_on = _string_tuple(seed.get("depends_on")) if self.config.allow_dependency_edges else ()
            budget = dict(seed.get("budget", {})) if isinstance(seed.get("budget"), dict) else {}
            for key in (
                "gap_id",
                "rationale",
                "source_path",
                "source_line",
                "owned_paths",
                "verification_commands",
            ):
                if key in seed:
                    seed_metadata[key] = seed[key]
        else:
            raise ValueError("Dynamic workstream seeds must be strings or objects")

        metadata = dict(seed_metadata)
        metadata["planning_origin"] = "dynamic_superleader"
        if "max_concurrency" in budget:
            raise ValueError("Dynamic workstream seed budget.max_concurrency is no longer supported; use max_teammates only.")
        return WorkstreamTemplate(
            workstream_id=workstream_id,
            title=title,
            summary=summary,
            team_name=team_name,
            depends_on=depends_on,
            acceptance_checks=acceptance_checks,
            budget_max_teammates=_int_value(
                budget.get("max_teammates"),
                self.config.default_budget_max_teammates,
            ),
            budget_max_iterations=_int_value(
                budget.get("max_iterations"),
                self.config.default_budget_max_iterations,
            ),
            budget_max_tokens=budget.get("max_tokens") if isinstance(budget.get("max_tokens"), int) else None,
            budget_max_seconds=budget.get("max_seconds") if isinstance(budget.get("max_seconds"), int) else None,
            metadata=metadata,
        )
