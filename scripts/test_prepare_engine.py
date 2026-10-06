import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "prepare_engine", Path(__file__).with_name("prepare-engine.py")
)
prepare_engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_engine)
SHA = "1" * 40
WHEEL = "nautilus_trader-2.0.0rc6-cp314-cp314-manylinux_2_39_x86_64.whl"


def release(
    tmp_path: Path, *, revision: str = SHA, wheels: tuple[str, ...] = (WHEEL,)
) -> Path:
    root = tmp_path / f"{SHA}-123-1"
    root.mkdir()
    with tarfile.open(root / "monitoring.tar.gz", "w:gz") as archive:
        for name, content in [
            ("REVISION", revision.encode()),
            *[(f"wheelhouse/{wheel}", b"prebuilt engine") for wheel in wheels],
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    checksum = hashlib.sha256((root / "monitoring.tar.gz").read_bytes()).hexdigest()
    (root / "monitoring.tar.gz.sha256").write_text(f"{checksum}  monitoring.tar.gz\n")
    (root / "install-monitoring.bash").write_text("#!/bin/bash\n")
    names = ("monitoring.tar.gz", "monitoring.tar.gz.sha256", "install-monitoring.bash")
    (root / "release.sha256").write_text(
        "".join(
            f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}\n"
            for name in names
        )
    )
    return root


def test_reuses_verified_wheel_and_records_source(tmp_path: Path) -> None:
    root = release(tmp_path)
    output = tmp_path / "selected"
    selected = prepare_engine.prepare_engine(root, output)
    assert Path(selected["wheel"]).read_bytes() == b"prebuilt engine"
    assert selected["sha256"] == hashlib.sha256(b"prebuilt engine").hexdigest()
    assert selected["engine_revision"] == SHA
    assert selected["engine_release"] == root.name
    assert json.loads((output / "selection.json").read_text()) == selected


def test_rejects_tampered_archive_before_extracting(tmp_path: Path) -> None:
    root = release(tmp_path)
    (root / "monitoring.tar.gz").write_bytes(b"changed")
    output = tmp_path / "selected"
    with pytest.raises(ValueError, match="checksum mismatch"):
        prepare_engine.prepare_engine(root, output)
    assert not output.exists()


def test_rejects_different_engine_revision(tmp_path: Path) -> None:
    root = release(tmp_path, revision="2" * 40)
    with pytest.raises(ValueError, match="revision differs"):
        prepare_engine.prepare_engine(root, tmp_path / "selected")


@pytest.mark.parametrize(
    "wheels",
    [
        (),
        (WHEEL, WHEEL.replace("2.0.0rc6", "2.0.0rc5")),
        ("nautilus_trader-2.0.0rc6-cp313-cp313-win_amd64.whl",),
        ("../" + WHEEL,),
    ],
)
def test_rejects_missing_ambiguous_or_incompatible_wheel(
    tmp_path: Path, wheels: tuple[str, ...]
) -> None:
    root = release(tmp_path, wheels=wheels)
    output = tmp_path / "selected"
    with pytest.raises(ValueError):
        prepare_engine.prepare_engine(root, output)
    assert not output.exists()


def test_rejects_manifest_path_traversal(tmp_path: Path) -> None:
    root = release(tmp_path)
    (root / "release.sha256").write_text(f"{'0' * 64}  ../outside\n")
    with pytest.raises(ValueError, match="Invalid engine release"):
        prepare_engine.prepare_engine(root, tmp_path / "selected")
