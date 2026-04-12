# Agent Working Rules

## 1. Mandatory Knowledge Read Before Execution

All agents working in this repository must read the knowledge base before doing substantial analysis, implementation, or documentation work.

所有 agent 在执行前都必须先完成这一步。

Required order:

1. Read `resource/knowledge/README.md`.
2. Match the task to the relevant knowledge package under `resource/knowledge/`.
3. Read that package's `README.md`.
4. Read the linked topic documents that directly apply to the task before execution.

If a task spans multiple topics, read every relevant package before proceeding.

If no existing package fits, proceed with the task, but create or extend the knowledge base before claiming completion.

## 2. Knowledge Use During Execution

Agents should treat `docs/resource/knowledge/` as reusable working context rather than optional background notes.

Use the knowledge base to:

- avoid re-deriving architecture or workflow conclusions that are already documented
- reuse existing runbooks, contracts, and failure patterns
- update existing knowledge instead of creating duplicate notes

## 3. Mandatory Knowledge Solidification Before Completion

Before declaring a task complete, every agent must evaluate whether the work produced reusable knowledge.

Solidify knowledge when the task produces any of the following:

- reusable architecture understanding
- durable directory or hotspot maps
- workflow rules, runbooks, or operating checklists
- delivery contracts or completion semantics
- recurring failure patterns or debugging lessons
- stable integration knowledge that future agents should reuse

All knowledge updates must follow:

- `docs/resource/knowledge/knowledge-solidification-standard.md`

## 4. Package Update Rules

When solidifying knowledge:

1. Update an existing package if the new knowledge belongs to that topic.
2. Create a new package only when the topic is materially different from existing packages.
3. If a new package is created, also update `resource/knowledge/README.md`.

## 5. Non-Compliant Completion Is Not Complete

If relevant knowledge was not read before execution, or reusable knowledge was not solidified before completion, the task should be treated as incomplete.
