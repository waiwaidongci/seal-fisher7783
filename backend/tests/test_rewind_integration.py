"""回卷功能端到端集成测试（仅标准库 + sqlite3，无需 FastAPI/MySQL）。

覆盖：
  1. 录入 → 还原 → 查看依据 → 采用写入原检验 的完整链路；
  2. 区间解不同但工艺计数相同 → 同一组 A/B 计数，允许采用；
  3. 无解 / 歧义 / 超时分别拒绝，且不可采用；
  4. 并发补录：按同一版还原，采用时源数据已改 → 拒绝；重试不重复写入；
  5. 已采用后更正 → 旧采用保留、班次 superseded、需重新还原。

运行：cd backend && python3 tests/test_rewind_integration.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 在导入应用模块前准备 sqlite 环境
_TMP = tempfile.mkdtemp(prefix='rewind-test-')
_SHIM = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_ormshim')
sys.path.insert(0, _SHIM)
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'test.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')

import sqlalchemy  # noqa: E402  # 测试垫片或真实 SQLAlchemy
from sqlalchemy import select  # noqa: E402
import src.config.database as database  # noqa: E402

# 用 sqlite 引擎替换模块级 engine/Session
database.engine = sqlalchemy.create_engine(os.environ['DATABASE_URL'])
database.Session.configure(bind=database.engine)
if hasattr(sqlalchemy, 'configure_default'):
    sqlalchemy.configure_default(database.engine)

from src.config.database import Base, Session  # noqa: E402
from src.models import user, study, observation, run, review, audit  # noqa: E402,F401
from src.models import rewind as models  # noqa: E402
from src.models.study import Study  # noqa: E402
from src.models.observation import Observation  # noqa: E402
from src.models.audit import Audit  # noqa: E402
from src.services import rewind as service  # noqa: E402
from src.services import rewind_solver as solver  # noqa: E402
from src.schemas.rewind import ShiftInput, ShiftPatch, AdoptInput, IntervalInput  # noqa: E402

Base.metadata.create_all(database.engine)


class Actor:
    def __init__(self, email):
        self.email = email
        self.role = 'admin'


ADMIN = Actor('admin@demo.local')
ANALYST = Actor('analyst@demo.local')


def iv(process, mod_total, mod_missed, ts, te, ls, le, upper):
    return IntervalInput(
        process=process, mod_total=mod_total, mod_missed=mod_missed,
        total_start=ts, total_end=te, missed_start=ls, missed_end=le, upper=upper,
    )


def shift_input(name, intervals, total, missed):
    return ShiftInput(name=name, intervals=intervals, actual_total=total, actual_missed=missed)


class RewindFlowTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(database.engine)
        Base.metadata.create_all(database.engine)

    def _create_single_turn_shift(self):
        # 90→10 回卷一圈：产量 20；漏封 45→0 回卷一圈：5
        data = shift_input('班次-单圈', [iv('A', 100, 50, 90, 10, 45, 0, 100)], 20, 5)
        return service.create_shift(Session(), data, ADMIN)

    def test_full_flow_and_write_into_original_inspection(self):
        with Session() as db:
            created = service.create_shift(
                db, shift_input('班次-完整', [iv('A', 100, 50, 90, 10, 45, 0, 100)], 20, 5), ADMIN)
            shift_id = created['id']
            self.assertEqual(created['version'], 1)
            self.assertEqual(created['status'], 'draft')

            restoration = service.restore(db, shift_id, ADMIN)
            self.assertEqual(restoration['status'], 'ok')
            self.assertEqual(restoration['counts']['A'], {'total': 20, 'missed': 5})
            self.assertEqual(restoration['counts']['B'], {'total': 0, 'missed': 0})
            # 还原依据可查看：公式与圈数
            basis = restoration['basis'][0]
            self.assertEqual(basis['formula_total'], '10-90+100×1=20')
            self.assertEqual(basis['formula_missed'], '0-45+50×1=5')

            adoption = service.adopt(
                db, shift_id, restoration['id'], AdoptInput(expected_version=1), ADMIN)
            self.assertEqual(adoption['version'], 1)
            self.assertEqual(adoption['written_keys'],
                             ['REWIND-%d-v1-A' % shift_id, 'REWIND-%d-v1-B' % shift_id])

            # 原检验 observations 已写入 A/B 两行，失败=漏封、合格=总件-漏封
            rows = db.scalars(select(Observation).where(
                Observation.source_key.in_(adoption['written_keys']))).all()
            self.assertEqual(len(rows), 2)
            values = {r.source_key: r.values for r in rows}
            self.assertEqual(values['REWIND-%d-v1-A' % shift_id],
                             {'group': 'A', 'success': 15, 'failure': 5})
            self.assertEqual(values['REWIND-%d-v1-B' % shift_id],
                             {'group': 'B', 'success': 0, 'failure': 0})

            detail = service.get_shift(db, shift_id)
            self.assertEqual(detail['status'], 'adopted')
            self.assertIsNotNone(detail['adoption'])

            # 审计留痕
            actions = {a.action for a in db.scalars(select(Audit))}
            self.assertIn('rewind.shift.create', actions)
            self.assertIn('rewind.restore', actions)
            self.assertIn('rewind.adopt', actions)

    def test_different_interval_solutions_same_process_counts(self):
        # 两个同工艺 A 区间，dx=0、模数 10、上限 20；实点总件 20。
        # 区间分配 (0,20)/(10,10)/(20,0) 不同，但 A 总件恒 20、漏封恒 0。
        data = shift_input('班次-同计数', [
            iv('A', 10, 10, 0, 0, 0, 0, 20),
            iv('A', 10, 10, 0, 0, 0, 0, 20),
        ], 20, 0)
        with Session() as db:
            created = service.create_shift(db, data, ANALYST)
            restoration = service.restore(db, created['id'], ANALYST)
            self.assertEqual(restoration['status'], 'ok')
            self.assertEqual(restoration['feasible_solution_count'], 3)
            self.assertGreaterEqual(len(restoration['samples']), 2)
            # 无论哪种区间解，工艺计数都是同一组
            self.assertEqual(restoration['counts']['A'], {'total': 20, 'missed': 0})
            self.assertEqual(restoration['counts']['B'], {'total': 0, 'missed': 0})
            adoption = service.adopt(
                db, created['id'], restoration['id'], AdoptInput(expected_version=1), ANALYST)
            self.assertEqual(adoption['counts']['A']['total'], 20)

    def test_no_solution_ambiguous_timeout_rejected_and_not_adoptable(self):
        from fastapi import HTTPException
        # 无解：实点不可达
        with Session() as db:
            created = service.create_shift(db, shift_input(
                '班次-无解', [iv('A', 100, 100, 0, 10, 0, 2, 100)], 999, 0), ADMIN)
            r = service.restore(db, created['id'], ADMIN)
            self.assertEqual(r['status'], solver.NO_SOLUTION)
            with self.assertRaises(HTTPException) as ctx:
                service.adopt(db, created['id'], r['id'], AdoptInput(expected_version=1), ADMIN)
            self.assertEqual(ctx.exception.status_code, 409)

        # 歧义：A/B 各一区间、模数 10、dx=0，实点 20 可在工艺间转移
        with Session() as db:
            created = service.create_shift(db, shift_input('班次-歧义', [
                iv('A', 10, 10, 0, 0, 0, 0, 20),
                iv('B', 10, 10, 0, 0, 0, 0, 20),
            ], 20, 0), ADMIN)
            r = service.restore(db, created['id'], ADMIN)
            self.assertEqual(r['status'], solver.AMBIGUOUS)
            with self.assertRaises(HTTPException):
                service.adopt(db, created['id'], r['id'], AdoptInput(expected_version=1), ADMIN)

        # 超时：求解器注入立即超时的时钟
        with Session() as db:
            created = service.create_shift(db, shift_input(
                '班次-超时', [iv('A', 100, 100, 0, 1, 0, 0, 5000)], 100, 0), ADMIN)

            class Boom:
                limit = 5.0
                def check(self):
                    raise solver.SolverError(solver.TIMEOUT, '超时')

            original = solver.Deadline
            solver.Deadline = lambda seconds: Boom()
            try:
                r = service.restore(db, created['id'], ADMIN)
            finally:
                solver.Deadline = original
            self.assertEqual(r['status'], solver.TIMEOUT)
            with self.assertRaises(HTTPException):
                service.adopt(db, created['id'], r['id'], AdoptInput(expected_version=1), ADMIN)

    def test_concurrent_amend_stale_version_rejects_then_retry_succeeds_once(self):
        from fastapi import HTTPException
        with Session() as db:
            created = service.create_shift(db, shift_input(
                '班次-并发', [iv('A', 100, 50, 90, 10, 45, 0, 100)], 20, 5), ADMIN)
            shift_id = created['id']
            v1_restoration = service.restore(db, shift_id, ADMIN)
            self.assertEqual(v1_restoration['version'], 1)

            # 另一会话补录（源数据已改）：版本升到 v2
            with Session() as db2:
                service.amend_shift(db2, shift_id, ShiftPatch(actual_total=21), ADMIN)
            stale = service.get_shift(db, shift_id)
            self.assertEqual(stale['version'], 2)

            # 仍拿 v1 的还原与 expected_version=1 去采用 → 拒绝
            with self.assertRaises(HTTPException) as ctx:
                service.adopt(db, shift_id, v1_restoration['id'],
                              AdoptInput(expected_version=1), ADMIN)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn('过期', ctx.exception.detail)

            # v1 还原本身也因版本落后被拒（即便前端回传 expected_version=2）
            with self.assertRaises(HTTPException) as ctx:
                service.adopt(db, shift_id, v1_restoration['id'],
                              AdoptInput(expected_version=2), ADMIN)
            self.assertIn('重新还原', ctx.exception.detail)

            # 按当前版重新还原 → 实点 21 与候选 20 不符 → 无解，仍不能采用
            r2_no = service.restore(db, shift_id, ADMIN)
            self.assertEqual(r2_no['status'], solver.NO_SOLUTION)

            # 更正为可达实点后重新还原并采用，只写一次
            service.amend_shift(db, shift_id, ShiftPatch(actual_total=20, actual_missed=5), ADMIN)
            r2 = service.restore(db, shift_id, ADMIN)
            self.assertEqual(r2['version'], 3)
            adoption = service.adopt(
                db, shift_id, r2['id'], AdoptInput(expected_version=3), ADMIN)
            self.assertEqual(adoption['version'], 3)

            # 重试（重复采用）→ 拒绝，且原检验没有重复行
            with self.assertRaises(HTTPException) as ctx:
                service.adopt(db, shift_id, r2['id'], AdoptInput(expected_version=3), ADMIN)
            self.assertEqual(ctx.exception.status_code, 409)
            count = len(db.scalars(select(Observation).where(
                Observation.source_key.like(f'REWIND-{shift_id}-%'))).all())
            self.assertEqual(count, 2)

    def test_adopt_then_amend_keeps_history_and_requires_new_restoration(self):
        from fastapi import HTTPException
        with Session() as db:
            created = service.create_shift(db, shift_input(
                '班次-采用后更正', [iv('A', 100, 50, 90, 10, 45, 0, 100)], 20, 5), ADMIN)
            shift_id = created['id']
            r1 = service.restore(db, shift_id, ADMIN)
            first = service.adopt(db, shift_id, r1['id'], AdoptInput(expected_version=1), ADMIN)

            # 采用后允许更正：状态转 superseded，版本 +1，旧采用保留
            amended = service.amend_shift(db, shift_id, ShiftPatch(actual_total=20, actual_missed=4), ADMIN)
            self.assertEqual(amended['version'], 2)
            self.assertEqual(amended['status'], 'superseded')
            self.assertIsNotNone(amended['adoption'])
            self.assertEqual(amended['adoption']['id'], first['id'])

            # 旧还原不能再采用
            with self.assertRaises(HTTPException):
                service.adopt(db, shift_id, r1['id'], AdoptInput(expected_version=2), ADMIN)

            # 新还原（漏封 4 与候选 5 不符 → 无解），历史还原链均可追溯
            r2 = service.restore(db, shift_id, ADMIN)
            self.assertEqual(r2['status'], solver.NO_SOLUTION)
            detail = service.get_shift(db, shift_id)
            self.assertEqual(len(detail['restorations']), 2)

    def test_validation_missed_gt_total_and_bad_readings(self):
        from fastapi import HTTPException
        with Session() as db:
            with self.assertRaises(HTTPException) as ctx:
                service.create_shift(db, shift_input(
                    '班次-非法', [iv('A', 100, 100, 0, 10, 0, 20, 100)], 10, 11), ADMIN)
            self.assertEqual(ctx.exception.status_code, 422)
            with self.assertRaises(HTTPException):
                service.create_shift(db, shift_input(
                    '班次-越界', [iv('A', 100, 100, 0, 100, 0, 1, 100)], 10, 1), ADMIN)


if __name__ == '__main__':
    unittest.main(verbosity=2)
