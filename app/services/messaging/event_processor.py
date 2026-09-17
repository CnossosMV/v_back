"""
Event Processor Service for Messaging Middleware
Processes events and triggers appropriate message templates
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import and_, case

# Reward attribution window — a user event within this long after our last
# intervention counts as a response (flips the dead responded_to_last signal).
RESPONSE_ATTRIBUTION_HOURS = 24

from app.models.messaging import (
    MessagingEvent, MessagingTemplate, MessagingUser, MessagingLog,
    MessagingChannel, MessagingEventLock, MessageStatus
)
from .template_renderer import template_renderer

logger = logging.getLogger(__name__)


class EventProcessor:
    """
    Processes messaging events and triggers appropriate messages.

    Flow:
    1. Event received (from SDK or backend API)
    2. Find templates triggered by this event
    3. Check deduplication (don't send same message twice)
    4. Render template with variables
    5. Dispatch to webhook
    6. Log the result
    """

    async def process_event(
        self,
        db: Session,
        event: MessagingEvent
    ) -> List[MessagingLog]:
        """
        Process an event and trigger any matching templates.

        Args:
            db: Database session
            event: The event to process

        Returns:
            List[MessagingLog]: List of message logs created
        """
        logs = []
        # Historical Project Import facts may feed deterministic derivation and
        # analytics, but must never trigger templates, funnels, Event Actions or
        # destinations. This guard is intentionally duplicated in the drain
        # query so direct callers are also fail-closed.
        if getattr(event, "processing_mode", None) not in {None, "live"}:
            event.processed = True
            event.processing_notes = {
                **(event.processing_notes or {}),
                "historical_side_effects_skipped": True,
                "processing_mode": event.processing_mode,
            }
            db.commit()
            return []
        # A final decision emitted by a legacy orchestrator is valid only for
        # the exact ownership epoch in which it was made. Facts continue
        # through the normal pipeline regardless of ownership.
        from app.services.orchestration_cutover_service import OrchestrationCutoverService
        cutover_block = OrchestrationCutoverService(db).legacy_decision_block_reason(
            event.project_id, event.properties,
        )
        if cutover_block:
            event.processed = True
            event.processing_notes = {
                **(event.processing_notes or {}),
                "legacy_decision_skipped": cutover_block,
            }
            db.commit()
            return []
        notes = {
            "user_resolved": bool(event.user_id),
            "templates_matched": 0,
            "templates_sent": 0,
            "actions_matched": 0,
            "actions_executed": 0,
            "funnels_enrolled": 0,
            "funnels_skipped_no_user": False,
            "scoring_processed": False,
            "warnings": []
        }

        try:
            if not event.user_id:
                notes["warnings"].append("No user resolved for this event")

            # Find templates triggered by this event
            templates = self.find_triggered_templates(db, event.project_id, event.event_name)
            notes["templates_matched"] = len(templates or [])

            # Get user if available
            user = None
            if event.user_id:
                user = db.query(MessagingUser).filter(
                    MessagingUser.id == event.user_id
                ).first()

            for template in (templates or []):
                try:
                    log = await self._process_template(db, event, template, user)
                    if log:
                        logs.append(log)
                        notes["templates_sent"] += 1
                except Exception as e:
                    logger.error(
                        f"Error processing template {template.id} for event {event.id}: {e}"
                    )
                    notes["warnings"].append(f"Template {template.id} failed: {str(e)[:100]}")

            # Event Actions execute through the FunnelEngine below (system funnels
            # mirrored from each EventAction rule). The legacy EventActionEngine is
            # retained only as a compile shim; it no longer processes events here.

            # Funnel Engine — check event triggers for auto-enrollment + goal events + wait_until
            try:
                from app.services.funnel_engine import FunnelEngine
                engine = FunnelEngine(db)
                if event.user_id:
                    enrolled = engine.check_event_triggers(db, event)
                    notes["funnels_enrolled"] = len(enrolled) if enrolled else 0
                else:
                    notes["funnels_skipped_no_user"] = True
                    notes["warnings"].append("Funnel enrollment skipped: no user_id")
                engine.check_goal_event(db, event)
                engine.check_wait_until_events(db, event)
            except Exception as e:
                logger.error(f"Error processing funnel triggers for event {event.id}: {e}")
                notes["warnings"].append(f"Funnel engine error: {str(e)[:100]}")

            # Intent Scoring — update features and recalculate scores
            try:
                from app.services.scoring.scoring_engine import ScoringEngine
                if event.user_id:
                    scoring = ScoringEngine(db)
                    scoring.process_event(event.project_id, event.user_id, event.event_name, event.properties)
                    notes["scoring_processed"] = True
            except Exception as e:
                logger.error(f"Error processing scoring for event {event.id}: {e}")

            # Client timezone trait — persist the SDK-observed timezone as a
            # feature-store trait row (cheap no-op when unchanged). Identified
            # users only; anonymous tz materializes on the first identify.
            try:
                props = event.properties or {}
                if event.user_id and props.get("timezone"):
                    from app.services.scoring.feature_store_service import FeatureStoreService
                    FeatureStoreService(db).set_trait(
                        event.project_id, event.user_id, "client:timezone",
                        {
                            "timezone": props.get("timezone"),
                            "utc_offset_minutes": props.get("utc_offset_minutes"),
                            "observed_at": props.get("timezone_observed_at"),
                            "source": props.get("timezone_source") or "sdk",
                        },
                    )
            except Exception as e:
                logger.warning(f"Error storing timezone trait for event {event.id}: {e}")

            # Segment Rules — re-evaluate after scoring
            if event.user_id and event.event_name != "segment_changed":
                try:
                    from app.services.segment_engine import SegmentEngine
                    SegmentEngine(db).evaluate_user(event.project_id, event.user_id, trigger="event")
                except Exception as e:
                    logger.error(f"Error processing segment rules for event {event.id}: {e}")

            # MES - invalidate by recipient identity or direct message attribution.
            try:
                from app.services.scoring.mes_engine import MESEngine
                MESEngine(db).mark_stale_for_event(event)
            except Exception as exc:
                logger.warning("Could not mark MES stale for event %s: %s", event.id, exc)

            # Journey snapshot — update user's current position in event graph
            if event.user_id:
                try:
                    from app.models.journey import JourneySnapshot
                    from sqlalchemy.dialects.postgresql import insert as pg_insert
                    from datetime import datetime as _dt

                    if event.source in ('send_service', 'delivery_tracker'):
                        # System send — update intervention fields only, not current_event
                        arm_id = (event.properties or {}).get('arm_id')
                        stmt = pg_insert(JourneySnapshot).values(
                            project_id=event.project_id,
                            user_id=event.user_id,
                            last_intervention_at=event.created_at,
                            last_intervention_arm=arm_id,
                            responded_to_last=False,
                        ).on_conflict_do_update(
                            constraint='uq_js_proj_user',
                            set_={
                                'last_intervention_at': event.created_at,
                                'last_intervention_arm': arm_id,
                                'responded_to_last': False,
                                'updated_at': _dt.utcnow(),
                            },
                        )
                    else:
                        # User event — update current position, reset terminal state.
                        # Flip responded_to_last True when this user event lands
                        # within the attribution window after our last intervention
                        # (fixes the dead reward signal: it was written False on
                        # every send and never set True on a response).
                        _resp_cutoff = event.created_at - timedelta(hours=RESPONSE_ATTRIBUTION_HOURS)
                        stmt = pg_insert(JourneySnapshot).values(
                            project_id=event.project_id,
                            user_id=event.user_id,
                            current_event=event.event_name,
                            current_event_at=event.created_at,
                            thermal_state='hot',
                            terminal_state=None,
                        ).on_conflict_do_update(
                            constraint='uq_js_proj_user',
                            set_={
                                'current_event': event.event_name,
                                'current_event_at': event.created_at,
                                'thermal_state': 'hot',
                                'terminal_state': None,
                                'responded_to_last': case(
                                    (
                                        and_(
                                            JourneySnapshot.last_intervention_at.isnot(None),
                                            JourneySnapshot.last_intervention_at >= _resp_cutoff,
                                        ),
                                        True,
                                    ),
                                    else_=JourneySnapshot.responded_to_last,
                                ),
                                'updated_at': _dt.utcnow(),
                            },
                        )
                    db.execute(stmt)
                    notes["journey_snapshot_updated"] = True
                except Exception as e:
                    logger.error(f"Error updating journey snapshot for event {event.id}: {e}")

            # Mark event as processed and save notes
            event.processed = True
            event.processing_notes = notes
            db.commit()

        except Exception as e:
            logger.error(f"Error processing event {event.id}: {e}")
            db.rollback()

        return logs

    async def _process_template(
        self,
        db: Session,
        event: MessagingEvent,
        template: MessagingTemplate,
        user: Optional[MessagingUser]
    ) -> Optional[MessagingLog]:
        """
        Process a single template for an event.

        Args:
            db: Database session
            event: The triggering event
            template: The template to process
            user: Optional associated user

        Returns:
            Optional[MessagingLog]: The created log entry or None
        """
        # Check deduplication
        if user and not self._check_can_send(db, event.project_id, user.id, event.event_name, template.id):
            logger.debug(
                f"Skipping duplicate: user={user.id}, event={event.event_name}, template={template.id}"
            )
            return None

        # Get recipient
        recipient = self._get_recipient(user, event)
        if not recipient:
            logger.warning(f"No recipient found for event {event.id}, template {template.id}")
            return None

        # Get channel
        channel = self._get_channel(db, template, event.project_id)
        if not channel:
            logger.warning(f"No channel found for template {template.id}")
            return None

        # Build variables
        variables = self._build_variables(event, user)

        # Render template
        rendered_body, rendered_subject, _, missing = template_renderer.render_template(
            template.body,
            variables,
            template.subject
        )

        if missing:
            logger.warning(
                f"Missing variables for template {template.id}: {missing}"
            )

        # Event templates are ephemeral automation episodes. Apply their
        # declared entry effects before materializing the contender; this has
        # no provider I/O and runs in the same transaction.
        if user:
            from app.services.orchestration_impact_service import OrchestrationImpactService

            entry = OrchestrationImpactService(db).apply_entry(
                "template", template, user.id,
            )
            if not entry.get("allowed"):
                logger.info(
                    "Template automation %s entry rejected for user %s: %s",
                    template.id, user.id, entry.get("reason"),
                )
                return None

        # Keep the legacy MessagingLog projection for UI/reporting, but the
        # only authoritative delivery path is SendService below.
        log = MessagingLog(
            project_id=event.project_id,
            template_id=template.id,
            channel_id=channel.id,
            user_id=user.id if user else None,
            event_id=event.id,
            template_slug=template.slug,
            channel_type=channel.channel_type.value,
            recipient=recipient,
            rendered_subject=rendered_subject,
            rendered_body=rendered_body,
            status=MessageStatus.pending
        )
        db.add(log)
        db.flush()

        _chan_str = channel.channel_type.value
        from app.services.channels.base import OutboundContent
        from app.services.channels.send_service import SendService
        if _chan_str == "email":
            outbound = OutboundContent(
                content_type="html", html=rendered_body, subject=rendered_subject,
            )
        else:
            outbound = OutboundContent(content_type="text", text=rendered_body)
        decision = await SendService(db).send(
            project_id=event.project_id,
            user_id=user.id if user else None,
            recipient=recipient,
            content=outbound,
            channel=_chan_str,
            source_type="template",
            source_id=template.id,
            template_id=template.id,
            render_context={
                "action_type": "send_template",
                "template_id": template.id,
                "variables": variables,
            },
        )
        if decision.status == "sent":
            log.status = MessageStatus.sent
        elif decision.status in {"candidate", "delayed", "deferred", "queued"}:
            log.status = MessageStatus.pending
        else:
            log.status = MessageStatus.failed
        if getattr(decision, "provider_message_id", None):
            log.provider_message_id = decision.provider_message_id
        db.flush()

        # Create deduplication lock if user exists
        if user:
            self._create_event_lock(db, event.project_id, user.id, event.event_name, template.id)

        db.commit()

        return log

    def find_triggered_templates(
        self,
        db: Session,
        project_id: int,
        event_name: str
    ) -> List[MessagingTemplate]:
        """
        Find all active templates triggered by an event.

        Args:
            db: Database session
            project_id: The project ID
            event_name: The event name to match

        Returns:
            List[MessagingTemplate]: Matching templates
        """
        templates = db.query(MessagingTemplate).filter(
            and_(
                MessagingTemplate.project_id == project_id,
                MessagingTemplate.is_active == True,
                MessagingTemplate.automation_enabled == True,
                MessagingTemplate.trigger_events.any(event_name)
            )
        ).all()

        return templates

    def _check_can_send(
        self,
        db: Session,
        project_id: int,
        user_id: int,
        event_name: str,
        template_id: int,
        cooldown_hours: int = 24
    ) -> bool:
        """
        Check if a message can be sent (deduplication).

        Args:
            db: Database session
            project_id: The project ID
            user_id: The user ID
            event_name: The event name
            template_id: The template ID
            cooldown_hours: Hours before same message can be sent again

        Returns:
            bool: True if message can be sent
        """
        cooldown_time = datetime.utcnow() - timedelta(hours=cooldown_hours)

        existing_lock = db.query(MessagingEventLock).filter(
            and_(
                MessagingEventLock.project_id == project_id,
                MessagingEventLock.user_id == user_id,
                MessagingEventLock.event_name == event_name,
                MessagingEventLock.template_id == template_id,
                MessagingEventLock.triggered_at > cooldown_time
            )
        ).first()

        return existing_lock is None

    def _create_event_lock(
        self,
        db: Session,
        project_id: int,
        user_id: int,
        event_name: str,
        template_id: int
    ) -> None:
        """
        Create a deduplication lock.

        Args:
            db: Database session
            project_id: The project ID
            user_id: The user ID
            event_name: The event name
            template_id: The template ID
        """
        # Delete old lock if exists
        db.query(MessagingEventLock).filter(
            and_(
                MessagingEventLock.project_id == project_id,
                MessagingEventLock.user_id == user_id,
                MessagingEventLock.event_name == event_name,
                MessagingEventLock.template_id == template_id
            )
        ).delete()

        # Create new lock
        lock = MessagingEventLock(
            project_id=project_id,
            user_id=user_id,
            event_name=event_name,
            template_id=template_id,
            triggered_at=datetime.utcnow()
        )
        db.add(lock)

    def _get_recipient(
        self,
        user: Optional[MessagingUser],
        event: MessagingEvent
    ) -> Optional[str]:
        """
        Get the recipient from user or event properties.

        Args:
            user: The messaging user
            event: The event

        Returns:
            Optional[str]: The recipient address
        """
        # Try user first
        if user:
            if user.email:
                return user.email
            if user.phone:
                return user.phone

        # Try event properties
        if event.properties:
            return (
                event.properties.get('email') or
                event.properties.get('phone') or
                event.properties.get('recipient')
            )

        return None

    def _get_channel(
        self,
        db: Session,
        template: MessagingTemplate,
        project_id: int
    ) -> Optional[MessagingChannel]:
        """
        Get the channel for a template.

        Args:
            db: Database session
            template: The template
            project_id: The project ID

        Returns:
            Optional[MessagingChannel]: The channel to use
        """
        # Use template's channel if specified
        if template.channel_id:
            return db.query(MessagingChannel).filter(
                and_(
                    MessagingChannel.id == template.channel_id,
                    MessagingChannel.is_active == True
                )
            ).first()

        # Use project's default channel
        return db.query(MessagingChannel).filter(
            and_(
                MessagingChannel.project_id == project_id,
                MessagingChannel.is_default == True,
                MessagingChannel.is_active == True
            )
        ).first()

    def _build_variables(
        self,
        event: MessagingEvent,
        user: Optional[MessagingUser]
    ) -> Dict[str, Any]:
        """
        Build template variables from event and user.

        Args:
            event: The event
            user: The user

        Returns:
            Dict[str, Any]: Variables for template rendering
        """
        variables = {}

        # Add event properties
        if event.properties:
            variables.update(event.properties)

        # Add event metadata
        variables['event'] = {
            'name': event.event_name,
            'timestamp': event.created_at.isoformat() if event.created_at else None,
            'source': event.source
        }

        # Add user data
        if user:
            first_name = (user.name or '').split()[0] if user.name else ''
            variables['user'] = {
                'id': user.external_id,
                'email': user.email,
                'phone': user.phone,
                'name': user.name,
                'first_name': first_name,
            }
            # Also add flat user properties
            if user.properties:
                variables.update(user.properties)
            # Convenience aliases
            variables['name'] = user.name
            variables['first_name'] = first_name
            variables['email'] = user.email
            variables['phone'] = user.phone

        return variables

    async def retry_failed_messages(
        self,
        db: Session,
        project_id: Optional[int] = None,
        max_retries: int = 3
    ) -> int:
        """
        Retry failed messages that are due for retry.

        Args:
            db: Database session
            project_id: Optional project ID to filter
            max_retries: Maximum number of retry attempts

        Returns:
            int: Number of messages retried
        """
        now = datetime.utcnow()
        query = db.query(MessagingLog).filter(
            and_(
                MessagingLog.status.in_([MessageStatus.pending, MessageStatus.failed]),
                MessagingLog.attempt_count < max_retries,
                MessagingLog.next_retry_at <= now
            )
        )

        if project_id:
            query = query.filter(MessagingLog.project_id == project_id)

        logs = query.all()
        retried = 0

        for log in logs:
            try:
                channel = db.query(MessagingChannel).filter(
                    MessagingChannel.id == log.channel_id
                ).first()

                if not channel or not channel.is_active:
                    log.status = MessageStatus.failed
                    log.error_message = "Channel not found or inactive"
                    log.failed_at = now
                    continue

                template = db.query(MessagingTemplate).filter(
                    MessagingTemplate.id == log.template_id,
                    MessagingTemplate.project_id == log.project_id,
                ).first()
                if not template:
                    log.status = MessageStatus.failed
                    log.error_message = "Template not found for retry"
                    continue
                source_type = "template" if log.event_id else "manual_send"
                if source_type == "template" and not template.automation_enabled:
                    log.status = MessageStatus.failed
                    log.error_message = "Template automation is disabled"
                    continue
                from app.services.channels.base import OutboundContent
                from app.services.channels.send_service import SendService
                channel_type = channel.channel_type.value
                content = (
                    OutboundContent(
                        content_type="html", html=log.rendered_body,
                        subject=log.rendered_subject,
                    )
                    if channel_type == "email"
                    else OutboundContent(content_type="text", text=log.rendered_body)
                )
                decision = await SendService(db).send(
                    project_id=log.project_id,
                    user_id=log.user_id,
                    recipient=log.recipient,
                    content=content,
                    channel=channel_type,
                    source_type=source_type,
                    source_id=template.id,
                    template_id=template.id,
                )
                log.attempt_count = int(log.attempt_count or 0) + 1
                if decision.status == "sent":
                    log.status = MessageStatus.sent
                    log.provider_message_id = decision.provider_message_id
                    log.next_retry_at = None
                elif decision.status in {"candidate", "delayed", "deferred", "queued"}:
                    log.status = MessageStatus.pending
                    log.next_retry_at = None
                else:
                    log.status = MessageStatus.failed
                    log.error_message = decision.error
                    log.next_retry_at = now + timedelta(seconds=channel.retry_delay_seconds)

                retried += 1

            except Exception as e:
                logger.error(f"Error retrying message {log.id}: {e}")

        db.commit()
        return retried

    async def send_direct(
        self,
        db: Session,
        project_id: int,
        template_slug: str,
        recipient: str,
        variables: Dict[str, Any],
        user_id: Optional[str] = None,
        channel_slug: Optional[str] = None
    ) -> MessagingLog:
        """
        Send a message directly (without event trigger).

        Args:
            db: Database session
            project_id: The project ID
            template_slug: The template slug
            recipient: The recipient address
            variables: Template variables
            user_id: Optional external user ID
            channel_slug: Optional channel slug override

        Returns:
            MessagingLog: The created log entry
        """
        # Get template
        template = db.query(MessagingTemplate).filter(
            and_(
                MessagingTemplate.project_id == project_id,
                MessagingTemplate.slug == template_slug,
                MessagingTemplate.is_active == True
            )
        ).first()

        if not template:
            raise ValueError(f"Template '{template_slug}' not found")

        # Get channel
        if channel_slug:
            channel = db.query(MessagingChannel).filter(
                and_(
                    MessagingChannel.project_id == project_id,
                    MessagingChannel.slug == channel_slug,
                    MessagingChannel.is_active == True
                )
            ).first()
        else:
            channel = self._get_channel(db, template, project_id)

        if not channel:
            raise ValueError("No active channel available")

        # Get or create user
        user = None
        if user_id:
            user = db.query(MessagingUser).filter(
                and_(
                    MessagingUser.project_id == project_id,
                    MessagingUser.external_id == user_id
                )
            ).first()

        # Build full variables
        full_variables = dict(variables)
        if user:
            full_variables['user'] = {
                'id': user.external_id,
                'email': user.email,
                'phone': user.phone,
                'name': user.name
            }
            if user.properties:
                full_variables.update(user.properties)

        # i18n: resolve the contact's locale/timezone and re-resolve the template to
        # the matching locale variant (right body + meta_language). Flag-gated.
        from app.models import Project
        from app.services.messaging.locale_resolver import contact_locale_tz
        from app.services.messaging.template_selector import resolve_template
        _project = db.query(Project).filter(Project.id == project_id).first()
        send_locale, send_tz = contact_locale_tz(_project, contact=user) if _project else (None, None)
        if _project:
            variant = resolve_template(db, project_id, template.slug, send_locale)
            if variant is not None:
                template = variant

        # Render template
        rendered_body, rendered_subject, _, missing = template_renderer.render_template(
            template.body,
            full_variables,
            template.subject,
            locale=send_locale,
            timezone=send_tz,
        )

        # Create log
        log = MessagingLog(
            project_id=project_id,
            template_id=template.id,
            channel_id=channel.id,
            user_id=user.id if user else None,
            template_slug=template.slug,
            channel_type=channel.channel_type.value,
            recipient=recipient,
            rendered_subject=rendered_subject,
            rendered_body=rendered_body,
            status=MessageStatus.pending
        )
        db.add(log)
        db.flush()

        # Explicit API sends still use the registered manual source, but never
        # bypass the common policy/provider boundary.
        from app.services.channels.base import OutboundContent
        from app.services.channels.send_service import SendService
        channel_type = channel.channel_type.value
        content = (
            OutboundContent(
                content_type="html", html=rendered_body, subject=rendered_subject,
            )
            if channel_type == "email"
            else OutboundContent(content_type="text", text=rendered_body)
        )
        decision = await SendService(db).send(
            project_id=project_id,
            user_id=user.id if user else None,
            recipient=recipient,
            content=content,
            channel=channel_type,
            source_type="manual_send",
            source_id=template.id,
            template_id=template.id,
        )
        if decision.status == "sent":
            log.status = MessageStatus.sent
            log.provider_message_id = decision.provider_message_id
        elif decision.status in {"delayed", "deferred", "queued"}:
            log.status = MessageStatus.pending
        else:
            log.status = MessageStatus.failed
            log.error_message = decision.error
        db.commit()

        return log


# Singleton instance
event_processor = EventProcessor()
