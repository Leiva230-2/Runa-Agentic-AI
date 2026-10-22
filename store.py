"""
Runa's memory across restarts.

Without a database, runa-sap keeps everything in memory, and a deploy or a
crash wipes the SAP records, the dashboard events, the facts, the learned
rules and the decisions. With a database, every change is saved and loaded
again when the app starts. Which database is chosen by the settings:

    RUNA_DYNAMO_TABLE set    Amazon DynamoDB (credentials from the usual
                             AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
                             AWS_REGION settings)
    DATABASE_URL set         Postgres (e.g. Neon), one table: runa_state
    neither                  memory only, exactly as before (local tests)

Both databases hold the same thing: one entry per collection ("facts",
"notebook", ...), the collection written as JSON. DynamoDB items are at most
400 KB, so a big collection is split into parts.

What is kept (keep_events):
    - the newest 100 messages from the group, whatever they were
    - older messages only if they led to something: an SAP write, a question,
      a handoff, a dock conflict or an undo
    - small talk ("siap bu") is forgotten after 24 hours, even inside the 100
    SAP records, the change log, the notebook, the facts and the decisions are
    never trimmed: they are the audit trail.

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
        "change_log", "events", "notebook", "facts", "decisions")


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


# ================================================================ Postgres
class PostgresBackend:
    def __init__(self, url: str):
        self.url = url
        self.name = "Postgres"
        self._conn = None

    def _connect(self):
        import psycopg
        return psycopg.connect(self.url, connect_timeout=10, autocommit=True)

    def read(self) -> dict[str, str]:
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS runa_state (
                                key     TEXT PRIMARY KEY,
                                value   TEXT NOT NULL,
                                updated TIMESTAMPTZ NOT NULL DEFAULT now())""")
            return dict(conn.execute("SELECT key, value FROM runa_state").fetchall())

    def write(self, rows: dict[str, str]) -> None:
        """One connection is reused; if the database dropped it (a free
        database sleeps when idle), reconnect once and try again."""
        for attempt in (1, 2):
            try:
                if self._conn is None or self._conn.closed:
                    self._conn = self._connect()
                with self._conn.transaction():
                    for key, value in rows.items():
                        self._conn.execute(
                            """INSERT INTO runa_state (key, value, updated)
                               VALUES (%s, %s, now())
                               ON CONFLICT (key) DO UPDATE
                               SET value = EXCLUDED.value, updated = now()""",
                            (key, value))
                return
            except Exception:
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None
                if attempt == 2:
                    raise


