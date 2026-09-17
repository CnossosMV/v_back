"""
One-shot data migration: rewrite legacy funnel `action` steps that carry
`action_type in ('send_template', 'send_whatsapp_message')` into proper
`send_message` steps (notification mode — expect_reply=false, handoff=none).

Funnels should use the `send_message` step for messaging. The `action` step is
reserved for side-effects (webhook / update_user / add_tag / api_call).

Usage:
    docker exec -w /app customer-backend python -m scripts.migrate_funnel_action_sends [--dry-run]

Idempotent: rows already converted (step_type != 'action') are skipped.
"""
import argparse
import logging
import sys
from typing import Any, Dict

from sqlalchemy.orm.attributes import flag_modified

from app.database import SessionLocal
from app.models import FunnelStep
from app.models.messaging import MessagingTemplate

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _convert_send_template(inner_cfg: Dict[str, Any], template: MessagingTemplate | None) -> Dict[str, Any]:
    """Build a send_message step_config from a send_template action config."""
    channel = "email"
    if template and template.channel_type:
        channel = template.channel_type.value
    return {
        "channel": channel,
        "message_type": "template" if (template and template.meta_template_name) else "template",
        "template_id": inner_cfg.get("template_id"),
        "recipient_field": inner_cfg.get("recipient_field", "email" if channel == "email" else "phone"),
        "variable_mapping": inner_cfg.get("variable_mapping") or {},
        "expect_reply": False,
        "handoff_type": "none",
        "instance_id": None,
        "email_instance_id": None,
        "_migrated_from": "action:send_template",
    }


def _convert_send_whatsapp(inner_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Build a send_message step_config from a send_whatsapp_message action config."""
    message_type = inner_cfg.get("message_type", "text")
    return {
        "channel": "whatsapp",
        "instance_id": inner_cfg.get("instance_id"),
        "message_type": message_type,
        "template_name": inner_cfg.get("template_name"),
        "template_language": inner_cfg.get("template_language", "en_US"),
        "template_components": inner_cfg.get("template_components"),
        "message": inner_cfg.get("message"),
        "recipient_field": inner_cfg.get("recipient_field", "phone"),
        "expect_reply": False,
        "handoff_type": "none",
        "_migrated_from": "action:send_whatsapp_message",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    args = parser.parse_args()

    db = SessionLocal()
    converted = 0
    skipped = 0
    try:
        steps = (
            db.query(FunnelStep)
            .filter(FunnelStep.step_type == "action")
            .all()
        )
        for step in steps:
            cfg = step.step_config or {}
            action_type = cfg.get("action_type")
            inner = cfg.get("config") or {}

            if action_type == "send_template":
                template = None
                tid = inner.get("template_id")
                if tid:
                    template = db.query(MessagingTemplate).filter(MessagingTemplate.id == tid).first()
                new_cfg = _convert_send_template(inner, template)
            elif action_type == "send_whatsapp_message":
                new_cfg = _convert_send_whatsapp(inner)
            else:
                skipped += 1
                continue

            logger.info(
                "Funnel %s step %s: action_type=%s -> send_message (%s)",
                step.funnel_id, step.id, action_type, new_cfg.get("channel"),
            )
            if args.dry_run:
                converted += 1
                continue

            step.step_type = "send_message"
            step.step_config = new_cfg
            flag_modified(step, "step_config")
            converted += 1

        if args.dry_run:
            logger.info("DRY-RUN: would convert %d step(s); %d non-send action step(s) unchanged", converted, skipped)
        else:
            db.commit()
            logger.info("Converted %d step(s); %d non-send action step(s) unchanged", converted, skipped)
        return 0
    except Exception as e:
        db.rollback()
        logger.exception("Migration failed: %s", e)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
