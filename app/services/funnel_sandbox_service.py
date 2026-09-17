"""Sandbox service for funnel testing without real message delivery."""

import logging
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import (
    Funnel, FunnelStep, FunnelEnrollment, FunnelEnrollmentLog,
    SandboxSession, SandboxActionLog,
)
from app.models.messaging import MessagingUser, MessagingEvent
from app.services.messaging.event_sources import SANDBOX as EVENT_SOURCE_SANDBOX

logger = logging.getLogger(__name__)


class FunnelSandboxService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Sandbox Contact CRUD
    # ------------------------------------------------------------------

    def create_sandbox_contact(
        self,
        project_id: int,
        name: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        properties: Optional[dict] = None,
    ) -> MessagingUser:
        """Create a MessagingUser with is_sandbox=True and auto-generated external_id."""
        external_id = f"sandbox_{uuid.uuid4().hex[:12]}"
        user = MessagingUser(
            project_id=project_id,
            external_id=external_id,
            name=name or f"Sandbox Contact",
            email=email,
            phone=phone,
            properties=properties or {},
            is_sandbox=True,
            is_subscribed=True,
            created_via="manual",
        )
        self.db.add(user)
        self.db.flush()
        return user

    def delete_sandbox_contact(self, contact_id: int, project_id: int) -> bool:
        """Hard-delete a sandbox contact and cascade all associated records."""
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == contact_id,
            MessagingUser.project_id == project_id,
            MessagingUser.is_sandbox == True,
        ).first()
        if not user:
            return False

        # Delete sandbox sessions (cascade will handle action logs)
        self.db.query(SandboxSession).filter(
            SandboxSession.contact_id == contact_id,
        ).delete(synchronize_session="fetch")

        # Delete enrollments (cascade handles enrollment logs)
        self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.user_id == contact_id,
        ).delete(synchronize_session="fetch")

        # Delete events
        self.db.query(MessagingEvent).filter(
            MessagingEvent.user_id == contact_id,
        ).delete(synchronize_session="fetch")

        # Delete routing states if any
        try:
            from app.models import ContactRoutingState
            self.db.query(ContactRoutingState).filter(
                ContactRoutingState.project_id == project_id,
            ).delete(synchronize_session="fetch")
        except Exception:
            pass

        # Delete the contact itself
        self.db.delete(user)
        self.db.flush()
        return True

    def update_contact_properties(
        self,
        contact_id: int,
        project_id: int,
        properties: dict,
    ) -> Optional[MessagingUser]:
        """Merge properties into a sandbox contact's properties dict."""
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == contact_id,
            MessagingUser.project_id == project_id,
            MessagingUser.is_sandbox == True,
        ).first()
        if not user:
            return None

        current = dict(user.properties or {})
        current.update(properties)
        user.properties = current
        flag_modified(user, "properties")
        self.db.flush()
        return user

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(
        self,
        project_id: int,
        funnel_id: int,
        contact_id: int,
        created_by: int,
        mode: str = "log_only",
        preview_channel: Optional[str] = None,
        preview_destination: Optional[str] = None,
        preview_instance_id: Optional[int] = None,
    ) -> SandboxSession:
        """Create a sandbox session linking contact to funnel."""
        # Check no existing active session
        existing = self.db.query(SandboxSession).filter(
            SandboxSession.funnel_id == funnel_id,
            SandboxSession.contact_id == contact_id,
            SandboxSession.status == "active",
        ).first()
        if existing:
            return existing

        session = SandboxSession(
            project_id=project_id,
            funnel_id=funnel_id,
            contact_id=contact_id,
            created_by=created_by,
            mode=mode,
            preview_channel=preview_channel,
            preview_destination=preview_destination,
            preview_instance_id=preview_instance_id,
            status="active",
        )
        self.db.add(session)
        self.db.flush()
        return session

    def update_session(
        self,
        session_id: int,
        mode: Optional[str] = None,
        preview_channel: Optional[str] = None,
        preview_destination: Optional[str] = None,
        preview_instance_id: Optional[int] = None,
    ) -> Optional[SandboxSession]:
        """Update session mode and preview settings."""
        session = self.db.query(SandboxSession).filter(
            SandboxSession.id == session_id,
        ).first()
        if not session:
            return None
        if mode is not None:
            session.mode = mode
        if preview_channel is not None:
            session.preview_channel = preview_channel
        if preview_destination is not None:
            session.preview_destination = preview_destination
        if preview_instance_id is not None:
            session.preview_instance_id = preview_instance_id
        session.updated_at = datetime.utcnow()
        self.db.flush()
        return session

    # ------------------------------------------------------------------
    # Enrollment operations
    # ------------------------------------------------------------------

    def force_enroll(self, session_id: int) -> Optional[FunnelEnrollment]:
        """Enroll sandbox contact via FunnelEngine with bypass_status_check."""
        session = self.db.query(SandboxSession).filter(
            SandboxSession.id == session_id,
            SandboxSession.status == "active",
        ).first()
        if not session:
            return None

        from app.services.funnel_engine import FunnelEngine
        engine = FunnelEngine(self.db)
        enrollment = engine.enroll_user(
            funnel_id=session.funnel_id,
            user_id=session.contact_id,
            bypass_status_check=True,
        )
        if enrollment:
            session.enrollment_id = enrollment.id
            self.db.flush()
        return enrollment

    def inject_event(
        self,
        session_id: int,
        event_name: str,
        properties: Optional[dict] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a real MessagingEvent and trigger funnel engine checks."""
        session = self.db.query(SandboxSession).filter(
            SandboxSession.id == session_id,
            SandboxSession.status == "active",
        ).first()
        if not session:
            return None

        # Create the event
        event = MessagingEvent(
            project_id=session.project_id,
            user_id=session.contact_id,
            event_name=event_name,
            properties=properties or {},
            source=EVENT_SOURCE_SANDBOX,
        )
        self.db.add(event)
        self.db.flush()

        result = {"event_id": event.id, "actions_triggered": []}

        # Trigger funnel engine checks
        from app.services.funnel_engine import FunnelEngine
        engine = FunnelEngine(self.db)

        # Check wait_until events
        try:
            engine.check_wait_until_events(self.db, event)
            result["actions_triggered"].append("wait_until_check")
        except Exception as e:
            logger.warning(f"Sandbox event wait_until check error: {e}")

        # Check goal events
        try:
            exited = engine.check_goal_event(self.db, event)
            if exited:
                result["actions_triggered"].append(f"goal_exit:{exited}")
        except Exception as e:
            logger.warning(f"Sandbox event goal check error: {e}")

        self.db.commit()
        return result

    def mock_webhook_response(
        self,
        session_id: int,
        step_id: int,
        response_data: dict,
    ) -> bool:
        """Store mock webhook response in enrollment metadata and advance if waiting."""
        session = self.db.query(SandboxSession).filter(
            SandboxSession.id == session_id,
            SandboxSession.status == "active",
        ).first()
        if not session or not session.enrollment_id:
            return False

        enrollment = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == session.enrollment_id,
        ).first()
        if not enrollment:
            return False

        step = self.db.query(FunnelStep).filter(FunnelStep.id == step_id).first()
        if not step:
            return False

        # Store the mock response under the store_result_as key
        config = step.step_config or {}
        action_cfg = config.get("config", {})
        store_key = action_cfg.get("store_result_as", f"webhook_response_{step_id}")

        meta = dict(enrollment.enrollment_metadata or {})
        meta[store_key] = response_data
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")

        # Log the mock
        self._log_action(
            session_id=session.id,
            enrollment_id=enrollment.id,
            step_id=step_id,
            action_type="webhook_mock",
            action_config={"store_key": store_key, "response": response_data},
            intercepted_mode="logged",
        )

        self.db.flush()
        return True

    def advance_past_wait(self, session_id: int) -> Optional[Dict[str, Any]]:
        """Manually skip current wait/wait_for_reply step."""
        session = self.db.query(SandboxSession).filter(
            SandboxSession.id == session_id,
            SandboxSession.status == "active",
        ).first()
        if not session or not session.enrollment_id:
            return None

        enrollment = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == session.enrollment_id,
            FunnelEnrollment.status == "active",
        ).first()
        if not enrollment:
            return None

        step = enrollment.current_step
        if not step:
            return None

        if step.step_type not in ("wait", "wait_for_reply", "wait_until", "send_message"):
            return {"error": f"Current step is {step.step_type}, not a wait-type step"}

        from app.services.funnel_engine import FunnelEngine
        engine = FunnelEngine(self.db)
        engine.advance_enrollment(enrollment)
        self.db.commit()

        return {
            "advanced": True,
            "previous_step_id": step.id,
            "previous_step_type": step.step_type,
            "current_step_id": enrollment.current_step_id,
            "status": enrollment.status,
        }

    def reset_enrollment(self, session_id: int, re_enroll: bool = False) -> Optional[Dict[str, Any]]:
        """Force-exit current enrollment, clear action logs, optionally re-enroll."""
        session = self.db.query(SandboxSession).filter(
            SandboxSession.id == session_id,
            SandboxSession.status == "active",
        ).first()
        if not session:
            return None

        result = {"reset": True, "re_enrolled": False}

        if session.enrollment_id:
            enrollment = self.db.query(FunnelEnrollment).filter(
                FunnelEnrollment.id == session.enrollment_id,
            ).first()
            if enrollment and enrollment.status == "active":
                enrollment.status = "exited"
                enrollment.exit_reason = "sandbox_reset"
                enrollment.exited_at = datetime.utcnow()

        # Clear action logs for this session
        self.db.query(SandboxActionLog).filter(
            SandboxActionLog.session_id == session.id,
        ).delete(synchronize_session="fetch")

        session.enrollment_id = None

        if re_enroll:
            enrollment = self.force_enroll(session.id)
            if enrollment:
                result["re_enrolled"] = True
                result["enrollment_id"] = enrollment.id

        self.db.flush()
        return result

    # ------------------------------------------------------------------
    # Action log
    # ------------------------------------------------------------------

    def get_action_log(self, session_id: int) -> List[SandboxActionLog]:
        """Return all SandboxActionLog entries for a session."""
        return (
            self.db.query(SandboxActionLog)
            .filter(SandboxActionLog.session_id == session_id)
            .order_by(SandboxActionLog.created_at.asc())
            .all()
        )

    def get_enrollment_logs(self, session_id: int) -> List[FunnelEnrollmentLog]:
        """Return enrollment logs for the session's current enrollment."""
        session = self.db.query(SandboxSession).filter(
            SandboxSession.id == session_id,
        ).first()
        if not session or not session.enrollment_id:
            return []

        return (
            self.db.query(FunnelEnrollmentLog)
            .filter(FunnelEnrollmentLog.enrollment_id == session.enrollment_id)
            .order_by(FunnelEnrollmentLog.created_at.asc())
            .all()
        )

    def _log_action(
        self,
        session_id: int,
        enrollment_id: Optional[int],
        step_id: Optional[int],
        action_type: str,
        action_config: Optional[dict] = None,
        intercepted_mode: str = "logged",
        preview_result: Optional[dict] = None,
        resolved_variables: Optional[dict] = None,
        suppression_check: Optional[dict] = None,
    ) -> SandboxActionLog:
        """Create a SandboxActionLog entry."""
        log = SandboxActionLog(
            session_id=session_id,
            enrollment_id=enrollment_id,
            step_id=step_id,
            action_type=action_type,
            action_config=action_config,
            intercepted_mode=intercepted_mode,
            preview_result=preview_result,
            resolved_variables=resolved_variables,
            suppression_check=suppression_check,
        )
        self.db.add(log)
        self.db.flush()
        return log
