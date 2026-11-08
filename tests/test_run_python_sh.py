import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import posix_bash

LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "run-python"
BASH = posix_bash()
USABLE = BASH is not None
# Shebang for the fake programs: the bash under test by absolute path, since PATH is cut down
# below (on macOS bash lives in /bin, outside it). Windows paths have spaces, so env is used there.
SHEBANG = "#!/usr/bin/env bash" if os.name == "nt" else f"#!{BASH}"


def _script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SHEBANG + "\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _tools_without_python(tmp_path: Path) -> str:
    """A folder holding `dirname` (the launcher needs it) and no python, for a cut-down PATH.

    Usually dirname's own folder; on Linux that is /usr/bin, which also holds python3,
    so a folder with just a link to dirname is made instead."""
    dirname = shutil.which("dirname") or BASH
    tools = os.path.dirname(dirname)
    if not any(os.path.exists(os.path.join(tools, name)) for name in ("python3", "python", "py")):
        return tools
    own = tmp_path / "tools"
    own.mkdir(exist_ok=True)
    os.symlink(dirname, own / "dirname")
    return str(own)


@pytest.mark.skipif(not USABLE, reason="needs a POSIX bash")
def test_launcher_skips_store_stub_and_starts_python_once(tmp_path):
    count = tmp_path / "starts.txt"
    real = Path(sys.executable).as_posix()
    # the Windows Store stub: must never be started (it costs ~200 ms even to fail)
    _script(tmp_path / "WindowsApps" / "python3", f'echo stub >> "{count.as_posix()}"\nexit 9009\n')
    _script(tmp_path / "bin" / "python", f'echo real >> "{count.as_posix()}"\nexec "{real}" "$@"\n')
    tools = _tools_without_python(tmp_path)
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PATH", "PYTHONPATH")}
    env["PATH"] = os.pathsep.join([str(tmp_path / "WindowsApps"), str(tmp_path / "bin"), tools])
    project = tmp_path / "proj"
    project.mkdir()
    r = subprocess.run([BASH, str(LAUNCHER), "stats"], cwd=project, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "nodes=0" in r.stdout, r.stderr
    assert count.read_text(encoding="utf-8").split() == ["real"]


@pytest.mark.skipif(not USABLE, reason="needs a POSIX bash")
def test_launcher_without_python_exits_quietly(tmp_path):
    tools = _tools_without_python(tmp_path)
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PATH", "PYTHONPATH")}
    env["PATH"] = os.pathsep.join([str(tmp_path / "empty"), tools])
    r = subprocess.run([BASH, str(LAUNCHER), "stats"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == ""
