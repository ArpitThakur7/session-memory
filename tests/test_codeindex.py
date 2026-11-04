"""Code outlines: symbol index, MCP code tools, CLI and big-file steering in the Read hook."""
import io
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from memgraph import codeindex, mcp_server
from memgraph.cli import main
from memgraph.store import memory_dir

T0 = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)


def spans(symbols):
    return [(s[0], s[1], s[2], s[3]) for s in symbols]


PY = '''"""Module doc."""
import os


def top(a, b=1):
    """Add things.

    More."""
    return a + b


class Shape:
    """A shape."""

    def area(self):
        return 0

    @property
    def name(self):
        return "s"

    async def load(self):
        pass


async def fetch():
    pass
'''


def test_python_outline_exact_ranges():
    syms = codeindex.parse_source(PY, "python")
    assert spans(syms) == [
        ("top", "function", 5, 9),
        ("Shape", "class", 12, 23),
        ("Shape.area", "method", 15, 16),
        ("Shape.name", "method", 18, 20),
        ("Shape.load", "async method", 22, 23),
        ("fetch", "async function", 26, 27),
    ]
    by = {s[0]: s for s in syms}
    assert by["top"][4] == "def top(a, b=1):" and by["top"][5] == "Add things."
    assert by["Shape"][5] == "A shape."
    assert by["Shape.name"][4] == "def name(self):"
    assert by["Shape.load"][4] == "async def load(self):"


def test_python_syntax_error_falls_back_to_regex():
    src = "def ok():\n    return 1\n\n\nclass K:\n    def m(self):\n        pass\n\ndef broken(:\n    pass\n"
    assert spans(codeindex.parse_source(src, "python")) == [
        ("ok", "function", 1, 2), ("K", "class", 5, 7), ("K.m", "method", 6, 7), ("broken", "function", 9, 10)]


def test_python_invalid_escape_does_not_warn():
    # the suite runs with -W error::SyntaxWarning; parsing user code must never raise it
    src = 'import re\n\ndef f():\n    return re.compile("\\d+")\n'
    assert spans(codeindex.parse_source(src, "python")) == [("f", "function", 3, 4)]


TS = '''import x from "y";

export function add(a: number, b: number): number {
  return a + b; // }
}

export const mul = (a, b) => {
  const s = "}{";
  return a * b;
};

const inc = (a) => a + 1;

export interface Point {
  x: number;
  y: number;
}

export class Box extends Base {
  private size = 0;

  constructor(size: number) {
    super();
    this.size = size;
  }

  async grow(by: number): Promise<void> {
    if (by > 0) {
      this.size += by;
    }
  }
}

/* function fake() { */
export default function () {}
'''


def test_js_ts_heuristics_exact():
    assert codeindex.lang_of("a/b.tsx") == codeindex.lang_of("x.js") == "js"
    syms = codeindex.parse_source(TS, "js")
    assert spans(syms) == [
        ("add", "function", 3, 5),
        ("mul", "function", 7, 10),
        ("inc", "function", 12, 12),
        ("Point", "interface", 14, 17),
        ("Box", "class", 19, 32),
        ("Box.constructor", "method", 22, 25),
        ("Box.grow", "method", 27, 31),
    ]
    assert syms[0][4] == "export function add(a: number, b: number): number {"


GO = '''package main

import "fmt"

type Server struct {
	Name string
}

type Handler interface {
	Serve() error
}

func NewServer(name string) *Server {
	return &Server{Name: name}
}

func (s *Server) Start() error {
	fmt.Println("start {")
	return nil
}

func main() {
	s := NewServer(`x}`)
	_ = s
}
'''


def test_go_heuristics_exact():
    assert spans(codeindex.parse_source(GO, "go")) == [
        ("Server", "struct", 5, 7),
        ("Handler", "interface", 9, 11),
        ("NewServer", "function", 13, 15),
        ("Server.Start", "method", 17, 20),
        ("main", "function", 22, 25),
    ]


JAVA = '''package demo;

import java.util.List;

public class Greeter {
    private final String name;

    public Greeter(String name) {
        this.name = name;
    }

    public String greet(List<String> others) {
        if (others.isEmpty()) {
            return "hi " + name;
        }
        return "hi all";
    }

    abstract void later();

    interface Listener {
        void on(String e);
    }
}

enum Color { RED, GREEN }
'''


