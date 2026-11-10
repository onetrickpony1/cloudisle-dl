from cloudisle_dl.platform_support import db_uri
import os, sqlite3, tempfile, unittest, random, time, json
from pathlib import Path
from unittest.mock import patch
from cloudisle_dl import job_totals, archive_base as base

ROOT = Path(os.environ.get("PACK_TEST_ROOT", tempfile.gettempdir()))


class TotalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="totals-", dir=ROOT)
        self.path = Path(self.tmp.name) / "state.sqlite"
        self.db = sqlite3.connect(db_uri(self.path), uri=True)
        self.db.execute(
            "CREATE TABLE jobs(id INTEGER PRIMARY KEY,url TEXT UNIQUE,state TEXT,kind TEXT,size INTEGER)"
        )
        self.db.executemany(
            "INSERT INTO jobs VALUES(?,?,?,?,?)",
            [
                (1, "a", "done", "laz", 100),
                (2, "b", "done", "zip", 200),
                (3, "c", "pending", "laz", 300),
                (4, "d", "done", "meta", 400),
            ],
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def check(self):
        expected = dict(
            self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state")
        )
        size = self.db.execute(
            "SELECT COALESCE(SUM(size),0) FROM jobs WHERE state='done' AND kind IN ('laz','zip')"
        ).fetchone()[0]
        self.assertEqual(job_totals.read(self.db), (expected, size))

    def test_migration_updates_and_rollback(self):
        job_totals.install(self.db)
        self.check()
        rng = random.Random(17)
        for i in range(80):
            self.db.execute(
                "UPDATE jobs SET state=?,kind=?,size=? WHERE id=?",
                (
                    rng.choice(["pending", "done", "failed"]),
                    rng.choice(["laz", "zip", "meta"]),
                    rng.choice([None, 0, 500, 123456]),
                    rng.randint(1, 4),
                ),
            )
            self.check()
            if i % 3:
                self.db.commit()
            else:
                self.db.rollback()
            self.check()
        self.db.execute("INSERT OR IGNORE INTO jobs VALUES(5,'a','done','laz',999)")
        self.check()
        self.db.execute("INSERT INTO jobs VALUES(5,'e','done','laz',999)")
        self.check()
        self.db.execute("DELETE FROM jobs WHERE id IN (1,2,5)")
        self.check()
        self.db.rollback()
        self.check()

    def test_reopen_and_idempotent_install(self):
        job_totals.install(self.db)
        self.db.close()
        self.db = sqlite3.connect(db_uri(self.path), uri=True)

        def auth(action, table, *args):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "jobs"
                else sqlite3.SQLITE_OK
            )

        self.db.set_authorizer(auth)
        job_totals.install(self.db)
        self.assertEqual(job_totals.read(self.db), ({"done": 3, "pending": 1}, 300))
        self.db.set_authorizer(None)

    def test_failed_install_is_atomic(self):
        self.db.set_authorizer(
            lambda action, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_TRIGGER
                else sqlite3.SQLITE_OK
            )
        )
        with self.assertRaises(sqlite3.DatabaseError):
            job_totals.install(self.db)
        self.db.set_authorizer(None)
        self.assertIsNone(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='job_totals_v1'"
            ).fetchone()
        )
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 4)
        job_totals.install(self.db)
        self.check()


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="report-perf-", dir=ROOT)
        p = Path(self.tmp.name)
        self.r = base.Runner(p / "data", p / "state")
        self.r.space_guard.mounted = lambda _: True
        self.r.space_guard.reserve = 0

    def tearDown(self):
        self.r.close_network()
        self.r.db.close()
        self.r.events.close()
        self.tmp.cleanup()

    def test_report_does_not_scan_jobs(self):
        self.r.last_backup = time.monotonic()
        self.r.db.set_authorizer(
            lambda action, table, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "jobs"
                else sqlite3.SQLITE_OK
            )
        )
        self.r.report(True)
        self.assertEqual(
            json.loads((self.r.state / "status.json").read_text())[
                "verified_payload_bytes"
            ],
            0,
        )
        self.r.db.set_authorizer(None)

    def test_slow_backup_interval_starts_after_completion(self):
        clock = [30000.0]
        real = self.r.db
        calls = []

        class Proxy:
            def __getattr__(self, name):
                return getattr(real, name)

            def backup(self, out):
                calls.append(1)
                real.backup(out)
                clock[0] += 30000

        self.r.db = Proxy()
        with patch.object(base.time, "monotonic", side_effect=lambda: clock[0]):
            self.r.report(True)
            self.assertEqual(self.r.last_backup, 60000)
            clock[0] += 1
            self.r.report(True)
            self.assertEqual(len(calls), 1)
            clock[0] += 21601
            self.r.report(True)
            self.assertEqual(len(calls), 2)

    def test_terminal_report_forces_snapshot(self):
        self.r.last_backup = time.monotonic()
        self.r.report(True, "finished")
        self.assertTrue((self.r.dest / "control/archive.sqlite").is_file())

    def test_first_snapshot_on_recent_boot(self):
        with patch.object(base.time, "monotonic", return_value=100):
            self.r.report(True)
        self.assertTrue((self.r.dest / "control/archive.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
