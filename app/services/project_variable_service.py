"""Service for managing project-level template variables."""
import base64
import hashlib
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

from sqlalchemy.orm import Session

from app.models import ProjectVariable
from app.services.encryption_service import encrypt_value

logger = logging.getLogger(__name__)


class ProjectVariableService:
    def __init__(self, db: Session):
        self.db = db

    def list(self, project_id: int) -> List[ProjectVariable]:
        return (
            self.db.query(ProjectVariable)
            .filter(ProjectVariable.project_id == project_id)
            .order_by(ProjectVariable.key)
            .all()
        )

    def get(self, project_id: int, variable_id: int) -> Optional[ProjectVariable]:
        return (
            self.db.query(ProjectVariable)
            .filter(
                ProjectVariable.id == variable_id,
                ProjectVariable.project_id == project_id,
            )
            .first()
        )

    def create(self, project_id: int, data: dict) -> ProjectVariable:
        existing = (
            self.db.query(ProjectVariable)
            .filter(
                ProjectVariable.project_id == project_id,
                ProjectVariable.key == data["key"],
            )
            .first()
        )
        if existing:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=409,
                detail=f"Variable with key '{data['key']}' already exists",
            )

        variable = ProjectVariable(
            project_id=project_id,
            key=data["key"],
            var_type=data.get("var_type", "text"),
            value=data.get("value"),
            url_params=[p.dict() if hasattr(p, "dict") else p for p in data["url_params"]] if data.get("url_params") else None,
            description=data.get("description"),
        )
        self.db.add(variable)
        self.db.commit()
        self.db.refresh(variable)
        return variable

    def update(
        self, project_id: int, variable_id: int, data: dict
    ) -> Optional[ProjectVariable]:
        variable = self.get(project_id, variable_id)
        if not variable:
            return None

        if "key" in data and data["key"] is not None and data["key"] != variable.key:
            dup = (
                self.db.query(ProjectVariable)
                .filter(
                    ProjectVariable.project_id == project_id,
                    ProjectVariable.key == data["key"],
                    ProjectVariable.id != variable_id,
                )
                .first()
            )
            if dup:
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=409,
                    detail=f"Variable with key '{data['key']}' already exists",
                )

        for field in ("key", "var_type", "value", "description"):
            if field in data and data[field] is not None:
                setattr(variable, field, data[field])

        if "url_params" in data:
            variable.url_params = (
                [p.dict() if hasattr(p, "dict") else p for p in data["url_params"]]
                if data["url_params"]
                else None
            )

        self.db.commit()
        self.db.refresh(variable)
        return variable

    def delete(self, project_id: int, variable_id: int) -> bool:
        variable = self.get(project_id, variable_id)
        if not variable:
            return False
        self.db.delete(variable)
        self.db.commit()
        return True

    def render_project_variables(
        self, project_id: int, contact_context: Dict[str, Any]
    ) -> Dict[str, str]:
        """Render all project variables, resolving URL params with contact context."""
        variables = self.list(project_id)
        result: Dict[str, str] = {}

        for var in variables:
            if var.var_type == "text":
                result[var.key] = var.value or ""
            elif var.var_type == "url":
                result[var.key] = self._render_url(var, contact_context)

        return result

    def preview_variable(
        self, variable: "ProjectVariable", contact_context: Dict[str, Any]
    ) -> str:
        """Preview a single variable's rendered value."""
        if variable.var_type == "text":
            return variable.value or ""
        return self._render_url(variable, contact_context)

    def _render_url(
        self, variable: "ProjectVariable", contact_context: Dict[str, Any]
    ) -> str:
        base_url = variable.value or ""
        if not variable.url_params:
            return base_url

        params = []
        for param in variable.url_params:
            p = param if isinstance(param, dict) else param
            key = p["key"] if isinstance(p, dict) else p.key
            raw_value = p["value"] if isinstance(p, dict) else p.value
            transform = p.get("transform", "none") if isinstance(p, dict) else getattr(p, "transform", "none")

            # Resolve template references like {{email}}
            resolved = self._resolve_value(raw_value, contact_context)
            # Apply transform
            transformed = self._apply_transform(resolved, transform)
            params.append((key, transformed))

        separator = "&" if "?" in base_url else "?"
        query_string = urlencode(params, quote_via=quote)
        return f"{base_url}{separator}{query_string}"

    def _resolve_value(self, value: str, contact_context: Dict[str, Any]) -> str:
        """Resolve {{key}} references from contact context."""
        import re

        def replacer(match):
            key = match.group(1).strip()
            return str(contact_context.get(key, ""))

        return re.sub(r"\{\{(.+?)\}\}", replacer, value)

    def _apply_transform(self, value: str, transform: str) -> str:
        if not value or transform == "none":
            return value
        if transform == "encrypt":
            return encrypt_value(value)
        if transform == "base64":
            return base64.urlsafe_b64encode(value.encode()).decode()
        if transform == "sha256":
            return hashlib.sha256(value.encode()).hexdigest()
        return value
