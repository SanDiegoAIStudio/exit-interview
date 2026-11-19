#!/usr/bin/env python3
"""Ask a closing Claude Code session for what it has not written down."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MARK = "EXIT INTERVIEW. This session is about to be closed."

QUESTION = """EXIT INTERVIEW. This session is about to be closed. Your whole conversation is already saved on disk and can be reopened at any time, so nothing recorded is at risk. Before it closes, answer four questions in your own voice.

1. What are you carrying that is not yet written to any file: intentions, half-made decisions, warnings, context you were relying on, open threads with people?
2. What is the one thing the next session, or the person you work for, must not lose?
3. Is any promise made to that person in this conversation still unmet?
4. In one line: what was this conversation really about, beneath its task?

Plain sentences, under 250 words, no headers. Do not summarize work that is already saved to disk. If everything you know is already on disk, say exactly that in one line."""

_SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "TERM")
_SIGN_IN_ENV = ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
_DRY_RUN_OK = 0
_SUMMARY_FAILED = 1


def build_command(claude, session_id, model):
    """The one resume command: no screen, no tools, hooks off, fork not saved."""
    return [claude, "-p", "--resume", session_id, "--fork-session",
            "--no-session-persistence", "--model", model, "--tools", "",
            "--strict-mcp-config", "--settings", '{"disableAllHooks":true}', QUESTION]


def scrubbed_env(extra=None):
    """Names the resume command may see. Nothing else is passed on."""
    names = _BASE_ENV + _SIGN_IN_ENV
    if extra:
        names = names + tuple(name for name in extra if name not in names)
    return {name: os.environ[name] for name in names if os.environ.get(name)}


def session_id_of(session):
    """UUID from a session id or a transcript path, or None."""
    text = str(session)
    name = Path(text).name
    stem = name[:-len(".jsonl")] if name.endswith(".jsonl") else text
    if name.endswith(".jsonl") and not _SESSION_ID.match(stem):
        return None
    return stem if _SESSION_ID.match(stem) else None


def find_transcript(session, projects):
    """The path given, or PROJECTS/*/<id>.jsonl. None when there is no file."""
    text = os.path.expanduser(str(session))
    path = Path(text)
    if path.name.endswith(".jsonl"):
        if session_id_of(text) and path.is_file():
            return path
        return None
    if not session_id_of(text):
        return None
    root = Path(projects)
    try:
        matches = sorted(item for item in root.glob("*/%s.jsonl" % text) if item.is_file())
    except OSError:
        return None
    return matches[0] if matches else None


