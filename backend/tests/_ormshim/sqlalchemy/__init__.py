"""测试专用的最小 SQLAlchemy 垫片（仅实现本项目实际使用的接口）。

生产环境使用 requirements.txt 中的真实 SQLAlchemy 2；本机无网络/pip 时，
集成测试通过 sys.path 优先加载本垫片，在 sqlite3 上验证业务与并发逻辑。
不追求完整 ORM 能力，只覆盖 rewind 服务链路用到的 API。
"""
import json
import sqlite3
import threading


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


class _Expr:
    def __init__(self, col, op, value):
        self.col = col
        self.op = op
        self.value = value


class _ColRef:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return _Expr(self, '=', value)

    def __ne__(self, value):
        return _Expr(self, '!=', value)

    def __gt__(self, value):
        return _Expr(self, '>', value)

    def __lt__(self, value):
        return _Expr(self, '<', value)

    def like(self, value):
        return _Expr(self, 'LIKE', value)

    def in_(self, values):
        return _Expr(self, 'IN', list(values))

    def desc(self):
        return ('DESC', self.name)

    def asc(self):
        return ('ASC', self.name)


# ---------- 列与声明基类 ----------

class Column(_ColRef):
    def __init__(self, *args, **kwargs):
        super().__init__(name=None)
        self.type = args[0] if args else None
        self.kwargs = kwargs
        self.foreign_key = None
        for arg in args:
            if type(arg).__name__ == 'ForeignKey':
                self.foreign_key = arg.target

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.__dict__.get(self.name)


class _Constraint:
    def __init__(self, *cols):
        self.cols = cols


class UniqueConstraint(_Constraint):
    def __init__(self, *cols, **kwargs):
        super().__init__(*cols)
        self.name = kwargs.get('name')


class Index(_Constraint):
    def __init__(self, name, *cols):
        super().__init__(*cols)
        self.index_name = name


class ForeignKey:
    def __init__(self, target):
        self.target = target


# 类型标记
class _Type:
    py = str


class Integer(_Type):
    py = int


class String(_Type):
    def __init__(self, length=None):
        self.length = length
    py = str


class Text(_Type):
    py = str


class JSON(_Type):
    py = 'json'


class _Registry:
    tables = {}


class _MetaData:
    def create_all(self, engine):
        _create_all(engine)

    def drop_all(self, engine):
        _drop_all(engine)


# DDL 在模块尾部定义，但仅在运行期调用，前向引用安全
class DeclarativeBase:
    __tablename__ = None
    __table_args__ = ()
    metadata = _MetaData()

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
        for col in self._columns():
            if col.name not in self.__dict__:
                default = col.kwargs.get('default')
                self.__dict__[col.name] = default() if callable(default) else default

    @classmethod
    def _columns(cls):
        cols = []
        for klass in reversed(cls.__mro__):
            for name, attr in vars(klass).items():
                if isinstance(attr, Column):
                    cols.append(attr)
        seen, ordered = set(), []
        for col in cols:
            if col.name not in seen:
                seen.add(col.name)
                ordered.append(col)
        return ordered

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if getattr(cls, '__tablename__', None):
            _Registry.tables[cls.__tablename__] = cls


def _sql_type(col):
    t = col.type
    if t is Integer or (isinstance(t, type) and issubclass(t, Integer)) or isinstance(t, Integer):
        return 'INTEGER'
    if t is JSON or isinstance(t, JSON):
        return 'TEXT'
    return 'TEXT'


# ---------- select / 表达式 ----------

class _Select:
    def __init__(self, entities):
        self.entities = entities
        self._where = []
        self._order = []
        self._limit = None
        self._for_update = False

    def where(self, *exprs):
        self._where.extend(exprs)
        return self

    def order_by(self, *exprs):
        self._order.extend(exprs)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def with_for_update(self):
        self._for_update = True
        return self


def select(*entities):
    return _Select(list(entities))


def _desc(col):
    return ('DESC', col.name if isinstance(col, Column) else str(col))


# ---------- 引擎与会话 ----------

