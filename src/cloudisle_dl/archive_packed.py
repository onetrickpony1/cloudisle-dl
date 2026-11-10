"""Packed acquisition worker with resumable, validated writes."""

import concurrent.futures, threading
from pathlib import Path
from . import archive_base as base
from .pack_store import PackStore, MAX_FILE, MAX_MEMBERS, decoded


class Runner(base.Runner):
    def __init__(self, *args, **kwargs):
        self.pack_enabled = kwargs.pop("pack_enabled", True)
        super().__init__(*args, **kwargs)
        if self.executor:
            self.executor.shutdown(wait=True)
        self.executor = None
        self.store = PackStore(self.dest, self.db, self.space)
        self.store.recover()
        self.pack_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.connections
        )
        self.pack_local = threading.local()
        self.transport_lock = threading.Lock()
        self.available_transports = [self.http, self.prefetch_http][: self.connections]

    def fetch_small(self, j):
        if not hasattr(self.pack_local, "http"):
            with self.transport_lock:
                self.pack_local.http = self.available_transports.pop()
        with self.network_open(j["url"], transport=self.pack_local.http) as response:
            size = int(response.headers.get("Content-Length", "-1"))
            if (
                response.status != 200
                or not 0 < size <= MAX_FILE
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                return None
            body = response.read(size + 1)
            if len(body) != size:
                raise ConnectionError("Truncated or oversized packed payload")
            decoded(body)  # Reject a bad source file independently before group commit.
            return (dict(j), body, dict(response.headers))

    def eligible(self, j):
        p = self.dest / j["rel"]
        return j["kind"] == "laz" and not any(
            x.exists() for x in (p, Path(str(p) + ".part"), Path(str(p) + ".part.json"))
        )

    def payload(self, j):
        if not self.pack_enabled or not self.eligible(j):
            return super().payload(j)
        self.flush_completions()
        # Recoverable packs are reconciled at startup before queue selection.
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE state='pending' AND priority=? AND id>=? ORDER BY id LIMIT ?",
            (j["priority"], j["id"], MAX_MEMBERS),
        ).fetchall()
        batch = []
        for row in rows:
            if not self.eligible(row):
                break
            batch.append(row)
        if not batch or batch[0]["id"] != j["id"]:
            return super().payload(j)
        futures = [(row, self.pack_pool.submit(self.fetch_small, row)) for row in batch]
        items = []
        current_error = None
        current_present = False
        for row, future in futures:
            try:
                item = future.result()
            except Exception as e:
                if row["id"] == j["id"]:
                    current_error = e
                continue  # Other failed jobs stay pending and take their normal retry turn.
            if item is not None:
                items.append(item)
                if row["id"] == j["id"]:
                    current_present = True
        if items:
            try:
                self.store.commit(items)
            except Exception as e:
                raise RuntimeError(
                    "Pack commit failed; restart recovery required: " + str(e)
                ) from e
        if current_error:
            raise current_error
        if not current_present:
            return super().payload(j)

    def close_network(self):
        self.network_stop.set()
        if hasattr(self, "pack_pool"):
            self.pack_pool.shutdown(wait=True, cancel_futures=True)
        super().close_network()

    def verify_job(self, j):
        return self.store.verify(j)
