from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    database_url = get_settings().resolved_database_url
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    is_memory_database = database_url in {"sqlite://", "sqlite:///:memory:"}
    if database_url.startswith("sqlite:///"):
        database_path = database_url.removeprefix("sqlite:///")
        if not is_memory_database and database_path != ":memory:":
            path = Path(database_path)
            if not path.is_absolute():
                path = Path.cwd() / path
            path.parent.mkdir(parents=True, exist_ok=True)
    engine_options = {"poolclass": StaticPool} if is_memory_database else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True, **engine_options)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def migrate_schema() -> None:
    """Apply the small additive SQLite changes needed between early milestones."""
    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    if "signals" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("signals")}
        additions = {
            "content_hash": "VARCHAR(64)",
            "claimed_at": "DATETIME",
            "sender_id": "VARCHAR(128)",
            "sender_name": "VARCHAR(255)",
        }
        missing = {name: sql_type for name, sql_type in additions.items() if name not in columns}
        if missing:
            with engine.begin() as connection:
                for name, sql_type in missing.items():
                    connection.execute(text(f"ALTER TABLE signals ADD COLUMN {name} {sql_type}"))
    if "email_verifications" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("email_verifications")}
        additions = {
            "send_count": "INTEGER NOT NULL DEFAULT 1",
            "send_window_started_at": "DATETIME",
            "locked_until": "DATETIME",
        }
        missing = {name: sql_type for name, sql_type in additions.items() if name not in columns}
        if missing:
            with engine.begin() as connection:
                for name, sql_type in missing.items():
                    connection.execute(text(f"ALTER TABLE email_verifications ADD COLUMN {name} {sql_type}"))
    if "password_reset_codes" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("password_reset_codes")}
        if "last_activity_at" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE password_reset_codes "
                        "ADD COLUMN last_activity_at DATETIME NOT NULL "
                        "DEFAULT '1970-01-01 00:00:00'"
                    )
                )
    if "mt5_accounts" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("mt5_accounts")}
        additions = {
            "token_ciphertext": "TEXT",
            "plan": "VARCHAR(16) NOT NULL DEFAULT 'ZERO'",
            "execution_mode": "VARCHAR(16) NOT NULL DEFAULT 'EA'",
        }
        missing = {name: sql_type for name, sql_type in additions.items() if name not in columns}
        if missing:
            with engine.begin() as connection:
                for name, sql_type in missing.items():
                    connection.execute(text(f"ALTER TABLE mt5_accounts ADD COLUMN {name} {sql_type}"))
    if "payment_orders" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("payment_orders")}
        if "execution_mode" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE payment_orders ADD COLUMN execution_mode VARCHAR(16) NOT NULL DEFAULT 'EA'")
                )
    if "managed_copy_profiles" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("managed_copy_profiles")}
        additions = {
            "login_status": "VARCHAR(24) NOT NULL DEFAULT 'UNCHECKED'",
            "login_message": "VARCHAR(240)",
        }
        missing = {name: sql_type for name, sql_type in additions.items() if name not in columns}
        if missing:
            with engine.begin() as connection:
                for name, sql_type in missing.items():
                    connection.execute(text(f"ALTER TABLE managed_copy_profiles ADD COLUMN {name} {sql_type}"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
