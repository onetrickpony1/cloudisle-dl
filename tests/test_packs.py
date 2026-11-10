from cloudisle_dl.platform_support import db_uri
import contextlib
import os, sys, tempfile, unittest, sqlite3, io, json, zipfile, hashlib, subprocess
from pathlib import Path
from unittest.mock import patch
import laspy, numpy as np
from cloudisle_dl import archive_base as base
from cloudisle_dl.pack_store import PackStore, MAX_FILE

ROOT = Path(os.environ.get("PACK_TEST_ROOT", tempfile.gettempdir()))


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pack-safety-", dir=ROOT)
        self.root = Path(self.tmp.name)
        self.r = base.Runner(self.root / "data", self.root / "state", batch_size=20)
        self.r.space_guard.mounted = lambda _: True
        self.r.space_guard.reserve = 0
        self.store = PackStore(self.r.dest, self.r.db, self.r.space)
        las = laspy.LasData(laspy.LasHeader(point_format=2, version="1.2"))
        las.x = np.arange(100)
        las.y = np.arange(100)
        las.z = np.arange(100)
        buf = io.BytesIO()
        las.write(buf, do_compress=True)
        self.body = buf.getvalue()
        for i in range(3):
            self.r.db.execute(
                "INSERT INTO jobs(url,rel,kind,source,node,points,priority) VALUES(?,?,'laz','s',?,100,0)",
                (f"https://example.test/{i}", f"pointclouds/s/{i}.laz", str(i)),
            )
        self.r.db.execute(
            "INSERT INTO sources(name,expected,step,metadata_status) VALUES('s',300,5,'ok')"
        )
        for i in range(3):
            self.r.db.execute("INSERT INTO nodes VALUES('s',?,0,100)", (str(i),))
        self.r.db.commit()
        self.jobs = [dict(j) for j in self.r.db.execute("select * from jobs")]

    def tearDown(self):
        self.r.close_network()
        self.r.db.close()
        self.r.events.close()
        self.tmp.cleanup()

    def items(self, n=3):
        return [(j, self.body, {"ETag": '"v1"'}) for j in self.jobs[:n]]

    def done(self):
        return self.r.db.execute(
            "select count(*) from jobs where state='done'"
        ).fetchone()[0]

    def row(self, i=1):
        return self.r.db.execute("select * from jobs where id=?", (i,)).fetchone()

    def rewrite(self, p, mutate):
        with zipfile.ZipFile(p) as z:
            contents = {x: z.read(x) for x in z.namelist()}
        mutate(contents)
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as z:
            for name, body in contents.items():
                z.writestr(name, body)

    def orphan(self):
        self.store.hook = lambda stage: (
            (_ for _ in ()).throw(RuntimeError("crash"))
            if stage == "after_rename"
            else None
        )
        with self.assertRaises(RuntimeError):
            self.store.commit(self.items())
        return next(self.store.folder.glob("*.zip"))

    def test_roundtrip_crc_hash_points_index(self):
        p = self.store.commit(self.items())
        self.assertEqual(self.done(), 3)
        self.assertEqual(len(self.store.inspect(p)), 3)
        for i in range(1, 4):
            self.assertEqual(self.store.read(self.row(i)), self.body)

    def test_no_done_before_durable_pack(self):
        def hook(stage):
            if stage != "after_commit":
                self.assertEqual(self.done(), 0)

        self.store.hook = hook
        self.store.commit(self.items())
        self.assertEqual(self.done(), 3)

    def test_recovery_idempotent(self):
        self.orphan()
        self.assertEqual(self.done(), 0)
        self.assertEqual(len(self.store.recover()[0]), 1)
        self.assertEqual(self.done(), 3)
        self.assertEqual(self.store.recover(), ([], []))

    def test_incomplete_pack_quarantined(self):
        p = self.store.folder / "broken.zip.part"
        p.write_bytes(b"PK incomplete")
        recovered, bad = self.store.recover()
        self.assertEqual(len(bad), 1)
        self.assertEqual(self.done(), 0)
        self.assertFalse(p.exists())

    def test_corrupt_orphan_quarantined(self):
        p = self.orphan()
        self.rewrite(p, lambda c: c.update({"pointclouds/s/0.laz": b"bad"}))
        self.assertEqual(len(self.store.recover()[1]), 1)
        self.assertEqual(self.done(), 0)

    def test_path_traversal_rejected(self):
        j = dict(self.jobs[0], rel="../escape")
        with self.assertRaises(ValueError):
            self.store.commit([(j, self.body, {})])
        self.assertEqual(self.done(), 0)

    def test_identity_mismatch_rejected(self):
        p = self.orphan()

        def mutate(c):
            m = json.loads(c["manifest.json"])
            m["members"][0]["url"] = "https://wrong.test/"
            c["manifest.json"] = json.dumps(m).encode()

        self.rewrite(p, mutate)
        self.assertEqual(len(self.store.recover()[1]), 1)

    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            self.store.commit([self.items()[0]] * 2)

    def test_bound_member_count(self):
        with self.assertRaises(ValueError):
            self.store.commit([self.items()[0]] * 21)

    def test_bound_member_size(self):
        with self.assertRaises(ValueError):
            self.store.commit([(self.jobs[0], b"x" * (MAX_FILE + 1), {})])

    def test_invalid_laz_not_committed(self):
        with self.assertRaises(Exception):
            self.store.commit([(self.jobs[0], b"notlas", {})])
        self.assertEqual(self.done(), 0)
        self.assertEqual(list(self.store.folder.iterdir()), [])

    def test_low_space_no_pack(self):
        with patch.object(self.store, "space", side_effect=RuntimeError("reserve")):
            with self.assertRaises(RuntimeError):
                self.store.commit(self.items())
        self.assertEqual(list(self.store.folder.iterdir()), [])

    def test_fsync_failure_no_done(self):
        with patch("cloudisle_dl.pack_store.os.fsync", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                self.store.commit(self.items())
        self.assertEqual(self.done(), 0)

    def test_rename_failure_no_done(self):
        with patch("cloudisle_dl.pack_store.os.replace", side_effect=OSError(5, "io")):
            with self.assertRaises(OSError):
                self.store.commit(self.items())
        self.assertEqual(self.done(), 0)

    def test_db_failure_recovers_pack(self):
        self.r.db.execute(
            "CREATE TRIGGER fail BEFORE UPDATE ON jobs BEGIN SELECT RAISE(ABORT,'fail'); END"
        )
        self.r.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit(self.items())
        self.assertEqual(self.done(), 0)
        self.assertEqual(
            self.r.db.execute("select count(*) from pack_members").fetchone()[0], 0
        )
        self.r.db.execute("DROP TRIGGER fail")
        self.r.db.commit()
        self.store.recover()
        self.assertEqual(self.done(), 3)

    def test_missing_pack_detected(self):
        p = self.store.commit(self.items())
        p.rename(p.with_suffix(".removed"))
        self.assertFalse(self.store.verify(self.row()))

    def test_corrupt_indexed_pack_detected(self):
        p = self.store.commit(self.items())
        self.rewrite(p, lambda c: c.update({"pointclouds/s/0.laz": b"wrong"}))
        self.assertFalse(self.store.verify(self.row()))

    def test_point_discrepancy_retained(self):
        self.r.db.execute("update jobs set points=99")
        self.r.db.commit()
        self.store.commit(self.items())
        self.assertEqual(
            self.r.db.execute("select count(*) from discrepancies").fetchone()[0], 3
        )

    def test_raw_and_packed_audit_report(self):
        self.store.commit(self.items(2))
        j = self.jobs[2]
        p = self.r.dest / j["rel"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.body)
        self.r.complete_laz(j, p, 100)
        self.r.flush_completions()
        self.r.verify_job = self.store.verify
        self.r.report = lambda *a, **kw: None
        self.r.final_audit()
        self.assertEqual(self.done(), 3)
        from cloudisle_dl import report_packed as report_archive

        result = report_archive.generate(
            self.r.dest, self.r.state, self.root / "reports"
        )
        self.assertEqual(result["complete_sources"], 1)
        self.assertEqual(result["payload_files"], 3)

    def crash_case(self, stage):
        (self.root / "fixture.laz").write_bytes(self.body)
        self.r.db.close()
        code = """import sys,os,json
from pathlib import Path
from cloudisle_dl import archive_base as base
from cloudisle_dl.pack_store import PackStore
root=Path(sys.argv[1]);r=base.Runner(root/'data',root/'state',batch_size=20)
r.space_guard.mounted=lambda _:True;r.space_guard.reserve=0
s=PackStore(r.dest,r.db,r.space);s.hook=lambda stage:os._exit(73) if stage==sys.argv[2] else None
body=(root/'fixture.laz').read_bytes();jobs=[dict(j) for j in r.db.execute('select * from jobs')]
s.commit([(j,body,{}) for j in jobs])
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src"))
        p = subprocess.run(
            [sys.executable, "-c", code, str(self.root), stage],
            env=env,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 73, p.stderr.decode())
        self.r.db = sqlite3.connect(db_uri(self.r.state / "archive.sqlite"), uri=True)
        self.r.db.row_factory = sqlite3.Row
        self.store = PackStore(self.r.dest, self.r.db, self.r.space)
        self.store.recover()
        self.assertEqual(self.done(), 3)
        for i in range(1, 4):
            self.assertTrue(self.store.verify(self.row(i)))

    def test_process_death_before_fsync(self):
        self.crash_case("before_fsync")

    def test_process_death_after_fsync(self):
        self.crash_case("after_fsync")

    def test_process_death_after_rename(self):
        self.crash_case("after_rename")

    def test_process_death_after_commit(self):
        self.crash_case("after_commit")

    def test_missing_pack_requeues_members(self):
        p = self.store.commit(self.items())
        p.rename(p.with_suffix(".removed"))
        self.store.recover()
        self.assertEqual(self.done(), 0)
        self.assertEqual(
            self.r.db.execute("select count(*) from pack_members").fetchone()[0], 0
        )
        self.store.commit(self.items())
        self.assertEqual(self.done(), 3)

    def test_recovery_failed_set_preserves_other_packs(self):
        first = self.store.commit(self.items(1))
        second = self.store.commit(self.items()[1:])
        self.r.db.execute("UPDATE jobs SET state='failed' WHERE id=1")
        self.r.db.commit()
        self.store.recover()
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.row(1)["state"], "pending")
        self.assertEqual(self.done(), 2)
        self.assertTrue(self.store.verify(self.row(2)))
        self.assertTrue(self.store.verify(self.row(3)))

    def test_corrupt_pack_audit_then_repair(self):
        p = self.store.commit(self.items())
        self.rewrite(p, lambda c: c.update({"pointclouds/s/0.laz": b"wrong"}))
        self.r.verify_job = self.store.verify
        self.r.report = lambda *a, **k: None
        self.r.final_audit()
        self.assertEqual(self.done(), 2)
        self.store.recover()
        self.assertEqual(self.done(), 0)
        self.store.commit(self.items())
        self.assertEqual(self.done(), 3)

    def test_backup_retains_index(self):
        self.store.commit(self.items())
        (self.r.dest / "control").mkdir(exist_ok=True)
        self.r.report(True)
        with contextlib.closing(
            sqlite3.connect(db_uri(self.r.dest / "control/archive.sqlite"), uri=True)
        ) as db:
            self.assertEqual(
                db.execute("select count(*) from pack_members").fetchone()[0], 3
            )

    def test_reader_and_packed_report(self):
        from cloudisle_dl import read_payload, report_packed

        self.store.commit(self.items())
        self.r.verify_job = self.store.verify
        self.r.report = lambda *a, **k: None
        self.r.final_audit()
        j, body = read_payload.read(self.r.dest, self.r.state, 1)
        self.assertEqual(body, self.body)
        report_packed.generate(self.r.dest, self.r.state, self.root / "packed-report")
        self.assertIn(
            "Packed LAZ members: **3**",
            (self.root / "packed-report/final-download-report.md").read_text(),
        )

    def test_invalid_laz_orphan_is_quarantined(self):
        p = self.orphan()

        def mutate(c):
            body = b"invalidlas"
            c["pointclouds/s/0.laz"] = body
            m = json.loads(c["manifest.json"])
            m["members"][0].update(
                size=len(body), sha256=hashlib.sha256(body).hexdigest()
            )
            c["manifest.json"] = json.dumps(m).encode()

        self.rewrite(p, mutate)
        self.assertEqual(len(self.store.recover()[1]), 1)
        self.assertEqual(self.done(), 0)

    def test_recovery_io_error_does_not_quarantine_good_data(self):
        p = self.orphan()
        with patch.object(self.store, "inspect", side_effect=OSError(5, "io")):
            with self.assertRaises(OSError):
                self.store.recover()
        self.assertTrue(p.exists())
        self.assertEqual(self.done(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
