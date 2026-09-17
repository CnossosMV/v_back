"""
Journey Graph Service — assembles graph payload from materialized tables,
resolves goal events, computes statistical derivations at query time.
"""
import math
import logging
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict

from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models import Project
from app.models.messaging import MessagingEvent, MessagingEventSchema
from app.models.journey import (
    JourneyNode, JourneyEdge, JourneyIntervention, JourneySnapshot,
)
from app.schemas.journey import (
    JourneyGraphResponse, JourneyNodeDTO, JourneyEdgeDTO,
    JourneyInterventionDTO, TerminalNodeDTO, GraphMetadataDTO,
    ThermalBreakdownDTO, UserJourneyResponse, UserPathStepDTO,
    UserInterventionDTO, NodeDetailResponse, EdgeSummaryDTO,
    StepWarning, DataHealthRecommendation, DataHealthResponse,
)

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 90
BOTTLENECK_THRESHOLD = 2.0  # score above this = bottleneck
CONVERSION_PATH_THRESHOLD = 0.3  # beta_mean above this = conversion path


def resolve_goal_event(db: Session, project_id: int, override: Optional[str] = None) -> Optional[str]:
    """Resolve the goal event: explicit override > project setting > auto-detect."""
    if override:
        return override

    project = db.query(Project).get(project_id)
    if project and project.goal_event:
        return project.goal_event

    # Auto-detect: exactly one conversion-category schema
    conversion_schemas = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.project_id == project_id,
        MessagingEventSchema.category == 'conversion',
        MessagingEventSchema.is_active == True,
    ).all()

    if len(conversion_schemas) == 1:
        return conversion_schemas[0].event_name

    return None


