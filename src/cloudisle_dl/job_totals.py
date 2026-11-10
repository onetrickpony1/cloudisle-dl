"""Exact status totals updated atomically with each job transaction."""


def install(db):
    # A single writer is already enforced by the acquisition writer lock.
    # The initial scan and trigger installation commit together, so crashes
    # cannot expose partially initialized totals.
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_totals_v1'"
    ).fetchone():
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "CREATE TABLE job_totals_v1(state TEXT PRIMARY KEY,n INTEGER NOT NULL,payload_bytes INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO job_totals_v1 SELECT state,COUNT(*),COALESCE(SUM(CASE WHEN state='done' AND kind IN ('laz','zip') THEN COALESCE(size,0) ELSE 0 END),0) FROM jobs NOT INDEXED GROUP BY state"
        )
        new_bytes = "CASE WHEN NEW.state='done' AND NEW.kind IN ('laz','zip') THEN COALESCE(NEW.size,0) ELSE 0 END"
        old_bytes = "CASE WHEN OLD.state='done' AND OLD.kind IN ('laz','zip') THEN COALESCE(OLD.size,0) ELSE 0 END"
        add = f"INSERT INTO job_totals_v1 VALUES(NEW.state,1,{new_bytes}) ON CONFLICT(state) DO UPDATE SET n=n+1,payload_bytes=payload_bytes+excluded.payload_bytes;"
        remove = f"UPDATE job_totals_v1 SET n=n-1,payload_bytes=payload_bytes-({old_bytes}) WHERE state IS OLD.state;"
        db.execute(
            f"CREATE TRIGGER job_totals_insert_v1 AFTER INSERT ON jobs BEGIN {add} END"
        )
        db.execute(
            f"CREATE TRIGGER job_totals_delete_v1 AFTER DELETE ON jobs BEGIN {remove} END"
        )
        db.execute(
            f"CREATE TRIGGER job_totals_update_v1 AFTER UPDATE OF state,kind,size ON jobs WHEN OLD.state IS NOT NEW.state OR OLD.kind IS NOT NEW.kind OR OLD.size IS NOT NEW.size BEGIN {remove} {add} END"
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def read(db):
    rows = db.execute(
        "SELECT state,n,payload_bytes FROM job_totals_v1 WHERE n>0"
    ).fetchall()
    return {r[0]: r[1] for r in rows}, sum(r[2] for r in rows)
