# cloudisle-dl: notes for AI assistants

Project-specific rules for working on this repo.

## Layout

- `cli.py`: the `cloudisle-dl` command, starting the background worker and controlling it.
- `archive_base.py`: discovery, the control database and the download queue.
- `archive_packed.py`: the packed worker.
- `pack_store.py`: immutable LAZ packs. Data is written durably first, then indexed, and can be recovered from the
  manifest.
- `job_totals.py`: status totals, updated in the same transaction as each job.
- `space_guard.py`: the free-space reserve.
- `platform_support.py`: portable locks and SQLite paths. No OS service is required.
- `report_packed.py` and `read_payload.py`: read-only. They never download.

## Rules

- Never lose or corrupt data on a crash. Write data before the index, keep packs immutable, and keep recovery from
  the manifest working.
- Only one worker may manage an archive at a time. Keep the advisory lock.
- All data, partial files, logs and state stay inside the destination folder.
- The destination must sit directly inside a mounted drive or share. A local disk needs `--allow-local`. Keep that check.
- Defaults stay conservative: 1 connection, 2 requests a second, 8 MiB a second, 50 GiB reserve.
- Installing or updating never restarts, migrates or stops a running worker, and never touches downloaded data.
- Queue size is not a completion percentage. Don't present it as one.

## Tests

`python -m unittest discover -s tests -v`. Tests use synthetic files and a local HTTP server, never the real site. CI
runs Linux, macOS and Windows × Python 3.11 and 3.13, and runs both installers.
