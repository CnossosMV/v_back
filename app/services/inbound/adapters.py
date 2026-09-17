"""
Channel Adapters — normalise raw webhook payloads into InboundMessage.

Each function takes the provider-specific data and returns a canonical InboundMessage.
"""

import logging
from typing import Optional

from app.schemas.inbound_router import ContentPiece, EmailThreadInfo, InboundMessage

logger = logging.getLogger(__name__)


# ── Evolution API adapter ───────────────────────────────────────────────

def normalize_evolution_api(
    webhook_data: dict,
    instance_name: str,
    instance_id: Optional[int] = None,
) -> Optional[InboundMessage]:
    """
    Convert an Evolution API MESSAGES_UPSERT payload into InboundMessage.

    Replaces inline extraction at whatsapp.py:766-804.
    """
    message_data = webhook_data.get("data", {})
    message = message_data.get("message", {})
    key = message_data.get("key", {})

    # Skip outgoing messages
    if key.get("fromMe", False):
        return None

    pieces = []
    primary_body = None

    # Text
    if "conversation" in message:
        body = message.get("conversation", "")
        pieces.append(ContentPiece(type="text", body=body))
        primary_body = body
    elif "extendedTextMessage" in message:
        body = message.get("extendedTextMessage", {}).get("text", "")
        pieces.append(ContentPiece(type="text", body=body))
        primary_body = body

    # Image
    if "imageMessage" in message:
        img = message["imageMessage"]
        caption = img.get("caption", "")
        media_url = img.get("url") or img.get("directPath")
        pieces.append(ContentPiece(
            type="image",
            body=caption or "[Image]",
            media_url=media_url,
            media_mime=img.get("mimetype"),
        ))
        if not primary_body:
            primary_body = caption or "[Image]"

    # Document
    if "documentMessage" in message:
        doc = message["documentMessage"]
        caption = doc.get("caption", "")
        media_url = doc.get("url") or doc.get("directPath")
        pieces.append(ContentPiece(
            type="document",
            body=caption or f"[Document: {doc.get('fileName', 'file')}]",
            media_url=media_url,
            media_mime=doc.get("mimetype"),
        ))
        if not primary_body:
            primary_body = caption or "[Document]"

    # Audio — WhatsApp always sends OGG Opus; Evolution may report wrong MIME
    if "audioMessage" in message:
        aud = message["audioMessage"]
        media_url = aud.get("url") or aud.get("directPath")
        raw_mime = aud.get("mimetype") or ""
        audio_mime = raw_mime if raw_mime and raw_mime != "application/octet-stream" else "audio/ogg"
        pieces.append(ContentPiece(
            type="audio",
            body="[Audio message]",
            media_url=media_url,
            media_mime=audio_mime,
        ))
        if not primary_body:
            primary_body = "[Audio message]"

    # Video
    if "videoMessage" in message:
        vid = message["videoMessage"]
        caption = vid.get("caption", "")
        media_url = vid.get("url") or vid.get("directPath")
        pieces.append(ContentPiece(
            type="video",
            body=caption or "[Video]",
            media_url=media_url,
            media_mime=vid.get("mimetype"),
        ))
        if not primary_body:
            primary_body = caption or "[Video]"

    # Sticker
    if "stickerMessage" in message:
        stk = message["stickerMessage"]
        pieces.append(ContentPiece(
            type="sticker",
            body="[Sticker]",
            media_url=stk.get("url") or stk.get("directPath"),
            media_mime=stk.get("mimetype"),
        ))
        if not primary_body:
            primary_body = "[Sticker]"

    # Contact
    if "contactMessage" in message:
        contact = message["contactMessage"]
        display = contact.get("displayName", "Unknown")
        pieces.append(ContentPiece(type="contact", body=f"[Contact: {display}]"))
        if not primary_body:
            primary_body = f"[Contact: {display}]"

    # Location
    if "locationMessage" in message:
        loc = message["locationMessage"]
        lat = loc.get("degreesLatitude", 0)
        lng = loc.get("degreesLongitude", 0)
        pieces.append(ContentPiece(type="location", body=f"[Location: {lat}, {lng}]"))
        if not primary_body:
            primary_body = f"[Location: {lat}, {lng}]"

    if not pieces:
        return None

    # Resolve contact identifier
    remote_jid = key.get("remoteJid", "")
    remote_jid_alt = key.get("remoteJidAlt", "")

    customer_phone = None
    if "@lid" in remote_jid and remote_jid_alt:
        customer_phone = remote_jid_alt.replace("@s.whatsapp.net", "")
    elif "@s.whatsapp.net" in remote_jid:
        customer_phone = remote_jid.replace("@s.whatsapp.net", "")

    contact_identifier = (
        remote_jid.replace("@s.whatsapp.net", "").replace("@lid", "")
        if remote_jid else ""
    )

    return InboundMessage(
        channel="whatsapp",
        provider_type="evolution_api",
        contact_identifier=contact_identifier,
        instance_name=instance_name,
        instance_id=instance_id,
        content_pieces=pieces,
        resolved_text=primary_body,
        raw_payload=message_data,
        timestamp=str(message_data.get("messageTimestamp", "")),
        message_id=key.get("id"),
        push_name=message_data.get("pushName"),
        channel_context={
            "remote_jid": remote_jid,
            "remote_jid_alt": remote_jid_alt,
            "customer_phone": customer_phone,
        },
    )


