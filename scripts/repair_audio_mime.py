"""
One-time repair script: fix audio files saved as .octet-stream.

WhatsApp audio messages received before the MIME fix are stored as:
  {MEDIA_ROOT}/{project_id}/{date}/{uuid}.octet-stream

This script:
  1. Finds all chat_messages with content_pieces containing .octet-stream media_url
  2. Re-downloads from Evolution API (getBase64) if the file is encrypted/corrupt
  3. Renames the file on disk to .ogg
  4. Updates content_pieces (media_url + media_mime) in the DB
  5. Optionally re-runs Whisper transcription for audio with missing transcription

Usage (inside the backend container):
  python scripts/repair_audio_mime.py [--dry-run] [--re-download] [--re-transcribe]
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import sys

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/app/media")
EVO_URL = os.getenv("EVOLUTION_API_BASE_URL", os.getenv("EVOLUTION_API_URL", ""))
EVO_KEY = os.getenv("EVOLUTION_API_GLOBAL_KEY", os.getenv("EVOLUTION_API_KEY", ""))


def main(dry_run: bool, re_download: bool, re_transcribe: bool):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        asyncio.run(_repair(db, dry_run, re_download, re_transcribe))
    finally:
        db.close()


async def _repair(db, dry_run: bool, re_download: bool, re_transcribe: bool):
    from sqlalchemy import text

    # Find messages that have at least one piece with .octet-stream media_url
    # OR audio pieces with missing/placeholder resolved_text
    rows = db.execute(
        text("""
            SELECT cm.id, cm.content_pieces, cm.session_id,
                   cs.session_metadata
            FROM chat_messages cm
            JOIN chat_sessions cs ON cs.id = cm.session_id
            WHERE cm.content_pieces::text LIKE '%.octet-stream%'
               OR (cm.content_pieces::text LIKE '%"type": "audio"%'
                   AND cm.content_pieces::text LIKE '%Audio message%'
                   AND cm.resolved_text LIKE '%Audio message%')
        """)
    ).fetchall()

    logger.info(f"Found {len(rows)} messages to check")

    updated = 0
    renamed = 0
    re_downloaded = 0
    transcribed = 0
    skipped = 0

    for row in rows:
        msg_id = row[0]
        pieces = row[1]
        session_id = row[2]
        session_meta = row[3] or {}

        if not pieces:
            continue

        changed = False
        new_pieces = []

        for piece in pieces:
            media_url = piece.get("media_url", "")
            piece_type = piece.get("type", "")

            needs_octet_fix = ".octet-stream" in media_url
            needs_transcription = (
                re_transcribe
                and piece_type == "audio"
                and piece.get("resolved_text") in (None, "", "[Audio message]", "[Audio]")
            )

            if not needs_octet_fix and not needs_transcription:
                new_pieces.append(piece)
                continue

            piece = dict(piece)

            if needs_octet_fix:
                rel_path = media_url.removeprefix("/api/v1/media/inbound/")
                old_abs = os.path.join(MEDIA_ROOT, rel_path)
                new_abs = old_abs.replace(".octet-stream", ".ogg")
                new_url = media_url.replace(".octet-stream", ".ogg")

                file_ok = False

                # Check if the .octet-stream file is actually encrypted (not valid OGG)
                if os.path.exists(old_abs):
                    with open(old_abs, "rb") as f:
                        header = f.read(4)
                    if header == b'OggS':
                        # File is valid OGG, just rename
                        if not dry_run:
                            os.rename(old_abs, new_abs)
                        logger.info(f"  {'[dry]' if dry_run else ''} Renamed: {old_abs}")
                        renamed += 1
                        file_ok = True
                    elif re_download:
                        # File is encrypted — re-download via Evolution API
                        result = await _redownload_from_evolution(db, session_id, session_meta, msg_id)
                        if result:
                            if not dry_run:
                                os.makedirs(os.path.dirname(new_abs), exist_ok=True)
                                with open(new_abs, "wb") as f:
                                    f.write(result)
                                # Remove the old encrypted file
                                os.remove(old_abs)
                            logger.info(f"  {'[dry]' if dry_run else ''} Re-downloaded: {new_abs} ({len(result)} bytes)")
                            re_downloaded += 1
                            file_ok = True
                        else:
                            logger.warning(f"  Re-download failed for msg {msg_id}")
                            skipped += 1
                    else:
                        # Just rename even if encrypted — better than .octet-stream
                        if not dry_run:
                            os.rename(old_abs, new_abs)
                        logger.info(f"  {'[dry]' if dry_run else ''} Renamed (possibly encrypted): {old_abs}")
                        renamed += 1
                        file_ok = True
                elif os.path.exists(new_abs):
                    logger.info(f"  Already renamed: {new_abs}")
                    file_ok = True
                else:
                    if re_download:
                        result = await _redownload_from_evolution(db, session_id, session_meta, msg_id)
                        if result:
                            if not dry_run:
                                os.makedirs(os.path.dirname(new_abs), exist_ok=True)
                                with open(new_abs, "wb") as f:
                                    f.write(result)
                            logger.info(f"  {'[dry]' if dry_run else ''} Downloaded missing file: {new_abs} ({len(result)} bytes)")
                            re_downloaded += 1
                            file_ok = True
                        else:
                            logger.warning(f"  File not found and re-download failed: {old_abs}")
                            skipped += 1
                    else:
                        logger.warning(f"  File not found: {old_abs} (use --re-download to fetch)")
                        skipped += 1

                piece["media_url"] = new_url
                piece["media_mime"] = "audio/ogg"
                changed = True

                # Re-transcribe if we got a valid file
                if needs_transcription and file_ok and os.path.exists(new_abs):
                    transcript = _transcribe(new_abs, db, session_id)
                    if transcript:
                        piece["resolved_text"] = transcript
                        transcribed += 1

            elif needs_transcription:
                # Media URL is fine but transcription missing
                if media_url.startswith("/api/v1/media/inbound/"):
                    rel = media_url.removeprefix("/api/v1/media/inbound/")
                    file_path = os.path.join(MEDIA_ROOT, rel)
                    if os.path.exists(file_path):
                        transcript = _transcribe(file_path, db, session_id)
                        if transcript:
                            piece["resolved_text"] = transcript
                            changed = True
                            transcribed += 1

            new_pieces.append(piece)

        if changed:
            if not dry_run:
                # Update content_pieces
                db.execute(
                    text("UPDATE chat_messages SET content_pieces = :cp WHERE id = :id"),
                    {"cp": json.dumps(new_pieces), "id": msg_id},
                )
                # Also update resolved_text if any transcription was added
                resolved_parts = []
                for p in new_pieces:
                    rt = p.get("resolved_text") or p.get("body") or ""
                    if rt:
                        resolved_parts.append(rt)
                resolved_text = "\n".join(resolved_parts)
                if resolved_text:
                    db.execute(
                        text("UPDATE chat_messages SET resolved_text = :rt, content = :rt WHERE id = :id"),
                        {"rt": resolved_text, "id": msg_id},
                    )
            updated += 1

    if not dry_run:
        db.commit()

    logger.info(
        f"Done. Messages updated: {updated}, files renamed: {renamed}, "
        f"re-downloaded: {re_downloaded}, transcribed: {transcribed}, "
        f"skipped: {skipped}"
        + (" [DRY RUN — no changes written]" if dry_run else "")
    )


async def _redownload_from_evolution(db, session_id: int, session_meta: dict, msg_id: int):
    """Try to re-download media from Evolution API's getBase64 endpoint."""
    if not EVO_URL:
        return None

    from sqlalchemy import text

    # Get the instance name from session metadata
    instance_id = session_meta.get("inbound_instance_id")
    if not instance_id:
        logger.warning(f"  No inbound_instance_id in session {session_id}")
        return None

    # Look up instance name
    row = db.execute(
        text("SELECT instance_name FROM whatsapp_instances WHERE id = :id"),
        {"id": instance_id},
    ).first()
    if not row:
        logger.warning(f"  Instance {instance_id} not found")
        return None
    instance_name = row[0]

    # Get the message's external_message_id and the remote_jid from session
    msg_row = db.execute(
        text("""
            SELECT external_message_id, message_metadata
            FROM chat_messages WHERE id = :id
        """),
        {"id": msg_id},
    ).first()
    if not msg_row or not msg_row[0]:
        logger.warning(f"  No external_message_id for msg {msg_id}")
        return None

    ext_id = msg_row[0]

    # Get remote_jid from the session's context_data or first user message
    remote_jid = None
    user_id_row = db.execute(
        text("SELECT user_identifier FROM chat_sessions WHERE id = :id"),
        {"id": session_id},
    ).first()
    if user_id_row:
        uid = user_id_row[0]
        # user_identifier might be the phone or jid
        if "@" in uid:
            remote_jid = uid
        else:
            remote_jid = f"{uid}@s.whatsapp.net"

    if not remote_jid:
        logger.warning(f"  Cannot resolve remote_jid for session {session_id}")
        return None

    body = {
        "message": {
            "key": {
                "id": ext_id,
                "remoteJid": remote_jid,
                "fromMe": False,
            },
        },
        "convertToMp4": False,
    }

    try:
        url = f"{EVO_URL.rstrip('/')}/chat/getBase64FromMediaMessage/{instance_name}"
        headers = {"apikey": EVO_KEY}

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            result = resp.json()

        b64_data = result.get("base64", "")
        if not b64_data:
            return None

        # Strip data URI prefix if present
        if "," in b64_data and b64_data.startswith("data:"):
            _, b64_data = b64_data.split(",", 1)

        return base64.b64decode(b64_data)
    except Exception as e:
        logger.warning(f"  Evolution getBase64 failed: {e}")
        return None


