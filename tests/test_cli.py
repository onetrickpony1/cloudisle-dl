import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from cloudisle_dl import cli
from cloudisle_dl.archive_base import Runner
from cloudisle_dl.platform_support import file_lock


class CLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "config"
        self.destination = self.root / "archive"

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *args):
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            cli.main(["--config-dir", str(self.home), *args])
        return capture.getvalue()

    def test_init_status_no_download(self):
        self.invoke("init", "--destination", str(self.destination), "--allow-local")
        self.assertFalse(self.destination.exists())
        data = json.loads(self.invoke("status", "--json"))
        self.assertEqual(data["phase"], "not started")
        with self.assertRaises(SystemExit):
            self.invoke("init", "--destination", str(self.destination))

    def test_external_status_reads_only_json(self):
        state = self.destination / "control/acquisition-state"
        state.mkdir(parents=True)
        (state / "status.json").write_text(
            json.dumps(
                {"phase": "running", "counts": {"done": 7}, "verified_payload_bytes": 9}
            )
        )
        self.assertEqual(
            json.loads(
                self.invoke("status", "--destination", str(self.destination), "--json")
            )["counts"]["done"],
            7,
        )
        self.assertFalse(self.home.exists())

    def test_mount_guard(self):
        with patch.object(cli.os.path, "ismount", return_value=False):
            with self.assertRaises(RuntimeError):
                cli.storage_check(
                    {
                        "destination": str(self.destination),
                        "state": str(self.destination / "control"),
                        "allow_local": False,
                    }
                )

    def test_cross_process_lock(self):
        p = self.root / "lock"
        code = "from cloudisle_dl.platform_support import file_lock; import sys;\nwith file_lock(sys.argv[1]): pass"
        with file_lock(p):
            result = subprocess.run(
                [sys.executable, "-c", code, str(p)], capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            subprocess.run(
                [sys.executable, "-c", code, str(p)], capture_output=True
            ).returncode,
            0,
        )

    def test_detached_start_status_stop_resume(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"test")

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.invoke(
            "init",
            "--destination",
            str(self.destination),
            "--allow-local",
            "--reserve-gib",
            "0.001",
            "--requests-per-second",
            "10",
        )
        state = self.destination / "control/acquisition-state"
        runner = Runner(self.destination, state)
        runner.db.execute("INSERT INTO settings VALUES('discovered','1')")
        for n in range(100):
            runner.db.execute(
                "INSERT INTO jobs(url,rel,kind,priority) VALUES(?,?,'control',1)",
                (f"http://127.0.0.1:{server.server_port}/{n}", f"controls/{n}"),
            )
        runner.db.commit()
        runner.close_network()
        runner.db.close()
        runner.events.close()
        try:
            self.invoke("start")
            deadline = time.monotonic() + 2
            status = {}
            while time.monotonic() < deadline:
                if (state / "status.json").exists():
                    status = json.loads((state / "status.json").read_text())
                    if status.get("http_reuse", {}).get("requests", 0) > 0:
                        break
                time.sleep(0.2)
            # Events/control show a live detached worker even before the next 30s report.
            self.assertIsNotNone(cli.control(self.home))
            self.invoke("stop")
            self.assertIsNone(cli.control(self.home))
            status = json.loads((state / "status.json").read_text())
            self.assertGreater(status["counts"].get("done", 0), 0)
            done = status["counts"]["done"]
            self.invoke("resume")
            time.sleep(1)
            self.invoke("stop")
            status = json.loads((state / "status.json").read_text())
            self.assertGreaterEqual(status["counts"].get("done", 0), done)
            self.assertEqual(status["phase"], "stopped")
        finally:
            if cli.control(self.home):
                self.invoke("stop")
            server.shutdown()
            server.server_close()
            thread.join()
