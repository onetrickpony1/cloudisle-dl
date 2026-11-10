"""Cross-platform command line and authenticated local worker control."""

import argparse
import datetime
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time

from . import __version__
from .core import atomic
from .platform_support import file_lock


def config_home():
    override = os.environ.get("CLOUDISLE_DL_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            / "cloudisle-dl"
        )
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "cloudisle-dl"
    )


def read_config(home):
    path = home / "config.json"
    if not path.exists():
        raise RuntimeError("Run cloudisle-dl init --destination PATH first")
    return json.loads(path.read_text())


def control(home, action="status"):
    try:
        runtime = json.loads((home / "runtime.json").read_text())
        with socket.create_connection(
            ("127.0.0.1", runtime["port"]), timeout=2
        ) as sock:
            sock.sendall(
                (
                    json.dumps({"token": runtime["token"], "action": action}) + "\n"
                ).encode()
            )
            with sock.makefile("rb") as stream:
                reply = json.loads(stream.readline(4096))
        if reply.get("pid") == runtime["pid"] and reply.get("ok"):
            return reply
    except (OSError, ValueError, KeyError):
        pass
    return None


def storage_check(cfg):
    destination = Path(cfg["destination"])
    if not cfg["allow_local"] and not os.path.ismount(destination.parent):
        raise RuntimeError(
            "Destination parent is not mounted. Mount the share, or explicitly initialize with --allow-local for a local disk."
        )
    state = Path(cfg["state"])
    if not state.resolve().is_relative_to(destination.resolve()):
        raise RuntimeError("State must remain inside the download destination")
    destination.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)


def worker(home):
    from .archive_packed import Runner

    cfg = read_config(home)
    storage_check(cfg)
    stopped = threading.Event()
    ready = threading.Event()
    token = secrets.token_urlsafe(32)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                request = json.loads(self.rfile.readline(4096))
                if not hmac.compare_digest(str(request.get("token", "")), token):
                    return
                action = request.get("action")
                if action not in ("status", "stop"):
                    return
                self.wfile.write(
                    (
                        json.dumps(
                            {
                                "ok": True,
                                "pid": os.getpid(),
                                "phase": "running" if ready.is_set() else "starting",
                            }
                        )
                        + "\n"
                    ).encode()
                )
                self.wfile.flush()
                if action == "stop" and not stopped.is_set():
                    stopped.set()
                    if ready.is_set():
                        import _thread

                        _thread.interrupt_main()
            except (OSError, ValueError):
                return

    with file_lock(home / "worker.lock"), file_lock(Path(cfg["state"]) / "writer.lock"):
        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            runtime = home / "runtime.json"
            atomic(
                runtime,
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "port": server.server_address[1],
                        "token": token,
                    }
                ).encode(),
            )
            if os.name != "nt":
                runtime.chmod(0o600)
            runner = None
            try:
                runner = Runner(
                    cfg["destination"],
                    cfg["state"],
                    rps=cfg["requests_per_second"],
                    mbps=cfg["mib_per_second"],
                    connections=cfg["connections"],
                )
                runner.space_guard.reserve = cfg["reserve_gib"] * 1024**3
                if cfg["allow_local"]:
                    runner.space_guard.mounted = lambda _: True
                ready.set()
                if stopped.is_set():
                    raise KeyboardInterrupt
                runner.run()
            except KeyboardInterrupt:
                if runner:
                    runner.event("stopped")
                    runner.report(True, "stopped")
            except Exception as error:
                if runner:
                    runner.event("fatal", error=str(error))
                    runner.report(True, "blocked")
                raise
            finally:
                ready.clear()
                if runner:
                    runner.close_network()
                    runner.db.close()
                    runner.events.close()
                runtime.unlink(missing_ok=True)
                server.shutdown()
                thread.join()


def start(home):
    if control(home):
        print("Already running. Use cloudisle-dl status.")
        return
    cfg = read_config(home)
    storage_check(cfg)
    # Refuse to compete with any other downloader using this archive.
    with file_lock(Path(cfg["state"]) / "writer.lock"):
        pass
    log = Path(cfg["state"]) / "worker.log"
    kwargs = (
        {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with log.open("ab") as stream:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cloudisle_dl",
                "--config-dir",
                str(home),
                "_worker",
            ],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            close_fds=True,
            **kwargs,
        )
    for _ in range(100):
        if control(home):
            print(
                f"Background worker started (PID {process.pid}). Use cloudisle-dl status."
            )
            return
        if process.poll() is not None:
            raise RuntimeError(f"Worker exited; inspect {log}")
        time.sleep(0.1)
    print(
        f"Worker launched (PID {process.pid}); initializing. Use cloudisle-dl status or logs."
    )