class JourneyGraphService:
    def __init__(self, db: Session):
        self.db = db

    def get_aggregate_graph(
        self,
        project_id: int,
        date_from: date,
        date_to: date,
        conversion_event: Optional[str] = None,
        show_interventions: bool = False,
        min_volume: int = 1,
        max_nodes: int = 30,
        visitor_type: str = "all",
    ) -> JourneyGraphResponse:
        goal = resolve_goal_event(self.db, project_id, conversion_event)

        # Counter selectors per visitor_type. anonymous_id and user_id are
        # disjoint, so summing identified + anonymous yields the all-visitor
        # totals without double-counting.
        def node_freq(n: JourneyNode) -> int:
            if visitor_type == "identified":
                return n.frequency
            if visitor_type == "anonymous":
                return n.frequency_anon
            return n.frequency + n.frequency_anon

        def node_uniq(n: JourneyNode) -> int:
            if visitor_type == "identified":
                return n.unique_users
            if visitor_type == "anonymous":
                return n.unique_anon_visitors
            return n.unique_users + n.unique_anon_visitors

        def edge_volume(e: JourneyEdge) -> int:
            if visitor_type == "identified":
                return e.total_transitions
            if visitor_type == "anonymous":
                return e.frequency_anon
            return e.total_transitions + e.frequency_anon

        def edge_uniq(e: JourneyEdge) -> int:
            if visitor_type == "identified":
                return e.unique_users
            if visitor_type == "anonymous":
                return e.unique_anon_visitors
            return e.unique_users + e.unique_anon_visitors

        # Fetch nodes — order/dedupe in Python so we can use the visitor_type
        # selector. max_nodes is small (≤200), so this is cheap.
        raw_nodes = self.db.query(JourneyNode).filter(
            JourneyNode.project_id == project_id,
            JourneyNode.period_start <= date_to,
            JourneyNode.period_end >= date_from,
        ).all()

        # Deduplicate: keep highest-frequency row per event_name (using selector)
        seen_events: dict = {}
        for n in raw_nodes:
            if n.event_name not in seen_events or node_freq(n) > node_freq(seen_events[n.event_name]):
                seen_events[n.event_name] = n
        # Drop nodes with zero traffic for the chosen visitor_type
        candidates = [n for n in seen_events.values() if node_freq(n) > 0]
        candidates.sort(key=node_freq, reverse=True)
        nodes_q = candidates[:max_nodes]

        node_names = {n.event_name for n in nodes_q}

        # Schema lookup for display names
        schemas = self.db.query(MessagingEventSchema).filter(
            MessagingEventSchema.project_id == project_id,
            MessagingEventSchema.event_name.in_(node_names),
        ).all()
        schema_map = {s.event_name: s for s in schemas}

        # Fetch edges filtered to visible nodes; min_volume applied in Python
        # because the effective volume depends on visitor_type.
        raw_edges = self.db.query(JourneyEdge).filter(
            JourneyEdge.project_id == project_id,
            JourneyEdge.period_start <= date_to,
            JourneyEdge.period_end >= date_from,
            JourneyEdge.source_event.in_(node_names),
            JourneyEdge.target_event.in_(node_names),
        ).all()

        # Deduplicate: keep highest-volume row per (source, target) pair
        seen_edges: dict = {}
        for e in raw_edges:
            key = (e.source_event, e.target_event)
            if key not in seen_edges or edge_volume(e) > edge_volume(seen_edges[key]):
                seen_edges[key] = e
        edges_q = [e for e in seen_edges.values() if edge_volume(e) >= min_volume]

        # Build node DTOs — frequency/unique_users reflect the chosen visitor_type
        node_dtos = []
        for n in nodes_q:
            schema = schema_map.get(n.event_name)
            bottleneck_score = self._bottleneck_score(n)
            node_dtos.append(JourneyNodeDTO(
                event_name=n.event_name,
                display_name=schema.display_name if schema else n.event_name.replace('_', ' ').title(),
                category=schema.category if schema else 'custom',
                frequency=node_freq(n),
                unique_users=node_uniq(n),
                current_occupancy=n.current_occupancy,
                thermal=ThermalBreakdownDTO(
                    hot=n.hot_count, warm=n.warm_count,
                    cold=n.cold_count, dead=n.dead_count,
                ),
                throughput_rate=n.throughput_rate,
                avg_dwell_seconds=n.avg_dwell_seconds,
                conversion_rate=n.conversion_rate if goal else None,
                is_bottleneck=bottleneck_score > BOTTLENECK_THRESHOLD,
            ))

        # Build edge DTOs — volume reflects the chosen visitor_type
        source_freq = {n.event_name: node_freq(n) for n in nodes_q}

        edge_dtos = []
        for e in edges_q:
            vol = edge_volume(e)
            src_f = source_freq.get(e.source_event, vol)
            pct = vol / src_f if src_f > 0 else 0.0

            beta_mean, ci_lower, ci_upper = None, None, None
            is_conv = False
            if goal and e.beta_alpha > 1.0:
                beta_mean, ci_lower, ci_upper = compute_beta_posterior(e.beta_alpha, e.beta_beta)
                is_conv = (beta_mean or 0) > CONVERSION_PATH_THRESHOLD

            edge_dtos.append(JourneyEdgeDTO(
                source=e.source_event,
                target=e.target_event,
                volume=vol,
                pct_from_source=round(pct, 4),
                median_seconds=e.median_seconds,
                p90_seconds=e.p90_seconds,
                converted_volume=e.converted_transitions,
                beta_alpha=e.beta_alpha,
                beta_beta=e.beta_beta,
                beta_mean=beta_mean,
                beta_ci_lower=ci_lower,
                beta_ci_upper=ci_upper,
                is_conversion_path=is_conv,
                drop_off_rate=e.drop_off_rate,
            ))

        # Interventions
        intervention_dtos = None
        if show_interventions:
            interventions_q = self.db.query(JourneyIntervention).filter(
                JourneyIntervention.project_id == project_id,
                JourneyIntervention.period_start <= date_to,
                JourneyIntervention.period_end >= date_from,
            ).all()
            intervention_dtos = [
                JourneyInterventionDTO(
                    pre_event=i.pre_event,
                    post_event=i.post_event,
                    channel=i.channel,
                    source_type=i.source_type,
                    arm_id=i.arm_id,
                    total_sent=i.total_sent,
                    total_responded=i.total_responded,
                    response_rate=i.total_responded / i.total_sent if i.total_sent > 0 else 0.0,
                    beta_alpha=i.beta_alpha,
                    beta_beta=i.beta_beta,
                ) for i in interventions_q
            ]

        # Terminal nodes
        terminal_nodes = self._compute_terminal_nodes(project_id)

        # Metadata — reflect the chosen visitor_type
        total_users = sum(node_uniq(n) for n in nodes_q)
        total_events = sum(node_freq(n) for n in nodes_q)
        last_mat = max((n.created_at for n in nodes_q), default=None)

        metadata = GraphMetadataDTO(
            total_users=total_users,
            total_events=total_events,
            conversion_event=goal,
            has_goal_configured=goal is not None,
            date_range={"from": date_from.isoformat(), "to": date_to.isoformat()},
            last_materialized_at=last_mat.isoformat() if last_mat else None,
        )

        return JourneyGraphResponse(
            nodes=node_dtos,
            edges=edge_dtos,
            interventions=intervention_dtos,
            terminal_nodes=terminal_nodes,
            metadata=metadata,
        )

    def get_user_journey(
        self,
        project_id: int,
        user_id: int,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> UserJourneyResponse:
        if not date_from:
            date_from = (datetime.utcnow() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date()
        if not date_to:
            date_to = datetime.utcnow().date()

        # Get aggregate graph
        agg = self.get_aggregate_graph(project_id, date_from, date_to, show_interventions=True)

        # Get user's events
        events = self.db.query(MessagingEvent).filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.user_id == user_id,
            MessagingEvent.created_at >= datetime.combine(date_from, datetime.min.time()),
            MessagingEvent.created_at <= datetime.combine(date_to, datetime.max.time()),
        ).order_by(MessagingEvent.created_at.asc()).all()

        # Build user path with properties and dwell time
        user_path = []
        for i, e in enumerate(events):
            dwell = None
            if i < len(events) - 1:
                gap = (events[i + 1].created_at - e.created_at).total_seconds()
                dwell = round(gap, 1)

            user_path.append(UserPathStepDTO(
                event_name=e.event_name,
                timestamp=e.created_at.isoformat(),
                session_id=e.session_id,
                is_intervention=e.source in ('send_service', 'delivery_tracker'),
                properties=e.properties,
                source=e.source,
                dwell_seconds=dwell,
            ))

        # Get snapshot
        snapshot = self.db.query(JourneySnapshot).filter(
            JourneySnapshot.project_id == project_id,
            JourneySnapshot.user_id == user_id,
        ).first()

        # User interventions
        user_interventions = [
            UserInterventionDTO(
                arm_id=(e.properties or {}).get('arm_id', ''),
                channel=(e.properties or {}).get('channel', ''),
                event_name=e.event_name,
                timestamp=e.created_at.isoformat(),
                responded=False,
            )
            for e in events if e.source in ('send_service', 'delivery_tracker')
        ]

        # Detect flow issues
        _detect_warnings(user_path)

        return UserJourneyResponse(
            nodes=agg.nodes,
            edges=agg.edges,
            interventions=agg.interventions,
            terminal_nodes=agg.terminal_nodes,
            metadata=agg.metadata,
            user_path=user_path,
            current_position=snapshot.current_event if snapshot else None,
            thermal_state=snapshot.thermal_state if snapshot else None,
            terminal_state=snapshot.terminal_state if snapshot else None,
            interventions_received=user_interventions,
        )

    def get_anonymous_journey(
        self,
        project_id: int,
        anonymous_id: str,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> UserJourneyResponse:
        """Same shape as get_user_journey, but for an unidentified visitor.

        JourneySnapshot/interventions/thermal_state are tied to user_id and
        don't apply pre-identification — they come back as null/empty.
        """
        if not date_from:
            date_from = (datetime.utcnow() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date()
        if not date_to:
            date_to = datetime.utcnow().date()

        agg = self.get_aggregate_graph(project_id, date_from, date_to, show_interventions=True)

        events = self.db.query(MessagingEvent).filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.anonymous_id == anonymous_id,
            MessagingEvent.created_at >= datetime.combine(date_from, datetime.min.time()),
            MessagingEvent.created_at <= datetime.combine(date_to, datetime.max.time()),
        ).order_by(MessagingEvent.created_at.asc()).all()

        user_path = []
        for i, e in enumerate(events):
            dwell = None
            if i < len(events) - 1:
                gap = (events[i + 1].created_at - e.created_at).total_seconds()
                dwell = round(gap, 1)

            user_path.append(UserPathStepDTO(
                event_name=e.event_name,
                timestamp=e.created_at.isoformat(),
                session_id=e.session_id,
                is_intervention=e.source in ('send_service', 'delivery_tracker'),
                properties=e.properties,
                source=e.source,
                dwell_seconds=dwell,
            ))

        _detect_warnings(user_path)

        return UserJourneyResponse(
            nodes=agg.nodes,
            edges=agg.edges,
            interventions=agg.interventions,
            terminal_nodes=agg.terminal_nodes,
            metadata=agg.metadata,
            user_path=user_path,
            current_position=None,
            thermal_state=None,
            terminal_state=None,
            interventions_received=[],
        )

    def get_node_detail(
        self,
        project_id: int,
        event_name: str,
        date_from: date,
        date_to: date,
    ) -> NodeDetailResponse:
        node = self.db.query(JourneyNode).filter(
            JourneyNode.project_id == project_id,
            JourneyNode.event_name == event_name,
            JourneyNode.period_start <= date_to,
            JourneyNode.period_end >= date_from,
        ).first()

        if not node:
            raise ValueError(f"Node '{event_name}' not found in materialized data")

        schema = self.db.query(MessagingEventSchema).filter(
            MessagingEventSchema.project_id == project_id,
            MessagingEventSchema.event_name == event_name,
        ).first()

        # Top incoming edges
        incoming = self.db.query(JourneyEdge).filter(
            JourneyEdge.project_id == project_id,
            JourneyEdge.target_event == event_name,
            JourneyEdge.period_start <= date_to,
            JourneyEdge.period_end >= date_from,
        ).order_by(JourneyEdge.total_transitions.desc()).limit(5).all()

        total_in = sum(e.total_transitions for e in incoming) or 1
        top_incoming = [
            EdgeSummaryDTO(
                event_name=e.source_event,
                volume=e.total_transitions,
                pct=round(e.total_transitions / total_in, 4),
            ) for e in incoming
        ]

        # Top outgoing edges
        outgoing = self.db.query(JourneyEdge).filter(
            JourneyEdge.project_id == project_id,
            JourneyEdge.source_event == event_name,
            JourneyEdge.period_start <= date_to,
            JourneyEdge.period_end >= date_from,
        ).order_by(JourneyEdge.total_transitions.desc()).limit(5).all()

        total_out = sum(e.total_transitions for e in outgoing) or 1
        top_outgoing = [
            EdgeSummaryDTO(
                event_name=e.target_event,
                volume=e.total_transitions,
                pct=round(e.total_transitions / total_out, 4),
            ) for e in outgoing
        ]

        bottleneck_score = self._bottleneck_score(node)

        return NodeDetailResponse(
            event_name=node.event_name,
            display_name=schema.display_name if schema else node.event_name.replace('_', ' ').title(),
            category=schema.category if schema else 'custom',
            frequency=node.frequency,
            unique_users=node.unique_users,
            current_occupancy=node.current_occupancy,
            thermal=ThermalBreakdownDTO(
                hot=node.hot_count, warm=node.warm_count,
                cold=node.cold_count, dead=node.dead_count,
            ),
            throughput_rate=node.throughput_rate,
            avg_dwell_seconds=node.avg_dwell_seconds,
            conversion_rate=node.conversion_rate,
            is_bottleneck=bottleneck_score > BOTTLENECK_THRESHOLD,
            top_incoming=top_incoming,
            top_outgoing=top_outgoing,
        )

    def _bottleneck_score(self, node: JourneyNode) -> float:
        """TOC bottleneck score: high occupancy + low throughput + high dwell."""
        throughput = node.throughput_rate or 0.0
        occupancy = node.current_occupancy or 0
        dwell = node.avg_dwell_seconds or 0.0
        return (1.0 - throughput) * math.log(occupancy + 1) * math.log(dwell + 1)

    def _compute_terminal_nodes(self, project_id: int) -> List[TerminalNodeDTO]:
        """Compute terminal node counts from journey_snapshots."""
        from sqlalchemy import func as sqlfunc

        results = []
        for state in ('converted', 'churned', 'engagement_timeout'):
            rows = self.db.query(
                JourneySnapshot.current_event,
                sqlfunc.count(JourneySnapshot.id),
            ).filter(
                JourneySnapshot.project_id == project_id,
                JourneySnapshot.terminal_state == state,
            ).group_by(JourneySnapshot.current_event).all()

            if rows:
                total = sum(r[1] for r in rows)
                from_events = [{"event_name": r[0], "count": r[1]} for r in rows[:5]]
                results.append(TerminalNodeDTO(type=state, count=total, from_events=from_events))

        return results


# ============================================================================
# Event Flow Warning Detection
# ============================================================================

SHADOW_PAIRS = {
    ('auth_signup_success', 'gtm_user_registered'),
    ('auth_login_success', 'gtm_user_login'),
    ('auth_logout', 'gtm_user_logout'),
    ('trial_started', 'gtm_trial_started'),
    ('onboarding_business_info_completed', 'gtm_business_info_complete'),
}


def _are_semantic_duplicates(a: str, b: str) -> bool:
    return (a, b) in SHADOW_PAIRS or (b, a) in SHADOW_PAIRS


def _detect_warnings(steps: List[UserPathStepDTO]) -> None:
    """Analyze user path steps and add warnings for flow issues."""
    if len(steps) < 2:
        return

    # Pre-compute event name counts
    name_counts: Dict[str, int] = {}
    for s in steps:
        name_counts[s.event_name] = name_counts.get(s.event_name, 0) + 1

    # Track which steps are already part of a burst to avoid double-tagging
    burst_tagged = set()

    for i in range(len(steps)):
        step = steps[i]

        # 1. Burst detection: find clusters of events within 1 second
        if i not in burst_tagged:
            burst_end = i
            while burst_end < len(steps) - 1:
                gap = steps[burst_end].dwell_seconds
                if gap is not None and gap < 1.0:
                    burst_end += 1
                else:
                    break
            burst_size = burst_end - i + 1
            if burst_size >= 3:
                related = list(range(i, burst_end + 1))
                event_names = [steps[j].event_name for j in related]
                steps[i].warnings.append(StepWarning(
                    type="burst",
                    message=f"{burst_size} events fired within 1 second: {', '.join(set(event_names))}",
                    related_steps=related,
                ))
                burst_tagged.update(related)

        # 2. Shadow pair: two events <100ms apart that are semantic duplicates
        if i > 0 and i not in burst_tagged:
            prev_dwell = steps[i - 1].dwell_seconds
            if prev_dwell is not None and prev_dwell < 0.1:
                if _are_semantic_duplicates(steps[i - 1].event_name, step.event_name):
                    step.warnings.append(StepWarning(
                        type="shadow_pair",
                        message=f"Fires simultaneously with '{steps[i - 1].event_name}' — consider merging into a single event",
                        related_steps=[i - 1, i],
                    ))

        # 3. Repeated: same event_name appears 3+ times
        count = name_counts.get(step.event_name, 0)
        if count >= 3 and not step.warnings:
            indices = [j for j, s in enumerate(steps) if s.event_name == step.event_name]
            # Only tag the first occurrence
            if indices[0] == i:
                step.warnings.append(StepWarning(
                    type="repeated",
                    message=f"'{step.event_name}' fired {count} times in this journey — may indicate retries or abandoned attempts",
                    related_steps=indices,
                ))


def get_data_health(db, project_id: int) -> DataHealthResponse:
    """Analyze event data quality across all users in a project."""
    from sqlalchemy import text

    # Get co-occurrence pairs: events that fire within 100ms of each other
    pair_sql = text("""
        WITH ordered AS (
            SELECT user_id, event_name, created_at,
                   LEAD(event_name) OVER (PARTITION BY user_id ORDER BY created_at) AS next_event,
                   EXTRACT(EPOCH FROM LEAD(created_at) OVER (PARTITION BY user_id ORDER BY created_at) - created_at) AS gap
            FROM messaging_events
            WHERE project_id = :pid
              AND source NOT IN ('system')
              AND user_id IS NOT NULL
              AND created_at >= NOW() - interval '90 days'
        )
        SELECT event_name, next_event, COUNT(*) AS pair_count,
               AVG(gap) AS avg_gap
        FROM ordered
        WHERE next_event IS NOT NULL AND gap < 0.1
        GROUP BY event_name, next_event
        HAVING COUNT(*) >= 3
        ORDER BY pair_count DESC
        LIMIT 20
    """)
    shadow_rows = db.execute(pair_sql, {"pid": project_id}).fetchall()

    # Get burst frequency: users with 4+ events in <1 second
    burst_sql = text("""
        WITH ordered AS (
            SELECT user_id, event_name, created_at,
                   EXTRACT(EPOCH FROM created_at - LAG(created_at, 3) OVER (PARTITION BY user_id ORDER BY created_at)) AS window_4
            FROM messaging_events
            WHERE project_id = :pid AND source NOT IN ('system')
              AND user_id IS NOT NULL AND created_at >= NOW() - interval '90 days'
        )
        SELECT COUNT(DISTINCT user_id) AS affected_users
        FROM ordered WHERE window_4 IS NOT NULL AND window_4 < 1.0
    """)
    burst_result = db.execute(burst_sql, {"pid": project_id}).fetchone()
    burst_users = burst_result[0] if burst_result else 0

    # Total users for percentage
    total_users_sql = text("""
        SELECT COUNT(DISTINCT user_id) FROM messaging_events
        WHERE project_id = :pid AND user_id IS NOT NULL AND created_at >= NOW() - interval '90 days'
    """)
    total_users = db.execute(total_users_sql, {"pid": project_id}).scalar() or 1

    # Total events
    total_events_sql = text("""
        SELECT COUNT(*) FROM messaging_events
        WHERE project_id = :pid AND created_at >= NOW() - interval '90 days'
    """)
    total_events = db.execute(total_events_sql, {"pid": project_id}).scalar() or 0

    recommendations = []

    # Shadow pair recommendations
    for row in shadow_rows:
        evt_a, evt_b, count, avg_gap = row
        pct = round(count / total_users * 100)
        recommendations.append(DataHealthRecommendation(
            severity="high" if pct > 80 else "medium",
            category="shadow_pair",
            title=f"Duplicate event pair detected",
            description=f"'{evt_a}' and '{evt_b}' fire within {round(avg_gap * 1000)}ms of each other in {count} cases ({pct}% of users)",
            affected_events=[evt_a, evt_b],
            suggestion=f"Merge '{evt_a}' and '{evt_b}' into a single event, or add deduplication logic in your SDK/backend",
        ))

    # Burst recommendation
    if burst_users > 0:
        burst_pct = round(burst_users / total_users * 100)
        recommendations.append(DataHealthRecommendation(
            severity="high" if burst_pct > 50 else "medium",
            category="burst",
            title="Event bursts detected",
            description=f"{burst_users} users ({burst_pct}%) have 4+ events firing within 1 second — likely programmatic batch triggers",
            affected_events=[],
            suggestion="Review your SDK implementation — batch events that represent the same user action into a single composite event",
        ))

    # Repeated event recommendations
    repeat_sql = text("""
        SELECT event_name, AVG(cnt)::int AS avg_repeats, COUNT(*) AS affected_users
        FROM (
            SELECT user_id, event_name, COUNT(*) AS cnt
            FROM messaging_events
            WHERE project_id = :pid AND user_id IS NOT NULL
              AND created_at >= NOW() - interval '90 days'
              AND source NOT IN ('system')
            GROUP BY user_id, event_name
            HAVING COUNT(*) >= 3
        ) sub
        GROUP BY event_name
        HAVING COUNT(*) >= 3
        ORDER BY COUNT(*) DESC
        LIMIT 5
    """)
    repeat_rows = db.execute(repeat_sql, {"pid": project_id}).fetchall()
    for row in repeat_rows:
        evt, avg_reps, affected = row
        recommendations.append(DataHealthRecommendation(
            severity="low",
            category="repeated",
            title=f"Frequently repeated event",
            description=f"'{evt}' fires an average of {avg_reps} times per user across {affected} users",
            affected_events=[evt],
            suggestion=f"If '{evt}' represents retries or abandoned attempts, consider tracking attempt count as a property instead of separate events",
        ))

    # Score: 100 minus penalties
    score = 100
    for r in recommendations:
        if r.severity == "high":
            score -= 15
        elif r.severity == "medium":
            score -= 8
        else:
            score -= 3
    score = max(0, score)

    return DataHealthResponse(
        score=score,
        total_events_analyzed=total_events,
        recommendations=recommendations,
    )


def compute_beta_posterior(alpha: float, beta: float) -> tuple:
    """
    Compute Beta distribution stats analytically (no scipy needed).
    Returns (mean, ci_lower, ci_upper) for 95% CI.
    """
    if alpha <= 0 or beta <= 0:
        return (None, None, None)

    mean = alpha / (alpha + beta)
    variance = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1))
    std = math.sqrt(variance) if variance > 0 else 0.0

    ci_lower = max(0.0, mean - 1.96 * std)
    ci_upper = min(1.0, mean + 1.96 * std)

    return (round(mean, 4), round(ci_lower, 4), round(ci_upper, 4))
