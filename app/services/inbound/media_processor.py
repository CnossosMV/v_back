"""
Media Processor — transcribe audio, describe images, extract document text.

Uses the project's LLM key (via LLMKeyResolver) for OpenAI Whisper + Vision.
Falls back gracefully: if processing fails, resolved_text stays as the placeholder.
"""

import logging
import os
from typing import Optional

from sqlalchemy.orm import Session

from app.schemas.inbound_router import ContentPiece
from .media_storage import MediaStorageService

logger = logging.getLogger(__name__)


class MediaProcessor:
    """Process media content pieces to extract text/descriptions."""

    def __init__(self, db: Session):
        self.db = db
        self.storage = MediaStorageService(db)

    async def process_content_piece(
        self,
        piece: ContentPiece,
        project_id: int,
        instance=None,
        provider_type: str = "evolution_api",
        raw_payload: Optional[dict] = None,
        instance_name: Optional[str] = None,
    ) -> ContentPiece:
        """
        Process a single content piece. Downloads media, then:
        - audio → Whisper transcription
        - image → Vision description
        - document → text extraction (PDF, DOCX, TXT)

        Returns a new ContentPiece with resolved_text filled in.
        """
        if not piece.media_url:
            return piece

        # Download media
        stored = await self.storage.download_and_store(
            media_url_or_id=piece.media_url,
            provider_type=provider_type,
            project_id=project_id,
            instance=instance,
            mime_type=piece.media_mime,
            raw_payload=raw_payload,
            instance_name=instance_name,
        )

        local_path = stored.get("local_path")
        if not local_path:
            logger.warning(f"Could not download media for piece type={piece.type}")
            return piece

        # Update media_url to a serveable path (provider URLs expire)
        from .media_storage import MEDIA_ROOT
        if local_path.startswith(MEDIA_ROOT):
            rel = local_path[len(MEDIA_ROOT):].lstrip("/")
            piece = piece.model_copy(update={
                "media_url": f"/api/v1/media/inbound/{rel}",
                "media_mime": stored.get("mime_type") or piece.media_mime,
            })

        resolved = None

        try:
            if piece.type == "audio":
                resolved = await self._transcribe_audio(local_path, project_id)
            elif piece.type == "image":
                resolved = await self._describe_image(local_path, project_id)
            elif piece.type == "document":
                resolved = self._extract_document_text(local_path, stored.get("mime_type", ""))
        except Exception as e:
            logger.warning(f"Media processing failed for {piece.type}: {e}")

        if resolved:
            piece = piece.model_copy(update={"resolved_text": resolved})

        return piece

    async def _transcribe_audio(self, file_path: str, project_id: int) -> Optional[str]:
        """Transcribe audio using OpenAI Whisper API."""
        api_key = self._resolve_openai_key(project_id)
        if not api_key:
            logger.warning("No OpenAI API key available for audio transcription")
            return None

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)

            with open(file_path, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                )
            text = transcript.text.strip()
            logger.info(f"Audio transcription ({len(text)} chars): {text[:100]}...")
            return text
        except Exception as e:
            logger.error(f"Whisper transcription failed: {e}")
            return None

    async def _describe_image(self, file_path: str, project_id: int) -> Optional[str]:
        """Describe image using OpenAI Vision API."""
        api_key = self._resolve_openai_key(project_id)
        if not api_key:
            logger.warning("No OpenAI API key available for image description")
            return None

        try:
            import base64
            from openai import OpenAI

            client = OpenAI(api_key=api_key)

            with open(file_path, "rb") as img_file:
                b64 = base64.b64encode(img_file.read()).decode()

            # Detect mime from extension
            ext = file_path.rsplit(".", 1)[-1].lower()
            mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
            mime = mime_map.get(ext, "image/jpeg")

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image concisely in one or two sentences. Focus on the main subject and any text visible."},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ],
                    }
                ],
                max_tokens=200,
            )
            description = response.choices[0].message.content.strip()
            logger.info(f"Image description: {description[:100]}...")
            return description
        except Exception as e:
            logger.error(f"Vision description failed: {e}")
            return None

    def _extract_document_text(self, file_path: str, mime_type: str) -> Optional[str]:
        """Extract text from PDF, DOCX, or plain text files."""
        try:
            if "pdf" in mime_type or file_path.endswith(".pdf"):
                return self._extract_pdf(file_path)
            elif "wordprocessingml" in mime_type or file_path.endswith(".docx"):
                return self._extract_docx(file_path)
            elif "text/plain" in mime_type or file_path.endswith(".txt"):
                with open(file_path, "r", errors="replace") as f:
                    return f.read()[:10000]
            else:
                logger.info(f"Unsupported document type: {mime_type}")
                return None
        except Exception as e:
            logger.error(f"Document extraction failed: {e}")
            return None

    def _extract_pdf(self, file_path: str) -> Optional[str]:
        """Extract text from PDF using PyPDF2."""
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(file_path)
            text_parts = []
            for page in reader.pages[:20]:  # Limit to 20 pages
                text_parts.append(page.extract_text() or "")
            full = "\n".join(text_parts).strip()
            return full[:10000] if full else None
        except ImportError:
            logger.warning("PyPDF2 not installed, skipping PDF extraction")
            return None

    def _extract_docx(self, file_path: str) -> Optional[str]:
        """Extract text from DOCX using python-docx."""
        try:
            import docx
            doc = docx.Document(file_path)
            text = "\n".join(p.text for p in doc.paragraphs)
            return text[:10000] if text.strip() else None
        except ImportError:
            logger.warning("python-docx not installed, skipping DOCX extraction")
            return None

    def _resolve_openai_key(self, project_id: int) -> Optional[str]:
        """Get OpenAI API key for the project via resolve_llm."""
        try:
            from app.services.chatbot.llm_key_resolver import resolve_llm
            cfg = resolve_llm(self.db, project_id, purpose="media")
            return cfg.api_key
        except Exception as e:
            logger.warning(f"LLM key resolution failed for media processing: {e}")
            return None


def assemble_resolved_text(pieces: list) -> str:
    """
    Combine resolved_text from all content pieces into a single string.
    Used as the message content for LLM processing.
    """
    parts = []
    for p in pieces:
        if hasattr(p, 'resolved_text') and p.resolved_text:
            parts.append(p.resolved_text)
        elif hasattr(p, 'body') and p.body:
            parts.append(p.body)
    return "\n".join(parts)
