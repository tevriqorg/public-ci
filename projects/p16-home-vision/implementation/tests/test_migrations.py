from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_alembic_upgrade_and_downgrade(tmp_path) -> None:
    db_path = tmp_path / "migration.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert {
        "source_recordings", "local_assets", "analysis_profiles", "analysis_runs",
        "archive_source_snapshots",
    } <= tables
    columns = {column["name"] for column in inspect(engine).get_columns("archive_jobs")}
    assert {"worker_id", "lease_token", "lease_expires_at", "lease_phase"} <= columns
    engine.dispose()
    command.downgrade(cfg, "base")
    engine = create_engine(f"sqlite:///{db_path}")
    assert inspect(engine).get_table_names() == ["alembic_version"] or inspect(engine).get_table_names() == []
    engine.dispose()
