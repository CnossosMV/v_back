"""
Journey Materializer Worker — background worker that populates
journey_edges, journey_nodes, journey_interventions, and journey_snapshots
from raw messaging_events data.

Runs every 5 minutes with advisory lock to prevent concurrent runs.
"""
import logging
import asyncio
import math
from datetime import datetime, timedelta, date
from typing import Optional, List, Tuple

from sqlalchemy import text, func as sqlfunc
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.messaging import MessagingEvent, MessagingEventSchema
from app.models.journey import (
    JourneyNode, JourneyEdge, JourneyIntervention, JourneySnapshot,
)

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 739001
# Exclude system/send events from behavioral transitions
SYSTEM_SOURCES = ('send_service', 'system', 'delivery_tracker')
DEFAULT_LOOKBACK_DAYS = 90


class JourneyMaterializerWorker:
    """Background worker that materializes the event graph."""

    def __init__(self, poll_interval: int = 300):
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Journey materializer already running")
            return
        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Journey materializer worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Journey materializer worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = self._process_cycle(db)
                    if processed > 0:
                        logger.info("Journey materializer processed %d projects", processed)
                finally:
                    db.close()
            except Exception as e:
                logger.error("Error in journey materializer: %s", e)

            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db: Session) -> int:
        """Run one materialization cycle across all projects."""
        acquired = db.execute(
            text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_ID})")
        ).scalar()
        if not acquired:
            return 0

        try:
            return self._do_materialize(db)
        finally:
            db.execute(text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})"))

    def _do_materialize(self, db: Session) -> int:
        from app.models import Project

        project_ids = [
            pid for (pid,) in
            db.query(Project.id).filter(Project.is_active == True).all()
        ]
        if not project_ids:
            return 0

        now = datetime.utcnow()
        period_start = (now - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date()
        period_end = now.date()
        processed = 0

        for pid in project_ids:
            try:
                project = db.query(Project).get(pid)
                goal_event = project.goal_event if project else None

                self._materialize_edges(db, pid, period_start, period_end, goal_event)
                self._materialize_nodes(db, pid, period_start, period_end, goal_event)
                self._materialize_interventions(db, pid, period_start, period_end, goal_event)
                self._materialize_snapshots(db, pid)
                db.commit()
                processed += 1
            except Exception as e:
                logger.error("Error materializing project %d: %s", pid, e)
                db.rollback()

        return processed

    # ──────────────────────────────────────────────────────────────────
    # EDGES — behavioral transitions A → B
    # ──────────────────────────────────────────────────────────────────

    def _materialize_edges(
        self, db: Session, project_id: int,
        period_start: date, period_end: date,
        goal_event: Optional[str],
    ) -> None:
        """Compute transitions using window function LEAD()."""
        # Anonymous transitions (parallel — partitioned by anonymous_id).
        anon_sql = text("""
            WITH visitor_events AS (
                SELECT
                    anonymous_id,
                    event_name,
                    LEAD(event_name) OVER (PARTITION BY anonymous_id ORDER BY created_at) AS next_event
                FROM messaging_events
                WHERE project_id = :pid
                  AND created_at >= :ps
                  AND created_at <= :pe + interval '1 day'
                  AND source NOT IN ('send_service', 'system', 'delivery_tracker')
                  AND user_id IS NULL
                  AND anonymous_id IS NOT NULL
            )
            SELECT event_name, next_event,
                   COUNT(*) AS frequency_anon,
                   COUNT(DISTINCT anonymous_id) AS unique_anon_visitors
            FROM visitor_events
            WHERE next_event IS NOT NULL
            GROUP BY event_name, next_event
        """)
        anon_rows = db.execute(anon_sql, {
            "pid": project_id, "ps": period_start, "pe": period_end,
        }).fetchall()
        anon_edge_map: dict = {(r[0], r[1]): (r[2], r[3]) for r in anon_rows}

        sql = text("""
            WITH user_events AS (
                SELECT
                    user_id,
                    event_name,
                    created_at,
                    LEAD(event_name) OVER (PARTITION BY user_id ORDER BY created_at) AS next_event,
                    LEAD(created_at) OVER (PARTITION BY user_id ORDER BY created_at) AS next_ts
                FROM messaging_events
                WHERE project_id = :pid
                  AND created_at >= :ps
                  AND created_at <= :pe + interval '1 day'
                  AND source NOT IN ('send_service', 'system', 'delivery_tracker')
                  AND user_id IS NOT NULL
            ),
            transitions AS (
                SELECT
                    event_name AS source_event,
                    next_event AS target_event,
                    user_id,
                    EXTRACT(EPOCH FROM (next_ts - created_at)) AS gap_seconds
                FROM user_events
                WHERE next_event IS NOT NULL
            )
            SELECT
                source_event,
                target_event,
                COUNT(*) AS total_transitions,
                COUNT(DISTINCT user_id) AS unique_users,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY gap_seconds) AS median_seconds,
                PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY gap_seconds) AS p90_seconds
            FROM transitions
            GROUP BY source_event, target_event
        """)

        rows = db.execute(sql, {
            "pid": project_id, "ps": period_start, "pe": period_end,
        }).fetchall()

        # Compute converted_transitions if goal_event set
        converted_map = {}
        if goal_event:
            converted_map = self._compute_converted_edges(db, project_id, period_start, period_end, goal_event)

        # Compute drop-off rates per source event
        source_totals = {}
        for row in rows:
            src = row[0]
            source_totals[src] = source_totals.get(src, 0) + row[2]

        for row in rows:
            src, tgt, total, uniq, median_s, p90_s = row
            converted = converted_map.get((src, tgt), 0)
            non_converted = total - converted
            alpha = converted + 1.0
            beta = non_converted + 1.0

            # Drop-off: what fraction of traffic from this source does NOT go to this target
            src_total = source_totals.get(src, total)
            drop_off = 1.0 - (total / src_total) if src_total > 0 else 0.0

            freq_a, uniq_a = anon_edge_map.pop((src, tgt), (0, 0))

            stmt = pg_insert(JourneyEdge).values(
                project_id=project_id,
                source_event=src,
                target_event=tgt,
                period_start=period_start,
                period_end=period_end,
                total_transitions=total,
                converted_transitions=converted,
                unique_users=uniq,
                frequency_anon=freq_a,
                unique_anon_visitors=uniq_a,
                median_seconds=median_s,
                p90_seconds=p90_s,
                beta_alpha=alpha,
                beta_beta=beta,
                drop_off_rate=drop_off,
            ).on_conflict_do_update(
                constraint='uq_je_proj_src_tgt_period',
                set_={
                    'total_transitions': total,
                    'converted_transitions': converted,
                    'unique_users': uniq,
                    'frequency_anon': freq_a,
                    'unique_anon_visitors': uniq_a,
                    'median_seconds': median_s,
                    'p90_seconds': p90_s,
                    'beta_alpha': alpha,
                    'beta_beta': beta,
                    'drop_off_rate': drop_off,
                },
            )
            db.execute(stmt)

        # Anonymous-only transitions (no identified equivalent).
        for (src, tgt), (freq_a, uniq_a) in anon_edge_map.items():
            stmt = pg_insert(JourneyEdge).values(
                project_id=project_id,
                source_event=src,
                target_event=tgt,
                period_start=period_start,
                period_end=period_end,
                total_transitions=0,
                converted_transitions=0,
                unique_users=0,
                frequency_anon=freq_a,
                unique_anon_visitors=uniq_a,
            ).on_conflict_do_update(
                constraint='uq_je_proj_src_tgt_period',
                set_={
                    'frequency_anon': freq_a,
                    'unique_anon_visitors': uniq_a,
                },
            )
            db.execute(stmt)

    def _compute_converted_edges(
        self, db: Session, project_id: int,
        period_start: date, period_end: date,
        goal_event: str,
    ) -> dict:
        """Count how many transitions A→B were made by users who eventually reached the goal."""
        sql = text("""
            WITH goal_users AS (
                SELECT DISTINCT user_id
                FROM messaging_events
                WHERE project_id = :pid
                  AND event_name = :goal
                  AND created_at >= :ps
                  AND created_at <= :pe + interval '1 day'
            ),
            user_events AS (
                SELECT
                    user_id, event_name, created_at,
                    LEAD(event_name) OVER (PARTITION BY user_id ORDER BY created_at) AS next_event
                FROM messaging_events
                WHERE project_id = :pid
                  AND created_at >= :ps
                  AND created_at <= :pe + interval '1 day'
                  AND source NOT IN ('send_service', 'system', 'delivery_tracker')
                  AND user_id IS NOT NULL
            )
            SELECT event_name, next_event, COUNT(*)
            FROM user_events
            WHERE next_event IS NOT NULL
              AND user_id IN (SELECT user_id FROM goal_users)
            GROUP BY event_name, next_event
        """)
        rows = db.execute(sql, {
            "pid": project_id, "ps": period_start, "pe": period_end, "goal": goal_event,
        }).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    # ──────────────────────────────────────────────────────────────────
    # NODES — per-event aggregates
    # ──────────────────────────────────────────────────────────────────

    def _materialize_nodes(
        self, db: Session, project_id: int,
        period_start: date, period_end: date,
        goal_event: Optional[str],
    ) -> None:
        # Anonymous-visitor aggregation (parallel to identified).
        # By construction (anonymous_id and user_id are disjoint), summing
        # these counters with the identified ones gives the all-visitors total.
        anon_sql = text("""
            SELECT event_name,
                   COUNT(*) AS frequency_anon,
                   COUNT(DISTINCT anonymous_id) AS unique_anon_visitors
            FROM messaging_events
            WHERE project_id = :pid
              AND created_at >= :ps
              AND created_at <= :pe + interval '1 day'
              AND source NOT IN ('send_service', 'system', 'delivery_tracker')
              AND user_id IS NULL
              AND anonymous_id IS NOT NULL
            GROUP BY event_name
        """)
        anon_rows = db.execute(anon_sql, {
            "pid": project_id, "ps": period_start, "pe": period_end,
        }).fetchall()
        anon_map: dict = {r[0]: (r[1], r[2]) for r in anon_rows}

        sql = text("""
            WITH event_agg AS (
                SELECT
                    event_name,
                    COUNT(*) AS frequency,
                    COUNT(DISTINCT user_id) AS unique_users
                FROM messaging_events
                WHERE project_id = :pid
                  AND created_at >= :ps
                  AND created_at <= :pe + interval '1 day'
                  AND source NOT IN ('send_service', 'system', 'delivery_tracker')
                  AND user_id IS NOT NULL
                GROUP BY event_name
            ),
            latest_per_user AS (
                SELECT DISTINCT ON (user_id)
                    user_id, event_name, created_at
                FROM messaging_events
                WHERE project_id = :pid
                  AND source NOT IN ('send_service', 'system', 'delivery_tracker')
                  AND user_id IS NOT NULL
                ORDER BY user_id, created_at DESC
            ),
            occupancy AS (
                SELECT
                    event_name,
                    COUNT(*) AS current_occupancy,
                    SUM(CASE WHEN created_at >= NOW() - interval '48 hours' THEN 1 ELSE 0 END) AS hot_count,
                    SUM(CASE WHEN created_at < NOW() - interval '48 hours'
                              AND created_at >= NOW() - interval '7 days' THEN 1 ELSE 0 END) AS warm_count,
                    SUM(CASE WHEN created_at < NOW() - interval '7 days'
                              AND created_at >= NOW() - interval '30 days' THEN 1 ELSE 0 END) AS cold_count,
                    SUM(CASE WHEN created_at < NOW() - interval '30 days' THEN 1 ELSE 0 END) AS dead_count
                FROM latest_per_user
                GROUP BY event_name
            )
            SELECT
                ea.event_name,
                ea.frequency,
                ea.unique_users,
                COALESCE(o.current_occupancy, 0),
                COALESCE(o.hot_count, 0),
                COALESCE(o.warm_count, 0),
                COALESCE(o.cold_count, 0),
                COALESCE(o.dead_count, 0)
            FROM event_agg ea
            LEFT JOIN occupancy o ON ea.event_name = o.event_name
        """)

        rows = db.execute(sql, {
            "pid": project_id, "ps": period_start, "pe": period_end,
        }).fetchall()

        # Compute throughput per node from edges
        edge_data = db.query(
            JourneyEdge.source_event,
            sqlfunc.sum(JourneyEdge.unique_users),
        ).filter(
            JourneyEdge.project_id == project_id,
            JourneyEdge.period_start == period_start,
            JourneyEdge.period_end == period_end,
        ).group_by(JourneyEdge.source_event).all()

        departures = {r[0]: r[1] for r in edge_data}

        # Compute dwell time per node
        dwell_sql = text("""
            WITH user_events AS (
                SELECT
                    user_id, event_name, created_at,
                    LEAD(created_at) OVER (PARTITION BY user_id ORDER BY created_at) AS next_ts
                FROM messaging_events
                WHERE project_id = :pid
                  AND created_at >= :ps
                  AND created_at <= :pe + interval '1 day'
                  AND source NOT IN ('send_service', 'system', 'delivery_tracker')
                  AND user_id IS NOT NULL
            )
            SELECT event_name, AVG(EXTRACT(EPOCH FROM (next_ts - created_at))) AS avg_dwell
            FROM user_events
            WHERE next_ts IS NOT NULL
            GROUP BY event_name
        """)
        dwell_rows = db.execute(dwell_sql, {"pid": project_id, "ps": period_start, "pe": period_end}).fetchall()
        dwell_map = {r[0]: r[1] for r in dwell_rows}

        # Conversion rate per node (if goal event)
        conversion_map = {}
        if goal_event:
            conv_sql = text("""
                WITH goal_users AS (
                    SELECT DISTINCT user_id
                    FROM messaging_events
                    WHERE project_id = :pid AND event_name = :goal
                      AND created_at >= :ps AND created_at <= :pe + interval '1 day'
                ),
                touched AS (
                    SELECT event_name, COUNT(DISTINCT user_id) AS total,
                           COUNT(DISTINCT CASE WHEN user_id IN (SELECT user_id FROM goal_users) THEN user_id END) AS converted
                    FROM messaging_events
                    WHERE project_id = :pid
                      AND created_at >= :ps AND created_at <= :pe + interval '1 day'
                      AND source NOT IN ('send_service', 'system', 'delivery_tracker')
                      AND user_id IS NOT NULL
                    GROUP BY event_name
                )
                SELECT event_name, CASE WHEN total > 0 THEN converted::float / total ELSE 0 END
                FROM touched
            """)
            conv_rows = db.execute(conv_sql, {
                "pid": project_id, "ps": period_start, "pe": period_end, "goal": goal_event,
            }).fetchall()
            conversion_map = {r[0]: r[1] for r in conv_rows}

        for row in rows:
            evt, freq, uniq, occ, hot, warm, cold, dead = row
            throughput = departures.get(evt, 0) / uniq if uniq > 0 else 0.0
            conv_rate = conversion_map.get(evt, 0.0)
            freq_a, uniq_a = anon_map.pop(evt, (0, 0))

            stmt = pg_insert(JourneyNode).values(
                project_id=project_id,
                event_name=evt,
                period_start=period_start,
                period_end=period_end,
                frequency=freq,
                unique_users=uniq,
                frequency_anon=freq_a,
                unique_anon_visitors=uniq_a,
                current_occupancy=occ,
                hot_count=hot,
                warm_count=warm,
                cold_count=cold,
                dead_count=dead,
                throughput_rate=min(throughput, 1.0),
                avg_dwell_seconds=dwell_map.get(evt),
                conversion_rate=conv_rate,
            ).on_conflict_do_update(
                constraint='uq_jn_proj_evt_period',
                set_={
                    'frequency': freq,
                    'unique_users': uniq,
                    'frequency_anon': freq_a,
                    'unique_anon_visitors': uniq_a,
                    'current_occupancy': occ,
                    'hot_count': hot,
                    'warm_count': warm,
                    'cold_count': cold,
                    'dead_count': dead,
                    'throughput_rate': min(throughput, 1.0),
                    'avg_dwell_seconds': dwell_map.get(evt),
                    'conversion_rate': conv_rate,
                },
            )
            db.execute(stmt)

        # Anonymous-only events (no identified equivalent yet) — upsert with
        # zero identified counters but real anonymous counters.
        for evt, (freq_a, uniq_a) in anon_map.items():
            stmt = pg_insert(JourneyNode).values(
                project_id=project_id,
                event_name=evt,
                period_start=period_start,
                period_end=period_end,
                frequency=0,
                unique_users=0,
                frequency_anon=freq_a,
                unique_anon_visitors=uniq_a,
            ).on_conflict_do_update(
                constraint='uq_jn_proj_evt_period',
                set_={
                    'frequency_anon': freq_a,
                    'unique_anon_visitors': uniq_a,
                },
            )
            db.execute(stmt)

    # ──────────────────────────────────────────────────────────────────
    # INTERVENTIONS — system sends bridged into the graph
    # ──────────────────────────────────────────────────────────────────

    def _materialize_interventions(
        self, db: Session, project_id: int,
        period_start: date, period_end: date,
        goal_event: Optional[str],
    ) -> None:
        sql = text("""
            WITH send_events AS (
                SELECT
                    id, user_id, created_at, properties
                FROM messaging_events
                WHERE project_id = :pid
                  AND source = 'send_service'
                  AND created_at >= :ps
                  AND created_at <= :pe + interval '1 day'
                  AND user_id IS NOT NULL
            ),
            with_context AS (
                SELECT
                    se.id AS send_id,
                    se.user_id,
                    se.created_at AS send_ts,
                    se.properties,
                    -- Last user event before the send
                    (SELECT event_name FROM messaging_events me
                     WHERE me.project_id = :pid
                       AND me.user_id = se.user_id
                       AND me.created_at < se.created_at
                       AND me.source NOT IN ('send_service', 'system', 'delivery_tracker')
                     ORDER BY me.created_at DESC LIMIT 1) AS pre_event,
                    -- First user event after the send
                    (SELECT event_name FROM messaging_events me
                     WHERE me.project_id = :pid
                       AND me.user_id = se.user_id
                       AND me.created_at > se.created_at
                       AND me.source NOT IN ('send_service', 'system', 'delivery_tracker')
                     ORDER BY me.created_at ASC LIMIT 1) AS post_event
                FROM send_events se
            )
            SELECT
                COALESCE(pre_event, '__entry__') AS pre_event,
                properties->>'arm_id' AS arm_id,
                properties->>'channel' AS channel,
                properties->>'source_type' AS source_type,
                (properties->>'template_id')::int AS template_id,
                COUNT(*) AS total_sent,
                COUNT(post_event) AS total_responded,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY
                    CASE WHEN post_event IS NOT NULL THEN
                        EXTRACT(EPOCH FROM (
                            (SELECT created_at FROM messaging_events me
                             WHERE me.project_id = :pid
                               AND me.user_id = with_context.user_id
                               AND me.created_at > send_ts
                               AND me.source NOT IN ('send_service', 'system', 'delivery_tracker')
                             ORDER BY me.created_at ASC LIMIT 1) - send_ts
                        ))
                    END
                ) AS median_response_seconds
            FROM with_context
            WHERE properties->>'arm_id' IS NOT NULL
            GROUP BY pre_event, properties->>'arm_id', properties->>'channel',
                     properties->>'source_type', (properties->>'template_id')::int
        """)

        rows = db.execute(sql, {
            "pid": project_id, "ps": period_start, "pe": period_end,
        }).fetchall()

        for row in rows:
            pre_evt, arm_id, channel, source_type, template_id, sent, responded, med_resp = row
            if not arm_id or not channel:
                continue

            alpha = responded + 1.0
            beta = (sent - responded) + 1.0

            stmt = pg_insert(JourneyIntervention).values(
                project_id=project_id,
                period_start=period_start,
                period_end=period_end,
                pre_event=pre_evt,
                channel=channel,
                source_type=source_type or 'unknown',
                template_id=template_id,
                arm_id=arm_id,
                total_sent=sent,
                total_responded=responded,
                total_converted=0,
                median_response_seconds=med_resp,
                beta_alpha=alpha,
                beta_beta=beta,
            ).on_conflict_do_update(
                constraint='uq_ji_proj_pre_arm_period',
                set_={
                    'total_sent': sent,
                    'total_responded': responded,
                    'median_response_seconds': med_resp,
                    'beta_alpha': alpha,
                    'beta_beta': beta,
                    'channel': channel,
                    'source_type': source_type or 'unknown',
                    'template_id': template_id,
                },
            )
            db.execute(stmt)

    # ──────────────────────────────────────────────────────────────────
    # SNAPSHOTS — per-user current state
    # ──────────────────────────────────────────────────────────────────

    def _materialize_snapshots(self, db: Session, project_id: int) -> None:
        """Bulk-update journey_snapshots for users whose last event has changed."""
        sql = text("""
            WITH latest AS (
                SELECT DISTINCT ON (user_id)
                    user_id, event_name, created_at
                FROM messaging_events
                WHERE project_id = :pid
                  AND source NOT IN ('send_service', 'system', 'delivery_tracker')
                  AND user_id IS NOT NULL
                ORDER BY user_id, created_at DESC
            )
            SELECT
                user_id, event_name, created_at,
                CASE
                    WHEN created_at >= NOW() - interval '48 hours' THEN 'hot'
                    WHEN created_at >= NOW() - interval '7 days' THEN 'warm'
                    WHEN created_at >= NOW() - interval '30 days' THEN 'cold'
                    ELSE 'dead'
                END AS thermal_state
            FROM latest
        """)

        rows = db.execute(sql, {"pid": project_id}).fetchall()
        for row in rows:
            uid, evt, ts, thermal = row
            stmt = pg_insert(JourneySnapshot).values(
                project_id=project_id,
                user_id=uid,
                current_event=evt,
                current_event_at=ts,
                thermal_state=thermal,
            ).on_conflict_do_update(
                constraint='uq_js_proj_user',
                set_={
                    'current_event': evt,
                    'current_event_at': ts,
                    'thermal_state': thermal,
                    'updated_at': datetime.utcnow(),
                },
            )
            db.execute(stmt)


# Singleton instance
journey_materializer_worker = JourneyMaterializerWorker()
