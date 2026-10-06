"""Install a checksum-verified prebuilt engine into the application environment."""

import argparse
import hashlib
import subprocess
import sys
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("sha256")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    wheel = args.wheel.resolve()
    with wheel.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != args.sha256.lower():
        parser.error("Engine wheel SHA256 mismatch")
    expected = (root / "engine-version.txt").read_text().strip()
    with ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            parser.error("Engine wheel must contain one package metadata file")
        metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
    if (
        metadata["Name"].replace("_", "-").lower() != "nautilus-trader"
        or metadata["Version"] != expected
    ):
        parser.error(f"Expected nautilus-trader {expected}")
    python = (
        root
        / "python/.venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        check=True,
    )
    subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata as m; import nautilus_trader; "
                f"assert m.version('nautilus-trader') == {expected!r}; "
                "print(nautilus_trader.__file__)"
            ),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