class Engine:
    def __init__(self, url):
        if not url.startswith('sqlite:///'):
            raise RuntimeError('shim 仅支持 sqlite')
        self.path = url.replace('sqlite:///', '', 1)
        self._lock = threading.RLock()
        # 多连接并发写：WAL + 忙等待，让 FOR UPDATE 语义由进程内 RLock 串行化
        self._pragma_done = False

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
        return conn

    @property
    def lock(self):
        return self._lock


def create_engine(url, **kwargs):
    return Engine(url)


class _ScalarQuery:
    def __init__(self, session, model):
        self.session = session
        self.model = model
        self.filters = []
        self.orders = []
        self.limit_n = None

    def where(self, *exprs):
        self.filters.extend(exprs)
        return self

    def order_by(self, *exprs):
        self.orders.extend(exprs)
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def _rows(self):
        return self.session._fetch(self.model, self.filters, self.orders, self.limit_n)

    def all(self):
        return [self.session._to_instance(self.model, row) for row in self._rows()]

    def first(self):
        rows = self._rows()
        return self.session._to_instance(self.model, rows[0]) if rows else None


class Session:
    def __init__(self, bind=None):
        self.bind = bind or _default_engine
        self._new = []
        self._deleted = []
        self._locked_tables = set()
        self._managed = {}
        self._snapshot = {}
        self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            self._conn = self.bind.connect()
        return self._conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            try:
                self.commit()
            except Exception:
                self.rollback()
                raise
        else:
            self.rollback()
        self.close()

    def add(self, instance):
        if getattr(instance, 'id', None) is None:
            self._new.append(instance)
        else:
            # 已持久化对象：登记为受管，提交时按脏字段 UPDATE
            self._managed.setdefault((type(instance).__tablename__, instance.id), instance)

    def delete(self, instance):
        self._deleted.append(instance)

    def _register_managed(self, instance, force=False):
        key = (type(instance).__tablename__, instance.id)
        if key in self._managed and not force:
            # 同会话内复用已登记实例，避免快照被新加载对象覆盖、脏检失效
            return self._managed[key]
        snapshot = {c.name: self._value(instance, c) for c in type(instance)._columns()}
        self._managed[key] = instance
        self._snapshot[key] = snapshot
        return instance

    def _reload_managed(self, model, pk):
        """锁内强制按最新已提交数据重建受管实例（模拟 SELECT ... FOR UPDATE 的当前读）。"""
        row = self.conn.execute(
            f'SELECT * FROM {model.__tablename__} WHERE "id" = ?', (pk,)).fetchone()
        if not row:
            return None
        instance = self._to_instance(model, row, register=False)
        return self._register_managed(instance, force=True)

    def flush(self):
        with self.bind.lock:
            self._persist(self.conn)

    def commit(self):
        self.flush()
        self.conn.commit()
        self._release_locks()
        self._new.clear()
        self._deleted.clear()
        self._managed.clear()
        self._snapshot.clear()
        self._locked_tables.clear()

    def rollback(self):
        if self._conn is not None:
            self._conn.rollback()
        self._release_locks()
        self._new.clear()
        self._deleted.clear()
        self._managed.clear()
        self._snapshot.clear()
        self._locked_tables.clear()

    def _release_locks(self):
        for _table in self._locked_tables:
            self.bind.lock.release()

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- 查询 ----

    def get(self, model, pk):
        row = self.conn.execute(
            f'SELECT * FROM {model.__tablename__} WHERE "id" = ?', (pk,)).fetchone()
        return self._to_instance(model, row) if row else None

    def scalars(self, stmt):
        return _ScalarResult(self._execute_scalars(stmt))

    def scalar(self, stmt):
        result = self._execute_scalars(stmt)
        return result[0] if result else None

    def execute(self, stmt):
        # 仅支持 select(...).with_for_update() 形式
        if not isinstance(stmt, _Select):
            raise RuntimeError('shim 仅支持 select 语句')
        model = stmt.entities[0]
        if stmt._for_update:
            # sqlite 无行锁：用进程内锁模拟串行化（生产 MySQL 使用真实 FOR UPDATE）。
            # 先释放本连接可能持有的只读快照，再取锁，保证锁内读到对方最新已提交数据。
            if self._conn is not None:
                self._conn.rollback()
            self.bind.lock.acquire()
            self._locked_tables.add(model.__tablename__)
        rows = self._fetch(model, stmt._where, stmt._order, stmt._limit)
        if stmt._for_update:
            # 当前读：按最新已提交数据重建，覆盖锁前 identity map 中的旧快照
            items = [self._reload_managed(model, row['id']) for row in rows]
        else:
            items = [self._to_instance(model, row) for row in rows]
        return _ScalarResult(items)

    def _execute_scalars(self, stmt):
        if isinstance(stmt, _Select):
            entity = stmt.entities[0]
            if isinstance(entity, Column):
                # select(Model.id) 形式：返回标量列
                model = _Registry.tables[next(
                    t for t, m in _Registry.tables.items()
                    if any(c.name == entity.name for c in m._columns()))]
                rows = self._fetch(model, stmt._where, stmt._order, stmt._limit)
                return [row[entity.name] for row in rows]
            rows = self._fetch(entity, stmt._where, stmt._order, stmt._limit)
            return [self._to_instance(entity, row) for row in rows]
        raise RuntimeError('不支持的 select 实体')

    def _fetch(self, model, filters, orders, limit_n):
        sql = f'SELECT * FROM {model.__tablename__}'
        params = []
        if filters:
            clauses = []
            for expr in filters:
                clause, vals = self._render_expr(expr)
                clauses.append(clause)
                params.extend(vals)
            sql += ' WHERE ' + ' AND '.join(clauses)
        if orders:
            rendered = []
            for order in orders:
                if isinstance(order, tuple) and order[0] == 'DESC':
                    rendered.append(_q(order[1]) + ' DESC')
                else:
                    rendered.append(_q(order.name if isinstance(order, Column) else str(order)))
            sql += ' ORDER BY ' + ', '.join(rendered)
        if limit_n is not None:
            sql += f' LIMIT {int(limit_n)}'
        return self.conn.execute(sql, params).fetchall()

    @staticmethod
    def _render_expr(expr):
        name = expr.col.name
        if expr.op == 'IN':
            placeholders = ', '.join('?' for _ in expr.value)
            return f'{_q(name)} IN ({placeholders})', list(expr.value)
        if expr.op == 'LIKE':
            return f'{_q(name)} LIKE ?', [expr.value]
        return f'{_q(name)} {expr.op} ?', [expr.value]

    def _to_instance(self, model, row, register=True):
        kwargs = {}
        for col in model._columns():
            raw = row[col.name]
            if col.type is JSON and raw is not None:
                raw = json.loads(raw)
            kwargs[col.name] = raw
        instance = model(**kwargs)
        return self._register_managed(instance) if register else instance

    def _value(self, instance, col):
        value = instance.__dict__.get(col.name)
        if value is None:
            default = col.kwargs.get('default')
            value = default() if callable(default) else default
        return value

    def _persist(self, conn):
        for instance in self._deleted:
            conn.execute(
                f'DELETE FROM {instance.__tablename__} WHERE "id" = ?', (instance.id,))
        for instance in self._new:
            model = type(instance)
            cols = model._columns()
            stored = {}
            for col in cols:
                value = self._value(instance, col)
                if isinstance(value, Column):
                    value = col.kwargs.get('default')
                if col.type is JSON and value is not None:
                    value = json.dumps(value, ensure_ascii=False)
                stored[col.name] = value
            if stored.get('id') is None:
                names = [c.name for c in cols if c.name != 'id']
                placeholders = ', '.join('?' for _ in names)
                cur = conn.execute(
                    f'INSERT INTO {model.__tablename__} ({", ".join(_q(n) for n in names)}) '
                    f'VALUES ({placeholders})',
                    [stored[n] for n in names])
                instance.id = cur.lastrowid
            else:
                names = [c.name for c in cols if c.name != 'id']
                assignments = ', '.join(f'{_q(n)} = ?' for n in names)
                conn.execute(
                    f'UPDATE {model.__tablename__} SET {assignments} WHERE "id" = ?',
                    [stored[n] for n in names] + [stored['id']])
        # 受管对象：与快照比对，仅更新变化字段
        for (table_name, pk), instance in self._managed.items():
            model = type(instance)
            changed = []
            params = []
            for col in model._columns():
                if col.name == 'id':
                    continue
                value = self._value(instance, col)
                if isinstance(value, Column):
                    value = col.kwargs.get('default')
                stored_value = json.dumps(value, ensure_ascii=False) if col.type is JSON else value
                if self._snapshot[(table_name, pk)].get(col.name) != value:
                    changed.append(_q(col.name))
                    params.append(stored_value)
            if changed:
                conn.execute(
                    f'UPDATE {table_name} SET {", ".join(f"{n} = ?" for n in changed)} '
                    f'WHERE "id" = ?', params + [pk])
            self._snapshot[(table_name, pk)] = {
                c.name: self._value(instance, c) for c in model._columns()}


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalar_one(self):
        if len(self._items) != 1:
            raise RuntimeError('期望恰好一行')
        return self._items[0]

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return list(self._items)

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