def _transcribe(file_path: str, db, session_id: int) -> str | None:
    """Transcribe audio using OpenAI Whisper."""
    from sqlalchemy import text

    # Get project_id from session
    row = db.execute(
        text("""
            SELECT c.project_id FROM chat_sessions cs
            JOIN chatbots c ON c.id = cs.chatbot_id
            WHERE cs.id = :id
        """),
        {"id": session_id},
    ).first()
    if not row:
        return None

    project_id = row[0]

    try:
        from app.services.chatbot.llm_key_resolver import resolve_llm
        cfg = resolve_llm(db, project_id, purpose="media")
        api_key = cfg.api_key
    except Exception:
        api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        with open(file_path, "rb") as f:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=f)
        text_result = transcript.text.strip()
        if text_result:
            logger.info(f"  Transcribed ({len(text_result)} chars): {text_result[:80]}...")
        return text_result or None
    except Exception as e:
        logger.warning(f"  Whisper transcription failed: {e}")
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report only, make no changes")
    parser.add_argument("--re-download", action="store_true", help="Re-download from Evolution API for missing/encrypted files")
    parser.add_argument("--re-transcribe", action="store_true", help="Re-run Whisper on audio missing transcription")
    args = parser.parse_args()

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    main(dry_run=args.dry_run, re_download=args.re_download, re_transcribe=args.re_transcribe)
