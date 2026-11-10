"""Read-only retrieval for a mixed archive; optional explicit single-file export."""

import argparse, hashlib, sqlite3, zipfile
from pathlib import Path
from .platform_support import db_uri


def read(destination, state, job_id):
    dest = Path(destination)
    db = sqlite3.connect(
        db_uri(Path(state) / "archive.sqlite", readonly=True), uri=True
    )
    db.row_factory = sqlite3.Row
    try:
        j = db.execute(
            "SELECT * FROM jobs WHERE id=? AND state='done'", (job_id,)
        ).fetchone()
        if not j:
            raise ValueError("Job is not verified/done")
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='pack_members'"
        ).fetchone()
        m = (
            db.execute(
                "SELECT pack,member FROM pack_members WHERE job_id=?", (job_id,)
            ).fetchone()
            if table
            else None
        )
        if m:
            with zipfile.ZipFile(dest / m["pack"]) as z:
                if z.getinfo(m["member"]).file_size != j["size"]:
                    raise ValueError("Member size mismatch")
                body = z.read(m["member"])
        else:
            body = (dest / j["rel"]).read_bytes()
        if len(body) != j["size"] or hashlib.sha256(body).hexdigest() != j["sha256"]:
            raise ValueError("Integrity failure")
        return dict(j), body
    finally:
        db.close()
