"""Run with: python -m unittest discover -s tests -v (SQLAlchemy and PyYAML required)."""
import asyncio
import ast
from contextlib import closing
import importlib
import sys
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from sqlalchemy import create_engine, text


# Exercise the integration without installing a complete Home Assistant runtime.
def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    sys.modules[name] = result
    return result


class ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        pass

    def _async_current_entries(self):
        return []

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}


module("homeassistant")
module("homeassistant.core", HomeAssistant=object)
module("homeassistant.config_entries", ConfigFlow=ConfigFlow, ConfigEntry=object)
module("homeassistant.data_entry_flow", FlowResult=dict)
module("homeassistant.helpers")
module("homeassistant.helpers.storage", Store=object)
module("homeassistant.components")
recorder = module("homeassistant.components.recorder", get_instance=lambda hass: hass.recorder)

PACKAGE = "gui_recorder_under_test"
package = module(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/gui_recorder")]
module(f"{PACKAGE}.panel", async_setup_panel=AsyncMock())
module(f"{PACKAGE}.websocket_api", async_setup_websocket_api=AsyncMock())
stats = importlib.import_module(f"{PACKAGE}.db_stats")
migration = importlib.import_module(f"{PACKAGE}.migration")
writer = importlib.import_module(f"{PACKAGE}.yaml_writer")
flow = importlib.import_module(f"{PACKAGE}.config_flow")
spec = importlib.util.spec_from_file_location(PACKAGE, Path(package.__path__[0]) / "__init__.py")
spec.loader.exec_module(package)


class DatabaseBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda name: str(Path(self.temp.name) / name)),
            recorder=types.SimpleNamespace(db_url="mysql://user:password@host/homeassistant"),
            data={"gui_recorder": {"data": {}}},
        )

        async def executor(func, *args):
            return func(*args)

        self.hass.async_add_executor_job = executor

    def test_setup_flow_accepts_supported_urls(self):
        for scheme in ("sqlite", "mysql", "mysql+pymysql", "mariadb", "mariadb+pymysql"):
            with self.subTest(scheme=scheme):
                self.hass.recorder.db_url = f"{scheme}://user:password@host/db"
                config_flow = flow.GuiRecorderConfigFlow()
                config_flow.hass = self.hass
                self.assertEqual(asyncio.run(config_flow.async_step_user())["type"], "create_entry")

    def test_setup_flow_rejects_unsupported_or_unavailable_recorder(self):
        for url, reason in (("postgresql://host/db", "unsupported_database"), (None, "recorder_not_ready")):
            with self.subTest(url=url):
                self.hass.recorder.db_url = url
                config_flow = flow.GuiRecorderConfigFlow()
                config_flow.hass = self.hass
                self.assertEqual(asyncio.run(config_flow.async_step_user())["reason"], reason)

    def test_existing_entry_initializes_on_mariadb(self):
        with patch.object(package, "_async_initialize", new_callable=AsyncMock) as initialize:
            self.assertTrue(asyncio.run(package.async_setup_entry(self.hass, object())))
            initialize.assert_awaited_once_with(self.hass)

    def test_existing_entry_rejects_unsupported_backend(self):
        self.hass.recorder.db_url = "postgresql://host/db"
        with patch.object(package, "_async_initialize", new_callable=AsyncMock) as initialize:
            self.assertFalse(asyncio.run(package.async_setup_entry(self.hass, object())))
            initialize.assert_not_awaited()

    def test_server_queries_on_modern_and_legacy_schemas(self):
        for modern in (True, False):
            with self.subTest(modern=modern):
                engine = create_engine("sqlite://")
                self.addCleanup(engine.dispose)
                self.hass.recorder.engine = engine
                with engine.begin() as connection:
                    if modern:
                        connection.execute(text("CREATE TABLE states_meta (metadata_id INTEGER, entity_id TEXT)"))
                        connection.execute(text("CREATE TABLE states (metadata_id INTEGER)"))
                        connection.execute(text("INSERT INTO states_meta VALUES (1, 'sensor.a'), (2, 'sensor.b')"))
                        connection.execute(text("INSERT INTO states VALUES (1), (1), (2)"))
                    else:
                        connection.execute(text("CREATE TABLE states (entity_id TEXT)"))
                        connection.execute(text("INSERT INTO states VALUES ('sensor.a'), ('sensor.a'), ('sensor.b')"))
                result = stats._analyze_server_db(self.hass)
                self.assertEqual(result["entity_counts"], {"sensor.a": 2, "sensor.b": 1})
                self.assertEqual(result["total_rows"], 3)
                self.assertIsNone(result["db_size_bytes"])
                self.assertNotIn("password", str(result))
                # The existing file-based SQLite analysis must retain its behavior too.
                path = Path(self.temp.name) / f"{modern}.db"
                with engine.connect() as connection:
                    with closing(sqlite3.connect(path)) as destination:
                        connection.connection.driver_connection.backup(destination)
                sqlite_result = stats._analyze_sqlite_db(str(path))
                self.assertEqual(sqlite_result["entity_counts"], result["entity_counts"])
                self.assertGreater(sqlite_result["db_size_bytes"], 0)

    def test_server_dispatch_never_reads_default_sqlite_file(self):
        expected = {"entity_counts": {}, "total_rows": 0, "error": None}
        with patch.object(stats, "_analyze_server_db", return_value=expected) as analyze, \
                patch.object(stats, "_analyze_sqlite_db") as sqlite, \
                patch.object(stats, "async_save_data", new_callable=AsyncMock):
            self.assertEqual(asyncio.run(stats.async_analyze_db(self.hass)), expected)
            analyze.assert_called_once_with(self.hass)
            sqlite.assert_not_called()

    def test_server_size_sums_only_current_database_tables(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        self.hass.recorder.engine = engine
        with engine.begin() as connection:
            connection.connection.driver_connection.create_function("DATABASE", 0, lambda: "recorder")
            connection.execute(text("CREATE TABLE states (entity_id TEXT)"))
            connection.execute(text("INSERT INTO states VALUES ('sensor.a')"))
            connection.execute(text("ATTACH DATABASE ':memory:' AS information_schema"))
            connection.execute(text("""
                CREATE TABLE information_schema.TABLES (
                    TABLE_SCHEMA TEXT, TABLE_TYPE TEXT, DATA_LENGTH INTEGER, INDEX_LENGTH INTEGER
                )
            """))
            connection.execute(text("""
                INSERT INTO information_schema.TABLES VALUES
                    ('recorder', 'BASE TABLE', 1000, 200),
                    ('recorder', 'BASE TABLE', 300, NULL),
                    ('other_database', 'BASE TABLE', 99999, 99999),
                    ('recorder', 'VIEW', 99999, 99999)
            """))
        result = stats._analyze_server_db(self.hass)
        self.assertEqual(result["db_size_bytes"], 1500)
        self.assertEqual(result["entity_counts"], {"sensor.a": 1})
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM information_schema.TABLES"))
        self.assertIsNone(stats._analyze_server_db(self.hass)["db_size_bytes"])

    def test_stats_payload_preserves_unknown_zero_and_known_sizes(self):
        # Load the real payload builder without registering HA websocket handlers.
        path = Path(package.__path__[0]) / "websocket_api.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_build_stats_payload")
        namespace = {"DOMAIN": "gui_recorder", "HomeAssistant": object}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        build_payload = namespace["_build_stats_payload"]
        for size in (None, 0, 1500):
            with self.subTest(size=size):
                self.hass.data["gui_recorder"]["data"]["stats"] = {"db_size_bytes": size}
                self.assertEqual(build_payload(self.hass)["db_size_bytes"], size)
        self.hass.data["gui_recorder"]["data"]["stats"] = {}
        self.assertIsNone(build_payload(self.hass)["db_size_bytes"])

    def test_server_failure_does_not_expose_credentials(self):
        with patch.object(stats, "_analyze_server_db", side_effect=RuntimeError(self.hass.recorder.db_url)), \
                patch.object(stats, "async_save_data", new_callable=AsyncMock):
            result = asyncio.run(stats.async_analyze_db(self.hass))
        self.assertIsNotNone(result["error"])
        self.assertNotIn("password", str(result))
        self.assertIsNone(result["db_size_bytes"])

    def test_migration_and_generated_yaml_preserve_database_url(self):
        for url in ("mysql://user:password@host/db?charset=utf8mb4", "mariadb+pymysql://host/db", "sqlite:////share/history.db"):
            with self.subTest(url=url):
                config = Path(self.temp.name) / "configuration.yaml"
                config.write_text(yaml.safe_dump({"recorder": {"db_url": url, "purge_keep_days": 12}}), encoding="utf-8")
                result = migration._import_legacy_sync(self.hass)
                self.assertTrue(result["ok"])
                self.hass.data["gui_recorder"]["data"] = result["storage_updates"]
                generated = asyncio.run(writer.async_write_yaml(self.hass))
                parsed = yaml.safe_load(Path(generated).read_text(encoding="utf-8"))
                self.assertEqual(parsed["db_url"], url)
                self.assertEqual(parsed["purge_keep_days"], 12)

    def test_unparseable_migration_cannot_silently_drop_database(self):
        config = Path(self.temp.name) / "configuration.yaml"
        original = "recorder:\n  db_url: !secret recorder_db_url\n"
        config.write_text(original, encoding="utf-8")
        result = migration._import_legacy_sync(self.hass)
        self.assertFalse(result["ok"])
        self.assertIn("!secret", result["error"])
        self.assertNotIn("storage_updates", result)
        self.assertEqual(config.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
