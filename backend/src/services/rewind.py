"""回卷还原业务服务：录入、版本化还原、乐观并发采用。

并发口径
--------
1. 每次录入/补录/更正都会让班次 version +1，并把旧还原保留为历史依据；
2. 还原结果记录计算时的 version；采用时要求 expected_version == 当前 version，
   且该还原 status=ok、班次尚无采用记录，否则拒绝（409）；
3. 采用在单个事务内完成“锁班次 → 校验版本 → 写原检验 → 写采用记录 → 置状态”，
   唯一约束兜底，并发重试不会重复写入。
"""
import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models.rewind import RewindShift, RewindInterval, RewindRestoration, RewindAdoption
from src.models.observation import Observation
from src.services import rewind_solver as solver
from src.services.audits import log
from src.utils.serializers import record, now, checksum

SOLVE_SECONDS = 5.0


# ---------- 录入 ----------

def _payload_from_shift(shift, intervals):
    return {
        'intervals': [
            {
                'process': iv.process,
                'mod_total': iv.mod_total, 'mod_missed': iv.mod_missed,
                'total_start': iv.total_start, 'total_end': iv.total_end,
                'missed_start': iv.missed_start, 'missed_end': iv.missed_end,
                'upper': iv.upper,
            }
            for iv in intervals
        ],
        'actual_total': shift.actual_total,
        'actual_missed': shift.actual_missed,
    }


def create_shift(db, data, user):
    payload = data.model_dump()
    try:
        solver.validate_shift(payload)
    except solver.SolverError as exc:
        raise HTTPException(422, exc.message)
    ts = now()
    shift = RewindShift(
        name=data.name, actual_total=data.actual_total,
        actual_missed=data.actual_missed, version=1, status='draft',
        created_by=user.email, created_at=ts, updated_at=ts,
    )
    db.add(shift)
    db.flush()
    for seq, iv in enumerate(data.intervals, 1):
        db.add(RewindInterval(shift_id=shift.id, seq=seq, **iv.model_dump()))
    log(db, user, 'rewind.shift.create', f'{shift.id} v1: {data.name}')
    db.commit()
    return get_shift(db, shift.id)


def list_shifts(db):
    shifts = db.scalars(select(RewindShift).order_by(RewindShift.id.desc())).all()
    out = []
    for shift in shifts:
        item = record(shift)
        item['interval_count'] = len(_intervals(db, shift.id))
        adoption = db.scalar(select(RewindAdoption).where(RewindAdoption.shift_id == shift.id))
        item['adopted'] = adoption is not None
        out.append(item)
    return out


def _intervals(db, shift_id):
    return list(db.scalars(
        select(RewindInterval).where(RewindInterval.shift_id == shift_id).order_by(RewindInterval.seq)
    ))


def require_shift(db, shift_id):
    shift = db.get(RewindShift, shift_id)
    if not shift:
        raise HTTPException(404, '班次不存在')
    return shift


def get_shift(db, shift_id):
    shift = require_shift(db, shift_id)
    item = record(shift)
    item['intervals'] = [record(iv) for iv in _intervals(db, shift_id)]
    item['restorations'] = [record(r) for r in db.scalars(
        select(RewindRestoration).where(RewindRestoration.shift_id == shift_id)
        .order_by(RewindRestoration.id.desc()))]
    adoption = db.scalar(select(RewindAdoption).where(RewindAdoption.shift_id == shift_id))
    item['adoption'] = record(adoption) if adoption else None
    return item


def amend_shift(db, shift_id, patch, user):
    """补录/更正：改动数据即升版；已采用班次允许更正但旧采用保留、状态转 superseded。"""
    # 行级加锁，与 adopt 互斥，保证“采用撞上更正”按提交顺序明确裁决；
    # 锁内为当前读，能看到对方已提交的最新状态（含 adopted）。
    shift = db.execute(
        select(RewindShift).where(RewindShift.id == shift_id).with_for_update()
    ).scalar_one()
    current = _intervals(db, shift_id)
    merged = {
        'name': patch.name if patch.name is not None else shift.name,
        'actual_total': patch.actual_total if patch.actual_total is not None else shift.actual_total,
        'actual_missed': patch.actual_missed if patch.actual_missed is not None else shift.actual_missed,
        'intervals': [iv.model_dump() for iv in patch.intervals] if patch.intervals is not None
        else [
            {
                'process': iv.process, 'mod_total': iv.mod_total, 'mod_missed': iv.mod_missed,
                'total_start': iv.total_start, 'total_end': iv.total_end,
                'missed_start': iv.missed_start, 'missed_end': iv.missed_end, 'upper': iv.upper,
            } for iv in current
        ],
    }
    try:
        solver.validate_shift(merged)
    except solver.SolverError as exc:
        raise HTTPException(422, exc.message)

    shift.name = merged['name']
    shift.actual_total = merged['actual_total']
    shift.actual_missed = merged['actual_missed']
    shift.version += 1
    shift.updated_at = now()
    if shift.status == 'adopted':
        shift.status = 'superseded'
    for iv in current:
        db.delete(iv)
    db.flush()
    for seq, iv in enumerate(merged['intervals'], 1):
        db.add(RewindInterval(shift_id=shift.id, seq=seq, **iv))
    log(db, user, 'rewind.shift.amend', f'{shift.id} -> v{shift.version}')
    db.commit()
    return get_shift(db, shift.id)


