"""Install latest private-repository source using gh credentials, without git."""

import argparse
import io
from pathlib import Path
import subprocess
import tempfile
import zipfile

REPOSITORY = "onetrickpony1/cloudisle-dl"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uv", required=True)
    parser.add_argument("--local", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cloudisle-dl-install-") as tmp:
        if args.local:
            source = args.local.resolve()
        else:
            subprocess.run(["gh", "auth", "status"], check=True)
            archive = subprocess.run(
                ["gh", "api", f"repos/{REPOSITORY}/zipball/main"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            root = Path(tmp).resolve()
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                for member in zipped.infolist():
                    if not (root / member.filename).resolve().is_relative_to(root):
                        raise RuntimeError("Unsafe archive path")
                zipped.extractall(root)
            source = next(root.glob("*/pyproject.toml")).parent
        subprocess.run(
            [args.uv, "tool", "install", "--force", "--python", "3.12", str(source)],
            check=True,
        )
        subprocess.run([args.uv, "tool", "update-shell"], check=True)
        print(
            "cloudisle-dl installed/updated. Open a new terminal if PATH was updated."
        )
        print("Use: cloudisle-dl status. Existing workers and data were not changed.")
        print("A running worker keeps its loaded version until you stop and resume it.")


if __name__ == "__main__":
    main()