def test_java_heuristics_exact():
    assert spans(codeindex.parse_source(JAVA, "java")) == [
        ("Greeter", "class", 5, 24),
        ("Greeter.Greeter", "method", 8, 10),
        ("Greeter.greet", "method", 12, 17),
        ("Greeter.later", "method", 19, 19),
        ("Greeter.Listener", "interface", 21, 23),
        ("Greeter.Listener.on", "method", 22, 22),
        ("Color", "enum", 26, 26),
    ]


RUST = '''pub struct Point {
    x: i32,
}

impl fmt::Display for Point {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.x)
    }
}

pub fn origin() -> Point {
    Point { x: 0 }
}
'''

RUBY = '''class Dog
  def bark
    puts "woof"
  end

  def self.make = new
end
'''


def test_other_languages_reasonable():
    assert spans(codeindex.parse_source(RUST, "rust")) == [
        ("Point", "struct", 1, 3), ("Point", "impl", 5, 9), ("Point.fmt", "method", 6, 8),
        ("origin", "function", 11, 13)]
    assert spans(codeindex.parse_source(RUBY, "ruby")) == [
        ("Dog", "class", 1, 7), ("Dog.bark", "method", 2, 4), ("Dog.make", "method", 6, 6)]
    c = "#include <stdio.h>\n\nstruct node;\n\nstatic int add(int a, int b)\n{\n    return a + b;\n}\n"
    assert spans(codeindex.parse_source(c, "c")) == [("add", "function", 5, 8)]


def test_outline_lines_format():
    entry = {"lines": 27, "symbols": codeindex.parse_source(PY, "python")}
    lines = codeindex.outline_lines(entry)
    assert lines[0] == "top (function) L5-L9  def top(a, b=1):  # Add things."
    assert lines[2] == "Shape.area (method) L15-L16  def area(self):"


# ---- index, cache ----

@pytest.fixture
def proj(tmp_path, monkeypatch):
    for var in ("SESSION_MEMORY_READ_GUARD", "SESSION_MEMORY_BIG_READS"):
        monkeypatch.delenv(var, raising=False)
    p = tmp_path / "proj"
    (p / "src").mkdir(parents=True)
    (p / "src" / "shapes.py").write_text(PY, encoding="utf-8")
    (p / "src" / "box.ts").write_text(TS, encoding="utf-8")
    (p / "notes.md").write_text("# not code\n", encoding="utf-8")
    return p


def test_index_skips_ignored_binary_big_and_minified(proj):
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "dep.js").write_text("function dep() {}\n", encoding="utf-8")
    (proj / "blob.c").write_bytes(b"int x;\0\0\0")
    (proj / "big.js").write_text("// x\n" * 220_000, encoding="utf-8")  # > 1 MB
    (proj / "min.js").write_text("function a(){}" + ";" * 2100 + "\n", encoding="utf-8")
    files = codeindex.refresh(proj, memory_dir(proj))
    assert sorted(f for f, e in files.items() if not e.get("skip")) == ["src/box.ts", "src/shapes.py"]
    assert "node_modules/dep.js" not in files
    assert files["blob.c"]["skip"] == "binary" and files["min.js"]["skip"] == "generated"
    assert "big.js" not in files or files["big.js"]["skip"] == "too big"


def test_cache_reused_and_incremental(proj, monkeypatch):
    mem = memory_dir(proj)
    codeindex.refresh(proj, mem)
    assert (mem / "codeindex.json").is_file()
    calls = []
    real = codeindex.parse_source
    monkeypatch.setattr(codeindex, "parse_source", lambda text, lang: calls.append(lang) or real(text, lang))
    codeindex.refresh(proj, mem)
    assert calls == []
    path = proj / "src" / "shapes.py"
    path.write_text(PY + "\ndef extra():\n    pass\n", encoding="utf-8")
    files = codeindex.refresh(proj, mem)
    assert calls == ["python"]
    assert "extra" in [s[0] for s in files["src/shapes.py"]["symbols"]]
    (proj / "src" / "box.ts").unlink()
    assert "src/box.ts" not in codeindex.refresh(proj, mem)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_ls_files_used_in_a_repo(proj):
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    (proj / ".gitignore").write_text("gen/\n", encoding="utf-8")
    (proj / "gen").mkdir()
    (proj / "gen" / "out.py").write_text("def g():\n    pass\n", encoding="utf-8")
    files = codeindex.refresh(proj, memory_dir(proj))
    assert sorted(files) == ["src/box.ts", "src/shapes.py"]


# ---- text API used by MCP and CLI ----

