import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "run-python"
BASH = shutil.which("bash")
USABLE = BASH is not None and "system32" not in BASH.lower()  # WSL's bash.exe sees another filesystem


def _script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.mark.skipif(not USABLE, reason="needs a POSIX bash")
def test_launcher_skips_store_stub_and_starts_python_once(tmp_path):
    count = tmp_path / "starts.txt"
    real = Path(sys.executable).as_posix()
    # the Windows Store stub: must never be started (it costs ~200 ms even to fail)
    _script(tmp_path / "WindowsApps" / "python3", f'echo stub >> "{count.as_posix()}"\nexit 9009\n')
    _script(tmp_path / "bin" / "python", f'echo real >> "{count.as_posix()}"\nexec "{real}" "$@"\n')
    tools = os.path.dirname(shutil.which("dirname") or BASH)
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
    tools = os.path.dirname(shutil.which("dirname") or BASH)
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PATH", "PYTHONPATH")}
    env["PATH"] = os.pathsep.join([str(tmp_path / "empty"), tools])
    r = subprocess.run([BASH, str(LAUNCHER), "stats"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == ""
