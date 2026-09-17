"""Asset Vector Store

ChromaDB wrapper for the knowledge asset library.
One collection per project: project_{id}_assets.
"""
import os
import logging
from typing import List, Dict, Optional
from pathlib import Path

from langchain.vectorstores import Chroma
from langchain.schema import Document

logger = logging.getLogger(__name__)


class AssetVectorStore:
    """ChromaDB wrapper for project-level knowledge assets."""

    def __init__(self, embeddings_or_api_key, vector_store_path: Optional[str] = None):
        """
        Args:
            embeddings_or_api_key: LangChain embeddings object (from create_embeddings())
                                   or an API key string for backward compatibility.
            vector_store_path: Path to store vector data.
        """
        self.vector_store_path = vector_store_path or os.getenv(
            "VECTOR_STORE_PATH", "/app/data/vector_stores"
        )
        if isinstance(embeddings_or_api_key, str):
            # Backward compatibility: raw API key → create OpenAI embeddings
            from langchain_openai import OpenAIEmbeddings
            self.embeddings = OpenAIEmbeddings(
                openai_api_key=embeddings_or_api_key,
                model="text-embedding-3-small",
            )
        else:
            self.embeddings = embeddings_or_api_key
        Path(self.vector_store_path).mkdir(parents=True, exist_ok=True)

    def _get_collection(self, project_id: int) -> Chroma:
        persist_dir = os.path.join(self.vector_store_path, f"project_{project_id}_assets")
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        return Chroma(
            persist_directory=persist_dir,
            embedding_function=self.embeddings,
            collection_name=f"project_{project_id}_assets",
        )

    def add_chunks(
        self,
        project_id: int,
        asset_id: int,
        asset_name: str,
        asset_type: str,
        collection_ids: List[int],
        documents: List[Document],
        source_data: Optional[Dict] = None,
    ) -> int:
        """Add document chunks for an asset to ChromaDB."""
        vectorstore = self._get_collection(project_id)

        coll_str = self._encode_collection_ids(collection_ids)

        # Store key source_data fields in metadata (ChromaDB only accepts flat scalars)
        extra_meta = {}
        if source_data:
            for key in ("web_url", "file_url", "snippet_shortcut"):
                if key in source_data and isinstance(source_data[key], str):
                    extra_meta[key] = source_data[key]

        for doc in documents:
            doc.metadata.update({
                "asset_id": asset_id,
                "asset_name": asset_name,
                "asset_type": asset_type,
                "collection_ids": coll_str,
                **extra_meta,
            })

        vectorstore.add_documents(documents)
        vectorstore.persist()
        logger.info(f"Added {len(documents)} chunks for asset {asset_id} in project {project_id}")
        return len(documents)

    def delete_asset_vectors(self, project_id: int, asset_id: int) -> bool:
        """Delete all chunks for an asset."""
        vectorstore = self._get_collection(project_id)
        try:
            collection = vectorstore._collection
            collection.delete(where={"asset_id": asset_id})
            vectorstore.persist()
            logger.info(f"Deleted vectors for asset {asset_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting vectors for asset {asset_id}: {e}")
            return False

    def sync_collection_ids(
        self,
        project_id: int,
        asset_id: int,
        new_collection_ids: List[int],
    ) -> None:
        """Update collection_ids metadata on all chunks for an asset."""
        vectorstore = self._get_collection(project_id)
        coll_str = self._encode_collection_ids(new_collection_ids)
        try:
            collection = vectorstore._collection
            results = collection.get(where={"asset_id": asset_id})
            if results and results["ids"]:
                collection.update(
                    ids=results["ids"],
                    metadatas=[{**m, "collection_ids": coll_str} for m in results["metadatas"]],
                )
                vectorstore.persist()
        except Exception as e:
            logger.warning(f"Failed to sync collection_ids for asset {asset_id}: {e}")

    def search(
        self,
        project_id: int,
        query: str,
        collection_ids: Optional[List[int]] = None,
        asset_types: Optional[List[str]] = None,
        k: int = 10,
        min_score: float = 0.0,
    ) -> List[Dict]:
        """Semantic search across project assets, optionally filtered by collections."""
        vectorstore = self._get_collection(project_id)

        where_filter = {}
        if collection_ids and len(collection_ids) == 1:
            where_filter["collection_ids"] = {"$contains": f"|{collection_ids[0]}|"}
        elif asset_types and len(asset_types) == 1:
            where_filter["asset_type"] = asset_types[0]

        try:
            if where_filter:
                results = vectorstore.similarity_search_with_score(query, k=k, filter=where_filter)
            else:
                results = vectorstore.similarity_search_with_score(query, k=k)
        except Exception as e:
            logger.warning(f"ChromaDB search error: {e}")
            return []

        # Filter by collection_ids (multi-collection) and min_score
        output = []
        for doc, score in results:
            if score < min_score:
                continue
            if collection_ids and len(collection_ids) > 1:
                chunk_colls = doc.metadata.get("collection_ids", "")
                if not any(f"|{cid}|" in chunk_colls for cid in collection_ids):
                    continue
            if asset_types and len(asset_types) > 1:
                if doc.metadata.get("asset_type") not in asset_types:
                    continue
            # Reconstruct source_data from metadata fields stored during indexing
            sd = {}
            for key in ("web_url", "file_url", "snippet_shortcut"):
                val = doc.metadata.get(key)
                if val:
                    sd[key] = val

            output.append({
                "asset_id": doc.metadata.get("asset_id"),
                "asset_name": doc.metadata.get("asset_name", ""),
                "asset_type": doc.metadata.get("asset_type", ""),
                "chunk_text": doc.page_content,
                "score": round(score, 4),
                "collection_ids": self._decode_collection_ids(doc.metadata.get("collection_ids", "")),
                "source_data": sd,
            })

        return output[:k]

    @staticmethod
    def _encode_collection_ids(ids: List[int]) -> str:
        """Encode list [1,3,7] → "|1|3|7|" for $contains filtering."""
        if not ids:
            return "||"
        return "|" + "|".join(str(i) for i in sorted(ids)) + "|"

    @staticmethod
    def _decode_collection_ids(s: str) -> List[int]:
        """Decode "|1|3|7|" → [1, 3, 7]."""
        if not s or s == "||":
            return []
        return [int(x) for x in s.strip("|").split("|") if x.isdigit()]
