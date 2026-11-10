"""Install an official checksummed GitHub CLI binary without administrator rights."""

import hashlib
import io
import json
import os
from pathlib import Path
import platform
import tarfile
import urllib.request
import zipfile


def fetch(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "cloudisle-dl-installer"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main():
    system = {"Linux": "linux", "Darwin": "macOS", "Windows": "windows"}.get(
        platform.system()
    )
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if not system or not arch:
        raise RuntimeError(
            "Unsupported platform/architecture for automatic GitHub CLI bootstrap"
        )
    release = json.loads(fetch("https://api.github.com/repos/cli/cli/releases/latest"))
    suffix = f"_{system}_{arch}." + ("zip" if system == "windows" else "tar.gz")
    asset = next(a for a in release["assets"] if a["name"].endswith(suffix))
    checksums = next(
        a for a in release["assets"] if a["name"].endswith("_checksums.txt")
    )
    expected = next(
        line.split()[0]
        for line in fetch(checksums["browser_download_url"]).decode().splitlines()
        if line.split()[-1].lstrip("*") == asset["name"]
    )
    body = fetch(asset["browser_download_url"])
    if hashlib.sha256(body).hexdigest() != expected:
        raise RuntimeError("GitHub CLI download checksum mismatch")
    executable = "gh.exe" if system == "windows" else "gh"
    if system == "windows":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            member = next(
                n for n in archive.namelist() if n.endswith("/bin/" + executable)
            )
            binary = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            member = next(
                m
                for m in archive.getmembers()
                if m.isfile() and m.name.endswith("/bin/" + executable)
            )
            binary = archive.extractfile(member).read()
    path = Path.home() / ".local/bin" / executable
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(binary)
    if os.name != "nt":
        path.chmod(0o755)
    print(f"Installed GitHub CLI: {path}")


if __name__ == "__main__":
    main()
