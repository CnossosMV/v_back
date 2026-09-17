"""Customer-safe Versya MCP server."""
from app.mcp import tools
from app.mcp.transport import transport_security_settings


def create_product_server():
    from mcp.server.fastmcp import FastMCP

    security = transport_security_settings()
    security_kwargs = {"transport_security": security} if security else {}
    server = FastMCP(
        name="Versya",
        instructions=(
            "Operate project by project and always pass an explicit project_id after resolving it with list_projects. "
            "For a new or incompletely configured project, call get_project_onboarding_contract before proposing "
            "writes, then call get_agent_skill_catalog and assess_project_setup. Persist the reviewed proposal as "
            "a versioned Project Setup Plan, record every material choice with its evidence label, and approve phases "
            "only at their current fingerprint. A phase approval records business intent but never executes modules "
            "or authorizes sends. Configure locale/timezone/goal semantics with configure_project_foundation. "
            "For markets call get_project_market_contract and use paginated first-class market tools; market is a "
            "business/calendar boundary while locale is a content axis, and page size is never a project limit. "
            "Interview the tenant in business language, label evidence as observed, inferred, recommended "
            "or tenant_decided, inventory only with granted read tools, and propose the smallest end-to-end value "
            "slice. Never present a recommendation as a tenant decision or ask a novice tenant to author tool JSON. "
            "For automation work, run list_event_actions/list_funnels, then run inspect_automation_conflicts "
            "and lint_funnel_configuration before proposing a change. Writes are dry-run by default and require "
            "an explicit confirmation token; never activate an automation while a blocking conflict remains. "
            "For source-system migration, inspect get_project_import_contract first, keep an existing project's "
            "history in place, create or resume by stable client_import_id, then PUT the exact ZIP bytes to the "
            "authenticated raw_url returned by create_project_import. Validate, dry-run, request confirmation, "
            "enqueue, poll get_project_import, and require audit_project_import.accepted=true before comparing "
            "or activating a lifecycle model. Never use SSH, SQL, the UI, or source credentials as a migration "
            "shortcut. Call get_lifecycle_model_contract before authoring tenant taxonomy; validate and "
            "dry-run a new immutable model, inspect its distribution and per-contact explanations, then "
            "put it in shadow. A purpose_key names stable business intent, never a channel or template. "
            "Keep locale and channel variants on that business key; multiple Event Actions may be active only "
            "when their event predicates are provably disjoint. Never suffix purpose keys to bypass overlap. "
            "Inventory purpose ownership and readiness before creating draft campaigns or transferring "
            "orchestration one purpose at a time with the exact expected epoch. "
            "Every funnel, campaign, decision-producing Event Action, or event-triggered template must declare "
            "a purpose_key and attention_policy. Template content availability is separate from automation_enabled; "
            "creating, importing, or editing a template never enables its event automation. "
            "Before activation call preview_orchestration_impact, resolve every blocker, explain the entry "
            "effects and materialized contacts to the tenant, then use the confirmation token bound to the "
            "returned impact_fingerprint. Priority selects who has attention; entry_effect separately decides "
            "whether lower episodes remain occluded, coexist, exit, or reject the new entry. "
            "Before enabling future_reservation, call get_attention_planning_contract, let the tenant choose "
            "the bounded horizon/collision policy in business terms, dry-run configure_attention_planning, "
            "and show its consequence_at_gate. Use preview_contact_attention_plan for representative contacts. "
            "A plan is never send authorization: the contest is recomputed at the dispatch deadline and Guardian "
            "still applies every permission and pressure gate. "
            "For recurring nurture, call get_campaign_recipe_contract before creating assets. A recipe is a "
            "persistent planning policy, an episode is one dated opportunity, and neither is send authorization. "
            "Preview the recipe, bind every lifecycle audience to an explicit lifecycle_model_id, declare market "
            "timezone/country and opportunity priorities, then create it as draft and activate recipe planning "
            "with separate confirmations. Read get_campaign_planning_inbox for due work. Write agent-authored copy "
            "only from the episode content brief and source facts; never invent a winner for an unresolved collision. "
            "materialize_campaign_episode creates an inactive draft only. Then preview the campaign plan and "
            "orchestration impact, activate the campaign, evaluate_campaign_run_consequences, present or apply the "
            "tenant's declared choice, and only then call schedule_campaign_run. Scheduling a run authorizes future "
            "external sends; Selection and Guardian still recompute at dispatch. Monitor with get_campaign_run_status "
            "and use cancel_campaign_run when needed; cancellation cannot recall provider calls already submitted. "
            "If work is missed, let the episode "
            "expire and keep Base eligible instead of sending stale copy. "
            "Never invent a source_type or bypass SendService: every outbound source must be registered in the "
            "platform source contract, and promotional automation requires Candidate and Selection in enforce. "
            "Before relying on imported lifecycle facts for new entrants, call get_event_ingestion_contract, "
            "implement the signed public profile/event path, and use get_event_ingestion_status plus contact "
            "timeline/lifecycle tools to prove convergence without SSH or SQL. "
            "Project tools accept an explicit project_id or use the connector default project."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        **security_kwargs,
    )

    server.tool()(tools.get_mcp_status)
    server.tool()(tools.list_projects)
    server.tool()(tools.get_project_overview)
    server.tool()(tools.get_project_onboarding_contract)
    server.tool()(tools.get_agent_skill_catalog)
    server.tool()(tools.assess_project_setup)
    server.tool()(tools.create_project_setup_plan)
    server.tool()(tools.list_project_setup_plans)
    server.tool()(tools.get_project_setup_plan)
    server.tool()(tools.record_project_setup_decision)
    server.tool()(tools.approve_project_setup_phase)
    server.tool()(tools.compare_project_setup_plans)
    server.tool()(tools.set_project_setup_plan_status)
    server.tool()(tools.configure_project_foundation)
    server.tool()(tools.get_project_market_contract)
    server.tool()(tools.list_project_markets)
    server.tool()(tools.create_project_market)
    server.tool()(tools.update_project_market)
    server.tool()(tools.set_default_project_market)
    server.tool()(tools.set_project_market_status)
    server.tool()(tools.get_event_ingestion_contract)
    server.tool()(tools.get_event_ingestion_status)
    server.tool()(tools.create_event_ingestion_key)
    server.tool()(tools.get_project_import_contract)
    server.tool()(tools.create_project_import)
    server.tool()(tools.list_project_imports)
    server.tool()(tools.get_project_import)
    server.tool()(tools.validate_project_import)
    server.tool()(tools.apply_project_import)
    server.tool()(tools.audit_project_import)
    server.tool()(tools.list_project_import_records)
    server.tool()(tools.list_lifecycle_models)
    server.tool()(tools.get_lifecycle_model_contract)
    server.tool()(tools.validate_lifecycle_model_definition)
    server.tool()(tools.create_lifecycle_model)
    server.tool()(tools.get_lifecycle_distribution)
    server.tool()(tools.compare_lifecycle_materialization)
    server.tool()(tools.explain_contact_lifecycle)
    server.tool()(tools.compare_lifecycle_models)
    server.tool()(tools.activate_lifecycle_model)
    server.tool()(tools.list_orchestration_cutovers)
    server.tool()(tools.get_orchestration_contract)
    server.tool()(tools.get_attention_planning_contract)
    server.tool()(tools.preview_contact_attention_plan)
    server.tool()(tools.configure_attention_planning)
    server.tool()(tools.list_orchestration_purposes)
    server.tool()(tools.assess_orchestration_readiness)
    server.tool()(tools.set_orchestration_cutover)
    server.tool()(tools.get_commercial_calendar_contract)
    server.tool()(tools.preview_orchestration_impact)
    server.tool()(tools.list_commercial_opportunities)
    server.tool()(tools.upsert_commercial_opportunity)
    server.tool()(tools.preview_commercial_opportunities)
    server.tool()(tools.get_campaign_recipe_contract)
    server.tool()(tools.preview_campaign_recipe)
    server.tool()(tools.list_campaign_recipes)
    server.tool()(tools.get_campaign_recipe)
    server.tool()(tools.create_draft_campaign_recipe)
    server.tool()(tools.update_campaign_recipe)
    server.tool()(tools.set_campaign_recipe_state)
    server.tool()(tools.get_campaign_planning_inbox)
    server.tool()(tools.materialize_campaign_episode)
    server.tool()(tools.list_campaigns)
    server.tool()(tools.get_campaign)
    server.tool()(tools.create_draft_campaign)
    server.tool()(tools.update_draft_campaign)
    server.tool()(tools.preview_campaign_plan)
    server.tool()(tools.activate_campaign)
    server.tool()(tools.evaluate_campaign_run_consequences)
    server.tool()(tools.choose_campaign_run_consequence)
    server.tool()(tools.schedule_campaign_run)
    server.tool()(tools.get_campaign_run_status)
    server.tool()(tools.cancel_campaign_run)
    server.tool()(tools.list_templates)
    server.tool()(tools.create_template)
    server.tool()(tools.update_template)
    server.tool()(tools.preview_template)
    server.tool()(tools.activate_template_automation)
    server.tool()(tools.list_project_variables)
    server.tool()(tools.create_project_variable)
    server.tool()(tools.update_project_variable)
    server.tool()(tools.list_event_actions)
    server.tool()(tools.inspect_automation_conflicts)
    server.tool()(tools.lint_funnel_configuration)
    server.tool()(tools.create_event_action)
    server.tool()(tools.update_event_action)
    server.tool()(tools.set_event_action_state)
    server.tool()(tools.list_funnels)
    server.tool()(tools.export_funnel)
    server.tool()(tools.import_funnel)
    server.tool()(tools.create_draft_funnel)
    server.tool()(tools.update_funnel_steps)
    server.tool()(tools.activate_funnel)
    server.tool()(tools.search_contacts)
    server.tool()(tools.get_contact_timeline)
    server.tool()(tools.list_chatbots)
    server.tool()(tools.list_agent_teams)
    server.tool()(tools.list_channel_health)

    return server
