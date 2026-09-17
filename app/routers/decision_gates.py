from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.schemas.decision_gates import DecisionChoiceCreate
from app.services.decision_gate_service import DecisionGateError, DecisionGateService

router = APIRouter(prefix="/projects/{project_id}/decision-gates", tags=["decision-gates"])


def evaluation_dict(row):
    return {"id": row.id, "project_id": row.project_id, "gate_type": row.gate_type, "subject_type": row.subject_type, "subject_id": row.subject_id, "subject_version": row.subject_version, "status": row.status, "method": row.method, "baseline": row.baseline, "options": row.options, "provenance": row.provenance, "evaluated_at": row.evaluated_at, "expires_at": row.expires_at}


@router.get("/{evaluation_id}")
def get_decision(project_id: int, evaluation_id: int, db: Session = Depends(get_db), _auth=Depends(require_project_role("viewer"))):
    row = DecisionGateService(db).get(project_id, evaluation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Decision evaluation not found")
    return evaluation_dict(row)


@router.post("/{evaluation_id}/choose")
def choose_decision(payload: DecisionChoiceCreate, project_id: int, evaluation_id: int, db: Session = Depends(get_db), auth=Depends(require_project_role("admin"))):
    try:
        row = DecisionGateService(db).choose(project_id, evaluation_id, payload.option_key, auth["user_id"], payload.reason)
        return {"id": row.id, "project_id": row.project_id, "evaluation_id": row.evaluation_id, "option_key": row.option_key, "consequence_snapshot": row.consequence_snapshot, "chosen_by_user_id": row.chosen_by_user_id, "chosen_at": row.chosen_at, "execution_type": row.execution_type, "execution_id": row.execution_id}
    except DecisionGateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
