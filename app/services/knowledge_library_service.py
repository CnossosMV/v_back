"""Knowledge Library Service

CRUD operations for knowledge assets, collections, and consumer bindings.
"""
import logging
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import func, and_

from app.models import (
    KnowledgeAsset, KnowledgeCollection, CollectionAsset,
    ConsumerKnowledgeBinding,
)

logger = logging.getLogger(__name__)

# Types that get embedded (knowledge); the rest are media (skipped)
KNOWLEDGE_TYPES = {"document", "url", "faq", "text"}
MEDIA_TYPES = {"snippet", "image", "video", "audio"}
ALL_ASSET_TYPES = KNOWLEDGE_TYPES | MEDIA_TYPES


class KnowledgeLibraryService:
    """Core service for knowledge asset library operations."""

    def __init__(self, db: Session):
        self.db = db

    # ── Assets ────────────────────────────────────────────────────────

    @staticmethod
    def _slugify(name: str) -> str:
        """Generate a URL-safe slug from asset name."""
        import re
        slug = name.lower().strip()
        slug = re.sub(r'[^\w\s-]', '', slug)
        slug = re.sub(r'[\s_]+', '-', slug)
        slug = re.sub(r'-+', '-', slug).strip('-')
        return slug[:100]

    def create_asset(
        self,
        project_id: int,
        asset_type: str,
        name: str,
        source_data: Dict[str, Any],
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        language: Optional[str] = None,
        status: str = "active",
        created_by: Optional[str] = None,
        usage_mode: Optional[str] = None,
        slug: Optional[str] = None,
        storage_key: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> KnowledgeAsset:
        if asset_type not in ALL_ASSET_TYPES:
            raise ValueError(f"Invalid asset_type: {asset_type}")

        # Default usage_mode if not provided
        if not usage_mode:
            usage_mode = "rag"

        if usage_mode not in ("rag", "direct", "both"):
            raise ValueError(f"Invalid usage_mode: {usage_mode}")

        # Auto-generate slug for direct/both if not provided
        if usage_mode in ("direct", "both") and not slug:
            slug = self._slugify(name)

        # Handle slug collisions by appending a numeric suffix
        if slug:
            base_slug = slug
            counter = 1
            while self.db.query(KnowledgeAsset).filter(
                KnowledgeAsset.project_id == project_id,
                KnowledgeAsset.slug == slug,
            ).first():
                slug = f"{base_slug}-{counter}"[:100]
                counter += 1

        # Determine processing_status based on usage_mode
        if usage_mode == "direct":
            processing_status = "skipped"
        elif asset_type in KNOWLEDGE_TYPES:
            processing_status = "pending"
        else:
            processing_status = "skipped"

        asset = KnowledgeAsset(
            project_id=project_id,
            asset_type=asset_type,
            name=name,
            description=description,
            tags=tags or [],
            language=language,
            source_data=source_data,
            status=status,
            created_by=created_by,
            processing_status=processing_status,
            usage_mode=usage_mode,
            slug=slug,
            storage_key=storage_key,
            file_size=file_size,
        )
        self.db.add(asset)
        self.db.commit()
        self.db.refresh(asset)
        logger.info(f"Created asset {asset.id} ({asset_type}, mode={usage_mode}) for project {project_id}")
        return asset

    def list_assets(
        self,
        project_id: int,
        asset_type: Optional[str] = None,
        collection_id: Optional[int] = None,
        tag: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[KnowledgeAsset], int]:
        q = self.db.query(KnowledgeAsset).filter(
            KnowledgeAsset.project_id == project_id,
        )
        if asset_type:
            q = q.filter(KnowledgeAsset.asset_type == asset_type)
        if status:
            q = q.filter(KnowledgeAsset.status == status)
        else:
            q = q.filter(KnowledgeAsset.status != "archived")
        if tag:
            q = q.filter(KnowledgeAsset.tags.any(tag))
        if search:
            q = q.filter(KnowledgeAsset.name.ilike(f"%{search}%"))
        if collection_id:
            q = q.join(CollectionAsset, CollectionAsset.asset_id == KnowledgeAsset.id).filter(
                CollectionAsset.collection_id == collection_id,
            )

        total = q.count()
        items = q.order_by(KnowledgeAsset.updated_at.desc()).offset(
            (page - 1) * page_size
        ).limit(page_size).all()
        return items, total

    def get_asset(self, project_id: int, asset_id: int) -> Optional[KnowledgeAsset]:
        return self.db.query(KnowledgeAsset).filter(
            KnowledgeAsset.id == asset_id,
            KnowledgeAsset.project_id == project_id,
        ).first()

    def update_asset(
        self,
        project_id: int,
        asset_id: int,
        data: Dict[str, Any],
    ) -> Optional[KnowledgeAsset]:
        asset = self.get_asset(project_id, asset_id)
        if not asset:
            return None

        source_data_changed = False
        for key, value in data.items():
            if value is not None:
                if key == "source_data" and value != asset.source_data:
                    source_data_changed = True
                setattr(asset, key, value)

        if source_data_changed and asset.asset_type in KNOWLEDGE_TYPES:
            asset.processing_status = "pending"
            asset.version += 1

        self.db.commit()
        self.db.refresh(asset)
        return asset

    def archive_asset(self, project_id: int, asset_id: int) -> Optional[KnowledgeAsset]:
        asset = self.get_asset(project_id, asset_id)
        if not asset:
            return None
        asset.status = "archived"
        # Remove from all collections
        self.db.query(CollectionAsset).filter(CollectionAsset.asset_id == asset_id).delete()
        self.db.commit()
        self.db.refresh(asset)
        return asset

    def restore_asset(self, project_id: int, asset_id: int) -> Optional[KnowledgeAsset]:
        asset = self.get_asset(project_id, asset_id)
        if not asset:
            return None
        asset.status = "active"
        if asset.asset_type in KNOWLEDGE_TYPES:
            asset.processing_status = "pending"
        self.db.commit()
        self.db.refresh(asset)
        return asset

    def delete_asset(self, project_id: int, asset_id: int) -> bool:
        asset = self.get_asset(project_id, asset_id)
        if not asset:
            return False
        # Clean up stored file if present
        if asset.storage_key:
            try:
                from app.services.storage import get_storage_backend
                get_storage_backend().delete(asset.storage_key)
            except Exception as e:
                logger.warning(f"Failed to delete stored file {asset.storage_key}: {e}")
        self.db.delete(asset)
        self.db.commit()
        return True

    def get_asset_collection_ids(self, asset_id: int) -> List[int]:
        rows = self.db.query(CollectionAsset.collection_id).filter(
            CollectionAsset.asset_id == asset_id
        ).all()
        return [r[0] for r in rows]

    # ── Collections ───────────────────────────────────────────────────

    def create_collection(
        self,
        project_id: int,
        name: str,
        description: Optional[str] = None,
        visibility: str = "selective",
    ) -> KnowledgeCollection:
        coll = KnowledgeCollection(
            project_id=project_id,
            name=name,
            description=description,
            visibility=visibility,
        )
        self.db.add(coll)
        self.db.commit()
        self.db.refresh(coll)
        return coll

    def list_collections(self, project_id: int) -> List[Dict[str, Any]]:
        colls = self.db.query(KnowledgeCollection).filter(
            KnowledgeCollection.project_id == project_id,
        ).order_by(KnowledgeCollection.name).all()

        result = []
        for c in colls:
            asset_count = self.db.query(func.count(CollectionAsset.id)).filter(
                CollectionAsset.collection_id == c.id
            ).scalar()
            consumer_count = self.db.query(func.count(ConsumerKnowledgeBinding.id)).filter(
                ConsumerKnowledgeBinding.collection_id == c.id
            ).scalar()
            result.append({
                "id": c.id,
                "project_id": c.project_id,
                "name": c.name,
                "description": c.description,
                "visibility": c.visibility,
                "asset_count": asset_count,
                "consumer_count": consumer_count,
                "created_at": c.created_at,
                "updated_at": c.updated_at,
            })
        return result

    def get_collection(self, project_id: int, collection_id: int) -> Optional[KnowledgeCollection]:
        return self.db.query(KnowledgeCollection).filter(
            KnowledgeCollection.id == collection_id,
            KnowledgeCollection.project_id == project_id,
        ).first()

    def update_collection(
        self,
        project_id: int,
        collection_id: int,
        data: Dict[str, Any],
    ) -> Optional[KnowledgeCollection]:
        coll = self.get_collection(project_id, collection_id)
        if not coll:
            return None
        for key, value in data.items():
            if value is not None:
                setattr(coll, key, value)
        self.db.commit()
        self.db.refresh(coll)
        return coll

    def delete_collection(self, project_id: int, collection_id: int) -> bool:
        coll = self.get_collection(project_id, collection_id)
        if not coll:
            return False
        self.db.delete(coll)
        self.db.commit()
        return True

    def add_assets_to_collection(
        self,
        project_id: int,
        collection_id: int,
        asset_ids: List[int],
        added_by: Optional[str] = None,
    ) -> int:
        coll = self.get_collection(project_id, collection_id)
        if not coll:
            raise ValueError("Collection not found")

        added = 0
        for aid in asset_ids:
            asset = self.get_asset(project_id, aid)
            if not asset:
                continue
            exists = self.db.query(CollectionAsset).filter(
                CollectionAsset.collection_id == collection_id,
                CollectionAsset.asset_id == aid,
            ).first()
            if exists:
                continue
            ca = CollectionAsset(
                collection_id=collection_id,
                asset_id=aid,
                added_by=added_by,
            )
            self.db.add(ca)
            added += 1

        self.db.commit()
        return added

    def remove_asset_from_collection(
        self,
        project_id: int,
        collection_id: int,
        asset_id: int,
    ) -> bool:
        coll = self.get_collection(project_id, collection_id)
        if not coll:
            return False
        deleted = self.db.query(CollectionAsset).filter(
            CollectionAsset.collection_id == collection_id,
            CollectionAsset.asset_id == asset_id,
        ).delete()
        self.db.commit()
        return deleted > 0

    def get_collection_assets(self, project_id: int, collection_id: int) -> List[KnowledgeAsset]:
        return self.db.query(KnowledgeAsset).join(
            CollectionAsset, CollectionAsset.asset_id == KnowledgeAsset.id
        ).filter(
            CollectionAsset.collection_id == collection_id,
            KnowledgeAsset.project_id == project_id,
        ).order_by(KnowledgeAsset.name).all()

    # ── Consumer Bindings ─────────────────────────────────────────────

    def create_binding(
        self,
        project_id: int,
        consumer_type: str,
        consumer_id: int,
        collection_id: int,
        permission: str = "rag",
    ) -> ConsumerKnowledgeBinding:
        binding = ConsumerKnowledgeBinding(
            project_id=project_id,
            consumer_type=consumer_type,
            consumer_id=consumer_id,
            collection_id=collection_id,
            permission=permission,
        )
        self.db.add(binding)
        self.db.commit()
        self.db.refresh(binding)
        return binding

    def list_bindings(
        self,
        project_id: int,
        consumer_type: str,
        consumer_id: int,
    ) -> List[ConsumerKnowledgeBinding]:
        return self.db.query(ConsumerKnowledgeBinding).filter(
            ConsumerKnowledgeBinding.project_id == project_id,
            ConsumerKnowledgeBinding.consumer_type == consumer_type,
            ConsumerKnowledgeBinding.consumer_id == consumer_id,
        ).all()

    def update_binding(
        self,
        project_id: int,
        binding_id: int,
        permission: str,
    ) -> Optional[ConsumerKnowledgeBinding]:
        binding = self.db.query(ConsumerKnowledgeBinding).filter(
            ConsumerKnowledgeBinding.id == binding_id,
            ConsumerKnowledgeBinding.project_id == project_id,
        ).first()
        if not binding:
            return None
        binding.permission = permission
        self.db.commit()
        self.db.refresh(binding)
        return binding

    def delete_binding(self, project_id: int, binding_id: int) -> bool:
        binding = self.db.query(ConsumerKnowledgeBinding).filter(
            ConsumerKnowledgeBinding.id == binding_id,
            ConsumerKnowledgeBinding.project_id == project_id,
        ).first()
        if not binding:
            return False
        self.db.delete(binding)
        self.db.commit()
        return True

    def get_effective_collections(
        self,
        project_id: int,
        consumer_type: str,
        consumer_id: int,
    ) -> List[Dict[str, Any]]:
        """Get all collections a consumer has access to (explicit + all-visibility)."""
        # Explicit bindings
        explicit = self.db.query(ConsumerKnowledgeBinding).filter(
            ConsumerKnowledgeBinding.project_id == project_id,
            ConsumerKnowledgeBinding.consumer_type == consumer_type,
            ConsumerKnowledgeBinding.consumer_id == consumer_id,
        ).all()

        explicit_coll_ids = {b.collection_id for b in explicit}

        # All-visibility collections
        all_vis = self.db.query(KnowledgeCollection).filter(
            KnowledgeCollection.project_id == project_id,
            KnowledgeCollection.visibility == "all",
        ).all()

        results = []

        for b in explicit:
            coll = self.db.query(KnowledgeCollection).filter(
                KnowledgeCollection.id == b.collection_id
            ).first()
            if not coll:
                continue
            asset_count = self.db.query(func.count(CollectionAsset.id)).filter(
                CollectionAsset.collection_id == coll.id
            ).scalar()
            results.append({
                "collection_id": coll.id,
                "collection_name": coll.name,
                "visibility": coll.visibility,
                "permission": b.permission,
                "source": "explicit",
                "asset_count": asset_count,
            })

        for coll in all_vis:
            if coll.id in explicit_coll_ids:
                continue
            asset_count = self.db.query(func.count(CollectionAsset.id)).filter(
                CollectionAsset.collection_id == coll.id
            ).scalar()
            results.append({
                "collection_id": coll.id,
                "collection_name": coll.name,
                "visibility": coll.visibility,
                "permission": "rag",
                "source": "all_visibility",
                "asset_count": asset_count,
            })

        return results

    def get_effective_collection_ids(
        self,
        project_id: int,
        consumer_type: str,
        consumer_id: int,
    ) -> List[int]:
        """Quick helper — just the IDs."""
        effective = self.get_effective_collections(project_id, consumer_type, consumer_id)
        return [e["collection_id"] for e in effective]
