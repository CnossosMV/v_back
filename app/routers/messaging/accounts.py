"""
Account Management API — CRUD for B2B accounts.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models.messaging import Account, ContactAccountAssociation, MessagingUser
from app.schemas.identity import AccountCreate, AccountUpdate, AccountResponse

router = APIRouter(prefix="/api/v1/projects/{project_id}/accounts", tags=["accounts"])


@router.get("")
def list_accounts(
    project_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """List accounts with contact count."""
    query = db.query(Account).filter(Account.project_id == project_id)
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (Account.name.ilike(search_filter)) |
            (Account.external_id.ilike(search_filter))
        )
    total = query.count()
    accounts = query.order_by(Account.created_at.desc()).offset(skip).limit(limit).all()

    results = []
    for a in accounts:
        resp = AccountResponse.model_validate(a)
        resp.contact_count = db.query(ContactAccountAssociation).filter(
            ContactAccountAssociation.account_id == a.id
        ).count()
        results.append(resp)
    return {"items": results, "total": total}


@router.get("/{account_id}")
def get_account(project_id: int, account_id: int, db: Session = Depends(get_db)):
    """Get account detail with associated contacts."""
    account = db.query(Account).filter(
        Account.id == account_id,
        Account.project_id == project_id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    associations = db.query(ContactAccountAssociation).filter(
        ContactAccountAssociation.account_id == account_id
    ).all()

    contacts = []
    for assoc in associations:
        user = db.query(MessagingUser).filter(MessagingUser.id == assoc.user_id).first()
        if user:
            contacts.append({
                "id": user.id,
                "external_id": user.external_id,
                "email": user.email,
                "name": user.name,
                "phone": user.phone,
                "role": assoc.role,
                "status": user.status
            })

    result = AccountResponse.model_validate(account).model_dump()
    result["contact_count"] = len(contacts)
    result["contacts"] = contacts
    return result


@router.post("", status_code=201)
def create_account(project_id: int, data: AccountCreate, db: Session = Depends(get_db)):
    """Create a new account."""
    existing = db.query(Account).filter(
        Account.project_id == project_id,
        Account.external_id == data.external_id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Account with this external_id already exists")

    account = Account(
        project_id=project_id,
        external_id=data.external_id,
        name=data.name,
        properties=data.properties
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return AccountResponse.model_validate(account)


@router.patch("/{account_id}")
def update_account(
    project_id: int, account_id: int,
    data: AccountUpdate, db: Session = Depends(get_db)
):
    """Update an account."""
    account = db.query(Account).filter(
        Account.id == account_id,
        Account.project_id == project_id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if data.name is not None:
        account.name = data.name
    if data.properties is not None:
        account.properties = data.properties

    db.commit()
    db.refresh(account)
    return AccountResponse.model_validate(account)


@router.delete("/{account_id}")
def delete_account(project_id: int, account_id: int, db: Session = Depends(get_db)):
    """Delete an account and its associations."""
    account = db.query(Account).filter(
        Account.id == account_id,
        Account.project_id == project_id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    db.delete(account)
    db.commit()
    return {"success": True}
