from sqlalchemy import Column, Integer, String, JSON, Text, ForeignKey, UniqueConstraint, Index
from src.config.database import Base


class RewindShift(Base):
    """一次班次录入：区间表 + 全班实点，所有还原在其版本链上进行。"""
    __tablename__ = 'rewind_shifts'
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    actual_total = Column(Integer, nullable=False)
    actual_missed = Column(Integer, nullable=False)
    # 当前数据版本号；任何录入改动都会 +1，并发采用时据此做乐观并发控制
    version = Column(Integer, nullable=False, default=1)
    # draft（未采用）/ adopted（已写入原检验）/ superseded（采用后源数据又被更正）
    status = Column(String(20), nullable=False, default='draft')
    created_by = Column(String(120), nullable=False)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)


class RewindInterval(Base):
    __tablename__ = 'rewind_intervals'
    __table_args__ = (Index('ix_rewind_interval_shift', 'shift_id', 'seq'),)
    id = Column(Integer, primary_key=True)
    shift_id = Column(Integer, ForeignKey('rewind_shifts.id'), nullable=False)
    seq = Column(Integer, nullable=False)
    process = Column(String(1), nullable=False)
    mod_total = Column(Integer, nullable=False)
    mod_missed = Column(Integer, nullable=False)
    total_start = Column(Integer, nullable=False)
    total_end = Column(Integer, nullable=False)
    missed_start = Column(Integer, nullable=False)
    missed_end = Column(Integer, nullable=False)
    upper = Column(Integer, nullable=False)


class RewindRestoration(Base):
    """一次还原计算及其结论。同一版数据可被多次计算，结论带版本号。"""
    __tablename__ = 'rewind_restorations'
    __table_args__ = (Index('ix_rewind_restoration_shift', 'shift_id', 'version'),)
    id = Column(Integer, primary_key=True)
    shift_id = Column(Integer, ForeignKey('rewind_shifts.id'), nullable=False)
    # 计算时所依据的班次数据版本；采用时必须等于 shift.version
    version = Column(Integer, nullable=False)
    # ok / no_solution / ambiguous / timeout / invalid
    status = Column(String(20), nullable=False)
    message = Column(Text, nullable=False, default='')
    counts = Column(JSON, nullable=True)
    basis = Column(JSON, nullable=True)
    samples = Column(JSON, nullable=True)
    feasible_solution_count = Column(Integer, nullable=True)
    elapsed_ms = Column(Integer, nullable=False, default=0)
    created_by = Column(String(120), nullable=False)
    created_at = Column(String(40), nullable=False)


class RewindAdoption(Base):
    """还原被采用后写入原检验的记录。乐观锁保证重试不重复写入。"""
    __tablename__ = 'rewind_adoptions'
    __table_args__ = (UniqueConstraint('restoration_id', name='uq_rewind_adoption_restoration'),)
    id = Column(Integer, primary_key=True)
    shift_id = Column(Integer, ForeignKey('rewind_shifts.id'), nullable=False)
    restoration_id = Column(Integer, ForeignKey('rewind_restorations.id'), nullable=False)
    version = Column(Integer, nullable=False)
    counts = Column(JSON, nullable=False)
    # 写入原检验后生成的工艺计数行 source_key，便于对账
    written_keys = Column(JSON, nullable=False)
    created_by = Column(String(120), nullable=False)
    created_at = Column(String(40), nullable=False)
