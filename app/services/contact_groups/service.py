"""Contact group CRUD, preview and durable membership materialization."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import and_, exists, func, insert, select, update
from sqlalchemy.orm import Query, Session

from app.models.campaigns import ContactGroup, ContactGroupMembership
from app.models.messaging import MessagingUser
from app.services.contact_groups.compiler import (
    GroupFilterError,
    base_contact_query,
    chunked,
    compile_group_query,
)


class ContactGroupService:
    """Project-scoped group operations.

    A membership row records materialized match state only.  It intentionally
    does not encode marketing consent, opt-out or channel availability.
    """

    def __init__(self, db: Session):
        self.db = db

    def get(self, project_id: int, group_id: int) -> ContactGroup | None:
        return self.db.query(ContactGroup).filter(
            ContactGroup.id == group_id,
            ContactGroup.project_id == project_id,
        ).first()

    def list(self, project_id: int, *, include_archived: bool = False) -> list[ContactGroup]:
        query = self.db.query(ContactGroup).filter(ContactGroup.project_id == project_id)
        if not include_archived:
            query = query.filter(ContactGroup.status != "archived")
        return query.order_by(ContactGroup.updated_at.desc(), ContactGroup.id.desc()).all()

    def create(self, project_id: int, values: dict[str, Any], created_by_user_id: int | None) -> ContactGroup:
        group_type = str(values.get("group_type") or "dynamic")
        if group_type not in {"dynamic", "static", "imported"}:
            raise GroupFilterError("group_type must be dynamic, static or imported")
        if group_type == "dynamic":
            # Compile once before writing so unsupported rules fail atomically.
            compile_group_query(self.db, project_id, values.get("rule_config")).limit(1).statement
        group = ContactGroup(
            project_id=project_id,
            name=str(values["name"]).strip(),
            description=values.get("description"),
            group_type=group_type,
            rule_config=values.get("rule_config"),
            source_ref=values.get("source_ref"),
            status=values.get("status") or "active",
            created_by_user_id=created_by_user_id,
        )
        if not group.name:
            raise GroupFilterError("name is required")
        self.db.add(group)
        self.db.commit()
        self.db.refresh(group)
        return group

    def update(self, group: ContactGroup, values: dict[str, Any]) -> ContactGroup:
        group_type = str(values.get("group_type", group.group_type))
        rule_config = values.get("rule_config", group.rule_config)
        if group_type not in {"dynamic", "static", "imported"}:
            raise GroupFilterError("group_type must be dynamic, static or imported")
        if group_type == "dynamic":
            compile_group_query(self.db, group.project_id, rule_config).limit(1).statement
        for field in ("name", "description", "group_type", "rule_config", "source_ref", "status"):
            if field in values:
                setattr(group, field, values[field])
        if not str(group.name or "").strip():
            raise GroupFilterError("name is required")
        group.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(group)
        return group

    def archive(self, group: ContactGroup) -> ContactGroup:
        group.status = "archived"
        group.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(group)
        return group

    def selection_query(self, group: ContactGroup, *, now: datetime | None = None) -> Query:
        if group.group_type == "dynamic":
            return compile_group_query(self.db, group.project_id, group.rule_config, now=now)
        return base_contact_query(self.db, group.project_id).filter(
            exists(select(ContactGroupMembership.id).where(
                ContactGroupMembership.project_id == group.project_id,
                ContactGroupMembership.group_id == group.id,
                ContactGroupMembership.user_id == MessagingUser.id,
                ContactGroupMembership.state == "included",
            ))
        )

    def preview(
        self,
        project_id: int,
        *,
        rule_config: dict[str, Any] | None = None,
        group: ContactGroup | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if group:
            if group.project_id != project_id:
                raise GroupFilterError("Group belongs to another project")
            ids = self.selection_query(group)
        else:
            ids = compile_group_query(self.db, project_id, rule_config)
        ids_subquery = ids.subquery()
        total = self.db.query(func.count()).select_from(ids_subquery).scalar() or 0
        samples = self.db.query(
            MessagingUser.id,
            MessagingUser.name,
            MessagingUser.email,
            MessagingUser.locale,
            MessagingUser.lifecycle_stage,
            MessagingUser.segment_name,
        ).join(ids_subquery, ids_subquery.c.id == MessagingUser.id).order_by(
            MessagingUser.id,
        ).limit(max(0, min(limit, 100))).all()
        return {
            "matched_count": int(total),
            "sample": [
                {
                    "user_id": row.id,
                    "name": row.name,
                    "email": row.email,
                    "locale": row.locale,
                    "lifecycle_stage": row.lifecycle_stage,
                    "segment_name": row.segment_name,
                }
                for row in samples
            ],
        }

    def evaluate(self, group: ContactGroup) -> dict[str, int]:
        if group.group_type != "dynamic":
            raise GroupFilterError("Only dynamic groups can be evaluated from rules")

        now = datetime.utcnow()
        matched = self.selection_query(group, now=now).subquery()

        # Preserve history by marking contacts that stopped matching excluded.
        excluded = self.db.query(ContactGroupMembership).filter(
            ContactGroupMembership.project_id == group.project_id,
            ContactGroupMembership.group_id == group.id,
            ContactGroupMembership.state == "included",
            ~exists(select(matched.c.id).where(matched.c.id == ContactGroupMembership.user_id)),
        ).update(
            {
                ContactGroupMembership.state: "excluded",
                ContactGroupMembership.reason: "rule_mismatch",
                ContactGroupMembership.evaluated_at: now,
                ContactGroupMembership.updated_at: now,
            },
            synchronize_session=False,
        )

        dialect = self.db.get_bind().dialect.name
        values = select(
            func.cast(group.project_id, ContactGroupMembership.project_id.type),
            func.cast(group.id, ContactGroupMembership.group_id.type),
            matched.c.id,
            func.cast("included", ContactGroupMembership.state.type),
            func.cast("dynamic_rule", ContactGroupMembership.source.type),
            func.cast(None, ContactGroupMembership.reason.type),
            func.cast(now, ContactGroupMembership.evaluated_at.type),
        )
        columns = ["project_id", "group_id", "user_id", "state", "source", "reason", "evaluated_at"]
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert

            statement = dialect_insert(ContactGroupMembership).from_select(columns, values)
            statement = statement.on_conflict_do_update(
                constraint="uq_contact_group_member",
                set_={
                    "state": "included",
                    "source": "dynamic_rule",
                    "reason": None,
                    "evaluated_at": now,
                    "updated_at": now,
                },
            )
            self.db.execute(statement)
        elif dialect == "sqlite":
            # SQLite cannot combine ON CONFLICT with every FROM SELECT shape;
            # update existing rows set-wise, then insert only missing rows.
            self.db.query(ContactGroupMembership).filter(
                ContactGroupMembership.project_id == group.project_id,
                ContactGroupMembership.group_id == group.id,
                exists(select(matched.c.id).where(matched.c.id == ContactGroupMembership.user_id)),
            ).update(
                {
                    ContactGroupMembership.state: "included",
                    ContactGroupMembership.source: "dynamic_rule",
                    ContactGroupMembership.reason: None,
                    ContactGroupMembership.evaluated_at: now,
                    ContactGroupMembership.updated_at: now,
                },
                synchronize_session=False,
            )
            missing = select(
                func.cast(group.project_id, ContactGroupMembership.project_id.type),
                func.cast(group.id, ContactGroupMembership.group_id.type),
                matched.c.id,
                func.cast("included", ContactGroupMembership.state.type),
                func.cast("dynamic_rule", ContactGroupMembership.source.type),
                func.cast(None, ContactGroupMembership.reason.type),
                func.cast(now, ContactGroupMembership.evaluated_at.type),
            ).where(~exists(select(ContactGroupMembership.id).where(
                ContactGroupMembership.project_id == group.project_id,
                ContactGroupMembership.group_id == group.id,
                ContactGroupMembership.user_id == matched.c.id,
            )))
            self.db.execute(insert(ContactGroupMembership).from_select(columns, missing))
        else:
            self._upsert_members(group, (row[0] for row in self.db.query(matched.c.id).all()), "dynamic_rule", now)

        group.last_evaluated_at = now
        self.db.commit()
        included = self.db.query(func.count(ContactGroupMembership.id)).filter(
            ContactGroupMembership.project_id == group.project_id,
            ContactGroupMembership.group_id == group.id,
            ContactGroupMembership.state == "included",
        ).scalar() or 0
        return {"included": int(included), "excluded": int(excluded)}

    def add_members(
        self,
        group: ContactGroup,
        user_ids: Iterable[int],
        *,
        source: str = "manual",
    ) -> int:
        if group.group_type == "dynamic":
            raise GroupFilterError("Dynamic group membership is controlled by its rule")
        ids = sorted({int(user_id) for user_id in user_ids})
        if not ids:
            return 0
        valid_ids: list[int] = []
        for batch in chunked(ids):
            valid_ids.extend(row[0] for row in self.db.query(MessagingUser.id).filter(
                MessagingUser.project_id == group.project_id,
                MessagingUser.id.in_(batch),
                MessagingUser.status == "active",
                MessagingUser.is_sandbox == False,  # noqa: E712
            ).all())
        self._upsert_members(group, valid_ids, source, datetime.utcnow())
        self.db.commit()
        return len(valid_ids)

    def remove_members(self, group: ContactGroup, user_ids: Iterable[int]) -> int:
        ids = sorted({int(user_id) for user_id in user_ids})
        if not ids:
            return 0
        now = datetime.utcnow()
        changed = 0
        for batch in chunked(ids):
            changed += self.db.query(ContactGroupMembership).filter(
                ContactGroupMembership.project_id == group.project_id,
                ContactGroupMembership.group_id == group.id,
                ContactGroupMembership.user_id.in_(batch),
                ContactGroupMembership.state == "included",
            ).update(
                {
                    ContactGroupMembership.state: "excluded",
                    ContactGroupMembership.reason: "manual_removal",
                    ContactGroupMembership.evaluated_at: now,
                    ContactGroupMembership.updated_at: now,
                },
                synchronize_session=False,
            )
        self.db.commit()
        return changed

    def _upsert_members(
        self,
        group: ContactGroup,
        user_ids: Iterable[int],
        source: str,
        now: datetime,
    ) -> None:
        ids = sorted({int(user_id) for user_id in user_ids})
        if not ids:
            return
        dialect = self.db.get_bind().dialect.name
        if dialect in {"postgresql", "sqlite"}:
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            else:
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            for batch in chunked(ids):
                rows = [{
                    "project_id": group.project_id,
                    "group_id": group.id,
                    "user_id": user_id,
                    "state": "included",
                    "source": source,
                    "reason": None,
                    "evaluated_at": now,
                } for user_id in batch]
                statement = dialect_insert(ContactGroupMembership).values(rows).on_conflict_do_update(
                    index_elements=["project_id", "group_id", "user_id"],
                    set_={
                        "state": "included",
                        "source": source,
                        "reason": None,
                        "evaluated_at": now,
                        "updated_at": now,
                    },
                )
                self.db.execute(statement)
            return
        for batch in chunked(ids):
            existing = {
                row.user_id: row
                for row in self.db.query(ContactGroupMembership).filter(
                    ContactGroupMembership.project_id == group.project_id,
                    ContactGroupMembership.group_id == group.id,
                    ContactGroupMembership.user_id.in_(batch),
                ).all()
            }
            for user_id in batch:
                membership = existing.get(user_id)
                if membership:
                    membership.state = "included"
                    membership.source = source
                    membership.reason = None
                    membership.evaluated_at = now
                else:
                    self.db.add(ContactGroupMembership(
                        project_id=group.project_id,
                        group_id=group.id,
                        user_id=user_id,
                        state="included",
                        source=source,
                        evaluated_at=now,
                    ))
