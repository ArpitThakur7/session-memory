"""Code outlines: a local symbol index so Claude can read one function instead of a whole file.

Project source files (from `git ls-files` in a git repo, else a folder walk that skips the usual
dependency and build folders) are parsed into symbols: name, kind, first and last line, the
definition line and, for Python, the first docstring line. Python uses `ast`; other languages use
line-based patterns with brace matching (indentation or `end` for Ruby). Binaries, files over 1 MB
and generated or minified files (a line over 2,000 characters) are skipped. The index is cached
in codeindex.json in the memory folder, keyed by path, mtime_ns and size, and rebuilt only for
files that changed. It is built lazily: by the code tools, and for one file by big-read steering.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import warnings
from functools import lru_cache
from pathlib import Path

from memgraph.store import load_state, write_json_atomic

INDEX_FILE = "codeindex.json"
VERSION = 1
MAX_FILE_BYTES = 1024 * 1024
MAX_LINE_CHARS = 2000
MAX_FILES = 20000
GIT_TIMEOUT = 5
OUTLINE_CHARS = 4000
FIND_CHARS = 4000
READ_LINES = 400
READ_CHARS = 60000
BIG_LINES = 600
STEER_CHARS = 3000
SIG_CHARS = 120
DOC_CHARS = 100

LANGS = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js",
    ".go": "go", ".java": "java", ".cs": "cs", ".rs": "rust",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift",
}
SKIP_DIRS = frozenset({
    ".superpowers", ".claude", ".git", "node_modules", "__pycache__", ".pytest_cache", ".venv",
    "venv", ".tox", ".mypy_cache", ".ruff_cache", "dist", "build", "target", "vendor", ".next", ".nuxt",
    "coverage", ".gradle", "obj", ".idea", ".vs", "bower_components", "site-packages",
})
CONTAINERS = frozenset({"class", "interface", "struct", "enum", "trait", "impl", "namespace", "module",
                        "extension", "object", "union"})
STEER_REASON = ("This file has {n} lines. Its outline:\n{outline}\nRead the lines you need with offset and limit, "
                "or use the code_read tool. To read the whole file anyway, read it again within 2 minutes.")
DASH = "–"


def lang_of(path) -> str | None:
    return LANGS.get(os.path.splitext(str(path))[1].lower())


def big_reads_enabled() -> bool:
    return os.environ.get("SESSION_MEMORY_BIG_READS", "1").strip() != "0"


# ---- parsing ----

def _cap(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def _python_symbols(text: str, lines: list[str]) -> list[list]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _regex_symbols(lines, "python")
    out: list[list] = []

    def visit(body, prefix: str, in_class: bool) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                kind = "class"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if in_class else "function"
                if isinstance(node, ast.AsyncFunctionDef):
                    kind = "async " + kind
            else:
                continue
            name = prefix + node.name
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            sig = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
            doc = ast.get_docstring(node) or ""
            doc = next((ln for ln in doc.splitlines() if ln.strip()), "")
            out.append([name, kind, start, node.end_lineno or node.lineno, _cap(sig, SIG_CHARS), _cap(doc, DOC_CHARS)])
            if kind == "class":
                visit(node.body, name + ".", True)

    visit(tree.body, "", False)
    return out


_MODS = (r"(?:(?:public|private|protected|internal|static|final|abstract|sealed|partial|open|data|inline|value|"
         r"annotation|inner|override|readonly|unsafe|new|fileprivate|export|declare|async|virtual|extern|"
         r"synchronized|native|strictfp|suspend|tailrec|operator|infix|external|mutating|convenience|required|"
         r"dynamic|lazy|weak|default)\s+)*")
_ANNOT = r"(?:@[\w.]+(?:\([^)]*\))?\s+)*"
_KEYWORDS = frozenset("""return if else while for foreach switch catch new throw case delete using goto sizeof
    await yield assert do try lock typeof in is not and or echo print defer go raise elif when match
    function constructor class struct interface enum namespace""".split()) - {"constructor"}


@lru_cache(maxsize=None)
def _specs(lang: str) -> list[tuple]:
    """(pattern, kind) pairs tried in order on each comment-free line; kind "method?" needs a container."""
    c = re.compile
    container = c(r"^\s*" + _ANNOT + _MODS + r"(?P<kw>class|interface|struct|enum|record|trait|object|protocol|"
                  r"extension|namespace|union)\s+(?:class\s+|struct\s+)?(?P<name>[A-Za-z_][\w.]*)")
    generic_fn = c(r"^\s*" + _ANNOT + r"(?P<pre>(?:[\w<>\[\],.?*&:~]+\s+)+)[*&]*(?P<name>~?[A-Za-z_][\w:]*)"
                   r"\s*(?:<[^>()]*>)?\s*\(")
    if lang == "python":
        return [(c(r"^\s*(?P<kw>async\s+def|def)\s+(?P<name>\w+)"), "function"),
                (c(r"^\s*class\s+(?P<name>\w+)"), "class")]
    if lang == "js":
        ident = r"[A-Za-z_$][\w$]*"
        return [
            (c(r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>"
               + ident + r")\s*[<(]"), "function"),
            (c(r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?class\s+(?P<name>" + ident + ")"),
             "class"),
            (c(r"^\s*(?:export\s+)?(?:declare\s+)?interface\s+(?P<name>" + ident + ")"), "interface"),
            (c(r"^\s*(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+(?P<name>" + ident + ")"), "enum"),
            (c(r"^\s*(?:export\s+)?(?:declare\s+)?type\s+(?P<name>" + ident + r")\s*(?:<[^=]*>)?\s*="), "type"),
            (c(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>" + ident + r")\s*(?::[^=]+)?=\s*(?:async\s+)?"
               r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|" + ident + r"\s*=>)"), "function"),
            (c(r"^\s+(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*\*?"
               r"(?P<name>#?" + ident + r")\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^{;]+?)?\s*\{\s*$"), "method?"),
        ]
    if lang == "go":
        return [
            (c(r"^func\s+\((?:\w+\s+)?\*?(?P<recv>\w+)(?:\[[^\]]*\])?\)\s*(?P<name>\w+)\s*[\[(]"), "method"),
            (c(r"^func\s+(?P<name>\w+)\s*[\[(]"), "function"),
            (c(r"^type\s+(?P<name>\w+)(?:\[[^\]]*\])?\s+(?P<kw>struct|interface)\b"), "kw"),
            (c(r"^type\s+(?P<name>\w+)\b"), "type"),
        ]
    if lang == "rust":
        vis = r"(?:pub(?:\([^)]*\))?\s+)?"
        return [
            (c(r"^\s*" + vis + r"(?:default\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?"
               r"fn\s+(?P<name>\w+)"), "function"),
            (c(r"^\s*" + vis + r"(?:unsafe\s+)?(?P<kw>struct|enum|trait|union)\s+(?P<name>\w+)"), "kw"),
            (c(r"^\s*(?:unsafe\s+)?impl(?:<[^{]*?>)?\s+(?:[\w:]+(?:<[^{]*?>)?\s+for\s+)?(?:[\w]+::)*(?P<name>\w+)"),
             "impl"),
            (c(r"^\s*" + vis + r"mod\s+(?P<name>\w+)\s*\{"), "module"),
        ]
    if lang == "ruby":
        return [
            (c(r"^\s*def\s+(?:self\.)?(?P<name>[\w?!=]+|\[\]=?)"), "function"),
            (c(r"^\s*class\s+(?P<name>[A-Z][\w:]*)"), "class"),
            (c(r"^\s*module\s+(?P<name>[A-Z][\w:]*)"), "module"),
        ]
    if lang == "php":
        return [(c(r"^\s*" + _MODS + r"function\s+&?(?P<name>\w+)\s*\("), "function"),
                (c(r"^\s*" + _MODS + r"(?P<kw>class|interface|trait|enum)\s+(?P<name>\w+)"), "kw")]
    if lang in ("kotlin", "swift"):
        return [(c(r"^\s*" + _ANNOT + _MODS + r"(?:fun|func)\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(?P<name>\w+)\s*[(<]"),
                 "function"),
                (container, "kw")]
    return [(container, "kw"), (generic_fn, "function")]  # java, cs, c, cpp


_KIND_ALIAS = {"record": "class", "protocol": "interface"}
_CHAR = re.compile(r"'(?:\\.|[^\\'])'")
_STYLE = {  # (// and /* */ comments, # comments, ' starts a full string, ` strings)
    "python": (False, True, True, False), "ruby": (False, True, True, False),
    "js": (True, False, True, True), "go": (True, False, False, True), "php": (True, True, True, False),
}


def _clean(lines: list[str], lang: str) -> list[str]:
    """The lines with comments removed and string contents blanked (quotes kept), roughly."""
    slashes, hashes, quote_string, backtick = _STYLE.get(lang, (True, False, False, False))
    out, state = [], None
    for line in lines:
        buf, i, n = [], 0, len(line)
        while i < n:
            ch = line[i]
            if state == "block":
                if line.startswith("*/", i):
                    state, i = None, i + 2
                else:
                    i += 1
                buf.append(" ")
                continue
            if state == "`":
                if ch == "\\":
                    i += 2
                elif ch == "`":
                    state, i = None, i + 1
                    buf.append("`")
                    continue
                else:
                    i += 1
                buf.append(" ")
                continue
            if (slashes and line.startswith("//", i)) or (hashes and ch == "#"):
                break
            if slashes and line.startswith("/*", i):
                state, i = "block", i + 2
                buf.append(" ")
                continue
            if ch == '"' or (ch == "'" and quote_string):
                j = i + 1
                while j < n and line[j] != ch:
                    j += 2 if line[j] == "\\" else 1
                buf.append(ch + " " * (min(j, n) - i - 1) + (ch if j < n else ""))
                i = j + 1
                continue
            if ch == "'":
                m = _CHAR.match(line, i)
                if m:
                    buf.append(" " * len(m.group()))
                    i = m.end()
                    continue
            if backtick and ch == "`":
                state = "`"
                buf.append("`")
                i += 1
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return out


_CONT_END = (",", "(", "=", "->", "=>", ":", "|", "&", "+", "\\")
_CONT_START = ("{", "throws", "where", "extends", "implements", ":", "->", "=>", "&&", "||", "?", ".", "const",
               "noexcept", "override", "final", "requires", ")")


def _brace_end(clean: list[str], i: int) -> tuple[int, bool]:
    """(last line, has a body) of the definition starting on line index i, by brace matching."""
    depth = paren = 0
    opened = False
    for k in range(i, len(clean)):
        line = clean[k]
        if not opened and k > i:
            prev, cur = clean[k - 1].strip(), line.strip()
            if not cur or k - i > 12:
                return i, False
            if paren == 0 and not prev.endswith(_CONT_END) and not cur.startswith(_CONT_START):
                return i, False
        for ch in line:
            if ch == "(":
                paren += 1
            elif ch == ")":
                paren = max(0, paren - 1)
            elif ch == "{" and (opened or paren == 0):
                depth += 1
                opened = True
            elif ch == "}" and opened:
                depth -= 1
                if depth == 0:
                    return k, True
            elif ch == ";" and not opened and paren == 0:
                return k, False
    return (len(clean) - 1, True) if opened else (i, False)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _indent_end(clean: list[str], i: int) -> int:
    """Python-like: the last non-blank line before the next line indented no deeper than line i."""
    base, last = _indent(clean[i]), i
    for k in range(i + 1, len(clean)):
        if not clean[k].strip():
            continue
        if _indent(clean[k]) <= base:
            break
        last = k
    return last


_RUBY_ONE_LINE = re.compile(r"(?:\bend\s*$)|^\s*def\s+\S+?(?:\([^)]*\))?\s*=(?!=)")


def _ruby_end(clean: list[str], i: int) -> int:
    if _RUBY_ONE_LINE.search(clean[i]):
        return i
    base = _indent(clean[i])
    for k in range(i + 1, len(clean)):
        s = clean[k].strip()
        if re.match(r"end\b", s) and _indent(clean[k]) <= base:
            return k
    return _indent_end(clean, i)


def _regex_symbols(lines: list[str], lang: str) -> list[list]:
    clean = _clean(lines, lang)
    specs = _specs(lang)
    found = []
    for i, line in enumerate(clean):
        if not line.strip():
            continue
        for rx, kind in specs:
            m = rx.match(line)
            if not m:
                continue
            groups = m.groupdict()
            name = groups["name"]
            if kind == "kw":
                kind = _KIND_ALIAS.get(groups["kw"], groups["kw"])
            elif kind == "function" and (groups.get("kw") or "").startswith("async"):
                kind = "async function"
            if groups.get("pre") is not None:
                first = groups["pre"].split()[0]
                if first in _KEYWORDS:
                    continue
            if name in _KEYWORDS:
                continue
            if groups.get("recv"):
                name = groups["recv"] + "." + name
            if "::" in name:
                name = name.replace("::", ".")
                kind = "method" if kind == "function" else kind
            if lang == "python":
                end, body = _indent_end(clean, i), True
            elif lang == "ruby":
                end, body = _ruby_end(clean, i), True
            else:
                end, body = _brace_end(clean, i)
            if not body and kind in CONTAINERS and lang in ("c", "cpp"):
                break  # a forward declaration or a variable of that type
            found.append([name, kind, i + 1, end + 1, _cap(lines[i], SIG_CHARS), ""])
            break
    return _nest(found)


def _nest(found: list[list]) -> list[list]:
    """Qualify members with their container (Class.method); drop definitions inside function bodies."""
    out, stack = [], []
    for sym in sorted(found, key=lambda s: (s[2], -s[3])):
        while stack and stack[-1][3] < sym[2]:
            stack.pop()
        if any(s[1] not in CONTAINERS for s in stack):
            continue
        parent = stack[-1] if stack else None
        if sym[1] == "method?":
            if parent is None:
                continue
            sym[1] = "method"
        if parent is not None:
            sym[0] = f"{parent[0]}.{sym[0]}"
            if sym[1] in ("function", "async function"):
                sym[1] = sym[1].replace("function", "method")
        out.append(sym)
        stack.append(sym)
    return out


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def parse_source(text: str, lang: str) -> list[list]:
    """Symbols as [name, kind, first line, last line, definition line, first docstring line]."""
    lines = _split_lines(text)
    if lang == "python":
        return _python_symbols(text, lines)
    return _regex_symbols(lines, lang)


# ---- the index ----

def _read_source(full: str) -> tuple[str | None, list[str], str]:
    """(text, lines, skip reason); text is None when the file is not indexed."""
    with open(full, "rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        return None, [], "too big"
    if b"\0" in data[:8192]:
        return None, [], "binary"
    text = data.decode("utf-8", errors="replace")
    lines = _split_lines(text)
    if any(len(ln) > MAX_LINE_CHARS for ln in lines):
        return None, [], "generated"
    return text, lines, ""


def _parse_file(full: str, st, lang: str) -> dict:
    entry = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
    text, lines, skip = _read_source(full)
    if text is None:
        entry["skip"] = skip
        return entry
    entry["lines"] = len(lines)
    entry["symbols"] = parse_source(text, lang)
    return entry


def _fresh(entry, st) -> bool:
    return isinstance(entry, dict) and entry.get("mtime_ns") == st.st_mtime_ns and entry.get("size") == st.st_size


def _git_files(root: Path) -> list[str] | None:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run(["git", "-c", "core.quotepath=off", "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard"], cwd=str(root), capture_output=True, timeout=GIT_TIMEOUT,
                           creationflags=flags)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if p.returncode != 0:
        return None
    return [f for f in p.stdout.decode("utf-8", errors="replace").split("\0") if f]


def _walk_files(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            if lang_of(name):
                out.append(name if rel_dir == "." else f"{rel_dir}/{name}".replace("\\", "/"))
        if len(out) > MAX_FILES:
            break
    return out


def list_files(root: Path) -> list[str]:
    """Project-relative (forward-slash) paths of the code files to index."""
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)
    keep = {f for f in files if lang_of(f) and not any(seg in SKIP_DIRS for seg in f.split("/")[:-1])}
    return sorted(keep)[:MAX_FILES]


def _load(mem: Path) -> dict:
    state = load_state(Path(mem) / INDEX_FILE)
    files = state.get("files")
    return files if state.get("version") == VERSION and isinstance(files, dict) else {}


def _save(mem: Path, files: dict) -> None:
    try:
        write_json_atomic(Path(mem) / INDEX_FILE, {"version": VERSION, "files": files})
    except OSError:
        pass  # another writer won; the cache is only a speed-up


def refresh(root: Path, mem: Path) -> dict:
    """Bring the whole index up to date (only changed files are parsed) and return path -> entry."""
    root = Path(root)
    old = _load(mem)
    files, changed = {}, False
    for rel in list_files(root):
        try:
            st = os.stat(root / rel)
        except (OSError, ValueError):
            continue
        entry = old.get(rel)
        if not _fresh(entry, st):
            try:
                entry = _parse_file(str(root / rel), st, lang_of(rel))
            except OSError:
                continue
            changed = True
        files[rel] = entry
    if changed or set(files) != set(old):
        _save(mem, files)
    return files


def _inside(root: Path, full: str) -> str | None:
    """Project-relative forward-slash path when `full` is inside `root`, else None."""
    base = os.path.normcase(os.path.realpath(root))
    target = os.path.realpath(full)
    try:
        if os.path.commonpath([base, os.path.normcase(target)]) != base:
            return None
    except ValueError:  # another drive
        return None
    rel = os.path.relpath(target, os.path.realpath(root)).replace("\\", "/")
    return None if rel == "." or rel.startswith("../") else rel


def resolve(root: Path, path: str) -> tuple[str, str]:
    """(absolute path, project-relative path) of a code file inside the project; ValueError otherwise."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    path = path.strip()
    full = path if os.path.isabs(path) else os.path.join(str(root), path)
    rel = _inside(Path(root), full)
    if rel is None:
        raise ValueError(f"{path} is outside the project")
    if not lang_of(rel):
        raise ValueError(f"{rel} is not a supported code file")
    if not os.path.isfile(full):
        raise ValueError(f"{rel} not found")
    return os.path.realpath(full), rel


