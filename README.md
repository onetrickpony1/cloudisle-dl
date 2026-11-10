# cloudisle-dl

Resumable acquisition of the University of Malta's published Cloudisle point-cloud representations. Runs in the background on **Linux, macOS and Windows**, with a clear status command. No browser, GPU, PDAL, or system-wide Python installation is required.

## Install / update

From a checkout, run the same command for installation and updates:

**Linux / macOS**
```sh
./install.sh
```
**Windows PowerShell**
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer provisions uv, Python 3.12 and the Python dependencies in an isolated tool environment. It fetches the latest `main` from this private repository via GitHub CLI; no git client is needed. If needed, it installs an official checksummed GitHub CLI binary in your user profile and asks you to authenticate. Your account must have access to the private repository. GitHub authentication cannot be completed on your behalf. No administrator privileges, Homebrew or winget are required. Linux/macOS need curl or wget to bootstrap uv; Windows uses built-in PowerShell HTTPS support.

Already have authenticated `gh`? Install/update without a checkout:

```sh
# Linux/macOS
 gh api repos/onetrickpony1/cloudisle-dl/contents/install.sh --jq .content | base64 -d | bash
```
```powershell
# Windows PowerShell
$script = gh api repos/onetrickpony1/cloudisle-dl/contents/install.ps1 --jq .content; & ([scriptblock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($script -join "`n")))))
```

For local development, `./install.sh --local` or `.\install.ps1 -Local` installs the checkout without GitHub authentication. Alternatively: `python -m pip install .` in a virtual environment.

Installation/update never starts, stops or migrates a downloader, and never removes downloaded data. A running worker continues using its loaded version; run `stop` then `resume` to load the update. Reopen your terminal if the installer updates PATH.

## Commands

```sh
# Configure only: destination directly inside a mounted share/drive.
cloudisle-dl init --destination /mnt/share/Cloudisle_2018
# macOS example: /Volumes/Archive/Cloudisle_2018
# Windows example: H:\Cloudisle_2018

cloudisle-dl start                 # detached background process
cloudisle-dl status                # totals, current file, timestamp, free space
cloudisle-dl status --json         # machine-readable status
cloudisle-dl logs --lines 30       # latest worker log lines
cloudisle-dl stop                  # graceful stop; never deletes data
cloudisle-dl resume                # continue the saved queue
cloudisle-dl run                   # foreground mode instead
cloudisle-dl report                # only after acquisition AND final audit
```

For a local directory use `init --destination PATH --allow-local`. Defaults: one connection, 2 requests/second, 8 MiB/second, **50 GiB free-space reserve**. Configure with `--connections 2`, `--requests-per-second 5`, `--mib-per-second 8`, `--reserve-gib 50`. To change saved settings, stop the worker and repeat `init` with `--force` and the desired options. Existing data stays in place.

`start`/`resume` detach from the terminal. They do **not** register a login/reboot service or restart after an OS crash: after reboot use `resume`. On macOS/Windows, sleep pauses network activity; the machine must remain awake. Startup can take minutes while existing packs are reconciled. `stop` requests a graceful exit and may wait for startup recovery or an in-flight operation; it does not force-kill the worker.

Inspect an archive managed by another worker without taking it over:
```sh
cloudisle-dl status --destination /path/to/Cloudisle_2018
```
This reads saved JSON only; it never locks/scans SQLite. Check the update timestamp: a saved `running` phase does not prove an external process is alive. `start` refuses an archive locked by another acquisition worker. Do not run the legacy and packaged downloader against the same archive simultaneously.

For separate configurations: `cloudisle-dl --config-dir PATH init ...` and use that same global option for all subsequent commands. `CLOUDISLE_DL_HOME` also sets the configuration directory.

## Storage and integrity

- Data, SQLite state, rollback journals, logs, partial transfers and backups all stay **inside the destination**. Only tiny CLI configuration/control files live in your user profile.
- State is at `control/acquisition-state`; `status.json` is published there and at the destination root.
- LAZ files are decoded and hashed. Small LAZ files are stored unchanged in immutable ZIP_STORED packs, up to 20 members each. Existing raw files and original ZIP archives keep their layout.
- Durable pack writes precede transactional indexing; orphan recovery runs before queue selection. Downloads resume with range/identity checks where supported.
- Discovery and payload processing alternate so a large directory crawl does not indefinitely defer known payloads. Server-directed backoff and bounded connections remain enforced.
- SQLite uses DELETE journals and FULL synchronous writes. POSIX uses unix-dotfile locking; Windows uses native SQLite locking. Use a reliable mounted filesystem; never delete SQLite journals to recover space. Never remove a lock while a worker may own the archive.
- Periodic database snapshots are taken every six hours after the previous copy finishes, and at final completion. Backup and audit operations can temporarily pause payload growth.
- A final per-source report and missing-file lists are produced only after acquisition and auditing finish.

These sources overlap. Bytes, jobs and point counts are **not unique national coverage**. Metadata can be missing or inconsistent; `done` records acquisition validation, not independent certification that Malta's survey is complete. A growing queue is normal during discovery. A source HTTP 404 does not prove the same measurements are absent from another representation.

## Scope

The source is `https://www.um.edu.mt/projects/cloudisle/DATA1/cloudisleII/webpublish/pointclouds/`, including grid/locality/region representations and the additional ZIP archives. This is not a generic downloader and does not bypass access controls. No credentials, survey data, live databases, local mount configuration or desktop notification scripts are included in this repository. Data rights remain with the publisher; this repository does not grant rights to redistribute the survey.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```
Tests use temporary synthetic data and a local HTTP server, not the production endpoint. `PACK_TEST_ROOT` can select a disposable test directory. GitHub Actions runs the test suite on Linux, macOS and Windows. See workflow results for actual platform validation; a CI definition alone is not a passing test.
