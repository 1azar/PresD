import os

from sqlalchemy import create_engine, event, exc
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _current_pid() -> int:
    return os.getpid()


@event.listens_for(engine, "connect")
def _record_connection_pid(dbapi_connection, connection_record) -> None:
    connection_record.info["pid"] = _current_pid()


@event.listens_for(engine, "checkout")
def _ensure_connection_pid(dbapi_connection, connection_record, connection_proxy) -> None:
    connection_pid = connection_record.info.get("pid")
    current_pid = _current_pid()
    if connection_pid == current_pid:
        return
    connection_record.dbapi_connection = connection_proxy.dbapi_connection = None
    raise exc.DisconnectionError(
        f"connection belongs to pid {connection_pid}; current pid is {current_pid}"
    )


def get_db():
    with SessionLocal() as db:
        yield db