def file_entry(root: Path, mem: Path, full: str, rel: str | None = None) -> dict:
    """The index entry of one file, from the cache when fresh (only this file is parsed and saved)."""
    st = os.stat(full)
    rel = rel if rel is not None else _inside(Path(root), full)
    files = _load(mem) if rel is not None else {}
    entry = files.get(rel) if rel is not None else None
    if not _fresh(entry, st):
        entry = _parse_file(full, st, lang_of(full))
        if rel is not None:
            files[rel] = entry
            _save(mem, files)
    return entry


# ---- text replies ----

def _range(sym) -> str:
    return f"L{sym[2]}{DASH}L{sym[3]}"


def outline_lines(entry: dict) -> list[str]:
    out = []
    for sym in entry.get("symbols") or []:
        line = f"{sym[0]} ({sym[1]}) {_range(sym)}  {sym[4]}"
        out.append(line + (f"  # {sym[5]}" if sym[5] else ""))
    return out


def _fit(lines: list[str], cap: int, more: str) -> str:
    """Join as many lines as fit in `cap` characters, ending with a note on what was left out."""
    out, used = [], 0
    for i, line in enumerate(lines):
        left = len(lines) - i
        note = more.format(n=left)
        if used + len(line) + 1 + (len(note) + 1 if left > 1 else 0) > cap:
            out.append(note)
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def outline_body(entry: dict, cap: int) -> str:
    lines = outline_lines(entry)
    if not lines:
        return "(no definitions found)"
    return _fit(lines, cap, "... and {n} more symbols (use code_find, or Read with offset and limit)")