# ---------- 还原 ----------

def restore(db, shift_id, user):
    shift = require_shift(db, shift_id)
    intervals = _intervals(db, shift_id)
    payload = _payload_from_shift(shift, intervals)
    started = time.perf_counter()
    try:
        result = solver.solve(payload, seconds=SOLVE_SECONDS)
        status, message = solver.OK, ''
    except solver.SolverError as exc:
        result, status, message = None, exc.code, exc.message
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    row = RewindRestoration(
        shift_id=shift.id, version=shift.version, status=status, message=message,
        counts=result['counts'] if result else None,
        basis=result['intervals'] if result else None,
        samples=result['samples'] if result else None,
        feasible_solution_count=(
            None if not result or result['feasible_solution_count'] == 'many'
            else result['feasible_solution_count']),
        elapsed_ms=elapsed_ms, created_by=user.email, created_at=now(),
    )
    db.add(row)
    log(db, user, 'rewind.restore', f'{shift.id} v{shift.version} -> {status} ({elapsed_ms}ms)')
    db.commit()
    return record(row)


def get_restoration(db, shift_id, restoration_id):
    row = db.get(RewindRestoration, restoration_id)
    if not row or row.shift_id != shift_id:
        raise HTTPException(404, '还原记录不存在')
    return record(row)


# ---------- 采用（乐观并发，写入原检验） ----------

def adopt(db, shift_id, restoration_id, data, user):
    shift = require_shift(db, shift_id)
    restoration = db.get(RewindRestoration, restoration_id)
    if not restoration or restoration.shift_id != shift_id:
        raise HTTPException(404, '还原记录不存在')

    # 行级序列化：同班次并发采用在数据库层排队，避免读后写竞争
    locked = db.execute(
        select(RewindShift).where(RewindShift.id == shift_id).with_for_update()
    ).scalar_one()

    existing = db.scalar(select(RewindAdoption).where(RewindAdoption.shift_id == shift_id))
    if existing:
        raise HTTPException(409, '该班次已采用过还原，不能重复写入；如需更正请补录后重新还原')
    if restoration.status != solver.OK:
        raise HTTPException(409, f'还原结论为 {restoration.status}，不可采用')
    if data.expected_version != locked.version:
        raise HTTPException(409, f'采用依据版本 v{data.expected_version} 已过期，当前为 v{locked.version}，请重新查看还原依据')
    if restoration.version != locked.version:
        raise HTTPException(409, f'该还原基于 v{restoration.version}，当前数据为 v{locked.version}，请按当前版重新还原')
    if locked.status == 'superseded':
        raise HTTPException(409, '班次数据已被更正，请基于当前版本重新还原后再采用')

    counts = restoration.counts
    written = []
    # 写入“原检验”：复用既有 observations 表（工艺 A/B 计数），source_key 带班次与版本，
    # 自然键 (study_id, source_key) 防重复。此处挂到专用来源批次，避免污染 Fisher 业务批次。
    study_id = _ensure_target_study(db, user)
    for process in solver.PROCESSES:
        source_key = f'REWIND-{shift.id}-v{locked.version}-{process}'
        dup = db.scalar(select(Observation).where(
            Observation.study_id == study_id, Observation.source_key == source_key))
        if dup:
            raise HTTPException(409, f'原检验已存在 {source_key}，为避免重复写入已拒绝')
        total = int(counts[process]['total'])
        missed = int(counts[process]['missed'])
        row = Observation(
            study_id=study_id, source_key=source_key,
            values={'group': process, 'success': total - missed, 'failure': missed},
        )
        db.add(row)
        written.append(source_key)

    adoption = RewindAdoption(
        shift_id=shift.id, restoration_id=restoration.id, version=locked.version,
        counts=counts, written_keys=written, created_by=user.email, created_at=now(),
    )
    db.add(adoption)
    locked.status = 'adopted'
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '采用冲突，已拒绝重复写入')
    log(db, user, 'rewind.adopt',
        f'{shift.id} v{locked.version} restoration={restoration.id} keys={written} checksum={checksum(counts)}')
    db.commit()
    return record(adoption)


def _ensure_target_study(db, user):
    """原检验写入目标：固定的“回卷还原并入”批次，幂等获取。"""
    from src.models.study import Study
    name = '回卷还原·原检验并入'
    study = db.scalar(select(Study).where(Study.name == name))
    if not study:
        study = Study(name=name, source_filename='rewind-adoption.json',
                      description='表盘回卷还原采用后写入的工艺 A/B 计数')
        db.add(study)
        db.flush()
        log(db, user, 'study.create', name)
    return study.id
