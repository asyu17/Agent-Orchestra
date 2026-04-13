from __future__ import annotations


def schema_statements(schema: str = "agent_orchestra") -> tuple[str, ...]:
    return (
        f"CREATE SCHEMA IF NOT EXISTS {schema};",
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.groups (
            group_id TEXT PRIMARY KEY,
            display_name TEXT,
            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.teams (
            team_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL REFERENCES {schema}.groups(group_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            member_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.objectives (
            objective_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL REFERENCES {schema}.groups(group_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.spec_nodes (
            node_id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL REFERENCES {schema}.objectives(objective_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            scope TEXT NOT NULL,
            lane_id TEXT,
            team_id TEXT,
            status TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.spec_edges (
            edge_id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL REFERENCES {schema}.objectives(objective_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            from_node_id TEXT NOT NULL,
            to_node_id TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.tasks (
            task_id TEXT PRIMARY KEY,
            group_id TEXT,
            team_id TEXT,
            lane TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.task_review_slots (
            task_id TEXT NOT NULL REFERENCES {schema}.tasks(task_id) ON DELETE CASCADE,
            reviewer_agent_id TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            latest_revision_id TEXT NOT NULL,
            stance TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            PRIMARY KEY (task_id, reviewer_agent_id)
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.task_review_revisions (
            revision_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES {schema}.tasks(task_id) ON DELETE CASCADE,
            reviewer_agent_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            replaces_revision_id TEXT,
            stance TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.review_items (
            item_id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL,
            item_kind TEXT NOT NULL,
            lane_id TEXT,
            team_id TEXT,
            source_task_id TEXT,
            title TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.team_position_reviews (
            position_review_id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            item_kind TEXT NOT NULL,
            team_id TEXT NOT NULL,
            leader_id TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.cross_team_leader_reviews (
            cross_review_id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            item_kind TEXT NOT NULL,
            reviewer_team_id TEXT NOT NULL,
            target_team_id TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.superleader_syntheses (
            item_id TEXT PRIMARY KEY,
            synthesis_id TEXT NOT NULL,
            item_kind TEXT NOT NULL,
            superleader_id TEXT NOT NULL,
            synthesized_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.leader_draft_plans (
            plan_id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL,
            planning_round_id TEXT NOT NULL,
            leader_id TEXT NOT NULL,
            lane_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.leader_peer_reviews (
            review_id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL,
            planning_round_id TEXT NOT NULL,
            reviewer_leader_id TEXT NOT NULL,
            reviewer_team_id TEXT NOT NULL,
            target_leader_id TEXT NOT NULL,
            target_team_id TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.superleader_global_reviews (
            objective_id TEXT NOT NULL,
            planning_round_id TEXT NOT NULL,
            review_id TEXT NOT NULL,
            superleader_id TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            PRIMARY KEY (objective_id, planning_round_id)
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.leader_revised_plans (
            plan_id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL,
            planning_round_id TEXT NOT NULL,
            leader_id TEXT NOT NULL,
            lane_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.activation_gate_decisions (
            objective_id TEXT NOT NULL,
            planning_round_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            status TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            PRIMARY KEY (objective_id, planning_round_id)
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.handoffs (
            handoff_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            from_team_id TEXT NOT NULL,
            to_team_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.blackboard_entries (
            entry_id TEXT PRIMARY KEY,
            blackboard_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            entry_kind TEXT NOT NULL,
            lane_id TEXT,
            team_id TEXT,
            task_id TEXT,
            created_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.blackboard_snapshots (
            blackboard_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            lane_id TEXT,
            team_id TEXT,
            version BIGINT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.worker_records (
            worker_id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            backend TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.agent_sessions (
            session_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL,
            phase TEXT NOT NULL,
            objective_id TEXT,
            lane_id TEXT,
            team_id TEXT,
            last_progress_at TEXT,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.worker_sessions (
            session_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            assignment_id TEXT,
            backend TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            lifecycle_status TEXT,
            started_at TEXT,
            last_active_at TEXT,
            idle_since TEXT,
            last_response_id TEXT,
            supervisor_id TEXT,
            supervisor_lease_id TEXT,
            supervisor_lease_expires_at TEXT,
            reactivation_count BIGINT NOT NULL DEFAULT 0,
            reattach_count BIGINT NOT NULL DEFAULT 0,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.work_sessions (
            work_session_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            root_objective_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            current_runtime_generation_id TEXT,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.runtime_generations (
            runtime_generation_id TEXT PRIMARY KEY,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            generation_index BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            continuity_mode TEXT NOT NULL,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            source_runtime_generation_id TEXT,
            group_id TEXT NOT NULL,
            objective_id TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.work_session_messages (
            message_id TEXT PRIMARY KEY,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            runtime_generation_id TEXT,
            created_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.conversation_heads (
            conversation_head_id TEXT PRIMARY KEY,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            runtime_generation_id TEXT NOT NULL,
            head_kind TEXT NOT NULL,
            scope_id TEXT,
            updated_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.session_events (
            session_event_id TEXT PRIMARY KEY,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            runtime_generation_id TEXT,
            event_kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.agent_turn_records (
            turn_record_id TEXT PRIMARY KEY,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            runtime_generation_id TEXT NOT NULL,
            head_kind TEXT NOT NULL,
            scope_id TEXT,
            assignment_id TEXT,
            created_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.tool_invocation_records (
            tool_invocation_id TEXT PRIMARY KEY,
            turn_record_id TEXT REFERENCES {schema}.agent_turn_records(turn_record_id) ON DELETE CASCADE,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            runtime_generation_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.artifact_refs (
            artifact_ref_id TEXT PRIMARY KEY,
            turn_record_id TEXT REFERENCES {schema}.agent_turn_records(turn_record_id) ON DELETE SET NULL,
            tool_invocation_id TEXT REFERENCES {schema}.tool_invocation_records(tool_invocation_id) ON DELETE SET NULL,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            runtime_generation_id TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            storage_kind TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.session_memory_items (
            memory_item_id TEXT PRIMARY KEY,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            runtime_generation_id TEXT NOT NULL,
            head_kind TEXT NOT NULL,
            scope_id TEXT,
            memory_kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            archived_at TEXT,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.resident_team_shells (
            resident_team_shell_id TEXT PRIMARY KEY,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            group_id TEXT NOT NULL,
            objective_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            lane_id TEXT NOT NULL,
            runtime_generation_id TEXT NOT NULL,
            status TEXT NOT NULL,
            leader_slot_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_progress_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.agent_slots (
            slot_id TEXT PRIMARY KEY,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            resident_team_shell_id TEXT,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            desired_state TEXT NOT NULL,
            preferred_backend TEXT,
            preferred_transport_class TEXT,
            current_incarnation_id TEXT,
            current_lease_id TEXT,
            restart_count BIGINT NOT NULL DEFAULT 0,
            last_failure_class TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.agent_incarnations (
            incarnation_id TEXT PRIMARY KEY,
            slot_id TEXT NOT NULL REFERENCES {schema}.agent_slots(slot_id) ON DELETE CASCADE,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            runtime_generation_id TEXT,
            status TEXT NOT NULL,
            backend TEXT NOT NULL,
            lease_id TEXT NOT NULL,
            restart_generation BIGINT NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            terminal_failure_class TEXT,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.slot_health_events (
            event_id TEXT PRIMARY KEY,
            slot_id TEXT NOT NULL REFERENCES {schema}.agent_slots(slot_id) ON DELETE CASCADE,
            incarnation_id TEXT,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            event_kind TEXT NOT NULL,
            failure_class TEXT,
            observed_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.session_attachments (
            attachment_id TEXT PRIMARY KEY,
            work_session_id TEXT NOT NULL REFERENCES {schema}.work_sessions(work_session_id) ON DELETE CASCADE,
            resident_team_shell_id TEXT,
            slot_id TEXT,
            incarnation_id TEXT,
            client_id TEXT NOT NULL,
            status TEXT NOT NULL,
            attached_at TEXT NOT NULL,
            detached_at TEXT,
            last_event_id TEXT,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.provider_route_health (
            route_key TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            backend TEXT NOT NULL,
            route_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            health_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            consecutive_failures BIGINT NOT NULL DEFAULT 0,
            last_failure_class TEXT,
            cooldown_expires_at TEXT,
            preferred BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.protocol_bus_cursors (
            stream TEXT NOT NULL,
            consumer TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            PRIMARY KEY (stream, consumer)
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.delivery_states (
            delivery_id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL REFERENCES {schema}.objectives(objective_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            lane_id TEXT,
            team_id TEXT,
            iteration BIGINT NOT NULL DEFAULT 0,
            mailbox_cursor TEXT,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.authority_states (
            group_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.coordination_outbox (
            outbox_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            recipient TEXT NOT NULL,
            sender TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        """.strip(),
    )
