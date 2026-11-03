"""Secret redaction. Every secret below is a fake built from pieces, so no scanner
mistakes this file for a leak and no real credential is ever written here."""
import json
from datetime import datetime, timezone

import pytest

from memgraph.redact import HIDDEN, redact
from memgraph.transcript import parse_line

H = HIDDEN
A36 = "a1B2c3D4e5" * 3 + "f6G7h8"          # 36 mixed letters/digits
B40 = "0123456789abcdefABCDEF0123456789abcdefAB"  # 40 chars


@pytest.mark.parametrize("text, expected", [
    # Anthropic and OpenAI style keys
    ("key sk-" + "ant-api03-" + A36 + " ok", f"key {H} ok"),
    ("use sk-" + "proj-" + A36, f"use {H}"),
    ("sk-" + "A" * 20 + "1", H),
    # GitHub
    *[(f"push {p}_{A36} now", f"push {H} now") for p in ("ghp", "gho", "ghu", "ghs", "ghr")],
    ("github_" + "pat_11ABCDEFG0" + "_" + A36, H),
    # AWS
    ("id AKIA" + "ABCDEFGHIJKLMNOP end", f"id {H} end"),
    ("aws_secret_access_key=" + B40, f"aws_secret_access_key={H}"),
    ("aws_secret_access_key = " + B40, f"aws_secret_access_key = {H}"),
    # Slack, Google, Stripe
    ("xox" + "b-1234567890-abcdefABCDEF", H),
    ("xox" + "p-1234567890-abcdefABCDEF", H),
    ("AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q", H),
    ("sk_" + "live_" + A36, H),
    ("rk_" + "live_" + A36, H),
    # JWT
    ("token eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
     f"token {H}"),
    # PEM private key, also when cut off before the END line
    ("a\n-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA PRIVATE KEY-----\nb", f"a\n{H}\nb"),
    ("-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkq", H),
    ("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3Blbn\n-----END OPENSSH PRIVATE KEY-----", H),
    # Bearer / Authorization headers
    ('curl -H "Authorization: Bearer abc123def456ghi789" x', f'curl -H "Authorization: Bearer {H}" x'),
    ("Authorization: Basic dXNlcjpwYXNz", f"Authorization: Basic {H}"),
    ("authorization: tok_9f8e7d6c5b", f"authorization: {H}"),
    ("Bearer abcdef0123456789", f"Bearer {H}"),
    # URL credentials: only the password goes
    ("git clone https://me:s3cretPass@github.com/x/y.git", f"git clone https://me:{H}@github.com/x/y.git"),
    ("postgres://app:hunter2@db:5432/app", f"postgres://app:{H}@db:5432/app"),
    # CLI flags
    ("mysql --password=hunter2 db", f"mysql --password={H} db"),
    ("tool --token abc123 run", f"tool --token {H} run"),
    ("tool --api-key 'k-123' run", f"tool --api-key '{H}' run"),
    # NAME=value assignments
    ("API_KEY=abc123", f"API_KEY={H}"),
    ("export GITHUB_TOKEN=xyz789abc", f"export GITHUB_TOKEN={H}"),
    ('$env:OPENAI_API_KEY="hello-world"', f'$env:OPENAI_API_KEY="{H}"'),
    ("$env:MyPassword = 'p@ss word'", f"$env:MyPassword = '{H}'"),
    ("db_passwd=hunter2 next", f"db_passwd={H} next"),
    ("set CLIENT_SECRET=s3cr3t&& run", f"set CLIENT_SECRET={H}&& run"),
    ("GOOGLE_APPLICATION_CREDENTIALS=/home/x/key.json", f"GOOGLE_APPLICATION_CREDENTIALS={H}"),
    ('{"api_key": "abc123"}', f'{{"api_key": "{H}"}}'),
])
def test_secret_is_hidden(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize("text", [
    "Add a login page and fix the failing test in auth.py",
    "Use a Bearer token in the Authorization header for the API",
    "The password field must be at least 8 characters",
    "C:\\Users\\you\\Desktop\\project\\src\\main.py",
    "/home/you/project/src/token_store.py",
    "git show 4cef607a1b2c3d4e5f60718293a4b5c6d7e8f901",
    "session 550e8400-e29b-41d4-a716-446655440000.jsonl",
    "42010 tokens, 1.25 x input, $3.75",
    "SESSION_MEMORY_MIN_TOKENS=30000",
    "MAX_TOKENS=4096 and USE_TOKEN=true",
    "export API_KEY=$API_KEY; echo ${SECRET_KEY}; set TOKEN=%TOKEN%",
    "API_KEY=<your key>",
    "if token == expected: return",
    "https://github.com/ArpitThakur7/session-memory",
    "http://localhost:8080/path",
    "skills and sk-short",
    "task-ant-colony and desk-lamp",
    "python -m pytest -q -W error::SyntaxWarning",
    "",
])
def test_normal_text_is_kept(text):
    assert redact(text) == text


def test_redact_handles_non_strings():
    assert redact(None) == ""


def _user(content):
    return json.dumps({"type": "user", "sessionId": "s", "timestamp": "2026-11-01T10:00:00Z",
                       "message": {"role": "user", "content": content}})


def _assistant(content):
    return json.dumps({"type": "assistant", "sessionId": "s", "timestamp": "2026-11-01T10:00:00Z",
                       "message": {"role": "assistant", "content": content}})


SECRET = "ghp_" + A36


def _all_text(events) -> str:
    return json.dumps([e.data for e in events])


def test_transcript_hides_secrets_in_every_stored_text():
    lines = [
        _user("deploy with token " + SECRET),
        _user([{"type": "tool_result", "is_error": True, "tool_use_id": "t", "content": "bad " + SECRET}]),
        _assistant([{"type": "text", "text": "I used " + SECRET},
                    {"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": "export GH_TOKEN=" + SECRET}},
                    {"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": "/x/" + SECRET + ".txt"}},
                    {"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "/x/" + SECRET}},
                    {"type": "tool_use", "id": "t", "name": "TodoWrite",
                     "input": {"todos": [{"content": "rotate " + SECRET, "status": "pending"}]}}]),
        _assistant([{"type": "tool_use", "id": "w", "name": "mcp__ccd_turn__wrap_up",
                     "input": {"recap": "Rotated " + SECRET}}]),
        json.dumps({"type": "custom-title", "customTitle": "Key " + SECRET, "sessionId": "s"}),
    ]
    events = [e for line in lines for e in parse_line(line)]
    kinds = {e.kind for e in events}
    assert {"prompt", "error", "reply", "command", "edit", "read", "todo", "title"} <= kinds
    text = _all_text(events)
    assert "ghp_" not in text and A36 not in text
    assert text.count(H) >= 9


def test_secret_past_the_cut_is_still_hidden_not_half_kept():
    events = parse_line(_user("x" * 195 + " " + SECRET))
    assert events[0].data["text"] == "x" * 195 + " " + H[:4]


def test_remember_hides_secrets():
    from memgraph.graph import Graph
    from memgraph.memory_ops import remember

    g = Graph()
    remember(g, "Use key " + SECRET, datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert [n["label"] for n in g.nodes.values()] == [f"Use key {H}"]
