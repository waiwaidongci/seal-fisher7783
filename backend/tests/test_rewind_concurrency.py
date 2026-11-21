"""并发采用压力测试（标准库 + sqlite + 测试垫片）。

验证题目要求：
  并发补录时按同一版还原，采用时源数据已改就拒绝，重试不重复写入；
  多个线程同时采用同一还原，至多一个成功，原检验绝不出现重复行。

运行：cd backend && python3 tests/test_rewind_concurrency.py
"""
import os
import sys
import tempfile
import threading
import unittest

_TMP = tempfile.mkdtemp(prefix='rewind-conc-')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '_ormshim'))
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'conc.db')
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
from src.schemas.rewind import ShiftInput, AdoptInput, IntervalInput  # noqa: E402

Base.metadata.create_all(database.engine)


class Actor:
    role = 'admin'
    def __init__(self, email):
        self.email = email


def make_shift():
    iv = IntervalInput(process='A', mod_total=100, mod_missed=50,
                       total_start=90, total_end=10, missed_start=45, missed_end=0, upper=100)
    return ShiftInput(name='并发班次', intervals=[iv], actual_total=20, actual_missed=5)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(database.engine)
        Base.metadata.create_all(database.engine)

    def test_parallel_adopt_only_one_wins(self):
        actor = Actor('admin@demo.local')
        with Session() as db:
            shift = service.create_shift(db, make_shift(), actor)
            restoration = service.restore(db, shift['id'], actor)
            shift_id, rid, version = shift['id'], restoration['id'], shift['version']

        outcomes = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()  # 最大化同时进入 adopt 的概率
            with Session() as db:
                try:
                    service.adopt(db, shift_id, rid, AdoptInput(expected_version=version), actor)
                    outcomes.append('ok')
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(getattr(exc, 'status_code', 'error'))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count('ok'), 1, f'应恰好一次成功，实际：{outcomes}')
        self.assertEqual(len(outcomes), 8)
        with Session() as db:
            keys = db.scalars(
                select(Observation).where(
                    Observation.source_key.like(f'REWIND-{shift_id}-%'))).all()
            self.assertEqual(len(keys), 2, '原检验只能写入 A/B 两行，重试不得重复写入')

    def test_adopt_collides_with_concurrent_amend(self):
        actor = Actor('admin@demo.local')
        with Session() as db:
            shift = service.create_shift(db, make_shift(), actor)
            restoration = service.restore(db, shift['id'], actor)
            shift_id, rid = shift['id'], restoration['id']

        result = {}

        def amender():
            with Session() as db:
                from src.schemas.rewind import ShiftPatch
                service.amend_shift(db, shift_id, ShiftPatch(actual_total=20, actual_missed=4), actor)
                result['amend'] = 'ok'

        def adopter():
            with Session() as db:
                try:
                    service.adopt(db, shift_id, rid, AdoptInput(expected_version=1), actor)
                    result['adopt'] = 'ok'
                except Exception as exc:  # noqa: BLE001
                    result['adopt'] = getattr(exc, 'status_code', 'error')

        t1 = threading.Thread(target=amender)
        t2 = threading.Thread(target=adopter)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(result['amend'], 'ok')
        with Session() as db:
            latest = service.get_shift(db, shift_id)
            if result['adopt'] == 'ok':
                # 采用先提交：更正随后升版，班次必须转为 superseded 且旧采用保留
                self.assertEqual(latest['status'], 'superseded')
                self.assertEqual(latest['version'], 2)
                self.assertIsNotNone(latest['adoption'])
            else:
                # 更正先提交：采用依据版本过期，必须 409 拒绝，且无采用记录
                self.assertEqual(result['adopt'], 409)
                self.assertIsNone(latest['adoption'])
            # 任一交错下都不得产生重复/孤立的原检验行
            from sqlalchemy import select as _sel
            rows = db.scalars(_sel(Observation).where(
                Observation.source_key.like(f'REWIND-{shift_id}-%'))).all()
            self.assertLessEqual(len(rows), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
