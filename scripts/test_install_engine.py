import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest

spec = importlib.util.spec_from_file_location(
    "install_engine", Path(__file__).with_name("install-engine.py")
)
install_engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_engine)
main = install_engine.main


def wheel(tmp_path: Path, version: str = "2.0.0rc6") -> tuple[Path, str]:
    path = tmp_path / "engine.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "nautilus_trader.dist-info/METADATA",
            f"Name: nautilus-trader\nVersion: {version}\n",
        )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_wrong_hash_rejects_installation(tmp_path: Path) -> None:
    path, _ = wheel(tmp_path)
    with (
        patch("sys.argv", ["install-engine.py", str(path), "0" * 64]),
        patch.object(install_engine.subprocess, "run") as run,
    ):
        with pytest.raises(SystemExit):
            main()
        run.assert_not_called()


def test_wrong_version_rejects_installation(tmp_path: Path) -> None:
    path, checksum = wheel(tmp_path, "1.0.0")
    with (
        patch("sys.argv", ["install-engine.py", str(path), checksum]),
        patch.object(install_engine.subprocess, "run") as run,
    ):
        with pytest.raises(SystemExit):
            main()
        run.assert_not_called()


def test_verified_wheel_installs_without_building_engine(tmp_path: Path) -> None:
    path, checksum = wheel(tmp_path)
    with (
        patch("sys.argv", ["install-engine.py", str(path), checksum]),
        patch.object(install_engine.subprocess, "run") as run,
    ):
        main()
    assert run.call_count == 2
    install = run.call_args_list[0].args[0]
    assert install[:3] == ["uv", "pip", "install"]
    assert "--no-deps" in install
    assert install[-1] == str(path)
