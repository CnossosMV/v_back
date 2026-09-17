"""Seed Tabloide locale variants and market-safe event actions.

Revision ID: 125_tabloide_i18n
Revises: 124_placeholder_ptn
Create Date: 2026-07-14
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "125_tabloide_i18n"
down_revision = "124_placeholder_ptn"
branch_labels = None
depends_on = None

GUEST_EMAIL_PATTERN = r"^(guest|anonymous|visitor)([._+\-].*)?@|@guest\."
SENDER_EMAIL = "rafael@tabloide.pro"
BR_SENDER_NAME = "Rafael - Tabloide"
US_SENDER_NAME = "Rafael from Tabloide"

COPY: dict[str, dict[str, Any]] = {
    "welcome-email": {
        "name": "Welcome",
        "url": "login_url",
        "pt-BR": ("Se precisar de ajuda...", [
            "Sua conta no Tabloide est\u00e1 pronta.",
            "Entre para criar campanhas profissionais com IA e divulgar as ofertas da sua loja.",
        ], "Acessar o Tabloide"),
        "en-US": ("Welcome to Tabloide", [
            "Your Tabloide account is ready.",
            "Sign in to create professional retail campaigns with AI and share your store's offers.",
        ], "Open Tabloide"),
        "es-US": ("Bienvenido a Tabloide", [
            "Tu cuenta de Tabloide est\u00e1 lista.",
            "Ingresa para crear campa\u00f1as profesionales con IA y compartir las ofertas de tu tienda.",
        ], "Abrir Tabloide"),
    },
    "trial-no-design-24h": {
        "name": "Trial no design 24h",
        "url": "dashboard_url",
        "pt-BR": ("N\u00e3o deixe de divulgar", [
            "Sua conta j\u00e1 est\u00e1 pronta, mas voc\u00ea ainda n\u00e3o criou a primeira campanha.",
            "Abra o Tabloide, escolha um modelo e deixe a IA montar uma pe\u00e7a para divulgar as ofertas da sua loja.",
        ], "Criar minha primeira campanha"),
        "en-US": ("Your first campaign is waiting", [
            "Your account is ready, but you have not created your first campaign yet.",
            "Open Tabloide, choose a template, and let AI build a campaign for your store's offers.",
        ], "Create my first campaign"),
        "es-US": ("Tu primera campa\u00f1a te espera", [
            "Tu cuenta est\u00e1 lista, pero todav\u00eda no creaste tu primera campa\u00f1a.",
            "Abre Tabloide, elige una plantilla y deja que la IA prepare una campa\u00f1a para las ofertas de tu tienda.",
        ], "Crear mi primera campa\u00f1a"),
    },
    "last-48h": {
        "name": "Trial expiring 48h",
        "url": "dashboard_url",
        "pt-BR": ("Voc\u00ea ainda tem 48h para testar", [
            "Seu teste do Tabloide est\u00e1 chegando ao fim, mas ainda d\u00e1 tempo de criar uma campanha com IA.",
            "Entre agora, escolha um modelo e veja sua pr\u00f3xima promo\u00e7\u00e3o pronta em poucos minutos.",
        ], "Criar campanha agora"),
        "en-US": ("You still have 48 hours to try Tabloide", [
            "Your Tabloide trial is almost over, but there is still time to create a campaign with AI.",
            "Sign in now, choose a template, and get your next promotion ready in minutes.",
        ], "Create a campaign"),
        "es-US": ("A\u00fan tienes 48 horas para probar Tabloide", [
            "Tu prueba de Tabloide est\u00e1 por terminar, pero a\u00fan tienes tiempo para crear una campa\u00f1a con IA.",
            "Ingresa ahora, elige una plantilla y prepara tu pr\u00f3xima promoci\u00f3n en minutos.",
        ], "Crear una campa\u00f1a"),
    },
    "renewal-reminder": {
        "name": "Renewal reminder",
        "url": "renew_url",
        "pt-BR": ("Sua assinatura {{plan_name}} vence em {{expiration_date}}", [
            "Sua assinatura do {{plan_name}} vence em {{expiration_date}}.",
            "Renove para manter o acesso e continuar criando campanhas com IA sem interrup\u00e7\u00e3o.",
        ], "Renovar assinatura"),
        "en-US": ("Your {{plan_name}} subscription expires on {{expiration_date}}", [
            "Your {{plan_name}} subscription expires on {{expiration_date}}.",
            "Renew to keep access and continue creating AI campaigns without interruption.",
        ], "Renew subscription"),
        "es-US": ("Tu suscripci\u00f3n {{plan_name}} vence el {{expiration_date}}", [
            "Tu suscripci\u00f3n {{plan_name}} vence el {{expiration_date}}.",
            "Renueva para mantener el acceso y seguir creando campa\u00f1as con IA sin interrupciones.",
        ], "Renovar suscripci\u00f3n"),
    },
    "subscription-expired": {
        "name": "Subscription expired",
        "url": "renew_url",
        "pt-BR": ("Sua assinatura {{plan_name}} expirou", [
            "Sua assinatura do {{plan_name}} expirou em {{expiration_date}}. Seus encartes e dados continuam salvos.",
            "Reative sua assinatura para voltar a criar e divulgar as ofertas da loja.",
        ], "Reativar agora"),
        "en-US": ("Your {{plan_name}} subscription has expired", [
            "Your {{plan_name}} subscription expired on {{expiration_date}}. Your campaigns and data are still saved.",
            "Reactivate your subscription to start creating and sharing your store's offers again.",
        ], "Reactivate now"),
        "es-US": ("Tu suscripci\u00f3n {{plan_name}} venci\u00f3", [
            "Tu suscripci\u00f3n {{plan_name}} venci\u00f3 el {{expiration_date}}. Tus campa\u00f1as y datos siguen guardados.",
            "Reactiva tu suscripci\u00f3n para volver a crear y compartir las ofertas de tu tienda.",
        ], "Reactivar ahora"),
    },
    "starter-downsell": {
        "name": "Starter downsell",
        "url": "destination_url",
        "pt-BR": ("Liberamos uma condi\u00e7\u00e3o mais leve para voc\u00ea come\u00e7ar", [
            "Se o valor pesou na decis\u00e3o, liberamos o plano Starter por {{starter_price}} para sua conta.",
            "\u00c9 uma forma de come\u00e7ar com um compromisso menor e subir de plano quando o uso crescer.",
        ], "Ver o plano Starter"),
        "en-US": ("A lighter way to get started", [
            "If price was holding you back, the Starter plan is now available to your account for {{starter_price}}.",
            "Start with a smaller commitment and upgrade whenever your usage grows.",
        ], "View the Starter plan"),
        "es-US": ("Una opci\u00f3n m\u00e1s accesible para comenzar", [
            "Si el precio influy\u00f3 en tu decisi\u00f3n, el plan Starter est\u00e1 disponible para tu cuenta por {{starter_price}}.",
            "Comienza con un compromiso menor y cambia de plan cuando aumente tu uso.",
        ], "Ver el plan Starter"),
    },
    "price-returned": {
        "name": "Price returned",
        "url": "cta_url",
        "pt-BR": ("Ficou em d\u00favida sobre qual plano escolher?", [
            "Voc\u00ea voltou para olhar os planos do Tabloide. O Pro Anual costuma oferecer o melhor custo para quem cria campanhas com frequ\u00eancia.",
            "Se preferir come\u00e7ar menor, o plano Mensal tamb\u00e9m continua dispon\u00edvel.",
        ], "Ver planos"),
        "en-US": ("Still deciding which plan fits best?", [
            "You came back to review Tabloide plans. Pro Annual usually offers the best value for stores creating campaigns regularly.",
            "If you prefer to start smaller, the Monthly plan is also available.",
        ], "View plans"),
        "es-US": ("\u00bfA\u00fan est\u00e1s eligiendo el mejor plan?", [
            "Volviste para revisar los planes de Tabloide. Pro Anual suele ofrecer el mejor valor para tiendas que crean campa\u00f1as con frecuencia.",
            "Si prefieres comenzar con algo menor, el plan Mensual tambi\u00e9n est\u00e1 disponible.",
        ], "Ver planes"),
    },
    "checkout-abandoned": {
        "name": "Checkout abandoned",
        "url": "checkout_resume_url",
        "pt-BR": ("Seu plano no Tabloide ficou separado", [
            "Seu plano ficou separado e voc\u00ea pode continuar de onde parou.",
            "Quando o pagamento for confirmado, seu acesso ser\u00e1 liberado para continuar criando campanhas.",
        ], "Concluir meu plano"),
        "en-US": ("Your Tabloide plan is still waiting", [
            "Your selected plan is still available, and you can continue right where you left off.",
            "Once payment is confirmed, your access will be ready for you to keep creating campaigns.",
        ], "Complete my purchase"),
        "es-US": ("Tu plan de Tabloide sigue disponible", [
            "El plan que elegiste sigue disponible y puedes continuar donde lo dejaste.",
            "Cuando se confirme el pago, tendr\u00e1s acceso para seguir creando campa\u00f1as.",
        ], "Completar mi compra"),
    },
    "credits-exhausted-step1": {
        "name": "Credits exhausted step 1",
        "url": "cta_url",
        "pt-BR": ("Seus cr\u00e9ditos acabaram, mas voc\u00ea pode continuar criando", [
            "Voc\u00ea usou todos os cr\u00e9ditos de IA do teste. Isso \u00e9 um bom sinal: colocou o Tabloide para trabalhar na pr\u00e1tica.",
            "Assine agora e receba +{{paid_bonus_credits}} cr\u00e9ditos extras no primeiro ciclo.",
        ], "Continuar criando"),
        "en-US": ("You used your AI credits, but you can keep creating", [
            "You used all your trial AI credits. That is a good sign: you put Tabloide to work.",
            "Subscribe now and receive +{{paid_bonus_credits}} extra credits in your first cycle.",
        ], "Keep creating"),
        "es-US": ("Usaste tus cr\u00e9ditos de IA, pero puedes seguir creando", [
            "Usaste todos los cr\u00e9ditos de IA de la prueba. Es una buena se\u00f1al: pusiste Tabloide a trabajar.",
            "Suscr\u00edbete ahora y recibe +{{paid_bonus_credits}} cr\u00e9ditos extra en tu primer ciclo.",
        ], "Seguir creando"),
    },
    "credits-exhausted-step2": {
        "name": "Credits exhausted step 2",
        "url": "cta_url",
        "pt-BR": ("Liberamos uma condi\u00e7\u00e3o melhor para voc\u00ea continuar", [
            "Como voc\u00ea usou todos os cr\u00e9ditos de IA e ainda n\u00e3o assinou, liberamos a melhor condi\u00e7\u00e3o dispon\u00edvel para sua conta.",
            "No Pro Anual, voc\u00ea recebe +{{paid_bonus_credits}} cr\u00e9ditos extras. No Mensal, recebe +{{monthly_bonus_credits}} cr\u00e9ditos.",
        ], "Ver minha condi\u00e7\u00e3o"),
        "en-US": ("A better offer is available for you", [
            "You used all your AI credits and have not subscribed yet, so we unlocked the best offer available for your account.",
            "Pro Annual includes +{{paid_bonus_credits}} extra credits. Monthly includes +{{monthly_bonus_credits}} credits.",
        ], "View my offer"),
        "es-US": ("Tienes una oferta mejor disponible", [
            "Usaste todos tus cr\u00e9ditos de IA y a\u00fan no te suscribiste, as\u00ed que liberamos la mejor oferta disponible para tu cuenta.",
            "Pro Anual incluye +{{paid_bonus_credits}} cr\u00e9ditos extra. Mensual incluye +{{monthly_bonus_credits}} cr\u00e9ditos.",
        ], "Ver mi oferta"),
    },
    "return-recency-whatsapp": {
        "name": "Return recency reopen",
        "url": "cta_url",
        "pt-BR": ("Ainda d\u00e1 para testar mais uma campanha", [
            "Se voc\u00ea ainda n\u00e3o se convenceu, d\u00e1 para testar mais uma campanha antes de decidir.",
            "Volte ao Tabloide e veja a condi\u00e7\u00e3o dispon\u00edvel para sua conta.",
        ], "Reabrir meu teste"),
        "en-US": ("There is still time to try one more campaign", [
            "If you are still deciding, you can try one more campaign before choosing a plan.",
            "Return to Tabloide and see the offer available for your account.",
        ], "Try one more campaign"),
        "es-US": ("A\u00fan puedes probar una campa\u00f1a m\u00e1s", [
            "Si todav\u00eda est\u00e1s decidiendo, puedes probar una campa\u00f1a m\u00e1s antes de elegir un plan.",
            "Vuelve a Tabloide y revisa la oferta disponible para tu cuenta.",
        ], "Probar una campa\u00f1a m\u00e1s"),
    },
    "return-recency-neutral": {
        "name": "Return recency neutral",
        "url": "cta_url",
        "pt-BR": ("Sua pr\u00f3xima promo\u00e7\u00e3o pode sair hoje", [
            "O Tabloide ajuda sua loja a criar encartes profissionais com IA em minutos.",
            "Volte quando fizer sentido e escolha o plano que combina com a rotina da sua loja.",
        ], "Voltar ao Tabloide"),
        "en-US": ("Your next promotion can be ready today", [
            "Tabloide helps your store create professional retail campaigns with AI in minutes.",
            "Come back when you are ready and choose the plan that fits your store.",
        ], "Return to Tabloide"),
        "es-US": ("Tu pr\u00f3xima promoci\u00f3n puede estar lista hoy", [
            "Tabloide ayuda a tu tienda a crear campa\u00f1as profesionales con IA en minutos.",
            "Vuelve cuando quieras y elige el plan que mejor se adapte a tu tienda.",
        ], "Volver a Tabloide"),
    },
    "pro-monthly-intro": {
        "name": "Pro Monthly Intro",
        "url": "cta_url",
        "source_slug": "price-returned",
        "pt-BR": ("Uma condi\u00e7\u00e3o mensal especial est\u00e1 dispon\u00edvel", [
            "Sua oferta anual expirou, mas liberamos o Pro Mensal por {{promotional_price_label}} nos primeiros {{promotional_cycles}} ciclos.",
            "Depois, o plano renova por {{regular_price_label}} ao m\u00e1s. Esta condi\u00e7\u00e3o fica dispon\u00edvel por {{validity_hours}} horas.",
        ], "Ver minha condi\u00e7\u00e3o"),
        "en-US": ("Pro Monthly: {{promotional_price_label}} for your first {{promotional_cycles}} billing cycles", [
            "Your annual welcome offer expired, but Pro Monthly is now available for {{promotional_price_label}} during your first {{promotional_cycles}} billing cycles.",
            "After that, it renews at {{regular_price_label}} per month. This offer is available for {{validity_hours}} hours.",
        ], "Claim my offer"),
        "es-US": ("Pro Mensual: {{promotional_price_label}} durante tus primeros {{promotional_cycles}} ciclos", [
            "Tu oferta anual de bienvenida venci\u00f3, pero Pro Mensual est\u00e1 disponible por {{promotional_price_label}} durante tus primeros {{promotional_cycles}} ciclos.",
            "Despu\u00e9s, se renueva por {{regular_price_label}} al mes. Esta oferta est\u00e1 disponible durante {{validity_hours}} horas.",
        ], "Obtener mi oferta"),
    },
}


def _html(locale: str, paragraphs: list[str], cta_text: str, url_var: str) -> str:
    greeting = {"pt-BR": "Oi", "en-US": "Hi", "es-US": "Hola"}[locale]
    team = {"pt-BR": "Equipe Tabloide.pro", "en-US": "The Tabloide team", "es-US": "El equipo de Tabloide"}[locale]
    unsubscribe = {
        "pt-BR": "Se n\u00e3o quiser receber estes avisos, cancele o recebimento.",
        "en-US": "If you no longer want these messages, unsubscribe here.",
        "es-US": "Si ya no quieres recibir estos mensajes, cancela la suscripci\u00f3n aqu\u00ed.",
    }[locale]
    body = "".join(
        f'<p style="font-size:16px;line-height:1.6;margin:0 0 18px;">{paragraph}</p>'
        for paragraph in paragraphs
    )
    return (
        f'<!doctype html><html lang="{locale}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;background:#f7f7f5;font-family:Arial,Helvetica,sans-serif;color:#1f2933;">'
        '<main style="max-width:640px;margin:0 auto;background:#ffffff;padding:32px 28px;">'
        f'<p style="font-size:16px;line-height:1.6;margin:0 0 18px;">{greeting}, {{{{first_name}}}}.</p>'
        f'{body}<p style="margin:28px 0;"><a href="{{{{{url_var}}}}}" '
        'style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;'
        f'padding:13px 18px;border-radius:6px;font-weight:700;">{cta_text}</a></p>'
        f'<p style="font-size:14px;line-height:1.6;color:#6b7280;margin:24px 0 0;">{team}</p>'
        '<p style="font-size:12px;line-height:1.5;color:#9ca3af;margin:18px 0 0;">'
        f'<a href="{{{{unsubscribe_url}}}}" style="color:#6b7280;text-decoration:underline;">{unsubscribe}</a>'
        '</p></main></body></html>'
    )


def _project_id(conn) -> int | None:
    return conn.execute(
        sa.text("SELECT id FROM projects WHERE lower(name) LIKE :name ORDER BY id LIMIT 1"),
        {"name": "%tabloide%"},
    ).scalar()


def _template_row(conn, project_id: int, slug: str, locale: str | None = None):
    if locale is None:
        return conn.execute(
            sa.text(
                """
                SELECT * FROM messaging_templates
                WHERE project_id=:project_id AND slug=:slug
                ORDER BY CASE WHEN locale='pt-BR' THEN 0 WHEN locale IS NULL THEN 1 ELSE 2 END, id
                LIMIT 1
                """
            ),
            {"project_id": project_id, "slug": slug},
        ).mappings().first()
    return conn.execute(
        sa.text(
            """
            SELECT * FROM messaging_templates
            WHERE project_id=:project_id AND slug=:slug AND locale=:locale
            ORDER BY id LIMIT 1
            """
        ),
        {"project_id": project_id, "slug": slug, "locale": locale},
    ).mappings().first()



def _normalize_source_locales(conn, project_id: int) -> None:
    slugs = {slug for slug in COPY}
    slugs.update(spec.get("source_slug", slug) for slug, spec in COPY.items())
    for slug in sorted(slugs):
        conn.execute(
            sa.text(
                """
                UPDATE messaging_templates
                SET locale='pt-BR', source_locale='pt-BR', updated_at=NOW()
                WHERE project_id=:project_id AND slug=:slug AND locale IS NULL
                  AND id=(
                    SELECT MIN(id) FROM messaging_templates
                    WHERE project_id=:project_id AND slug=:slug AND locale IS NULL
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM messaging_templates existing
                    WHERE existing.project_id=:project_id
                      AND existing.slug=:slug AND existing.locale='pt-BR'
                  )
                """
            ),
            {"project_id": project_id, "slug": slug},
        )
        conn.execute(
            sa.text(
                """
                UPDATE messaging_templates
                SET is_active=false, updated_at=NOW()
                WHERE project_id=:project_id AND slug=:slug
                  AND locale IS NULL AND is_active=true
                  AND EXISTS (
                    SELECT 1 FROM messaging_templates existing
                    WHERE existing.project_id=:project_id
                      AND existing.slug=:slug AND existing.locale='pt-BR'
                  )
                """
            ),
            {"project_id": project_id, "slug": slug},
        )
def _upsert_variant(conn, project_id: int, slug: str, locale: str, source, spec) -> int:
    subject, paragraphs, cta_text = spec[locale]
    body = _html(locale, paragraphs, cta_text, spec["url"])
    existing = _template_row(conn, project_id, slug, locale)
    values = {
        "project_id": project_id,
        "slug": slug,
        "locale": locale,
        "name": f'{spec["name"]} ({locale})',
        "subject": subject,
        "body": body,
        "from_email": SENDER_EMAIL,
        "from_name": BR_SENDER_NAME if locale == "pt-BR" else US_SENDER_NAME,
        "reply_to": source["reply_to"] or SENDER_EMAIL,
        "source_locale": "pt-BR",
        "translation_status": "reviewed",
    }
    if existing:
        values["id"] = existing["id"]
        conn.execute(
            sa.text(
                """
                UPDATE messaging_templates
                SET name=:name, subject=:subject, body=:body, locale=:locale,
                    source_locale=:source_locale, translation_status=:translation_status,
                    translated_at=NOW(), from_email=:from_email, from_name=:from_name,
                    reply_to=:reply_to, is_active=true, updated_at=NOW()
                WHERE id=:id
                """
            ),
            values,
        )
        return int(existing["id"])

    values["source_id"] = source["id"]
    return int(
        conn.execute(
            sa.text(
                """
                INSERT INTO messaging_templates (
                  project_id, channel_id, slug, name, subject, channel_type,
                  from_email, from_name, reply_to, folder, media_url, body_format,
                  body, template_metadata, trigger_events, is_active, locale,
                  source_locale, translation_status, translated_at,
                  meta_template_name, meta_language, meta_components,
                  whatsapp_instance_id, external_source, external_last_synced_at,
                  created_at, updated_at
                )
                SELECT
                  :project_id, channel_id, :slug, :name, :subject, channel_type,
                  :from_email, :from_name, :reply_to, folder, media_url, body_format,
                  :body, template_metadata, trigger_events, true, :locale,
                  :source_locale, :translation_status, NOW(),
                  NULL, NULL, NULL, NULL, external_source, external_last_synced_at,
                  NOW(), NOW()
                FROM messaging_templates WHERE id=:source_id
                RETURNING id
                """
            ),
            values,
        ).scalar_one()
    )


def _seed_templates(conn, project_id: int) -> dict[str, int]:
    pt_template_ids: dict[str, int] = {}
    for slug, spec in COPY.items():
        source_slug = spec.get("source_slug", slug)
        source = _template_row(conn, project_id, slug) or _template_row(conn, project_id, source_slug)
        if source is None:
            raise RuntimeError(f"Missing source template for Tabloide family: {slug}")
        for locale in ("pt-BR", "en-US", "es-US"):
            pt_or_variant_id = _upsert_variant(conn, project_id, slug, locale, source, spec)
            if locale == "pt-BR":
                pt_template_ids[slug] = pt_or_variant_id
    return pt_template_ids


def _seed_monthly_intro_action(conn, project_id: int, template_id: int) -> None:
    conditions = [
        {"field": "country", "operator": "==", "value": "US"},
        {"field": "user_email", "operator": "not_matches", "value": GUEST_EMAIL_PATTERN},
    ]
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
    stop_conditions = [
        {"event": "billing.payment.completed", "within_seconds": 259200},
        {"event": "payment.completed", "within_seconds": 259200},
    ]
    existing = conn.execute(
        sa.text(
            """
            SELECT id FROM event_actions
            WHERE project_id=:project_id
              AND trigger_event='tabloide.campaign.pro_monthly_intro'
            ORDER BY id LIMIT 1
            """
        ),
        {"project_id": project_id},
    ).scalar()
    values = {
        "project_id": project_id,
        "name": "[Tabloide US] Pro Monthly Intro email",
        "description": "US lifecycle offer after WELCOME_US_12 expires without purchase.",
        "conditions": json.dumps(conditions),
        "actions": json.dumps(actions),
        "stops": json.dumps(stop_conditions),
    }
    if existing:
        values["id"] = existing
        conn.execute(
            sa.text(
                """
                UPDATE event_actions
                SET name=:name, description=:description,
                    conditions=CAST(:conditions AS json), actions=CAST(:actions AS json),
                    stop_conditions=CAST(:stops AS json), cooldown_seconds=315360000,
                    is_active=true, updated_at=NOW()
                WHERE id=:id
                """
            ),
            values,
        )
    else:
        conn.execute(
            sa.text(
                """
                INSERT INTO event_actions (
                  project_id, name, description, trigger_event, conditions, actions,
                  stop_conditions, is_active, priority, cooldown_seconds,
                  react_to_delivery, created_at, updated_at
                ) VALUES (
                  :project_id, :name, :description,
                  'tabloide.campaign.pro_monthly_intro',
                  CAST(:conditions AS json), CAST(:actions AS json), CAST(:stops AS json),
                  true, 100, 315360000, false, NOW(), NOW()
                )
                """
            ),
            values,
        )


def _restrict_whatsapp_to_br(conn, project_id: int) -> None:
    rows = conn.execute(
        sa.text(
            """
            SELECT id, conditions, actions
            FROM event_actions
            WHERE project_id=:project_id AND is_active=true
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    for row in rows:
        actions = row["actions"] or []
        is_whatsapp = False
        for action in actions:
            template_id = (action.get("config") or {}).get("template_id")
            if not template_id:
                continue
            channel = conn.execute(
                sa.text("SELECT channel_type::text FROM messaging_templates WHERE id=:id"),
                {"id": template_id},
            ).scalar()
            if channel == "whatsapp":
                is_whatsapp = True
                break
        if not is_whatsapp:
            continue
        conditions = list(row["conditions"] or [])
        conditions = [c for c in conditions if c.get("field") not in ("country", "market_country")]
        conditions.append({"field": "country", "operator": "==", "value": "BR"})
        conn.execute(
            sa.text(
                "UPDATE event_actions SET conditions=CAST(:conditions AS json), updated_at=NOW() WHERE id=:id"
            ),
            {"conditions": json.dumps(conditions), "id": row["id"]},
        )


