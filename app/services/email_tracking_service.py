"""
Email Tracking Service — pixel injection, link rewriting, open + click recording.

Provides:
- 1x1 transparent GIF pixel
- Token generation for per-email tracking
- Pixel injection into HTML email content
- Link rewriting for click tracking
- Open recording with MessagingEvent emission
- Click recording with MessagingEvent emission
- Bot/proxy classification (security scanners blocked, proxy opens recorded with flag)
"""
import base64
import logging
import re
from datetime import datetime
from urllib.parse import quote, unquote
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import SendLog, SendLogClick, MessagingEvent

logger = logging.getLogger(__name__)

# 1x1 transparent GIF (43 bytes)
PIXEL_GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00'
    b'\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00'
    b'\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02'
    b'\x44\x01\x00\x3b'
)

# Security scanners — these prefetch URLs for safety analysis, NOT user opens.
# Hits from these are silently discarded.
SECURITY_SCANNERS = [
    "Barracuda", "Proofpoint", "Mimecast",
    "SafeLinks",
    "facebookexternalhit", "Twitterbot",
]

# Email proxy signatures — these load images on behalf of the user.
# Gmail/Yahoo/Apple proxy images for privacy. The open IS real (the user opened
# the email), but the timing/UA is from the proxy, not the user's browser.
# We record these opens but flag them as proxy-mediated.
PROXY_SIGNATURES = [
    "GoogleImageProxy",
    "YahooMailProxy",
    "YMailNorrin",
    "Apple Mail",  # Apple Mail Privacy Protection proxy
]


