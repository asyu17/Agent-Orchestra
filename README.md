# Agent-Orchestra

[简体中文](README.zh-CN.md)

<p align="center">
  <img src="assets/agent-orchestra-mascot.png" alt="Agent-Orchestra mascot" width="180" />
</p>

> A usable alpha runtime for multi-team agent orchestration in Python.

Agent-Orchestra is a Python runtime and control plane for coordinating multiple teams of agents under a shared objective. It focuses on session continuity, attach/wake/fork workflows, and backend-flexible execution so multi-team handoffs stay structured instead of ad hoc.

## Quick Capabilities

- Package / CLI entrypoint: `agent-orchestra` with `group`, `team`, `session`, `schema`, and `self-host`.
- Multi-team orchestration runtime with group, team, and session primitives.
- Session continuity control plane: `new/list/inspect/attach/wake/fork`.
- Store backends: `in-memory` for fast local trials and `postgres` for durable continuity.
- Execution backends registered in the runtime builder: `in_process`, `subprocess`, `tmux`, `codex_cli`.
- Self-hosting bootstrap helpers for inventory and seed templates.

## Quickstart

### Install (dev mode)

```bash
python -m pip install -e .
```

Package and CLI entrypoint:

```bash
agent-orchestra --help
```

### CLI preview

```bash
agent-orchestra {group,team,session,schema,self-host} --help
```

If you are running from the repo, this is the most reliable entrypoint:

```bash
export PYTHONPATH=src
python -m agent_orchestra.cli.main --help
```

### Minimal session flow

```bash
export PYTHONPATH=src

python -m agent_orchestra.cli.main session new \
  --store-backend in-memory \
  --group-id demo-group \
  --objective-id demo-objective \
  --title "AO demo" \
  --output pretty

python -m agent_orchestra.cli.main session list \
  --store-backend in-memory \
  --group-id demo-group \
  --objective-id demo-objective \
  --output pretty

python -m agent_orchestra.cli.main session inspect \
  --store-backend in-memory \
  --work-session-id <work_session_id> \
  --output pretty
```

If you want durable continuity instead of an in-memory local trial, switch to `--store-backend postgres`, initialize the schema with `agent-orchestra schema`, and provide `AGENT_ORCHESTRA_DSN` or `--dsn`.

Session operations available today:

```bash
agent-orchestra session {list,inspect,new,attach,wake,fork} --help
```

Self-hosting helpers:

```bash
agent-orchestra self-host {inventory,seed-template} --help
```

## Why This Is Architecturally Interesting

If you are building agent infrastructure, Agent-Orchestra is useful because it tries to move team coordination out of “prompt agreements” and into explicit runtime surfaces:

- Session continuity is a first-class control plane, not a side effect of logs.
- Execution transport is separated from orchestration logic via backend registration.
- Store-backed continuity makes multi-person or multi-process handoff practical.
- Self-hosting bootstrap turns “what to build next” into structured templates.

## Architecture At A Glance

- **CLI**: `agent_orchestra.cli` exposes `group`, `team`, `session`, `schema`, and `self-host`.
- **Runtime**: `agent_orchestra.runtime` implements orchestration semantics and uses a backend registry for execution transport.
- **Storage/Bus**: `agent_orchestra.storage` and `agent_orchestra.bus` provide persistence and coordination channels. The CLI supports `in-memory` and `postgres`.

Registered execution backends:

- `in_process`
- `subprocess`
- `tmux`
- `codex_cli`

## Current Status (Usable Alpha)

Working today:

- CLI control plane with `group`, `team`, `session`, `schema`, `self-host`.
- Session operations: `list`, `inspect`, `new`, `attach`, `wake`, `fork`.
- Store backends visible in the CLI: `in-memory` and `postgres`.
- Runtime backends registered in the builder: `in_process`, `subprocess`, `tmux`, `codex_cli`.

Still alpha:

- `attach`/`wake` return structured control-plane results; they are not a full interactive shell reattach.
- Durability and reconnect hardening are evolving; treat this as a usable alpha rather than production-ready.

## Repository Map

```text
Agent-Orchestra/
  src/agent_orchestra/         # Core Python package (runtime/cli/storage/self_hosting)
  docs/resource/knowledge/     # Architecture, runbooks, contracts, failure patterns
  tests/                       # Unit/integration tests (coverage varies)
  pyproject.toml               # Package name and CLI entrypoint
```

## Docs And Knowledge Base

Start here for the authoritative architecture and runtime notes:

- [docs/resource/knowledge/README.md](docs/resource/knowledge/README.md)
- [docs/resource/knowledge/agent-orchestra-runtime/README.md](docs/resource/knowledge/agent-orchestra-runtime/README.md)
- [docs/resource/knowledge/agent-orchestra-runtime/architecture.md](docs/resource/knowledge/agent-orchestra-runtime/architecture.md)
- [docs/resource/knowledge/agent-orchestra-runtime/agent-orchestra-cli-operations-and-agent-handoff.md](docs/resource/knowledge/agent-orchestra-runtime/agent-orchestra-cli-operations-and-agent-handoff.md)

## Inspired By

This project is inspired by Claude Code-style team runtime ideas, especially the direction of “team + session continuity + host-owned coordination.” It is not affiliated with, endorsed by, or a clone of any external project.

## Chinese README

[README.zh-CN.md](README.zh-CN.md)