def _assert_no_mojibake(conn, project_id: int) -> None:
    slugs = tuple(COPY)
    rows = conn.execute(
        sa.text(
            """
            SELECT slug, locale, subject, body
            FROM messaging_templates
            WHERE project_id=:project_id
            """
        ),
        {"project_id": project_id},
    ).mappings()
    for row in rows:
        if row["slug"] not in slugs:
            continue
        text = f'{row["subject"] or ""} {row["body"] or ""}'
        if any(marker in text for marker in ("\u00c3", "\u00c2", "\u00e2\u20ac", "\ufffd")):
            raise RuntimeError(f'Mojibake remains in {row["slug"]}/{row["locale"]}')


def upgrade() -> None:
    conn = op.get_bind()
    project_id = _project_id(conn)
    if project_id is None:
        return

    conn.execute(
        sa.text(
            """
            UPDATE projects
            SET default_locale='pt-BR',
                supported_locales=CAST(:locales AS json)
            WHERE id=:project_id
            """
        ),
        {"locales": json.dumps(["pt-BR", "en-US", "es-US"]), "project_id": project_id},
    )
    _normalize_source_locales(conn, project_id)
    template_ids = _seed_templates(conn, project_id)
    _seed_monthly_intro_action(conn, project_id, template_ids["pro-monthly-intro"])
    _restrict_whatsapp_to_br(conn, project_id)
    _assert_no_mojibake(conn, project_id)


def downgrade() -> None:
    conn = op.get_bind()
    project_id = _project_id(conn)
    if project_id is None:
        return
    # Preserve seeded variants on rollback: existing reviewed rows cannot be
    # distinguished safely from rows inserted by this data migration.
    conn.execute(
        sa.text(
            """
            UPDATE event_actions
            SET is_active=false, updated_at=NOW()
            WHERE project_id=:project_id
              AND trigger_event='tabloide.campaign.pro_monthly_intro'
            """
        ),
        {"project_id": project_id},
    )