def test_outline_refuses_paths_outside_project(proj, tmp_path):
    other = tmp_path / "other.py"
    other.write_text("def x():\n    pass\n", encoding="utf-8")
    mem = memory_dir(proj)
    for bad in (str(other), "../other.py"):
        with pytest.raises(ValueError, match="outside the project"):
            codeindex.outline(proj, mem, bad)
    with pytest.raises(ValueError, match="not a supported code file"):
        codeindex.outline(proj, mem, "notes.md")
    text = codeindex.outline(proj, mem, str(proj / "src" / "shapes.py"))
    assert text.splitlines()[0] == "src/shapes.py - 27 lines, 6 symbols"


def test_outline_capped(proj):
    body = "".join(f"def function_number_{i}(argument_one, argument_two):\n    pass\n" for i in range(300))
    (proj / "many.py").write_text(body, encoding="utf-8")
    text = codeindex.outline(proj, memory_dir(proj), "many.py")
    assert len(text) <= 4000
    assert "more symbols" in text.splitlines()[-1]


def test_find_orders_exact_prefix_substring(proj):
    (proj / "more.py").write_text("def area_total():\n    pass\n\ndef garea():\n    pass\n", encoding="utf-8")
    text = codeindex.find(proj, memory_dir(proj), "area")
    names = [line.split("  ")[1].split(" (")[0] for line in text.splitlines()[1:]]
    assert names == ["Shape.area", "area_total", "garea"]
    assert text.splitlines()[1].startswith("src/shapes.py:L15-L16  Shape.area (method)")
    assert "Nothing found" in codeindex.find(proj, memory_dir(proj), "zzzz")
    assert len(codeindex.find(proj, memory_dir(proj), "a", limit=2).splitlines()) == 3


def test_read_symbol_lines_and_truncation(proj):
    mem = memory_dir(proj)
    text = codeindex.read_symbol(proj, mem, "src/shapes.py", "Shape.name")
    lines = text.splitlines()
    assert lines[0] == "src/shapes.py  Shape.name (method) L18-L20"
    assert lines[1:] == ["18\t    @property", "19\t    def name(self):", '20\t        return "s"']
    assert codeindex.read_symbol(proj, mem, "src/shapes.py", "area").splitlines()[1] == "15\t    def area(self):"
    with pytest.raises(ValueError, match="No symbol"):
        codeindex.read_symbol(proj, mem, "src/shapes.py", "nope")
    big = "def huge():\n" + "    x = 1\n" * 500
    (proj / "huge.py").write_text(big, encoding="utf-8")
    out = codeindex.read_symbol(proj, mem, "huge.py", "huge").splitlines()
    assert len(out) == 1 + 400 + 1
    assert out[-1].startswith("... truncated: huge has 501 lines")


# ---- MCP tools ----

def drive(project, *msgs):
    data = b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in msgs)
    out = io.BytesIO()
    mcp_server.serve(io.BytesIO(data), out, project)
    return [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]


