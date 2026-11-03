"""Hide secrets (API keys, tokens, passwords) in text before it is stored in memory.

Only the secret itself becomes HIDDEN; the words around it stay, so the memory still
reads well ("export GITHUB_TOKEN=[hidden]"). Normal prose, paths, git SHAs, UUIDs and
numbers are left alone. This is a best-effort filter, not a guarantee.
"""
from __future__ import annotations

import re

HIDDEN = "[hidden]"

_KEYWORDS = r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)"
_NAME = r"[A-Za-z0-9_]*" + _KEYWORDS + r"[A-Za-z0-9_]*"
_TOKEN_CHARS = r"[A-Za-z0-9._~+/=\-]"

# Whole-match secrets: the full match is the secret.
_WHOLE = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9_-])xox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"(?<![A-Za-z0-9_])(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"),
]

# Secrets after a marker: only the named group "s" is replaced.
_GROUPED = [
    # aws_secret_access_key = value (with or without spaces)
    (re.compile(r"(?i)aws_secret_access_key[\"']?\s*[=:]\s*[\"']?(?P<s>[A-Za-z0-9/+=]{20,})"), None),
    # Authorization: <scheme> <token>
    (re.compile(r"(?i)\bAuthorization\s*:\s*(?:Bearer|Basic|Token|Bot|Digest)\s+(?P<s>" + _TOKEN_CHARS + r"+)"), None),
    # Authorization: <token> (no scheme)
    (re.compile(r"(?i)\bAuthorization\s*:\s*(?P<s>" + _TOKEN_CHARS + r"{8,})"), "looks_secret"),
    # Bearer <token>
    (re.compile(r"(?i)\bBearer\s+(?P<s>" + _TOKEN_CHARS + r"{8,})"), "looks_secret"),
    # scheme://user:password@host
    (re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:(?P<s>[^\s/@]+)@"), None),
    # --password value, --token=value, --api-key 'value'
    (re.compile(r"(?i)(?<![A-Za-z0-9_-])--(?:password|passwd|token|api-key|apikey|api-token|access-token|auth-token|secret)"
                r"(?:=|\s+)(?:\"(?P<s>[^\"]*)\"|'(?P<s2>[^']*)'|(?P<s3>[^\s\"']+))"), "value"),
    # "api_key": "value" (JSON / YAML with quotes)
    (re.compile(r"(?i)[\"'](?P<n>" + _NAME + r")[\"']\s*:\s*\"(?P<s>[^\"]*)\""), "value"),
    # NAME=value, export NAME=value, $env:NAME = "value", set NAME=value
    (re.compile(r"(?i)(?<![A-Za-z0-9_])" + _NAME + r"(?:=(?!=)|\s*=(?!=)\s*(?=[\"']))"
                r"(?:\"(?P<s>[^\"]*)\"|'(?P<s2>[^']*)'|(?P<s3>[^\s\"'`;&|,)}]+))"), "value"),
]

_NOT_A_VALUE = re.compile(
    r"^(?:[-+]?\d+(?:\.\d+)?|true|false|null|none|yes|no|\$\w+|\$\{[^}]*\}|\$\([^)]*\)|%\w+%|<.*|\[hidden\])$",
    re.I)


def _looks_secret(value: str) -> bool:
    """A token-like word, not prose: has a digit, or is very long."""
    return (len(value) >= 8 and any(c.isdigit() for c in value)) or len(value) >= 32


def _is_value(value: str) -> bool:
    """An actual secret value, not empty, a number, a flag word or a variable reference."""
    return bool(value) and not _NOT_A_VALUE.match(value)


_CHECKS = {None: lambda v: True, "looks_secret": _looks_secret, "value": _is_value}


def _replace_groups(match: re.Match, check) -> str:
    whole, start = match.group(0), match.start()
    for name in ("s", "s2", "s3"):
        if name in match.re.groupindex and match.group(name) is not None:
            value = match.group(name)
            if not check(value):
                return whole
            a, b = match.span(name)
            return whole[: a - start] + HIDDEN + whole[b - start:]
    return whole


def redact(text) -> str:
    """`text` with every recognised secret replaced by HIDDEN."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if not text:
        return text
    for pattern in _WHOLE:
        text = pattern.sub(HIDDEN, text)
    for pattern, check in _GROUPED:
        text = pattern.sub(lambda m, c=_CHECKS[check]: _replace_groups(m, c), text)
    return text
