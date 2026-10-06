"""Read an engine wheel from a verified release saved on the self-hosted runner."""

import argparse
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path


def prepare_engine(release: Path, output: Path) -> dict[str, str]:
    release = release.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}-[0-9]+-[0-9]+", release.name):
        raise ValueError("Select a saved engine release named SHA-RUN_ID-ATTEMPT")
    manifest = {}
    for line in (release / "release.sha256").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if (
            match is None
            or match[2]
            not in (
                "monitoring.tar.gz",
                "monitoring.tar.gz.sha256",
                "install-monitoring.bash",
            )
            or match[2] in manifest
        ):
            raise ValueError("Invalid engine release checksum manifest")
        manifest[match[2]] = match[1]
    if set(manifest) != {
        "monitoring.tar.gz",
        "monitoring.tar.gz.sha256",
        "install-monitoring.bash",
    }:
        raise ValueError("Incomplete engine release checksum manifest")
    for name, expected in manifest.items():
        with (release / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"Engine release checksum mismatch: {name}")
    archive_checksum = (release / "monitoring.tar.gz.sha256").read_text().strip()
    if archive_checksum != f"{manifest['monitoring.tar.gz']}  monitoring.tar.gz":
        raise ValueError("Engine archive checksum files disagree")
    with tarfile.open(release / "monitoring.tar.gz", "r:gz") as archive:
        revision = archive.extractfile("REVISION")
        if revision is None or revision.read().decode().strip() != release.name[:40]:
            raise ValueError("Engine archive revision differs from selected release")
        wheels = [
            member
            for member in archive.getmembers()
            if member.name.startswith("wheelhouse/nautilus_trader-")
            and member.name.endswith(".whl")
        ]
        if len(wheels) != 1:
            raise ValueError("Engine release must contain exactly one engine wheel")
        member = wheels[0]
        filename = Path(member.name).name
        if (
            not member.isfile()
            or member.name != f"wheelhouse/{filename}"
            or not re.fullmatch(
                r"nautilus_trader-[A-Za-z0-9_.+]+-cp314-cp314-[A-Za-z0-9_.]*linux[A-Za-z0-9_.]*x86_64\.whl",
                filename,
            )
        ):
            raise ValueError("Requires a regular Linux x86_64 CPython 3.14 wheel")
        output.mkdir(parents=True, exist_ok=True)
        wheel = (output / filename).resolve()
        source = archive.extractfile(member)
        with wheel.open("wb") as destination:
            shutil.copyfileobj(source, destination)
    with wheel.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    selection = {
        "wheel": str(wheel),
        "sha256": checksum,
        "engine_revision": release.name[:40],
        "engine_release": release.name,
        "archive_sha256": manifest["monitoring.tar.gz"],
    }
    (output / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
    return selection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    try:
        selection = prepare_engine(args.release_dir, args.output)
    except (OSError, ValueError, tarfile.TarError, KeyError) as e:
        parser.error(str(e))
    if args.github_env:
        values = {
            "ENGINE_WHEEL": selection["wheel"],
            "ENGINE_SHA256": selection["sha256"],
            "ENGINE_SOURCE_MANIFEST": str((args.output / "selection.json").resolve()),
        }
        if any("\n" in value or "\r" in value for value in values.values()):
            parser.error("Invalid newline in engine artifact path")
        with args.github_env.open("a") as stream:
            for name, value in values.items():
                stream.write(f"{name}={value}\n")
    print(json.dumps(selection))


if __name__ == "__main__":
    main()
