"""install.sh and install.ps1 must at least parse (they are run by hand, never here for real)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_install_sh_parses():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "\r" not in text
    # bytes, not text (text mode adds CR on Windows); stdin avoids path translation
    r = subprocess.run(["bash", "-n"], input=text.encode("utf-8"), capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_install_ps1_parses():
    path = str(ROOT / "install.ps1").replace("'", "''")
    script = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
              f"'{path}', [ref]$null, [ref]$e); $e.Count")
    r = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "0", r.stdout + r.stderr


def test_install_ps1_is_ascii():
    """Windows PowerShell 5.1 reads a BOM-less script as ANSI, so keep it ASCII."""
    (ROOT / "install.ps1").read_bytes().decode("ascii")


def test_gitattributes_pins_installer_line_endings():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "install.sh text eol=lf" in text and "install.ps1 text eol=crlf" in text
