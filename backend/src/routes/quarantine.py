from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.domain.spec import SPEC
from src.models.source import Dataset
from src.schemas.requests import ReleaseRequest
from src.services import quarantine as service
from src.services.audit import record

router = APIRouter(prefix='/quarantine')


@router.get('')
def index(db: Session = Depends(connection), actor: User = Depends(current)):
    """Every quarantine group, current state and its release destination."""
    return service.snapshot(db)


@router.post('/release')
def release(body: ReleaseRequest, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN))):
    """Release one corrected group after whole-group validation.

    The corrected batch must already be imported.  Releasing the same corrected
    group twice is idempotent: the second call writes no new record.
    """
    dataset = db.get(Dataset, body.dataset_id)
    if not dataset:
        raise HTTPException(404, '找不到指定的数据批次')
    allowed = SPEC.get('release_destinations') or []
    if allowed and body.destination not in allowed:
        raise ValueError('请选择项目支持的放行去向')
    records, created = service.release(db, dataset.id, dataset.rows, body.group_id, actor.username,
                                       body.destination, body.note or None)
    if created:
        record(db, actor.username, 'quarantine.released', body.group_id,
               signatures=[item.signature for item in records], destination=body.destination,
               dataset_id=dataset.id)
    db.commit()
    return {"group_id": body.group_id, "status": records[0].status, "destination": body.destination,
            "signatures": [item.signature for item in records], "created": created}
