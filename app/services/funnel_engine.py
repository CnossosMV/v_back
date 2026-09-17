"""
Funnel Engine — core execution logic for multi-step funnels.

Handles enrollment, step advancement, event triggers, and wait processing.
"""
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import and_, text

from app.models import EventAction, Funnel, FunnelStep, FunnelEnrollment, FunnelEnrollmentLog, FunnelEnrollmentThread
from app.models.messaging import MessagingUser, MessagingEvent
from app.services.messaging.phone_normalizer import PhoneNormalizer
from app.services.messaging.template_renderer import template_renderer

logger = logging.getLogger(__name__)


class FunnelEngine:
    # Priority levels for deferred sends
    PRIORITY_NORMAL = 0
    PRIORITY_HIGH = 10
    PRIORITY_URGENT = 20

    def __init__(self, db: Session):
        self.db = db

    def _funnel_execution_gate(self, funnel: Funnel) -> dict:
        from app.services.orchestration_cutover_service import OrchestrationCutoverService
        return OrchestrationCutoverService(self.db).execution_gate(
            funnel.project_id, getattr(funnel, "purpose_key", None),
        )

    def _allow_enrollment_progress(self, enrollment: FunnelEnrollment) -> bool:
        funnel = enrollment.funnel
        if not funnel:
            return False
        gate = self._funnel_execution_gate(funnel)
        metadata = dict(enrollment.enrollment_metadata or {})
        if gate["allowed"]:
            if "_orchestration_blocked" in metadata:
                metadata.pop("_orchestration_blocked", None)
                enrollment.enrollment_metadata = metadata
            return True
        marker = {
            "purpose_key": gate.get("purpose_key"),
            "mode": gate.get("mode"),
            "orchestration_epoch": gate.get("orchestration_epoch"),
        }
        if metadata.get("_orchestration_blocked") != marker:
            metadata["_orchestration_blocked"] = marker
            enrollment.enrollment_metadata = metadata
            self._log(enrollment, enrollment.current_step, "orchestration_blocked", marker)
        return False

    def _send_source(self, enrollment: FunnelEnrollment, fallback_source_id: Optional[int] = None) -> tuple[str, Optional[int]]:
        """Return public send attribution for a funnel enrollment.

        EventAction rules execute through mirrored system funnels internally. For
        send logs and policy traces, expose those sends as EventAction activity
        so operators don't confuse the hidden implementation with a real funnel.
        """
        funnel = enrollment.funnel
        if funnel and getattr(funnel, "source", None) == "event_action" and getattr(funnel, "event_action_id", None):
            return "event_action", funnel.event_action_id
        return "funnel", fallback_source_id if fallback_source_id is not None else enrollment.funnel_id

    # ------------------------------------------------------------------
    # Phone normalization helper
    # ------------------------------------------------------------------

    def _get_wa_fallback_cc(self, project_id: int) -> Optional[str]:
        """Get default_country_code from the project's first active WhatsApp instance."""
        try:
            from app.models import WhatsAppInstance, Project
            project = self.db.query(Project).filter(Project.id == project_id).first()
            if not project:
                return None
            instance = self.db.query(WhatsAppInstance).filter(
                WhatsAppInstance.workspace_id == project.workspace_id,
                WhatsAppInstance.is_active == True,
                WhatsAppInstance.default_country_code != None,
            ).first()
            return instance.default_country_code if instance else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Event-property namespace helper
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_event_into_metadata(
        enrollment: "FunnelEnrollment",
        event_name: str,
        event_props: dict,
    ) -> None:
        """Namespace event properties into enrollment_metadata.

        Structure produced in ``enrollment_metadata``:
        - Flat top-level merge (last-write-wins, backward compat)
        - ``events.<event_name>``  → latest props
        - ``events.<event_name>._history`` → list of snapshots (capped at 20)

        Callers must still ``flag_modified(enrollment, "enrollment_metadata")``
        after calling this helper (or let the caller's existing call cover it).
        """
        meta = dict(enrollment.enrollment_metadata or {})
        now = datetime.utcnow().isoformat() + "Z"

        # 1. Flat merge (backward compat) — skip internal keys
        for k, v in event_props.items():
            if not k.startswith("_"):
                meta[k] = v

        # 2. Namespaced merge under events.<event_name>
        events_dict = meta.setdefault("events", {})
        existing = events_dict.get(event_name, {})
        history = list(existing.get("_history", []))

        # Append snapshot to history (cap at 20)
        snapshot = {**event_props, "_at": now}
        history.append(snapshot)
        if len(history) > 20:
            history = history[-20:]

        # Overwrite latest
        new_entry = {**event_props, "_history": history}
        events_dict[event_name] = new_entry

        # _last shorthand — always points to the most recent event regardless of name
        events_dict["_last"] = {**event_props, "_name": event_name, "_at": now}

        meta["events"] = events_dict

        enrollment.enrollment_metadata = meta

    # ------------------------------------------------------------------
    # Enrollment
    # ------------------------------------------------------------------

    def enroll_user(
        self,
        funnel_id: int,
        user_id: int,
        metadata: Optional[dict] = None,
        bypass_status_check: bool = False,
    ) -> Optional[FunnelEnrollment]:
        """Enroll a user in a funnel, process the first step immediately."""
        filters = [Funnel.id == funnel_id]
        if not bypass_status_check:
            filters.append(Funnel.status == "active")
        funnel = self.db.query(Funnel).filter(*filters).first()
        if not funnel:
            logger.warning(f"Funnel {funnel_id} not active or not found")
            return None
        gate = self._funnel_execution_gate(funnel)
        if not gate["allowed"]:
            logger.info(
                "Funnel %s enrollment blocked: purpose=%s mode=%s epoch=%s",
                funnel.id, funnel.purpose_key, gate.get("mode"), gate.get("orchestration_epoch"),
            )
            return None

        # Dedup — one active enrollment per user+funnel
        existing = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.funnel_id == funnel_id,
            FunnelEnrollment.user_id == user_id,
            FunnelEnrollment.status == "active",
        ).first()
        if existing:
            logger.debug(f"User {user_id} already enrolled in funnel {funnel_id}")
            return existing

        # Automation pause: refuse NEW enrollments (segment triggers,
        # event-action add_to_funnel) while an operator handles the contact
        # manually. manual_enroll stays ungated — explicit admin intent.
        paused = self.db.query(MessagingUser.id).filter(
            MessagingUser.id == user_id,
            MessagingUser.automations_paused == True,  # noqa: E712
        ).first()
        if paused:
            logger.info(
                f"User {user_id} not enrolled in funnel {funnel_id}: automations paused"
            )
            return None

        from app.services.orchestration_impact_service import OrchestrationImpactService

        entry = OrchestrationImpactService(self.db).apply_entry("funnel", funnel, user_id)
        if not entry["allowed"]:
            logger.info(
                "Funnel %s enrollment rejected by attention policy for user %s: %s",
                funnel.id, user_id, entry.get("reason"),
            )
            return None

        # Find first step (position=0 or lowest, branch='main')
        first_step = (
            self.db.query(FunnelStep)
            .filter(
                FunnelStep.funnel_id == funnel_id,
                FunnelStep.branch == "main",
                FunnelStep.parent_step_id.is_(None),
            )
            .order_by(FunnelStep.position)
            .first()
        )

        now = datetime.utcnow()
        enrollment = FunnelEnrollment(
            funnel_id=funnel_id,
            user_id=user_id,
            status="active",
            current_step_id=first_step.id if first_step else None,
            current_branch="main",
            enrollment_metadata=metadata or {},
            entered_step_at=now,
        )
        self.db.add(enrollment)
        self.db.flush()

        self._log(enrollment, first_step, "entered", {})

        if first_step:
            self._process_current_step(enrollment, first_step)

        self.db.commit()
        return enrollment

    # ------------------------------------------------------------------
    # Manual Enrollment (admin recovery)
    # ------------------------------------------------------------------

    def manual_enroll(
        self,
        funnel_id: int,
        user_id: int,
        target_step_ids: Optional[List[int]] = None,
        source_enrollment_id: Optional[int] = None,
        copy_metadata: bool = True,
        metadata_overrides: Optional[dict] = None,
        force_exit_active: bool = False,
    ) -> dict:
        """Manually enroll (or re-enroll) a user at specific funnel step(s).

        target_step_ids supports:
        - Empty/None: start from the first step
        - Single step on main branch: simple enrollment at that step
        - Single step inside a fork path: enrollment in that one path only
        - Multiple steps in different fork paths: enrollment with threads for each path

        Returns dict with enrollment info or error details.
        """
        funnel = self.db.query(Funnel).filter(Funnel.id == funnel_id).first()
        if not funnel:
            return {"error": "Funnel not found"}

        user = self.db.query(MessagingUser).filter(MessagingUser.id == user_id).first()
        if not user:
            return {"error": "User not found"}

        # Resolve target steps
        if not target_step_ids:
            first_step = (
                self.db.query(FunnelStep)
                .filter(
                    FunnelStep.funnel_id == funnel_id,
                    FunnelStep.branch == "main",
                    FunnelStep.parent_step_id.is_(None),
                )
                .order_by(FunnelStep.position)
                .first()
            )
            if not first_step:
                return {"error": "Funnel has no steps"}
            target_step_ids = [first_step.id]

        # Validate and load all target steps
        target_steps: List[FunnelStep] = []
        for sid in target_step_ids:
            step = self.db.query(FunnelStep).filter(
                FunnelStep.id == sid,
                FunnelStep.funnel_id == funnel_id,
            ).first()
            if not step:
                return {"error": f"Target step {sid} not found in this funnel"}
            target_steps.append(step)

        # Handle existing active enrollment
        force_exited_id = None
        existing = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.funnel_id == funnel_id,
            FunnelEnrollment.user_id == user_id,
            FunnelEnrollment.status == "active",
        ).first()
        if existing:
            if not force_exit_active:
                return {"error": "User already has an active enrollment. Set force_exit_active to true."}
            self._exit_enrollment(existing, "manual_superseded")
            force_exited_id = existing.id
            self.db.flush()

        # Build metadata
        metadata: dict = {}
        if source_enrollment_id and copy_metadata:
            source = self.db.query(FunnelEnrollment).filter(
                FunnelEnrollment.id == source_enrollment_id,
            ).first()
            if source and source.enrollment_metadata:
                # Copy user-facing metadata but strip internal state keys
                # (e.g. _approval_paused, _approval_ticket_id, _whatsapp_handoff_*)
                # to prevent stale state from blocking the new enrollment.
                metadata = {
                    k: v for k, v in source.enrollment_metadata.items()
                    if not k.startswith("_")
                }
        if metadata_overrides:
            metadata.update(metadata_overrides)

        from app.services.orchestration_impact_service import OrchestrationImpactService

        entry = OrchestrationImpactService(self.db).apply_entry("funnel", funnel, user_id)
        if not entry["allowed"]:
            return {
                "error": "Enrollment rejected by attention policy",
                "reason": entry.get("reason"),
                "blocking_asset": entry.get("blocking_asset"),
            }
        metadata["_manual_enroll"] = {
            "by": "admin",
            "at": datetime.utcnow().isoformat(),
            "source_enrollment_id": source_enrollment_id,
            "target_step_ids": [s.id for s in target_steps],
        }

        now = datetime.utcnow()
        # Use the first target as the enrollment's primary current step
        primary_step = target_steps[0]
        enrollment = FunnelEnrollment(
            funnel_id=funnel_id,
            user_id=user_id,
            status="active",
            current_step_id=primary_step.id,
            current_branch=primary_step.branch if len(target_steps) == 1 else "main",
            enrollment_metadata=metadata,
            entered_step_at=now,
        )
        self.db.add(enrollment)
        self.db.flush()

        # Determine if targets are inside fork paths
        # Group targets by their fork ancestor: {fork_step_id: [(path_branch, target_step)]}
        fork_targets: Dict[int, List[tuple]] = {}
        non_fork_targets: List[FunnelStep] = []

        for ts in target_steps:
            fork_info = self._find_fork_ancestor(ts)
            if fork_info:
                fork_step_id, path_branch = fork_info
                fork_targets.setdefault(fork_step_id, []).append((path_branch, ts))
            else:
                non_fork_targets.append(ts)

        self._log(enrollment, primary_step, "manual_enrolled", {
            "source_enrollment_id": source_enrollment_id,
            "target_step_ids": [s.id for s in target_steps],
            "copied_metadata": bool(source_enrollment_id and copy_metadata),
            "force_exited_id": force_exited_id,
            "fork_paths": len(fork_targets),
        })

        if fork_targets:
            # Set enrollment's current_step to the fork step itself (parent of
            # threads) up front, so a synchronous fork-join during processing
            # advances from the right place instead of being overwritten.
            enrollment.current_step_id = next(iter(fork_targets))
            enrollment.current_branch = "main"

            # Create threads for each fork path target
            thread_idx = 0
            for fork_step_id, path_targets in fork_targets.items():
                fork_step = self.db.query(FunnelStep).filter(FunnelStep.id == fork_step_id).first()
                enrollment.thread_count = (enrollment.thread_count or 0) + len(path_targets)

                # Pass 1: create all threads for this fork before processing any.
                # A thread that completes synchronously must not join the fork
                # while sibling threads are still being created.
                created_threads: List[tuple] = []  # (thread, ts, path_branch)
                for path_branch, ts in path_targets:
                    thread = FunnelEnrollmentThread(
                        enrollment_id=enrollment.id,
                        thread_index=thread_idx,
                        fork_step_id=fork_step_id,
                        parent_thread_id=None,
                        label=f"Manual: {path_branch}",
                        status="active",
                        current_step_id=ts.id,
                        current_branch=ts.branch,
                        entered_step_at=now,
                    )
                    self.db.add(thread)
                    created_threads.append((thread, ts, path_branch))
                    thread_idx += 1

                self.db.flush()
                # Drop the stale cached collection so _check_fork_join sees every thread.
                self.db.expire(enrollment, ["threads"])

                # Pass 2: process each thread's target step.
                for thread, ts, path_branch in created_threads:
                    self._log(enrollment, ts, "entered", {
                        "thread_index": thread.thread_index,
                        "branch": path_branch,
                        "manual": True,
                    })
                    self._process_thread_step(enrollment, thread, ts)
        elif non_fork_targets:
            # Single non-fork target
            self._process_current_step(enrollment, non_fork_targets[0])

        self.db.commit()
        return {
            "enrollment_id": enrollment.id,
            "user_id": user_id,
            "target_step_ids": [s.id for s in target_steps],
            "copied_metadata": bool(source_enrollment_id and copy_metadata),
            "source_enrollment_id": source_enrollment_id,
            "force_exited_enrollment_id": force_exited_id,
        }

    def _find_fork_ancestor(self, step: FunnelStep) -> Optional[tuple]:
        """Find the immediate fork ancestor of a step.

        Returns (fork_step_id, path_branch) or None if not inside a fork.
        """
        current = step
        while current:
            if current.branch.startswith("path_") and current.parent_step_id:
                parent = self.db.query(FunnelStep).filter(
                    FunnelStep.id == current.parent_step_id
                ).first()
                if parent and parent.step_type == "fork":
                    return (parent.id, current.branch)
            if current.parent_step_id:
                current = self.db.query(FunnelStep).filter(
                    FunnelStep.id == current.parent_step_id
                ).first()
            else:
                break
        return None

    def _resolve_step_ancestry(self, step: FunnelStep) -> List[tuple]:
        """Walk up parent_step_id chain. Returns [(step, branch), ...] from root to target."""
        chain = []
        current = step
        while current:
            chain.append((current, current.branch))
            if current.parent_step_id:
                current = self.db.query(FunnelStep).filter(
                    FunnelStep.id == current.parent_step_id
                ).first()
            else:
                break
        chain.reverse()
        return chain

    # ------------------------------------------------------------------
    # Advance
    # ------------------------------------------------------------------

    def advance_enrollment(self, enrollment: FunnelEnrollment) -> None:
        """Advance an enrollment from its current step."""
        if enrollment.status != "active":
            return
        if enrollment.funnel and enrollment.funnel.status != "active":
            logger.info(
                "Skipping enrollment %s because parent funnel %s is %s",
                enrollment.id,
                enrollment.funnel_id,
                enrollment.funnel.status,
            )
            return
        if not self._allow_enrollment_progress(enrollment):
            return

        # Do not advance if enrollment is paused waiting for approval
        meta = enrollment.enrollment_metadata or {}
        if meta.get("_approval_paused"):
            return

        step = enrollment.current_step
        if not step:
            self._complete_enrollment(enrollment, "no_steps")
            return

        step_type = step.step_type

        if step_type == "wait":
            if not self._wait_elapsed(enrollment, step):
                return  # still waiting
            elapsed = (datetime.utcnow() - enrollment.entered_step_at).total_seconds() if enrollment.entered_step_at else None
            self._log(enrollment, step, "wait_completed", {
                "outputs": {"elapsed_seconds": elapsed},
            })
            self._advance_to_next(enrollment, step)

        elif step_type == "action":
            deferred = self._execute_action_step(enrollment, step)
            if not deferred:
                self._advance_to_next(enrollment, step)

        elif step_type == "condition":
            self._handle_condition_step(enrollment, step)

        elif step_type == "exit":
            reason = (step.step_config or {}).get("reason", "completed")
            self._complete_enrollment(enrollment, reason)

    # ------------------------------------------------------------------
    # Event triggers
    # ------------------------------------------------------------------

    def check_event_triggers(self, db: Session, event) -> List[FunnelEnrollment]:
        """Check active funnels for event-based triggers matching the event.

        Evaluates trigger_config.conditions before enrollment so mirrored
        event-action funnels can filter by event properties / user traits.
        Orders by trigger_config.priority DESC.
        Skips enrollment when a recent enrollment exists within the funnel's
        cooldown_seconds window.
        """
        enrolled = []

        # Snapshot event attrs upfront — prior commits (e.g. event_action_engine)
        # may have expired them via expire_on_commit.
        event_props = dict(event.properties or {})
        event_event_name = event.event_name
        event_user_id = event.user_id
        event_record_id = getattr(event, "id", None)
        event_external_id = getattr(event, "external_event_id", None)

        funnels = db.query(Funnel).filter(
            Funnel.status == "active",
            Funnel.trigger_type == "event",
            Funnel.project_id == event.project_id,
        ).all()
        event_action_ids = [
            f.event_action_id for f in funnels
            if getattr(f, "source", None) == "event_action" and f.event_action_id
        ]
        event_action_rules = {}
        if event_action_ids:
            event_action_rules = {
                ea.id: ea for ea in db.query(EventAction)
                .filter(EventAction.id.in_(event_action_ids))
                .all()
            }

        # Priority-ordered: higher priority first. User funnels default to 0.
        funnels.sort(key=lambda f: int((f.trigger_config or {}).get("priority") or 0), reverse=True)

        if not event_user_id:
            return enrolled

        evaluator = None
        user = None

        for funnel in funnels:
            trigger_cfg = funnel.trigger_config or {}
            event_action_rule = (
                event_action_rules.get(funnel.event_action_id)
                if getattr(funnel, "source", None) == "event_action"
                else None
            )
            ev_name = (
                event_action_rule.trigger_event
                if event_action_rule is not None
                else trigger_cfg.get("event_name")
            )
            if not ev_name or ev_name != event_event_name:
                continue

            conditions = (
                list(event_action_rule.conditions or [])
                if event_action_rule is not None
                else trigger_cfg.get("conditions") or []
            )
            if conditions:
                if evaluator is None:
                    from app.services.event_actions.conditions import ConditionEvaluator
                    evaluator = ConditionEvaluator()
                if user is None:
                    user = db.query(MessagingUser).filter(MessagingUser.id == event_user_id).first()
                user_data = {}
                if user:
                    user_data = {
                        "email": user.email,
                        "phone": user.phone,
                        "name": user.name,
                        "external_id": user.external_id,
                        "segment_name": user.segment_name,
                        **(user.properties or {}),
                    }
                event_data = {
                    "event_name": event_event_name,
                    "properties": event_props,
                }
                match_mode = trigger_cfg.get("match_mode", "all")
                if not evaluator.evaluate_all(conditions, event_data, user_data, match_mode):
                    continue

            # Cooldown check — mirrors EventActionCooldown semantics.
            # EventAction funnels complete immediately. Playground bypasses
            # cooldown by design, so dedupe the exact trigger event separately.
            if getattr(funnel, "source", None) == "event_action" and (event_record_id or event_external_id):
                lock_key = f"event_action_funnel:{funnel.id}:{event_user_id}:{event_record_id or event_external_id}"
                try:
                    bind = db.get_bind()
                    if bind is not None and bind.dialect.name == "postgresql":
                        db.execute(
                            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                            {"lock_key": lock_key},
                        )
                except Exception as exc:
                    logger.warning(
                        "EventAction idempotency lock unavailable funnel=%s user=%s event=%s: %s",
                        funnel.id,
                        event_user_id,
                        event_record_id or event_external_id,
                        exc,
                    )

                recent_cutoff = datetime.utcnow() - timedelta(days=30)
                recent_enrollments = (
                    db.query(FunnelEnrollment)
                    .filter(
                        FunnelEnrollment.funnel_id == funnel.id,
                        FunnelEnrollment.user_id == event_user_id,
                        FunnelEnrollment.enrolled_at > recent_cutoff,
                    )
                    .all()
                )
                duplicate_event_enrollment = False
                for recent_enrollment in recent_enrollments:
                    meta = recent_enrollment.enrollment_metadata or {}
                    if event_record_id and str(meta.get("_trigger_event_record_id")) == str(event_record_id):
                        duplicate_event_enrollment = True
                    if event_external_id and (
                        str(meta.get("_trigger_external_event_id")) == str(event_external_id)
                        or str(meta.get("event_id")) == str(event_external_id)
                    ):
                        duplicate_event_enrollment = True
                    if duplicate_event_enrollment:
                        logger.info(
                            "Skipping duplicate EventAction funnel enrollment funnel=%s user=%s event=%s",
                            funnel.id,
                            event_user_id,
                            event_record_id or event_external_id,
                        )
                        break
                if duplicate_event_enrollment:
                    continue

            playground_bypass_cooldown = (
                event_props.get("playground_source") == "tabloide_admin_test"
                and event_props.get("playground_bypass_cooldown") is True
            )
            cooldown = funnel.cooldown_seconds
            if cooldown and cooldown > 0 and not playground_bypass_cooldown:
                cutoff = datetime.utcnow() - timedelta(seconds=cooldown)
                recent = (
                    db.query(FunnelEnrollment)
                    .filter(
                        FunnelEnrollment.funnel_id == funnel.id,
                        FunnelEnrollment.user_id == event_user_id,
                        FunnelEnrollment.enrolled_at > cutoff,
                    )
                    .first()
                )
                if recent:
                    logger.debug(
                        "Skipping enrollment (cooldown) funnel=%s user=%s",
                        funnel.id, event_user_id,
                    )
                    continue

            # Build complete metadata upfront so condition steps see all data.
            trigger_meta = dict(event_props)
            trigger_meta["_trigger_event"] = event_event_name
            if event_record_id:
                trigger_meta["_trigger_event_record_id"] = str(event_record_id)
            if event_external_id:
                trigger_meta["_trigger_external_event_id"] = str(event_external_id)
            now = datetime.utcnow().isoformat() + "Z"
            snapshot = {**event_props, "_at": now}
            trigger_meta["events"] = {
                event_event_name: {**event_props, "_history": [snapshot]},
                "_last": {**event_props, "_name": event_event_name, "_at": now},
            }

            enrollment = self.enroll_user(funnel.id, event_user_id, metadata=trigger_meta)
            if enrollment:
                enrolled.append(enrollment)

        return enrolled

    # ------------------------------------------------------------------
    # Segment triggers
    # ------------------------------------------------------------------

    def process_segment_triggers(self) -> int:
        """Evaluate segment-based funnels and enroll matching users."""
        now = datetime.utcnow()
        funnels = self.db.query(Funnel).filter(
            Funnel.status == "active",
            Funnel.trigger_type == "segment",
        ).all()

        enrolled_count = 0
        for funnel in funnels:
            if not self._funnel_execution_gate(funnel)["allowed"]:
                continue
            trigger_cfg = funnel.trigger_config or {}
            conditions = trigger_cfg.get("conditions", [])
            if not conditions:
                continue

            # Throttle by evaluation frequency
            freq_hours = trigger_cfg.get("evaluation_frequency_hours", 24)
            last_eval = trigger_cfg.get("last_evaluated_at")
            if last_eval:
                try:
                    last_dt = datetime.fromisoformat(last_eval)
                    if now < last_dt + timedelta(hours=freq_hours):
                        continue
                except (ValueError, TypeError):
                    pass

            # Evaluate conditions against all project users
            match_mode = trigger_cfg.get("match_mode", "all")
            from app.services.event_actions.conditions import ConditionEvaluator
            evaluator = ConditionEvaluator()

            users = self.db.query(MessagingUser).filter(
                MessagingUser.project_id == funnel.project_id,
                MessagingUser.is_sandbox == False,
                MessagingUser.automations_paused == False,
            ).all()

            for user in users:
                user_data = {
                    "email": user.email,
                    "phone": user.phone,
                    "name": user.name,
                    "external_id": user.external_id,
                    "segment_name": user.segment_name,
                    "segment_rule_id": user.segment_rule_id,
                    **(user.properties or {}),
                }
                event_data = {"properties": {}}
                if evaluator.evaluate_all(conditions, event_data, user_data, match_mode):
                    enrollment = self.enroll_user(funnel.id, user.id)
                    if enrollment:
                        enrolled_count += 1

            # Update last_evaluated_at
            trigger_cfg["last_evaluated_at"] = now.isoformat()
            funnel.trigger_config = trigger_cfg
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(funnel, "trigger_config")

        if enrolled_count:
            self.db.commit()
        return enrolled_count

    # ------------------------------------------------------------------
    # Global exit rules
    # ------------------------------------------------------------------

    def check_global_exit_rules(self) -> int:
        """Check all active enrollments against their funnel's global exit rules."""
        now = datetime.utcnow()
        enrollments = (
            self.db.query(FunnelEnrollment)
            .join(MessagingUser, FunnelEnrollment.user_id == MessagingUser.id)
            .filter(
                FunnelEnrollment.status == "active",
                MessagingUser.is_sandbox == False,
            )
            .all()
        )

        exited = 0
        for enrollment in enrollments:
            funnel = enrollment.funnel
            if not funnel:
                continue
            exit_cfg = funnel.global_exit_config or {}

            # Time limit
            time_limit = exit_cfg.get("time_limit_hours")
            if time_limit and enrollment.enrolled_at:
                if now > enrollment.enrolled_at + timedelta(hours=time_limit):
                    self._exit_enrollment(enrollment, "time_limit")
                    exited += 1
                    continue

            # Message limit
            msg_limit = exit_cfg.get("message_limit")
            if msg_limit and (enrollment.messages_sent or 0) >= msg_limit:
                self._exit_enrollment(enrollment, "message_limit")
                exited += 1
                continue

        if exited:
            self.db.commit()
        return exited

    def check_goal_event(self, db: Session, event) -> int:
        """Check if an event matches a goal event for any active enrollments."""
        if not event.user_id:
            return 0

        enrollments = db.query(FunnelEnrollment).join(Funnel).filter(
            FunnelEnrollment.status == "active",
            FunnelEnrollment.user_id == event.user_id,
            Funnel.project_id == event.project_id,
        ).all()

        exited = 0
        now = datetime.utcnow()
        for enrollment in enrollments:
            funnel = enrollment.funnel
            exit_cfg = funnel.global_exit_config or {}
            goal_events = exit_cfg.get("goal_events") or (
                [exit_cfg["goal_event"]] if exit_cfg.get("goal_event") else []
            )
            if goal_events and event.event_name in goal_events:
                self._exit_enrollment(enrollment, "goal_reached", goal_event=event.event_name)
                exited += 1
                continue

            # event_within_window stop-conditions (mirrored from EventAction.stop_conditions).
            for rule in exit_cfg.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                if rule.get("type") != "event_within_window":
                    continue
                if rule.get("event_name") != event.event_name:
                    continue
                window = rule.get("within_seconds")
                if window and enrollment.enrolled_at:
                    if now > enrollment.enrolled_at + timedelta(seconds=int(window)):
                        continue
                self._exit_enrollment(enrollment, "stop_condition", goal_event=event.event_name)
                exited += 1
                break

        if exited:
            db.commit()
        return exited

    def _transition_to_exited(
        self,
        enrollment: FunnelEnrollment,
        reason: str,
        *,
        goal_event: str | None = None,
        status: str | None = None,
        kill_threads: bool = False,
    ) -> None:
        """Single terminal-transition point for an enrollment.

        Sets status/exit_reason/exited_at, applies the exit tag, emits the
        exit event. Every exit path routes through here so later phases can
        hook enrollment termination (e.g. the supersession episode-kill
        cascade) in exactly one place instead of three.

        kill_threads: force-kill any active threads. Forced-exit paths pass
        True so a forked enrollment never leaves orphaned active threads
        behind (the latent bug return_from_human had). Natural completion
        does NOT force — it guards against active threads in the caller and
        only transitions once they have finished.

        Per-path LOGGING stays with the caller (the log action/details differ
        by path). status: pass an explicit terminal status to preserve a
        path's existing semantics; default derives "completed" for reason
        "completed", else "exited".
        """
        if kill_threads and enrollment.thread_count:
            for thread in enrollment.threads:
                if thread.status == "active":
                    thread.status = "exited"
        enrollment.status = status or ("completed" if reason == "completed" else "exited")
        enrollment.exit_reason = reason
        enrollment.exited_at = datetime.utcnow()
        self._apply_exit_tag(enrollment, reason)
        self._emit_exit_event(enrollment, reason, goal_event=goal_event)
        # Phase 1 episode-kill cascade. A relevance-losing exit supersedes the
        # enrollment's still-pending sends; natural completion / goal-reached
        # PRESERVE them — a pending send is the episode's planned final effect
        # and is re-validated at dispatch (ruling 11: completion cancels nothing).
        if reason not in ("completed", "goal_reached"):
            self._supersede_pending_sends(enrollment, reason)

    def _supersede_pending_sends(self, enrollment: FunnelEnrollment, reason: str) -> None:
        """Flip this enrollment's pending deferred/delayed SendLogs (retries
        included — they carry deferred_source_enrollment_id) to 'superseded'.

        Status-guarded UPDATE so it's atomic against the dispatch worker: a row
        the worker already locked + sent is no longer deferred/delayed and won't
        match. SHADOW by default (SEND_SUPERSEDE_ENFORCE) — logs the count it
        WOULD supersede so we can validate before enforcing."""
        from app.models import SendLog
        pending = self.db.query(SendLog).filter(
            SendLog.deferred_source_enrollment_id == enrollment.id,
            SendLog.status.in_(("deferred", "delayed")),
        ).all()
        if not pending:
            return
        from app.services.engine_rollout_service import effective_mode
        mode = effective_mode(self.db, enrollment.project_id, "supersede")
        if mode != "enforce":
            logger.info(
                "[supersede][%s] enrollment %s exit reason=%s WOULD supersede %d pending send(s)",
                mode, enrollment.id, reason, len(pending),
            )
            return
        now = datetime.utcnow()
        for log in pending:
            log.status = "superseded"
            log.superseded_at = now
            log.superseded_reason = f"episode_exit:{reason}"
        self.db.flush()
        logger.info(
            "[supersede] enrollment %s exit reason=%s superseded %d pending send(s)",
            enrollment.id, reason, len(pending),
        )

    def _exit_enrollment(self, enrollment: FunnelEnrollment, reason: str, goal_event: str | None = None) -> None:
        """Exit an enrollment with the given reason. Also kills active threads."""
        log_details: dict = {"reason": reason}
        if goal_event:
            log_details["goal_event"] = goal_event
        log_details["outputs"] = {
            "exit_reason": reason,
            "goal_event": goal_event,
            "exit_tag_applied": bool((enrollment.funnel.global_exit_config or {}).get("exit_tags", {}).get(reason)) if enrollment.funnel else False,
            "event_emitted": "funnel_completed" if reason == "completed" else "funnel_exited",
        }
        self._log(enrollment, enrollment.current_step, "exited", log_details)
        # Preserve existing semantics: this path marks status "exited" even
        # for reason "completed" (the funnel_completed *event* still fires),
        # and force-kills active threads (forced exit).
        self._transition_to_exited(
            enrollment, reason, goal_event=goal_event, status="exited", kill_threads=True,
        )

    # ------------------------------------------------------------------
    # Exit tag + event helpers
    # ------------------------------------------------------------------

    def _apply_exit_tag(self, enrollment: FunnelEnrollment, reason: str) -> None:
        """Tag the contact based on the exit reason, if configured in global_exit_config.exit_tags."""
        funnel = enrollment.funnel
        if not funnel:
            return
        exit_cfg = funnel.global_exit_config or {}
        exit_tags = exit_cfg.get("exit_tags", {})
        tag = exit_tags.get(reason)
        if not tag:
            return

        user = self.db.query(MessagingUser).filter(MessagingUser.id == enrollment.user_id).first()
        if not user:
            return

        from sqlalchemy.orm.attributes import flag_modified
        current_tags = list(user.tags or [])
        if tag not in current_tags:
            current_tags.append(tag)
        user.tags = current_tags
        flag_modified(user, "tags")

    def _emit_exit_event(self, enrollment: FunnelEnrollment, reason: str, goal_event: str | None = None) -> None:
        """Emit a funnel_completed or funnel_exited system event for downstream automation."""
        funnel = enrollment.funnel
        if not funnel:
            return

        event_name = "funnel_completed" if reason == "completed" else "funnel_exited"

        # Count steps completed (entered logs)
        steps_completed = self.db.query(FunnelEnrollmentLog).filter(
            FunnelEnrollmentLog.enrollment_id == enrollment.id,
            FunnelEnrollmentLog.action == "entered",
        ).count()

        # Count messages sent
        messages_sent = self.db.query(FunnelEnrollmentLog).filter(
            FunnelEnrollmentLog.enrollment_id == enrollment.id,
            FunnelEnrollmentLog.action == "action_executed",
        ).count()

        properties = {
            "funnel_id": funnel.id,
            "funnel_name": funnel.name,
            "exit_reason": reason,
            "enrollment_id": enrollment.id,
            "steps_completed": steps_completed,
            "messages_sent": messages_sent,
        }
        if goal_event:
            properties["goal_event"] = goal_event

        event = MessagingEvent(
            project_id=funnel.project_id,
            user_id=enrollment.user_id,
            event_name=event_name,
            source="system",
            processed=False,
            properties=properties,
        )
        self.db.add(event)
        self.db.flush()

    # ------------------------------------------------------------------
    # Suppression
    # ------------------------------------------------------------------

    # Log actions that count as "a message was sent (or queued to send)"
    _SUPPRESSION_ACTIONS = {
        "whatsapp_template_sent",
        "email_sent",
        "approval_requested",
    }

    def _check_suppression(self, enrollment: FunnelEnrollment) -> bool:
        """Return True if the enrollment is suppressed (should skip action)."""
        funnel = enrollment.funnel
        if not funnel:
            return False
        exit_cfg = funnel.global_exit_config or {}
        suppression_hours = exit_cfg.get("suppression_hours")
        if not suppression_hours:
            return False

        cutoff = datetime.utcnow() - timedelta(hours=suppression_hours)
        recent = self.db.query(FunnelEnrollmentLog).filter(
            FunnelEnrollmentLog.enrollment_id == enrollment.id,
            FunnelEnrollmentLog.action.in_(self._SUPPRESSION_ACTIONS),
            FunnelEnrollmentLog.created_at >= cutoff,
        ).first()
        return recent is not None

    def _get_suppression_window_end(self, enrollment: FunnelEnrollment) -> datetime:
        """Calculate when the current suppression window ends."""
        exit_cfg = enrollment.funnel.global_exit_config or {}
        suppression_hours = exit_cfg.get("suppression_hours", 1)
        last = self.db.query(FunnelEnrollmentLog).filter(
            FunnelEnrollmentLog.enrollment_id == enrollment.id,
            FunnelEnrollmentLog.action.in_(self._SUPPRESSION_ACTIONS),
        ).order_by(FunnelEnrollmentLog.created_at.desc()).first()
        if last:
            return last.created_at + timedelta(hours=suppression_hours)
        return datetime.utcnow() + timedelta(hours=suppression_hours)

    def _defer_suppressed_send(self, enrollment: FunnelEnrollment, step: FunnelStep) -> None:
        """Defer a send that is blocked by the suppression window instead of discarding it."""
        from app.services.channels.deferred_send_helper import DeferredSendHelper

        config = step.step_config or {}
        channel = config.get("channel", "whatsapp")
        window_end = self._get_suppression_window_end(enrollment)

        # Resolve recipient
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id
        ).first()
        recipient = ""
        if user:
            recipient = user.phone if channel == "whatsapp" else (user.email or "")

        # Build render context (same pattern as existing policy deferral)
        render_context = {
            "action_type": f"{channel}_send_message",
            "config": config,
            "enrollment_id": enrollment.id,
            "step_id": step.id,
            "instance_id": config.get("instance_id"),
        }

        # Include revalidation_condition if set
        reval = config.get("revalidation_condition")
        if reval:
            render_context["revalidation_condition"] = reval

        # Compute expires_at from max_deferred_hours if set
        exit_cfg = enrollment.funnel.global_exit_config or {}
        max_deferred_hours = exit_cfg.get("max_deferred_hours")
        expires_at = None
        if max_deferred_hours:
            expires_at = datetime.utcnow() + timedelta(hours=int(max_deferred_hours))

        # Determine priority from step config
        priority = config.get("message_priority", self.PRIORITY_NORMAL)
        send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)

        helper = DeferredSendHelper(self.db)
        log = helper.create_suppression_deferred(
            project_id=enrollment.funnel.project_id,
            user_id=enrollment.user_id,
            channel=channel,
            recipient=recipient,
            defer_until=window_end,
            reason="suppression_window",
            source_type=send_source_type,
            source_id=send_source_id,
            template_id=config.get("template_id"),
            render_context=render_context,
            expires_at=expires_at,
            enrollment_id=enrollment.id,
            priority=priority,
        )

        self._log(enrollment, step, "action_deferred", {
            "reason": "suppressed",
            "send_log_id": log.id,
            "defer_until": window_end.isoformat(),
            "context": {
                "suppression": {
                    "reason": "suppression_window",
                    "defer_until": window_end.isoformat(),
                },
            },
        })
        self.db.commit()
        # Do NOT advance — enrollment pauses here

    def _check_urgent_cooldown(self, enrollment: FunnelEnrollment) -> bool:
        """Check if enough time has passed since the last send for urgent messages.

        Returns True if the contact can receive an urgent message now.
        """
        exit_cfg = enrollment.funnel.global_exit_config or {}
        cooldown_min = exit_cfg.get("urgent_cooldown_minutes", 30)
        cutoff = datetime.utcnow() - timedelta(minutes=cooldown_min)
        recent = self.db.query(FunnelEnrollmentLog).filter(
            FunnelEnrollmentLog.enrollment_id == enrollment.id,
            FunnelEnrollmentLog.action.in_(self._SUPPRESSION_ACTIONS),
            FunnelEnrollmentLog.created_at >= cutoff,
        ).first()
        return recent is None

    def _defer_urgent_send(self, enrollment: FunnelEnrollment, step: FunnelStep) -> None:
        """Defer a skip_suppression send that is blocked by the urgent cooldown."""
        from app.services.channels.deferred_send_helper import DeferredSendHelper

        config = step.step_config or {}
        channel = config.get("channel", "whatsapp")
        exit_cfg = enrollment.funnel.global_exit_config or {}
        cooldown_min = exit_cfg.get("urgent_cooldown_minutes", 30)

        # Find the most recent send to calculate cooldown end
        last = self.db.query(FunnelEnrollmentLog).filter(
            FunnelEnrollmentLog.enrollment_id == enrollment.id,
            FunnelEnrollmentLog.action.in_(self._SUPPRESSION_ACTIONS),
        ).order_by(FunnelEnrollmentLog.created_at.desc()).first()
        if last:
            scheduled_at = last.created_at + timedelta(minutes=cooldown_min)
        else:
            scheduled_at = datetime.utcnow() + timedelta(minutes=cooldown_min)

        # Resolve recipient
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id
        ).first()
        recipient = ""
        if user:
            recipient = user.phone if channel == "whatsapp" else (user.email or "")

        render_context = {
            "action_type": f"{channel}_send_message",
            "config": config,
            "enrollment_id": enrollment.id,
            "step_id": step.id,
            "instance_id": config.get("instance_id"),
        }

        reval = config.get("revalidation_condition")
        if reval:
            render_context["revalidation_condition"] = reval

        # Compute expires_at from max_deferred_hours if set
        max_deferred_hours = exit_cfg.get("max_deferred_hours")
        expires_at = None
        if max_deferred_hours:
            expires_at = datetime.utcnow() + timedelta(hours=int(max_deferred_hours))

        send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
        helper = DeferredSendHelper(self.db)
        log = helper.create_suppression_deferred(
            project_id=enrollment.funnel.project_id,
            user_id=enrollment.user_id,
            channel=channel,
            recipient=recipient,
            defer_until=scheduled_at,
            reason="urgent_cooldown",
            source_type=send_source_type,
            source_id=send_source_id,
            template_id=config.get("template_id"),
            render_context=render_context,
            expires_at=expires_at,
            enrollment_id=enrollment.id,
            priority=self.PRIORITY_URGENT,
        )

        self._log(enrollment, step, "action_deferred", {
            "reason": "urgent_cooldown",
            "send_log_id": log.id,
            "defer_until": scheduled_at.isoformat(),
        })
        self.db.commit()

    # ------------------------------------------------------------------
    # Scheduler entry
    # ------------------------------------------------------------------

    def process_wait_steps(self) -> int:
        """Process all active enrollments on wait steps whose duration has elapsed."""
        now = datetime.utcnow()
        enrollments = (
            self.db.query(FunnelEnrollment)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .join(FunnelStep, FunnelEnrollment.current_step_id == FunnelStep.id)
            .join(MessagingUser, FunnelEnrollment.user_id == MessagingUser.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelStep.step_type == "wait",
                MessagingUser.is_sandbox == False,
                self._contact_not_paused(),
            )
            .all()
        )

        processed = 0
        for enrollment in enrollments:
            step = enrollment.current_step
            if self._wait_elapsed(enrollment, step):
                try:
                    self.advance_enrollment(enrollment)
                    processed += 1
                except Exception as e:
                    logger.error(f"Error advancing enrollment {enrollment.id}: {e}")

        if processed:
            self.db.commit()
        return processed

    def process_condition_steps(self) -> int:
        """Re-evaluate active enrollments stuck on condition steps."""
        enrollments = (
            self.db.query(FunnelEnrollment)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .join(FunnelStep, FunnelEnrollment.current_step_id == FunnelStep.id)
            .join(MessagingUser, FunnelEnrollment.user_id == MessagingUser.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelStep.step_type == "condition",
                MessagingUser.is_sandbox == False,
                self._contact_not_paused(),
            )
            .all()
        )

        processed = 0
        for enrollment in enrollments:
            step = enrollment.current_step
            try:
                self._handle_condition_step(enrollment, step)
                processed += 1
            except Exception as e:
                logger.error(f"Error processing condition step for enrollment {enrollment.id}: {e}")

        if processed:
            self.db.commit()
        return processed

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _contact_locale_tz(self, enrollment) -> tuple:
        """i18n: (locale, timezone) for the enrollment's contact, project defaults
        otherwise. Safe when flags are off (contact.locale is simply None)."""
        try:
            from app.models import Project
            from app.services.messaging.locale_resolver import contact_locale_tz
            user = self.db.query(MessagingUser).filter(MessagingUser.id == enrollment.user_id).first()
            project = self.db.query(Project).filter(Project.id == user.project_id).first() if user else None
            if not project:
                return None, None
            return contact_locale_tz(project, contact=user)
        except Exception:
            return None, None

    def _process_current_step(self, enrollment: FunnelEnrollment, step: FunnelStep = None) -> None:
        """Process the current step immediately if it's an action or exit."""
        if enrollment.funnel and enrollment.funnel.status != "active":
            logger.info(
                "Skipping step processing for enrollment %s because parent funnel %s is %s",
                enrollment.id,
                enrollment.funnel_id,
                enrollment.funnel.status,
            )
            return
        if not self._allow_enrollment_progress(enrollment):
            return

        # Do not process if enrollment is paused waiting for approval
        meta = enrollment.enrollment_metadata or {}
        if meta.get("_approval_paused"):
            return

        if step is None:
            step = enrollment.current_step
        if not step:
            return

        if step.step_type == "action":
            deferred = self._execute_action_step(enrollment, step)
            if not deferred:
                self._advance_to_next(enrollment, step)
        elif step.step_type == "exit":
            reason = (step.step_config or {}).get("reason", "completed")
            self._complete_enrollment(enrollment, reason)
        elif step.step_type == "condition":
            self._handle_condition_step(enrollment, step)
        elif step.step_type == "wait_for_reply":
            self._setup_wait_for_reply(enrollment, step)
        elif step.step_type == "wait_until":
            self._handle_wait_until_step(enrollment, step)
        elif step.step_type == "fork":
            self._handle_fork_step(enrollment, step)
        elif step.step_type == "send_message":
            self._handle_send_message_step(enrollment, step)
        elif step.step_type == "wait":
            config = step.step_config or {}
            wait_type = config.get("wait_type", "duration")
            expected_end = None

            if wait_type == "duration":
                duration = config.get("duration", 1)
                unit = config.get("unit", "hours")
                if unit == "minutes":
                    delta = timedelta(minutes=duration)
                elif unit == "days":
                    delta = timedelta(days=duration)
                else:
                    delta = timedelta(hours=duration)
                expected_end = enrollment.entered_step_at + delta if enrollment.entered_step_at else None
            elif wait_type == "until_day_time":
                tz_name = self._get_project_timezone(enrollment.funnel.project_id, config, enrollment)
                try:
                    tz = ZoneInfo(tz_name)
                except Exception:
                    tz = dt_timezone.utc
                entered = enrollment.entered_step_at.replace(tzinfo=dt_timezone.utc).astimezone(tz) if enrollment.entered_step_at else datetime.now(tz)
                target = self._next_weekday_time(entered, config.get("target_day", 0), config.get("target_time", "00:00"))
                if target <= entered:
                    target += timedelta(weeks=1)
                expected_end = target
            elif wait_type == "until_time":
                tz_name = self._get_project_timezone(enrollment.funnel.project_id, config, enrollment)
                try:
                    tz = ZoneInfo(tz_name)
                except Exception:
                    tz = dt_timezone.utc
                entered = enrollment.entered_step_at.replace(tzinfo=dt_timezone.utc).astimezone(tz) if enrollment.entered_step_at else datetime.now(tz)
                h, m = map(int, config.get("target_time", "00:00").split(":"))
                target = entered.replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= entered:
                    target += timedelta(days=1)
                expected_end = target
            elif wait_type == "until_datetime":
                target_iso = config.get("target_datetime")
                if target_iso:
                    try:
                        expected_end = datetime.fromisoformat(target_iso.replace("Z", "+00:00"))
                    except Exception:
                        pass
            elif wait_type == "until_timestamp":
                expected_end = self._resolve_dynamic_wait_target(enrollment, step, config)

            self._log(enrollment, step, "wait_started", {
                "inputs": config,
                "expected_end_at": expected_end.isoformat() if expected_end else None,
            })
        # wait → scheduler will pick it up after the wait_started log is written

    # ------------------------------------------------------------------
    # Timezone & weekly-minute helpers
    # ------------------------------------------------------------------

    def _get_project_timezone(self, project_id: int, step_config: dict = None, enrollment=None) -> str:
        """Resolve timezone: step_config override → ProjectSendConfig → UTC."""
        if step_config and step_config.get("timezone"):
            return step_config["timezone"]
        if enrollment is not None:
            _locale, contact_timezone = self._contact_locale_tz(enrollment)
            if contact_timezone:
                return contact_timezone
        from app.models import ProjectSendConfig
        cfg = self.db.query(ProjectSendConfig).filter(
            ProjectSendConfig.project_id == project_id,
        ).first()
        return (cfg.quiet_hours_timezone if cfg else None) or "UTC"

    @staticmethod
    def _compute_weekly_minute(dt) -> int:
        """Convert a datetime to weekly minutes (Mon 00:00 = 0 … Sun 23:59 = 10079)."""
        return dt.weekday() * 1440 + dt.hour * 60 + dt.minute

    @staticmethod
    def _is_in_weekly_window(now_wm: int, start_wm: int, end_wm: int) -> bool:
        """Check if now_wm is inside [start_wm, end_wm) on a 7-day cycle."""
        if start_wm <= end_wm:
            return start_wm <= now_wm < end_wm
        else:
            # Window wraps around the week (e.g., Sat 19:00 → Mon 05:00)
            return now_wm >= start_wm or now_wm < end_wm

    @staticmethod
    def _next_weekday_time(now, target_day: int, target_time: str):
        """Find the next occurrence of target_day (0=Mon) at target_time ('HH:MM')."""
        h, m = map(int, target_time.split(":"))
        days_ahead = target_day - now.weekday()
        if days_ahead < 0:
            days_ahead += 7
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_ahead)
        if candidate <= now and days_ahead == 0:
            candidate += timedelta(weeks=1)
        return candidate

    # ------------------------------------------------------------------
    # Wait elapsed dispatch
    # ------------------------------------------------------------------

    def _wait_elapsed(self, enrollment: FunnelEnrollment, step: FunnelStep) -> bool:
        config = step.step_config or {}
        wait_type = config.get("wait_type", "duration")

        if wait_type == "duration":
            return self._wait_elapsed_duration(enrollment, config)
        elif wait_type == "until_day_time":
            return self._wait_elapsed_until_day_time(enrollment, step, config)
        elif wait_type == "until_time":
            return self._wait_elapsed_until_time(enrollment, step, config)
        elif wait_type == "until_datetime":
            return self._wait_elapsed_until_datetime(config)
        elif wait_type == "until_timestamp":
            return self._wait_elapsed_until_timestamp(enrollment, step, config)
        return False

    @staticmethod
    def _dotted_value(root, path: str):
        value = root
        for part in path.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = getattr(value, part, None)
            if value is None:
                return None
        return value

    @staticmethod
    def _coerce_wait_timestamp(value):
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(value, tz=dt_timezone.utc)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_timezone.utc)
        return parsed.astimezone(dt_timezone.utc)

    def _resolve_dynamic_wait_target(self, enrollment: FunnelEnrollment, step: FunnelStep, config: dict):
        """Resolve and persist a dynamic timestamp once per enrollment/step.

        Persisting the target makes retries deterministic: a later profile
        update cannot silently move a wait that has already started.
        """
        from sqlalchemy.orm.attributes import flag_modified

        meta = dict(enrollment.enrollment_metadata or {})
        key = f"_wait_timestamp_{step.id}"
        cached = meta.get(key)
        if isinstance(cached, dict) and cached.get("target_at"):
            try:
                return self._coerce_wait_timestamp(cached["target_at"])
            except (ValueError, TypeError, OSError):
                return None
        if isinstance(cached, dict) and cached.get("missing") is True:
            return datetime.now(dt_timezone.utc) if cached.get("on_missing") == "advance" else None

        source = config.get("source")
        path = str(config.get("path") or "")
        if source == "contact":
            root = self.db.query(MessagingUser).filter(MessagingUser.id == enrollment.user_id).first()
        elif source == "enrollment":
            root = enrollment
        else:
            root = None
        raw = self._dotted_value(root, path) if root is not None and path else None
        try:
            target = self._coerce_wait_timestamp(raw)
        except (ValueError, TypeError, OSError):
            target = None
        on_missing = config.get("on_missing", "hold")
        if target is not None:
            target += timedelta(seconds=int(config.get("offset_seconds", 0)))
            state = {
                "target_at": target.isoformat(),
                "source": source,
                "path": path,
                "offset_seconds": int(config.get("offset_seconds", 0)),
                "resolved_at": datetime.now(dt_timezone.utc).isoformat(),
                "raw_value": str(raw),
                "missing": False,
            }
        else:
            if on_missing == "error":
                raise ValueError(f"until_timestamp could not resolve {source}.{path}")
            state = {
                "target_at": None,
                "source": source,
                "path": path,
                "resolved_at": datetime.now(dt_timezone.utc).isoformat(),
                "missing": True,
                "on_missing": on_missing,
            }
            if on_missing == "advance":
                target = datetime.now(dt_timezone.utc)
        meta[key] = state
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")
        self.db.flush()
        return target

    def _wait_elapsed_until_timestamp(self, enrollment: FunnelEnrollment, step: FunnelStep, config: dict) -> bool:
        target = self._resolve_dynamic_wait_target(enrollment, step, config)
        return target is not None and datetime.now(dt_timezone.utc) >= target

    @staticmethod
    def _wait_elapsed_duration(enrollment: FunnelEnrollment, config: dict) -> bool:
        duration = config.get("duration", 1)
        unit = config.get("unit", "hours")
        if unit == "minutes":
            delta = timedelta(minutes=duration)
        elif unit == "days":
            delta = timedelta(days=duration)
        else:
            delta = timedelta(hours=duration)
        return datetime.utcnow() >= enrollment.entered_step_at + delta

    def _wait_elapsed_until_day_time(self, enrollment: FunnelEnrollment, step: FunnelStep, config: dict) -> bool:
        """Wait until next [Weekday] at [Time] in project timezone."""
        target_day = config.get("target_day", 0)
        target_time = config.get("target_time", "00:00")
        tz_name = self._get_project_timezone(enrollment.funnel.project_id, config, enrollment)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = dt_timezone.utc
        now = datetime.now(tz)
        entered = enrollment.entered_step_at.replace(tzinfo=dt_timezone.utc).astimezone(tz)
        target_dt = self._next_weekday_time(entered, target_day, target_time)
        if target_dt <= entered:
            target_dt += timedelta(weeks=1)
        return now >= target_dt

    def _wait_elapsed_until_time(self, enrollment: FunnelEnrollment, step: FunnelStep, config: dict) -> bool:
        """Wait until next occurrence of [Time] daily in project timezone."""
        target_time = config.get("target_time", "00:00")
        tz_name = self._get_project_timezone(enrollment.funnel.project_id, config, enrollment)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = dt_timezone.utc
        now = datetime.now(tz)
        h, m = map(int, target_time.split(":"))
        entered = enrollment.entered_step_at.replace(tzinfo=dt_timezone.utc).astimezone(tz)
        target = entered.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= entered:
            target += timedelta(days=1)
        return now >= target

    @staticmethod
    def _wait_elapsed_until_datetime(config: dict) -> bool:
        """Wait until a specific one-shot ISO datetime."""
        target_iso = config.get("target_datetime")
        if not target_iso:
            return True
        try:
            target = datetime.fromisoformat(target_iso.replace("Z", "+00:00"))
            if target.tzinfo:
                return datetime.now(dt_timezone.utc) >= target
            return datetime.utcnow() >= target
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Sandbox interception
    # ------------------------------------------------------------------

    def _is_sandbox_contact(self, user_id: int) -> bool:
        """Check if a user is a sandbox test contact."""
        user = self.db.query(MessagingUser).filter(MessagingUser.id == user_id).first()
        return user.is_sandbox if user else False

    @staticmethod
    def _contact_not_paused():
        """Filter expression: enrollment's contact has automations paused.

        Correlates via FunnelEnrollment (present in every scheduler processor
        query) so it works whether or not MessagingUser is already joined.
        A paused contact's enrollments simply stop advancing (hold freeze);
        they resume on the next scheduler cycle after release."""
        from sqlalchemy import exists, and_
        return ~exists().where(and_(
            MessagingUser.id == FunnelEnrollment.user_id,
            MessagingUser.automations_paused == True,  # noqa: E712
        )).correlate(FunnelEnrollment)

    def _intercept_sandbox_action(
        self, enrollment, step, action_type, action_config, variables=None
    ):
        """Log a sandbox action interception, optionally send preview."""
        from app.models import SandboxSession, SandboxActionLog

        session = self.db.query(SandboxSession).filter(
            SandboxSession.contact_id == enrollment.user_id,
            SandboxSession.funnel_id == enrollment.funnel_id,
            SandboxSession.status == "active",
        ).first()
        if not session:
            return

        # Run suppression check (informational only)
        suppression_info = None
        try:
            suppression_info = {"suppressed": self._check_suppression(enrollment)}
        except Exception:
            pass

        intercepted_mode = "logged"
        preview_result = None

        # If preview mode, attempt to dispatch to user's channel
        if session.mode == "preview" and session.preview_destination:
            try:
                preview_result = self._send_preview(
                    session, action_type, action_config, variables
                )
                intercepted_mode = "preview_sent" if preview_result.get("success") else "preview_failed"
            except Exception as e:
                preview_result = {"success": False, "error": str(e)}
                intercepted_mode = "preview_failed"

        log = SandboxActionLog(
            session_id=session.id,
            enrollment_id=enrollment.id,
            step_id=step.id if step else None,
            action_type=action_type or "unknown",
            action_config=action_config,
            intercepted_mode=intercepted_mode,
            preview_result=preview_result,
            resolved_variables=variables,
            suppression_check=suppression_info,
        )
        self.db.add(log)
        self.db.flush()

    def _send_preview(self, session, action_type, action_config, variables):
        """Send a preview message to the sandbox session's preview destination."""
        channel = session.preview_channel or "email"
        destination = session.preview_destination

        if channel == "email" and destination:
            try:
                from app.services.email_service import EmailService
                email_svc = EmailService(self.db)
                subject = f"[Sandbox Preview] Action: {action_type}"
                body = (
                    f"<h3>Sandbox Preview</h3>"
                    f"<p><b>Action:</b> {action_type}</p>"
                    f"<p><b>Config:</b> <pre>{action_config}</pre></p>"
                    f"<p><b>Variables:</b> <pre>{variables}</pre></p>"
                )
                result = email_svc.send_html_email(
                    to_email=destination,
                    subject=subject,
                    html_body=body,
                )
                return result
            except Exception as e:
                return {"success": False, "error": str(e)}

        if channel == "whatsapp" and destination and session.preview_instance_id:
            try:
                from app.services.whatsapp_sender import WhatsAppSender
                from app.models import WhatsAppInstance
                import asyncio
                import concurrent.futures

                instance = self.db.query(WhatsAppInstance).filter(
                    WhatsAppInstance.id == session.preview_instance_id,
                ).first()
                if not instance:
                    return {"success": False, "error": "Preview instance not found"}

                sender = WhatsAppSender(self.db)
                text_msg = f"[Sandbox Preview] {action_type}: {action_config}"
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        sender.send_text_message(instance, destination, text_msg),
                    ).result(timeout=15)
                return result
            except Exception as e:
                return {"success": False, "error": str(e)}

        return {"success": False, "error": f"Unsupported preview channel: {channel}"}

    def _execute_action_step(self, enrollment: FunnelEnrollment, step: FunnelStep) -> bool:
        """Execute an action step via the ActionExecutor.

        Returns True if the step was deferred (caller must NOT advance).
        """
        # Sandbox interception — intercept before real dispatch
        if self._is_sandbox_contact(enrollment.user_id):
            config = step.step_config or {}
            action_type = config.get("action_type")
            action_config = config.get("config", {})
            self._intercept_sandbox_action(enrollment, step, action_type, action_config)
            self._log(enrollment, step, "action_executed", {"sandbox": True, "action_type": action_type})
            return

        # Check policy layer before executing send actions
        config = step.step_config or {}
        action_type = config.get("action_type")
        action_config = config.get("config", {})

        if action_type in ("send_template", "send_whatsapp_message"):
            # Funnels should carry sends as `send_message` steps, not `action` steps.
            # This branch is kept as a safety net for any un-migrated legacy data.
            logger.warning(
                "Deprecated funnel action step %s carries action_type=%s; "
                "run scripts/migrate_funnel_action_sends.py to convert to send_message.",
                step.id, action_type,
            )

        if action_type in ("send_template", "send_whatsapp_message") and enrollment.user_id:
            try:
                from app.services.scoring.policy_service import PolicyService
                funnel = enrollment.funnel
                policy_svc = PolicyService(self.db)
                channel = "whatsapp" if action_type == "send_whatsapp_message" else "email"
                send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
                decision = policy_svc.check_can_contact(
                    project_id=funnel.project_id,
                    user_id=enrollment.user_id,
                    channel=channel,
                    source=send_source_type,
                    source_id=send_source_id,
                )
                if not decision.allowed:
                    from app.services.channels.deferred_send_helper import DeferredSendHelper
                    helper = DeferredSendHelper(self.db)
                    if helper.should_defer(funnel.project_id, decision):
                        expires_hours = action_config.get("send_expires_after_hours")
                        from datetime import timedelta as _td
                        expires_at = (decision.defer_until + _td(hours=int(expires_hours))) if expires_hours and decision.defer_until else None
                        user = self.db.query(MessagingUser).filter(MessagingUser.id == enrollment.user_id).first()
                        recipient = ""
                        if user:
                            recipient = user.phone if channel == "whatsapp" else (user.email or "")
                        log = helper.create_deferred_send(
                            project_id=funnel.project_id,
                            user_id=enrollment.user_id,
                            channel=channel,
                            recipient=recipient,
                            decision=decision,
                            source_type=send_source_type,
                            source_id=send_source_id,
                            template_id=action_config.get("template_id"),
                            render_context={
                                "action_type": action_type,
                                "config": action_config,
                                "enrollment_id": enrollment.id,
                                "step_id": step.id,
                            },
                            expires_at=expires_at,
                            enrollment_id=enrollment.id,
                        )
                        self._log(enrollment, step, "action_deferred", {
                            "send_log_id": log.id,
                            "defer_until": decision.defer_until.isoformat() if decision.defer_until else None,
                            "context": {
                                "policy_decision": {
                                    "allowed": decision.allowed,
                                    "reason": decision.reason,
                                    "rule_name": getattr(decision, 'rule_name', None),
                                    "defer_until": decision.defer_until.isoformat() if decision.defer_until else None,
                                },
                            },
                        })
                        self.db.commit()
                        return True  # Deferred — caller must NOT advance
                    self._log(enrollment, step, "action_skipped", {
                        "reason": f"policy: {decision.reason}",
                        "context": {
                            "policy_decision": {
                                "allowed": decision.allowed,
                                "reason": decision.reason,
                                "defer_until": decision.defer_until.isoformat() if hasattr(decision, 'defer_until') and decision.defer_until else None,
                            },
                        },
                    })
                    return
            except Exception as e:
                logger.warning(f"Error checking policy for funnel step: {e}")
        if not action_type:
            self._log(enrollment, step, "action_failed", {"error": "no action_type"})
            return

        try:
            import asyncio
            from app.services.event_actions.executor import ActionExecutor, ActionContext

            user = self.db.query(MessagingUser).filter(
                MessagingUser.id == enrollment.user_id,
            ).first()
            user_data = None
            if user:
                # Fall back to the triggering event's contact fields (captured in
                # enrollment_metadata) when the contact row hasn't caught up yet —
                # e.g. welcome funnels that fire on registration before the email
                # column is committed. Prevents "Could not determine recipient".
                # Only accept a REAL address: a synthetic guest/anonymous
                # placeholder (guest_*@guest.tabloide.pro) must never be used as a
                # recipient — it hard-bounces and hurts sender reputation.
                from app.services.messaging.recipient_validation import (
                    first_sendable_email, project_placeholder_pattern,
                )
                _meta = enrollment.enrollment_metadata or {}
                _ph_pattern = project_placeholder_pattern(
                    self.db, getattr(enrollment.funnel, "project_id", None),
                )
                user_data = {
                    "id": user.id,
                    "external_id": user.external_id,
                    "email": first_sendable_email(
                        user.email, _meta.get("email"), _meta.get("user_email"),
                        pattern=_ph_pattern,
                    ),
                    "phone": user.phone or _meta.get("phone") or _meta.get("user_phone"),
                    "name": user.name or _meta.get("name") or _meta.get("user_name"),
                    "properties": user.properties or {},
                }

            funnel = enrollment.funnel

            # Build template variables: user fields + user properties + enrollment metadata
            # Later keys override earlier ones, so enrollment metadata wins
            variables = {}
            variables["enrollment_id"] = str(enrollment.id)
            variables["enrollment_status"] = enrollment.status or ""
            if user:
                variables["name"] = user.name or ""
                variables["first_name"] = (user.name or "").split()[0] if user.name else ""
                variables["email"] = user.email or ""
                variables["phone"] = user.phone or ""
                variables["external_id"] = user.external_id or ""
                # Flatten user custom properties
                for k, v in (user.properties or {}).items():
                    variables[k] = v
            # Merge enrollment metadata (trigger event props + any extra)
            for k, v in (enrollment.enrollment_metadata or {}).items():
                if not k.startswith("_") and k != "events":  # skip internal keys and events dict
                    variables[k] = v

            # Inject namespaced events dict for {{ events.event_name.key }} syntax
            if enrollment.enrollment_metadata and "events" in enrollment.enrollment_metadata:
                variables["events"] = enrollment.enrollment_metadata["events"]

            # Inject WhatsApp window info into action variables
            try:
                wa_ctx = (enrollment.enrollment_metadata or {}).get("_wa_ctx")
                if wa_ctx:
                    from app.services.whatsapp_window_service import WhatsAppWindowService
                    ws = WhatsAppWindowService(self.db)
                    wa_iid, wa_phone = wa_ctx.get("instance_id"), wa_ctx.get("phone")
                    if wa_iid and wa_phone:
                        is_open = ws.is_window_open(wa_iid, wa_phone)
                        window = ws.get_window(wa_iid, wa_phone)
                        remaining = max(0, int((window.window_expires_at - datetime.utcnow()).total_seconds() / 60)) if window and is_open else 0
                        variables["whatsapp_window"] = {
                            "is_open": is_open,
                            "opened_at": window.window_opens_at.isoformat() if window else None,
                            "expires_at": window.window_expires_at.isoformat() if window else None,
                            "minutes_remaining": remaining,
                        }
            except Exception as e:
                logger.warning(f"Error injecting WhatsApp window vars: {e}")

            # Inject project variables
            try:
                from app.services.project_variable_service import ProjectVariableService
                contact_ctx = {
                    "name": variables.get("name", ""),
                    "first_name": variables.get("first_name", ""),
                    "email": variables.get("email", ""),
                    "phone": variables.get("phone", ""),
                    "external_id": variables.get("external_id", ""),
                }
                for k, v in (user.properties or {}).items() if user else []:
                    contact_ctx[k] = v
                project_variables = ProjectVariableService(self.db).render_project_variables(
                    funnel.project_id, contact_ctx
                )
                variables["project"] = project_variables
                variables["projects"] = project_variables
            except Exception as e:
                logger.warning(f"Error injecting project variables in funnel: {e}")

            # Forward the trigger event's properties so the executor's recipient
            # resolution can read the email/phone from the EVENT payload when the
            # contact row hasn't caught up yet (event arrives before identify
            # persists). This is the same treatment direct event actions get; the
            # funnel path previously passed event_data={} and lost it.
            _emeta = enrollment.enrollment_metadata or {}
            _elast = (_emeta.get("events") or {}).get("_last") or {}
            _eprops = {k: v for k, v in _elast.items() if not str(k).startswith("_")}
            if not _eprops:
                _eprops = {
                    k: v for k, v in _emeta.items()
                    if k != "events" and not str(k).startswith("_")
                }
            context = ActionContext(
                db=self.db,
                project_id=funnel.project_id,
                user_id=enrollment.user_id,
                event_id=None,
                event_data={"properties": _eprops, "event_name": _elast.get("_name")},
                user_data=user_data,
                variables=variables,
                event_action_id=(
                    funnel.event_action_id
                    if getattr(funnel, "source", None) == "event_action"
                    else None
                ),
            )

            executor = ActionExecutor()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    executor.execute(action_type, action_config, context),
                ).result(timeout=30)

            # Executor may have committed (cross-thread), expiring all
            # session attributes.  Refresh to get clean state.
            self.db.refresh(enrollment)

            if result.success:
                if action_type in ("send_template", "send_whatsapp_message"):
                    enrollment.messages_sent = (enrollment.messages_sent or 0) + 1

                # Collect all metadata changes in a single dict to avoid
                # multiple lazy-reload / overwrite issues.
                meta = dict(enrollment.enrollment_metadata or {})
                meta_changed = False

                # Store _wa_ctx for WhatsApp actions so later steps can query window
                if action_type == "send_whatsapp_message" and result.data:
                    wa_phone = result.data.get("phone")
                    wa_instance_id = action_config.get("instance_id")
                    if wa_phone and wa_instance_id:
                        meta["_wa_ctx"] = {"instance_id": wa_instance_id, "phone": wa_phone}
                        meta_changed = True

                # Persist action result to enrollment_metadata when store_result_as is set
                store_as = action_config.get("store_result_as")
                if store_as and result.data:
                    stored_value = result.data.get("result") if result.data.get("result") is not None else result.data
                    meta[store_as] = stored_value
                    meta_changed = True

                if meta_changed:
                    enrollment.enrollment_metadata = meta
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(enrollment, "enrollment_metadata")

                # Log includes structured inputs/outputs/context
                log_data = {
                    "action_type": action_type,
                    "message": result.message,
                    "inputs": {
                        "config": {k: v for k, v in action_config.items() if k != "store_result_as"},
                        "variables": variables,
                    },
                    "outputs": {
                        "success": True,
                        "message": result.message,
                        "data": result.data if result.data else None,
                    },
                }
                context = {}
                if action_type == "send_whatsapp_message" and result.data:
                    context["window_open"] = result.data.get("window_open")
                if context:
                    log_data["context"] = context
                # Keep backward-compat keys
                if result.data:
                    log_data["result"] = result.data
                safe_config = {k: v for k, v in action_config.items() if k != "store_result_as"}
                if safe_config:
                    log_data["config"] = safe_config
                # Keep variables at top level for backward compat
                log_data["variables"] = variables
                self._log(enrollment, step, "action_executed", log_data)
            else:
                self._log(enrollment, step, "action_failed", {
                    "action_type": action_type,
                    "error": result.error,
                    "inputs": {
                        "config": {k: v for k, v in action_config.items() if k != "store_result_as"},
                        "variables": variables,
                    },
                    "outputs": {"success": False, "error": result.error},
                })

        except Exception as e:
            logger.error(f"Action step execution error enrollment={enrollment.id}: {e}")
            self._log(enrollment, step, "action_failed", {"error": str(e)})

    def _handle_condition_step(self, enrollment: FunnelEnrollment, step: FunnelStep) -> None:
        """Evaluate a condition step and branch accordingly."""
        config = step.step_config or {}
        conditions = config.get("conditions", [])
        match_mode = config.get("match_mode", "all")

        # Build context from user data + enrollment metadata
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        user_data = {}
        if user:
            user_data = {
                "email": user.email,
                "phone": user.phone,
                "name": user.name,
                "external_id": user.external_id,
                **(user.properties or {}),
            }

        from app.services.event_actions.conditions import ConditionEvaluator
        evaluator = ConditionEvaluator()
        event_data = {"properties": enrollment.enrollment_metadata or {}}

        # Inject user scores into condition context
        extra_ctx = {}
        try:
            from app.services.scoring.scoring_engine import ScoringEngine
            funnel = enrollment.funnel
            if funnel and enrollment.user_id:
                snapshots = ScoringEngine(self.db).get_user_scores(funnel.project_id, enrollment.user_id)
                score_data = {}
                for s in snapshots:
                    slug = s.score_definition.slug
                    score_data[slug] = {"value": s.score, "tier": s.tier}
                    extra_ctx[f"score.{slug}"] = s.score
                    extra_ctx[f"score.{slug}.tier"] = s.tier
                if score_data:
                    extra_ctx["score"] = score_data
        except Exception as e:
            logger.warning(f"Error loading scores for funnel condition: {e}")

        # Inject WhatsApp window state
        try:
            wa_ctx = (enrollment.enrollment_metadata or {}).get("_wa_ctx")
            if wa_ctx:
                from app.services.whatsapp_window_service import WhatsAppWindowService
                ws = WhatsAppWindowService(self.db)
                wa_iid, wa_phone = wa_ctx.get("instance_id"), wa_ctx.get("phone")
                if wa_iid and wa_phone:
                    is_open = ws.is_window_open(wa_iid, wa_phone)
                    window = ws.get_window(wa_iid, wa_phone)
                    extra_ctx["whatsapp_window.is_open"] = is_open
                    if window and is_open:
                        extra_ctx["whatsapp_window.opened_at"] = window.window_opens_at.isoformat()
                        extra_ctx["whatsapp_window.expires_at"] = window.window_expires_at.isoformat()
                        remaining = (window.window_expires_at - datetime.utcnow()).total_seconds() / 60
                        extra_ctx["whatsapp_window.minutes_remaining"] = max(0, int(remaining))
                    else:
                        extra_ctx["whatsapp_window.is_open"] = False
                        extra_ctx["whatsapp_window.opened_at"] = None
                        extra_ctx["whatsapp_window.expires_at"] = None
                        extra_ctx["whatsapp_window.minutes_remaining"] = 0
        except Exception as e:
            logger.warning(f"Error loading WhatsApp window for funnel condition: {e}")

        # Inject datetime context for datetime.* conditions
        try:
            has_datetime = any(c.get("category") == "datetime" for c in conditions)
            if has_datetime:
                funnel = enrollment.funnel
                tz_name = self._get_project_timezone(funnel.project_id, config, enrollment)
                try:
                    tz = ZoneInfo(tz_name)
                except Exception:
                    tz = dt_timezone.utc
                now = datetime.now(tz)
                for cond in conditions:
                    if cond.get("category") != "datetime":
                        continue
                    field = cond.get("field", "")
                    if field == "datetime.weekly_window":
                        start_wm = int(cond.get("window_start", 0))
                        end_wm = int(cond.get("window_end", 0))
                        now_wm = self._compute_weekly_minute(now)
                        extra_ctx["datetime.weekly_window"] = self._is_in_weekly_window(now_wm, start_wm, end_wm)
                    elif field == "datetime.hour":
                        extra_ctx["datetime.hour"] = now.hour
                    elif field == "datetime.weekday":
                        extra_ctx["datetime.weekday"] = now.weekday()
                    elif field == "datetime.day_of_month":
                        extra_ctx["datetime.day_of_month"] = now.day
        except Exception as e:
            logger.warning(f"Error loading datetime context for funnel condition: {e}")

        # Inject event/channel occurrence flags and counts for event.*/channel.* conditions
        try:
            event_conditions = {}
            for cond in conditions:
                field = cond.get("field", "")
                count_mode = cond.get("count_mode", False)
                since = cond.get("since", "enrollment")

                # Support both event.* and channel.* conditions
                if field.startswith("event."):
                    evt_name = field[len("event."):]
                    event_conditions[evt_name] = {"since": since, "count_mode": count_mode, "prefix": "event"}
                elif field.startswith("channel."):
                    # channel.whatsapp.delivered → event_name = "channel.whatsapp.delivered"
                    evt_name = field
                    instance_filter = cond.get("instance_id")
                    event_conditions[evt_name] = {
                        "since": since, "count_mode": count_mode, "prefix": "channel",
                        "instance_id": instance_filter,
                    }

            if event_conditions:
                funnel = enrollment.funnel
                from sqlalchemy import func as sqla_func
                for evt_name, opts in event_conditions.items():
                    query = self.db.query(MessagingEvent).filter(
                        MessagingEvent.user_id == enrollment.user_id,
                        MessagingEvent.project_id == funnel.project_id,
                        MessagingEvent.event_name == evt_name,
                    )
                    if opts["since"] == "enrollment" and enrollment.enrolled_at:
                        query = query.filter(
                            MessagingEvent.created_at >= enrollment.enrolled_at,
                        )
                    elif opts["since"] == "last_step" and enrollment.entered_step_at:
                        query = query.filter(
                            MessagingEvent.created_at >= enrollment.entered_step_at,
                        )
                    # "ever" = no filter

                    # Instance-aware filtering for channel conditions
                    cond_instance_id = opts.get("instance_id")
                    if cond_instance_id and opts["prefix"] == "channel":
                        query = query.filter(
                            MessagingEvent.properties["instance_id"].as_integer() == int(cond_instance_id),
                        )

                    if opts["count_mode"]:
                        # Count mode: inject numeric count for >=, <=, == etc.
                        count = query.count()
                        if opts["prefix"] == "event":
                            extra_ctx[f"event.{evt_name}"] = count
                        else:
                            extra_ctx[evt_name] = count
                    else:
                        matched_event = query.order_by(MessagingEvent.created_at.desc()).first()
                        if opts["prefix"] == "event":
                            if matched_event:
                                extra_ctx[f"event.{evt_name}"] = True
                                # Namespace event properties into enrollment metadata
                                evt_props = matched_event.properties or {}
                                self._merge_event_into_metadata(enrollment, evt_name, dict(evt_props))
                                meta = dict(enrollment.enrollment_metadata or {})
                                meta[f"_event_{evt_name}"] = True
                                enrollment.enrollment_metadata = meta
                                from sqlalchemy.orm.attributes import flag_modified
                                flag_modified(enrollment, "enrollment_metadata")
                        else:
                            # channel conditions: set True/False
                            extra_ctx[evt_name] = matched_event is not None
        except Exception as e:
            logger.warning(f"Error loading event/channel flags for funnel condition: {e}")

        result = evaluator.evaluate_all(conditions, event_data, user_data, match_mode, extra_context=extra_ctx)

        target_branch = "yes" if result else "no"

        # Find first step in target branch
        branch_step = (
            self.db.query(FunnelStep)
            .filter(
                FunnelStep.funnel_id == enrollment.funnel_id,
                FunnelStep.parent_step_id == step.id,
                FunnelStep.branch == target_branch,
            )
            .order_by(FunnelStep.position)
            .first()
        )

        log_details: dict = {"condition_result": result, "branch": target_branch}
        if hasattr(evaluator, "_last_details"):
            log_details["conditions"] = evaluator._last_details
        # Structured context for debug UI
        log_context = {}
        if extra_ctx.get("score"):
            log_context["scores"] = extra_ctx["score"]
        wa_open = extra_ctx.get("whatsapp_window.is_open")
        if wa_open is not None:
            log_context["whatsapp_window"] = {
                "open": wa_open,
                "expires_at": extra_ctx.get("whatsapp_window.expires_at"),
            }
        event_flags = {k: v for k, v in extra_ctx.items() if k.startswith("event.") or k.startswith("channel.")}
        if event_flags:
            log_context["event_flags"] = event_flags
        if log_context:
            log_details["context"] = log_context
        log_details["evaluated_at"] = datetime.utcnow().isoformat()
        self._log(enrollment, step, "advanced", log_details)

        # Debug email notification on condition evaluation
        debug_email = config.get("debug_notify_email")
        if debug_email:
            try:
                self._send_condition_debug_email(
                    enrollment, step, user_data, log_details, debug_email,
                    config.get("debug_notify_message", ""),
                )
            except Exception as e:
                logger.warning(f"Failed to send condition debug email: {e}")

        if branch_step:
            enrollment.current_step_id = branch_step.id
            enrollment.current_branch = target_branch
            enrollment.entered_step_at = datetime.utcnow()
            self._log(enrollment, branch_step, "entered", {"branch": target_branch})
            self._process_current_step(enrollment, branch_step)
        else:
            # Empty branch — return to main flow after condition step
            self._return_to_main_after(enrollment, step)

    def _send_condition_debug_email(
        self,
        enrollment: FunnelEnrollment,
        step: FunnelStep,
        user_data: dict,
        log_details: dict,
        to_email: str,
        custom_message: str,
    ) -> None:
        """Send a debug notification email when a condition step is evaluated."""
        import re
        from app.services.email_service import EmailService

        funnel = enrollment.funnel
        variables = {
            "enrollment_id": str(enrollment.id),
            "funnel_name": funnel.name if funnel else "N/A",
            "funnel_id": str(enrollment.funnel_id),
            "step_position": str(step.position),
            "user_email": user_data.get("email") or "N/A",
            "user_name": user_data.get("name") or "N/A",
            "user_id": str(enrollment.user_id),
            "condition_result": str(log_details.get("condition_result", False)),
            "evaluated_at": log_details.get("evaluated_at", datetime.utcnow().isoformat()),
        }

        # Render custom message with {{var}} placeholders
        rendered_message = custom_message
        if rendered_message:
            for key, val in variables.items():
                rendered_message = rendered_message.replace("{{" + key + "}}", val)

        # Build conditions detail rows
        cond_rows = ""
        for c in log_details.get("conditions", []):
            cond_rows += (
                f"<tr><td style='padding:4px 8px;border:1px solid #ddd;'>{c.get('field','')}</td>"
                f"<td style='padding:4px 8px;border:1px solid #ddd;'>{c.get('operator','')}</td>"
                f"<td style='padding:4px 8px;border:1px solid #ddd;'>{c.get('expected','')}</td>"
                f"<td style='padding:4px 8px;border:1px solid #ddd;'>{c.get('actual','')}</td>"
                f"<td style='padding:4px 8px;border:1px solid #ddd;'>{c.get('result','')}</td></tr>"
            )

        context_section = ""
        ctx = log_details.get("context", {})
        if ctx:
            import json
            context_section = (
                f"<h3 style='margin:16px 0 8px;'>Extra Context</h3>"
                f"<pre style='background:#f5f5f5;padding:12px;border-radius:4px;font-size:13px;'>"
                f"{json.dumps(ctx, indent=2, default=str)}</pre>"
            )

        html_body = f"""
        <div style="font-family:sans-serif;max-width:640px;margin:0 auto;">
            <h2 style="color:{'#16a34a' if log_details.get('condition_result') else '#b45309'};">Funnel Condition — {'TRUE' if log_details.get('condition_result') else 'FALSE'}</h2>
            {"<p style='background:#fef3c7;padding:12px;border-radius:4px;'>" + rendered_message + "</p>" if rendered_message else ""}
            <table style="border-collapse:collapse;width:100%;margin:16px 0;">
                <tr><td style="padding:4px 8px;font-weight:bold;">Enrollment ID</td><td style="padding:4px 8px;">{variables['enrollment_id']}</td></tr>
                <tr><td style="padding:4px 8px;font-weight:bold;">Funnel</td><td style="padding:4px 8px;">{variables['funnel_name']} (#{variables['funnel_id']})</td></tr>
                <tr><td style="padding:4px 8px;font-weight:bold;">Step Position</td><td style="padding:4px 8px;">{variables['step_position']}</td></tr>
                <tr><td style="padding:4px 8px;font-weight:bold;">Contact</td><td style="padding:4px 8px;">{variables['user_name']} &lt;{variables['user_email']}&gt; (ID: {variables['user_id']})</td></tr>
                <tr><td style="padding:4px 8px;font-weight:bold;">Result</td><td style="padding:4px 8px;color:{'#16a34a' if log_details.get('condition_result') else '#dc2626'};font-weight:bold;">{'TRUE' if log_details.get('condition_result') else 'FALSE'}</td></tr>
                <tr><td style="padding:4px 8px;font-weight:bold;">Evaluated At</td><td style="padding:4px 8px;">{variables['evaluated_at']}</td></tr>
            </table>
            {"<h3 style='margin:16px 0 8px;'>Conditions Detail</h3><table style='border-collapse:collapse;width:100%;'><tr><th style='padding:4px 8px;border:1px solid #ddd;background:#f5f5f5;'>Field</th><th style='padding:4px 8px;border:1px solid #ddd;background:#f5f5f5;'>Operator</th><th style='padding:4px 8px;border:1px solid #ddd;background:#f5f5f5;'>Expected</th><th style='padding:4px 8px;border:1px solid #ddd;background:#f5f5f5;'>Actual</th><th style='padding:4px 8px;border:1px solid #ddd;background:#f5f5f5;'>Result</th></tr>" + cond_rows + "</table>" if cond_rows else ""}
            {context_section}
        </div>
        """

        result_label = "TRUE" if log_details.get("condition_result") else "FALSE"
        subject = f"[Funnel Debug] {variables['funnel_name']} — condition {result_label} (enrollment #{variables['enrollment_id']})"
        EmailService().send_html_email(to_email=to_email, subject=subject, html_body=html_body)

    @staticmethod
    def _resolve_wa_variable_mapping(
        wa_mapping: Dict[str, str],
        variables: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Build Meta template_components from wa_variable_mapping + resolved variables.

        wa_mapping keys are like "body.1", "body.first_name", "header.1".
        Supports both numbered ({{1}}) and named ({{first_name}}) placeholders.
        Values are data paths like "name", "email", "properties.order_id".
        Returns Meta-format components list with positional parameters.
        """
        # Group by component type, preserving insertion order
        grouped: Dict[str, list] = {}
        for slot_key, raw_path in wa_mapping.items():
            if not raw_path:
                continue
            parts = slot_key.split(".", 1)
            if len(parts) != 2:
                continue
            comp_type, var_id = parts
            # Strip {{ }} if user included them in the data path
            data_path = raw_path.strip().removeprefix("{{").removesuffix("}}")

            # Resolve data path from variables (supports dot notation)
            value = variables
            for segment in data_path.split("."):
                if isinstance(value, dict):
                    value = value.get(segment)
                else:
                    value = None
                    break
            # Fallback: try flat lookup
            if value is None:
                value = variables.get(data_path, "")
            resolved = str(value) if value is not None else ""

            # For numbered vars, use the number as sort key; for named, use order
            is_named = not var_id.isdigit()
            try:
                sort_key = int(var_id)
            except ValueError:
                sort_key = len(grouped.get(comp_type, []))
            grouped.setdefault(comp_type, []).append((sort_key, var_id, resolved, is_named))

        components = []
        for comp_type, entries in grouped.items():
            entries.sort(key=lambda x: x[0])
            parameters = []
            for _, var_id, val, is_named in entries:
                param: Dict[str, Any] = {"type": "text", "text": val}
                param["parameter_name"] = var_id  # Always include (aligns with scheduled_send_worker)
                parameters.append(param)
            components.append({"type": comp_type, "parameters": parameters})
        return components

    def _advance_to_next(self, enrollment: FunnelEnrollment, current_step: FunnelStep) -> None:
        """Move enrollment to the next step in sequence."""
        next_step = self._get_next_step(enrollment, current_step)

        if next_step:
            enrollment.current_step_id = next_step.id
            enrollment.current_branch = next_step.branch
            enrollment.entered_step_at = datetime.utcnow()
            self._log(enrollment, next_step, "entered", {})
            self._process_current_step(enrollment, next_step)
        else:
            self._complete_enrollment(enrollment, "completed")

    def _get_next_step(self, enrollment: FunnelEnrollment, current_step: FunnelStep) -> Optional[FunnelStep]:
        """Find the next step after the current one, handling branch exhaustion."""
        branch = current_step.branch
        parent_id = current_step.parent_step_id

        if branch in ("yes", "no") or branch.startswith("exit_") or branch.startswith("path_"):
            # Within a child branch — find next step in same branch
            next_in_branch = (
                self.db.query(FunnelStep)
                .filter(
                    FunnelStep.funnel_id == enrollment.funnel_id,
                    FunnelStep.parent_step_id == parent_id,
                    FunnelStep.branch == branch,
                    FunnelStep.position > current_step.position,
                )
                .order_by(FunnelStep.position)
                .first()
            )
            if next_in_branch:
                return next_in_branch

            # Branch exhausted — return to main after parent step
            parent = self.db.query(FunnelStep).filter(FunnelStep.id == parent_id).first()
            if parent:
                return self._find_main_step_after(enrollment.funnel_id, parent)
            return None

        # Main flow — find next main step
        return self._find_main_step_after(enrollment.funnel_id, current_step)

    def _find_main_step_after(self, funnel_id: int, step: FunnelStep) -> Optional[FunnelStep]:
        """Find next step on the main branch after the given step."""
        return (
            self.db.query(FunnelStep)
            .filter(
                FunnelStep.funnel_id == funnel_id,
                FunnelStep.branch == "main",
                FunnelStep.parent_step_id.is_(None),
                FunnelStep.position > step.position,
            )
            .order_by(FunnelStep.position)
            .first()
        )

    def _return_to_main_after(self, enrollment: FunnelEnrollment, condition_step: FunnelStep) -> None:
        """Return to the main flow after a condition step with an empty branch."""
        next_main = self._find_main_step_after(enrollment.funnel_id, condition_step)
        if next_main:
            enrollment.current_step_id = next_main.id
            enrollment.current_branch = "main"
            enrollment.entered_step_at = datetime.utcnow()
            self._log(enrollment, next_main, "entered", {"returned_from": "condition"})
            self._process_current_step(enrollment, next_main)
        else:
            self._complete_enrollment(enrollment, "completed")

    def _complete_enrollment(self, enrollment: FunnelEnrollment, reason: str) -> None:
        # Guard: don't complete while threads are still active or forked
        active_threads = [t for t in enrollment.threads if t.status in ("active", "forked")]
        if active_threads:
            logger.warning(
                f"Blocked completion of enrollment {enrollment.id}: "
                f"{len(active_threads)} active/forked threads still running"
            )
            return

        self._log(enrollment, enrollment.current_step, "exited", {
            "reason": reason,
            "outputs": {
                "exit_reason": reason,
                "exit_tag_applied": bool((enrollment.funnel.global_exit_config or {}).get("exit_tags", {}).get(reason)) if enrollment.funnel else False,
                "event_emitted": "funnel_completed" if reason == "completed" else "funnel_exited",
            },
        })
        # Derives "completed"/"exited" from reason — this path's existing semantics.
        self._transition_to_exited(enrollment, reason)

    def _log(self, enrollment: FunnelEnrollment, step: Optional[FunnelStep], action: str, details: dict) -> None:
        log = FunnelEnrollmentLog(
            enrollment_id=enrollment.id,
            step_id=step.id if step else None,
            action=action,
            details=details,
        )
        self.db.add(log)
        self.db.flush()

    # ------------------------------------------------------------------
    # Wait Until support
    # ------------------------------------------------------------------

    def _handle_wait_until_step(self, enrollment: FunnelEnrollment, step: FunnelStep, thread=None) -> None:
        """Set up a wait_until step — stores pending conditions in enrollment_metadata.
        Does retroactive check: if event already occurred, resolve immediately."""
        config = step.step_config or {}
        conditions = config.get("conditions", [])
        if not conditions:
            self._advance_to_next(enrollment, step)
            return

        from sqlalchemy.orm.attributes import flag_modified

        meta = dict(enrollment.enrollment_metadata or {})
        wu_key = f"_wait_until_{step.id}"
        meta[wu_key] = {
            "step_id": step.id,
            "resolved_by": None,
            "pending_conditions": [
                {"index": i, "type": c.get("type"), "label": c.get("label", "")}
                for i, c in enumerate(conditions)
            ],
        }
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")

        self._log(enrollment, step, "wait_until_started", {
            "conditions": len(conditions),
            "inputs": {
                "condition_count": len(conditions),
                "condition_types": [c.get("type") for c in conditions],
                "retroactive_check": config.get("retroactive_check", True),
            },
        })

        # Retroactive check — see if any event condition already fired
        retroactive = config.get("retroactive_check", True)
        if retroactive:
            funnel = enrollment.funnel
            for i, cond in enumerate(conditions):
                if cond.get("type") == "event":
                    event_name = cond.get("event_name")
                    if not event_name:
                        continue
                    # Check if this event occurred since funnel enrollment
                    existing = (
                        self.db.query(MessagingEvent)
                        .filter(
                            MessagingEvent.user_id == enrollment.user_id,
                            MessagingEvent.project_id == funnel.project_id,
                            MessagingEvent.event_name == event_name,
                            MessagingEvent.created_at >= enrollment.enrolled_at,
                        )
                        .first()
                    )
                    if existing:
                        self._resolve_wait_until(enrollment, step, i, {
                            "trigger": "retroactive",
                            "event_name": event_name,
                        }, thread=thread)
                        return

    def _resolve_wait_until(self, enrollment: FunnelEnrollment, step: FunnelStep, winning_idx: int, trigger_info: dict, thread=None) -> None:
        """Atomically resolve a wait_until step. CAS on resolved_by being null."""
        from sqlalchemy.orm.attributes import flag_modified

        meta = dict(enrollment.enrollment_metadata or {})
        wu_key = f"_wait_until_{step.id}"
        wu = meta.get(wu_key) or meta.get("_wait_until", {})  # fallback for old enrollments

        # CAS: only resolve if not already resolved
        if wu.get("resolved_by") is not None:
            return

        wu["resolved_by"] = winning_idx
        wu["trigger_info"] = trigger_info
        meta[wu_key] = wu
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")

        config = step.step_config or {}
        conditions = config.get("conditions", [])
        label = conditions[winning_idx].get("label", f"exit_{winning_idx}") if winning_idx < len(conditions) else f"exit_{winning_idx}"

        self._log(enrollment, step, "wait_until_resolved", {
            "winning_exit": winning_idx,
            "label": label,
            **trigger_info,
            "outputs": {
                "resolved_by_index": winning_idx,
                "resolved_label": label,
                "trigger_type": trigger_info.get("trigger"),
            },
        })

        # Find first child step in the winning exit branch
        exit_branch = f"exit_{winning_idx}"
        branch_step = (
            self.db.query(FunnelStep)
            .filter(
                FunnelStep.funnel_id == enrollment.funnel_id,
                FunnelStep.parent_step_id == step.id,
                FunnelStep.branch == exit_branch,
            )
            .order_by(FunnelStep.position)
            .first()
        )

        if branch_step:
            if thread:
                # Thread-scoped: update the thread, not the enrollment
                thread.current_step_id = branch_step.id
                thread.current_branch = exit_branch
                thread.entered_step_at = datetime.utcnow()
                self._log(enrollment, branch_step, "entered", {"branch": exit_branch, "thread_index": thread.thread_index})
                self._process_thread_step(enrollment, thread, branch_step)
            else:
                # Top-level (current behavior)
                enrollment.current_step_id = branch_step.id
                enrollment.current_branch = exit_branch
                enrollment.entered_step_at = datetime.utcnow()
                self._log(enrollment, branch_step, "entered", {"branch": exit_branch})
                self._process_current_step(enrollment, branch_step)
        else:
            if thread:
                # Empty exit branch in thread — advance thread past the wait_until
                self._advance_thread(enrollment, thread, step)
            else:
                # Empty exit branch — return to main after wait_until step
                self._return_to_main_after(enrollment, step)

    def check_wait_until_events(self, db: Session, event) -> int:
        """Called on every event — check if it resolves any wait_until step.

        Handles both top-level wait_until (enrollment.current_step) and
        wait_until inside fork threads (thread.current_step).
        """
        if not event.user_id:
            return 0

        resolved = 0

        # --- 1. Top-level wait_until ---
        enrollments = (
            db.query(FunnelEnrollment)
            .join(FunnelStep, FunnelEnrollment.current_step_id == FunnelStep.id)
            .join(Funnel)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelEnrollment.user_id == event.user_id,
                Funnel.project_id == event.project_id,
                FunnelStep.step_type == "wait_until",
            )
            .all()
        )

        for enrollment in enrollments:
            step = enrollment.current_step
            if not step:
                continue

            meta = enrollment.enrollment_metadata or {}
            wu_key = f"_wait_until_{step.id}"
            wu = meta.get(wu_key) or meta.get("_wait_until", {})
            if wu.get("resolved_by") is not None:
                continue

            config = step.step_config or {}
            conditions = config.get("conditions", [])

            for i, cond in enumerate(conditions):
                if cond.get("type") == "event" and cond.get("event_name") == event.event_name:
                    # Merge event properties into enrollment metadata
                    from sqlalchemy.orm.attributes import flag_modified
                    self._merge_event_into_metadata(enrollment, event.event_name, dict(event.properties or {}))
                    flag_modified(enrollment, "enrollment_metadata")

                    self._resolve_wait_until(enrollment, step, i, {
                        "trigger": "event",
                        "event_name": event.event_name,
                        "event_id": event.id,
                    })
                    resolved += 1
                    break

        # --- 2. Forked wait_until (thread sitting on a wait_until step) ---
        threads = (
            db.query(FunnelEnrollmentThread)
            .join(FunnelStep, FunnelEnrollmentThread.current_step_id == FunnelStep.id)
            .join(FunnelEnrollment, FunnelEnrollmentThread.enrollment_id == FunnelEnrollment.id)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollmentThread.status == "active",
                FunnelStep.step_type == "wait_until",
                FunnelEnrollment.status == "active",
                FunnelEnrollment.user_id == event.user_id,
                Funnel.project_id == event.project_id,
            )
            .all()
        )

        for thread in threads:
            step = db.query(FunnelStep).filter(FunnelStep.id == thread.current_step_id).first()
            if not step:
                continue

            enrollment = thread.enrollment
            meta = enrollment.enrollment_metadata or {}
            wu_key = f"_wait_until_{step.id}"
            wu = meta.get(wu_key) or meta.get("_wait_until", {})
            if wu.get("resolved_by") is not None:
                continue

            config = step.step_config or {}
            conditions = config.get("conditions", [])

            for i, cond in enumerate(conditions):
                if cond.get("type") == "event" and cond.get("event_name") == event.event_name:
                    # Merge event properties into enrollment metadata
                    from sqlalchemy.orm.attributes import flag_modified
                    self._merge_event_into_metadata(enrollment, event.event_name, dict(event.properties or {}))
                    flag_modified(enrollment, "enrollment_metadata")

                    self._resolve_wait_until(enrollment, step, i, {
                        "trigger": "event",
                        "event_name": event.event_name,
                        "event_id": event.id,
                        "thread_index": thread.thread_index,
                    }, thread=thread)
                    resolved += 1

                    # Check fork join after thread resolution
                    fork_step = enrollment.current_step
                    if fork_step and fork_step.step_type == "fork":
                        self._check_fork_join(enrollment, fork_step)

                    break

        if resolved:
            db.commit()
        return resolved

    def process_wait_until_steps(self) -> int:
        """Scheduler: check timeout conditions on wait_until steps.

        Handles both top-level wait_until (enrollment.current_step) and
        wait_until inside fork threads (thread.current_step).
        """
        now = datetime.utcnow()
        processed = 0

        # --- 1. Top-level wait_until (enrollment directly on a wait_until step) ---
        enrollments = (
            self.db.query(FunnelEnrollment)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .join(FunnelStep, FunnelEnrollment.current_step_id == FunnelStep.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelStep.step_type == "wait_until",
                self._contact_not_paused(),
            )
            .all()
        )

        for enrollment in enrollments:
            step = enrollment.current_step
            if not step:
                continue

            meta = enrollment.enrollment_metadata or {}
            wu_key = f"_wait_until_{step.id}"
            wu = meta.get(wu_key) or meta.get("_wait_until", {})
            if wu.get("resolved_by") is not None:
                continue

            config = step.step_config or {}
            conditions = config.get("conditions", [])

            for i, cond in enumerate(conditions):
                if cond.get("type") != "timeout":
                    continue
                duration = cond.get("duration", 24)
                unit = cond.get("unit", "hours")
                if unit == "minutes":
                    delta = timedelta(minutes=duration)
                elif unit == "days":
                    delta = timedelta(days=duration)
                else:
                    delta = timedelta(hours=duration)

                if now >= enrollment.entered_step_at + delta:
                    try:
                        self._resolve_wait_until(enrollment, step, i, {
                            "trigger": "timeout",
                            "duration": duration,
                            "unit": unit,
                        })
                        processed += 1
                    except Exception as e:
                        logger.error(f"Error resolving wait_until timeout enrollment={enrollment.id}: {e}")
                    break  # Only one condition can win

        # --- 2. Forked wait_until (thread sitting on a wait_until step) ---
        threads = (
            self.db.query(FunnelEnrollmentThread)
            .join(FunnelStep, FunnelEnrollmentThread.current_step_id == FunnelStep.id)
            .join(FunnelEnrollment, FunnelEnrollmentThread.enrollment_id == FunnelEnrollment.id)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelEnrollmentThread.status == "active",
                FunnelStep.step_type == "wait_until",
                self._contact_not_paused(),
            )
            .all()
        )

        for thread in threads:
            step = self.db.query(FunnelStep).filter(FunnelStep.id == thread.current_step_id).first()
            if not step:
                continue

            enrollment = thread.enrollment
            if not enrollment or enrollment.status != "active":
                continue

            meta = enrollment.enrollment_metadata or {}
            wu_key = f"_wait_until_{step.id}"
            wu = meta.get(wu_key) or meta.get("_wait_until", {})
            if wu.get("resolved_by") is not None:
                continue

            config = step.step_config or {}
            conditions = config.get("conditions", [])

            for i, cond in enumerate(conditions):
                if cond.get("type") != "timeout":
                    continue
                duration = cond.get("duration", 24)
                unit = cond.get("unit", "hours")
                if unit == "minutes":
                    delta = timedelta(minutes=duration)
                elif unit == "days":
                    delta = timedelta(days=duration)
                else:
                    delta = timedelta(hours=duration)

                if now >= thread.entered_step_at + delta:
                    try:
                        self._resolve_wait_until(enrollment, step, i, {
                            "trigger": "timeout",
                            "duration": duration,
                            "unit": unit,
                            "thread_index": thread.thread_index,
                        }, thread=thread)
                        processed += 1

                        # Check fork join after thread resolution
                        fork_step = enrollment.current_step
                        if fork_step and fork_step.step_type == "fork":
                            self._check_fork_join(enrollment, fork_step)
                    except Exception as e:
                        logger.error(f"Error resolving forked wait_until timeout thread={thread.id}: {e}")
                    break

        if processed:
            self.db.commit()
        return processed

    # ------------------------------------------------------------------
    # Fork support
    # ------------------------------------------------------------------

    def _handle_fork_step(self, enrollment: FunnelEnrollment, step: FunnelStep) -> None:
        """Create threads for each fork path and start processing them."""
        config = step.step_config or {}
        paths = config.get("paths", [])
        if len(paths) < 2:
            logger.warning(f"Fork step {step.id} has fewer than 2 paths, skipping")
            self._advance_to_next(enrollment, step)
            return

        num_paths = len(paths)
        enrollment.thread_count = num_paths

        self._log(enrollment, step, "fork_started", {
            "paths": num_paths,
            "inputs": {
                "thread_details": [
                    {"index": i, "label": paths[i].get("label", f"Path {i+1}"), "has_steps": True}
                    for i in range(num_paths)
                ],
            },
        })

        # Pass 1: create ALL threads before processing any of them. Processing a
        # thread can complete it synchronously (fire-and-forget send_message,
        # action, exit, empty path), which triggers _check_fork_join. If sibling
        # threads don't exist yet, the fork would join — and the enrollment could
        # complete — prematurely.
        created_threads: List[tuple] = []  # (thread, first_step, index, branch_key)
        for i in range(num_paths):
            path_label = paths[i].get("label", f"Path {i + 1}")
            branch_key = f"path_{i}"

            # Find first step in this path branch
            first_step = (
                self.db.query(FunnelStep)
                .filter(
                    FunnelStep.funnel_id == enrollment.funnel_id,
                    FunnelStep.parent_step_id == step.id,
                    FunnelStep.branch == branch_key,
                )
                .order_by(FunnelStep.position)
                .first()
            )

            thread = FunnelEnrollmentThread(
                enrollment_id=enrollment.id,
                thread_index=i,
                fork_step_id=step.id,
                parent_thread_id=None,
                label=path_label,
                status="active",
                current_step_id=first_step.id if first_step else None,
                current_branch=branch_key,
                entered_step_at=datetime.utcnow(),
            )
            self.db.add(thread)
            created_threads.append((thread, first_step, i, branch_key))

        self.db.flush()
        # Drop the stale cached collection so _check_fork_join sees every thread.
        self.db.expire(enrollment, ["threads"])

        # Pass 2: process the first step of each thread now that all siblings exist.
        for thread, first_step, i, branch_key in created_threads:
            if first_step:
                self._log(enrollment, first_step, "entered", {"thread_index": i, "branch": branch_key})
                self._process_thread_step(enrollment, thread, first_step)
            else:
                # Empty path — mark complete immediately
                thread.status = "completed"

        # Pass 3: join only if every path finished synchronously (all empty or all
        # fire-and-forget). No-op while any thread is still active.
        self._check_fork_join(enrollment, step)

    def _process_thread_step(self, enrollment: FunnelEnrollment, thread: FunnelEnrollmentThread, step: FunnelStep) -> None:
        """Process a step within a fork thread. Like _process_current_step but scoped to a thread."""
        if enrollment.funnel and enrollment.funnel.status != "active":
            logger.info(
                "Skipping thread step processing for enrollment %s because parent funnel %s is %s",
                enrollment.id,
                enrollment.funnel_id,
                enrollment.funnel.status,
            )
            return
        if not self._allow_enrollment_progress(enrollment):
            return

        if thread.status != "active":
            return

        if step.step_type == "action":
            deferred = self._execute_action_step(enrollment, step)
            if not deferred:
                self._advance_thread(enrollment, thread, step)
        elif step.step_type == "exit":
            thread.status = "exited"
            self._log(enrollment, step, "thread_exited", {"thread_index": thread.thread_index})
            # Trigger fork join check
            if thread.fork_step_id:
                fork_step = self.db.query(FunnelStep).filter(FunnelStep.id == thread.fork_step_id).first()
                if fork_step:
                    self._check_fork_join(enrollment, fork_step)
        elif step.step_type == "fork":
            # Nested fork inside a thread — spawn sub-threads
            self._handle_nested_fork(enrollment, thread, step)
        elif step.step_type == "condition":
            self._handle_thread_condition(enrollment, thread, step)
        elif step.step_type == "wait":
            # Scheduler will pick it up via process_forked_wait_steps
            pass
        elif step.step_type == "wait_until":
            self._handle_wait_until_step(enrollment, step, thread=thread)
        elif step.step_type == "send_message":
            self._handle_send_message_step(enrollment, step, thread=thread)
        elif step.step_type == "wait_for_reply":
            self._setup_wait_for_reply(enrollment, step, thread=thread)

    def _handle_nested_fork(self, enrollment: FunnelEnrollment, parent_thread: FunnelEnrollmentThread, fork_step: FunnelStep) -> None:
        """Handle a fork step inside a thread path — spawn sub-threads, pause parent."""
        config = fork_step.step_config or {}
        paths = config.get("paths", [])
        if len(paths) < 2:
            logger.warning(f"Nested fork step {fork_step.id} has fewer than 2 paths, skipping")
            self._advance_thread(enrollment, parent_thread, fork_step)
            return

        # Pause the parent thread while nested fork runs
        parent_thread.status = "forked"

        # Compute next thread_index as max existing + 1
        max_idx = max((t.thread_index for t in enrollment.threads), default=-1)

        self._log(enrollment, fork_step, "fork_started", {
            "paths": len(paths),
            "nested": True,
            "parent_thread_index": parent_thread.thread_index,
        })

        # Pass 1: create all sub-threads before processing any. A thread that
        # completes synchronously must not join the fork while sibling threads
        # are still being created.
        created_threads: List[tuple] = []  # (thread, first_step, index, branch_key)
        for i, path_def in enumerate(paths):
            path_label = path_def.get("label", f"Path {i + 1}")
            branch_key = f"path_{i}"
            thread_idx = max_idx + 1 + i

            first_step = (
                self.db.query(FunnelStep)
                .filter(
                    FunnelStep.funnel_id == enrollment.funnel_id,
                    FunnelStep.parent_step_id == fork_step.id,
                    FunnelStep.branch == branch_key,
                )
                .order_by(FunnelStep.position)
                .first()
            )

            thread = FunnelEnrollmentThread(
                enrollment_id=enrollment.id,
                thread_index=thread_idx,
                fork_step_id=fork_step.id,
                parent_thread_id=parent_thread.id,
                label=path_label,
                status="active",
                current_step_id=first_step.id if first_step else None,
                current_branch=branch_key,
                entered_step_at=datetime.utcnow(),
            )
            self.db.add(thread)
            created_threads.append((thread, first_step, thread_idx, branch_key))

        self.db.flush()
        # Drop the stale cached collection so _check_fork_join sees every thread.
        self.db.expire(enrollment, ["threads"])

        # Pass 2: process the first step of each sub-thread.
        for thread, first_step, thread_idx, branch_key in created_threads:
            if first_step:
                self._log(enrollment, first_step, "entered", {"thread_index": thread_idx, "branch": branch_key})
                self._process_thread_step(enrollment, thread, first_step)
            else:
                thread.status = "completed"

        # Pass 3: check if all paths finished instantly
        self._check_fork_join(enrollment, fork_step)

    def _handle_thread_condition(self, enrollment: FunnelEnrollment, thread: FunnelEnrollmentThread, step: FunnelStep) -> None:
        """Evaluate condition within a thread and branch accordingly."""
        config = step.step_config or {}
        conditions = config.get("conditions", [])
        match_mode = config.get("match_mode", "all")

        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        user_data = {}
        if user:
            user_data = {
                "email": user.email,
                "phone": user.phone,
                "name": user.name,
                "external_id": user.external_id,
                **(user.properties or {}),
            }

        from app.services.event_actions.conditions import ConditionEvaluator
        evaluator = ConditionEvaluator()
        event_data = {"properties": enrollment.enrollment_metadata or {}}
        result = evaluator.evaluate_all(conditions, event_data, user_data, match_mode)

        target_branch = "yes" if result else "no"
        branch_step = (
            self.db.query(FunnelStep)
            .filter(
                FunnelStep.funnel_id == enrollment.funnel_id,
                FunnelStep.parent_step_id == step.id,
                FunnelStep.branch == target_branch,
            )
            .order_by(FunnelStep.position)
            .first()
        )

        self._log(enrollment, step, "advanced", {"condition_result": result, "branch": target_branch, "thread_index": thread.thread_index})

        if branch_step:
            thread.current_step_id = branch_step.id
            thread.current_branch = target_branch
            thread.entered_step_at = datetime.utcnow()
            self._process_thread_step(enrollment, thread, branch_step)
        else:
            # Empty branch — advance thread past the condition
            self._advance_thread(enrollment, thread, step)

    def _advance_thread(self, enrollment: FunnelEnrollment, thread: FunnelEnrollmentThread, current_step: FunnelStep) -> None:
        """Move a thread to the next step, unwinding nested branches as needed."""
        next_step = self._find_next_thread_step(enrollment, current_step)

        if next_step:
            thread.current_step_id = next_step.id
            thread.current_branch = next_step.branch
            thread.entered_step_at = datetime.utcnow()
            self._log(enrollment, next_step, "entered", {"thread_index": thread.thread_index})
            self._process_thread_step(enrollment, thread, next_step)
        else:
            # Path exhausted — thread complete
            thread.status = "completed"
            self._log(enrollment, current_step, "thread_completed", {"thread_index": thread.thread_index})
            # Trigger fork join check
            if thread.fork_step_id:
                fork_step = self.db.query(FunnelStep).filter(FunnelStep.id == thread.fork_step_id).first()
                if fork_step:
                    self._check_fork_join(enrollment, fork_step)

    def _find_next_thread_step(self, enrollment: FunnelEnrollment, current_step: FunnelStep) -> Optional[FunnelStep]:
        """Find the next step for a thread, unwinding nested branches (exit_*, yes, no) until reaching the fork path level."""
        step = current_step
        while step:
            branch = step.branch
            parent_id = step.parent_step_id

            if branch in ("yes", "no") or (branch and branch.startswith("exit_")):
                # Inside a nested branch (condition yes/no or wait_until exit_N)
                # Look for next sibling in same branch
                next_sibling = (
                    self.db.query(FunnelStep).filter(
                        FunnelStep.funnel_id == enrollment.funnel_id,
                        FunnelStep.parent_step_id == parent_id,
                        FunnelStep.branch == branch,
                        FunnelStep.position > step.position,
                    ).order_by(FunnelStep.position).first()
                )
                if next_sibling:
                    return next_sibling
                # Branch exhausted — unwind to parent and try again
                step = self.db.query(FunnelStep).filter(FunnelStep.id == parent_id).first()
                continue

            elif branch and branch.startswith("path_"):
                # At fork path level — find next step in this path
                return self._find_next_in_path(enrollment.funnel_id, step, branch)

            else:
                return None

        return None

    def _find_next_in_path(self, funnel_id: int, current_step: FunnelStep, path_branch: str) -> Optional[FunnelStep]:
        """Find the next step within a fork path branch after the given step."""
        # Look for next sibling in the same branch
        parent_id = current_step.parent_step_id
        return (
            self.db.query(FunnelStep)
            .filter(
                FunnelStep.funnel_id == funnel_id,
                FunnelStep.parent_step_id == parent_id,
                FunnelStep.branch == path_branch,
                FunnelStep.position > current_step.position,
            )
            .order_by(FunnelStep.position)
            .first()
        )

    def _check_fork_join(self, enrollment: FunnelEnrollment, fork_step: FunnelStep = None) -> None:
        """If all threads for a specific fork are completed/exited, join and continue."""
        if not fork_step:
            return

        # Only check threads belonging to THIS fork level
        fork_threads = [t for t in enrollment.threads if t.fork_step_id == fork_step.id]
        if not fork_threads:
            return

        # Guard: still running — at least one thread for this fork is active.
        active = [t for t in fork_threads if t.status == "active"]
        if active:
            return

        # Idempotency guard: this fork already joined once. _check_fork_join is
        # called from many places (including Pass 3 of fork setup), so a fork
        # whose threads all finished synchronously would otherwise re-join and
        # re-log, advancing the enrollment twice.
        already_joined = (
            self.db.query(FunnelEnrollmentLog.id)
            .filter(
                FunnelEnrollmentLog.enrollment_id == enrollment.id,
                FunnelEnrollmentLog.step_id == fork_step.id,
                FunnelEnrollmentLog.action == "fork_joined",
            )
            .first()
        )
        if already_joined:
            return

        self._log(enrollment, fork_step, "fork_joined", {
            "fork_step_id": fork_step.id,
            "threads_completed": len([t for t in fork_threads if t.status == "completed"]),
            "threads_exited": len([t for t in fork_threads if t.status == "exited"]),
            "outputs": {
                "thread_results": [
                    {"index": t.thread_index, "status": t.status, "label": t.label}
                    for t in fork_threads
                ],
            },
        })

        # Check if this is a nested fork (threads have a parent_thread)
        sample_thread = fork_threads[0]
        if sample_thread.parent_thread_id:
            # Nested fork join — re-activate the parent thread and advance it past the fork step
            parent_thread = next(
                (t for t in enrollment.threads if t.id == sample_thread.parent_thread_id),
                None,
            )
            if parent_thread and parent_thread.status == "forked":
                parent_thread.status = "active"
                self._advance_thread(enrollment, parent_thread, fork_step)
            return

        # Top-level fork join — un-fork and continue main flow
        enrollment.thread_count = None

        next_main = self._find_main_step_after(enrollment.funnel_id, fork_step)
        if next_main:
            enrollment.current_step_id = next_main.id
            enrollment.current_branch = "main"
            enrollment.entered_step_at = datetime.utcnow()
            self._log(enrollment, next_main, "entered", {"returned_from": "fork"})
            self._process_current_step(enrollment, next_main)
        else:
            self._complete_enrollment(enrollment, "completed")

    def process_forked_wait_steps(self) -> int:
        """Scheduler: advance threads on wait steps whose duration elapsed."""
        now = datetime.utcnow()
        threads = (
            self.db.query(FunnelEnrollmentThread)
            .join(FunnelStep, FunnelEnrollmentThread.current_step_id == FunnelStep.id)
            .join(FunnelEnrollment, FunnelEnrollmentThread.enrollment_id == FunnelEnrollment.id)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .join(MessagingUser, FunnelEnrollment.user_id == MessagingUser.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelEnrollmentThread.status == "active",
                FunnelStep.step_type == "wait",
                MessagingUser.is_sandbox == False,
                self._contact_not_paused(),
            )
            .all()
        )

        processed = 0
        for thread in threads:
            step = thread.current_step
            if not step:
                continue

            config = step.step_config or {}
            duration = config.get("duration", 1)
            unit = config.get("unit", "hours")
            if unit == "minutes":
                delta = timedelta(minutes=duration)
            elif unit == "days":
                delta = timedelta(days=duration)
            else:
                delta = timedelta(hours=duration)

            if now >= thread.entered_step_at + delta:
                try:
                    enrollment = thread.enrollment
                    # _advance_thread now triggers _check_fork_join internally
                    # via thread.fork_step_id — no need for manual fork lookup
                    self._advance_thread(enrollment, thread, step)
                    processed += 1
                except Exception as e:
                    logger.error(f"Error advancing forked wait thread {thread.id}: {e}")

        if processed:
            self.db.commit()
        return processed

    # ------------------------------------------------------------------
    # send_message composite step
    # ------------------------------------------------------------------

    def _handle_send_message_step(self, enrollment: FunnelEnrollment, step: FunnelStep, thread=None) -> None:
        """Handle a send_message composite step.

        Two modes:
        - conversation / expect_reply=true (default): Send template + set routing state for handoff + wait
        - notification / expect_reply=false: Fire-and-forget message, then advance immediately

        Backward compat: if 'channel' missing → 'whatsapp'. If 'expect_reply' missing → derive from 'mode'.
        """
        config = step.step_config or {}

        # Backward compat: normalize new fields from legacy config
        channel = config.get("channel", "whatsapp")
        expect_reply = config.get("expect_reply")
        if expect_reply is None:
            mode = config.get("mode", "conversation")
            expect_reply = mode != "notification"
        handoff_type = config.get("handoff_type", "chatbot")

        # Verify channel is enabled for this project
        from app.services.channels.channel_registry_service import ChannelRegistryService
        reg = ChannelRegistryService(self.db)
        if not reg.is_channel_available(enrollment.funnel.project_id, channel):
            logger.warning(f"Funnel {enrollment.funnel_id} step {step.id}: channel '{channel}' not available, skipping")
            self._notification_advance(enrollment, step, thread)
            return

        # Suppression check — defer instead of discard
        # _bypass_suppression is set by retry_deferred_step() for manual retries
        if not config.get("_bypass_suppression"):
            skip_suppression = config.get("skip_suppression", False)
            if skip_suppression:
                # Time-sensitive: bypass suppression but respect urgent cooldown
                if not self._check_urgent_cooldown(enrollment):
                    self._defer_urgent_send(enrollment, step)
                    return
                # Otherwise: proceed to send immediately
            elif self._check_suppression(enrollment):
                self._defer_suppressed_send(enrollment, step)
                return

        # Sandbox interception — intercept send_message before real dispatch
        if self._is_sandbox_contact(enrollment.user_id):
            self._intercept_sandbox_action(
                enrollment, step, "send_message", config,
            )
            self._log(enrollment, step, "send_message_intercepted", {"sandbox": True, "channel": channel})
            # For notification mode, advance immediately; for conversation, stay paused
            if not expect_reply or handoff_type == "none":
                self._notification_advance(enrollment, step, thread)
            return

        if not expect_reply or handoff_type == "none":
            self._handle_send_message_notification(enrollment, step, thread=thread)
            return

        # ── Channel dispatch ────────────────────────────────────────
        if channel == "email":
            self._handle_send_message_email(enrollment, step, thread=thread)
            return

        if channel == "sms":
            self._handle_sms_notification(enrollment, step, thread=thread)
            return

        # ── Debug mode: intercept outbound for approval ────────────
        if enrollment.funnel.debug_mode:
            try:
                preview_text, enriched_config = self._render_whatsapp_for_approval(enrollment, step, config)
                self._create_approval_request(
                    enrollment, step, preview_text,
                    send_config=enriched_config, thread=thread,
                )
            except Exception as e:
                logger.error(f"Error creating approval for enrollment {enrollment.id}: {e}", exc_info=True)
                self._log(enrollment, step, "approval_error", {"error": str(e)})
            return

        # ── WhatsApp conversation mode (original behaviour) ────────
        from sqlalchemy.orm.attributes import flag_modified

        instance_id = config.get("instance_id")
        template_name = config.get("template_name")
        template_language = config.get("template_language")  # i18n: derived from contact locale if unset
        template_components = config.get("template_components")
        recipient_field = config.get("recipient_field", "phone")
        handoff_type = config.get("handoff_type", "chatbot")  # chatbot | agent_team | human
        handoff_id = config.get("handoff_id")
        timeout_hours = config.get("reply_timeout_hours", config.get("timeout_hours", 24))
        return_action = config.get("return_action", "resume")

        if not instance_id:
            self._log(enrollment, step, "send_message_skipped", {
                "reason": "missing instance_id",
            })
            # Treat as failed — go to timeout branch (exit_1)
            self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
            return

        # Load the WhatsApp instance
        from app.models import WhatsAppInstance
        instance = self.db.query(WhatsAppInstance).filter(
            WhatsAppInstance.id == instance_id,
            WhatsAppInstance.is_active == True,
        ).first()
        if not instance:
            self._log(enrollment, step, "send_message_failed", {
                "error": f"WhatsApp instance {instance_id} not found or inactive",
            })
            self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
            return

        # Get user phone number
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        if not user:
            self._log(enrollment, step, "send_message_failed", {"error": "user not found"})
            self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
            return

        # i18n: resolve the contact's locale, then (a) default the template language
        # from it, (b) route to the per-locale sender instance when one is mapped.
        from app.models import Project
        from app.services.messaging.locale_resolver import contact_locale_tz, to_meta_language
        from app.services.messaging.channel_router import resolve_send_instance
        _project = self.db.query(Project).filter(Project.id == user.project_id).first()
        _loc, _ = contact_locale_tz(_project, contact=user) if _project else (None, None)
        if not template_language:
            template_language = to_meta_language(_loc) if _loc else "en_US"
        routed_id = resolve_send_instance(self.db, user.project_id, _loc, "whatsapp", instance_id)
        if routed_id and routed_id != instance.id:
            routed = self.db.query(WhatsAppInstance).filter(
                WhatsAppInstance.id == routed_id, WhatsAppInstance.is_active == True,
            ).first()
            if routed:
                instance = routed

        # Resolve recipient: look up the field from user model, user properties, or enrollment metadata
        phone = (
            getattr(user, recipient_field, None)
            or (user.properties or {}).get(recipient_field)
            or (enrollment.enrollment_metadata or {}).get(recipient_field)
        )
        if not phone:
            self._log(enrollment, step, "send_message_failed", {
                "error": f"User has no value for recipient_field '{recipient_field}'",
            })
            self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
            return

        # Check window state (for logging + handoff-only guard)
        from app.services.whatsapp_sender import WhatsAppSender
        sender = WhatsAppSender(self.db)
        window_open = sender._check_window(instance, phone)

        if not template_name:
            # Handoff-only mode: if Meta + window closed → can't proceed
            if window_open is False:
                self._log(enrollment, step, "send_message_failed", {
                    "error": "Handoff-only requires open 24h window (Meta Cloud API)",
                    "window_open": False,
                })
                self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
                return
            self._log(enrollment, step, "whatsapp_handoff_only", {
                "phone": phone, "window_open": window_open,
                "reason": "no template — handoff only",
            })

        # Resolve wa_variable_mapping into template_components if not already set
        if not template_components and config.get("wa_variable_mapping"):
            wa_vars: Dict[str, Any] = {}
            if user:
                if user.name:
                    wa_vars["name"] = user.name
                    wa_vars["first_name"] = user.name.split()[0]
                if user.email:
                    wa_vars["email"] = user.email
                if user.phone:
                    wa_vars["phone"] = user.phone
                if user.external_id:
                    wa_vars["external_id"] = user.external_id
                wa_vars["properties"] = user.properties or {}
                for k, v in (user.properties or {}).items():
                    wa_vars[k] = v
            for k, v in (enrollment.enrollment_metadata or {}).items():
                if not k.startswith("_") and k != "events":
                    wa_vars[k] = v
            # Inject project variables
            try:
                from app.services.project_variable_service import ProjectVariableService
                project_variables = ProjectVariableService(self.db).render_project_variables(
                    enrollment.funnel.project_id, wa_vars,
                )
                wa_vars["project"] = project_variables
                wa_vars["projects"] = project_variables
            except Exception as e:
                logger.warning(f"Error injecting project vars for WA mapping: {e}")
            template_components = self._resolve_wa_variable_mapping(
                config["wa_variable_mapping"], wa_vars,
            )

        # 1. Send the WhatsApp template message (if template specified).
        # All delivery now goes through SendService so the funnel participates
        # in the same source contract, Candidate/Selection, Guardian, ledger
        # and audit path as every other outbound source.
        template_sent = False
        if template_name:
            try:
                import asyncio
                import concurrent.futures
                from app.services.channels.base import OutboundContent
                from app.services.channels.send_service import SendService

                send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
                content = OutboundContent(
                    content_type="template",
                    template_name=template_name,
                    template_language=template_language,
                    template_components=template_components,
                )
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        SendService(self.db).send(
                            project_id=funnel.project_id,
                            user_id=enrollment.user_id,
                            recipient=phone,
                            content=content,
                            channel="whatsapp",
                            source_type=send_source_type,
                            source_id=send_source_id,
                            slot_id=getattr(step, "slot_id", None),
                            instance_config={"instance_id": instance.id},
                        ),
                    ).result(timeout=30)

                if result.success:
                    template_sent = True
                    self._log(enrollment, step, "whatsapp_template_sent", {
                        "template_name": template_name,
                        "phone": phone,
                        "provider": instance.provider_type,
                        "window_open": window_open,
                        "send_log_id": result.send_log_id,
                    })
                else:
                    self._log(enrollment, step, "whatsapp_template_failed", {
                        "error": result.error or "unknown",
                        "template_name": template_name,
                        "window_open": window_open,
                    })
                    # Template failed — resolve as timeout immediately
                    self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
                    return
            except Exception as e:
                logger.error(f"Error sending WhatsApp template for enrollment {enrollment.id}: {e}")
                self._log(enrollment, step, "whatsapp_template_failed", {"error": str(e)})
                self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
                return

        # 2. Set routing state to delegate inbound replies
        # For WhatsApp, use phone as identifier (must match inbound contact_identifier from webhook)
        channel = "whatsapp"
        funnel = enrollment.funnel
        # Use pre-normalized phone_e164 when available, fall back to inline normalization
        identifier = user.phone_e164 or PhoneNormalizer.normalize(
            str(phone), fallback_country_code=instance.default_country_code
        )[0] or ""

        try:
            from app.services.inbound.routing_state_service import RoutingStateService
            expires_at = datetime.utcnow() + timedelta(hours=timeout_hours)
            state_svc = RoutingStateService(self.db)
            state_svc.set_state(
                project_id=funnel.project_id,
                identifier=identifier,
                channel=channel,
                handler_type="funnel_send",
                handler_id=enrollment.id,
                enrollment_id=enrollment.id,
                expires_at=expires_at,
                metadata={
                    "step_id": step.id,
                    "handoff_type": handoff_type,
                    "handoff_id": handoff_id,
                    "instance_id": instance_id,
                    "thread_index": thread.thread_index if thread else None,
                    "context_message": config.get("context_message"),
                    "return_action": return_action,
                },
            )
        except Exception as e:
            logger.error(f"Error setting routing state for send_message: {e}")
            self._log(enrollment, step, "send_message_routing_error", {"error": str(e)})

        # 3. Store tracking metadata in enrollment_metadata
        meta = dict(enrollment.enrollment_metadata or {})
        ho_key = f"_whatsapp_handoff_{step.id}"
        meta[ho_key] = {
            "instance_id": instance_id,
            "handoff_type": handoff_type,
            "handoff_id": handoff_id,
            "started_at": datetime.utcnow().isoformat(),
            "timeout_hours": timeout_hours,
            "identifier": identifier,
            "channel": channel,
            "template_sent": template_sent,
        }
        meta["_wa_ctx"] = {"instance_id": instance_id, "phone": phone}
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")

        self._log(enrollment, step, "whatsapp_handoff_started", {
            "handoff_type": handoff_type,
            "handoff_id": handoff_id,
            "timeout_hours": timeout_hours,
            "template_name": template_name,
            "thread_index": thread.thread_index if thread else None,
        })

    def _handle_send_message_notification(self, enrollment: FunnelEnrollment, step: FunnelStep, thread=None) -> None:
        """Fire-and-forget message (notification mode).

        Sends either a free-form text or a template message, then immediately
        advances the enrollment to the next step — no handoff, no routing state.
        """
        from sqlalchemy.orm.attributes import flag_modified

        config = step.step_config or {}
        channel = config.get("channel", "whatsapp")

        # Email notification dispatch
        if channel == "email":
            self._handle_email_notification(enrollment, step, thread=thread)
            return

        # SMS notification dispatch
        if channel == "sms":
            self._handle_sms_notification(enrollment, step, thread=thread)
            return

        # Debug mode: intercept notification sends too
        if enrollment.funnel.debug_mode:
            try:
                preview_text, enriched_config = self._render_whatsapp_for_approval(enrollment, step, config)
                self._create_approval_request(
                    enrollment, step, preview_text,
                    send_config=enriched_config, thread=thread,
                )
            except Exception as e:
                logger.error(f"Error creating notification approval for enrollment {enrollment.id}: {e}", exc_info=True)
                self._log(enrollment, step, "approval_error", {"error": str(e)})
            return

        import os as _os
        instance_id = config.get("instance_id")
        message_type = config.get("message_type", "text")
        message_text = config.get("message", "")
        template_name = config.get("template_name", "")
        template_language = config.get("template_language")  # i18n: derived from contact locale if unset
        recipient_field = config.get("recipient_field", "phone")

        if not instance_id:
            self._log(enrollment, step, "send_message_notification_skipped", {"reason": "missing instance_id"})
            self._notification_advance(enrollment, step, thread)
            return

        # Load instance
        from app.models import WhatsAppInstance
        instance = self.db.query(WhatsAppInstance).filter(
            WhatsAppInstance.id == instance_id,
            WhatsAppInstance.is_active == True,
        ).first()
        if not instance:
            self._log(enrollment, step, "send_message_notification_failed", {
                "error": f"WhatsApp instance {instance_id} not found or inactive",
            })
            self._notification_advance(enrollment, step, thread)
            return

        # Get user phone
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        if not user:
            self._log(enrollment, step, "send_message_notification_failed", {"error": "user not found"})
            self._notification_advance(enrollment, step, thread)
            return

        # i18n: derive template language from contact locale when not pinned by author
        if not template_language:
            from app.models import Project
            from app.services.messaging.locale_resolver import contact_locale_tz, to_meta_language
            _project = self.db.query(Project).filter(Project.id == user.project_id).first()
            _loc, _ = contact_locale_tz(_project, contact=user) if _project else (None, None)
            template_language = to_meta_language(_loc) if _loc else "en_US"

        phone = (
            getattr(user, recipient_field, None)
            or (user.properties or {}).get(recipient_field)
            or (enrollment.enrollment_metadata or {}).get(recipient_field)
        )
        if not phone:
            self._log(enrollment, step, "send_message_notification_failed", {
                "error": f"User has no value for recipient_field '{recipient_field}'",
            })
            self._notification_advance(enrollment, step, thread)
            return

        # Policy check
        try:
            from app.services.scoring.policy_service import PolicyService
            funnel = enrollment.funnel
            send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
            decision = PolicyService(self.db).check_can_contact(
                project_id=funnel.project_id,
                user_id=enrollment.user_id,
                channel=channel,
                source=send_source_type,
                source_id=send_source_id,
            )
            if not decision.allowed:
                from app.services.channels.deferred_send_helper import DeferredSendHelper
                helper = DeferredSendHelper(self.db)
                if helper.should_defer(funnel.project_id, decision):
                    step_cfg = step.step_config or {}
                    log = helper.create_deferred_send(
                        project_id=funnel.project_id,
                        user_id=enrollment.user_id,
                        channel="whatsapp",
                        recipient=phone,
                        decision=decision,
                        source_type=send_source_type,
                        source_id=send_source_id,
                        render_context={
                            "action_type": "send_message_notification",
                            "config": step_cfg,
                            "enrollment_id": enrollment.id,
                            "step_id": step.id,
                            "instance_id": instance_id,
                        },
                        enrollment_id=enrollment.id,
                    )
                    self._log(enrollment, step, "action_deferred", {
                        "send_log_id": log.id,
                        "defer_until": decision.defer_until.isoformat() if decision.defer_until else None,
                    })
                    self.db.commit()
                    return  # Do NOT advance — enrollment pauses here
                self._log(enrollment, step, "send_message_notification_skipped", {
                    "reason": f"policy: {decision.reason}",
                })
                self._notification_advance(enrollment, step, thread)
                return
        except Exception as e:
            logger.warning(f"Error checking policy for notification step: {e}")

        # Build variable context for text substitution
        variables = {}
        if user:
            if user.name:
                variables["name"] = user.name
                variables["first_name"] = user.name.split()[0]
            if user.email:
                variables["email"] = user.email
            if user.phone:
                variables["phone"] = user.phone
            if user.external_id:
                variables["external_id"] = user.external_id
            for k, v in (user.properties or {}).items():
                variables[k] = v
        for k, v in (enrollment.enrollment_metadata or {}).items():
            if not k.startswith("_") and k != "events":
                variables[k] = v

        # Resolve wa_variable_mapping into template_components
        wa_components = None
        if config.get("wa_variable_mapping"):
            wa_resolve_vars = dict(variables)
            if user:
                wa_resolve_vars["properties"] = user.properties or {}
            # Inject project variables
            try:
                from app.services.project_variable_service import ProjectVariableService
                project_variables = ProjectVariableService(self.db).render_project_variables(
                    enrollment.funnel.project_id, wa_resolve_vars,
                )
                wa_resolve_vars["project"] = project_variables
                wa_resolve_vars["projects"] = project_variables
            except Exception as e:
                logger.warning(f"Error injecting project vars for WA mapping: {e}")
            wa_components = self._resolve_wa_variable_mapping(
                config["wa_variable_mapping"], wa_resolve_vars,
            )

        try:
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService
            import asyncio
            import concurrent.futures

            if message_type == "template" and template_name:
                content = OutboundContent(
                    content_type="template",
                    template_name=template_name,
                    template_language=template_language,
                    template_components=wa_components,
                )
            else:
                _lc, _tz = self._contact_locale_tz(enrollment)
                text, _, _, _ = template_renderer.render_template(message_text, variables, locale=_lc, timezone=_tz)
                content = OutboundContent(content_type="text", text=text)

            svc = SendService(self.db)
            send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    svc.send(
                        project_id=enrollment.funnel.project_id,
                        user_id=enrollment.user_id,
                        recipient=phone,
                        content=content,
                        channel="whatsapp",
                        source_type=send_source_type,
                        source_id=send_source_id,
                        slot_id=getattr(step, "slot_id", None),
                        instance_config={"instance_id": instance_id},
                    ),
                ).result(timeout=30)

            # SendService may have committed; refresh enrollment state.
            self.db.refresh(enrollment)

            if result.success:
                from sqlalchemy.orm.attributes import flag_modified as _flag_modified
                enrollment.messages_sent = (enrollment.messages_sent or 0) + 1
                meta = dict(enrollment.enrollment_metadata or {})
                meta["_wa_ctx"] = {"instance_id": instance_id, "phone": phone}
                enrollment.enrollment_metadata = meta
                _flag_modified(enrollment, "enrollment_metadata")
                self._log(enrollment, step, "whatsapp_notification_sent", {
                    "message_type": message_type, "phone": phone,
                    "send_log_id": result.send_log_id,
                })
                # Record the contact in the ledger so WhatsApp contact_caps /
                # channel_cooldowns actually bite. SendService only writes the
                # ledger when SEND_LEDGER_IN_SEND is on (default off), so this
                # funnel path was leaving WA sends uncounted. Guard on the same
                # flag to avoid double-counting once the send-layer cutover lands.
                try:
                    from app.services.channels.consolidation import ledger_in_send_enabled
                    if not ledger_in_send_enabled(self.db, enrollment.funnel.project_id):
                        from app.services.scoring.policy_service import PolicyService
                        PolicyService(self.db).record_contact(
                            project_id=enrollment.funnel.project_id,
                            user_id=enrollment.user_id,
                            channel="whatsapp",
                            source=send_source_type,
                            source_id=send_source_id,
                        )
                except Exception as e:
                    logger.warning(f"Error recording WA contact ledger in funnel: {e}")
            else:
                self._log(enrollment, step, "whatsapp_notification_failed", {
                    "error": result.error or "unknown",
                })
        except Exception as e:
            logger.error(f"Error sending WhatsApp notification for enrollment {enrollment.id}: {e}")
            self._log(enrollment, step, "whatsapp_notification_failed", {"error": str(e)})

        self._notification_advance(enrollment, step, thread)

    def _notification_advance(self, enrollment: FunnelEnrollment, step: FunnelStep, thread=None) -> None:
        """Advance after a notification-mode send_message step (no branches)."""
        if thread:
            self._advance_thread(enrollment, thread, step)
        else:
            self._advance_to_next(enrollment, step)

    # ------------------------------------------------------------------
    # Email send_message handlers
    # ------------------------------------------------------------------

    def _handle_send_message_email(self, enrollment: FunnelEnrollment, step: FunnelStep, thread=None) -> None:
        """Handle send_message step with channel=email (conversation mode — handoff enabled)."""
        from sqlalchemy.orm.attributes import flag_modified

        config = step.step_config or {}
        funnel = enrollment.funnel

        # Debug mode: intercept outbound for approval
        if funnel.debug_mode:
            preview_text, enriched_config = self._render_email_for_approval(enrollment, step, config)
            self._create_approval_request(
                enrollment, step, preview_text,
                send_config=enriched_config, thread=thread,
            )
            return

        message_type = config.get("message_type", "template")
        template_id = config.get("template_id")
        recipient_field = config.get("recipient_field", "email")
        handoff_type = config.get("handoff_type", "chatbot")
        handoff_id = config.get("handoff_id")
        timeout_hours = config.get("reply_timeout_hours", config.get("timeout_hours", 48))
        return_action = config.get("return_action", "resume")
        from_email = config.get("from_email")
        from_name = config.get("from_name")
        reply_to = config.get("reply_to")
        use_direct_smtp = config.get("use_direct_smtp", False)
        variable_mapping = config.get("variable_mapping", {})

        # Load user
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        if not user:
            self._log(enrollment, step, "email_send_failed", {"error": "user not found"})
            self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
            return

        # Resolve recipient email (debug override takes precedence)
        notify_override = config.get("notify_override_email")
        if notify_override:
            email_addr = notify_override
        else:
            email_addr = (
                getattr(user, recipient_field, None)
                or (user.properties or {}).get(recipient_field)
                or (enrollment.enrollment_metadata or {}).get(recipient_field)
            )
        if not email_addr:
            self._log(enrollment, step, "email_send_failed", {
                "error": f"User has no value for recipient_field '{recipient_field}'",
            })
            self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
            return

        # Policy check (skip for debug override — it's going to the user, not the contact)
        if not notify_override:
            try:
                from app.services.scoring.policy_service import PolicyService
                send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
                decision = PolicyService(self.db).check_can_contact(
                    project_id=funnel.project_id,
                    user_id=enrollment.user_id,
                    channel="email",
                    source=send_source_type,
                    source_id=send_source_id,
                )
                if not decision.allowed:
                    from app.services.channels.deferred_send_helper import DeferredSendHelper
                    helper = DeferredSendHelper(self.db)
                    if helper.should_defer(funnel.project_id, decision):
                        log = helper.create_deferred_send(
                            project_id=funnel.project_id,
                            user_id=enrollment.user_id,
                            channel="email",
                            recipient=email_addr,
                            decision=decision,
                            source_type=send_source_type,
                            source_id=send_source_id,
                            template_id=config.get("template_id"),
                            render_context={
                                "action_type": "email_send_message",
                                "config": config,
                                "enrollment_id": enrollment.id,
                                "step_id": step.id,
                            },
                            enrollment_id=enrollment.id,
                        )
                        self._log(enrollment, step, "action_deferred", {
                            "send_log_id": log.id,
                            "defer_until": decision.defer_until.isoformat() if decision.defer_until else None,
                        })
                        self.db.commit()
                        return  # Do NOT advance — enrollment pauses here
                    self._log(enrollment, step, "email_send_skipped", {"reason": f"policy: {decision.reason}"})
                    self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
                    return
            except Exception as e:
                logger.warning(f"Error checking policy for email send_message: {e}")

        # Build variable context
        variables = {}
        if user:
            if user.name:
                variables["name"] = user.name
                variables["first_name"] = user.name.split()[0]
            if user.email:
                variables["email"] = user.email
            if user.phone:
                variables["phone"] = user.phone
            if user.external_id:
                variables["external_id"] = user.external_id
            for k, v in (user.properties or {}).items():
                variables[k] = v
        for k, v in (enrollment.enrollment_metadata or {}).items():
            if not k.startswith("_") and k != "events":
                variables[k] = v
        if enrollment.enrollment_metadata and "events" in enrollment.enrollment_metadata:
            variables["events"] = enrollment.enrollment_metadata["events"]

        # Apply explicit variable_mapping (template_var -> dotted.source.path)
        for tpl_var, source_path in variable_mapping.items():
            resolved = self._resolve_variable_path(variables, source_path)
            if resolved is not None:
                variables[tpl_var] = resolved

        # i18n: resolve the contact's locale/timezone for variant selection + formatting
        from app.models import Project
        from app.services.messaging.locale_resolver import contact_locale_tz
        from app.services.messaging.template_selector import resolve_template
        _user = self.db.query(MessagingUser).filter(MessagingUser.id == enrollment.user_id).first()
        _project = self.db.query(Project).filter(Project.id == funnel.project_id).first()
        send_locale, send_tz = contact_locale_tz(_project, contact=_user) if _project else (None, None)

        # Resolve email body + subject
        subject = ""
        html_body = ""

        if message_type == "template" and template_id:
            from app.models.messaging import MessagingTemplate
            tpl = self.db.query(MessagingTemplate).filter(
                MessagingTemplate.id == template_id,
                MessagingTemplate.project_id == funnel.project_id,
            ).first()
            if not tpl:
                self._log(enrollment, step, "email_send_failed", {
                    "error": f"Email template {template_id} not found",
                })
                self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
                return
            # re-resolve to the contact's locale variant of this family
            if tpl.slug:
                variant = resolve_template(self.db, funnel.project_id, tpl.slug, send_locale)
                if variant is not None:
                    tpl = variant
            subject = tpl.subject or ""
            html_body = tpl.body or ""
            tpl_body_format = getattr(tpl, 'body_format', None) or "html"
            # Use template defaults if not overridden
            if not from_email and tpl.from_email:
                from_email = tpl.from_email
            if not from_name and tpl.from_name:
                from_name = tpl.from_name
            if not reply_to and tpl.reply_to:
                reply_to = tpl.reply_to
        else:
            subject = config.get("subject", "")
            html_body = config.get("message", "")
            tpl_body_format = "html"

        # Substitute {{variable}} placeholders (locale-aware formatting)
        html_body, subject, _, missing = template_renderer.render_template(
            html_body or "", variables, subject or None,
            locale=send_locale, timezone=send_tz,
        )
        if missing:
            logger.warning(f"Email send_message enrollment={enrollment.id}: unresolved vars: {missing}")

        # All funnel e-mail sends use the Send Layer. `use_direct_smtp` still
        # selects the project's SMTP transport, but it no longer bypasses the
        # common source contract, Candidate/Selection, Guardian and SendLog.
        sent = False
        _via_send_layer = False
        try:
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService
            import asyncio
            import concurrent.futures

            # For plain text templates, use text content_type so email adapter does \n→<br>
            if tpl_body_format == "plain":
                sl_content_type = "text"
                sl_text = html_body
                sl_html = None
            else:
                sl_content_type = "html"
                sl_text = None
                sl_html = html_body

            content = OutboundContent(
                content_type=sl_content_type,
                html=sl_html,
                text=sl_text,
                subject=subject,
                metadata={
                    "from_email": from_email,
                    "from_name": from_name,
                    "reply_to": reply_to,
                },
            )
            instance_config = {}
            email_instance_id = config.get("email_instance_id") or config.get("instance_id")
            if email_instance_id:
                # Preserve an explicitly selected sender as authoritative.
                instance_config["instance_id"] = email_instance_id
            elif use_direct_smtp:
                # Select legacy project SMTP inside the adapter, without
                # escaping the common policy and audit pipeline.
                instance_config["use_direct_smtp"] = True
            if from_email:
                instance_config["from_email"] = from_email
            if from_name:
                instance_config["from_name"] = from_name
            if reply_to:
                instance_config["reply_to"] = reply_to

            svc = SendService(self.db)
            send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    svc.send(
                        project_id=funnel.project_id,
                        user_id=enrollment.user_id,
                        recipient=email_addr,
                        content=content,
                        channel="email",
                        source_type=send_source_type,
                        source_id=send_source_id,
                        slot_id=getattr(step, "slot_id", None),
                        template_id=template_id,
                        instance_config=instance_config or None,
                    ),
                ).result(timeout=30)
            sent = result.success
            _via_send_layer = True
            if not sent:
                self._log(enrollment, step, "email_send_failed", {
                    "error": result.error or "send_layer_failed",
                    "via": "send_layer",
                })
        except Exception as e:
            logger.error(f"Error sending email via send layer for enrollment {enrollment.id}: {e}")
            self._log(enrollment, step, "email_send_failed", {"error": str(e), "via": "send_layer"})

        if not sent:
            self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
            return

        # Record success
        enrollment.messages_sent = (enrollment.messages_sent or 0) + 1
        self._log(enrollment, step, "email_sent", {
            "to": email_addr,
            "template_id": template_id,
            "subject": subject[:100],
        })

        # Contact ledger — when send() is the ledger writer, it owns the write;
        # otherwise retain the legacy compatibility writer until the project
        # ledger rollout is enabled.
        from app.services.channels.consolidation import ledger_in_send_enabled
        if not (_via_send_layer and ledger_in_send_enabled(self.db, funnel.project_id)):
            try:
                from app.models import ContactLedger
                send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
                self.db.add(ContactLedger(
                    project_id=funnel.project_id,
                    user_id=enrollment.user_id,
                    channel="email",
                    source=send_source_type,
                    source_id=send_source_id,
                ))
                self.db.flush()
            except Exception as e:
                logger.warning(f"Error recording contact ledger for email: {e}")

        # Set routing state for handoff
        try:
            from app.services.inbound.routing_state_service import RoutingStateService
            expires_at = datetime.utcnow() + timedelta(hours=timeout_hours)
            state_svc = RoutingStateService(self.db)
            state_svc.set_state(
                project_id=funnel.project_id,
                identifier=email_addr,
                channel="email",
                handler_type="funnel_send",
                handler_id=enrollment.id,
                enrollment_id=enrollment.id,
                expires_at=expires_at,
                metadata={
                    "step_id": step.id,
                    "handoff_type": handoff_type,
                    "handoff_id": handoff_id,
                    "thread_index": thread.thread_index if thread else None,
                    "context_message": config.get("context_message"),
                    "return_action": return_action,
                },
            )
        except Exception as e:
            logger.error(f"Error setting routing state for email send_message: {e}")
            self._log(enrollment, step, "send_message_routing_error", {"error": str(e)})

        # Store handoff metadata
        meta = dict(enrollment.enrollment_metadata or {})
        ho_key = f"_email_handoff_{step.id}"
        meta[ho_key] = {
            "handoff_type": handoff_type,
            "handoff_id": handoff_id,
            "started_at": datetime.utcnow().isoformat(),
            "timeout_hours": timeout_hours,
            "identifier": email_addr,
            "channel": "email",
            "template_id": template_id,
        }
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")

        self._log(enrollment, step, "email_handoff_started", {
            "handoff_type": handoff_type,
            "handoff_id": handoff_id,
            "timeout_hours": timeout_hours,
            "template_id": template_id,
            "thread_index": thread.thread_index if thread else None,
        })

    def _handle_email_notification(self, enrollment: FunnelEnrollment, step: FunnelStep, thread=None) -> None:
        """Fire-and-forget email (notification mode). Send then advance immediately."""
        from sqlalchemy.orm.attributes import flag_modified

        config = step.step_config or {}
        funnel = enrollment.funnel

        # Debug mode
        if funnel.debug_mode:
            preview_text, enriched_config = self._render_email_for_approval(enrollment, step, config)
            self._create_approval_request(
                enrollment, step, preview_text,
                send_config=enriched_config, thread=thread,
            )
            return

        message_type = config.get("message_type", "template")
        template_id = config.get("template_id")
        recipient_field = config.get("recipient_field", "email")
        from_email = config.get("from_email")
        from_name = config.get("from_name")
        reply_to = config.get("reply_to")
        use_direct_smtp = config.get("use_direct_smtp", False)
        variable_mapping = config.get("variable_mapping", {})

        # Load user
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        if not user:
            self._log(enrollment, step, "email_notification_failed", {"error": "user not found"})
            self._notification_advance(enrollment, step, thread)
            return

        # Debug: override recipient if notify_override_email is set
        notify_override = config.get("notify_override_email")
        if notify_override:
            email_addr = notify_override
        else:
            from app.services.messaging.recipient_validation import (
                is_sendable_email, project_placeholder_pattern,
            )
            email_addr = (
                getattr(user, recipient_field, None)
                or (user.properties or {}).get(recipient_field)
                or (enrollment.enrollment_metadata or {}).get(recipient_field)
            )
            # Never send to a synthetic placeholder address (per-project pattern).
            if email_addr and not is_sendable_email(
                email_addr, project_placeholder_pattern(self.db, funnel.project_id)
            ):
                email_addr = None
        if not email_addr:
            self._log(enrollment, step, "email_notification_failed", {
                "error": f"User has no value for recipient_field '{recipient_field}'",
            })
            self._notification_advance(enrollment, step, thread)
            return

        # Policy check (skip for debug override — it's going to the user, not the contact)
        if not notify_override:
            try:
                from app.services.scoring.policy_service import PolicyService
                send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
                decision = PolicyService(self.db).check_can_contact(
                    project_id=funnel.project_id,
                    user_id=enrollment.user_id,
                    channel="email",
                    source=send_source_type,
                    source_id=send_source_id,
                )
                if not decision.allowed:
                    from app.services.channels.deferred_send_helper import DeferredSendHelper
                    helper = DeferredSendHelper(self.db)
                    if helper.should_defer(funnel.project_id, decision):
                        step_cfg = step.step_config or {}
                        cfg = step_cfg.get("config", {})
                        log = helper.create_deferred_send(
                            project_id=funnel.project_id,
                            user_id=enrollment.user_id,
                            channel="email",
                            recipient=email_addr,
                            decision=decision,
                            source_type=send_source_type,
                            source_id=send_source_id,
                            template_id=cfg.get("template_id"),
                            render_context={
                                "action_type": "email_notification",
                                "config": cfg,
                                "enrollment_id": enrollment.id,
                                "step_id": step.id,
                            },
                            enrollment_id=enrollment.id,
                        )
                        self._log(enrollment, step, "action_deferred", {
                            "send_log_id": log.id,
                            "defer_until": decision.defer_until.isoformat() if decision.defer_until else None,
                        })
                        self.db.commit()
                        return  # Do NOT advance — enrollment pauses here
                    self._log(enrollment, step, "email_notification_skipped", {"reason": f"policy: {decision.reason}"})
                    self._notification_advance(enrollment, step, thread)
                    return
            except Exception as e:
                logger.warning(f"Error checking policy for email notification: {e}")

        # Build variable context
        variables = {}
        if user:
            if user.name:
                variables["name"] = user.name
                variables["first_name"] = user.name.split()[0]
            if user.email:
                variables["email"] = user.email
            if user.phone:
                variables["phone"] = user.phone
            if user.external_id:
                variables["external_id"] = user.external_id
            for k, v in (user.properties or {}).items():
                variables[k] = v
        for k, v in (enrollment.enrollment_metadata or {}).items():
            if not k.startswith("_") and k != "events":
                variables[k] = v
        if enrollment.enrollment_metadata and "events" in enrollment.enrollment_metadata:
            variables["events"] = enrollment.enrollment_metadata["events"]

        for tpl_var, source_path in variable_mapping.items():
            resolved = self._resolve_variable_path(variables, source_path)
            if resolved is not None:
                variables[tpl_var] = resolved

        # Resolve email body + subject
        subject = ""
        html_body = ""

        if message_type == "template" and template_id:
            from app.models.messaging import MessagingTemplate
            tpl = self.db.query(MessagingTemplate).filter(
                MessagingTemplate.id == template_id,
                MessagingTemplate.project_id == funnel.project_id,
            ).first()
            if not tpl:
                self._log(enrollment, step, "email_notification_failed", {
                    "error": f"Email template {template_id} not found",
                })
                self._notification_advance(enrollment, step, thread)
                return
            subject = tpl.subject or ""
            html_body = tpl.body or ""
            tpl_body_format = getattr(tpl, 'body_format', None) or "html"
            if not from_email and tpl.from_email:
                from_email = tpl.from_email
            if not from_name and tpl.from_name:
                from_name = tpl.from_name
            if not reply_to and tpl.reply_to:
                reply_to = tpl.reply_to
        else:
            subject = config.get("subject", "")
            html_body = config.get("message", "")
            tpl_body_format = "html"

        _lc, _tz = self._contact_locale_tz(enrollment)
        html_body, subject, _, missing = template_renderer.render_template(
            html_body or "", variables, subject or None, locale=_lc, timezone=_tz
        )
        if missing:
            logger.warning(f"Email notification enrollment={enrollment.id}: unresolved vars: {missing}")

        # Notification e-mails use the same Send Layer as conversation e-mails.
        # `use_direct_smtp` selects the transport in EmailAdapter; it is not an
        # escape hatch from the attention and audit pipeline.
        sent = False
        try:
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService
            import asyncio
            import concurrent.futures

            if tpl_body_format == "plain":
                sl_content_type = "text"
                sl_text = html_body
                sl_html = None
            else:
                sl_content_type = "html"
                sl_text = None
                sl_html = html_body

            content = OutboundContent(
                content_type=sl_content_type,
                html=sl_html,
                text=sl_text,
                subject=subject,
                metadata={
                    "from_email": from_email,
                    "from_name": from_name,
                    "reply_to": reply_to,
                },
            )
            instance_config = {}
            email_instance_id = config.get("email_instance_id") or config.get("instance_id")
            if email_instance_id:
                instance_config["instance_id"] = email_instance_id
            elif use_direct_smtp:
                # This only selects the transport inside EmailAdapter. The
                # SendService policy, Selection, Guardian and ledger remain
                # mandatory.
                instance_config["use_direct_smtp"] = True
            if from_email:
                instance_config["from_email"] = from_email
            if from_name:
                instance_config["from_name"] = from_name
            if reply_to:
                instance_config["reply_to"] = reply_to

            svc = SendService(self.db)
            send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    svc.send(
                        project_id=funnel.project_id,
                        user_id=enrollment.user_id,
                        recipient=email_addr,
                        content=content,
                        channel="email",
                        source_type=send_source_type,
                        source_id=send_source_id,
                        slot_id=getattr(step, "slot_id", None),
                        template_id=template_id,
                        instance_config=instance_config or None,
                    ),
                ).result(timeout=30)
            sent = result.success
            if not sent:
                self._log(enrollment, step, "email_notification_failed", {
                    "error": result.error or "send_layer_failed",
                })
        except Exception as e:
            logger.error(f"Error sending email notification via send layer: {e}")
            self._log(enrollment, step, "email_notification_failed", {"error": str(e)})

        if sent:
            enrollment.messages_sent = (enrollment.messages_sent or 0) + 1
            self._log(enrollment, step, "email_notification_sent", {
                "to": email_addr, "subject": subject[:100],
            })
            # Contact ledger — skipped when send() is the ledger writer (0D)
            from app.services.channels.consolidation import ledger_in_send_enabled
            if not ledger_in_send_enabled(self.db, funnel.project_id):
                try:
                    from app.models import ContactLedger
                    send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
                    self.db.add(ContactLedger(
                        project_id=funnel.project_id,
                        user_id=enrollment.user_id,
                        channel="email",
                        source=send_source_type,
                        source_id=send_source_id,
                    ))
                    self.db.flush()
                except Exception as e:
                    logger.warning(f"Error recording contact ledger for email notification: {e}")

        self._notification_advance(enrollment, step, thread)

    def _handle_sms_notification(self, enrollment: FunnelEnrollment, step: FunnelStep, thread=None) -> None:
        """Fire-and-forget SMS. Send via SendService → SmsAdapter then advance immediately."""
        from sqlalchemy.orm.attributes import flag_modified

        config = step.step_config or {}
        funnel = enrollment.funnel

        template_id = config.get("template_id")
        recipient_field = config.get("recipient_field", "phone")
        variable_mapping = config.get("variable_mapping", {})

        # Load user
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        if not user:
            self._log(enrollment, step, "sms_notification_failed", {"error": "user not found"})
            self._notification_advance(enrollment, step, thread)
            return

        # Resolve phone number
        phone = (
            getattr(user, "phone_e164", None)
            or getattr(user, recipient_field, None)
            or (user.properties or {}).get(recipient_field)
            or (enrollment.enrollment_metadata or {}).get(recipient_field)
        )
        if not phone:
            self._log(enrollment, step, "sms_notification_failed", {
                "error": f"User has no value for recipient_field '{recipient_field}'",
            })
            self._notification_advance(enrollment, step, thread)
            return

        # Policy check
        try:
            from app.services.scoring.policy_service import PolicyService
            send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
            decision = PolicyService(self.db).check_can_contact(
                project_id=funnel.project_id,
                user_id=enrollment.user_id,
                channel="sms",
                source=send_source_type,
                source_id=send_source_id,
            )
            if not decision.allowed:
                from app.services.channels.deferred_send_helper import DeferredSendHelper
                helper = DeferredSendHelper(self.db)
                if helper.should_defer(funnel.project_id, decision):
                    log = helper.create_deferred_send(
                        project_id=funnel.project_id,
                        user_id=enrollment.user_id,
                        channel="sms",
                        recipient=phone,
                        decision=decision,
                        source_type=send_source_type,
                        source_id=send_source_id,
                        template_id=template_id,
                        render_context={
                            "action_type": "sms_notification",
                            "config": config,
                            "enrollment_id": enrollment.id,
                            "step_id": step.id,
                        },
                        enrollment_id=enrollment.id,
                    )
                    self._log(enrollment, step, "action_deferred", {
                        "send_log_id": log.id,
                        "defer_until": decision.defer_until.isoformat() if decision.defer_until else None,
                    })
                    self.db.commit()
                    return  # Do NOT advance — enrollment pauses here
                self._log(enrollment, step, "sms_notification_skipped", {"reason": f"policy: {decision.reason}"})
                self._notification_advance(enrollment, step, thread)
                return
        except Exception as e:
            logger.warning(f"Error checking policy for SMS notification: {e}")

        # Build variable context
        variables = {}
        if user:
            if user.name:
                variables["name"] = user.name
                variables["first_name"] = user.name.split()[0]
            if user.email:
                variables["email"] = user.email
            if user.phone:
                variables["phone"] = user.phone
            if user.external_id:
                variables["external_id"] = user.external_id
            for k, v in (user.properties or {}).items():
                variables[k] = v
        for k, v in (enrollment.enrollment_metadata or {}).items():
            if not k.startswith("_") and k != "events":
                variables[k] = v
        if enrollment.enrollment_metadata and "events" in enrollment.enrollment_metadata:
            variables["events"] = enrollment.enrollment_metadata["events"]

        for tpl_var, source_path in variable_mapping.items():
            resolved = self._resolve_variable_path(variables, source_path)
            if resolved is not None:
                variables[tpl_var] = resolved

        # Resolve SMS body
        sms_body = ""
        if template_id:
            from app.models.messaging import MessagingTemplate
            tpl = self.db.query(MessagingTemplate).filter(
                MessagingTemplate.id == template_id,
                MessagingTemplate.project_id == funnel.project_id,
            ).first()
            if not tpl:
                self._log(enrollment, step, "sms_notification_failed", {
                    "error": f"SMS template {template_id} not found",
                })
                self._notification_advance(enrollment, step, thread)
                return
            sms_body = tpl.body or ""
        else:
            sms_body = config.get("message", "")

        _lc, _tz = self._contact_locale_tz(enrollment)
        sms_body, _, _, missing = template_renderer.render_template(sms_body or "", variables, locale=_lc, timezone=_tz)
        if missing:
            logger.warning(f"SMS notification enrollment={enrollment.id}: unresolved vars: {missing}")

        if not sms_body.strip():
            self._log(enrollment, step, "sms_notification_failed", {"error": "empty message body"})
            self._notification_advance(enrollment, step, thread)
            return

        # Send via SendService
        sent = False
        try:
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService
            import asyncio
            import concurrent.futures

            content = OutboundContent(content_type="text", text=sms_body)
            instance_config = {}
            if config.get("provider_id"):
                instance_config["provider_id"] = config["provider_id"]

            svc = SendService(self.db)
            send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    svc.send(
                        project_id=funnel.project_id,
                        user_id=enrollment.user_id,
                        recipient=phone,
                        content=content,
                        channel="sms",
                        source_type=send_source_type,
                        source_id=send_source_id,
                        slot_id=getattr(step, "slot_id", None),
                        instance_config=instance_config or None,
                    ),
                ).result(timeout=30)
            sent = result.success
            if not sent:
                self._log(enrollment, step, "sms_notification_failed", {
                    "error": result.error or "send_layer_failed",
                })
        except Exception as e:
            logger.error(f"Error sending SMS notification: {e}", exc_info=True)
            self._log(enrollment, step, "sms_notification_failed", {"error": str(e)})

        if sent:
            enrollment.messages_sent = (enrollment.messages_sent or 0) + 1
            self._log(enrollment, step, "sms_notification_sent", {
                "to": phone, "body": sms_body[:100],
            })
            # Contact ledger — skipped when send() is the ledger writer (0D)
            from app.services.channels.consolidation import ledger_in_send_enabled
            if not ledger_in_send_enabled(self.db, funnel.project_id):
                try:
                    from app.models import ContactLedger
                    send_source_type, send_source_id = self._send_source(enrollment, enrollment.funnel_id)
                    self.db.add(ContactLedger(
                        project_id=funnel.project_id,
                        user_id=enrollment.user_id,
                        channel="sms",
                        source=send_source_type,
                        source_id=send_source_id,
                    ))
                    self.db.flush()
                except Exception as e:
                    logger.warning(f"Error recording contact ledger for SMS notification: {e}")

        self._notification_advance(enrollment, step, thread)

    @staticmethod
    def _resolve_variable_path(data: dict, dotted_path: str):
        """Resolve a dot-separated path like 'events.signup.source' in a nested dict."""
        if not dotted_path or not data:
            return None
        parts = dotted_path.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
            if current is None:
                return None
        return current

    def _render_email_for_approval(
        self, enrollment: FunnelEnrollment, step: FunnelStep, config: dict,
    ) -> tuple:
        """Render the email template for an approval request.

        Returns (rendered_text_preview, enriched_send_config) where
        enriched_send_config has 'html', 'subject', and 'rendered_body' filled in.
        """
        from app.models.messaging import MessagingTemplate

        funnel = enrollment.funnel
        message_type = config.get("message_type", "template")
        template_id = config.get("template_id")

        # Build variable context (same as _handle_send_message_email)
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()

        variables = {}
        if user:
            if user.name:
                variables["name"] = user.name
                variables["first_name"] = user.name.split()[0]
            if user.email:
                variables["email"] = user.email
            if user.phone:
                variables["phone"] = user.phone
            if user.external_id:
                variables["external_id"] = user.external_id
            for k, v in (user.properties or {}).items():
                variables[k] = v
        for k, v in (enrollment.enrollment_metadata or {}).items():
            if not k.startswith("_") and k != "events":
                variables[k] = v
        if enrollment.enrollment_metadata and "events" in enrollment.enrollment_metadata:
            variables["events"] = enrollment.enrollment_metadata["events"]

        variable_mapping = config.get("variable_mapping", {})
        for tpl_var, source_path in variable_mapping.items():
            resolved = self._resolve_variable_path(variables, source_path)
            if resolved is not None:
                variables[tpl_var] = resolved

        subject = ""
        html_body = ""
        body_format = "html"
        from_email = config.get("from_email")
        from_name = config.get("from_name")
        reply_to = config.get("reply_to")

        if message_type == "template" and template_id:
            tpl = self.db.query(MessagingTemplate).filter(
                MessagingTemplate.id == template_id,
                MessagingTemplate.project_id == funnel.project_id,
            ).first()
            if tpl:
                subject = tpl.subject or ""
                html_body = tpl.body or ""
                body_format = getattr(tpl, 'body_format', None) or "html"
                if not from_email and tpl.from_email:
                    from_email = tpl.from_email
                if not from_name and tpl.from_name:
                    from_name = tpl.from_name
                if not reply_to and tpl.reply_to:
                    reply_to = tpl.reply_to
            else:
                return f"Email template #{template_id} (not found)", dict(config)
        else:
            subject = config.get("subject", "")
            html_body = config.get("message", "")

        # Render variables
        _lc, _tz = self._contact_locale_tz(enrollment)
        html_body, subject, _, _ = template_renderer.render_template(
            html_body or "", variables, subject or None, locale=_lc, timezone=_tz,
        )

        # Build enriched send_config with rendered content
        enriched = dict(config)
        enriched["html"] = html_body
        enriched["subject"] = subject
        enriched["rendered_body"] = html_body
        enriched["body_format"] = body_format  # "html" or "plain"
        if from_email:
            enriched["from_email"] = from_email
        if from_name:
            enriched["from_name"] = from_name
        if reply_to:
            enriched["reply_to"] = reply_to

        # Build a human-readable text preview
        import re
        text_preview = re.sub(r"<[^>]+>", "", html_body or "")
        text_preview = re.sub(r"\s+", " ", text_preview).strip()
        if len(text_preview) > 200:
            text_preview = text_preview[:200] + "..."
        preview_line = f"[{subject}] {text_preview}" if subject else text_preview
        return preview_line or f"Email template #{config.get('template_id', '?')}", enriched

    def _build_approval_variables(self, enrollment: FunnelEnrollment) -> Dict[str, Any]:
        """Build the full variable context for approval previews (shared by WA + text)."""
        variables: Dict[str, Any] = {}
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        if user:
            if user.name:
                variables["name"] = user.name
                variables["first_name"] = user.name.split()[0]
            if user.email:
                variables["email"] = user.email
            if user.phone:
                variables["phone"] = user.phone
            if user.external_id:
                variables["external_id"] = user.external_id
            variables["properties"] = user.properties or {}
            for k, v in (user.properties or {}).items():
                variables[k] = v
        for k, v in (enrollment.enrollment_metadata or {}).items():
            if not k.startswith("_") and k != "events":
                variables[k] = v
        if enrollment.enrollment_metadata and "events" in enrollment.enrollment_metadata:
            variables["events"] = enrollment.enrollment_metadata["events"]
        # Inject project variables
        try:
            from app.services.project_variable_service import ProjectVariableService
            funnel = enrollment.funnel
            if funnel:
                project_variables = ProjectVariableService(self.db).render_project_variables(
                    funnel.project_id, variables,
                )
                variables["project"] = project_variables
                variables["projects"] = project_variables
        except Exception as e:
            logger.warning(f"Error injecting project vars for approval: {e}")
        return variables

    def _render_whatsapp_for_approval(
        self, enrollment: FunnelEnrollment, step: FunnelStep, config: dict,
    ) -> tuple:
        """Render WhatsApp template content for an approval preview.

        For Meta Cloud API template messages, fetches the real template from
        the Graph API and renders it as styled HTML (like email previews).
        Resolves wa_variable_mapping into template_components so the approval
        send handler can pass them to the Meta API.
        Returns (preview_text, enriched_send_config).
        """
        import re

        template_name = config.get("template_name")
        instance_id = config.get("instance_id")

        # Build variable context for resolution
        variables = self._build_approval_variables(enrollment)

        # Non-template messages: resolve {{var}} in text and return
        if not template_name:
            msg = config.get("message") or "WhatsApp message"
            _lc, _tz = self._contact_locale_tz(enrollment)
            rendered_msg, _, _, _ = template_renderer.render_template(msg, variables, locale=_lc, timezone=_tz)
            enriched = dict(config)
            enriched["message"] = rendered_msg
            return rendered_msg, enriched

        if not instance_id:
            return f"Template: {template_name}", dict(config)

        # Resolve wa_variable_mapping into template_components
        resolved_components = config.get("template_components")
        if not resolved_components and config.get("wa_variable_mapping"):
            resolved_components = self._resolve_wa_variable_mapping(
                config["wa_variable_mapping"], variables,
            )

        # Load the WhatsApp instance
        from app.models import WhatsAppInstance
        instance = self.db.query(WhatsAppInstance).filter(
            WhatsAppInstance.id == instance_id,
        ).first()

        if not instance or getattr(instance, "provider_type", None) != "meta_cloud_api":
            enriched = dict(config)
            if resolved_components:
                enriched["template_components"] = resolved_components
            return f"Template: {template_name}", enriched

        # Decrypt access token
        import os
        from cryptography.fernet import Fernet
        access_token = None
        if instance.meta_access_token_enc:
            try:
                enc_key = os.getenv("ENCRYPTION_KEY")
                if enc_key:
                    f = Fernet(enc_key.encode())
                    access_token = f.decrypt(instance.meta_access_token_enc.encode()).decode()
                else:
                    access_token = instance.meta_access_token_enc
            except Exception as e:
                logger.error(f"Error decrypting Meta access token for template preview: {e}")

        if not access_token or not instance.meta_waba_id:
            enriched = dict(config)
            if resolved_components:
                enriched["template_components"] = resolved_components
            return f"Template: {template_name}", enriched

        # Fetch template from Meta Graph API (async → sync bridge)
        template_language = config.get("template_language", "en_US")
        template_data = None
        try:
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            from app.services.meta_cloud_api_service import meta_cloud_api_service

            def _fetch():
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(
                        meta_cloud_api_service.get_template_by_name(
                            instance.meta_waba_id, access_token,
                            template_name, template_language,
                        )
                    )
                finally:
                    loop.close()

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_fetch)
                template_data = future.result(timeout=15)
        except Exception as e:
            logger.error(f"Failed to fetch Meta template '{template_name}': {e}")

        if not template_data:
            enriched = dict(config)
            if resolved_components:
                enriched["template_components"] = resolved_components
            return f"Template: {template_name}", enriched

        # Render HTML preview with resolved components
        html = self._render_whatsapp_template_html(
            template_data, resolved_components,
        )

        enriched = dict(config)
        enriched["html"] = html
        if resolved_components:
            enriched["template_components"] = resolved_components

        # Build text preview with resolved variables
        body_comp = next(
            (c for c in template_data.get("components", []) if c.get("type") == "BODY"),
            None,
        )
        body_text = body_comp.get("text", "") if body_comp else template_name
        # Substitute {{var}} in preview text using resolved component parameters
        if resolved_components:
            for comp in resolved_components:
                params = comp.get("parameters", [])
                for i, param in enumerate(params):
                    text_val = param.get("text", "")
                    # Replace numbered: {{1}}, {{2}}
                    body_text = body_text.replace(f"{{{{{i + 1}}}}}", text_val)
                    # Replace named: {{first_name}}, {{plans_url}}
                    pname = param.get("parameter_name")
                    if pname:
                        body_text = body_text.replace("{{" + pname + "}}", text_val)
        text_preview = re.sub(r"\s+", " ", body_text).strip()
        if len(text_preview) > 200:
            text_preview = text_preview[:200] + "..."

        return text_preview or f"Template: {template_name}", enriched

    @staticmethod
    def _render_whatsapp_template_html(
        template_data: dict, template_components_override=None,
    ) -> str:
        """Render a Meta WhatsApp template as self-contained inline-CSS HTML.

        Produces a styled WhatsApp-like message bubble with header, body,
        footer, and buttons extracted from the template components.
        """
        import re

        tpl_name = template_data.get("name", "")
        tpl_lang = template_data.get("language", "")
        components = template_data.get("components", [])

        # Extract component texts
        header_text = ""
        body_text = ""
        footer_text = ""
        buttons = []

        for comp in components:
            ctype = comp.get("type", "").upper()
            if ctype == "HEADER" and comp.get("format", "").upper() == "TEXT":
                header_text = comp.get("text", "")
            elif ctype == "BODY":
                body_text = comp.get("text", "")
            elif ctype == "FOOTER":
                footer_text = comp.get("text", "")
            elif ctype == "BUTTONS":
                for btn in comp.get("buttons", []):
                    buttons.append(btn.get("text", ""))

        # Apply variable overrides from step config
        override_map = {}  # {component_type: {index: value}}
        if template_components_override:
            for ov in template_components_override:
                ov_type = ov.get("type", "").upper()
                for i, param in enumerate(ov.get("parameters", [])):
                    idx = param.get("index", i)  # fallback to enumeration index
                    val = param.get("text", param.get("value", ""))
                    if val:
                        override_map.setdefault(ov_type, {})[idx] = val

        def substitute_vars(text: str, comp_type: str) -> str:
            overrides = override_map.get(comp_type, {})
            def replacer(m):
                idx = int(m.group(1)) - 1  # {{1}} → index 0
                if idx in overrides:
                    return str(overrides[idx])
                return f"[{m.group(0)}]"
            return re.sub(r"\{\{(\d+)\}\}", replacer, text)

        header_text = substitute_vars(header_text, "HEADER")
        body_text = substitute_vars(body_text, "BODY")

        # Escape HTML in text content
        def esc(s):
            return (s.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))

        # Build HTML
        header_html = f'<div style="font-weight:700;font-size:15px;margin-bottom:6px;">{esc(header_text)}</div>' if header_text else ""
        body_html = f'<div style="font-size:14px;line-height:1.5;white-space:pre-wrap;">{esc(body_text)}</div>' if body_text else ""
        footer_html = f'<div style="font-size:12px;color:#8696a0;margin-top:8px;">{esc(footer_text)}</div>' if footer_text else ""

        buttons_html = ""
        if buttons:
            btn_items = "".join(
                f'<div style="text-align:center;padding:8px 0;border-top:1px solid #e2e8f0;">'
                f'<span style="color:#00a884;font-size:14px;font-weight:500;">{esc(b)}</span></div>'
                for b in buttons
            )
            buttons_html = f'<div style="margin-top:8px;">{btn_items}</div>'

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#e5ddd5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:420px;margin:0 auto;">
<div style="text-align:center;margin-bottom:12px;">
<span style="display:inline-block;background:#d1fae5;color:#065f46;font-size:11px;font-weight:600;padding:3px 10px;border-radius:10px;text-transform:uppercase;letter-spacing:0.5px;">WhatsApp Template Preview</span>
</div>
<div style="background:#ffffff;border-radius:8px;padding:10px 12px;box-shadow:0 1px 2px rgba(0,0,0,0.1);border-left:4px solid #25d366;">
{header_html}{body_html}{footer_html}
</div>
{buttons_html}
<div style="text-align:center;margin-top:10px;font-size:11px;color:#8696a0;">
{esc(tpl_name)} &middot; {esc(tpl_lang)}
</div>
</div>
</body></html>"""
        return html

    def _create_approval_request(
        self, enrollment: FunnelEnrollment, step: FunnelStep,
        message_content: str, send_config: dict, thread=None,
    ) -> None:
        """Create an approval request for debug mode funnels.

        Instead of sending the message, stores it as pending_approval in a
        SupportTicket tagged with 'approval_queue' for attendant review.
        """
        from sqlalchemy.orm.attributes import flag_modified

        try:
            from app.models import SupportTicket, ChatSession, ChatMessage

            project_id = enrollment.funnel.project_id
            user = self.db.query(MessagingUser).filter(
                MessagingUser.id == enrollment.user_id,
            ).first()
            if not user:
                self._log(enrollment, step, "approval_skipped", {"reason": "user_not_found"})
                return

            identifier = user.phone_e164 or user.phone or user.external_id or user.email or ""

            # Find or create a session for this contact.
            # Use ANY chatbot in the project (active or inactive) — the chatbot
            # is only needed as a FK anchor for the ChatSession, not for bot logic.
            # If no chatbot exists at all, create a placeholder.
            from app.models import Chatbot
            chatbot = self.db.query(Chatbot).filter(
                Chatbot.project_id == project_id,
            ).first()

            if not chatbot:
                chatbot = Chatbot(
                    project_id=project_id,
                    name="Funnel Approvals",
                    status="inactive",
                )
                self.db.add(chatbot)
                self.db.flush()

            session = self.db.query(ChatSession).filter(
                ChatSession.user_identifier == identifier,
                ChatSession.chatbot_id == chatbot.id,
                ChatSession.is_active == True,
            ).first()

            if not session:
                session = ChatSession(
                    chatbot_id=chatbot.id,
                    user_identifier=identifier,
                    channel=send_config.get("channel", "whatsapp"),
                    is_active=True,
                    handler_type="human",
                )
                self.db.add(session)
                self.db.flush()

            # Store the message as pending_approval
            pending_msg = ChatMessage(
                session_id=session.id,
                role="assistant",
                content=message_content,
                sender_type="funnel_approval",
                delivery_status="pending_approval",
                message_metadata={
                    "approval_source": "funnel",
                    "funnel_id": enrollment.funnel_id,
                    "step_id": step.id,
                    "enrollment_id": enrollment.id,
                    "send_config": send_config,
                    "thread_index": thread.thread_index if thread else None,
                },
            )
            self.db.add(pending_msg)
            self.db.flush()

            # Create approval ticket (allow_duplicate so each message gets its own ticket)
            from app.services.support_inbox_service import SupportInboxService
            inbox_svc = SupportInboxService(self.db)
            ticket = inbox_svc.create_ticket(
                session_id=session.id,
                reason=f"Debug approval: funnel '{enrollment.funnel.name}' step #{step.position}",
                priority="medium",
                tags=["approval_queue"],
                escalation_origin={
                    "type": "funnel_approval",
                    "ref_id": enrollment.id,
                    "step_id": step.id,
                    "message_id": pending_msg.id,
                    "thread_index": thread.thread_index if thread else None,
                },
                contact_id=enrollment.user_id,
                notification_title="Funnel Approval Needed",
                notification_body=f"'{enrollment.funnel.name}' step #{step.position} — {(message_content or '')[:80]}",
                allow_duplicate=True,
            )

            # Store ticket_id in message metadata for frontend per-message actions
            msg_meta = dict(pending_msg.message_metadata or {})
            msg_meta["approval_ticket_id"] = ticket.id
            pending_msg.message_metadata = msg_meta
            flag_modified(pending_msg, "message_metadata")

            # Pause enrollment waiting for approval
            meta = dict(enrollment.enrollment_metadata or {})
            meta["_approval_paused"] = True
            meta["_approval_ticket_id"] = ticket.id
            meta["_approval_message_id"] = pending_msg.id
            enrollment.enrollment_metadata = meta
            flag_modified(enrollment, "enrollment_metadata")

            self._log(enrollment, step, "approval_requested", {
                "ticket_id": ticket.id,
                "message_id": pending_msg.id,
                "message_preview": message_content[:100],
            })

            self.db.commit()

        except Exception as e:
            logger.error(f"Error creating approval request for enrollment {enrollment.id}: {e}", exc_info=True)

    def _resolve_send_handoff(self, enrollment: FunnelEnrollment, step: FunnelStep, reason: str, thread=None) -> None:
        """Resolve a send_message step. reason = 'completed' | 'timeout'"""
        from sqlalchemy.orm.attributes import flag_modified

        meta = dict(enrollment.enrollment_metadata or {})

        # Check both email and WhatsApp handoff metadata keys
        ho_key = f"_email_handoff_{step.id}"
        ho = meta.get(ho_key)
        if not ho:
            ho_key = f"_whatsapp_handoff_{step.id}"
            ho = meta.get(ho_key, {})

        identifier = ho.get("identifier", "")
        channel = ho.get("channel", "whatsapp")

        # Clear routing state for this enrollment
        try:
            from app.services.inbound.routing_state_service import RoutingStateService
            RoutingStateService(self.db).clear_state(
                enrollment.funnel.project_id, identifier, channel,
                enrollment_id=enrollment.id,
            )
        except Exception as e:
            logger.warning(f"Error clearing routing state for send_message: {e}")

        # Update metadata
        ho["resolved_at"] = datetime.utcnow().isoformat()
        ho["resolution"] = reason
        meta[ho_key] = ho
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")

        self._log(enrollment, step, "send_handoff_resolved", {
            "reason": reason,
            "channel": channel,
            "thread_index": thread.thread_index if thread else None,
        })

        # Navigate to exit branch: exit_0 = completed, exit_1 = timeout
        exit_branch = "exit_0" if reason == "completed" else "exit_1"
        branch_step = (
            self.db.query(FunnelStep)
            .filter(
                FunnelStep.funnel_id == enrollment.funnel_id,
                FunnelStep.parent_step_id == step.id,
                FunnelStep.branch == exit_branch,
            )
            .order_by(FunnelStep.position)
            .first()
        )

        if branch_step:
            if thread:
                thread.current_step_id = branch_step.id
                thread.current_branch = exit_branch
                thread.entered_step_at = datetime.utcnow()
                self._log(enrollment, branch_step, "entered", {"branch": exit_branch, "thread_index": thread.thread_index})
                self._process_thread_step(enrollment, thread, branch_step)
            else:
                enrollment.current_step_id = branch_step.id
                enrollment.current_branch = exit_branch
                enrollment.entered_step_at = datetime.utcnow()
                self._log(enrollment, branch_step, "entered", {"branch": exit_branch})
                self._process_current_step(enrollment, branch_step)
        else:
            if thread:
                self._advance_thread(enrollment, thread, step)
            else:
                self._return_to_main_after(enrollment, step)

    def complete_send_handoff(self, enrollment_id: int, step_id: int = None) -> dict:
        """Called externally when a chatbot/agent_team/human marks the conversation as done.
        Resolves the send_message step as 'completed' (exit_0)."""
        enrollment = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
            FunnelEnrollment.status == "active",
        ).first()
        if not enrollment:
            return {"resolved": False, "reason": "enrollment_not_found"}

        step = enrollment.current_step
        if not step or step.step_type != "send_message":
            # Check if the step_id is in a thread
            if step_id:
                step = self.db.query(FunnelStep).filter(FunnelStep.id == step_id).first()
                if not step or step.step_type != "send_message":
                    return {"resolved": False, "reason": "not_on_send_message_step"}
                # Find the thread on this step
                thread = next(
                    (t for t in enrollment.threads if t.status == "active" and t.current_step_id == step_id),
                    None,
                )
                if thread:
                    self._resolve_send_handoff(enrollment, step, "completed", thread=thread)
                    self.db.commit()
                    return {"resolved": True, "reason": "completed", "thread_index": thread.thread_index}
            return {"resolved": False, "reason": "not_on_send_message_step"}

        self._resolve_send_handoff(enrollment, step, "completed")
        self.db.commit()
        return {"resolved": True, "reason": "completed"}

    def return_from_human(self, enrollment_id: int, step_id: int = None, action: str = "resume") -> dict:
        """Called when a human support ticket is resolved, returning control to the funnel.

        Args:
            enrollment_id: The funnel enrollment ID
            step_id: The step that triggered the human handoff
            action: "resume" to continue the funnel, "exit" to mark enrollment exited
        """
        enrollment = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
            FunnelEnrollment.status == "active",
        ).first()
        if not enrollment:
            return {"returned": False, "reason": "enrollment_not_found"}

        if action == "exit":
            self._log(enrollment, enrollment.current_step, "enrollment_exited", {
                "reason": "human_exit", "step_id": step_id,
            })
            # Forced exit: kill active threads so a forked enrollment doesn't
            # leave orphans (previously this path ignored threads entirely).
            self._transition_to_exited(
                enrollment, "human_exit", status="exited", kill_threads=True,
            )
            self.db.commit()
            return {"returned": True, "action": "exit"}

        # action == "resume": complete the send_message handoff
        step = None
        if step_id:
            step = self.db.query(FunnelStep).filter(FunnelStep.id == step_id).first()
        if not step:
            step = enrollment.current_step

        if step and step.step_type == "send_message":
            self._resolve_send_handoff(enrollment, step, "completed")
            self.db.commit()
            return {"returned": True, "action": "resume"}

        # If paused (legacy), try resume_enrollment
        meta = enrollment.enrollment_metadata or {}
        if meta.get("_is_paused"):
            result = self.resume_enrollment(enrollment_id)
            return {"returned": result.get("resumed", False), "action": "resume", "detail": result}

        return {"returned": False, "reason": "not_on_send_message_step"}

    def process_send_handoff_steps(self) -> int:
        """Process send_message steps whose timeout has expired."""
        now = datetime.utcnow()

        # Top-level enrollments on send_message steps
        enrollments = (
            self.db.query(FunnelEnrollment)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .join(FunnelStep, FunnelEnrollment.current_step_id == FunnelStep.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelStep.step_type == "send_message",
                self._contact_not_paused(),
            )
            .all()
        )

        processed = 0
        for enrollment in enrollments:
            # Skip paused enrollments — timer frozen during pause
            if (enrollment.enrollment_metadata or {}).get("_is_paused"):
                continue
            step = enrollment.current_step
            config = step.step_config or {}
            if config.get("mode") == "notification" or config.get("handoff_type") == "none":
                continue  # notification steps advance immediately, never wait
            timeout_hours = config.get("timeout_hours", 24)
            if now >= enrollment.entered_step_at + timedelta(hours=timeout_hours):
                try:
                    self._resolve_send_handoff(enrollment, step, "timeout")
                    processed += 1
                except Exception as e:
                    logger.error(f"Error resolving timed-out send_message {enrollment.id}: {e}")

        # Also check threads on send_message steps
        threads = (
            self.db.query(FunnelEnrollmentThread)
            .join(FunnelStep, FunnelEnrollmentThread.current_step_id == FunnelStep.id)
            .join(FunnelEnrollment, FunnelEnrollmentThread.enrollment_id == FunnelEnrollment.id)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelEnrollmentThread.status == "active",
                FunnelStep.step_type == "send_message",
                self._contact_not_paused(),
            )
            .all()
        )
        for thread in threads:
            step = self.db.query(FunnelStep).filter(FunnelStep.id == thread.current_step_id).first()
            if not step:
                continue
            config = step.step_config or {}
            if config.get("mode") == "notification" or config.get("handoff_type") == "none":
                continue  # notification steps advance immediately, never wait
            timeout_hours = config.get("timeout_hours", 24)
            if now >= thread.entered_step_at + timedelta(hours=timeout_hours):
                try:
                    enrollment = thread.enrollment
                    self._resolve_send_handoff(enrollment, step, "timeout", thread=thread)
                    processed += 1
                except Exception as e:
                    logger.error(f"Error resolving timed-out send_message thread {thread.id}: {e}")

        if processed:
            self.db.commit()
        return processed

    # ------------------------------------------------------------------
    # Wait-for-reply support
    # ------------------------------------------------------------------

    def _setup_wait_for_reply(self, enrollment: FunnelEnrollment, step: FunnelStep, thread=None) -> None:
        """Set up ContactRoutingState to funnel_wait so InboundRouter forwards replies here."""
        config = step.step_config or {}
        timeout_hours = config.get("timeout_hours", 24)
        timeout_unit = config.get("timeout_unit", "hours")
        if timeout_unit == "minutes":
            timeout_delta = timedelta(minutes=timeout_hours)
        else:
            timeout_delta = timedelta(hours=timeout_hours)
        expires_at = datetime.utcnow() + timeout_delta

        # Find the user's contact identifier and channel
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        if not user:
            self._log(enrollment, step, "wait_for_reply_skipped", {"reason": "user not found"})
            return

        funnel = enrollment.funnel
        channel = (config.get("channel") or "whatsapp")

        if channel == "whatsapp" and (user.phone_e164 or user.phone):
            identifier = user.phone_e164 or PhoneNormalizer.normalize(
                str(user.phone or ""),
                fallback_country_code=self._get_wa_fallback_cc(funnel.project_id),
            )[0] or ""
        else:
            identifier = user.external_id or user.phone or user.email or ""
        if not identifier:
            self._log(enrollment, step, "wait_for_reply_skipped", {"reason": "no identifier"})
            return

        # Store thread info in routing metadata so handle_inbound_reply
        # knows which thread to advance
        routing_meta = None
        if thread is not None:
            routing_meta = {"thread_index": thread.thread_index}

        try:
            from app.services.inbound.routing_state_service import RoutingStateService
            state_svc = RoutingStateService(self.db)
            state_svc.set_state(
                project_id=funnel.project_id,
                identifier=identifier,
                channel=channel,
                handler_type="funnel_wait",
                handler_id=enrollment.id,
                enrollment_id=enrollment.id,
                expires_at=expires_at,
                metadata=routing_meta,
            )
            log_data = {
                "timeout_hours": timeout_hours,
                "identifier": identifier,
                "channel": channel,
                "inputs": {
                    "expires_at": expires_at.isoformat(),
                    "any_reply_advances": config.get("any_reply_advances", True),
                    "keyword_conditions": config.get("keywords", []),
                    "goals": config.get("goals", []),
                },
            }
            if thread is not None:
                log_data["thread_index"] = thread.thread_index
            self._log(enrollment, step, "wait_for_reply_started", log_data)
        except Exception as e:
            logger.error(f"Error setting routing state for wait_for_reply: {e}")
            self._log(enrollment, step, "wait_for_reply_error", {"error": str(e)})

    def handle_inbound_reply(self, enrollment_id: int, message) -> dict:
        """
        Called by InboundRouter when a message arrives for a funnel_wait enrollment.

        Args:
            enrollment_id: FunnelEnrollment.id
            message: InboundMessage (Pydantic model)

        Returns:
            dict with {advanced: bool, reason: str}
        """
        enrollment = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
            FunnelEnrollment.status == "active",
        ).first()

        if not enrollment:
            return {"advanced": False, "reason": "enrollment_not_found"}

        # Check if this is a thread-level wait_for_reply (inside a fork)
        thread = None
        step = enrollment.current_step

        if step and step.step_type == "wait_for_reply":
            # Main-flow wait_for_reply — use enrollment.current_step
            pass
        else:
            # Check if any active thread is on a wait_for_reply step
            thread = (
                self.db.query(FunnelEnrollmentThread)
                .join(FunnelStep, FunnelEnrollmentThread.current_step_id == FunnelStep.id)
                .filter(
                    FunnelEnrollmentThread.enrollment_id == enrollment.id,
                    FunnelEnrollmentThread.status == "active",
                    FunnelStep.step_type == "wait_for_reply",
                )
                .first()
            )
            if thread:
                step = thread.current_step
            else:
                return {"advanced": False, "reason": "not_on_wait_for_reply_step"}

        config = step.step_config or {}
        any_reply_advances = config.get("any_reply_advances", True)
        keyword_conditions = config.get("keyword_conditions", [])
        reply_text = message.resolved_text or ""

        # Log the reply
        log_data = {
            "body": reply_text[:500],
            "contact": message.contact_identifier,
        }
        if thread:
            log_data["thread_index"] = thread.thread_index
        self._log(enrollment, step, "reply_received", log_data)

        should_advance = False

        if any_reply_advances:
            should_advance = True
        elif keyword_conditions:
            # Evaluate keyword conditions via ConditionEvaluator
            from app.services.event_actions.conditions import ConditionEvaluator
            evaluator = ConditionEvaluator()
            event_data = {"body": reply_text, "message.body": reply_text}
            match_mode = config.get("match_mode", "any")
            should_advance = evaluator.evaluate_all(
                keyword_conditions, event_data, match_mode=match_mode,
            )

        if should_advance:
            adv_data = {"body": reply_text[:200]}
            if thread:
                adv_data["thread_index"] = thread.thread_index
            self._log(enrollment, step, "reply_advanced", adv_data)
            # Clear routing state for this enrollment
            try:
                funnel = enrollment.funnel
                from app.services.inbound.routing_state_service import RoutingStateService
                RoutingStateService(self.db).clear_state(
                    funnel.project_id,
                    message.contact_identifier,
                    message.channel,
                    enrollment_id=enrollment.id,
                )
            except Exception as e:
                logger.warning(f"Error clearing routing state after reply: {e}")

            if thread:
                # Advance the thread, not the main enrollment
                self._advance_thread(enrollment, thread, step)
                # Check if all threads done → join fork
                fork_step = enrollment.current_step
                self._check_fork_join(enrollment, fork_step)
            else:
                self.advance_enrollment(enrollment)
            self.db.commit()
            return {"advanced": True, "reason": "reply_matched"}

        return {"advanced": False, "reason": "no_match"}

    # ------------------------------------------------------------------
    # Milestone classification — handle_classified_reply
    # ------------------------------------------------------------------

    def handle_classified_reply(self, enrollment_id: int, message, classification) -> dict:
        """Route inbound message based on MilestoneClassifier result.

        Args:
            enrollment_id: FunnelEnrollment.id
            message: InboundMessage
            classification: ClassificationResult from MilestoneClassifier

        Returns:
            dict with routing outcome
        """
        enrollment = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
            FunnelEnrollment.status == "active",
        ).first()
        if not enrollment:
            return {"advanced": False, "reason": "enrollment_not_found"}

        # Find the active step (main or thread)
        thread = None
        step = enrollment.current_step
        if not step or step.step_type not in ("wait_for_reply", "send_message"):
            thread = (
                self.db.query(FunnelEnrollmentThread)
                .join(FunnelStep, FunnelEnrollmentThread.current_step_id == FunnelStep.id)
                .filter(
                    FunnelEnrollmentThread.enrollment_id == enrollment.id,
                    FunnelEnrollmentThread.status == "active",
                    FunnelStep.step_type.in_(["wait_for_reply", "send_message"]),
                )
                .first()
            )
            if thread:
                step = thread.current_step
            else:
                return {"advanced": False, "reason": "not_on_interactive_step"}

        reply_text = message.resolved_text or ""
        self._log(enrollment, step, "classified_reply", {
            "body": reply_text[:500],
            "outcome": classification.outcome,
            "goal_id": classification.goal_id,
            "confidence": classification.confidence,
            "compound": classification.compound,
        })

        outcome = classification.outcome

        if outcome == "goal_met":
            return self._handle_goal_met(enrollment, step, message, classification, thread)
        elif outcome == "on_topic":
            return self._handle_on_topic(enrollment, step, message, classification)
        elif outcome == "blocker":
            return self._handle_blocker(enrollment, step, message, classification)
        elif outcome == "off_topic":
            return self._handle_off_topic(enrollment, step, message, classification)
        else:
            return {"advanced": False, "reason": f"unknown_outcome:{outcome}"}

    def _handle_goal_met(self, enrollment, step, message, classification, thread=None) -> dict:
        """Handle goal_met classification — store milestone and optionally advance."""
        from sqlalchemy.orm.attributes import flag_modified

        config = step.step_config or {}
        goals = config.get("goals", [])
        goal_id = classification.goal_id

        # Find the matching goal config
        goal_config = next((g for g in goals if g.get("id") == goal_id), {})

        # Store milestone in enrollment_metadata
        meta = dict(enrollment.enrollment_metadata or {})
        milestones = meta.get("_milestones", [])
        milestones.append({
            "goal_id": goal_id,
            "step_id": step.id,
            "message": (message.resolved_text or "")[:500],
            "confidence": classification.confidence,
            "at": datetime.utcnow().isoformat(),
        })
        meta["_milestones"] = milestones
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")

        self._log(enrollment, step, "milestone_reached", {
            "goal_id": goal_id,
            "confidence": classification.confidence,
            "reasoning": classification.reasoning,
        })

        # Check if this goal should advance the enrollment
        advance_on_match = goal_config.get("advance_on_match", True)

        if advance_on_match:
            # Clear routing state for this enrollment
            try:
                funnel = enrollment.funnel
                from app.services.inbound.routing_state_service import RoutingStateService
                RoutingStateService(self.db).clear_state(
                    funnel.project_id,
                    message.contact_identifier,
                    message.channel,
                    enrollment_id=enrollment.id,
                )
            except Exception as e:
                logger.warning(f"Error clearing routing state after goal_met: {e}")

            if thread:
                self._advance_thread(enrollment, thread, step)
                fork_step = enrollment.current_step
                self._check_fork_join(enrollment, fork_step)
            else:
                self.advance_enrollment(enrollment)
            self.db.commit()

            # Handle compound case — goal met but also off-topic side request
            if classification.compound:
                self._create_compound_inbox_thread(enrollment, message)

            return {"advanced": True, "reason": "goal_met", "goal_id": goal_id}

        self.db.commit()
        return {"advanced": False, "reason": "goal_met_no_advance", "goal_id": goal_id}

    def _handle_on_topic(self, enrollment, step, message, classification) -> dict:
        """Handle on_topic classification — delegate to handler, keep funnel ownership."""
        config = step.step_config or {}
        on_topic_handler = config.get("on_topic_handler")

        self._log(enrollment, step, "on_topic_reply", {
            "body": (message.resolved_text or "")[:200],
            "confidence": classification.confidence,
        })

        if on_topic_handler:
            return {
                "advanced": False,
                "reason": "on_topic",
                "delegate": on_topic_handler,
            }

        return {"advanced": False, "reason": "on_topic_no_handler"}

    def _handle_blocker(self, enrollment, step, message, classification) -> dict:
        """Handle blocker classification — pause enrollment if enabled, otherwise log and stay."""
        config = step.step_config or {}
        if not config.get("pause_on_blocker", True):
            self._log(enrollment, step, "blocker_no_pause", {
                "body": (message.resolved_text or "")[:200],
                "confidence": classification.confidence,
            })
            # Delegate to on_topic_handler if configured (treat like on_topic)
            on_topic_handler = config.get("on_topic_handler")
            if on_topic_handler:
                return {"advanced": False, "reason": "blocker_delegated", "delegate": on_topic_handler}
            return {"advanced": False, "reason": "blocker_no_pause"}
        return self._pause_enrollment(enrollment, step, message, "blocker", classification)

    def _handle_off_topic(self, enrollment, step, message, classification) -> dict:
        """Handle off_topic classification — pause enrollment if enabled, otherwise log and stay."""
        config = step.step_config or {}
        if not config.get("pause_on_off_topic", True):
            self._log(enrollment, step, "off_topic_no_pause", {
                "body": (message.resolved_text or "")[:200],
                "confidence": classification.confidence,
            })
            on_topic_handler = config.get("on_topic_handler")
            if on_topic_handler:
                return {"advanced": False, "reason": "off_topic_delegated", "delegate": on_topic_handler}
            return {"advanced": False, "reason": "off_topic_no_pause"}
        return self._pause_enrollment(enrollment, step, message, "off_topic", classification)

    # ------------------------------------------------------------------
    # Pause / Resume system
    # ------------------------------------------------------------------

    def _pause_enrollment(self, enrollment, step, message, reason: str, classification) -> dict:
        """Pause a funnel enrollment and create a support ticket.

        Args:
            enrollment: FunnelEnrollment
            step: Current FunnelStep
            message: InboundMessage
            reason: "blocker" or "off_topic"
            classification: ClassificationResult

        Returns:
            dict with pause info
        """
        from sqlalchemy.orm.attributes import flag_modified

        now = datetime.utcnow()
        elapsed = (now - enrollment.entered_step_at).total_seconds() if enrollment.entered_step_at else 0

        config = step.step_config or {}
        blocker_escalation = config.get("blocker_escalation", {})

        # Store pause state in enrollment_metadata
        meta = dict(enrollment.enrollment_metadata or {})
        pause_key = f"_pause_{step.id}"
        meta["_is_paused"] = True
        meta["_paused_step_id"] = step.id
        meta[pause_key] = {
            "paused_at": now.isoformat(),
            "reason": reason,
            "elapsed_seconds": elapsed,
            "contact_identifier": message.contact_identifier,
            "channel": message.channel,
            "ticket_id": None,
            "classification_reasoning": classification.reasoning,
        }
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")

        # Clear the funnel routing row for this enrollment, then create human row
        try:
            funnel = enrollment.funnel
            from app.services.inbound.routing_state_service import RoutingStateService
            state_svc = RoutingStateService(self.db)
            # Delete the funnel-specific routing row
            state_svc.clear_state(
                funnel.project_id,
                message.contact_identifier,
                message.channel,
                enrollment_id=enrollment.id,
            )
            # Create human row (no enrollment_id — non-funnel type)
            state_svc.set_state(
                project_id=funnel.project_id,
                identifier=message.contact_identifier,
                channel=message.channel,
                handler_type="human",
                handler_id=None,
                metadata={
                    "pause_reason": reason,
                },
            )
        except Exception as e:
            logger.error(f"Error setting human routing state for pause: {e}")

        # Try to create a support ticket
        ticket_id = self._create_pause_ticket(
            enrollment, step, message, reason, blocker_escalation,
        )
        if ticket_id:
            meta[pause_key]["ticket_id"] = ticket_id
            enrollment.enrollment_metadata = meta
            flag_modified(enrollment, "enrollment_metadata")

        self._log(enrollment, step, "enrollment_paused", {
            "reason": reason,
            "elapsed_seconds": elapsed,
            "ticket_id": ticket_id,
            "contact": message.contact_identifier,
        })

        self.db.commit()
        return {
            "advanced": False,
            "reason": f"paused:{reason}",
            "ticket_id": ticket_id,
        }

    def _create_pause_ticket(self, enrollment, step, message, reason, escalation_config) -> Optional[int]:
        """Create a support ticket for a paused enrollment.

        Finds or creates a ChatSession for the contact, then creates a ticket.
        """
        try:
            from app.models import ChatSession, SupportTicket

            funnel = enrollment.funnel
            funnel_name = funnel.name if funnel else "Unknown"

            # Priority based on reason
            priority = escalation_config.get("priority", "high" if reason == "blocker" else "medium")
            tags = escalation_config.get("tags", [])
            tags = list(tags) + [f"funnel:{funnel_name}", f"step:{step.id}", reason]

            # Find existing active session for this contact
            session = (
                self.db.query(ChatSession)
                .filter(
                    ChatSession.user_identifier == message.contact_identifier,
                    ChatSession.is_active == True,
                )
                .order_by(ChatSession.created_at.desc())
                .first()
            )

            if not session:
                # No active session — we can't create a ticket without one
                # (SupportInboxService.create_ticket requires a session_id)
                logger.warning(
                    f"No active session for {message.contact_identifier}, "
                    f"skipping ticket creation for paused enrollment {enrollment.id}"
                )
                return None

            # Check for existing open ticket on this session
            existing = self.db.query(SupportTicket).filter(
                SupportTicket.session_id == session.id,
                SupportTicket.status.in_(["open", "in_progress", "waiting_customer"]),
            ).first()
            if existing:
                return existing.id

            from app.services.support_inbox_service import SupportInboxService
            inbox_service = SupportInboxService(self.db)
            ticket = inbox_service.create_ticket(
                session_id=session.id,
                reason=f"Funnel pause ({reason}): {funnel_name}",
                priority=priority,
                tags=tags,
                contact_id=enrollment.user_id,
            )
            return ticket.id if ticket else None

        except Exception as e:
            logger.error(f"Error creating pause ticket: {e}", exc_info=True)
            return None

    def _create_compound_inbox_thread(self, enrollment, message) -> None:
        """For compound results (goal_met + off_topic), create a support ticket for the side request."""
        try:
            from app.models import ChatSession, SupportTicket

            funnel = enrollment.funnel
            funnel_name = funnel.name if funnel else "Unknown"

            self._log(enrollment, None, "compound_side_request", {
                "body": (message.resolved_text or "")[:500],
                "contact": message.contact_identifier,
            })

            # Find active ChatSession for this contact
            session = (
                self.db.query(ChatSession)
                .filter(
                    ChatSession.user_identifier == message.contact_identifier,
                    ChatSession.is_active == True,
                )
                .order_by(ChatSession.created_at.desc())
                .first()
            )
            if not session:
                logger.info(
                    f"No active session for {message.contact_identifier}, "
                    f"skipping compound ticket for enrollment {enrollment.id}"
                )
                return

            from app.services.support_inbox_service import SupportInboxService
            inbox_service = SupportInboxService(self.db)
            ticket = inbox_service.create_ticket(
                session_id=session.id,
                reason=f"Side request during funnel: {funnel_name}",
                priority="medium",
                tags=["compound", "side_request", f"funnel:{funnel_name}"],
                contact_id=enrollment.user_id,
            )

            if ticket:
                from sqlalchemy.orm.attributes import flag_modified
                meta = dict(enrollment.enrollment_metadata or {})
                meta["_compound_ticket_id"] = ticket.id
                enrollment.enrollment_metadata = meta
                flag_modified(enrollment, "enrollment_metadata")
                self.db.commit()
                logger.info(f"Created compound ticket {ticket.id} for enrollment {enrollment.id}")

        except Exception as e:
            logger.warning(f"Error creating compound inbox thread: {e}", exc_info=True)

    def retry_deferred_step(self, enrollment_id: int, skip: bool = False) -> dict:
        """Retry or skip a step stuck on a deferred send.

        If skip=False: re-processes the current step, bypassing cooldown/suppression.
        If skip=True: advances past the current step without sending.

        Also cancels any pending deferred SendLogs for this enrollment.
        """
        enrollment = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
            FunnelEnrollment.status == "active",
        ).first()
        if not enrollment:
            return {"success": False, "reason": "enrollment_not_found"}

        step = enrollment.current_step
        if not step:
            return {"success": False, "reason": "no_current_step"}

        # Cancel any pending deferred send logs for this enrollment
        from app.models import SendLog
        pending = self.db.query(SendLog).filter(
            SendLog.deferred_source_enrollment_id == enrollment_id,
            SendLog.status == "deferred",
        ).all()
        for sl in pending:
            if skip:
                sl.status = "skipped"
                sl.error_message = "Skipped by user"
            else:
                # Real supersession — use the dedicated terminal status, not
                # the 'skipped' mislabel.
                sl.status = "superseded"
                sl.superseded_at = datetime.utcnow()
                sl.superseded_reason = "manual_retry"

        if skip:
            self._log(enrollment, step, "deferred_step_skipped", {"manual": True})
            self._advance_to_next(enrollment, step)
            self.db.commit()
            return {"success": True, "action": "skipped", "enrollment_id": enrollment_id}

        # Re-process the step, bypassing cooldown/suppression
        self._log(enrollment, step, "deferred_step_retried", {"manual": True})
        # Temporarily mark the step as non-suppressed for this execution
        original_config = step.step_config or {}
        step.step_config = {**original_config, "_bypass_suppression": True}
        try:
            self._process_current_step(enrollment, step)
        finally:
            step.step_config = original_config

        self.db.commit()
        return {"success": True, "action": "retried", "enrollment_id": enrollment_id}

    def resume_enrollment(self, enrollment_id: int) -> dict:
        """Resume a paused funnel enrollment.

        Resets step timer, clears pause flags, re-establishes funnel routing state,
        and optionally sends a nudge message.

        Only clears the human routing row if no OTHER enrollments are still paused
        for this same contact+channel.
        """
        from sqlalchemy.orm.attributes import flag_modified

        enrollment = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
            FunnelEnrollment.status == "active",
        ).first()
        if not enrollment:
            return {"resumed": False, "reason": "enrollment_not_found"}

        meta = dict(enrollment.enrollment_metadata or {})
        if not meta.get("_is_paused"):
            return {"resumed": False, "reason": "not_paused"}

        paused_step_id = meta.get("_paused_step_id")
        pause_key = f"_pause_{paused_step_id}" if paused_step_id else None

        # Find the step
        step = None
        if paused_step_id:
            step = self.db.query(FunnelStep).filter(FunnelStep.id == paused_step_id).first()
        if not step:
            step = enrollment.current_step
        if not step:
            return {"resumed": False, "reason": "step_not_found"}

        # Reset timer — full reset
        enrollment.entered_step_at = datetime.utcnow()

        # Clear pause flags
        meta["_is_paused"] = False
        if pause_key and pause_key in meta:
            meta[pause_key]["resumed_at"] = datetime.utcnow().isoformat()
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")

        # Check if any OTHER enrollments are still paused for this user
        # (to determine whether to clear the human routing row)
        pause_info = meta.get(pause_key, {}) if pause_key else {}
        contact_identifier = pause_info.get("contact_identifier", "")
        channel = pause_info.get("channel", "whatsapp")
        funnel = enrollment.funnel

        if contact_identifier and funnel:
            other_paused = (
                self.db.query(FunnelEnrollment)
                .filter(
                    FunnelEnrollment.id != enrollment.id,
                    FunnelEnrollment.status == "active",
                    FunnelEnrollment.enrollment_metadata["_is_paused"].astext == "true",
                )
                .count()
            )

            if other_paused == 0:
                # No other paused enrollments — clear the human routing row
                try:
                    from app.services.inbound.routing_state_service import RoutingStateService
                    RoutingStateService(self.db).clear_state(
                        funnel.project_id, contact_identifier, channel,
                    )
                except Exception as e:
                    logger.warning(f"Error clearing human routing state on resume: {e}")

        # Re-establish funnel routing state (creates a new funnel row)
        if step.step_type == "wait_for_reply":
            self._setup_wait_for_reply(enrollment, step)
        elif step.step_type == "send_message":
            # Re-establish funnel_send routing state
            config = step.step_config or {}
            self._reestablish_send_routing(enrollment, step, config)

        self._log(enrollment, step, "enrollment_resumed", {
            "paused_step_id": paused_step_id,
        })

        self.db.commit()

        # Send nudge message if configured
        config = step.step_config or {}
        nudge_text = config.get("resume_nudge", "")
        if nudge_text:
            self._send_resume_nudge(enrollment, config, nudge_text)

        return {"resumed": True, "enrollment_id": enrollment_id}

    def _reestablish_send_routing(self, enrollment, step, config) -> None:
        """Re-establish funnel_send routing state after resume."""
        timeout_hours = config.get("timeout_hours", 24)
        handoff_type = config.get("handoff_type", "chatbot")
        handoff_id = config.get("handoff_id")

        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        if not user:
            return

        funnel = enrollment.funnel
        channel = config.get("channel", "whatsapp")

        if channel == "email":
            # For email, resolve email address as identifier
            identifier = ""
            recipient_field = config.get("recipient_field")
            if recipient_field and recipient_field != "email":
                props = user.properties or {}
                identifier = str(props.get(recipient_field, ""))
            if not identifier and user.email:
                identifier = user.email
            if not identifier:
                identifier = user.external_id or ""
        else:
            # For whatsapp/sms, resolve phone-based identifier
            if user.phone_e164 or user.phone:
                identifier = user.phone_e164 or PhoneNormalizer.normalize(
                    str(user.phone or ""),
                    fallback_country_code=self._get_wa_fallback_cc(funnel.project_id),
                )[0] or ""
            else:
                identifier = user.external_id or user.email or ""

        try:
            from app.services.inbound.routing_state_service import RoutingStateService
            expires_at = datetime.utcnow() + timedelta(hours=timeout_hours)
            RoutingStateService(self.db).set_state(
                project_id=funnel.project_id,
                identifier=identifier,
                channel=channel,
                handler_type="funnel_send",
                handler_id=enrollment.id,
                enrollment_id=enrollment.id,
                expires_at=expires_at,
                metadata={
                    "step_id": step.id,
                    "handoff_type": handoff_type,
                    "handoff_id": handoff_id,
                },
            )
        except Exception as e:
            logger.error(f"Error re-establishing {channel} routing state: {e}")

    def _send_resume_nudge(self, enrollment, config: dict, nudge_text: str) -> None:
        """Send a resume nudge message via WhatsApp."""
        try:
            nudge_only_if_window = config.get("nudge_only_if_window_open", True)

            # Get WhatsApp context from enrollment metadata
            meta = enrollment.enrollment_metadata or {}
            wa_ctx = meta.get("_wa_ctx", {})
            instance_id = wa_ctx.get("instance_id") or config.get("instance_id")
            phone = wa_ctx.get("phone")

            if not instance_id or not phone:
                logger.info(f"No WhatsApp context for nudge on enrollment {enrollment.id}")
                return

            from app.models import WhatsAppInstance
            instance = self.db.query(WhatsAppInstance).filter(
                WhatsAppInstance.id == instance_id,
                WhatsAppInstance.is_active == True,
            ).first()
            if not instance:
                return

            # Check window if needed
            if nudge_only_if_window:
                if instance.provider_type == "meta_cloud_api":
                    from app.services.whatsapp_window_service import WhatsAppWindowService
                    from app.utils.phone import normalize_phone
                    clean_phone = normalize_phone(phone, instance.default_country_code)
                    window_open = WhatsAppWindowService(self.db).is_window_open(
                        instance.id, clean_phone,
                    )
                else:
                    window_open = None
                if window_open is False:
                    logger.info(
                        f"24h window closed for {phone}, skipping resume nudge "
                        f"(enrollment {enrollment.id})"
                    )
                    return

            # Send free-form text through the funnel's registered source. The
            # provider adapter owns the actual WhatsApp transport.
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService
            import asyncio
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                decision = pool.submit(
                    asyncio.run,
                    SendService(self.db).send(
                        project_id=enrollment.funnel.project_id,
                        user_id=enrollment.user_id,
                        recipient=phone,
                        content=OutboundContent(content_type="text", text=nudge_text),
                        channel="whatsapp",
                        source_type="funnel",
                        source_id=enrollment.funnel_id,
                        instance_config={"instance_id": instance.id},
                    ),
                ).result(timeout=30)

            if not decision.success:
                logger.info(
                    "Resume nudge did not send for enrollment %s: %s",
                    enrollment.id, decision.error or decision.status,
                )
                return

            self._log(enrollment, None, "resume_nudge_sent", {
                "phone": phone,
                "nudge": nudge_text[:200],
                "send_log_id": decision.send_log_id,
            })
        except Exception as e:
            logger.warning(f"Error sending resume nudge for enrollment {enrollment.id}: {e}")

    def process_paused_enrollment_expiry(self) -> int:
        """Check paused enrollments and exit those that exceed max_pause_hours."""
        now = datetime.utcnow()
        processed = 0

        enrollments = (
            self.db.query(FunnelEnrollment)
            .filter(
                FunnelEnrollment.status == "active",
            )
            .all()
        )

        for enrollment in enrollments:
            meta = enrollment.enrollment_metadata or {}
            if not meta.get("_is_paused"):
                continue

            paused_step_id = meta.get("_paused_step_id")
            pause_key = f"_pause_{paused_step_id}" if paused_step_id else None
            pause_data = meta.get(pause_key, {}) if pause_key else {}
            paused_at_str = pause_data.get("paused_at")
            if not paused_at_str:
                continue

            try:
                paused_at = datetime.fromisoformat(paused_at_str)
            except (ValueError, TypeError):
                continue

            # Determine max pause hours (default: 2x step timeout)
            step = None
            if paused_step_id:
                step = self.db.query(FunnelStep).filter(FunnelStep.id == paused_step_id).first()
            config = (step.step_config if step else None) or {}
            step_timeout_hours = config.get("timeout_hours", 24)
            max_pause_hours = config.get("max_pause_hours", step_timeout_hours * 2)

            if (now - paused_at).total_seconds() > max_pause_hours * 3600:
                try:
                    self._log(enrollment, step, "pause_expired", {
                        "max_pause_hours": max_pause_hours,
                        "paused_at": paused_at_str,
                    })
                    self._exit_enrollment(enrollment, "pause_expired")
                    processed += 1
                except Exception as e:
                    logger.error(f"Error expiring paused enrollment {enrollment.id}: {e}")

        if processed:
            self.db.commit()
        return processed

    def get_enrollment_milestones(self, enrollment_id: int) -> list:
        """Return milestones for an enrollment."""
        enrollment = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
        ).first()
        if not enrollment:
            return []
        meta = enrollment.enrollment_metadata or {}
        return meta.get("_milestones", [])

    def process_wait_for_reply_steps(self) -> int:
        """Process wait_for_reply steps whose timeout has expired (main flow + threads)."""
        now = datetime.utcnow()
        processed = 0

        # --- Main-flow wait_for_reply ---
        enrollments = (
            self.db.query(FunnelEnrollment)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .join(FunnelStep, FunnelEnrollment.current_step_id == FunnelStep.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelStep.step_type == "wait_for_reply",
                self._contact_not_paused(),
            )
            .all()
        )

        for enrollment in enrollments:
            # Skip paused enrollments — timer frozen during pause
            if (enrollment.enrollment_metadata or {}).get("_is_paused"):
                continue
            step = enrollment.current_step
            config = step.step_config or {}
            timeout_hours = config.get("timeout_hours", 24)
            timeout_unit = config.get("timeout_unit", "hours")
            if timeout_unit == "minutes":
                timeout_delta = timedelta(minutes=timeout_hours)
            else:
                timeout_delta = timedelta(hours=timeout_hours)
            if now >= enrollment.entered_step_at + timeout_delta:
                try:
                    self._log(enrollment, step, "wait_for_reply_timeout", {
                        "timeout_hours": timeout_hours,
                        "timeout_unit": timeout_unit,
                    })
                    self._clear_routing_state_for_enrollment(enrollment, config)
                    self.advance_enrollment(enrollment)
                    processed += 1
                except Exception as e:
                    logger.error(f"Error advancing timed-out wait_for_reply {enrollment.id}: {e}")

        # --- Thread-level wait_for_reply (inside forks) ---
        threads = (
            self.db.query(FunnelEnrollmentThread)
            .join(FunnelStep, FunnelEnrollmentThread.current_step_id == FunnelStep.id)
            .join(FunnelEnrollment, FunnelEnrollmentThread.enrollment_id == FunnelEnrollment.id)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .filter(
                Funnel.status == "active",
                FunnelEnrollment.status == "active",
                FunnelEnrollmentThread.status == "active",
                FunnelStep.step_type == "wait_for_reply",
                self._contact_not_paused(),
            )
            .all()
        )

        for thread in threads:
            step = thread.current_step
            if not step:
                continue
            config = step.step_config or {}
            timeout_hours = config.get("timeout_hours", 24)
            timeout_unit = config.get("timeout_unit", "hours")
            if timeout_unit == "minutes":
                timeout_delta = timedelta(minutes=timeout_hours)
            else:
                timeout_delta = timedelta(hours=timeout_hours)
            if now >= thread.entered_step_at + timeout_delta:
                try:
                    enrollment = thread.enrollment
                    self._log(enrollment, step, "wait_for_reply_timeout", {
                        "timeout_hours": timeout_hours,
                        "timeout_unit": timeout_unit,
                        "thread_index": thread.thread_index,
                    })
                    self._clear_routing_state_for_enrollment(enrollment, config)
                    self._advance_thread(enrollment, thread, step)

                    # Check if fork can join
                    fork_step = enrollment.current_step
                    self._check_fork_join(enrollment, fork_step)
                    processed += 1
                except Exception as e:
                    logger.error(f"Error advancing timed-out wait_for_reply thread {thread.id}: {e}")

        if processed:
            self.db.commit()
        return processed

    def _clear_routing_state_for_enrollment(self, enrollment: FunnelEnrollment, config: dict) -> None:
        """Clear routing state for an enrollment's user."""
        try:
            funnel = enrollment.funnel
            user = self.db.query(MessagingUser).filter(
                MessagingUser.id == enrollment.user_id,
            ).first()
            if user:
                channel = config.get("channel", "whatsapp")
                if channel == "whatsapp" and (user.phone_e164 or user.phone):
                    identifier = user.phone_e164 or PhoneNormalizer.normalize(
                        str(user.phone or ""),
                        fallback_country_code=self._get_wa_fallback_cc(funnel.project_id),
                    )[0] or ""
                else:
                    identifier = user.external_id or user.phone or user.email or ""
                from app.services.inbound.routing_state_service import RoutingStateService
                RoutingStateService(self.db).clear_state(
                    funnel.project_id, identifier, channel,
                    enrollment_id=enrollment.id,
                )
        except Exception as e:
            logger.warning(f"Error clearing routing state: {e}")
