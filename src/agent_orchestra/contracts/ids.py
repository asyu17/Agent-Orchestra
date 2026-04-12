from __future__ import annotations

from uuid import uuid4


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def make_group_id() -> str:
    return make_id("group")


def make_team_id() -> str:
    return make_id("team")


def make_agent_id() -> str:
    return make_id("agent")


def make_task_id() -> str:
    return make_id("task")


def make_handoff_id() -> str:
    return make_id("handoff")


def make_event_id() -> str:
    return make_id("evt")


def make_objective_id() -> str:
    return make_id("obj")


def make_spec_node_id() -> str:
    return make_id("node")


def make_edge_id() -> str:
    return make_id("edge")


def make_blackboard_entry_id() -> str:
    return make_id("bbentry")


def make_work_session_id() -> str:
    return make_id("worksession")


def make_runtime_generation_id() -> str:
    return make_id("runtimegen")


def make_work_session_message_id() -> str:
    return make_id("wsmsg")


def make_conversation_head_id() -> str:
    return make_id("convhead")


def make_session_event_id() -> str:
    return make_id("sevt")


def make_resident_team_shell_id() -> str:
    return make_id("residentteamshell")


def make_agent_turn_record_id() -> str:
    return make_id("turnrec")


def make_tool_invocation_id() -> str:
    return make_id("toolinv")


def make_artifact_ref_id() -> str:
    return make_id("artifactref")


def make_memory_item_id() -> str:
    return make_id("memitem")
