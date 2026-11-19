#!/usr/bin/env python3
"""Plant one bug at a time in a temp copy of exit_interview.py and run its tests.

A mutation is caught only when that run reports at least one assertion failure.
An error without a failure is crashed. A clean run is survived.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "exit_interview.py"
PYTHON = sys.executable

# Each item is (name, old text, new text). Applied to the original, one at a time.
MUTATIONS = [
    ("drop-p", ', "-p"', ""),
    ("drop-resume", ', "--resume"', ""),
    ("drop-fork-session", ', "--fork-session"', ""),
    ("drop-no-session-persistence", '"--no-session-persistence", ', ""),
    ("drop-model", ', "--model", model', ""),
    ("weaken-tools", ', "--tools", ""', ', "--tools", "Bash"'),
    ("drop-strict-mcp-config", '"--strict-mcp-config", ', ""),
    ("drop-settings", ', "--settings"', ""),
    ("hooks-not-disabled", '\'{"disableAllHooks":true}\'', '\'{"disableAllHooks":false}\''),
    ("drop-path", '"PATH", ', ""),
    ("drop-config-dir", '"CLAUDE_CONFIG_DIR", ', ""),
    ("drop-api-key", '"ANTHROPIC_API_KEY", ', ""),
    ("drop-oauth-token", ', "CLAUDE_CODE_OAUTH_TOKEN"', ""),
    ("transcript-check-always-clean", 'return MARK.encode("utf-8") in gained', "return False"),
    ("copy-check-always-clean", "return sorted(found)", "return []"),
    ("ignore-offset", "handle.seek(offset)", "handle.seek(0)"),
    (
        "copy-check-ignores-mark",
        '        if MARK.encode("utf-8") in tail:\n            found.append(path.name)',
        "        found.append(path.name)",
    ),
    (
        "skip-checks-after-timeout",
        "timeout_reason, None, True, _collect(transcript, offset, names)",
        "timeout_reason, None, True, {}",
    ),
    ("exit-0-becomes-1", "return 0  # finished", "return 1  # finished"),
    ("exit-1-becomes-0", "return 1  # every session could not", "return 0  # every session could not"),
    ("exit-2-becomes-0", "return 2  # bad arguments", "return 0  # bad arguments"),
    ("exit-3-becomes-0", "return 3  # leak", "return 0  # leak"),
    ("exit-4-becomes-0", "return 4  # unchecked", "return 0  # unchecked"),
    ("skip-existing-note", "if note_path.is_file():", "if False:"),
    (
        "skip-atomic-rename",
        "os.replace(temporary, note_path)",
        'note_path.write_text(body, encoding="utf-8")',
    ),
    ("skip-folder-recovery", "for _ in range(60):", "for _ in range(0):"),
    ("dry-run-runs", "if args.dry_run:", "if False:"),
    (
        "limit-stops-only-the-command",
        "os.killpg(proc.pid, signal.SIGKILL)",
        "os.kill(proc.pid, signal.SIGKILL)",
    ),
    ("command-shares-our-session", "start_new_session=True", "start_new_session=False"),
    ("command-inherits-stdin", "stdin=subprocess.DEVNULL", "stdin=None"),
    ("terminate-leaves-the-command", "signal.signal(signal.SIGTERM, _on_terminate)", "None"),
    (
        "bare-name-found-in-current-folder",
        "if named:",
        "if named or Path(name).is_file():",
    ),
    ("relative-hit-left-relative", "return os.path.abspath(found)", "return found"),
    ("zero-timeout-accepted", "or number <= 0 or", "or number < 0 or"),
    ("any-failure-exits-1", "if rows and failed == len(rows):", "if failed:"),
    (
        "path-with-bad-name-accepted",
        "if session_id_of(text) and path.is_file():",
        "if path.is_file():",
    ),
    (
        "unchecked-ignores-copies",
        'or row.get("copies_written") is None)]',
        "or False)]",
    ),
    ("pass-env-ignored", "scrubbed_env(extra)", "scrubbed_env()"),
    ("stop-skips-the-summary", "_write_summary(out_dir, rows, stopped=True)", "None"),
    ("stop-skips-the-check", "checks = _collect(transcript, offset, names)", "checks = {}"),
    ("stop-hides-the-leak", "show_leak = bool(leaked)", "show_leak = False"),
    ("stopped-flag-missing", 'payload["stopped"] = True', "None"),
    ("interrupt-exit-code", "return 130", "return 1"),
    (
        "interrupt-says-nothing",
        "        _say_stopped()\n        return 130",
        "        return 130",
    ),
    (
        "hang-up-not-handled",
        "signal.signal(signal.SIGHUP, _on_hangup)",
        "signal.signal(signal.SIGHUP, signal.SIG_DFL)",
    ),
    (
        "hang-up-overrides-nohup",
        "if previous_hup != signal.SIG_IGN:",
        "if previous_hup == previous_hup:",
    ),
    ("huge-timeout-accepted", "if timeout > _MAX_TIMEOUT:", "if False:"),
    ("timeout-ceiling-off-by-one", "if timeout > _MAX_TIMEOUT:", "if timeout >= _MAX_TIMEOUT:"),
    (
        "late-stop-skips-the-check",
        "found = _collect(transcript, offset, names)",
        "found = {}",
    ),
    (
        "late-stop-loses-the-row",
        '_pending_stop = _row(session_id, "could_not", "stopped", None, True, found)',
        "_pending_stop = None",
    ),
    ("stop-leaves-the-temp-file", "os.remove(temporary)", "None"),
]


def preflight(source):
    bad = False
    for name, old, new in MUTATIONS:
        count = source.count(old)
        if count != 1 or old == new:
            print("%s matches %s times" % (name, count), file=sys.stderr)
            bad = True
    return bad


def judge(output, returncode):
    if returncode == 0:
        return "survived"
    closing = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("OK") or stripped.startswith("FAILED"):
            closing = stripped
    match = re.search(r"failures=(\d+)", closing)
    if match and int(match.group(1)) > 0:
        return "caught"
    return "crashed"


def stage(temp, mutated):
    (temp / "exit_interview.py").write_text(mutated, encoding="utf-8")
    tests = temp / "tests"
    tests.mkdir()
    for name in ("test_exit_interview.py", "test_demo.py"):
        shutil.copy2(str(ROOT / "tests" / name), str(tests / name))
    shutil.copytree(str(ROOT / "examples"), str(temp / "examples"))


def _stop_suite(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def run_tests(temp):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen(
        [PYTHON, "-m", "unittest", "discover", "-s", "tests"],
        cwd=str(temp),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        _stop_suite(proc)
        return "timed out"
    return judge((out or "") + "\n" + (err or ""), proc.returncode)


def main():
    source = SOURCE.read_text(encoding="utf-8")
    if preflight(source):
        return 2
    clean = tempfile.mkdtemp(prefix="exit-mutate-")
    try:
        stage(Path(clean), source)
        status = run_tests(Path(clean))
    finally:
        shutil.rmtree(clean, ignore_errors=True)
    if status != "survived":
        print("the clean copy does not pass", file=sys.stderr)
        return 2
    caught = 0
    for name, old, new in MUTATIONS:
        mutated = source.replace(old, new, 1)
        temp = tempfile.mkdtemp(prefix="exit-mutate-")
        try:
            stage(Path(temp), mutated)
            status = run_tests(Path(temp))
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        if status == "caught":
            caught += 1
        print("%s %s" % (name, status))
        sys.stdout.flush()
    total = len(MUTATIONS)
    print("caught %s of %s" % (caught, total))
    return 0 if caught == total else 1


if __name__ == "__main__":
    sys.exit(main())