def show_status(home, destination=None, as_json=False):
    cfg = (
        {
            "destination": str(destination),
            "state": str(Path(destination) / "control/acquisition-state"),
        }
        if destination
        else read_config(home)
    )
    path = Path(cfg["state"]) / "status.json"
    data = (
        json.loads(path.read_text())
        if path.exists()
        else {"phase": "not started", "counts": {}, "verified_payload_bytes": 0}
    )
    runtime = control(home) if not destination else None
    data = dict(data, managed_worker=runtime, destination=cfg["destination"])
    if as_json:
        print(json.dumps(data, indent=2))
        return
    print(f"Destination: {cfg['destination']}")
    print(
        "Managed worker: "
        + (
            f"{runtime['phase']} (PID {runtime['pid']})"
            if runtime
            else "not detected (an external worker may own this archive)"
        )
    )
    print(f"Saved phase: {data['phase']} | Updated: {data.get('updated', 'never')}")
    print(f"Validated payload: {data.get('verified_payload_bytes', 0) / 1e9:,.2f} GB")
    print(
        "Jobs: "
        + ", ".join(f"{key}={value:,}" for key, value in data.get("counts", {}).items())
    )
    if data.get("active"):
        print("Current: " + json.dumps(data["active"]))
    if "free_bytes" in data:
        print(f"Free disk: {data['free_bytes'] / 1e12:.2f} TB")
    print(
        "Queue counts are not a whole-survey completion percentage; discovery can add jobs."
    )


def positive(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be positive and finite")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cloudisle-dl", description="Resumable Cloudisle point-cloud downloader"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config-dir", type=Path, default=config_home())
    subs = parser.add_subparsers(dest="command", required=True)
    init = subs.add_parser("init", help="Configure a destination; does not download")
    init.add_argument("--destination", required=True, type=Path)
    init.add_argument(
        "--allow-local",
        action="store_true",
        help="Allow a local disk instead of a mounted share",
    )
    init.add_argument("--requests-per-second", type=positive, default=2)
    init.add_argument("--mib-per-second", type=positive, default=8)
    init.add_argument("--connections", type=int, choices=(1, 2), default=1)
    init.add_argument("--reserve-gib", type=positive, default=50)
    init.add_argument(
        "--force", action="store_true", help="Replace stopped-worker configuration"
    )
    for command, description in [
        ("start", "Start in background"),
        ("resume", "Resume in background"),
        ("stop", "Stop gracefully, keeping all data"),
        ("run", "Run in foreground"),
        ("_worker", "Internal background worker (use start instead)"),
    ]:
        subs.add_parser(command, help=description)
    status = subs.add_parser(
        "status", help="Show saved totals without scanning the database"
    )
    status.add_argument("--json", action="store_true")
    status.add_argument(
        "--destination",
        type=Path,
        help="Inspect an existing archive without configuring or controlling it",
    )
    logs = subs.add_parser("logs", help="Show recent worker logs")
    logs.add_argument("--lines", type=int, default=30)
    report = subs.add_parser(
        "report", help="Generate report after final audit completes"
    )
    report.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    home = args.config_dir.expanduser().resolve()
    try:
        if args.command == "init":
            if control(home):
                raise RuntimeError(
                    "Stop the managed worker before changing configuration"
                )
            if (home / "config.json").exists() and not args.force:
                raise RuntimeError(
                    "Configuration already exists; use --force to replace it"
                )
            destination = args.destination.expanduser().resolve()
            cfg = {
                key: getattr(args, key)
                for key in (
                    "allow_local",
                    "requests_per_second",
                    "mib_per_second",
                    "connections",
                    "reserve_gib",
                )
            }
            cfg.update(
                destination=str(destination),
                state=str(destination / "control/acquisition-state"),
            )
            atomic(home / "config.json", json.dumps(cfg, indent=2).encode())
            print("Configured. Run cloudisle-dl start when ready.")
        elif args.command in ("start", "resume"):
            start(home)
        elif args.command in ("run", "_worker"):
            worker(home)
        elif args.command == "status":
            show_status(home, args.destination, args.json)
        elif args.command == "stop":
            if not control(home, "stop"):
                print(
                    "No managed worker is responding. No process was killed and no data changed."
                )
                return
            for _ in range(60):
                if not control(home):
                    print("Stopped. Data retained; use cloudisle-dl resume.")
                    return
                time.sleep(1)
            print(
                "Graceful stop requested; still finishing recovery or an in-flight operation. No force kill was sent."
            )
        elif args.command == "logs":
            cfg = read_config(home)
            with (Path(cfg["state"]) / "worker.log").open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 1024 * 1024))
                print(
                    b"\n".join(
                        stream.read().splitlines()[-max(1, args.lines) :]
                    ).decode(errors="replace")
                )
        elif args.command == "report":
            from .report_packed import generate

            cfg = read_config(home)
            output = args.output or Path(cfg["destination"]) / "reports"
            if control(home):
                raise RuntimeError(
                    "Wait for acquisition/final audit to finish before generating the report"
                )
            print(json.dumps(generate(cfg["destination"], cfg["state"], output)))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"cloudisle-dl: {error}\n")
