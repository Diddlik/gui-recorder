from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .storage import async_save_data

_LOGGER = logging.getLogger(__name__)
_SUPPORTED_BACKENDS = {"sqlite", "mysql", "mariadb", "postgresql"}


def _get_recorder_db_url(hass: HomeAssistant) -> str | None:
    """Return the configured recorder db_url, or None if recorder isn't ready."""
    try:
        instance = get_instance(hass)
        return getattr(instance, "db_url", None)
    except Exception:  # noqa: BLE001 - recorder not loaded yet, etc.
        return None


def get_recorder_backend(hass: HomeAssistant) -> str | None:
    """Return the SQLAlchemy recorder backend, or None when unavailable."""
    db_url = _get_recorder_db_url(hass)
    if not db_url:
        return None
    try:
        return make_url(db_url).get_backend_name().lower()
    except (TypeError, ValueError, SQLAlchemyError):
        return "unsupported"


def is_supported_recorder(hass: HomeAssistant) -> bool | None:
    """True for supported backends, False for unsupported, None if not ready."""
    backend = get_recorder_backend(hass)
    return None if backend is None else backend in _SUPPORTED_BACKENDS


def _resolve_sqlite_db_path(hass: HomeAssistant) -> Path:
    """Return the real on-disk SQLite path from recorder.db_url, falling back to default."""
    db_url = _get_recorder_db_url(hass)
    if db_url and db_url.lower().startswith("sqlite://"):
        # SQLAlchemy URL formats supported:
        #   sqlite:///relative/path.db
        #   sqlite:////absolute/path.db   (the 4th slash is the root)
        #   sqlite://         -> in-memory (treat as default file)
        _, _, raw = db_url.partition("://")
        # db_url might have url parameters. strip them out.
        if "?" in raw:
            raw, _, _ = raw.partition('?')
        # remove all leading '/'
        raw = raw.lstrip("/")
        if raw:
            # If the original had 4 slashes ("sqlite:////"), it is absolute.
            if db_url.startswith("sqlite:////"):
                return Path("/" + raw)
            return Path(hass.config.path(raw))
    return Path(hass.config.path("home-assistant_v2.db"))


def _sqlite_total_size_bytes(db_path: Path) -> int:
    """Sum the .db, .db-wal and .db-shm sidecar files (WAL mode is the HA default)."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix) if suffix else db_path
        try:
            total += sidecar.stat().st_size
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass
    return total


def _database_display_name(db_url: str, backend: str, sqlite_path: Path | None) -> str:
    """Identify the database without exposing its host or credentials."""
    if backend == "sqlite":
        return str(sqlite_path) if sqlite_path is not None else "home-assistant_v2.db"
    database_name = make_url(db_url).database or "unknown"
    label = "MariaDB / MySQL" if backend in ("mysql", "mariadb") else "PostgreSQL"
    return f"{label}: {database_name}"


def _get_server_db_size(session, backend: str) -> int | None:
    """Query database size through the existing Home Assistant connection."""
    if backend in ("mysql", "mariadb"):
        size = session.execute(
            text(
                "SELECT SUM(DATA_LENGTH + INDEX_LENGTH) "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            )
        ).scalar()
    elif backend == "postgresql":
        size = session.execute(text("SELECT pg_database_size(current_database())")).scalar()
    else:
        raise ValueError(f"Unsupported server database backend: {backend}")
    return int(size) if size is not None else None


def _analyze_recorder_db(recorder, backend: str, db_display: str, sqlite_path: Path | None) -> dict:
    """Run state counts in the HA recorder's DB executor using a read-only session."""
    with session_scope(session=recorder.get_session(), read_only=True) as session:
        # State entity IDs moved from states to states_meta in newer HA schemas.
        if inspect(session.connection()).has_table("states_meta"):
            rows = session.execute(
                text(
                    "SELECT sm.entity_id, COUNT(*) "
                    "FROM states AS s JOIN states_meta AS sm ON sm.metadata_id = s.metadata_id "
                    "GROUP BY sm.entity_id ORDER BY COUNT(*) DESC"
                )
            ).all()
        else:
            rows = session.execute(
                text("SELECT entity_id, COUNT(*) FROM states GROUP BY entity_id ORDER BY COUNT(*) DESC")
            ).all()

        # Failure to read server size should not throw away entity counts.
        size_error = None
        if backend == "sqlite":
            size_bytes = _sqlite_total_size_bytes(sqlite_path) if sqlite_path else 0
        else:
            try:
                size_bytes = _get_server_db_size(session, backend)
                if size_bytes is None:
                    size_error = "Database size is unavailable with the current database permissions."
            except SQLAlchemyError as err:
                _LOGGER.warning("Could not read %s recorder database size: %s", backend, err)
                size_bytes = None
                size_error = "Database size query failed; check database permissions and Home Assistant logs."

    entity_counts = {entity_id: int(count) for entity_id, count in rows if entity_id is not None}
    return {
        "entity_counts": entity_counts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": db_display,
        "db_size_bytes": size_bytes,
        "total_rows": sum(entity_counts.values()),
        "error": size_error,
    }


async def async_analyze_db(hass: HomeAssistant) -> dict:
    """Compute and persist recorder statistics for all supported backends."""
    data = hass.data[DOMAIN]["data"]
    backend = get_recorder_backend(hass)
    db_url = _get_recorder_db_url(hass)
    sqlite_path = _resolve_sqlite_db_path(hass) if backend == "sqlite" else None
    db_display = "Recorder database unavailable"

    try:
        if backend not in _SUPPORTED_BACKENDS or not db_url:
            raise RuntimeError("Recorder database backend is unsupported or not ready")
        db_display = _database_display_name(db_url, backend, sqlite_path)
        recorder = get_instance(hass)
        # Use the recorder's own DB executor / connection pool.
        stats = await recorder.async_add_executor_job(
            _analyze_recorder_db, recorder, backend, db_display, sqlite_path
        )
    except Exception as err:  # noqa: BLE001 - show DB errors in the panel
        _LOGGER.warning("GUI Recorder database analysis failed: %s", err)
        stats = {
            "entity_counts": {},
            "generated_at": None,
            "db_path": db_display,
            "db_size_bytes": None,
            "total_rows": 0,
            "error": str(err),
        }

    data["stats"] = stats
    await async_save_data(hass, data)
    return stats
