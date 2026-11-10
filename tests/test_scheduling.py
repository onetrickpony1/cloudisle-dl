from cloudisle_dl.platform_support import db_uri
import sqlite3, unittest
from cloudisle_dl.archive_base import Runner


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.r = object.__new__(Runner)
        self.r.db = sqlite3.connect(":memory:")
        self.r.db.row_factory = sqlite3.Row
        self.r.db.executescript(
            "CREATE TABLE jobs(id INTEGER PRIMARY KEY,state DEFAULT 'pending',priority,kind,source);CREATE INDEX job_queue ON jobs(state,priority,id);"
        )

    def tearDown(self):
        self.r.db.close()

    def add(self, p, k, s="s", state="pending"):
        return self.r.db.execute(
            "INSERT INTO jobs(priority,kind,source,state) VALUES(?,?,?,?)",
            (p, k, s, state),
        ).lastrowid

    def test_alternation(self):
        a = self.add(10, "dir")
        b = self.add(11, "laz")
        self.assertEqual(
            [self.r.select_job()["id"] for _ in range(6)], [b, a, b, a, b, a]
        )

    def test_hierarchy_alternation(self):
        a = self.add(10, "hrc")
        b = self.add(11, "laz")
        self.assertEqual(self.r.select_job()["id"], b)
        self.assertEqual(self.r.select_job()["id"], a)

    def test_no_payload(self):
        a = self.add(10, "dir")
        self.assertEqual(self.r.select_job()["id"], a)

    def test_no_cross_source_or_metadata_jump(self):
        a = self.add(10, "dir")
        self.add(11, "laz", "other")
        self.assertEqual(self.r.select_job()["id"], a)

    def test_control_is_serviced(self):
        self.add(10, "dir")
        b = self.add(11, "control")
        self.assertEqual(self.r.select_job()["id"], b)

    def test_excludes_buffered_and_done(self):
        a = self.add(10, "dir")
        b = self.add(11, "laz")
        self.add(11, "laz", state="done")
        c = self.add(11, "laz")
        self.assertEqual(self.r.select_job((b,))["id"], c)

    def test_preserves_top_level_priority(self):
        a = self.add(0, "zip")
        self.add(10, "dir")
        self.add(11, "laz")
        self.assertEqual(self.r.select_job()["id"], a)

    def test_fallback_hierarchy_does_not_block_payload_turn(self):
        a = self.add(10, "dir")
        b = self.add(11, "hrc")
        c = self.add(11, "laz")
        self.assertEqual(self.r.select_job()["id"], b)
        self.r.db.execute("UPDATE jobs SET state='done' WHERE id=?", (b,))
        self.assertEqual(self.r.select_job()["id"], a)
        self.assertEqual(self.r.select_job()["id"], c)

    def test_empty(self):
        self.assertIsNone(self.r.select_job())


if __name__ == "__main__":
    unittest.main()
