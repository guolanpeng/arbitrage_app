"""Generate a Linux uv project from the application's dependency declarations."""

import argparse
import json
import shutil
from pathlib import Path

import tomllib


def release_project(source: Path, output: Path, index: Path) -> None:
    if output.exists():
        raise ValueError("Release project already exists; use a clean build directory")
    config = tomllib.loads((source / "pyproject.toml").read_text())
    project = config["project"]
    output.mkdir(parents=True)
    text = (
        "[project]\n"
        f"name = {json.dumps(project['name'])}\n"
        f"version = {json.dumps(project['version'])}\n"
        'requires-python = "==3.14.*"\n'
        f"dependencies = {json.dumps(project['dependencies'])}\n\n"
        "[dependency-groups]\n"
        f"test = {json.dumps(config['dependency-groups']['test'])}\n\n"
        "[tool.uv]\npackage = false\n"
        "environments = [\"sys_platform == 'linux' and platform_machine == 'x86_64'\"]\n\n"
        "required-environments = [\"sys_platform == 'linux' and platform_machine == 'x86_64'\"]\n\n"
        '[tool.uv.sources]\nnautilus-trader = { index = "engine" }\n\n'
        '[[tool.uv.index]]\nname = "engine"\nformat = "flat"\nexplicit = true\n'
        f"url = {json.dumps(index.resolve().as_posix())}\n"
    )
    (output / "pyproject.toml").write_text(text, encoding="utf-8")
    shutil.copyfile(source / "uv.lock", output / "uv.lock")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    release_project(root / "python", root / "dist/project", args.index)


if __name__ == "__main__":
    main()
