"""Immutable packs: durable data first, transactional index second; recover from manifest."""

import hashlib, io, json, os, uuid, zipfile, sqlite3
from pathlib import Path, PurePosixPath
import laspy

MAX_FILE = 8 * 1024**2
MAX_MEMBERS = 20


def decoded(body):
    with laspy.open(io.BytesIO(body)) as reader:
        count = sum(len(c) for c in reader.chunk_iterator(1000000))
        if count != reader.header.point_count:
            raise ValueError("LAS point-count mismatch")
        return count


def safe_rel(rel):
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or ".." in p.parts or "\\" in rel:
        raise ValueError("Unsafe member path")
    return rel


class PackStore:
    def __init__(self, root, db, space):
        self.root = Path(root)
        self.db = db
        self.space = space
        self.folder = self.root / "packs"
        self.folder.mkdir(exist_ok=True)
        self.db.executescript("""CREATE TABLE IF NOT EXISTS packs(path TEXT PRIMARY KEY,sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pack_members(job_id INTEGER PRIMARY KEY,pack TEXT NOT NULL,member TEXT NOT NULL,UNIQUE(pack,member));""")
        self.db.commit()
        self.hook = lambda stage: None

    def _hash(self, p):
        h = hashlib.sha256()
        with p.open("rb") as f:
            for b in iter(lambda: f.read(1024**2), b""):
                h.update(b)
        return h.hexdigest()

    def inspect(self, path, decode=True):
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("manifest.json")
            if info.file_size > 1024**2:
                raise ValueError("Manifest oversized")
            manifest = json.loads(z.read(info))
            if manifest.get("version") != 1:
                raise ValueError("Unsupported pack version")
            rows = manifest["members"]
            if not 1 <= len(rows) <= MAX_MEMBERS:
                raise ValueError("Pack member limit")
            names = [safe_rel(r["rel"]) for r in rows]
            if len(set(names)) != len(names) or len({r["id"] for r in rows}) != len(
                rows
            ):
                raise ValueError("Duplicate member")
            if sorted(z.namelist()) != sorted(["manifest.json"] + names):
                raise ValueError("Unexpected or duplicate archive entries")
            for r in rows:
                j = self.db.execute(
                    "SELECT url,rel,kind,source,node FROM jobs WHERE id=?", (r["id"],)
                ).fetchone()
                if (
                    not j
                    or j["url"] != r["url"]
                    or j["rel"] != r["rel"]
                    or j["kind"] != "laz"
                    or j["source"] != r["source"]
                    or j["node"] != r["node"]
                ):
                    raise ValueError("Manifest/job identity mismatch")
                entry = z.getinfo(r["rel"])
                if (
                    entry.compress_type != zipfile.ZIP_STORED
                    or not 0 < entry.file_size <= MAX_FILE
                    or entry.file_size != r["size"]
                ):
                    raise ValueError("Invalid member size or encoding")
                body = z.read(entry)
                if hashlib.sha256(body).hexdigest() != r["sha256"]:
                    raise ValueError("Member hash mismatch")
                if decode and decoded(body) != r["points"]:
                    raise ValueError("Member point mismatch")
        return rows

    def index(self, path, rows):
        rel = str(path.relative_to(self.root))
        checksum = self._hash(path)
        with self.db:
            for r in rows:
                old = self.db.execute(
                    "SELECT pack FROM pack_members WHERE job_id=?", (r["id"],)
                ).fetchone()
                if old and old["pack"] != rel:
                    raise ValueError("Job already indexed in another pack")
                expected = self.db.execute(
                    "SELECT points FROM jobs WHERE id=?", (r["id"],)
                ).fetchone()[0]
                if expected is not None and expected != r["points"]:
                    self.db.execute(
                        "INSERT OR IGNORE INTO discrepancies VALUES(?,?,?,?,?)",
                        (
                            r["source"],
                            r["node"],
                            "hierarchy_vs_decoded_count",
                            str(expected),
                            str(r["points"]),
                        ),
                    )
                self.db.execute(
                    "INSERT OR REPLACE INTO pack_members VALUES(?,?,?)",
                    (r["id"], rel, r["rel"]),
                )
                self.db.execute(
                    "UPDATE jobs SET state='done',size=?,sha256=?,points=?,etag=?,modified=?,error=NULL,attempts=attempts+1 WHERE id=?",
                    (
                        r["size"],
                        r["sha256"],
                        r["points"],
                        r.get("etag"),
                        r.get("modified"),
                        r["id"],
                    ),
                )
            self.db.execute("INSERT OR REPLACE INTO packs VALUES(?,?)", (rel, checksum))

    def commit(self, items):
        if not 1 <= len(items) <= MAX_MEMBERS:
            raise ValueError("Pack member limit")
        rows = []
        for job, body, headers in items:
            if not 0 < len(body) <= MAX_FILE:
                raise ValueError("Member size limit")
            row = {k: job[k] for k in ("id", "url", "rel", "source", "node")}
            safe_rel(row["rel"])
            row.update(
                size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                points=decoded(body),
                etag=headers.get("ETag"),
                modified=headers.get("Last-Modified"),
            )
            rows.append(row)
        if len({r["rel"] for r in rows}) != len(rows):
            raise ValueError("Duplicate member")
        manifest = json.dumps({"version": 1, "members": rows}, sort_keys=True).encode()
        self.space(
            sum(len(body) for _, body, _ in items) + len(manifest) + 65536, consume=True
        )
        path = self.folder / (uuid.uuid4().hex + ".zip")
        part = Path(str(path) + ".part")
        with part.open("xb") as f:
            with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_STORED) as z:
                z.writestr("manifest.json", manifest)
                for row, (_, body, _) in zip(rows, items):
                    z.writestr(row["rel"], body)
            f.flush()
            self.hook("before_fsync")
            os.fsync(f.fileno())
        self.hook("after_fsync")
        self.inspect(part, decode=False)
        os.replace(part, path)
        self.hook("after_rename")
        self.index(path, rows)
        self.hook("after_commit")
        return path

    def invalidate(self, relative):
        path = self.root / relative
        if path.exists():
            os.replace(path, Path(str(path) + ".invalid-" + uuid.uuid4().hex))
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='pending',error='Pack unavailable; reacquisition required' WHERE id IN (SELECT job_id FROM pack_members WHERE pack=?)",
                (relative,),
            )
            self.db.execute("DELETE FROM pack_members WHERE pack=?", (relative,))
            self.db.execute("DELETE FROM packs WHERE path=?", (relative,))

    def recover(self):
        recovered = []
        quarantined = []
        known = {r[0] for r in self.db.execute("SELECT path FROM packs")}
        # The writer lock excludes other acquisition writers during recovery.
        # Fetch the failed-pack set once instead of taking a remote SQLite lock
        # for each of potentially hundreds of thousands of packs.
        failed_packs = {
            r[0]
            for r in self.db.execute(
                "SELECT DISTINCT pack_members.pack FROM jobs JOIN pack_members ON jobs.id=pack_members.job_id WHERE jobs.state='failed'"
            )
        }
        total = len(known)
        print(json.dumps(dict(event="pack_recovery_started", packs=total)), flush=True)
        # CIFS supplies file type in directory entries. Reuse this listing
        # instead of issuing a separate remote stat for every known pack.
        with os.scandir(self.folder) as entries:
            files = {
                str((self.folder / e.name).relative_to(self.root)): self.folder / e.name
                for e in entries
                if e.is_file()
            }
        for checked, relative in enumerate(list(known), 1):
            if relative not in files or relative in failed_packs:
                self.invalidate(relative)
                known.remove(relative)
                files.pop(relative, None)
            if checked % 10000 == 0:
                print(
                    json.dumps(
                        dict(
                            event="pack_recovery_progress", checked=checked, total=total
                        )
                    ),
                    flush=True,
                )
        for p in sorted(files.values()):
            if not (p.name.endswith(".zip") or p.name.endswith(".zip.part")):
                continue
            if str(p.relative_to(self.root)) in known:
                continue
            try:
                rows = self.inspect(p)
            except Exception as e:
                if isinstance(e, (OSError, sqlite3.Error)):
                    raise
                target = Path(str(p) + ".invalid-" + uuid.uuid4().hex)
                os.replace(p, target)
                quarantined.append((str(target), str(e)))
                continue
            if p.name.endswith(".part"):
                dest = Path(str(p)[:-5])
                os.replace(p, dest)
                p = dest
            self.index(p, rows)
            recovered.append(str(p))
        return recovered, quarantined

    def verify(self, j):
        mapping = self.db.execute(
            "SELECT pack,member FROM pack_members WHERE job_id=?", (j["id"],)
        ).fetchone()
        if not mapping:
            p = self.root / j["rel"]
            return (
                p.is_file()
                and p.stat().st_size == j["size"]
                and self._hash(p) == j["sha256"]
            )
        try:
            with zipfile.ZipFile(self.root / mapping["pack"]) as z:
                entry = z.getinfo(mapping["member"])
                if entry.file_size != j["size"] or entry.file_size > MAX_FILE:
                    return False
                return hashlib.sha256(z.read(entry)).hexdigest() == j["sha256"]
        except (FileNotFoundError, KeyError, ValueError, zipfile.BadZipFile, EOFError):
            return False

    def read(self, j):
        if not self.verify(j):
            raise ValueError("Stored payload failed integrity check")
        mapping = self.db.execute(
            "SELECT pack,member FROM pack_members WHERE job_id=?", (j["id"],)
        ).fetchone()
        if mapping:
            with zipfile.ZipFile(self.root / mapping["pack"]) as z:
                return z.read(mapping["member"])
        return (self.root / j["rel"]).read_bytes()
