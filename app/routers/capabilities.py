from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.dependencies import require_project_role
from app.schemas.capabilities import CapabilityInvoke
from app.services.capability_registry import CAPABILITIES, CapabilityError, CapabilityRegistry
from app.services.provider_state_service import ProviderStateService

router = APIRouter(prefix="/projects/{project_id}", tags=["capabilities"])

@router.get("/capabilities")
def list_capabilities(project_id: int, db: Session = Depends(get_db), _auth=Depends(require_project_role("viewer"))):
    return {"capabilities": CapabilityRegistry(db).definitions()}

@router.post("/capabilities/{capability_key}/invoke")
def invoke_capability(payload: CapabilityInvoke, project_id: int, capability_key: str, db: Session = Depends(get_db), auth=Depends(require_project_role("viewer"))):
    capability = CAPABILITIES.get(capability_key)
    required = capability.rbac.get(payload.phase) if capability else None
    rank = {"viewer": 0, "support_agent": 1, "editor": 2, "admin": 3, "owner": 4}
    if required and rank.get(auth.get("role"), -1) < rank[required]:
        raise HTTPException(status_code=403, detail=f"Capability phase requires {required}")
    try:
        return CapabilityRegistry(db).invoke(project_id=project_id, key=capability_key, phase=payload.phase, payload=payload.payload, actor_user_id=auth["user_id"], idempotency_key=payload.idempotency_key, decision_choice_id=payload.decision_choice_id)
    except CapabilityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.get("/provider-operational-state")
def provider_state(project_id: int, refresh: bool = False, db: Session = Depends(get_db), _auth=Depends(require_project_role("viewer"))):
    service = ProviderStateService(db); rows = service.refresh(project_id) if refresh else service.list(project_id)
    return {"providers": [service.serialize(row) for row in rows]}
