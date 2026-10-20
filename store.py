"""
Runa's memory across restarts.

Without a database, runa-sap keeps everything in memory, and a deploy or a
crash wipes the SAP records, the dashboard events and the learned notes.
With DATABASE_URL set (a Postgres connection string), every change is saved
and loaded again when the app starts.

    DATABASE_URL unset    memory only, exactly as before (local tests)
    DATABASE_URL set      one table, runa_state: one row per collection

What is kept (keep_events):
    - the newest 100 messages from the group, whatever they were
    - older messages only if they led to something: an SAP write, a question,
      a handoff, a dock conflict or an undo
    - small talk ("siap bu") is forgotten after 24 hours, even inside the 100
    SAP records, the change log and the notebook are never trimmed: they are
    the audit trail.

If the database can't be read at startup, saving is switched off for that
run. Otherwise the empty demo data would overwrite what the database holds.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

RECENT_MESSAGES = 100
CHATTER_HOURS = 24
KEYS = ("deliveries", "material_documents", "quality_notifications",
        "change_log", "events", "notebook")


def keep_events(events: list[dict], now: datetime | None = None) -> list[dict]:
    """The retention rule. Pure function, so it is easy to test."""
    now = now or datetime.now(timezone.utc)
    chatter_cutoff = now - timedelta(hours=CHATTER_HOURS)
    recent = {id(e) for e in events[-RECENT_MESSAGES:]}
    kept = []
    for e in events:
        if e.get("decision") == "ignored":
            at = e.get("at")
            if at and datetime.fromisoformat(at) < chatter_cutoff:
                continue                      # small talk older than a day
            if id(e) not in recent:
                continue                      # small talk outside the newest 100
        kept.append(e)
    return kept


class Store:
    def __init__(self, url: str | None = None):
        self.url = url if url is not None else os.getenv("DATABASE_URL", "")
        self.enabled = bool(self.url)
        self.status = "memory only (DATABASE_URL not set)"
        self._lock = threading.Lock()
        self._conn = None

    def _connect(self):
        import psycopg
        return psycopg.connect(self.url, connect_timeout=10, autocommit=True)

    def load(self) -> dict | None:
        """Everything saved, or None when there's nothing (or no database)."""
        if not self.enabled:
            return None
        try:
            with self._connect() as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS runa_state (
                                    key     TEXT PRIMARY KEY,
                                    value   TEXT NOT NULL,
                                    updated TIMESTAMPTZ NOT NULL DEFAULT now())""")
                rows = conn.execute("SELECT key, value FROM runa_state").fetchall()
        except Exception as e:
            self.enabled = False
            self.status = f"DATABASE UNREACHABLE, saving off for this run: {type(e).__name__}"
            print(f"[store] {self.status}: {e}")
            return None
        saved = {k: json.loads(v) for k, v in rows if k in KEYS}
        self.status = "database" + ("" if saved else " (empty: starting from the demo data)")
        print(f"[store] {self.status}: {', '.join(saved) or 'nothing saved yet'}")
        return saved or None

    def save(self, snapshot) -> None:
        """Write the current state. `snapshot` is a function returning it,
        called inside the lock, so the last save always holds the newest state.
        One connection is reused; if the database dropped it (a free database
        sleeps when idle), Runa reconnects once and tries again."""
        if not self.enabled:
            return
        with self._lock:
            data = snapshot()
            rows = [(k, json.dumps(data[k], ensure_ascii=False)) for k in KEYS]
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = self._connect()
                    with self._conn.transaction():
                        for key, value in rows:
                            self._conn.execute(
                                """INSERT INTO runa_state (key, value, updated)
                                   VALUES (%s, %s, now())
                                   ON CONFLICT (key) DO UPDATE
                                   SET value = EXCLUDED.value, updated = now()""",
                                (key, value))
                    return
                except Exception as e:
                    self._close()
                    if attempt == 2:
                        print(f"[store] save failed ({type(e).__name__}): {e}")

    def _close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
