"""
Channel Health Service — aggregates delivery metrics from send_logs.

Provides per-project and per-instance health summaries for the
Channel Health Dashboard (admin-only).
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, case, and_
from sqlalchemy.orm import Session

from app.models import SendLog, DeliveryStatusEvent, WhatsAppInstance

logger = logging.getLogger(__name__)


def _blocked_reason_code(message: Optional[str]) -> str:
    normalized = (message or "").lower()
    if "no attention_policy" in normalized:
        return "source_contract_missing_attention_policy"
    if "no purpose_key" in normalized:
        return "source_contract_missing_purpose_key"
    if "outbound source contract" in normalized:
        return "source_contract"
    if "missing consent" in normalized:
        return "missing_consent"
    if "opt-out" in normalized or "opted out" in normalized:
        return "opt_out"
    if "verification" in normalized:
        return "contact_verification"
    if "placeholder" in normalized:
        return "placeholder_recipient"
    if "policy" in normalized:
        return "policy"
    return "other"


class ChannelHealthService:
    def __init__(self, db: Session):
        self.db = db

    def get_project_health(
        self, project_id: int, hours: int = 24
    ) -> Dict[str, Any]:
        """Aggregate channel health summary cards for a project."""
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        base = self.db.query(SendLog).filter(
            SendLog.project_id == project_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
        )

        total = base.count()
        if total == 0:
            return {
                "total_attempts": 0,
                "total_sent": 0,
                "blocked_count": 0,
                "pending_count": 0,
                "skipped_count": 0,
                "delivered_count": 0,
                "read_count": 0,
                "failed_count": 0,
                "status_counts": {},
                "blocked_reasons": [],
                "delivery_rate": 0.0,
                "failure_rate": 0.0,
                "channels": [],
                "hours": hours,
            }

        stats = self.db.query(
            SendLog.channel,
            func.count(SendLog.id).label("total"),
            func.count(case((SendLog.status == "delivered", 1))).label("delivered"),
            func.count(case((SendLog.status == "read", 1))).label("read"),
            func.count(case((SendLog.status == "failed", 1))).label("failed"),
            func.count(case((SendLog.status == "sent", 1))).label("sent"),
            func.count(case((SendLog.status == "blocked", 1))).label("blocked"),
            func.count(case((SendLog.status.in_(["candidate", "deferred", "delayed", "submitting"]), 1))).label("pending"),
            func.count(case((SendLog.status.in_(["skipped", "canceled", "exhausted", "policy_window_expired"]), 1))).label("skipped"),
        ).filter(
            SendLog.project_id == project_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
        ).group_by(SendLog.channel).all()

        status_rows = self.db.query(
            SendLog.status,
            func.count(SendLog.id).label("count"),
        ).filter(
            SendLog.project_id == project_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
        ).group_by(SendLog.status).all()
        blocked_rows = self.db.query(
            SendLog.error_message,
            func.count(SendLog.id).label("count"),
        ).filter(
            SendLog.project_id == project_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
            SendLog.status == "blocked",
        ).group_by(SendLog.error_message).order_by(
            func.count(SendLog.id).desc(),
        ).limit(20).all()

        channels = []
        total_attempts = 0
        total_sent = 0
        total_delivered = 0
        total_read = 0
        total_failed = 0
        total_blocked = 0
        total_pending = 0
        total_skipped = 0

        for row in stats:
            ch_total = row.total
            ch_delivered = row.delivered + row.read  # read implies delivered
            ch_failed = row.failed
            ch_sent = row.sent + row.delivered + row.read
            total_attempts += ch_total
            total_sent += ch_sent
            total_delivered += ch_delivered
            total_read += row.read
            total_failed += ch_failed
            total_blocked += row.blocked
            total_pending += row.pending
            total_skipped += row.skipped

            channels.append({
                "channel": row.channel,
                "total": ch_total,  # Backward-compatible dashboard alias.
                "total_attempts": ch_total,
                "sent": ch_sent,
                "blocked": row.blocked,
                "pending": row.pending,
                "skipped": row.skipped,
                "delivered": ch_delivered,
                "read": row.read,
                "failed": ch_failed,
                "delivery_rate": round(ch_delivered / ch_sent * 100, 1) if ch_sent else 0,
                "failure_rate": round(ch_failed / ch_total * 100, 1) if ch_total else 0,
            })

        return {
            "total_attempts": total_attempts,
            "total_sent": total_sent,
            "blocked_count": total_blocked,
            "pending_count": total_pending,
            "skipped_count": total_skipped,
            "delivered_count": total_delivered,
            "read_count": total_read,
            "failed_count": total_failed,
            "status_counts": {str(row.status): row.count for row in status_rows},
            "blocked_reasons": [
                {
                    "code": _blocked_reason_code(row.error_message),
                    "reason": (row.error_message or "Unknown block reason")[:220],
                    "count": row.count,
                }
                for row in blocked_rows
            ],
            "delivery_rate": round(total_delivered / total_sent * 100, 1) if total_sent else 0,
            "failure_rate": round(total_failed / total_attempts * 100, 1) if total_attempts else 0,
            "channels": channels,
            "hours": hours,
        }

    def get_instance_health(
        self, project_id: int, instance_id: int, hours: int = 24
    ) -> Dict[str, Any]:
        """Detailed metrics for a specific channel instance."""
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        base = self.db.query(SendLog).filter(
            SendLog.project_id == project_id,
            SendLog.instance_id == instance_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
        )

        total = base.count()

        stats = self.db.query(
            func.count(SendLog.id).label("total"),
            func.count(case((SendLog.status == "delivered", 1))).label("delivered"),
            func.count(case((SendLog.status == "read", 1))).label("read"),
            func.count(case((SendLog.status == "failed", 1))).label("failed"),
            func.count(case((SendLog.status == "sent", 1))).label("sent"),
            func.count(case((SendLog.status == "blocked", 1))).label("blocked"),
        ).filter(
            SendLog.project_id == project_id,
            SendLog.instance_id == instance_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
        ).first()

        # Average delivery time (sent_at → delivered_at)
        avg_delivery = self.db.query(
            func.avg(
                func.extract('epoch', SendLog.delivered_at) -
                func.extract('epoch', SendLog.sent_at)
            )
        ).filter(
            SendLog.project_id == project_id,
            SendLog.instance_id == instance_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
            SendLog.delivered_at.isnot(None),
            SendLog.sent_at.isnot(None),
        ).scalar()

        # Error breakdown
        errors = self.db.query(
            SendLog.error_message,
            func.count(SendLog.id).label("count"),
        ).filter(
            SendLog.project_id == project_id,
            SendLog.instance_id == instance_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
            SendLog.status == "failed",
        ).group_by(SendLog.error_message).order_by(func.count(SendLog.id).desc()).limit(10).all()

        # Hourly volume
        hourly = self.db.query(
            func.date_trunc('hour', SendLog.queued_at).label("hour"),
            func.count(SendLog.id).label("count"),
        ).filter(
            SendLog.project_id == project_id,
            SendLog.instance_id == instance_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
        ).group_by(func.date_trunc('hour', SendLog.queued_at)).order_by("hour").all()

        # WhatsApp instance connection status
        connection_status = None
        wa_instance = self.db.query(WhatsAppInstance).filter(
            WhatsAppInstance.id == instance_id,
        ).first()
        if wa_instance:
            connection_status = getattr(wa_instance, 'connection_status', None)

        delivered = (stats.delivered or 0) + (stats.read or 0) if stats else 0
        sent = (stats.sent or 0) + delivered if stats else 0

        return {
            "instance_id": instance_id,
            "total_attempts": stats.total if stats else 0,
            "total_sent": sent,
            "blocked_count": stats.blocked if stats else 0,
            "delivered_count": delivered,
            "read_count": stats.read if stats else 0,
            "failed_count": stats.failed if stats else 0,
            "delivery_rate": round(delivered / sent * 100, 1) if sent else 0,
            "failure_rate": round((stats.failed or 0) / stats.total * 100, 1) if stats and stats.total else 0,
            "avg_delivery_time_seconds": round(avg_delivery, 1) if avg_delivery else None,
            "error_breakdown": [
                {"error": e.error_message or "Unknown", "count": e.count}
                for e in errors
            ],
            "hourly_volume": [
                {"hour": h.hour.isoformat(), "count": h.count}
                for h in hourly
            ],
            "connection_status": connection_status,
            "hours": hours,
        }

    def get_instances_summary(
        self, project_id: int, hours: int = 24
    ) -> List[Dict[str, Any]]:
        """List all instances for a project with summary metrics."""
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        # Get instances with send activity
        instance_stats = self.db.query(
            SendLog.instance_id,
            SendLog.channel,
            func.count(SendLog.id).label("total"),
            func.count(case((SendLog.status == "sent", 1))).label("sent"),
            func.count(case((SendLog.status == "delivered", 1))).label("delivered"),
            func.count(case((SendLog.status == "read", 1))).label("read"),
            func.count(case((SendLog.status == "failed", 1))).label("failed"),
            func.max(SendLog.queued_at).label("last_activity"),
        ).filter(
            SendLog.project_id == project_id,
            SendLog.queued_at >= cutoff,
            SendLog.is_historical.is_(False),
            SendLog.instance_id.isnot(None),
        ).group_by(SendLog.instance_id, SendLog.channel).all()

        instances = []
        for row in instance_stats:
            delivered = (row.delivered or 0) + (row.read or 0)
            sent = (row.sent or 0) + delivered
            instances.append({
                "instance_id": row.instance_id,
                "channel": row.channel,
                "total_attempts": row.total,
                "total_sent": sent,
                "delivered_count": delivered,
                "failed_count": row.failed or 0,
                "delivery_rate": round(delivered / sent * 100, 1) if sent else 0,
                "failure_rate": round((row.failed or 0) / row.total * 100, 1) if row.total else 0,
                "last_activity": row.last_activity.isoformat() if row.last_activity else None,
            })

        return instances
