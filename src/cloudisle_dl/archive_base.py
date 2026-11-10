#!/usr/bin/env python3
"""Persistent, conservative Cloudisle 2018 acquisition. Explicit run command only.
Control DB uses rollback journaling on the destination share. All HTTP requests
share global rate limits; only one database/file writer may run.
"""

import argparse, contextlib, datetime, email.utils, hashlib, http.client
import json, os, re, shutil, signal, sqlite3, sys, time, urllib.error, urllib.parse
import urllib.request, zipfile, io, threading, concurrent.futures
from pathlib import Path
from html.parser import HTMLParser
from . import core
from .space_guard import SpaceGuard
from .platform_support import db_uri

BASE = core.BASE
ROOT = BASE + "pointclouds/"
RESERVE = 50 * 1024**3


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(v for k, v in attrs if k == "href")


def hrefs(data):
    p = Links()
    p.feed(data.decode("utf8"))
    return p.links


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha(path):
    return core.digest(path)


class PersistentHTTP:
    """One sequential connection; discard it after incomplete reads or errors."""

    def __init__(self):
        self.connection = None
        self.origin = None
        self.connections = 0
        self.requests = 0
        self.last_used = 0
        self.last_attempt_reused = False
        self.idle_recycles = 0
        self.stale_reconnects = 0

    def close(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None
        self.origin = None

    def open(self, url, headers):
        if self.connection is not None and time.monotonic() - self.last_used > 4:
            self.close()
            self.idle_recycles += 1
        u = urllib.parse.urlsplit(url)
        origin = (u.scheme, u.hostname, u.port)
        if u.scheme not in {"http", "https"}:
            raise ValueError("Unsupported HTTP scheme")
        self.last_attempt_reused = self.connection is not None and self.origin == origin
        if self.connection is None or self.origin != origin:
            self.close()
            cls = (
                http.client.HTTPSConnection
                if u.scheme == "https"
                else http.client.HTTPConnection
            )
            self.connection = cls(u.hostname, u.port, timeout=45)
            self.origin = origin
            self.connections += 1
        target = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, ""))
        try:
            self.requests += 1
            self.connection.request("GET", target, headers=headers)
            response = self.connection.getresponse()
        except BaseException:
            self.close()
            raise
        if response.status >= 400:
            error = urllib.error.HTTPError(
                url, response.status, response.reason, response.headers, None
            )
            response.close()
            self.close()
            raise error
        return PersistentResponse(self, response)


class PersistentResponse:
    def __init__(self, owner, response):
        self.owner = owner
        self.response = response
        self.status = response.status
        self.headers = response.headers

    def read(self, *args):
        try:
            return self.response.read(*args)
        except BaseException:
            self.owner.close()
            raise

    def close(self):
        if not self.response.isclosed() or self.response.will_close:
            self.owner.close()
        self.response.close()
        self.owner.last_used = time.monotonic()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is not None:
            self.owner.close()
        self.close()


class BufferedResponse(io.BytesIO):
    def __init__(self, data, status, headers):
        super().__init__(data)
        self.status = status
        self.headers = headers


class LimitedResponse:
    def __init__(self, response, runner):
        self.response = response
        self.runner = runner
        self.status = response.status
        self.headers = response.headers

    def read(self, *args):
        data = self.response.read(*args)
        self.runner.limit_bytes(len(data))
        return data

    def close(self):
        self.response.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.response.__exit__(*exc)


from . import job_totals


