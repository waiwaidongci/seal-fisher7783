"""题目两个验收点的端到端实证脚本，输出可人工核对的结论。

A. 区间解不同但工艺计数相同：允许多个区间还原，但 A/B 计数唯一，可采用。
B. 采用撞上更正：并发下版本过期即拒绝，重试不重复写入。

运行：cd backend && python3 tests/verify_acceptance.py
"""
import os
import sys
import tempfile
import threading

_TMP = tempfile.mkdtemp(prefix='rewind-accept-')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '_ormshim'))
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'accept.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')

import sqlalchemy  # noqa: E402
from sqlalchemy import select  # noqa: E402
import src.config.database as database  # noqa: E402

database.engine = sqlalchemy.create_engine(os.environ['DATABASE_URL'])
database.Session.configure(bind=database.engine)
sqlalchemy.configure_default(database.engine)

from src.config.database import Base, Session  # noqa: E402
from src.models import user, study, observation, run, review, audit  # noqa: E402,F401
from src.models.observation import Observation  # noqa: E402
from src.services import rewind as service  # noqa: E402
from src.schemas.rewind import ShiftInput, ShiftPatch, AdoptInput, IntervalInput  # noqa: E402

Base.metadata.create_all(database.engine)


class Actor:
    role = 'admin'
    def __init__(self, e):
        self.email = e


ADMIN = Actor('admin@demo.local')


def iv(process, mt, ml, ts, te, ls, le, up):
    return IntervalInput(process=process, mod_total=mt, mod_missed=ml,
                         total_start=ts, total_end=te, missed_start=ls, missed_end=le, upper=up)


def acceptance_a():
    print('\n=== 验收 A：区间解不同，但工艺计数相同 ===')
    data = ShiftInput(name='验收A', intervals=[
        iv('A', 10, 10, 0, 0, 0, 0, 20),
        iv('A', 10, 10, 0, 0, 0, 0, 20),
    ], actual_total=20, actual_missed=0)
    with Session() as db:
        shift = service.create_shift(db, data, ADMIN)
        r = service.restore(db, shift['id'], ADMIN)
        print('还原状态:', r['status'])
        print('可行还原组数:', r['feasible_solution_count'])
        allocations = {(s[0]['total_count'], s[1]['total_count']) for s in r['samples']}
        print('采样到的不同区间分配:', sorted(allocations))
        print('工艺计数 A:', r['counts']['A'], ' B:', r['counts']['B'])
        assert r['status'] == 'ok'
        assert r['feasible_solution_count'] == 3
        assert len(allocations) >= 2, '应存在不同的区间解'
        assert r['counts']['A'] == {'total': 20, 'missed': 0}
        adoption = service.adopt(db, shift['id'], r['id'],
                                 AdoptInput(expected_version=shift['version']), ADMIN)
        print('采用成功，写入行:', adoption['written_keys'])
        assert len(adoption['written_keys']) == 2
    print('结论 A：区间解有多种，但所有可行解给出同一组 A/B 计数，判定唯一并允许采用。✓')


def acceptance_b():
    print('\n=== 验收 B：采用撞上更正 ===')
    data = ShiftInput(name='验收B', intervals=[
        iv('A', 100, 50, 90, 10, 45, 0, 100)], actual_total=20, actual_missed=5)
    with Session() as db:
        shift = service.create_shift(db, data, ADMIN)
        r1 = service.restore(db, shift['id'], ADMIN)
        sid, rid = shift['id'], r1['id']

    # 另一会话先更正（源数据已改，版本 v1 -> v2）
    with Session() as db:
        service.amend_shift(db, sid, ShiftPatch(actual_total=20, actual_missed=4), ADMIN)

    conflict = {}
    with Session() as db:
        try:
            service.adopt(db, sid, rid, AdoptInput(expected_version=1), ADMIN)
        except Exception as exc:  # noqa: BLE001
            conflict = {'code': exc.status_code, 'detail': exc.detail}
    print('基于旧版 v1 采用被拒:', conflict)
    assert conflict['code'] == 409

    # 并发：8 个线程同时采用当前版还原，只能一个成功，原检验不重复
    with Session() as db:
        r2 = service.restore(db, sid, ADMIN)
        assert r2['status'] == 'no_solution'  # 漏封改 4，与候选 5 不符
        service.amend_shift(db, sid, ShiftPatch(actual_total=20, actual_missed=5), ADMIN)
        r3 = service.restore(db, sid, ADMIN)
        rid3, ver3 = r3['id'], r3['version']

    outcomes = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        with Session() as db:
            try:
                service.adopt(db, sid, rid3, AdoptInput(expected_version=ver3), ADMIN)
                outcomes.append('ok')
            except Exception:  # noqa: BLE001
                outcomes.append('409')

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with Session() as db:
        rows = db.scalars(select(Observation).where(
            Observation.source_key.like(f'REWIND-{sid}-%'))).all()
    print('并发采用结果:', outcomes)
    print('原检验实际写入行数:', len(rows))
    assert outcomes.count('ok') == 1
    assert len(rows) == 2
    print('结论 B：源数据改动后旧版采用被拒；并发重试仅一次写入，无重复。✓')


if __name__ == '__main__':
    acceptance_a()
    acceptance_b()
    print('\n两项验收全部通过。')