# ================================================================ DynamoDB
class DynamoBackend:
    """For each collection, a few items in the table:
         "facts"      {"parts": 2, "updated": "..."}   which parts to read
         "facts#0"    {"v": <bytes>}                   the JSON, part 1
         "facts#1"    {"v": <bytes>}                   the JSON, part 2
    Parts are written first and the "facts" item last, so a reader never sees
    a part count for parts that aren't there yet.

    The table's own key names are read from AWS (DescribeTable), so any table
    works whose partition key is a String: "facts" goes in the partition key,
    and if the table also has a sort key, every item uses the same sort value."""
    PART_BYTES = 350_000                      # DynamoDB's limit is 400 KB per item
    SORT_VALUE = {"S": "runa", "N": 0, "B": b"runa"}

    def __init__(self, table: str):
        import boto3
        self.region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-southeast-2"
        self.table = boto3.resource("dynamodb", region_name=self.region).Table(table)
        self.name = f"DynamoDB {table} ({self.region})"
        self.parts: dict[str, int] = {}       # how many parts each key has now
        self._schema = None

    def _learn_schema(self):
        """Which attributes are the keys, and their types. Asked once."""
        if self._schema:
            return
        desc = self.table.meta.client.describe_table(TableName=self.table.name)["Table"]
        types = {a["AttributeName"]: a["AttributeType"] for a in desc["AttributeDefinitions"]}
        keys = {k["KeyType"]: k["AttributeName"] for k in desc["KeySchema"]}
        hash_key, sort_key = keys["HASH"], keys.get("RANGE")
        if types[hash_key] != "S":
            raise ValueError(f"the partition key '{hash_key}' must be a String")
        self._schema = (hash_key, sort_key, self.SORT_VALUE[types[sort_key]] if sort_key else None)
        self.name += f" [key {hash_key}" + (f" + {sort_key}]" if sort_key else "]")

    def _key(self, name: str) -> dict:
        hash_key, sort_key, sort_value = self._schema
        return {hash_key: name, sort_key: sort_value} if sort_key else {hash_key: name}

    def _get(self, names: list[str]) -> dict[str, dict]:
        """BatchGetItem, 100 keys at a time, retrying what DynamoDB sends back."""
        hash_key = self._schema[0]
        found = {}
        for i in range(0, len(names), 100):
            request = {self.table.name: {"Keys": [self._key(n) for n in names[i:i + 100]]}}
            while request:
                resp = self.table.meta.client.batch_get_item(RequestItems=request)
                for item in resp["Responses"].get(self.table.name, []):
                    found[item[hash_key]] = item
                request = resp.get("UnprocessedKeys") or None
        return found

    def read(self) -> dict[str, str]:
        self._learn_schema()
        heads = self._get(list(KEYS))
        self.parts = {k: int(heads[k]["parts"]) for k in heads}
        names = [f"{k}#{i}" for k, n in self.parts.items() for i in range(n)]
        parts = self._get(names) if names else {}
        rows = {}
        for key, n in self.parts.items():
            chunks = [parts.get(f"{key}#{i}") for i in range(n)]
            if all(chunks):
                rows[key] = b"".join(bytes(c["v"]) for c in chunks).decode("utf-8")
        return rows

    def write(self, rows: dict[str, str]) -> None:
        self._learn_schema()
        hash_key, sort_key, _ = self._schema
        pkeys = [hash_key] + ([sort_key] if sort_key else [])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        heads, stale = [], []
        with self.table.batch_writer(overwrite_by_pkeys=pkeys) as batch:
            for key, value in rows.items():
                data = value.encode("utf-8")
                chunks = [data[i:i + self.PART_BYTES]
                          for i in range(0, len(data), self.PART_BYTES)] or [b""]
                for i, chunk in enumerate(chunks):
                    batch.put_item(Item={**self._key(f"{key}#{i}"), "v": chunk})
                heads.append({**self._key(key), "parts": len(chunks), "updated": now})
                stale += [f"{key}#{i}" for i in range(len(chunks), self.parts.get(key, 0))]
                self.parts[key] = len(chunks)
        with self.table.batch_writer(overwrite_by_pkeys=pkeys) as batch:
            for head in heads:
                batch.put_item(Item=head)
        if stale:                                 # a collection got smaller
            with self.table.batch_writer() as batch:
                for name in stale:
                    batch.delete_item(Key=self._key(name))


# =================================================================== Store
class Store:
    def __init__(self):
        table = os.getenv("RUNA_DYNAMO_TABLE", "")
        url = os.getenv("DATABASE_URL", "")
        self.backend = None
        self.status = "memory only (no database set)"
        try:
            if table:
                self.backend = DynamoBackend(table)
            elif url:
                self.backend = PostgresBackend(url)
        except Exception as e:                       # e.g. boto3 missing
            self.status = f"DATABASE UNAVAILABLE ({type(e).__name__}), memory only"
            print(f"[store] {self.status}: {e}")
        self.enabled = self.backend is not None
        self._saved: dict[str, str] = {}             # what the database holds now
        self._lock = threading.Lock()

    def load(self) -> dict | None:
        """Everything saved, or None when there's nothing (or no database)."""
        if not self.enabled:
            return None
        try:
            rows = self.backend.read()
        except Exception as e:
            self.enabled = False
            self.status = (f"{self.backend.name} UNREACHABLE, saving off for this run: "
                           f"{type(e).__name__}")
            print(f"[store] {self.status}: {e}")
            return None
        rows = {k: v for k, v in rows.items() if k in KEYS}
        self._saved = dict(rows)
        self.status = self.backend.name + ("" if rows else " (empty: starting from the demo data)")
        print(f"[store] {self.status}: {', '.join(rows) or 'nothing saved yet'}")
        return {k: json.loads(v) for k, v in rows.items()} or None

    def save(self, snapshot) -> None:
        """Write what changed. `snapshot` is a function returning the current
        state, called inside the lock, so the last save always holds the
        newest state. Unchanged collections are not written again."""
        if not self.enabled:
            return
        with self._lock:
            data = snapshot()
            rows = {k: json.dumps(data[k], ensure_ascii=False) for k in KEYS}
            changed = {k: v for k, v in rows.items() if self._saved.get(k) != v}
            if not changed:
                return
            try:
                self.backend.write(changed)
                self._saved.update(changed)
            except Exception as e:
                print(f"[store] save failed ({type(e).__name__}): {e}")
