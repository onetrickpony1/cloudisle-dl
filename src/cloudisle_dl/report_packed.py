#!/usr/bin/env python3
"""Produce a concise completion report from the acquisition ledger; never downloads."""

import argparse, collections, csv, json, sqlite3
from pathlib import Path
from .platform_support import db_uri


def generate(destination, state, output):
    destination = Path(destination)
    state = Path(state)
    output = Path(output)
    db = sqlite3.connect(db_uri(state / "archive.sqlite", readonly=True), uri=True)
    db.row_factory = sqlite3.Row
    finished = db.execute("SELECT value FROM settings WHERE key='finished'").fetchone()
    pending = db.execute(
        "SELECT COUNT(*) FROM jobs WHERE state IN ('pending','running')"
    ).fetchone()[0]
    if not finished or pending:
        raise RuntimeError(
            "Acquisition/final audit not finished; refusing a completion report"
        )
    audit = json.loads((destination / "completeness.json").read_text())
    if not audit.get("checked") or not audit.get("sources"):
        raise RuntimeError("Missing final source audit")
    output.mkdir(parents=True, exist_ok=True)
    failures = [
        dict(r)
        for r in db.execute(
            "SELECT source,kind,url,rel,state,error FROM jobs WHERE state!='done' ORDER BY source,kind,url"
        )
    ]
    warnings = (
        [dict(r) for r in db.execute("SELECT * FROM discrepancies")]
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='discrepancies'"
        ).fetchone()
        else []
    )
    (output / "metadata-discrepancies.json").write_text(
        json.dumps(warnings, indent=2)
    ) if output.exists() else None
    groups = collections.Counter()
    for row in failures:
        if row["state"] == "missing":
            category = "Unavailable " + row["kind"]
        elif "integrity" in (row["error"] or "").lower():
            category = "Local integrity failure"
        else:
            category = "Unresolved " + row["kind"] + " failure"
        groups[category] += 1
    with (output / "missing-files.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["source", "kind", "url", "rel", "state", "error"]
        )
        writer.writeheader()
        writer.writerows(failures)
    (output / "metadata-discrepancies.json").write_text(json.dumps(warnings, indent=2))
    fields = [
        "source",
        "expected_points",
        "hierarchy_points",
        "verified_points",
        "complete_relative_to_metadata",
    ]
    with (output / "source-completeness.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(audit["sources"])
    totals = db.execute(
        "SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes FROM jobs WHERE kind IN ('laz','zip') AND state='done'"
    ).fetchone()
    complete = [s for s in audit["sources"] if s["complete_relative_to_metadata"]]
    ziprows = []
    for z in db.execute("SELECT * FROM jobs WHERE kind='zip' ORDER BY rel"):
        members = db.execute(
            "SELECT COUNT(*) FROM zipmembers WHERE archive=?", (z["source"],)
        ).fetchone()[0]
        ziprows.append(
            f"| {z['source']} | {z['state']} | {(z['size'] or 0):,} | {members:,} |"
        )
    packed = (
        db.execute("SELECT COUNT(*) FROM pack_members").fetchone()[0]
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='pack_members'"
        ).fetchone()
        else 0
    )
    lines = [
        "# Cloudisle 2018 acquisition report",
        "",
        f"Final audit: {audit['checked']}",
        "",
        f"Destination: `{destination}` (mpc H drive).",
        "",
        f"Packed LAZ members: **{packed:,}**. Payload counts and byte totals describe logical source files, excluding pack container overhead. Existing ordinary files and original ZIP archives retain their original layouts.",
        "",
        f"Validated payloads: **{totals['n']:,} files**, **{totals['bytes']:,} bytes** ({totals['bytes'] / 10**12:.3f} TB).",
        f"Sources complete relative to their published metadata: **{len(complete)} of {len(audit['sources'])}**.",
        f"Missing or unresolved jobs: **{len(failures):,}**.",
        f"Publisher metadata disagreements: **{len(warnings):,}**; see metadata-discrepancies.json. Readable data was retained, but affected sources are not certified complete.",
        "",
        "These source representations overlap. Their bytes and points must not be interpreted as unique national coverage. "
        "A source marked complete has matching expected, enumerated and decoded point counts and no unresolved jobs. "
        "This is not independent proof that the original national survey is complete.",
        "",
        "## ZIP archives",
        "",
        "| Archive | State | Bytes | CRC-checked members |",
        "|---|---|---:|---:|",
        *ziprows,
        "",
        "A done ZIP passed full member CRC reads and the final local SHA-256 audit. Archives remain compressed.",
        "",
        "## Missing or unverified",
        "",
        "| Category | Jobs |",
        "|---|---:|",
    ]
    lines += [f"| {k} | {v:,} |" for k, v in sorted(groups.items())] or ["| None | 0 |"]
    lines += [
        "",
        "See `missing-files.csv` for every affected URL and its error, and `source-completeness.csv` for per-source counts.",
        "",
        "HTTP 404 entries establish that the tested URL was unavailable, not that the corresponding measurements are absent "
        "from every overlapping dataset. Missing hierarchy/control files may leave coverage unknown even when some chunks were recovered.",
        "",
        "## Validation limits",
        "",
        "LAZ files were fully decompressed and compared with hierarchy counts where available. SHA-256 hashes detect local corruption. "
        "No authoritative publisher checksum manifest was available. Unknown point-count expectations and uncovered branches remain explicitly unverified.",
    ]
    (output / "final-download-report.md").write_text("\n".join(lines) + "\n")
    db.close()
    return dict(
        payload_files=totals["n"],
        bytes=totals["bytes"],
        complete_sources=len(complete),
        failures=len(failures),
    )
