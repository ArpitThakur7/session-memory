"""Shared filter that keeps helper-agent scratch noise out of the brief, pack and image.

The graph and the 3D map keep everything; only the readable summaries use this.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

IGNORED_SEGMENTS = frozenset({
    ".superpowers", ".claude", ".git", "node_modules", "__pycache__",
    ".pytest_cache", ".venv",
})
# Where Windows keeps temp folders, even when the recorded user is not this one.
_TEMP_HINT = "/appdata/local/temp/"

_CMD_SEGMENT = re.compile(
    r"(?:^|[\s\"'`=(/\\])(?:" + "|".join(re.escape(s) for s in sorted(IGNORED_SEGMENTS)) +
    r")(?=[/\\\s\"'`)]|$)", re.IGNORECASE)
_DRIVE = re.compile(r"[A-Za-z]:[\\/]")


def _norm(text) -> str:
    """Lower-cased text with one kind of slash, for comparing paths."""
    return re.sub(r"/+", "/", str(text).replace("\\", "/")).casefold()


def _mentions_temp(text) -> bool:
    """True when the text contains the system temp folder (or a Windows Temp folder)."""
    norm = _norm(text)
    temp = _norm(tempfile.gettempdir()).rstrip("/")
    if temp and re.search(re.escape(temp) + r"(?=[/\s\"'`)\]]|$)", norm):
        return True
    return _TEMP_HINT in norm + "/"


def _is_absolute(label: str) -> bool:
    return bool(_DRIVE.match(label)) or label.startswith(("/", "\\"))


def is_project_file(label, project_root: Path | None) -> bool:
    """False for scratch: outside `project_root`, under an ignored folder, or in the temp folder."""
    label = str(label or "")
    absolute = _is_absolute(label)
    rel = _norm(label)
    if absolute and project_root is not None:
        root = _norm(project_root).rstrip("/")
        if not (rel == root or rel.startswith(root + "/")):
            return False
        rel = rel[len(root):]  # inside the project: even a root under the temp folder is fine
    elif absolute and _mentions_temp(label):
        return False
    return not any(s in IGNORED_SEGMENTS for s in rel.split("/"))


def is_scratch_command(cmd_label, project_root: Path | None = None) -> bool:
    """True when the command touches the temp folder or an ignored folder (including `cd` into one)."""
    text = str(cmd_label or "")
    return _mentions_temp(text) or bool(_CMD_SEGMENT.search(text))


def project_errors(graph, errors, project_root: Path | None = None) -> list[dict]:
    """The error nodes worth showing: not from scratch commands or files, not mentioning temp paths."""
    sources: dict[str, list[str]] = {}
    for e in graph.edges.values():
        if e.get("rel") == "failed":
            sources.setdefault(e.get("dst"), []).append(e.get("src"))
    keep = []
    for err in errors:
        if _mentions_temp(err.get("label", "")):
            continue
        noisy = []
        for src in sources.get(err.get("id"), []):
            node = graph.nodes.get(src) or {}
            if node.get("kind") == "command":
                noisy.append(is_scratch_command(node.get("label", ""), project_root))
            elif node.get("kind") == "file":
                noisy.append(not is_project_file(node.get("label", ""), project_root))
        if noisy and all(noisy):
            continue
        keep.append(err)
    return keep
