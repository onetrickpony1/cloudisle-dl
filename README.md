# cloudisle-dl

Download the University of Malta’s Cloudisle point-cloud datasets on **Windows, macOS or Linux**. Runs in the background, resumes interrupted downloads and validates downloaded files.

## Install or update

**Linux / macOS**

```sh
curl -LsSf https://raw.githubusercontent.com/onetrickpony1/cloudisle-dl/main/install.sh | sh
```

**Windows PowerShell**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/onetrickpony1/cloudisle-dl/main/install.ps1 | iex"
```

The installer sets up Python and dependencies automatically. No administrator rights, GitHub account or browser required. Reopen your terminal if the command is not found.

Run the same command to update, then `stop` and `resume` to load the new version. Installation leaves running downloads and stored data untouched.

## Get started

Choose a folder directly inside a mounted drive or share:

```sh
cloudisle-dl init --destination /mnt/share/Cloudisle_2018
cloudisle-dl start
cloudisle-dl status
```

Use `/Volumes/Archive/Cloudisle_2018` on macOS or `H:\Cloudisle_2018` on Windows. For a local folder, add `--allow-local` to `init`. Configuration alone does not start downloading.

## Commands

| Command | Purpose |
|---|---|
| `cloudisle-dl start` | Start in the background |
| `cloudisle-dl status` | Show progress, current file and free space |
| `cloudisle-dl status --json` | Return machine-readable progress |
| `cloudisle-dl logs` | Show recent worker logs |
| `cloudisle-dl stop` | Stop gracefully and keep all data |
| `cloudisle-dl resume` | Continue the saved queue |
| `cloudisle-dl run` | Run in the foreground |
| `cloudisle-dl report` | Generate a report after the final audit |

You can close the terminal after `start`. Keep the PC awake; after a reboot, run `resume`. Startup recovery and graceful stopping can take several minutes.

## Settings

Defaults: **1 connection, 2 requests/second, 8 MiB/second and a 50 GiB free-space reserve**.

Set options during `init`:

```sh
cloudisle-dl init --destination /mnt/share/Cloudisle_2018 --connections 2 --requests-per-second 5 --mib-per-second 8 --reserve-gib 50
```

To change settings, stop the worker and repeat `init` with `--force` and your desired options. Use `--help` for command options.

## Data and completeness

Data, partial files, logs and database state stay inside your chosen destination. Small LAZ files are bundled unchanged into packs; files are decoded and hashed, and original ZIP archives are CRC-checked. Downloads resume where supported.

Discovery may grow the queue, so its size is **not an overall completion percentage**. Source representations overlap, and matching point totals do not prove complete national coverage. The final report lists per-source results and unresolved files.

To inspect an existing archive without taking it over:

```sh
cloudisle-dl status --destination /path/to/Cloudisle_2018
```

Check the status timestamp: saved progress alone does not prove another worker is still running. Only one worker should manage an archive at a time.

Source: [University of Malta Cloudisle files](https://www.um.edu.mt/projects/cloudisle/DATA1/cloudisleII/webpublish/pointclouds/). Data rights remain with the publisher.

## Uninstall

```sh
uv tool uninstall cloudisle-dl
```

Stop the worker first. Uninstalling leaves downloaded data in place.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests use synthetic files and a local HTTP server. CI checks Linux, macOS and Windows.
