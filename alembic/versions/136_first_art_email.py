"""Seed Tabloide first-art email variants and event action.

Revision ID: 136_first_art_email
Revises: 135_send_windows
Create Date: 2026-08-18
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

from alembic_event_action_compiler import compile_event_action_for_migration


revision = "136_first_art_email"
down_revision = "135_send_windows"
branch_labels = None
depends_on = None


SLUG = "first-art-no-export-24h"
TRIGGER_EVENT = "tabloide.campaign.first_art_no_export"
SENDER_EMAIL = "rafael@tabloide.pro"

COPY = {
    "pt-BR": {
        "name": "Primeira arte sem exportacao - 24h",
        "subject": "Sua arte continua aqui, pronta para baixar",
        "greeting": "Oi",
        "intro": "Sua arte continua salva no Tabloide. Você pode abri-la, editar os detalhes e baixar quando quiser.",
        "trial": "E não se esqueça: seu período de teste dura {{trial_duration_days}} dias a partir do cadastro. Você ainda tem {{trial_days_remaining}} dias e pode criar mais {{additional_art_count}} artes durante esse período.",
        "cta": "Ver, editar ou baixar minha arte",
        "team": "Equipe Tabloide.pro",
        "unsubscribe": "Se não quiser receber estes avisos, cancele o recebimento.",
        "from_name": "Rafael - Tabloide",
    },
    "en-US": {
        "name": "First art without export - 24h",
        "subject": "Your first design is ready to download",
        "greeting": "Hi",
        "intro": "Your first Tabloide design is ready. Open it to edit the details and download it whenever you are ready.",
        "trial": "Your trial lasts {{trial_duration_days}} days from signup. You still have {{trial_days_remaining}} days and can create {{additional_art_count}} more designs during the trial.",
        "cta": "View, edit, or download my design",
        "team": "The Tabloide team",
        "unsubscribe": "If you no longer want these messages, unsubscribe here.",
        "from_name": "Rafael from Tabloide",
    },
    "es-US": {
        "name": "Primera pieza sin exportar - 24h",
        "subject": "Tu primera pieza está lista para descargar",
        "greeting": "Hola",
        "intro": "Tu primera pieza de Tabloide está lista. Ábrela para editar los detalles y descargarla cuando quieras.",
        "trial": "Tu prueba dura {{trial_duration_days}} días desde el registro. Aún tienes {{trial_days_remaining}} días y puedes crear {{additional_art_count}} piezas más durante la prueba.",
        "cta": "Ver, editar o descargar mi pieza",
        "team": "El equipo de Tabloide",
        "unsubscribe": "Si ya no quieres recibir estos mensajes, cancela la suscripción aquí.",
        "from_name": "Rafael from Tabloide",
    },
}


def _project_id(conn) -> int | None:
    return conn.execute(
        sa.text("SELECT id FROM projects WHERE lower(name) LIKE :name ORDER BY id LIMIT 1"),
        {"name": "%tabloide%"},
    ).scalar()


def _html(locale: str, copy: dict[str, str]) -> str:
    return (
        f'<!doctype html><html lang="{locale}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;background:#f7f7f5;font-family:Arial,Helvetica,sans-serif;color:#1f2933;">'
        '<main style="max-width:640px;margin:0 auto;background:#ffffff;padding:32px 28px;">'
        '<p style="font-size:16px;line-height:1.6;margin:0 0 18px;">'
        f'{copy["greeting"]}, {{{{first_name}}}}.</p>'
        f'<p style="font-size:16px;line-height:1.6;margin:0 0 18px;">{copy["intro"]}</p>'
        '<a href="{{cta_url}}" style="display:block;text-decoration:none;margin:24px 0;">'
        '<img src="{{preview_image_url}}" alt="{{design_name}}" width="584" '
        'style="display:block;width:100%;max-width:584px;height:auto;border:1px solid #e5e7eb;border-radius:10px;">'
        '</a>'
        f'<p style="font-size:16px;line-height:1.6;margin:0 0 18px;">{copy["trial"]}</p>'
        '<p style="margin:28px 0;"><a href="{{cta_url}}" '
        'style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;'
        f'padding:13px 18px;border-radius:6px;font-weight:700;">{copy["cta"]}</a></p>'
        f'<p style="font-size:14px;line-height:1.6;color:#6b7280;margin:24px 0 0;">{copy["team"]}</p>'
        '<p style="font-size:12px;line-height:1.5;color:#9ca3af;margin:18px 0 0;">'
        f'<a href="{{{{unsubscribe_url}}}}" style="color:#6b7280;text-decoration:underline;">{copy["unsubscribe"]}</a>'
        '</p></main></body></html>'
    )


def _upsert_template(conn, project_id: int, locale: str, copy: dict[str, str]) -> int:
    existing = conn.execute(
        sa.text(
            """
            SELECT id FROM messaging_templates
            WHERE project_id=:project_id AND slug=:slug AND locale=:locale
            ORDER BY id LIMIT 1
            """
        ),
        {"project_id": project_id, "slug": SLUG, "locale": locale},
    ).scalar()
    values = {
        "project_id": project_id,
        "slug": SLUG,
        "locale": locale,
        "name": f'{copy["name"]} ({locale})',
        "subject": copy["subject"],
        "body": _html(locale, copy),
        "from_name": copy["from_name"],
    }
    if existing:
        values["id"] = existing
        conn.execute(
            sa.text(
                """
                UPDATE messaging_templates
                SET name=:name, subject=:subject, body=:body, channel_type='email',
                    from_email=:sender, from_name=:from_name, reply_to=:sender,
                    body_format='html', is_active=true, source_locale='pt-BR',
                    translation_status='reviewed', translated_at=NOW(), updated_at=NOW()
                WHERE id=:id
                """
            ),
            {**values, "sender": SENDER_EMAIL},
        )
        return int(existing)
    return int(
        conn.execute(
            sa.text(
                """
                INSERT INTO messaging_templates (
                    project_id, slug, name, subject, channel_type, from_email,
                    from_name, reply_to, body_format, body, is_active, locale,
                    source_locale, translation_status, translated_at,
                    created_at, updated_at
                ) VALUES (
                    :project_id, :slug, :name, :subject, 'email', :sender,
                    :from_name, :sender, 'html', :body, true, :locale,
                    'pt-BR', 'reviewed', NOW(), NOW(), NOW()
                ) RETURNING id
                """
            ),
            {**values, "sender": SENDER_EMAIL},
        ).scalar_one()
    )


def _upsert_event_action(conn, project_id: int, template_id: int) -> int:
    actions = [{
        "type": "send_template",
        "config": {
            "template_id": template_id,
            "recipient_field": "email",
            "variable_mapping": {"first_name": "user_name"},
            "use_direct_smtp": True,
        },
        "delay_seconds": 0,
    }]
    existing = conn.execute(
        sa.text(
            """
            SELECT id FROM event_actions
            WHERE project_id=:project_id AND trigger_event=:trigger
            ORDER BY id LIMIT 1
            """
        ),
        {"project_id": project_id, "trigger": TRIGGER_EVENT},
    ).scalar()
    values = {
        "project_id": project_id,
        "trigger": TRIGGER_EVENT,
        "actions": json.dumps(actions),
        "name": "[Tabloide] First art without export email",
        "description": "Email 24h into an active trial after one AI art and no export.",
    }
    if existing:
        values["id"] = existing
        conn.execute(
            sa.text(
                """
                UPDATE event_actions
                SET name=:name, description=:description, conditions='[]'::json,
                    actions=CAST(:actions AS json), stop_conditions='[]'::json,
                    is_active=true, priority=70, cooldown_seconds=315360000,
                    react_to_delivery=false, updated_at=NOW()
                WHERE id=:id
                """
            ),
            values,
        )
        return int(existing)
    return int(
        conn.execute(
            sa.text(
                """
                INSERT INTO event_actions (
                    project_id, name, description, trigger_event, conditions,
                    actions, stop_conditions, is_active, priority,
                    cooldown_seconds, react_to_delivery, created_at, updated_at
                ) VALUES (
                    :project_id, :name, :description, :trigger, '[]'::json,
                    CAST(:actions AS json), '[]'::json, true, 70,
                    315360000, false, NOW(), NOW()
                ) RETURNING id
                """
            ),
            values,
        ).scalar_one()
    )


def upgrade() -> None:
    conn = op.get_bind()
    project_id = _project_id(conn)
    if project_id is None:
        return
    template_ids = {
        locale: _upsert_template(conn, project_id, locale, copy)
        for locale, copy in COPY.items()
    }
    event_action_id = _upsert_event_action(conn, project_id, template_ids["pt-BR"])
    compile_event_action_for_migration(conn, event_action_id)


def downgrade() -> None:
    conn = op.get_bind()
    project_id = _project_id(conn)
    if project_id is None:
        return
    conn.execute(
        sa.text(
            """
            UPDATE event_actions SET is_active=false, updated_at=NOW()
            WHERE project_id=:project_id AND trigger_event=:trigger
            """
        ),
        {"project_id": project_id, "trigger": TRIGGER_EVENT},
    )
    conn.execute(
        sa.text(
            """
            UPDATE messaging_templates SET is_active=false, updated_at=NOW()
            WHERE project_id=:project_id AND slug=:slug
            """
        ),
        {"project_id": project_id, "slug": SLUG},
    )
