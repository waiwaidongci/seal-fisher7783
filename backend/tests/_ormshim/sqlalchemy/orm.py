"""垫片：sqlalchemy.orm 子模块。"""
from sqlalchemy import DeclarativeBase as _DB, sessionmaker, Session

DeclarativeBase = _DB
__all__ = ['DeclarativeBase', 'sessionmaker', 'Session']
