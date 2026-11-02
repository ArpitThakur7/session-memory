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
