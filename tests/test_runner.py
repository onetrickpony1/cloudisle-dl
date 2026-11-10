import io, json, os, tempfile, threading, http.server, unittest, time, hashlib
from pathlib import Path
from unittest.mock import patch
import laspy, numpy as np
from cloudisle_dl import archive_base as base
from cloudisle_dl import archive_packed as packed

ROOT = Path(os.environ.get("PACK_TEST_ROOT", tempfile.gettempdir()))


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        with self.server.lock:
            self.server.requests.append(
                (self.path, time.monotonic(), self.headers.get("Range"))
            )
        mode = self.server.modes.get(self.path, "normal")
        body = self.server.body
        if mode == "throttle":
            self.send_response(429)
            self.send_header("Retry-After", "901")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "large":
            self.send_response(200)
            self.send_header("Content-Length", 9 * 1024**2)
            self.send_header("Connection", "close")
            self.end_headers()
            return
        if mode == "invalid":
            body = b"invalid"
        offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
        self.send_response(206 if offset else 200)
        self.send_header("Content-Length", len(body) - offset)
        self.send_header("ETag", '"test-v1"')
        if offset:
            self.send_header(
                "Content-Range", f"bytes {offset}-{len(body) - 1}/{len(body)}"
            )
        if mode == "truncate":
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(
                body[offset : len(body) // 2] if mode == "truncate" else body[offset:]
            )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="runner-safety-", dir=ROOT)
        self.root = Path(self.tmp.name)
        las = laspy.LasData(laspy.LasHeader(point_format=2, version="1.2"))
        las.x = np.arange(100)
        las.y = np.arange(100)
        las.z = np.arange(100)
        buf = io.BytesIO()
        las.write(buf, do_compress=True)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.body = buf.getvalue()
        self.server.requests = []
        self.server.lock = threading.Lock()
        self.server.modes = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        self.patch = patch.object(base, "BASE", self.url)
        self.patch.start()
        self.r = packed.Runner(
            self.root / "data",
            self.root / "state",
            rps=30,
            batch_size=20,
            connections=2,
        )
        self.r.space_guard.mounted = lambda _: True
        self.r.space_guard.reserve = 0

    def tearDown(self):
        self.r.close_network()
        self.r.db.close()
        self.r.events.close()
        self.patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def seed(self, n):
        for i in range(n):
            self.r.db.execute(
                "INSERT INTO jobs(url,rel,kind,source,node,points,priority) VALUES(?,?,'laz','s',?,100,0)",
                (self.url + str(i), f"{i}.laz", str(i)),
            )
        self.r.db.commit()

    def job(self, i=1):
        return self.r.db.execute("select * from jobs where id=?", (i,)).fetchone()

    def done(self):
        return self.r.db.execute(
            "select count(*) from jobs where state='done'"
        ).fetchone()[0]

    def test_pack_limits_and_request_limit(self):
        self.seed(25)
        self.r.payload(self.job())
        self.assertEqual(self.done(), 20)
        self.assertEqual(len(self.server.requests), 20)
        times = sorted(t for _, t, _ in self.server.requests)
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 0.025)
        self.r.payload(self.job(21))
        self.assertEqual(self.done(), 25)
        self.assertEqual(
            self.r.db.execute("select count(*) from packs").fetchone()[0], 2
        )

    def test_truncated_current_keeps_other_successes(self):
        self.seed(3)
        self.server.modes["/0"] = "truncate"
        with self.assertRaises(Exception):
            self.r.payload(self.job())
        self.assertEqual(self.done(), 2)
        self.server.modes.clear()
        self.r.payload(self.job())
        self.assertEqual(self.done(), 3)

    def test_noncurrent_error_stays_pending(self):
        self.seed(3)
        self.server.modes["/1"] = "truncate"
        self.r.payload(self.job())
        self.assertEqual(self.done(), 2)
        self.assertEqual(self.job(2)["state"], "pending")

    def test_invalid_laz_never_marked_done(self):
        self.seed(2)
        self.server.modes["/0"] = "invalid"
        with self.assertRaises(Exception):
            self.r.payload(self.job())
        self.assertEqual(self.done(), 1)
        self.assertEqual(self.job()["state"], "pending")

    def test_existing_partial_uses_resume(self):
        self.seed(2)
        j = self.job()
        p = self.r.dest / "0.laz.part"
        p.write_bytes(self.server.body[:100])
        Path(str(p) + ".json").write_text(
            json.dumps(
                dict(
                    url=j["url"],
                    size=len(self.server.body),
                    etag='"test-v1"',
                    modified=None,
                )
            )
        )
        self.r.payload(j)
        self.r.flush_completions()
        self.assertEqual(self.done(), 1)
        self.assertEqual(self.server.requests[0][2], "bytes=100-")
        self.assertEqual(
            self.r.db.execute("select count(*) from packs").fetchone()[0], 0
        )

    def test_existing_final_uses_local_validation(self):
        self.seed(1)
        (self.r.dest / "0.laz").write_bytes(self.server.body)
        self.r.payload(self.job())
        self.r.flush_completions()
        self.assertEqual(self.done(), 1)
        self.assertEqual(self.server.requests, [])

    def test_large_not_buffered(self):
        self.seed(1)
        self.server.modes["/0"] = "large"
        self.assertIsNone(self.r.fetch_small(self.job()))

    def test_throttle_shared_and_shutdown_interrupts(self):
        self.seed(2)
        self.server.modes["/0"] = "throttle"
        with self.assertRaises(base.urllib.error.HTTPError):
            self.r.fetch_small(self.job())
        self.assertGreater(self.r.backoff_until - time.monotonic(), 900)
        future = self.r.pack_pool.submit(self.r.fetch_small, self.job(2))
        start = time.monotonic()
        self.r.close_network()
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(len(self.server.requests), 1)

    def test_run_does_not_repeat_packed_jobs(self):
        self.seed(24)
        with (
            patch.object(self.r, "discover"),
            patch.object(self.r, "report"),
            patch.object(self.r, "final_audit"),
        ):
            self.r.run()
        self.assertEqual(self.done(), 24)
        self.assertEqual(len(self.server.requests), 24)

    def test_disable_new_packing_keeps_old_pack_readable(self):
        self.seed(2)
        self.r.payload(self.job())
        self.r.pack_enabled = False
        self.r.db.execute(
            "INSERT INTO jobs(url,rel,kind,source,node,points,priority) VALUES(?,?,'laz','s','2',100,0)",
            (self.url + "2", "2.laz"),
        )
        self.r.db.commit()
        self.r.payload(self.job(3))
        self.r.flush_completions()
        self.assertTrue((self.r.dest / "2.laz").exists())
        self.assertEqual(self.done(), 3)
        self.assertEqual(self.r.store.read(self.job()), self.server.body)

    def test_interleaved_payload_still_packs_with_discovery_pending(self):
        self.seed(25)
        self.r.db.execute("UPDATE jobs SET priority=11")
        self.r.db.execute(
            "INSERT INTO jobs(url,rel,kind,source,priority) VALUES('dir','directory.html','dir','s',10)"
        )
        self.r.db.commit()
        j = self.r.select_job()
        self.assertEqual(j["kind"], "laz")
        self.r.payload(j)
        self.assertEqual(self.done(), 20)
        self.assertEqual(self.r.select_job()["kind"], "dir")
        j = self.r.select_job()
        self.assertEqual(j["id"], 21)
        self.r.payload(j)
        self.assertEqual(self.done(), 25)
        self.assertEqual(self.r.select_job()["kind"], "dir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
