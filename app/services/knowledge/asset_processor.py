"""Asset Processor

Processes knowledge assets: extract text → store full text → chunk → embed.
Reuses KnowledgeLoader for file/URL parsing.
"""
import hashlib
import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from langchain.schema import Document

from app.models import KnowledgeAsset, CollectionAsset
from app.services.chatbot.knowledge_loader import KnowledgeLoader
from app.services.knowledge.asset_vector_store import AssetVectorStore, EMBEDDING_MODEL

logger = logging.getLogger(__name__)


class AssetProcessor:
    """Process knowledge assets into vector embeddings."""

    def __init__(self, db: Session, openai_api_key: str):
        self.db = db
        self.loader = KnowledgeLoader(chunk_size=1000, chunk_overlap=200)
        self.vector_store = AssetVectorStore(openai_api_key)

    def process_asset(self, asset: KnowledgeAsset) -> None:
        """Process a single asset: extract, chunk, embed."""
        asset_type = asset.asset_type

        # Direct-only assets skip processing entirely
        if getattr(asset, 'usage_mode', 'rag') == 'direct':
            asset.processing_status = "skipped"
            self.db.commit()
            return

        # Media types and snippets don't get embedded
        if asset_type in ("snippet", "image", "video", "audio"):
            asset.processing_status = "skipped"
            self.db.commit()
            return

        asset.processing_status = "processing"
        self.db.commit()

        try:
            extracted_text, chunks = self._extract_and_chunk(asset)

            if not chunks:
                asset.processing_status = "ready"
                asset.chunk_count = 0
                asset.extracted_text = extracted_text
                asset.content_hash = self._hash(extracted_text or "")
                asset.last_processed_at = datetime.utcnow()
                self.db.commit()
                return

            # Get collection IDs for this asset
            coll_ids = [
                r[0] for r in self.db.query(CollectionAsset.collection_id)
                .filter(CollectionAsset.asset_id == asset.id).all()
            ]

            # Delete old vectors then add new
            self.vector_store.delete_asset_vectors(asset.project_id, asset.id)

            chunk_count = self.vector_store.add_chunks(
                project_id=asset.project_id,
                asset_id=asset.id,
                asset_name=asset.name,
                asset_type=asset.asset_type,
                collection_ids=coll_ids,
                documents=chunks,
                source_data=asset.source_data,
            )

            asset.processing_status = "ready"
            asset.chunk_count = chunk_count
            asset.extracted_text = extracted_text
            asset.content_hash = self._hash(extracted_text or "")
            asset.embedding_model = EMBEDDING_MODEL
            asset.last_processed_at = datetime.utcnow()
            self.db.commit()

            logger.info(f"Processed asset {asset.id}: {chunk_count} chunks")

        except Exception as e:
            logger.error(f"Failed to process asset {asset.id}: {e}")
            asset.processing_status = "failed"
            asset.processing_error = str(e)[:2000]
            self.db.commit()

    def _extract_and_chunk(self, asset: KnowledgeAsset):
        """Extract text and create chunks based on asset type."""
        sd = asset.source_data or {}

        if asset.asset_type == "document":
            # Uploaded files: resolve local path via storage backend
            if getattr(asset, 'storage_key', None):
                from app.services.storage import get_storage_backend
                file_path = get_storage_backend().get_local_path(asset.storage_key)
            else:
                file_path = sd.get("file_url")
            if not file_path:
                raise ValueError("No file_url or storage_key for document")
            content_hash, chunks, _ = self.loader.load_file(file_path)
            full_text = "\n\n".join(doc.page_content for doc in chunks)
            return full_text, chunks

        elif asset.asset_type == "url":
            web_url = sd.get("web_url")
            if not web_url:
                raise ValueError("No web_url in source_data")
            content_hash, chunks, _ = self.loader.load_url(web_url)
            full_text = "\n\n".join(doc.page_content for doc in chunks)
            return full_text, chunks

        elif asset.asset_type == "faq":
            question = sd.get("question", "")
            answer = sd.get("answer", "")
            text = f"{question}\n\nAnswer: {answer}"
            doc = Document(page_content=text, metadata={"source": f"faq_{asset.id}"})
            return text, [doc]  # Single chunk, no split

        elif asset.asset_type == "text":
            raw_text = sd.get("raw_text", "")
            if not raw_text:
                return "", []
            full_doc = Document(page_content=raw_text, metadata={"source": f"text_{asset.id}"})
            if len(raw_text) > 1000:
                chunks = self.loader.text_splitter.split_documents([full_doc])
            else:
                chunks = [full_doc]
            return raw_text, chunks

        return None, []

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
