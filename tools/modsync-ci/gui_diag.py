"""Bounded, redacted worker diagnostics shared by the GUI lane drivers.

The GUI drivers run their real work in an isolated worker whose evidence is
sealed. When that worker dies without a report the plain CI log used to show
nothing. This module turns the worker's own stdout/stderr (and traceback) into
a short tail with secrets and machine paths removed, plus the exit status and
the phase the worker reached. It is only used on failure paths.
"""

import os
from pathlib import Path
import re
import signal

TAIL_LINES = 120
LINE_LIMIT = 400
READ_LIMIT = 256 * 1024
PHASES = ("launch", "preflight", "first_screenshot", "install")
PHASE_FILE = "phase.txt"
REDACTED = "[REDACTED]"

SECRET_ENV_NAME = re.compile(r"(?i)(token|secret|key|passw(?:or)?d|credential)")
SECRET_PATTERNS = (
    (re.compile(r"(?i)([\"']?(?:proxy-)?authorization[\"']?\s*[:=]\s*)[^\r\n]*"), r"\1" + REDACTED),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
    (re.compile(
        r"(?i)(\b[\w.-]*(?:key|token|secret|passw(?:or)?d)[\"']?\s*[=:]\s*[\"']?)"
        r"(?!\[REDACTED\])[^\s\"'&,;]{8,}"), r"\1" + REDACTED),
)
HOME_PATTERNS = (
    re.compile(r"/home/runner/work/_temp(?=[/\s:'\")\]]|$)"),
    re.compile(r"/home/runner/work/[^/\s]+/[^/\s]+"),
    re.compile(r"/(?:home|Users)/[^/\s:'\")\]]+"),
)
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def path_labels(home=None, paths=None, env=None):
    env = os.environ if env is None else env
    labels = {}
    for name, label in (("RUNNER_TEMP", "$RUNNER_TEMP"), ("GITHUB_WORKSPACE", "$GITHUB_WORKSPACE"),
                        ("RUNNER_TOOL_CACHE", "$RUNNER_TOOL_CACHE")):
        if env.get(name):
            labels[env[name]] = label
    for path, label in (paths or {}).items():
        labels[str(path)] = label
    home = home if home is not None else env.get("HOME")
    if home:
        labels[str(home)] = "~"
    return sorted(((path.rstrip("/"), label) for path, label in labels.items()
                   if len(path.rstrip("/")) > 3), key=lambda item: -len(item[0]))


def redact(text, home=None, paths=None, env=None):
    """Shorten $HOME/runner/known paths and mask anything secret-shaped."""
    env = os.environ if env is None else env
    text = str(text)
    for name, value in env.items():
        if len(value) >= 8 and SECRET_ENV_NAME.search(name) and "\n" not in value:
            text = text.replace(value, REDACTED)
    for path, label in path_labels(home, paths, env):
        text = re.sub(re.escape(path) + r"(?=[/\s:'\")\]]|$)", lambda _: label, text)
    for pattern in HOME_PATTERNS:
        text = pattern.sub(lambda match: "$RUNNER_TEMP" if match.group(0).endswith("_temp")
                           else "$WORKSPACE" if "/work/" in match.group(0) else "~", text)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def tail_lines(path, limit=TAIL_LINES, home=None, paths=None, env=None):
    """Last `limit` lines of a file, redacted before any line is shortened."""
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return []
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - READ_LIMIT))
            data = stream.read()
    except OSError:
        return []
    text = redact(data.decode("utf-8", errors="replace"), home, paths, env)
    lines = [CONTROL.sub("", line.replace("\r", "")) for line in text.splitlines()][-limit:]
    return [line if len(line) <= LINE_LIMIT else line[:LINE_LIMIT] + "..." for line in lines]


def mark_phase(private, name, stage=None):
    try:
        (Path(private) / PHASE_FILE).write_text(name + "\n" + (stage or "") + "\n", encoding="utf-8")
    except OSError:
        pass


def read_phase(private):
    """(phase, stage); a worker that never wrote the file died during launch."""
    try:
        lines = (Path(private) / PHASE_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return "launch", None
    phase = (lines[0].strip() if lines else "")[:64] or "launch"
    stage = (lines[1].strip() if len(lines) > 1 else "")[:80] or None
    return phase, stage


def describe_exit(code, timed_out=False):
    """Exit code and, when it encodes one, the signal (negative = direct, >128 = shell-style)."""
    info = {"exit_code": code, "signal": None, "signal_inferred": False, "timed_out": bool(timed_out)}
    if isinstance(code, int) and code != 0:
        number = -code if code < 0 else code - 128 if 128 < code <= 128 + 64 else 0
        if number:
            try:
                info["signal"] = signal.Signals(number).name
                info["signal_inferred"] = code > 0
            except ValueError:
                info["signal"] = "SIG%d" % number
                info["signal_inferred"] = code > 0
    return info


def collect(private, sources, code=None, timed_out=False, worker_result=False,
            error=None, home=None, paths=None, env=None):
    """Build the failure diagnostics. `sources` is [(label, filename, line_limit)]."""
    phase, stage = read_phase(private)
    diagnostics = {"phase": phase, "stage": stage, "worker_result_present": bool(worker_result)}
    diagnostics.update(describe_exit(code, timed_out))
    if error is not None:
        diagnostics["error_type"] = type(error).__name__
        if type(error).__name__ != "Blocked":
            diagnostics["error_detail"] = redact(str(error), home, paths, env)[:300]
    diagnostics["tails"] = {
        label: tail_lines(Path(private) / name, limit, home, paths, env)
        for label, name, limit in sources
    }
    return diagnostics


def summary(diagnostics):
    return "phase=%s stage=%s exit_code=%s signal=%s%s timed_out=%s worker_result=%s" % (
        diagnostics.get("phase"), diagnostics.get("stage"), diagnostics.get("exit_code"), diagnostics.get("signal"),
        " (inferred)" if diagnostics.get("signal_inferred") else "",
        str(bool(diagnostics.get("timed_out"))).lower(),
        "present" if diagnostics.get("worker_result_present") else "absent")


def render_group(diagnostics, title="gui worker tail"):
    """CI log lines; every tail line is prefixed so it can never read as a workflow command."""
    lines = ["::group::" + title, summary(diagnostics)]
    for key in ("error_type", "error_detail"):
        if diagnostics.get(key):
            lines.append("%s: %s" % (key, diagnostics[key]))
    for label, tail in diagnostics.get("tails", {}).items():
        lines.append("[%s] %d line(s)" % (label, len(tail)))
        lines.extend("[%s] %s" % (label, line) for line in tail)
    lines += ["::endgroup::", "gui worker diagnostics: " + summary(diagnostics)]
    return lines