def session_folder(transcript):
    """First cwd in the first 60 lines, or None. Non-JSON lines are skipped."""
    try:
        handle = open(transcript, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for _ in range(60):
            line = handle.readline()
            if not line:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                cwd = record.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    return None


def wrote_into(path, offset):
    """True or False for MARK after offset. None if the file is gone or shorter."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() < offset:
                return None
            handle.seek(offset)
            gained = handle.read()
    except OSError:
        return None
    return MARK.encode("utf-8") in gained


def new_copies(folder, before):
    """New .jsonl names holding MARK, [] if none, None if the folder cannot be read."""
    folder = Path(folder)
    try:
        if not folder.is_dir():
            return None
        current = [item for item in folder.glob("*.jsonl") if item.is_file()]
    except OSError:
        return None
    found = []
    known = set(before)
    for path in current:
        if path.name in known:
            continue
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 1000000))
                tail = handle.read()
        except OSError:
            return None
        if MARK.encode("utf-8") in tail:
            found.append(path.name)
    return sorted(found)


def _collect(transcript, offset, names):
    if offset is None or names is None:
        return {"transcript_written": None, "copies_written": None}
    return {"transcript_written": wrote_into(transcript, offset),
            "copies_written": new_copies(transcript.parent, names)}


def _row(session_id, state, reason, length, called, checks):
    row = {"id": session_id, "state": state, "reason": reason, "length": length,
           "called": called, "transcript_written": None, "copies_written": None}
    if called:
        row.update(checks)
    return row


def _said(proc):
    parts = []
    for item in (getattr(proc, "stderr", None), getattr(proc, "stdout", None)):
        if isinstance(item, bytes):
            item = item.decode("utf-8", "replace")
        text = str(item).strip() if item else ""
        if text:
            parts.append(text)
    return "\n".join(parts)[:300]


def _timeout_text(timeout):
    return str(int(timeout)) if timeout == int(timeout) else str(timeout)


def _write_note(note_path, session_id, model, reply):
    """Temp name, then rename, so a half-written note never exists."""
    note_path.parent.mkdir(parents=True, exist_ok=True)
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    heading = "# %s %s %s no tools, hooks off, fork not saved" % (session_id, when, model)
    body = heading + "\n\n---\n\n" + reply + "\n"
    temporary = note_path.with_name(note_path.name + ".tmp")
    try:
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, note_path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _snapshot(transcript):
    try:
        offset = transcript.stat().st_size
        names = {item.name for item in transcript.parent.glob("*.jsonl")}
    except OSError:
        return None, None
    return offset, names


def _kill_group(proc):
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()


def _run(command, folder, timeout, extra=None):
    with subprocess.Popen(
        command, cwd=folder, env=scrubbed_env(extra), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", start_new_session=True) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:
            _kill_group(proc)
            raise
    return subprocess.CompletedProcess(command, proc.returncode, out, err)


def interview(session, out_dir, projects, claude, model, timeout, extra=None):
    """One session. `called` is false when no command ran; summary.json omits it."""
    session_id = session_id_of(session) or str(session)
    transcript = find_transcript(session, projects)
    if transcript is None:
        reason = "no transcript for this session under %s" % projects
        return _row(session_id, "could_not", reason, None, False, None)
    note_path = Path(out_dir) / ("%s.md" % session_id[:8])
    if note_path.is_file():
        return _row(session_id, "no_work", "already has a note", None, False, None)
    folder = session_folder(transcript)
    if not folder:
        return _row(session_id, "could_not", "no cwd recorded for this session", None, False, None)
    if not Path(folder).is_dir():
        return _row(session_id, "could_not", "folder is gone: %s" % folder, None, False, None)
    offset, names = _snapshot(transcript)

    def finish(state, reason, length):
        return _row(session_id, state, reason, length, True, _collect(transcript, offset, names))

    command = build_command(claude, session_id, model)
    try:
        proc = _run(command, folder, timeout, extra)
    except subprocess.TimeoutExpired as error:
        timeout_reason = "timeout after %s seconds" % _timeout_text(timeout)
        said = _said(error)
        if said:
            timeout_reason = "%s: %s" % (timeout_reason, said)
        return _row(session_id, "could_not", timeout_reason, None, True, _collect(transcript, offset, names))
    except OSError as error:
        return finish("could_not", ("could not start: %s" % error)[:300], None)
    except (KeyboardInterrupt, SystemExit) as error:
        if not _is_stop(error):
            raise
        global _pending_stop
        checks = _collect(transcript, offset, names)
        _pending_stop = _row(session_id, "could_not", "stopped", None, True, checks)
        raise
    try:
        reply = (proc.stdout or "").strip()
        if proc.returncode != 0 or not reply:
            said = _said(proc)
            if proc.returncode != 0:
                fail_reason = "exit %s: %s" % (proc.returncode, said)
            else:
                fail_reason = "empty reply: %s" % said if said else "empty reply"
            return finish("could_not", fail_reason, None)
        try:
            _write_note(note_path, session_id, model, reply)
        except OSError as error:
            return finish("could_not", ("could not write the note: %s" % error)[:300], None)
        return finish("did_work", None, len(reply))
    except (KeyboardInterrupt, SystemExit) as error:
        if not _is_stop(error):
            raise
        found = _collect(transcript, offset, names)
        _pending_stop = _row(session_id, "could_not", "stopped", None, True, found)
        raise


def _default_projects():
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config:
        return Path(os.path.expanduser(config)) / "projects"
    return Path.home() / ".claude" / "projects"


def _resolve_claude(name):
    """Absolute path, because the command runs in the session folder."""
    named = os.sep in name or name.startswith("~") or (os.altsep is not None and os.altsep in name)
    if named:
        path = Path(os.path.expanduser(name))
        if path.is_file():
            return str(path.resolve())
        return None
    found = shutil.which(name)
    return os.path.abspath(found) if found else None


def _usage(message):
    print(message, file=sys.stderr)
    return 2  # bad arguments


_MAX_TIMEOUT = 2147483


def _positive_timeout(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0 or number == float("inf"):
        return None
    return number


def _print_dry_run(session, projects, claude, model):
    session_id = session_id_of(session) or str(session)
    print("session: %s" % session_id)
    transcript = find_transcript(session, projects)
    if transcript is None:
        print("could not: no transcript for this session under %s" % projects)
        return
    print("transcript: %s" % transcript)
    folder = session_folder(transcript)
    if not folder:
        print("could not: no cwd recorded for this session")
        return
    if not Path(folder).is_dir():
        print("could not: folder is gone: %s" % folder)
        return
    print("folder: %s" % folder)
    print("command: %s" % shlex.join(build_command(claude, session_id, model)))


def _line(row, out_dir):
    short = row["id"][:8]
    if row["state"] == "did_work":
        path = Path(out_dir) / ("%s.md" % short)
        return "%s written (%s characters)" % (path, row["length"])
    if row["state"] == "no_work":
        return "%s already has a note" % short
    return "%s: could not: %s" % (short, row["reason"])


def _summary_row(row):
    called = bool(row.get("called"))
    return {"id": row["id"], "state": row["state"], "reason": row.get("reason"),
            "length": row.get("length"),
            "transcript_written": row.get("transcript_written") if called else None,
            "copies_written": row.get("copies_written") if called else None}


def _write_summary(out_dir, rows, stopped=False):
    payload = {"time": datetime.now().isoformat(timespec="seconds"),
               "sessions": [_summary_row(row) for row in rows]}
    if stopped:
        payload["stopped"] = True
    (Path(out_dir) / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _judge(rows, stopped=False):
    leaked = [row["id"][:8] for row in rows
              if row.get("transcript_written") is True or row.get("copies_written")]
    unchecked = [row["id"][:8] for row in rows
                 if row.get("called") and (row.get("transcript_written") is None
                                            or row.get("copies_written") is None)]
    if stopped:
        show_leak = bool(leaked)
        if show_leak:
            _emit("LEAK: %s: a reader that judges a session by its newest record may now see the interview"
                  % ", ".join(leaked), sys.stdout)
        elif unchecked:
            _emit("UNCHECKED: %s" % ", ".join(unchecked), sys.stdout)
        return 0
    if leaked:
        print("LEAK: %s: a reader that judges a session by its newest record may now see the interview"
              % ", ".join(leaked))
        return 3  # leak
    if unchecked:
        print("UNCHECKED: %s" % ", ".join(unchecked))
        return 4  # unchecked
    done = sum(1 for row in rows if row["state"] == "did_work")
    already = sum(1 for row in rows if row["state"] == "no_work")
    failed = sum(1 for row in rows if row["state"] == "could_not")
    print("%s notes written, %s already there, %s could not" % (done, already, failed))
    if rows and failed == len(rows):
        return 1  # every session could not
    return 0  # finished


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(message, file=sys.stderr)
        raise SystemExit(2)


_pending_stop = None


def _is_stop(error):
    return isinstance(error, KeyboardInterrupt) or (isinstance(error, SystemExit) and error.code in (129, 143))


def _emit(text, stream):
    try:
        print(text, file=stream)
        stream.flush()
    except (OSError, ValueError):
        sys.stdout = sys.stderr = open(os.devnull, "w")


def _say_stopped():
    _emit("stopped", sys.stderr)


def _on_terminate(signum, frame): raise SystemExit(143)
def _on_hangup(signum, frame): raise SystemExit(129)


def main(argv=None):
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_hup = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGTERM, _on_terminate)
    if previous_hup != signal.SIG_IGN:
        signal.signal(signal.SIGHUP, _on_hangup)
    try:
        return _main(argv)
    except KeyboardInterrupt:
        _say_stopped()
        return 130
    except SystemExit as error:
        if error.code in (129, 143):
            _say_stopped()
            return error.code
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGHUP, previous_hup)


def _main(argv=None):
    parser = _Parser(prog="exit_interview.py")
    parser.add_argument("--out", default="exit-notes")
    parser.add_argument("--projects", default=None)
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--timeout", default="300")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pass-env", action="append", default=None)
    parser.add_argument("sessions", nargs="*")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        code = error.code
        if code in (129, 143):
            raise
        return code if isinstance(code, int) else 2
    if not args.sessions:
        return _usage("name at least one session")
    timeout = _positive_timeout(args.timeout)
    if timeout is None:
        return _usage("timeout must be a positive number")
    if timeout > _MAX_TIMEOUT:
        return _usage("timeout must be at most 2147483 seconds")
    if any((not item) or "=" in item for item in (args.pass_env or ())):
        return _usage("pass-env needs a variable name")
    claude = _resolve_claude(args.claude)
    if claude is None:
        return _usage("claude not found: %s" % args.claude)
    if args.projects:
        projects = str(Path(os.path.expanduser(args.projects)))
    else:
        projects = str(_default_projects())
    out_dir = str(Path(os.path.expanduser(args.out)))
    if args.dry_run:
        for session in args.sessions:
            _print_dry_run(session, projects, claude, args.model)
        return _DRY_RUN_OK
    try:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print("could not create the output folder: %s" % error, file=sys.stderr)
        return _SUMMARY_FAILED
    rows = []
    global _pending_stop
    _pending_stop = None
    try:
        for session in args.sessions:
            row = interview(session, out_dir, projects, claude, args.model, timeout, args.pass_env)
            rows.append(row)
            print(_line(row, out_dir))
    except (KeyboardInterrupt, SystemExit) as error:
        if not _is_stop(error):
            raise
        if _pending_stop is not None:
            rows.append(_pending_stop)
            _emit(_line(_pending_stop, out_dir), sys.stdout)
        try:
            _write_summary(out_dir, rows, stopped=True)
        except OSError as io_error:
            _emit("could not write summary: %s" % io_error, sys.stderr)
        _judge(rows, stopped=True)
        raise
    try:
        _write_summary(out_dir, rows)
    except OSError as error:
        print("could not write summary: %s" % error, file=sys.stderr)
        return _SUMMARY_FAILED
    return _judge(rows)


if __name__ == "__main__":
    sys.exit(main())