# ── Meta Cloud API adapter ─────────────────────────────────────────────

def normalize_meta_cloud_api(
    message: dict,
    value: dict,
    instance_id: Optional[int] = None,
    instance_name: Optional[str] = None,
) -> InboundMessage:
    """
    Convert a Meta Cloud API webhook message into InboundMessage.

    Replaces inline extraction at meta_whatsapp.py:364-383.
    """
    from_number = message.get("from", "")
    msg_id = message.get("id", "")
    msg_type = message.get("type", "text")
    timestamp = message.get("timestamp", "")

    pieces = []
    primary_body = None

    if msg_type == "text":
        body = message.get("text", {}).get("body", "")
        pieces.append(ContentPiece(type="text", body=body))
        primary_body = body
    elif msg_type == "image":
        img = message.get("image", {})
        caption = img.get("caption", "")
        pieces.append(ContentPiece(
            type="image",
            body=caption or "[Image]",
            media_url=img.get("id"),  # Meta media ID
            media_mime=img.get("mime_type"),
        ))
        primary_body = caption or "[Image]"
    elif msg_type == "document":
        doc = message.get("document", {})
        caption = doc.get("caption", "")
        pieces.append(ContentPiece(
            type="document",
            body=caption or f"[Document: {doc.get('filename', 'file')}]",
            media_url=doc.get("id"),
            media_mime=doc.get("mime_type"),
        ))
        primary_body = caption or "[Document]"
    elif msg_type == "audio":
        aud = message.get("audio", {})
        raw_mime = aud.get("mime_type") or ""
        audio_mime = raw_mime if raw_mime and raw_mime != "application/octet-stream" else "audio/ogg"
        pieces.append(ContentPiece(
            type="audio",
            body="[Audio]",
            media_url=aud.get("id"),
            media_mime=audio_mime,
        ))
        primary_body = "[Audio]"
    elif msg_type == "video":
        vid = message.get("video", {})
        caption = vid.get("caption", "")
        pieces.append(ContentPiece(
            type="video",
            body=caption or "[Video]",
            media_url=vid.get("id"),
            media_mime=vid.get("mime_type"),
        ))
        primary_body = caption or "[Video]"
    elif msg_type == "location":
        loc = message.get("location", {})
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        pieces.append(ContentPiece(type="location", body=f"[Location: {lat}, {lng}]"))
        primary_body = f"[Location: {lat}, {lng}]"
    elif msg_type == "contacts":
        pieces.append(ContentPiece(type="contact", body="[Contact]"))
        primary_body = "[Contact]"
    elif msg_type == "reaction":
        emoji = message.get("reaction", {}).get("emoji", "")
        pieces.append(ContentPiece(type="reaction", body=emoji))
        primary_body = emoji
    elif msg_type == "sticker":
        stk = message.get("sticker", {})
        pieces.append(ContentPiece(
            type="sticker",
            body="[Sticker]",
            media_url=stk.get("id"),
            media_mime=stk.get("mime_type"),
        ))
        primary_body = "[Sticker]"

    # Contact info
    contacts = value.get("contacts", [])
    push_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

    return InboundMessage(
        channel="whatsapp",
        provider_type="meta_cloud_api",
        contact_identifier=from_number,
        instance_id=instance_id,
        instance_name=instance_name,
        content_pieces=pieces,
        resolved_text=primary_body,
        raw_payload=message,
        timestamp=timestamp,
        message_id=msg_id,
        push_name=push_name,
        channel_context={
            "remote_jid": from_number,
            "customer_phone": from_number,
        },
    )


