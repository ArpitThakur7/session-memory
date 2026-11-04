import os
import subprocess
from pathlib import Path

import pytest

CMD = Path(__file__).resolve().parent.parent / "bin" / "run-python.cmd"


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
def test_cmd_runs_stats_from_another_folder(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run(["cmd", "/c", str(CMD), "stats"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "nodes=0" in r.stdout


def test_gitattributes_forces_crlf_for_cmd():
    text = (CMD.parent.parent / ".gitattributes").read_text(encoding="utf-8")
    assert "bin/run-python.cmd text eol=crlf" in text


def test_cmd_starts_python_once_via_py():
    lines = [ln.strip() for ln in CMD.read_text(encoding="utf-8").splitlines()]
    py_lines = [ln for ln in lines if ln.startswith("py ")]
    assert py_lines and py_lines[0].startswith("py -3 -m memgraph.cli"), "py must run the CLI directly, no probe first"


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
def test_cmd_falls_back_to_python_without_py(tmp_path):
    import sys

    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PATH", "Path")}
    env["PATH"] = os.pathsep.join([os.path.dirname(sys.executable), system32])
    r = subprocess.run(["cmd", "/c", str(CMD), "stats"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "nodes=0" in r.stdout
