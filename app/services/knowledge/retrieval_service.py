"""Retrieval Service

Hybrid search + RAG retrieval scoped to consumer's effective collection set.
Snippet autocomplete for inbox agents.
"""
import logging
from typing import List, Dict, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import KnowledgeAsset, CollectionAsset, ConsumerKnowledgeBinding, KnowledgeCollection
from app.services.knowledge.asset_vector_store import AssetVectorStore
from app.services.knowledge_library_service import KnowledgeLibraryService

logger = logging.getLogger(__name__)


class RetrievalService:
    """Search and retrieval for knowledge assets."""

    def __init__(self, db: Session, openai_api_key: str):
        self.db = db
        self.vector_store = AssetVectorStore(openai_api_key)
        self.lib_service = KnowledgeLibraryService(db)

    def search(
        self,
        project_id: int,
        query: str,
        collection_ids: Optional[List[int]] = None,
        consumer_type: Optional[str] = None,
        consumer_id: Optional[int] = None,
        asset_types: Optional[List[str]] = None,
        limit: int = 10,
        min_score: float = 0.0,
    ) -> List[Dict]:
        """
        Semantic + keyword hybrid search across knowledge assets.
        If consumer_type/consumer_id provided, scopes to their effective collections.
        """
        # Resolve collection scope
        if consumer_type and consumer_id:
            effective = self.lib_service.get_effective_collection_ids(
                project_id, consumer_type, consumer_id
            )
            if effective:
                if collection_ids:
                    # Intersect requested with effective
                    collection_ids = [c for c in collection_ids if c in effective]
                else:
                    collection_ids = effective

                if not collection_ids:
                    return []
            else:
                # No bindings for this consumer — fall back to project-wide search
                logger.info(
                    "No effective collections for %s/%s in project %s, falling back to project-wide search",
                    consumer_type, consumer_id, project_id,
                )

        # Semantic search from ChromaDB
        results = self.vector_store.search(
            project_id=project_id,
            query=query,
            collection_ids=collection_ids,
            asset_types=asset_types,
            k=limit,
            min_score=min_score,
        )

        # Supplement with keyword search on FAQ/snippet (not embedded)
        keyword_results = self._keyword_search(
            project_id=project_id,
            query=query,
            collection_ids=collection_ids,
            asset_types=asset_types,
            limit=limit,
        )

        # Merge: add keyword results not already in semantic results
        seen_ids = {r["asset_id"] for r in results}
        for kr in keyword_results:
            if kr["asset_id"] not in seen_ids:
                results.append(kr)
                seen_ids.add(kr["asset_id"])

        results = results[:limit]

        # Enrich results with source_data and usage_mode from DB
        asset_ids = list({r["asset_id"] for r in results if r.get("asset_id")})
        if asset_ids:
            assets = self.db.query(KnowledgeAsset).filter(
                KnowledgeAsset.id.in_(asset_ids),
                KnowledgeAsset.project_id == project_id,
            ).all()
            asset_map = {a.id: a for a in assets}
            for r in results:
                asset = asset_map.get(r["asset_id"])
                if asset:
                    r["source_data"] = asset.source_data or {}
                    r["usage_mode"] = getattr(asset, 'usage_mode', 'rag')

        return results

    def retrieve_for_rag(
        self,
        project_id: int,
        query: str,
        consumer_type: str,
        consumer_id: int,
        k: int = 5,
        min_score: float = 0.3,
        mode: str = "chunks",
    ) -> Dict:
        """
        RAG retrieval scoped to a consumer's effective collection set.
        Mode "chunks" returns top-k chunks; "full_text" returns entire asset texts.
        """
        effective_ids = self.lib_service.get_effective_collection_ids(
            project_id, consumer_type, consumer_id
        )
        if not effective_ids:
            return {"results": [], "mode": mode}

        if mode == "full_text":
            # Semantic search to find relevant assets, then return full text
            chunk_results = self.vector_store.search(
                project_id=project_id,
                query=query,
                collection_ids=effective_ids,
                k=k,
                min_score=min_score,
            )
            # Deduplicate by asset_id and fetch full text
            seen = set()
            results = []
            for cr in chunk_results:
                aid = cr["asset_id"]
                if aid in seen:
                    continue
                seen.add(aid)
                asset = self.db.query(KnowledgeAsset).filter(
                    KnowledgeAsset.id == aid,
                    KnowledgeAsset.project_id == project_id,
                ).first()
                if asset and asset.extracted_text:
                    results.append({
                        "asset_id": asset.id,
                        "asset_name": asset.name,
                        "asset_type": asset.asset_type,
                        "chunk_text": asset.extracted_text,
                        "score": cr["score"],
                        "collection_ids": cr.get("collection_ids", []),
                        "source_data": asset.source_data or {},
                    })
            return {"results": results[:k], "mode": mode}

        else:
            # Default: chunk-based
            results = self.vector_store.search(
                project_id=project_id,
                query=query,
                collection_ids=effective_ids,
                k=k,
                min_score=min_score,
            )
            # Enrich source_data from DB (vector store doesn't store it)
            asset_ids = list({r["asset_id"] for r in results if r["asset_id"]})
            if asset_ids:
                assets = self.db.query(KnowledgeAsset).filter(
                    KnowledgeAsset.id.in_(asset_ids),
                    KnowledgeAsset.project_id == project_id,
                ).all()
                sd_map = {a.id: a.source_data or {} for a in assets}
                for r in results:
                    r["source_data"] = sd_map.get(r["asset_id"], {})
            return {"results": results, "mode": mode}

    def snippet_autocomplete(
        self,
        project_id: int,
        prefix: str,
        consumer_type: Optional[str] = None,
        consumer_id: Optional[int] = None,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Autocomplete snippets by shortcut prefix.
        Scoped to consumer's effective collections if provided.
        """
        q = self.db.query(KnowledgeAsset).filter(
            KnowledgeAsset.project_id == project_id,
            KnowledgeAsset.asset_type == "snippet",
            KnowledgeAsset.status == "active",
        )

        # If consumer scoping, filter by collections
        if consumer_type and consumer_id:
            effective_ids = self.lib_service.get_effective_collection_ids(
                project_id, consumer_type, consumer_id
            )
            if effective_ids:
                q = q.join(
                    CollectionAsset, CollectionAsset.asset_id == KnowledgeAsset.id
                ).filter(CollectionAsset.collection_id.in_(effective_ids))
            else:
                # No collections bound — return all project snippets as fallback
                pass

        assets = q.all()

        results = []
        prefix_lower = prefix.lower().lstrip("/")
        for asset in assets:
            sd = asset.source_data or {}
            shortcut = sd.get("snippet_shortcut", "")
            body = sd.get("snippet_body", "")
            if shortcut.lower().startswith(prefix_lower) or asset.name.lower().startswith(prefix_lower):
                results.append({
                    "asset_id": asset.id,
                    "name": asset.name,
                    "shortcut": shortcut,
                    "body": body,
                })

        results.sort(key=lambda x: x["shortcut"])
        return results[:limit]

    def _keyword_search(
        self,
        project_id: int,
        query: str,
        collection_ids: Optional[List[int]] = None,
        asset_types: Optional[List[str]] = None,
        limit: int = 5,
    ) -> List[Dict]:
        """Simple keyword search on asset name and FAQ content."""
        q = self.db.query(KnowledgeAsset).filter(
            KnowledgeAsset.project_id == project_id,
            KnowledgeAsset.status == "active",
        )

        if asset_types:
            q = q.filter(KnowledgeAsset.asset_type.in_(asset_types))

        if collection_ids:
            q = q.join(
                CollectionAsset, CollectionAsset.asset_id == KnowledgeAsset.id
            ).filter(CollectionAsset.collection_id.in_(collection_ids))

        # Name match
        q = q.filter(KnowledgeAsset.name.ilike(f"%{query}%"))
        matches = q.limit(limit).all()

        results = []
        for asset in matches:
            sd = asset.source_data or {}
            if asset.asset_type == "faq":
                text = f"{sd.get('question', '')}\n\nAnswer: {sd.get('answer', '')}"
            elif asset.asset_type == "snippet":
                text = sd.get("snippet_body", "")
            elif asset.asset_type == "text":
                text = sd.get("raw_text", "")[:500]
            else:
                text = asset.name

            coll_ids = [
                r[0] for r in self.db.query(CollectionAsset.collection_id)
                .filter(CollectionAsset.asset_id == asset.id).all()
            ]

            results.append({
                "asset_id": asset.id,
                "asset_name": asset.name,
                "asset_type": asset.asset_type,
                "chunk_text": text,
                "score": 0.5,  # Fixed score for keyword matches
                "collection_ids": coll_ids,
                "source_data": sd,
            })

        return results
