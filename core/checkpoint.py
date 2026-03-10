import pickle
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver


class SQLiteBackedMemorySaver(InMemorySaver):
    """Persist LangGraph checkpoints by snapshotting the in-memory saver to SQLite.

    This keeps the same runtime behavior as `InMemorySaver` while surviving process
    restarts, which is enough for local durable checkpointing without extra packages.
    """

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self.db_path = Path(db_path)
        self._db_lock = threading.Lock()
        self._init_db()
        self._load_from_disk()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoint_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    storage BLOB NOT NULL,
                    writes BLOB NOT NULL,
                    blobs BLOB NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _load_from_disk(self) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT storage, writes, blobs FROM checkpoint_state WHERE id = 1"
            ).fetchone()
        if not row:
            return

        storage_blob, writes_blob, blobs_blob = row
        storage = pickle.loads(storage_blob)
        writes = pickle.loads(writes_blob)
        blobs = pickle.loads(blobs_blob)

        rebuilt_storage = defaultdict(lambda: defaultdict(dict))
        for thread_id, namespaces in storage.items():
            ns_bucket = defaultdict(dict)
            for checkpoint_ns, checkpoints in namespaces.items():
                ns_bucket[checkpoint_ns] = dict(checkpoints)
            rebuilt_storage[thread_id] = ns_bucket

        rebuilt_writes = defaultdict(dict)
        for key, value in writes.items():
            rebuilt_writes[key] = dict(value)

        self.storage = rebuilt_storage
        self.writes = rebuilt_writes
        self.blobs = dict(blobs)

    def _persist(self) -> None:
        storage = {
            thread_id: {ns: dict(checkpoints) for ns, checkpoints in namespaces.items()}
            for thread_id, namespaces in self.storage.items()
        }
        writes = {key: dict(value) for key, value in self.writes.items()}
        blobs = dict(self.blobs)

        payload = (
            pickle.dumps(storage),
            pickle.dumps(writes),
            pickle.dumps(blobs),
        )

        with self._db_lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO checkpoint_state (id, storage, writes, blobs, updated_at)
                    VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        storage = excluded.storage,
                        writes = excluded.writes,
                        blobs = excluded.blobs,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    payload,
                )

    def put(self, config, checkpoint, metadata, new_versions):
        result = super().put(config, checkpoint, metadata, new_versions)
        self._persist()
        return result

    def put_writes(self, config, writes, task_id, task_path: str = "") -> None:
        super().put_writes(config, writes, task_id, task_path)
        self._persist()

    def delete_thread(self, thread_id: str) -> None:
        super().delete_thread(thread_id)
        self._persist()


__all__ = ["SQLiteBackedMemorySaver"]
