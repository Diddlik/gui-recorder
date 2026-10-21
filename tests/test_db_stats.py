"""Unit tests for GUI Recorder's supported database backends."""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def db_stats(monkeypatch):
    """Load the integration module with light Home Assistant stubs."""
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    recorder = types.ModuleType("homeassistant.components.recorder")
    recorder.__path__ = []
    recorder.get_instance = lambda hass: hass.recorder
    util = types.ModuleType("homeassistant.components.recorder.util")

    @contextmanager
    def session_scope(*, session, read_only):
        assert read_only
        try:
            yield session
        finally:
            session.close()

    util.session_scope = session_scope
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    package = types.ModuleType("custom_components")
    package.__path__ = []
    subpackage = types.ModuleType("custom_components.gui_recorder")
    subpackage.__path__ = []
    const = types.ModuleType("custom_components.gui_recorder.const")
    const.DOMAIN = "gui_recorder"
    storage = types.ModuleType("custom_components.gui_recorder.storage")

    async def async_save_data(hass, data):
        pass

    storage.async_save_data = async_save_data
    modules = {
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.recorder": recorder,
        "homeassistant.components.recorder.util": util,
        "homeassistant.core": core,
        "custom_components": package,
        "custom_components.gui_recorder": subpackage,
        "custom_components.gui_recorder.const": const,
        "custom_components.gui_recorder.storage": storage,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    path = Path(__file__).resolve().parents[1] / "custom_components/gui_recorder/db_stats.py"
    spec = importlib.util.spec_from_file_location("custom_components.gui_recorder.db_stats", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("url, backend", [
    ("sqlite:////config/home-assistant_v2.db", "sqlite"),
    ("mysql://user:p%40ss@db:3306/ha", "mysql"),
    ("mysql+pymysql://user:pass@db/ha", "mysql"),
    ("mariadb+pymysql://user:pass@db/ha", "mariadb"),
    ("postgresql+psycopg2://user:pass@db/ha", "postgresql"),
    ("oracle://user:pass@db/ha", "oracle"),
])
def test_supported_backends(db_stats, url, backend):
    hass = types.SimpleNamespace(recorder=types.SimpleNamespace(db_url=url))
    assert db_stats.get_recorder_backend(hass) == backend
    assert db_stats.is_supported_recorder(hass) is (backend != "oracle")


def test_recorder_not_ready(db_stats):
    assert db_stats.is_supported_recorder(types.SimpleNamespace()) is None


def test_display_name_masks_credentials(db_stats):
    url = "postgresql://supersecret:password%23with%3F@database.local:5432/ha_db"
    display = db_stats._database_display_name(url, "postgresql", None)
    assert display == "PostgreSQL: ha_db"
    assert "password" not in display
    assert "database.local" not in display


def test_sqlite_counts_and_size(db_stats, tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE states_meta (metadata_id INT PRIMARY KEY, entity_id TEXT)"))
        conn.execute(text("CREATE TABLE states (metadata_id INT)"))
        conn.execute(text("INSERT INTO states_meta VALUES (1,'sensor.a'), (2,'sensor.b')"))
        conn.execute(text("INSERT INTO states VALUES (1), (1), (2)"))
    path = tmp_path / "states.db"
    path.write_bytes(b"abcdef")
    (tmp_path / "states.db-wal").write_bytes(b"xxx")
    (tmp_path / "states.db-shm").write_bytes(b"xx")
    instance = types.SimpleNamespace(get_session=sessionmaker(engine))
    stats = db_stats._analyze_recorder_db(instance, "sqlite", str(path), path)
    assert stats["entity_counts"] == {"sensor.a": 2, "sensor.b": 1}
    assert stats["total_rows"] == 3
    assert stats["db_size_bytes"] == 11
    engine.dispose()


def test_legacy_states_table(db_stats):
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE states (entity_id TEXT)"))
        conn.execute(text("INSERT INTO states VALUES ('sensor.old'), ('sensor.old')"))
    instance = types.SimpleNamespace(get_session=sessionmaker(engine))
    stats = db_stats._analyze_recorder_db(instance, "sqlite", "db", None)
    assert stats["entity_counts"] == {"sensor.old": 2}
    engine.dispose()


@pytest.mark.parametrize("backend,expected", [
    ("mysql", "information_schema.TABLES"),
    ("mariadb", "information_schema.TABLES"),
    ("postgresql", "pg_database_size(current_database())"),
])
def test_server_size_queries(db_stats, backend, expected):
    session = MagicMock()
    session.execute.return_value.scalar.return_value = 87654
    assert db_stats._get_server_db_size(session, backend) == 87654
    assert expected in str(session.execute.call_args.args[0])


def test_size_error_does_not_lose_counts(db_stats, monkeypatch):
    from sqlalchemy.exc import OperationalError
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE states_meta (metadata_id INT PRIMARY KEY, entity_id TEXT)"))
        conn.execute(text("CREATE TABLE states (metadata_id INT)"))
        conn.execute(text("INSERT INTO states_meta VALUES (1,'sensor.a')"))
        conn.execute(text("INSERT INTO states VALUES (1)"))

    def reject(_session, _backend):
        raise OperationalError("size query", {}, Exception("permission denied"))

    monkeypatch.setattr(db_stats, "_get_server_db_size", reject)
    instance = types.SimpleNamespace(get_session=sessionmaker(engine))
    stats = db_stats._analyze_recorder_db(instance, "postgresql", "PostgreSQL: ha", None)
    assert stats["total_rows"] == 1
    assert stats["db_size_bytes"] is None
    assert "size query failed" in stats["error"]
    engine.dispose()