def call(i, tool, **args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": tool, "arguments": args}}


def test_mcp_code_tools(proj, tmp_path):
    outline, find, read, outside, bad = drive(
        proj, call(1, "code_outline", path="src/box.ts"), call(2, "code_find", name="grow"),
        call(3, "code_read", path="src/box.ts", symbol="Box.grow"),
        call(4, "code_outline", path=str(tmp_path)), call(5, "code_find", name="x", limit=0))
    assert outline["result"]["isError"] is False
    assert "Box.grow (method) L27-L31" in outline["result"]["content"][0]["text"]
    assert "src/box.ts:L27-L31  Box.grow (method)" in find["result"]["content"][0]["text"]
    assert read["result"]["content"][0]["text"].splitlines()[1] == "27\t  async grow(by: number): Promise<void> {"
    assert outside["result"]["isError"] is True and "outside the project" in outside["result"]["content"][0]["text"]
    assert bad["result"]["isError"] is True


# ---- big-file steering in the Read hook ----

def big_file(proj, n_funcs=400, name="big.py"):
    path = proj / name
    path.write_text("".join(f"def f{i}():\n    return {i}\n" for i in range(n_funcs)), encoding="utf-8")
    return path


def payload(proj, path, session="s1", **tool):
    return {"session_id": session, "transcript_path": str(proj / "t.jsonl"), "cwd": str(proj),
            "hook_event_name": "PreToolUse", "tool_name": "Read",
            "tool_input": {"file_path": str(path), **tool}}


def run(name, data, now):
    out = io.StringIO()
    assert main([name], io.StringIO(json.dumps(data)), out, now=now) == 0
    return out.getvalue()


def read(data, now=T0):
    text = run("hook-pre-read", data, now)
    if not text:
        assert run("hook-post-read", dict(data, hook_event_name="PostToolUse"), now) == ""
    return text


def reason_of(text):
    return json.loads(text)["hookSpecificOutput"]["permissionDecisionReason"] if text else None


def test_big_code_read_steered_once_then_allowed(proj):
    path = big_file(proj)
    reason = reason_of(read(payload(proj, path)))
    assert reason.startswith("This file has 800 lines. Its outline:\nf0 (function) L1-L2  def f0():\n")
    assert reason.endswith("\nRead the lines you need with offset and limit (or the code_read tool if it is "
                           "available). To read the whole file anyway, read it again within 2 minutes.")
    assert len(reason) <= 3000
    assert reason.rsplit("\nRead the lines", 1)[0].endswith(" more symbols \u2014 use code_outline")
    outline = reason.split("Its outline:\n", 1)[1].rsplit("\nRead the lines", 1)[0]
    assert len(outline) <= 3000 and "more symbols" in outline
    assert read(payload(proj, path), T0 + timedelta(seconds=60)) == ""  # the escape hatch
    # read and unchanged now: the read guard takes over
    assert reason_of(read(payload(proj, path), T0 + timedelta(minutes=5))).startswith("Not re-read:")
    events = [json.loads(l) for l in (memory_dir(proj) / "events.jsonl").read_text().splitlines() if l]
    steer = [e for e in events if e["type"] == "big_read_steer"]
    assert len(steer) == 1 and steer[0]["lines"] == 800 and steer[0]["outline_chars"] == len(outline)


def test_steering_retry_after_two_minutes_is_steered_again(proj):
    path = big_file(proj)
    assert reason_of(read(payload(proj, path)))
    assert reason_of(read(payload(proj, path), T0 + timedelta(minutes=3)))


def test_steering_skips_ranges_small_and_non_code(proj, monkeypatch):
    path = big_file(proj)
    assert read(payload(proj, path, offset=1, limit=100)) == ""
    small = big_file(proj, n_funcs=300, name="small.py")  # exactly 600 lines
    assert read(payload(proj, small)) == ""
    notes = proj / "long.md"
    notes.write_text("line\n" * 900, encoding="utf-8")
    assert read(payload(proj, notes)) == ""


def test_steering_env_off_and_independent_of_read_guard(proj, monkeypatch):
    path = big_file(proj)
    monkeypatch.setenv("SESSION_MEMORY_READ_GUARD", "0")
    assert reason_of(read(payload(proj, path))).startswith("This file has 800 lines.")
    assert read(payload(proj, path), T0 + timedelta(seconds=30)) == ""
    monkeypatch.setenv("SESSION_MEMORY_BIG_READS", "0")
    assert read(payload(proj, big_file(proj, name="other.py"))) == ""


def test_cli_outline_and_find_symbol(proj, monkeypatch):
    monkeypatch.chdir(proj)
    out = io.StringIO()
    assert main(["outline", "src/shapes.py"], io.StringIO(""), out) == 0
    assert out.getvalue().startswith("src/shapes.py - 27 lines, 6 symbols\n")
    out = io.StringIO()
    assert main(["find-symbol", "fetch"], io.StringIO(""), out) == 0
    assert "src/shapes.py:L26-L27  fetch (async function)" in out.getvalue()
    out = io.StringIO()
    assert main(["outline", "../elsewhere.py"], io.StringIO(""), out) == 2
    assert "outside the project" in out.getvalue()
    assert main(["outline"], io.StringIO(""), io.StringIO()) == 2


def test_cli_extension_list_matches_index():
    from memgraph import cli

    assert cli._CODE_EXTS == frozenset(codeindex.LANGS)


def test_whole_reason_capped_for_long_names(proj):
    long = "".join(f"def a_rather_long_function_name_number_{i:04d}(first_argument, second_argument, third):\n"
                   f"    return {i}\n" for i in range(400))
    (proj / "long.py").write_text(long, encoding="utf-8")
    reason = reason_of(read(payload(proj, proj / "long.py")))
    assert len(reason) <= 3000 and "more symbols \u2014 use code_outline" in reason


def test_rendered_ranges_are_ascii(proj, monkeypatch):
    monkeypatch.chdir(proj)
    for argv in (["outline", "src/shapes.py"], ["find-symbol", "area"]):
        out = io.StringIO()
        assert main(argv, io.StringIO(""), out) == 0
        assert out.getvalue().isascii() and "L15-L16" in out.getvalue()
    text = codeindex.read_symbol(proj, memory_dir(proj), "src/shapes.py", "area")
    assert text.splitlines()[0] == "src/shapes.py  Shape.area (method) L15-L16"
