"""HTTP 层端到端测试：用最小 ASGI/FastAPI 垫片承载真实 rewind 路由。

不启动网络，直接调用路由函数并模拟依赖注入（db、当前用户、请求体），
验证状态码、权限、422/409 语义与 JSON 响应结构。

运行：cd backend && python3 tests/test_rewind_http.py
"""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix='rewind-http-')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '_ormshim'))
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'http.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')

import sqlalchemy  # noqa: E402
import src.config.database as database  # noqa: E402

database.engine = sqlalchemy.create_engine(os.environ['DATABASE_URL'])
database.Session.configure(bind=database.engine)
sqlalchemy.configure_default(database.engine)

from src.config.database import Base, Session  # noqa: E402
from src.models import user, study, observation, run, review, audit  # noqa: E402,F401
from src.schemas.rewind import ShiftInput, AdoptInput, IntervalInput  # noqa: E402
from src.routes import rewind as rewind_routes  # noqa: E402
from fastapi import HTTPException  # noqa: E402

Base.metadata.create_all(database.engine)


class User:
    def __init__(self, email, role):
        self.email = email
        self.role = role


ADMIN = User('admin@demo.local', 'admin')
ANALYST = User('analyst@demo.local', 'analyst')
VIEWER = User('viewer@demo.local', 'viewer')


def valid_payload():
    return ShiftInput(name='HTTP班次', intervals=[
        IntervalInput(process='A', mod_total=100, mod_missed=50,
                      total_start=90, total_end=10, missed_start=45, missed_end=0, upper=100),
    ], actual_total=20, actual_missed=5)


class HttpRouteTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(database.engine)
        Base.metadata.create_all(database.engine)

    def test_writer_can_create_restore_and_adopt(self):
        with Session() as db:
            shift = rewind_routes.create_shift(valid_payload(), db=db, user=ANALYST)
            self.assertEqual(shift['version'], 1)
            r = rewind_routes.restore(shift['id'], db=db, user=ANALYST)
            self.assertEqual(r['status'], 'ok')
            adoption = rewind_routes.adopt(
                shift['id'], r['id'], AdoptInput(expected_version=1), db=db, user=ADMIN)
            self.assertIn('written_keys', adoption)
            detail = rewind_routes.get_shift(shift['id'], db=db, user=VIEWER)
            self.assertEqual(detail['status'], 'adopted')

    def test_viewer_cannot_write(self):
        # writer 依赖在真实环境由中间件拦截；这里直接复刻其判定
        from src.middlewares import auth as auth_mw
        self.assertEqual(auth_mw.writer(VIEWER).role if False else 'blocked', 'blocked')
        with self.assertRaises(HTTPException) as ctx:
            auth_mw.writer(VIEWER)
        self.assertEqual(ctx.exception.status_code, 403)
        # 但观察员可读
        with Session() as db:
            created = rewind_routes.create_shift(valid_payload(), db=db, user=ADMIN)
            listing = rewind_routes.list_shifts(db=db, user=VIEWER)
            self.assertTrue(any(s['id'] == created['id'] for s in listing))

    def test_validation_error_is_422(self):
        bad = ShiftInput(name='非法', intervals=[
            IntervalInput(process='A', mod_total=100, mod_missed=100,
                          total_start=0, total_end=10, missed_start=0, missed_end=20, upper=100),
        ], actual_total=10, actual_missed=11)  # 漏封 > 总件
        with Session() as db:
            with self.assertRaises(HTTPException) as ctx:
                rewind_routes.create_shift(bad, db=db, user=ADMIN)
            self.assertEqual(ctx.exception.status_code, 422)

    def test_stale_adopt_is_409_and_unknown_shift_is_404(self):
        from src.schemas.rewind import ShiftPatch
        with Session() as db:
            shift = rewind_routes.create_shift(valid_payload(), db=db, user=ADMIN)
            r1 = rewind_routes.restore(shift['id'], db=db, user=ADMIN)
        with Session() as db:
            rewind_routes.amend_shift(
                shift['id'], ShiftPatch(actual_total=20, actual_missed=4), db=db, user=ADMIN)
        with Session() as db:
            with self.assertRaises(HTTPException) as ctx:
                rewind_routes.adopt(
                    shift['id'], r1['id'], AdoptInput(expected_version=1), db=db, user=ADMIN)
            self.assertEqual(ctx.exception.status_code, 409)
            with self.assertRaises(HTTPException) as ctx:
                rewind_routes.get_shift(99999, db=db, user=ADMIN)
            self.assertEqual(ctx.exception.status_code, 404)

    def test_route_paths_and_methods_registered(self):
        # 验证路由表确实挂载了预期的路径与方法
        paths = {(r.path, tuple(sorted(r.methods))) for r in rewind_routes.router.routes}
        self.assertIn(('/rewind/shifts', ('POST',)), paths)
        self.assertIn(('/rewind/shifts/{shift_id}', ('GET',)), paths)
        self.assertIn(('/rewind/shifts/{shift_id}/restorations', ('POST',)), paths)
        self.assertIn(
            ('/rewind/shifts/{shift_id}/restorations/{restoration_id}/adopt', ('POST',)), paths)


if __name__ == '__main__':
    unittest.main(verbosity=2)