def _skip_note(rel: str, entry: dict) -> str:
    return f"{rel} is not indexed ({entry['skip']})."


def outline(root: Path, mem: Path, path: str, cap: int = OUTLINE_CHARS) -> str:
    full, rel = resolve(root, path)
    entry = file_entry(root, mem, full, rel)
    if entry.get("skip"):
        return _skip_note(rel, entry)
    header = f"{rel} — {entry['lines']} lines, {len(entry['symbols'])} symbols"
    return header + "\n" + outline_body(entry, cap - len(header) - 1)


def find(root: Path, mem: Path, name: str, limit: int = 20) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer from 1 to 200")
    name = name.strip()
    low = name.casefold()
    hits = []
    for rel, entry in refresh(root, mem).items():
        for sym in entry.get("symbols") or []:
            full = sym[0]
            short = full.rsplit(".", 1)[-1]
            fl, sl = full.casefold(), short.casefold()
            if name in (full, short):
                rank = 0
            elif low in (fl, sl):
                rank = 1
            elif fl.startswith(low) or sl.startswith(low):
                rank = 2
            elif low in fl:
                rank = 3
            else:
                continue
            hits.append((rank, rel, sym[2], f"{rel}:{_range(sym)}  {full} ({sym[1]})  {sym[4]}"))
    if not hits:
        return f'Nothing found for "{name}".'
    hits.sort()
    shown = hits[:limit]
    header = f'{len(hits)} matches for "{name}"' + (f" (first {limit})" if len(hits) > limit else "") + ":"
    return header + "\n" + _fit([h[3] for h in shown], FIND_CHARS - len(header) - 1, "... and {n} more")


