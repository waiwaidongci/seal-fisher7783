from fastapi import APIRouter, Depends
from sqlalchemy.exc import OperationalError

from src.config.database import get_db
from src.middlewares.auth import current_user, writer
from src.schemas.rewind import ShiftInput, ShiftPatch, AdoptInput
from src.controllers import rewind as controller

router = APIRouter(prefix='/rewind', tags=['rewind'])


@router.get('/shifts')
def list_shifts(db=Depends(get_db), user=Depends(current_user)):
    return controller.list_shifts(db)


@router.post('/shifts', status_code=201)
def create_shift(data: ShiftInput, db=Depends(get_db), user=Depends(writer)):
    return controller.create_shift(db, data, user)


@router.get('/shifts/{shift_id}')
def get_shift(shift_id: int, db=Depends(get_db), user=Depends(current_user)):
    return controller.get_shift(db, shift_id)


@router.patch('/shifts/{shift_id}')
def amend_shift(shift_id: int, patch: ShiftPatch, db=Depends(get_db), user=Depends(writer)):
    return controller.amend_shift(db, shift_id, patch, user)


@router.post('/shifts/{shift_id}/restorations', status_code=201)
def restore(shift_id: int, db=Depends(get_db), user=Depends(writer)):
    return controller.restore(db, shift_id, user)


@router.get('/shifts/{shift_id}/restorations/{restoration_id}')
def get_restoration(shift_id: int, restoration_id: int, db=Depends(get_db),
                    user=Depends(current_user)):
    return controller.get_restoration(db, shift_id, restoration_id)


@router.post('/shifts/{shift_id}/restorations/{restoration_id}/adopt', status_code=201)
def adopt(shift_id: int, restoration_id: int, data: AdoptInput,
          db=Depends(get_db), user=Depends(writer)):
    try:
        return controller.adopt(db, shift_id, restoration_id, data, user)
    except OperationalError as exc:  # 锁等待超时等并发问题 → 明确 409，不产生部分写入
        db.rollback()
        from fastapi import HTTPException
        raise HTTPException(409, '采用并发冲突（锁等待），未写入，请刷新后重试') from exc
