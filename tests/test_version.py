"""Telling releases apart: the build ID that `--version` prints and make_release.py stamps."""

import importlib.util
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import x100

ROOT = Path(__file__).resolve().parents[1]
LINE = re.compile(r"^x100-control \d+\.\d+\.\d+ \(build ([0-9a-f]{8}, \d{4}-\d{2}-\d{2}|dev)\)$")


def load_make_release():
    spec = importlib.util.spec_from_file_location("make_release", ROOT / "tools" / "make_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mr = load_make_release()


@pytest.mark.parametrize("module", ["x100", "x100_bridge"])
def test_version_flag(module):
    out = subprocess.run([sys.executable, "-m", module, "--version"], cwd=ROOT, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == x100.version_line()
    assert LINE.match(out.stdout.strip()), out.stdout


def test_version_works_beside_a_settings_file_you_cant_read(tmp_path, monkeypatch, capsys):
    """In /opt/x100-control, bridge.env is root:x100 640, so your own login can't read it."""
    from x100 import cli

    (tmp_path / "bridge.env").write_text("LOCAL_API_TOKEN=secret\n")
    monkeypatch.chdir(tmp_path)

    def unreadable(path):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(cli, "load_env_file", unreadable)
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    assert stop.value.code == 0 and capsys.readouterr().out.strip() == x100.version_line()
    assert cli.main(["status"]) == 2  # anything else explains instead of a traceback
    assert "sudo -u x100" in capsys.readouterr().err


def test_the_listener_version_flag(capsys):
    pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    from x100_listener import main as lmain

    with pytest.raises(SystemExit) as stop:
        lmain.main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == x100.version_line()


def make_tree(root: Path) -> None:
    files = {
        "README.md": "readme\n",
        "x100/__init__.py": "x = 1\n",
        "x100/_build.py": 'BUILD = "old"\n',
        "x100/__pycache__/cli.cpython-311.pyc": "junk",
        "deploy/install.sh": "#!/bin/sh\n",
        "deploy/bridge.env.example": "LOCAL_API_TOKEN=\n",
        "bridge.env": "LOCAL_API_TOKEN=secret\n",
        "listener.env": "BRIDGE_TOKEN=secret\n",
        "config/README.md": "inputs\n",
        "config/inputs.json": "{}\n",
        "tests/test_a.py": "\n",
        "tests/data/e2e-cache/clip.wav": "RIFF",
        "x100_control.egg-info/PKG-INFO": "meta\n",
        ".venv311/bin/python": "",
        "build/lib/x100/__init__.py": "",
        "models/hey_jarvis.onnx": "",
        ".gitignore": "x100/_build.py\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    os.chmod(root / "deploy/install.sh", 0o755)


def test_only_the_release_is_released(tmp_path):
    make_tree(tmp_path)
    names = [p.as_posix() for p in mr.released_files(tmp_path)]
    assert names == [".gitignore", "README.md", "config/README.md", "deploy/bridge.env.example",
                     "deploy/install.sh", "tests/test_a.py", "x100/__init__.py"]


def test_the_build_id_tracks_every_change_but_not_the_stamp(tmp_path):
    make_tree(tmp_path)

    def build():
        return mr.build_id(mr.released_files(tmp_path), tmp_path)

    first = build()
    assert re.fullmatch(r"[0-9a-f]{8}", first) and build() == first
    mr.write_stamp(first, "2026-10-12", tmp_path)                 # stamping doesn't change the ID it records
    (tmp_path / "bridge.env").write_text("LOCAL_API_TOKEN=other\n")  # nor do your settings
    assert build() == first
    (tmp_path / "README.md").write_text("readme.\n")
    second = build()
    assert second != first
    os.chmod(tmp_path / "README.md", 0o755)
    assert build() != second
    os.chmod(tmp_path / "README.md", 0o644)
    assert build() == second
    (tmp_path / "config/README.md").rename(tmp_path / "config/INPUTS.md")
    assert build() != second


def test_the_zip_unpacks_into_one_folder_with_the_stamp_and_exec_bits(tmp_path):
    src = tmp_path / "src"
    make_tree(src)
    files = mr.released_files(src)
    build = mr.build_id(files, src)
    mr.write_stamp(build, "2026-10-12", src)
    out = tmp_path / "release.zip"
    mr.write_zip(files, out, src)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert all(n.startswith("x100-control/") for n in names)
        assert "x100-control/x100/_build.py" in names and "x100-control/bridge.env" not in names
        assert f'BUILD = "{build}"' in zf.read("x100-control/x100/_build.py").decode()
        assert zf.getinfo("x100-control/deploy/install.sh").external_attr >> 16 & stat.S_IXUSR
        assert not zf.getinfo("x100-control/README.md").external_attr >> 16 & stat.S_IXUSR