def read_symbol(root: Path, mem: Path, path: str, symbol: str, max_lines: int = READ_LINES) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    symbol = symbol.strip()
    full, rel = resolve(root, path)
    entry = file_entry(root, mem, full, rel)
    if entry.get("skip"):
        return _skip_note(rel, entry)
    syms = entry.get("symbols") or []
    matches = [s for s in syms if s[0] == symbol] or [s for s in syms if s[0].rsplit(".", 1)[-1] == symbol]
    if not matches:
        raise ValueError(f'No symbol "{symbol}" in {rel}; code_outline lists them')
    if len(matches) > 1 and len({s[0] for s in matches}) > 1:
        names = ", ".join(f"{s[0]} {_range(s)}" for s in matches[:10])
        return f'"{symbol}" matches several symbols in {rel}: {names}. Ask for one by its full name.'
    sym = matches[0]
    _text, lines, _skip = _read_source(full)
    start, end = sym[2], min(sym[3], len(lines))
    out = [f"{rel}  {sym[0]} ({sym[1]}) {_range(sym)}"]
    used = 0
    for n in range(start, end + 1):
        line = f"{n}\t{lines[n - 1]}"
        if n - start >= max_lines or used + len(line) > READ_CHARS:
            out.append(f"... truncated: {sym[0].rsplit('.', 1)[-1]} has {end - start + 1} lines; "
                       f"Read the rest with offset={n} and limit={end - n + 1}")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


# ---- big-file steering (PreToolUse Read) ----

def _count_lines(full: str) -> int:
    with open(full, "rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def big_read_steer(payload: dict, root: Path, mem: Path, now) -> tuple[str, dict] | None:
    """(deny reason, event) for a whole-file Read of a long code file the first time; else None."""
    if not big_reads_enabled():
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or "pages" in tool_input:
        return None
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path or not lang_of(file_path):
        return None
    full = file_path if os.path.isabs(file_path) else os.path.join(str(payload.get("cwd") or ""), file_path)
    if not os.path.isfile(full) or os.path.getsize(full) > MAX_FILE_BYTES or _count_lines(full) <= BIG_LINES:
        return None
    entry = file_entry(root, mem, full)
    if entry.get("skip") or entry.get("lines", 0) <= BIG_LINES:
        return None
    from memgraph import readguard

    if not readguard.deny_once(payload, mem, now):
        return None
    body = outline_body(entry, STEER_CHARS)
    event = {"type": "big_read_steer", "lines": entry["lines"], "outline_chars": len(body),
             "session": payload.get("session_id")}
    return STEER_REASON.format(n=entry["lines"], outline=body), event