class _SessionMaker:
    def __init__(self, bind=None):
        self.bind = bind

    def __call__(self):
        return Session(bind=self.bind)

    def configure(self, bind=None):
        self.bind = bind


def sessionmaker(bind=None, **kwargs):
    return _SessionMaker(bind=bind)


# ---------- DDL ----------

def _create_all(engine):
    conn = engine.connect()
    try:
        for table_name, model in _Registry.tables.items():
            cols_sql = ['id INTEGER PRIMARY KEY AUTOINCREMENT']
            for col in model._columns():
                if col.name == 'id':
                    continue
                line = f'{_q(col.name)} {_sql_type(col)}'
                if col.kwargs.get('nullable') is False:
                    line += ' NOT NULL'
                if col.foreign_key:
                    fk = col.foreign_key
                    target = fk if isinstance(fk, str) else fk.target
                    table, _, column = target.partition('.')
                    line += f' REFERENCES {table}({column or "id"})'
                cols_sql.append(line)
            conn.execute(f'CREATE TABLE IF NOT EXISTS {table_name} ({", ".join(cols_sql)})')
            for arg in getattr(model, '__table_args__', ()):
                if isinstance(arg, UniqueConstraint):
                    conn.execute(
                        f'CREATE UNIQUE INDEX IF NOT EXISTS uq_{table_name}_'
                        f'{"_".join(arg.cols)} ON {table_name} ({", ".join(_q(c) for c in arg.cols)})')
                elif isinstance(arg, Index):
                    conn.execute(
                        f'CREATE INDEX IF NOT EXISTS {arg.index_name} '
                        f'ON {table_name} ({", ".join(_q(c) for c in arg.cols)})')
        conn.commit()
    finally:
        conn.close()


def _drop_all(engine):
    conn = engine.connect()
    try:
        for table_name in list(_Registry.tables):
            conn.execute(f'DROP TABLE IF EXISTS {table_name}')
        conn.commit()
    finally:
        conn.close()


class _ColumnAttr:
    def __init__(self, name):
        self.key = name


class _Mapper:
    def __init__(self, model):
        self.column_attrs = [_ColumnAttr(c.name) for c in model._columns()]


class _Inspection:
    def __init__(self, model):
        self.mapper = _Mapper(model)


def inspect(obj):
    model = obj if isinstance(obj, type) else type(obj)
    return _Inspection(model)


class _Base(DeclarativeBase):
    pass


Base = _Base
_default_engine = None


def configure_default(engine):
    global _default_engine
    _default_engine = engine


# IntegrityError 占位
class IntegrityError(Exception):
    pass


class OperationalError(Exception):
    pass


# exceptions 模块
class _Exc:
    IntegrityError = IntegrityError
    OperationalError = OperationalError


exc = _Exc()