class EmailTrackingService:

    @staticmethod
    def generate_token() -> str:
        return str(uuid4())

    @staticmethod
    def classify_user_agent(user_agent: str) -> str:
        """Classify a user-agent as 'human', 'proxy', or 'bot'.

        - bot: security scanners, link crawlers — discard
        - proxy: email image proxies (Gmail, Yahoo, Apple) — record with flag
        - human: real email client — record normally
        """
        if not user_agent:
            return "human"
        ua_lower = user_agent.lower()
        for sig in SECURITY_SCANNERS:
            if sig.lower() in ua_lower:
                return "bot"
        for sig in PROXY_SIGNATURES:
            if sig.lower() in ua_lower:
                return "proxy"
        return "human"

    @staticmethod
    def inject_pixel(html: str, tracking_url: str) -> str:
        """Inject 1x1 tracking pixel before </body> or at end of HTML."""
        pixel_tag = f'<img src="{tracking_url}" width="1" height="1" style="display:none" alt="" />'
        lower = html.lower()
        idx = lower.rfind('</body>')
        if idx != -1:
            return html[:idx] + pixel_tag + html[idx:]
        return html + pixel_tag

    def record_open(self, db: Session, token: str, user_agent: str = "") -> bool:
        """Record email open. Updates SendLog + creates MessagingEvent.

        Security scanners are silently discarded.
        Proxy opens (Gmail, Yahoo, Apple) are recorded with is_proxy=True.
        Direct opens are recorded normally.
        """
        classification = self.classify_user_agent(user_agent)

        if classification == "bot":
            logger.debug("Security scanner filtered: %s", user_agent[:100])
            return False

        send_log = db.query(SendLog).filter(SendLog.tracking_token == token).first()
        if not send_log:
            return False

        is_proxy = classification == "proxy"
        now = datetime.utcnow()
        first_open = send_log.opened_at is None

        send_log.open_count = (send_log.open_count or 0) + 1
        if first_open:
            send_log.opened_at = now

        # Advance status to "read" if still in sent/delivered
        if send_log.status in ("sent", "delivered"):
            send_log.status = "read"
            send_log.read_at = now

        # Emit channel event only on FIRST open (avoid duplicate events)
        if first_open:
            event = MessagingEvent(
                project_id=send_log.project_id,
                user_id=send_log.user_id,
                event_name="channel.email.opened",
                source="delivery_tracker",
                properties={
                    "send_log_id": send_log.id,
                    "channel": "email",
                    "recipient": send_log.recipient,
                    "template_id": send_log.template_id,
                    "source_type": send_log.source_type,
                    "source_id": send_log.source_id,
                    "provider_message_id": send_log.provider_message_id,
                    "previous_status": send_log.status,
                    "is_proxy": is_proxy,
                    "proxy_ua": user_agent[:200] if is_proxy else None,
                },
            )
            db.add(event)

        db.commit()

        # Mark MES as stale
        try:
            from app.services.scoring.mes_engine import MESEngine
            MESEngine(db).mark_stale(send_log.id)
            db.commit()
        except Exception:
            pass

        logger.info(
            "Recorded email open for send_log %d (count=%d, first=%s, proxy=%s)",
            send_log.id, send_log.open_count, first_open, is_proxy,
        )
        return True

    # ── Link rewriting for click tracking ──────────────────────────────

    # Regex to find <a href="..."> tags (handles single and double quotes)
    _LINK_RE = re.compile(r'(<a\s[^>]*href\s*=\s*["\'])([^"\']+)(["\'][^>]*>)', re.IGNORECASE)

    # Schemes to skip rewriting
    _SKIP_SCHEMES = ("mailto:", "tel:", "sms:", "javascript:", "#")

    # Regex to find bare URLs in plain-text content (WhatsApp/SMS)
    _TEXT_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)

    @staticmethod
    def encode_url(url: str) -> str:
        """Base64-encode a URL for the redirect parameter."""
        return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

    @staticmethod
    def decode_url(encoded: str) -> str:
        """Decode a base64-encoded URL, adding padding as needed."""
        padded = encoded + "=" * (4 - len(encoded) % 4)
        return base64.urlsafe_b64decode(padded).decode()

    @staticmethod
    def append_utm_params(url: str, utm_params: dict) -> str:
        """Append UTM params to a destination URL (skips keys already present).

        utm_content carries the send's tracking_token so a later page_view /
        track event can be hard-linked back to the SendLog.
        """
        if not utm_params:
            return url
        try:
            fragment = ""
            base = url
            if "#" in url:
                base, fragment = url.split("#", 1)
                fragment = "#" + fragment
            existing = base.split("?", 1)[1] if "?" in base else ""
            parts = []
            for key, value in utm_params.items():
                if f"{key}=" in existing:
                    continue
                parts.append(f"{key}={quote(str(value), safe='')}")
            if not parts:
                return url
            sep = "&" if "?" in base else "?"
            return f"{base}{sep}{'&'.join(parts)}{fragment}"
        except Exception:
            return url

    @classmethod
    def rewrite_links(
        cls, html: str, tracking_base_url: str, tracking_token: str,
        utm_params: dict = None,
    ) -> str:
        """Rewrite <a href> links in HTML for click tracking.

        Each link becomes: {tracking_base_url}/t/c/{tracking_token}/{index}?u={base64_url}
        The destination URL gets utm_params appended (deterministic attribution).

        Skips: mailto, tel, anchors, javascript, unsubscribe links, pixel URLs.
        """
        link_index = 0

        def replacer(match):
            nonlocal link_index
            prefix = match.group(1)   # <a href="
            url = match.group(2)      # the URL
            suffix = match.group(3)   # "> ...

            # Skip non-http links
            if any(url.lower().startswith(s) for s in cls._SKIP_SCHEMES):
                return match.group(0)

            # Skip tracking pixel and unsubscribe URLs
            if "/t/o/" in url or "/unsubscribe/" in url:
                return match.group(0)

            destination = cls.append_utm_params(url, utm_params) if utm_params else url
            encoded = cls.encode_url(destination)
            tracked_url = f"{tracking_base_url}/t/c/{tracking_token}/{link_index}?u={encoded}"
            link_index += 1
            return f"{prefix}{tracked_url}{suffix}"

        return cls._LINK_RE.sub(replacer, html)

    @classmethod
    def rewrite_text_links(
        cls, content: str, tracking_base_url: str, tracking_token: str,
        utm_params: dict = None,
    ) -> str:
        """Rewrite bare URLs in plain-text content (WhatsApp/SMS) for click tracking.

        Same redirect format as rewrite_links. Trailing punctuation that is
        likely sentence punctuation (.,!?;:) is left outside the tracked URL.
        """
        link_index = 0

        def replacer(match):
            nonlocal link_index
            url = match.group(0)

            trailing = ""
            while url and url[-1] in ".,!?;:":
                trailing = url[-1] + trailing
                url = url[:-1]

            if "/t/c/" in url or "/t/o/" in url or "/unsubscribe/" in url:
                return match.group(0)

            destination = cls.append_utm_params(url, utm_params) if utm_params else url
            encoded = cls.encode_url(destination)
            tracked_url = f"{tracking_base_url}/t/c/{tracking_token}/{link_index}?u={encoded}"
            link_index += 1
            return tracked_url + trailing

        return cls._TEXT_URL_RE.sub(replacer, content)

    def record_click(
        self,
        db: Session,
        tracking_token: str,
        link_index: int,
        original_url: str,
        user_agent: str = "",
    ) -> bool:
        """Record a link click. Updates SendLog + SendLogClick + emits MessagingEvent."""
        classification = self.classify_user_agent(user_agent)
        if classification == "bot":
            logger.debug("Security scanner click filtered: %s", user_agent[:100])
            return False

        send_log = db.query(SendLog).filter(SendLog.tracking_token == tracking_token).first()
        if not send_log:
            return False

        now = datetime.utcnow()

        # Upsert SendLogClick row
        click_row = db.query(SendLogClick).filter(
            SendLogClick.send_log_id == send_log.id,
            SendLogClick.link_index == link_index,
        ).first()

        if click_row:
            click_row.click_count = (click_row.click_count or 0) + 1
            click_row.last_click_at = now
        else:
            click_row = SendLogClick(
                send_log_id=send_log.id,
                link_index=link_index,
                original_url=original_url[:2000],
                click_count=1,
                first_click_at=now,
                last_click_at=now,
                user_agent=user_agent[:500] if user_agent else None,
            )
            db.add(click_row)

        # Update SendLog aggregate
        send_log.click_count = (send_log.click_count or 0) + 1
        if not send_log.first_click_at:
            send_log.first_click_at = now

        # If they clicked, they opened — ensure open status
        if not send_log.opened_at:
            send_log.opened_at = now
            send_log.open_count = (send_log.open_count or 0) + 1
        if send_log.status in ("sent", "delivered"):
            send_log.status = "read"
            send_log.read_at = now

        # Emit event
        is_proxy = classification == "proxy"
        event = MessagingEvent(
            project_id=send_log.project_id,
            user_id=send_log.user_id,
            event_name="channel.email.clicked",
            source="delivery_tracker",
            properties={
                "send_log_id": send_log.id,
                "channel": "email",
                "recipient": send_log.recipient,
                "url": original_url[:500],
                "link_index": link_index,
                "is_proxy": is_proxy,
                "template_id": send_log.template_id,
                "source_type": send_log.source_type,
            },
        )
        db.add(event)

        db.commit()

        # Mark MES as stale
        try:
            from app.services.scoring.mes_engine import MESEngine
            MESEngine(db).mark_stale(send_log.id)
            db.commit()
        except Exception:
            pass

        logger.info(
            "Recorded email click for send_log %d link %d (total=%d)",
            send_log.id, link_index, send_log.click_count,
        )
        return True