class Runner:
    def __init__(self, dest, state, rps=2, mbps=8, batch_size=1, connections=1):
        self.dest = Path(dest)
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.dest.mkdir(parents=True, exist_ok=True)
        self.rps = rps
        self.rate = mbps * 1024**2
        self.last_request = 0
        self.last_report = 0
        self.last_backup = float("-inf")
        self.active = None
        self.stopped = False
        self.space_guard = SpaceGuard(self.dest)
        self.http = PersistentHTTP()
        self.connections = connections
        self.prefetch_http = PersistentHTTP()
        self.request_lock = threading.Lock()
        self.byte_lock = threading.Lock()
        self.network_stop = threading.Event()
        self.next_byte_time = 0
        self.backoff_until = 0
        self.executor = (
            concurrent.futures.ThreadPoolExecutor(max_workers=1)
            if connections == 2
            else None
        )
        self.prefetch = None
        self.next_prefetch = None
        self.batch_size = batch_size
        self.completed_batch = {}
        self.last_flush = time.monotonic()
        self.db = sqlite3.connect(
            db_uri(self.state / "archive.sqlite"), uri=True, timeout=30
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,url TEXT UNIQUE,rel TEXT,kind TEXT,
            source TEXT,node TEXT,points INTEGER,priority INTEGER,state TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,size INTEGER,etag TEXT,modified TEXT,sha256 TEXT,error TEXT);
          CREATE INDEX IF NOT EXISTS job_queue ON jobs(state,priority,id);
          CREATE TABLE IF NOT EXISTS sources(name TEXT PRIMARY KEY,metadata TEXT,expected INTEGER,
            step INTEGER,metadata_status TEXT DEFAULT 'pending',error TEXT);
          CREATE TABLE IF NOT EXISTS nodes(source TEXT,name TEXT,mask INTEGER,points INTEGER,
            PRIMARY KEY(source,name));
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS events(time TEXT,event TEXT,detail TEXT);
          CREATE TABLE IF NOT EXISTS discrepancies(source TEXT,node TEXT,kind TEXT,expected TEXT,actual TEXT,
            PRIMARY KEY(source,node,kind,expected,actual));
          CREATE TABLE IF NOT EXISTS zipmembers(archive TEXT,name TEXT,size INTEGER,crc INTEGER,
            PRIMARY KEY(archive,name));
        """)
        self.db.commit()
        print(json.dumps(dict(event="job_totals_initializing")), flush=True)
        job_totals.install(self.db)
        print(json.dumps(dict(event="job_totals_ready")), flush=True)
        if (self.dest / "control" / "archive.sqlite").exists():
            self.last_backup = time.monotonic()
        self.db.execute("UPDATE jobs SET state='pending' WHERE state='running'")
        self.db.commit()
        self.events = open(self.state / "events.jsonl", "a", buffering=1)

    def flush_completions(self):
        if not self.completed_batch:
            return
        # Buffer only metadata in memory; never hold a DB transaction across HTTP.
        with self.db:
            for (
                job,
                size,
                checksum,
                points,
                etag,
                modified,
            ) in self.completed_batch.values():
                if job["points"] is not None and points != job["points"]:
                    self.db.execute(
                        "INSERT OR IGNORE INTO discrepancies VALUES(?,?,?,?,?)",
                        (
                            job["source"],
                            job["node"],
                            "hierarchy_vs_decoded_count",
                            str(job["points"]),
                            str(points),
                        ),
                    )
                self.db.execute(
                    "UPDATE jobs SET state='done',size=?,sha256=?,points=?,etag=?,modified=?,error=NULL,attempts=attempts+1 WHERE id=?",
                    (size, checksum, points, etag, modified, job["id"]),
                )
        self.completed_batch.clear()
        self.last_flush = time.monotonic()

    def complete_laz(self, j, p, points, etag=None, modified=None):
        self.completed_batch[j["id"]] = (
            dict(j),
            p.stat().st_size,
            sha(p),
            points,
            etag,
            modified,
        )
        if (
            len(self.completed_batch) >= self.batch_size
            or time.monotonic() - self.last_flush >= 5
        ):
            self.flush_completions()

    def event(self, event, **kw):
        self.flush_completions()
        record = dict(time=now(), event=event, **kw)
        self.events.write(json.dumps(record) + "\n")
        self.db.execute(
            "INSERT INTO events VALUES(?,?,?)", (record["time"], event, json.dumps(kw))
        )
        self.db.commit()
        print(json.dumps(record), flush=True)

    def add(self, url, rel, kind, source="", node="", points=None, priority=10):
        if not url.startswith(ROOT):
            raise ValueError("Out-of-scope data URL " + url)
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe relative path")
        self.db.execute(
            "INSERT OR IGNORE INTO jobs(url,rel,kind,source,node,points,priority) VALUES(?,?,?,?,?,?,?)",
            (url, rel, kind, source, node, points, priority),
        )

    def limit_request(self):
        with self.request_lock:
            while True:
                wait = (
                    max(self.last_request + 1 / self.rps, self.backoff_until)
                    - time.monotonic()
                )
                if wait <= 0:
                    break
                if self.network_stop.wait(wait):
                    raise InterruptedError("Downloader stopping")
            if self.network_stop.is_set():
                raise InterruptedError("Downloader stopping")
            self.last_request = time.monotonic()

    def limit_bytes(self, n):
        if not n:
            return
        with self.byte_lock:
            self.next_byte_time = (
                max(time.monotonic(), self.next_byte_time) + n / self.rate
            )
            wait = self.next_byte_time - time.monotonic()
            if wait > 0:
                time.sleep(wait)

    def network_open(self, url, headers=None, transport=None):
        transport = transport or self.http
        self.limit_request()
        h = {
            "User-Agent": "CloudisleResearchArchive/1.0",
            "Accept-Encoding": "identity",
        }
        h.update(headers or {})
        try:
            same_origin = (
                urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(BASE).netloc
            )
            proxy = urllib.request.getproxies().get(urllib.parse.urlsplit(url).scheme)
            if same_origin and not proxy:
                try:
                    response = transport.open(url, h)
                except (
                    http.client.RemoteDisconnected,
                    ConnectionResetError,
                    BrokenPipeError,
                ):
                    # Only retry an idempotent GET that failed before response headers
                    # on a reused socket. Fresh-connection failures retain normal backoff.
                    if not transport.last_attempt_reused:
                        raise
                    transport.close()
                    transport.stale_reconnects += 1
                    self.limit_request()
                    response = transport.open(url, h)
                if not 300 <= response.status < 400:
                    return LimitedResponse(response, self)
                response.close()
                transport.close()
                self.limit_request()
            return LimitedResponse(
                urllib.request.urlopen(
                    urllib.request.Request(url, headers=h), timeout=45
                ),
                self,
            )
        except urllib.error.HTTPError as e:
            if e.code in {403, 429, 503}:
                delay = 900
                ra = e.headers.get("Retry-After", "")
                if ra.isdigit():
                    delay = max(delay, int(ra))
                elif ra:
                    try:
                        delay = max(
                            delay,
                            email.utils.parsedate_to_datetime(ra).timestamp()
                            - time.time(),
                        )
                    except (ValueError, TypeError):
                        pass
                self.backoff_until = max(self.backoff_until, time.monotonic() + delay)
            raise

    def open(self, url, headers=None):
        return self.network_open(url, headers)

    def fetch_ahead(self, url):
        # Only small new LAZ files are buffered; ZIPs and partials use disk streaming.
        with self.network_open(url, transport=self.prefetch_http) as r:
            size = int(r.headers.get("Content-Length", "-1"))
            if r.status != 200 or size < 1 or size > 8 * 1024**2:
                return None
            chunks = []
            total = 0
            while total < size:
                data = r.read(min(256 * 1024, size - total))
                if not data:
                    raise ConnectionError("Truncated prefetched file")
                chunks.append(data)
                total += len(data)
            return BufferedResponse(b"".join(chunks), r.status, r.headers)

    def start_prefetch(self):
        if self.executor and self.prefetch is None and self.next_prefetch:
            url = self.next_prefetch
            self.next_prefetch = None
            self.prefetch = (url, self.executor.submit(self.fetch_ahead, url))

    def open_payload(self, url, headers):
        if self.prefetch and self.prefetch[0] == url:
            _, future = self.prefetch
            self.prefetch = None
            response = (
                future.result()
            )  # Propagate errors into the existing retry/backoff path.
            self.start_prefetch()
            if response is not None:
                return response
        else:
            self.start_prefetch()
        return self.open(url, headers)

    def close_network(self):
        self.network_stop.set()
        self.next_prefetch = None
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
        if (
            self.prefetch
            and self.prefetch[1].done()
            and not self.prefetch[1].cancelled()
        ):
            try:
                response = self.prefetch[1].result()
                if response is not None:
                    response.close()
            except Exception:
                pass
        self.http.close()
        self.prefetch_http.close()

    def space(self, need=0, consume=False):
        self.space_guard.check(need, consume)

    def write(self, path, data):
        self.space(len(data), consume=True)
        core.atomic(path, data)

    def small(self, url, cap=8 * 1024**2):
        with self.open(url) as r:
            if r.status != 200:
                raise ValueError("Unexpected metadata response")
            b = r.read(cap + 1)
            if len(b) > cap:
                raise ValueError("Control response exceeds size limit")
            return b

    def report(self, force=False, phase="running"):
        if force or time.monotonic() - self.last_flush >= 5:
            self.flush_completions()
        if not force and time.monotonic() - self.last_report < 30:
            return
        self.last_report = time.monotonic()
        self.flush_completions()
        counts, verified = job_totals.read(self.db)
        record = dict(
            updated=now(),
            phase=phase,
            active=self.active,
            counts=counts,
            verified_payload_bytes=verified,
            completion_batch_limit=self.batch_size,
            http_reuse=dict(
                requests=self.http.requests + self.prefetch_http.requests,
                connections=self.http.connections + self.prefetch_http.connections,
                stale_reconnects=self.http.stale_reconnects
                + self.prefetch_http.stale_reconnects,
                idle_recycles=self.http.idle_recycles
                + self.prefetch_http.idle_recycles,
            ),
            concurrent_download_limit=self.connections,
            free_bytes=shutil.disk_usage(self.dest).free,
            rate_limits=dict(
                requests_per_second=self.rps, MiB_per_second=self.rate / 1024**2
            ),
        )
        core.atomic(self.state / "status.json", json.dumps(record, indent=2).encode())
        self.write(self.dest / "status.json", json.dumps(record, indent=2).encode())
        if (
            phase.startswith("finished")
            or time.monotonic() - self.last_backup > 6 * 3600
        ):
            target = self.dest / "control" / "archive.sqlite"
            target.parent.mkdir(exist_ok=True)
            tmp = Path(str(target) + ".tmp-" + str(time.time_ns()))
            # A connection context manager commits but does not close SQLite.
            # Explicitly close and checkpoint before publishing a standalone DB.
            with contextlib.closing(
                sqlite3.connect(db_uri(tmp), uri=True, timeout=30)
            ) as out:
                self.db.backup(out)
                out.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                out.execute("PRAGMA journal_mode=DELETE")
            os.replace(tmp, target)
            # Start the interval after completion; a slow copy must not make
            # another backup immediately due. The live DB is always durable.
            self.last_backup = time.monotonic()
        self.last_report = time.monotonic()
        print(json.dumps(record), flush=True)

    def discover(self):
        # Refresh on first run only; inventory evidence is preserved with the archive.
        if self.db.execute("SELECT 1 FROM settings WHERE key='discovered'").fetchone():
            return
        data = self.small(ROOT)
        self.write(self.dest / "control" / "source-directory.html", data)
        directories = []
        archives = []
        for item in hrefs(data):
            if re.fullmatch(r"[A-Za-z0-9_.-]+/", item):
                directories.append(item[:-1])
            elif re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", item):
                archives.append(item)
        if len(directories) < 400:
            raise ValueError("Unexpectedly short official source directory listing")
        for name in archives:
            self.add(ROOT + name, "archives/" + name, "zip", name, priority=0)
        # Every actually listed dataset; grids first, including the two extra names.
        ordered = sorted(
            directories,
            key=lambda n: (not bool(re.fullmatch(r"\d{3}_\d{4}(?:\.laz)?", n)), n),
        )
        for i, name in enumerate(ordered):
            self.db.execute("INSERT OR IGNORE INTO sources(name) VALUES(?)", (name,))
            self.add(
                ROOT + name + "/cloud.js",
                "pointclouds/" + name + "/cloud.js",
                "meta",
                name,
                priority=100 + i * 10,
            )
        # Record published but absent grid entries as explicit probes, not silently excluded.
        audit = json.loads(
            Path(__file__).with_name("2018-metadata-audit.json").read_text()
        )
        for row in audit:
            name = row["tile"]
            if name not in directories:
                self.db.execute(
                    "INSERT OR IGNORE INTO sources(name) VALUES(?)", (name,)
                )
                self.add(
                    ROOT + name + "/cloud.js",
                    "pointclouds/" + name + "/cloud.js",
                    "meta",
                    name,
                    priority=10000,
                )
        # Additional broken viewer references found in official listing.
        for name in [
            "GZ_2018_LiDAR",
            "MT_2018_run2",
            "71_MGI",
            "60_Swieqi",
            "62_Tarxien",
        ]:
            self.db.execute("INSERT OR IGNORE INTO sources(name) VALUES(?)", (name,))
            self.add(
                ROOT + name + "/cloud.js",
                "pointclouds/" + name + "/cloud.js",
                "meta",
                name,
                priority=10001,
            )
        self.db.execute("INSERT INTO settings VALUES('discovered',?)", (now(),))
        self.db.commit()
        self.event(
            "inventory_created", listed_sources=len(directories), archives=archives
        )

    def mark(self, job, path, etag=None, modified=None):
        self.db.execute(
            "UPDATE jobs SET state='done',size=?,sha256=?,etag=?,modified=?,error=NULL WHERE id=?",
            (path.stat().st_size, sha(path), etag, modified, job["id"]),
        )
        self.db.commit()

    def metadata(self, j):
        p = self.dest / j["rel"]
        data = self.small(j["url"])
        m = json.loads(data)
        if (
            m.get("version") != "1.7"
            or m.get("pointAttributes") != "LAZ"
            or m.get("octreeDir") != "data"
        ):
            raise ValueError("Unsupported cloud metadata format")
        step = m["hierarchyStepSize"]
        if not isinstance(step, int) or not 1 <= step <= 10:
            raise ValueError("Invalid hierarchy step")
        self.write(p, data)
        self.db.execute(
            "UPDATE sources SET metadata=?,expected=?,step=?,metadata_status='ok' WHERE name=?",
            (json.dumps(m), m["points"], step, j["source"]),
        )
        self.add(
            ROOT + j["source"] + "/data/r/r.hrc",
            "pointclouds/" + j["source"] + "/data/r/r.hrc",
            "hrc",
            j["source"],
            "r",
            priority=j["priority"] + 1,
        )
        self.mark(j, p)

    def hierarchy(self, j):
        src = self.db.execute(
            "SELECT * FROM sources WHERE name=?", (j["source"],)
        ).fetchone()
        p = self.dest / j["rel"]
        b = self.small(j["url"])
        rows = core.decode_hierarchy(b, j["node"], src["step"])
        self.write(p, b)
        for name, mask, points in rows:
            old = self.db.execute(
                "SELECT mask,points FROM nodes WHERE source=? AND name=?",
                (j["source"], name),
            ).fetchone()
            if old and (old["mask"], old["points"]) != (mask, points):
                self.db.execute(
                    "INSERT OR IGNORE INTO discrepancies VALUES(?,?,?,?,?)",
                    (
                        j["source"],
                        name,
                        "hierarchy_disagreement",
                        json.dumps(dict(old)),
                        json.dumps(dict(mask=mask, points=points)),
                    ),
                )
                # The deeper hierarchy root is the more detailed record. Preserve both in the audit.
                self.db.execute(
                    "UPDATE nodes SET mask=?,points=? WHERE source=? AND name=?",
                    (old["mask"] | mask, points, j["source"], name),
                )
            self.db.execute(
                "INSERT OR IGNORE INTO nodes VALUES(?,?,?,?)",
                (j["source"], name, mask, points),
            )
            path = "data/" + core.hierarchy_path(name, src["step"]) + "/" + name
            self.add(
                ROOT + j["source"] + "/" + path + ".laz",
                "pointclouds/" + j["source"] + "/" + path + ".laz",
                "laz",
                j["source"],
                name,
                points,
                priority=j["priority"] + 1,
            )
            if len(name) - len(j["node"]) == src["step"] and mask:
                self.add(
                    ROOT + j["source"] + "/" + path + ".hrc",
                    "pointclouds/" + j["source"] + "/" + path + ".hrc",
                    "hrc",
                    j["source"],
                    name,
                    priority=j["priority"],
                )
        self.mark(j, p)

    def directory(self, j):
        # Fallback for missing hierarchies/metadata. Enumerate only actual server links.
        b = self.small(j["url"])
        self.write(self.dest / j["rel"], b)
        prefix = j["url"]
        relative = Path(j["rel"]).parent
        for link in hrefs(b):
            if re.fullmatch(r"[0-7r]+/", link):
                self.add(
                    prefix + link,
                    str(relative / link / "directory.html"),
                    "dir",
                    j["source"],
                    priority=j["priority"],
                )
            elif re.fullmatch(r"r[0-7]*\.(laz|hrc)", link):
                kind = link.rsplit(".", 1)[1]
                node = link.split(".")[0]
                known = self.db.execute(
                    "SELECT points FROM nodes WHERE source=? AND name=?",
                    (j["source"], node),
                ).fetchone()
                # If no metadata, retain hierarchy file as opaque control; it cannot prove coverage.
                src = self.db.execute(
                    "SELECT step FROM sources WHERE name=?", (j["source"],)
                ).fetchone()
                self.add(
                    prefix + link,
                    str(relative / link),
                    kind if kind == "laz" or src["step"] else "control",
                    j["source"],
                    node,
                    known["points"] if known else None,
                    priority=j["priority"] + 1,
                )
        self.mark(j, self.dest / j["rel"])

    def validate(self, j, p):
        if j["kind"] == "laz":
            import laspy

            with laspy.open(p) as reader:
                header_count = reader.header.point_count
                n = sum(len(chunk) for chunk in reader.chunk_iterator(1_000_000))
            if n != header_count:
                raise ValueError("Decoded point count differs from LAS header")
            if self.batch_size > 1:
                return n
            if j["points"] is not None and n != j["points"]:
                self.db.execute(
                    "INSERT OR IGNORE INTO discrepancies VALUES(?,?,?,?,?)",
                    (
                        j["source"],
                        j["node"],
                        "hierarchy_vs_decoded_count",
                        str(j["points"]),
                        str(n),
                    ),
                )
            self.db.execute("UPDATE jobs SET points=? WHERE id=?", (n, j["id"]))
            return n
        elif j["kind"] == "zip":
            self.active = {
                "file": j["rel"],
                "stage": "ZIP CRC validation",
                "bytes": p.stat().st_size,
            }
            with zipfile.ZipFile(p) as z:
                # Full member reads validate CRC; archive contents are not extracted.
                for member in z.infolist():
                    with z.open(member) as f:
                        for block in iter(lambda: f.read(1024**2), b""):
                            self.report()
                    self.db.execute(
                        "INSERT OR REPLACE INTO zipmembers VALUES(?,?,?,?)",
                        (j["source"], member.filename, member.file_size, member.CRC),
                    )
                    self.db.commit()
            self.event("zip_crc_validated", archive=j["source"])

    def payload(self, j):
        p = self.dest / j["rel"]
        p.parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(p) + ".part")
        info = Path(str(p) + ".part.json")
        # Recover crash between final rename and DB commit without transferring again.
        if p.exists():
            points = self.validate(j, p)
            if j["kind"] == "laz" and self.batch_size > 1:
                self.complete_laz(j, p, points)
            else:
                self.mark(j, p)
            return
        ident = json.loads(info.read_text()) if info.exists() else {}
        offset = part.stat().st_size if part.exists() else 0
        validator = ident.get("etag") or ident.get("modified")
        if not validator or ident.get("url") != j["url"]:
            offset = 0
        if not (offset and offset == ident.get("size")):
            h = {}
            if offset:
                h = {"Range": f"bytes={offset}-", "If-Range": validator}
            with self.open_payload(j["url"], h) as r:
                if r.status == 206:
                    cr = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)", r.headers.get("Content-Range", "")
                    )
                    if not cr or int(cr[1]) != offset or int(cr[2]) + 1 != int(cr[3]):
                        raise ValueError("Invalid resume response range")
                    size = int(cr[3])
                    etag = r.headers.get("ETag")
                    modified = r.headers.get("Last-Modified")
                    if size != ident["size"] or (
                        ident.get("etag") and etag != ident["etag"]
                    ):
                        raise ValueError("Remote partial file changed")
                elif r.status == 200:
                    offset = 0
                    size = int(r.headers.get("Content-Length", "-1"))
                    etag = r.headers.get("ETag")
                    modified = r.headers.get("Last-Modified")
                else:
                    raise ValueError("Unexpected payload response")
                if size < 1:
                    raise ValueError("Missing file size")
                if int(r.headers.get("Content-Length", "-1")) != size - offset:
                    raise ValueError("Incorrect response length")
                if r.headers.get("Content-Encoding", "identity") != "identity":
                    raise ValueError("Unexpected HTTP encoding")
                self.space(size - offset)
                if etag and etag.startswith("W/"):
                    etag = None
                ident = dict(url=j["url"], size=size, etag=etag, modified=modified)
                self.write(info, json.dumps(ident).encode())
                if j["kind"] != "laz" or self.batch_size == 1:
                    self.db.execute(
                        "UPDATE jobs SET size=?,etag=?,modified=? WHERE id=?",
                        (size, etag, modified, j["id"]),
                    )
                    self.db.commit()
                start = time.monotonic()
                received = 0
                with open(part, "ab" if offset else "wb") as out:
                    try:
                        while True:
                            data = r.read(256 * 1024)
                            if not data:
                                break
                            if out.tell() + len(data) > size:
                                raise ValueError("File exceeds advertised length")
                            self.space(len(data), consume=True)
                            out.write(data)
                            received += len(data)
                            self.active = dict(
                                file=j["rel"],
                                stage="download",
                                bytes=out.tell(),
                                total=size,
                            )
                            self.report()
                    finally:
                        out.flush()
                        os.fsync(out.fileno())
                if part.stat().st_size != size:
                    raise ConnectionError("Truncated download")
        try:
            points = self.validate(j, part)
        except Exception:
            os.replace(part, str(part) + ".invalid-" + str(time.time_ns()))
            raise
        if j["kind"] == "laz" and self.batch_size > 1:
            os.replace(part, p)
            info.unlink(missing_ok=True)
            self.complete_laz(j, p, points, ident.get("etag"), ident.get("modified"))
            return
        checksum = sha(part)
        os.replace(part, p)
        info.unlink(missing_ok=True)
        self.db.execute(
            "UPDATE jobs SET state='done',size=?,sha256=?,error=NULL WHERE id=?",
            (p.stat().st_size, checksum, j["id"]),
        )
        self.db.commit()
        if j["kind"] == "zip":
            self.event("archive_saved", file=j["rel"], bytes=p.stat().st_size)

    def fail(self, j, e):
        permanent = isinstance(e, urllib.error.HTTPError) and e.code in {404, 410}
        state = "missing" if permanent else "failed"
        self.db.execute(
            "UPDATE jobs SET state=?,error=? WHERE id=?", (state, str(e), j["id"])
        )
        if j["kind"] == "meta":
            self.db.execute(
                "UPDATE sources SET metadata_status=?,error=? WHERE name=?",
                (state, str(e), j["source"]),
            )
        if permanent and j["kind"] in {"meta", "hrc"}:
            rel = "pointclouds/" + j["source"] + "/data/"
            if j["kind"] == "hrc":
                rel = str(Path(j["rel"]).parent) + "/"
            self.add(
                BASE + rel,
                rel + "directory.html",
                "dir",
                j["source"],
                priority=j["priority"] + 2,
            )
        self.db.commit()
        self.event("job_failure", file=j["rel"], state=state, error=str(e))

    def verify_job(self, j):
        p = self.dest / j["rel"]
        return p.exists() and p.stat().st_size == j["size"] and sha(p) == j["sha256"]

    def final_audit(self):
        self.flush_completions()
        self.event("final_audit_started")
        for j in self.db.execute("SELECT * FROM jobs WHERE state='done' ORDER BY id"):
            p = self.dest / j["rel"]
            self.active = {"file": j["rel"], "stage": "final SHA-256 audit"}
            if not self.verify_job(j):
                self.db.execute(
                    "UPDATE jobs SET state='failed',error='Final local integrity mismatch' WHERE id=?",
                    (j["id"],),
                )
                self.db.commit()
            self.report()
        rows = []
        for src in self.db.execute("SELECT * FROM sources ORDER BY name"):
            counts = {
                r["state"]: r["n"]
                for r in self.db.execute(
                    "SELECT state,COUNT(*) n FROM jobs WHERE source=? GROUP BY state",
                    (src["name"],),
                )
            }
            inventoried = self.db.execute(
                "SELECT COALESCE(SUM(points),0) FROM nodes WHERE source=?",
                (src["name"],),
            ).fetchone()[0]
            verified = self.db.execute(
                "SELECT COALESCE(SUM(points),0) FROM jobs WHERE source=? AND kind='laz' AND state='done'",
                (src["name"],),
            ).fetchone()[0]
            warnings = self.db.execute(
                "SELECT COUNT(*) FROM discrepancies WHERE source=?", (src["name"],)
            ).fetchone()[0]
            rows.append(
                dict(
                    metadata_discrepancies=warnings,
                    source=src["name"],
                    expected_points=src["expected"],
                    hierarchy_points=inventoried,
                    verified_points=verified,
                    states=counts,
                    complete_relative_to_metadata=bool(
                        not warnings
                        and src["expected"] is not None
                        and src["expected"] == inventoried == verified
                        and not any(
                            counts.get(s)
                            for s in ["failed", "missing", "pending", "running"]
                        )
                    ),
                )
            )
        failures = [
            dict(r)
            for r in self.db.execute(
                "SELECT url,rel,kind,source,state,error FROM jobs WHERE state!='done'"
            )
        ]
        self.write(
            self.dest / "completeness.json",
            json.dumps(
                dict(
                    checked=now(),
                    sources=rows,
                    failures=failures,
                    limitation="Representations overlap. Point-count equality verifies each representation, not national survey completeness or uniqueness.",
                ),
                indent=2,
            ).encode(),
        )
        self.write(
            self.dest / "metadata-discrepancies.json",
            json.dumps(
                [dict(r) for r in self.db.execute("SELECT * FROM discrepancies")],
                indent=2,
            ).encode(),
        )
        self.write(
            self.dest / "missing-files.json", json.dumps(failures, indent=2).encode()
        )
        self.event(
            "acquisition_finished",
            failed_jobs=len(failures),
            complete_sources=sum(r["complete_relative_to_metadata"] for r in rows),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO settings VALUES('finished',?)", (now(),)
        )
        self.db.commit()
        self.report(True, "finished_with_gaps" if failures else "finished")

    def select_job(self, ids=()):
        exclusions = (
            (" AND id NOT IN (" + ",".join("?" for _ in ids) + ")") if ids else ""
        )
        first = self.db.execute(
            "SELECT * FROM jobs WHERE state='pending'"
            + exclusions
            + " ORDER BY priority,id LIMIT 1",
            ids,
        ).fetchone()
        if first is None or first["kind"] not in ("dir", "hrc"):
            return first
        # Alternate one discovery job with one payload batch. Both lookups use
        # job_queue; never scan all pending jobs or rewrite stored priorities.
        payload_turn = getattr(self, "payload_turn", True)
        self.payload_turn = not payload_turn
        if payload_turn:
            candidate = self.db.execute(
                "SELECT * FROM jobs WHERE state='pending' AND priority=?"
                + exclusions
                + " ORDER BY id LIMIT 1",
                (first["priority"] + 1, *ids),
            ).fetchone()
            if (
                candidate is not None
                and candidate["source"] == first["source"]
                and candidate["kind"] in ("laz", "control", "hrc")
            ):
                return candidate
        return first

    def run(self):
        self.discover()
        self.event("run_started", destination=str(self.dest))
        while True:
            if time.monotonic() - self.last_flush >= 5:
                self.flush_completions()
            ids = tuple(self.completed_batch)
            j = self.select_job(ids)
            if j is None:
                self.flush_completions()
                break
            if j["kind"] != "laz":
                self.flush_completions()
            self.next_prefetch = None
            if self.executor and j["kind"] == "laz":
                excluded = tuple(self.completed_batch) + (j["id"],)
                next_job = self.db.execute(
                    "SELECT * FROM jobs WHERE state='pending' AND id NOT IN ("
                    + ",".join("?" for _ in excluded)
                    + ") ORDER BY priority,id LIMIT 1",
                    excluded,
                ).fetchone()
                if next_job is not None and next_job["kind"] == "laz":
                    path = self.dest / next_job["rel"]
                    if not path.exists() and not Path(str(path) + ".part").exists():
                        self.next_prefetch = next_job["url"]

            self.space()
            self.active = {"file": j["rel"], "stage": j["kind"]}
            self.report()
            if j["kind"] != "laz" or self.batch_size == 1:
                self.db.execute(
                    "UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?",
                    (j["id"],),
                )
                self.db.commit()
            for attempt in range(6):
                try:
                    if j["kind"] == "meta":
                        self.metadata(j)
                    elif j["kind"] == "hrc":
                        self.hierarchy(j)
                    elif j["kind"] == "dir":
                        self.directory(j)
                    elif j["kind"] == "control":
                        p = self.dest / j["rel"]
                        self.write(p, self.small(j["url"]))
                        self.mark(j, p)
                    else:
                        self.payload(j)
                    break
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as e:
                    if (
                        isinstance(e, RuntimeError)
                        or isinstance(e, OSError)
                        and getattr(e, "errno", None) in {5, 13, 28, 30, 107, 112}
                    ):
                        raise
                    permanent = isinstance(e, urllib.error.HTTPError) and e.code in {
                        404,
                        410,
                    }
                    if permanent or attempt == 5:
                        self.fail(j, e)
                        break
                    delay = min(300, 20 * 2**attempt)
                    if isinstance(e, urllib.error.HTTPError) and e.code in {
                        403,
                        429,
                        503,
                    }:
                        delay = max(delay, 900)
                        ra = e.headers.get("Retry-After", "")
                        if ra.isdigit():
                            delay = max(delay, int(ra))
                        elif ra:
                            try:
                                delay = max(
                                    delay,
                                    email.utils.parsedate_to_datetime(ra).timestamp()
                                    - time.time(),
                                )
                            except (ValueError, TypeError):
                                pass
                    self.event(
                        "retry",
                        file=j["rel"],
                        attempt=attempt + 1,
                        delay_seconds=delay,
                        error=str(e),
                    )
                    self.active = {
                        "file": j["rel"],
                        "stage": "backoff",
                        "retry_at": time.time() + delay,
                    }
                    self.report(True)
                    time.sleep(delay)
        # One delayed retry pass for transient failures, never hammer permanent 404s.
        if not self.db.execute(
            "SELECT 1 FROM settings WHERE key='retry_pass'"
        ).fetchone():
            self.db.execute("UPDATE jobs SET state='pending' WHERE state='failed'")
            self.db.execute("INSERT INTO settings VALUES('retry_pass','1')")
            self.db.commit()
            if self.db.execute("SELECT 1 FROM jobs WHERE state='pending'").fetchone():
                return self.run()
        self.final_audit()