# ── Meta Messenger / Instagram DM adapter ─────────────────────────────

def normalize_meta_messaging(
    event: dict,
    channel: str,
    project_id: int,
    instance_id: int,
    instance_name: Optional[str] = None,
    push_name: Optional[str] = None,
    channel_context: Optional[dict] = None,
) -> Optional[InboundMessage]:
    """
    Convert a Messenger/Instagram `messaging[]` webhook event into InboundMessage.

    channel: "messenger" | "instagram"
    contact_identifier is the PSID (Messenger) or IGSID (Instagram).
    project_id is resolved upfront from the MetaPageConnection — page/instagram
    webhooks are app-level, so instance lookup happens before normalization.
    """
    sender_id = (event.get("sender") or {}).get("id", "")
    if not sender_id:
        return None

    message = event.get("message") or {}
    postback = event.get("postback") or {}

    pieces = []
    primary_body = None

    text = message.get("text")
    if text:
        pieces.append(ContentPiece(type="text", body=text))
        primary_body = text

    # Quick reply payload (arrives inside message)
    quick_reply = (message.get("quick_reply") or {}).get("payload")
    if quick_reply and not text:
        pieces.append(ContentPiece(type="text", body=quick_reply))
        primary_body = quick_reply

    # Attachments — payload.url is a direct (expiring) CDN URL
    for att in message.get("attachments", []) or []:
        att_type = att.get("type", "")
        payload = att.get("payload") or {}
        media_url = payload.get("url")
        if att_type == "image":
            pieces.append(ContentPiece(type="image", body="[Image]", media_url=media_url))
            primary_body = primary_body or "[Image]"
        elif att_type == "video":
            pieces.append(ContentPiece(type="video", body="[Video]", media_url=media_url))
            primary_body = primary_body or "[Video]"
        elif att_type == "audio":
            pieces.append(ContentPiece(type="audio", body="[Audio]", media_url=media_url, media_mime="audio/mp4"))
            primary_body = primary_body or "[Audio]"
        elif att_type == "file":
            pieces.append(ContentPiece(type="document", body="[File]", media_url=media_url))
            primary_body = primary_body or "[File]"
        elif att_type == "location":
            coords = payload.get("coordinates") or {}
            body = f"[Location: {coords.get('lat')}, {coords.get('long')}]"
            pieces.append(ContentPiece(type="location", body=body))
            primary_body = primary_body or body
        elif att_type in ("story_mention", "share", "reel", "ig_reel", "template", "fallback"):
            body = f"[{att_type.replace('_', ' ').title()}]"
            pieces.append(ContentPiece(type="text", body=body))
            primary_body = primary_body or body

    # Postbacks (button taps) arrive as a separate event type
    if postback:
        body = postback.get("payload") or postback.get("title") or "[Postback]"
        pieces.append(ContentPiece(type="text", body=body))
        primary_body = primary_body or body

    # Reactions
    reaction = event.get("reaction") or {}
    if reaction and reaction.get("action") != "unreact":
        emoji = reaction.get("emoji", "")
        if emoji:
            pieces.append(ContentPiece(type="reaction", body=emoji))
            primary_body = primary_body or emoji

    if not pieces:
        return None

    msg_id = message.get("mid") or f"{channel}_{sender_id}_{event.get('timestamp', '')}"

    return InboundMessage(
        channel=channel,
        provider_type="meta_graph",
        contact_identifier=sender_id,
        project_id=project_id,
        instance_id=instance_id,
        instance_name=instance_name,
        content_pieces=pieces,
        resolved_text=primary_body,
        raw_payload=event,
        timestamp=str(event.get("timestamp", "")),
        message_id=msg_id,
        push_name=push_name,
        channel_context=channel_context or {},
    )


# ── Twilio adapter ─────────────────────────────────────────────────────

