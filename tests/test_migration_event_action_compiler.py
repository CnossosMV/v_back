import sqlalchemy as sa

from alembic_event_action_compiler import compile_event_action_for_migration


def test_migration_compiler_uses_only_reflected_columns_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    event_actions = sa.Table(
        "event_actions", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.Integer, nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("trigger_event", sa.String, nullable=False),
        sa.Column("conditions", sa.JSON),
        sa.Column("actions", sa.JSON, nullable=False),
        sa.Column("stop_conditions", sa.JSON),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("cooldown_seconds", sa.Integer, nullable=False),
        sa.Column("react_to_delivery", sa.Boolean, nullable=False),
    )
    funnels = sa.Table(
        "funnels", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.Integer, nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("trigger_type", sa.String, nullable=False),
        sa.Column("trigger_config", sa.JSON),
        sa.Column("global_exit_config", sa.JSON),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("is_system", sa.Boolean, nullable=False),
        sa.Column("event_action_id", sa.Integer, nullable=False, unique=True),
        sa.Column("cooldown_seconds", sa.Integer),
        sa.Column("react_to_delivery", sa.Boolean, nullable=False),
    )
    funnel_steps = sa.Table(
        "funnel_steps", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("funnel_id", sa.Integer, nullable=False),
        sa.Column("step_type", sa.String, nullable=False),
        sa.Column("step_config", sa.JSON),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("branch", sa.String, nullable=False),
        sa.Column("slot_id", sa.String(36)),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        action_id = conn.execute(event_actions.insert().values(
            project_id=1,
            name="Historical action",
            description=None,
            trigger_event="historical.event",
            conditions=[],
            actions=[{
                "type": "send_template",
                "config": {"template_id": 7},
                "delay_seconds": 60,
            }],
            stop_conditions=[],
            is_active=True,
            priority=5,
            cooldown_seconds=3600,
            react_to_delivery=False,
        ).returning(event_actions.c.id)).scalar_one()

        first_funnel = compile_event_action_for_migration(conn, action_id)
        second_funnel = compile_event_action_for_migration(conn, action_id)

        assert first_funnel == second_funnel
        assert conn.execute(sa.select(sa.func.count()).select_from(funnels)).scalar_one() == 1
        steps = conn.execute(
            sa.select(funnel_steps).order_by(funnel_steps.c.position)
        ).mappings().all()
        assert [step["step_type"] for step in steps] == ["wait", "action"]
        assert all(len(step["slot_id"]) == 36 for step in steps)
        stored_actions = conn.execute(
            sa.select(event_actions.c.actions).where(event_actions.c.id == action_id)
        ).scalar_one()
        assert len(stored_actions[0]["slot_id"]) == 36
