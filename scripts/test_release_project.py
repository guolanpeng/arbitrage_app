import importlib.util
from pathlib import Path

import pytest
import tomllib

spec = importlib.util.spec_from_file_location(
    "release_project", Path(__file__).with_name("release-project.py")
)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def test_release_uses_same_dependencies_and_linux_engine_index(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "python"
    output = tmp_path / "project"
    index = tmp_path / "wheels"
    generator.release_project(source, output, index)
    original = tomllib.loads((source / "pyproject.toml").read_text())
    release = tomllib.loads((output / "pyproject.toml").read_text())
    assert release["project"]["dependencies"] == original["project"]["dependencies"]
    assert release["dependency-groups"] == original["dependency-groups"]
    assert release["project"]["requires-python"] == "==3.14.*"
    assert release["tool"]["uv"]["index"][0]["url"] == index.resolve().as_posix()
    assert release["tool"]["uv"]["index"][0]["explicit"] is True
    assert (output / "uv.lock").read_bytes() == (source / "uv.lock").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        generator.release_project(source, output, index)