def normalize_twilio(
    message_data: dict,
    provider_type: str,
    provider_id: Optional[int] = None,
) -> InboundMessage:
    """
    Convert parsed Twilio form data (via twilio_service.parse_incoming_message)
    into InboundMessage.
    """
    body = message_data.get("body", "")
    pieces = [ContentPiece(type="text", body=body)] if body else []

    # Twilio may include media
    num_media = int(message_data.get("NumMedia", 0))
    for i in range(num_media):
        media_url = message_data.get(f"MediaUrl{i}")
        media_mime = message_data.get(f"MediaContentType{i}", "")
        media_type = "image"
        if "audio" in media_mime:
            media_type = "audio"
        elif "video" in media_mime:
            media_type = "video"
        elif "pdf" in media_mime or "document" in media_mime:
            media_type = "document"
        pieces.append(ContentPiece(
            type=media_type,
            body=f"[{media_type.capitalize()}]",
            media_url=media_url,
            media_mime=media_mime,
        ))

    channel = message_data.get("channel", "sms")
    contact_identifier = message_data.get("from", "")

    return InboundMessage(
        channel=channel,
        provider_type=provider_type,
        contact_identifier=contact_identifier,
        provider_id=provider_id,
        content_pieces=pieces,
        resolved_text=body or (pieces[0].body if pieces else ""),
        raw_payload=message_data,
        message_id=message_data.get("message_sid"),
        channel_context={
            "customer_phone": contact_identifier,
        },
    )


# ── Web Chat Widget adapter ────────────────────────────────────────────

def normalize_web_chat(
    message: str,
    user_identifier: str,
    session_id: Optional[int] = None,
    instance_id: Optional[int] = None,
) -> InboundMessage:
    """
    Convert a web chat widget request into InboundMessage.
    """
    pieces = [ContentPiece(type="text", body=message)] if message else []

    return InboundMessage(
        channel="web",
        provider_type="web",
        contact_identifier=user_identifier,
        instance_id=instance_id,
        content_pieces=pieces,
        resolved_text=message,
    )


# ── Email adapter ─────────────────────────────────────────────────────

def normalize_email(
    parsed: dict,
    provider_type: str,
    instance_id: Optional[int] = None,
    project_id: Optional[int] = None,
    inbound_address_id: Optional[int] = None,
) -> InboundMessage:
    """
    Convert a parsed email dict (provider-agnostic) into InboundMessage.

    Expected `parsed` keys:
    - from_email: str
    - from_name: str | None
    - subject: str
    - body_text: str
    - body_html: str | None
    - message_id_header: str | None
    - in_reply_to: str | None
    - references: str | None (space-separated)
    - attachments: list[dict] | None  (each: {filename, content_type, url|data})
    - timestamp: str | None
    """
    from_email = parsed.get("from_email", "")
    body_text = parsed.get("body_text", "")
    subject = parsed.get("subject", "")

    pieces = []
    if body_text:
        pieces.append(ContentPiece(type="text", body=body_text))

    # Attachments
    for att in parsed.get("attachments", []) or []:
        content_type = att.get("content_type", "")
        media_type = "document"
        if "image" in content_type:
            media_type = "image"
        elif "audio" in content_type:
            media_type = "audio"
        elif "video" in content_type:
            media_type = "video"
        pieces.append(ContentPiece(
            type=media_type,
            body=f"[{media_type.capitalize()}: {att.get('filename', 'file')}]",
            media_url=att.get("url"),
            media_mime=content_type,
        ))

    # Threading
    refs_raw = parsed.get("references") or ""
    references = refs_raw.split() if refs_raw else None
    thread = EmailThreadInfo(
        message_id=parsed.get("message_id_header"),
        in_reply_to=parsed.get("in_reply_to"),
        references=references,
        subject=subject,
    )

    return InboundMessage(
        channel="email",
        provider_type=provider_type,
        contact_identifier=from_email,
        project_id=project_id,
        instance_id=instance_id,
        content_pieces=pieces,
        resolved_text=body_text or f"[{subject}]",
        raw_payload=parsed,
        timestamp=parsed.get("timestamp"),
        message_id=parsed.get("message_id_header"),
        push_name=parsed.get("from_name"),
        channel_context={
            "subject": subject,
            "from_name": parsed.get("from_name"),
        },
        thread=thread,
        inbound_address_id=inbound_address_id,
    )
