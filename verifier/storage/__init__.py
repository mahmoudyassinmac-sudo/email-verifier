from .base import Storage
from .sqlite_store import SQLiteStorage


def open_storage(settings) -> Storage:
    """Factory: the only place that decides which backend is used."""
    return SQLiteStorage(settings.db_path)


__all__ = ["Storage", "SQLiteStorage", "open_storage"]
