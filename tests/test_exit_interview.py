#!/usr/bin/env python3
"""Tests for the exit interview. Every transcript and stand-in is made in a temp folder."""

import contextlib
import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import exit_interview as ei

SID = "a1b2c3d4-e5f6-4789-a012-3456789abcde"
NEAR = "b2c3d4e5-f6a7-4890-b123-456789abcdef"
GONE_A = "12121212-3434-4565-8787-909090909090"
GONE_B = "23232323-4545-4676-8989-010101010101"
REPLY = "The map still has an unnamed river."
QUESTION = """EXIT INTERVIEW. This session is about to be closed. Your whole conversation is already saved on disk and can be reopened at any time, so nothing recorded is at risk. Before it closes, answer four questions in your own voice.

1. What are you carrying that is not yet written to any file: intentions, half-made decisions, warnings, context you were relying on, open threads with people?
2. What is the one thing the next session, or the person you work for, must not lose?
3. Is any promise made to that person in this conversation still unmet?
4. In one line: what was this conversation really about, beneath its task?

Plain sentences, under 250 words, no headers. Do not summarize work that is already saved to disk. If everything you know is already on disk, say exactly that in one line."""


def last_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _stop_pid(pid):
    """SIGKILL one pid. A missing process is ignored. Never a process group."""
    if not pid or pid <= 1 or pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _read_pids(path):
    pids = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return pids
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _stop_recorded(path):
    for pid in _read_pids(path):
        _stop_pid(pid)


def _wait_for_pids(path, count, proc, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pids = _read_pids(path)
        if len(pids) >= count:
            return pids
        if proc is not None and proc.poll() is not None:
            break
        time.sleep(0.05)
    return _read_pids(path)


def _descendant_pids(root):
    try:
        text = subprocess.check_output(["ps", "-ax", "-o", "pid=,ppid="], text=True)
    except (subprocess.CalledProcessError, OSError):
        return []
    children = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    found = []
    stack = list(children.get(root, []))
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, []))
    return found


class ExitInterviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.home / ".claude"
        self.projects = self.config / "projects"
        self.project = self.projects / "picnic"
        self.project.mkdir(parents=True)
        self.work = self.root / "work"
        self.work.mkdir()
        self.out = self.root / "notes"
        self._env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.config),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def executable(self, name, body):
        path = self.root / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return path

    def write_script(self, path, body):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!%s\n%s" % (sys.executable, body), encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return path

    def sleeper_body(self, pid_path):
        return (
            "import os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(31)\"])\n"
            "handle = open(%r, 'w', encoding='utf-8')\n"
            "handle.write('%%s\\n%%s\\n' %% (os.getpid(), child.pid))\n"
            "handle.close()\n"
            "time.sleep(30)\n" % str(pid_path)
        )

    def _set_signal(self, signum, handler):
        def _preexec():
            signal.signal(signum, handler)
        return _preexec

    def start_tool(self, script, sessions, preexec=None, timeout="60", extra=None):
        command = [
            sys.executable, str(ROOT / "exit_interview.py"),
            "--projects", str(self.projects), "--out", str(self.out),
            "--claude", str(script), "--timeout", timeout,
        ]
        if extra:
            command.extend(extra)
        command.extend(sessions)
        holder = {}

        def cleanup_tool():
            proc = holder.get("proc")
            if proc is None:
                return
            if proc.poll() is None:
                for pid in _descendant_pids(proc.pid):
                    _stop_pid(pid)
                _stop_pid(proc.pid)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass

        self.addCleanup(cleanup_tool)
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            preexec_fn=preexec,
        )
        holder["proc"] = proc
        return proc

    def assert_gone(self, pid):
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if time.monotonic() >= deadline:
                self.fail("process %s was still running" % pid)
            time.sleep(0.05)

    def run_in(self, folder, path_value, args):
        previous = os.getcwd()
        os.chdir(folder)
        try:
            with mock.patch.dict(os.environ, {"PATH": path_value}):
                return self.run_main(args)
        finally:
            os.chdir(previous)

    def files_under(self, root):
        found = set()
        for dirpath, dirnames, filenames in os.walk(root):
            for name in filenames:
                found.add(os.path.join(dirpath, name))
        return found

    def clean_script(self, reply=REPLY):
        return self.executable("clean.py", "import sys\nsys.stdout.write(%r)\n" % (reply + "\n"))

    def write_transcript(self, sid=SID, cwd=None, folder=None, lines=None):
        folder = Path(folder) if folder is not None else self.project
        folder.mkdir(parents=True, exist_ok=True)
        if lines is None:
            record = {
                "cwd": str(self.work if cwd is None else cwd),
                "type": "user",
                "message": {"content": "Draw a map of a town that does not exist."},
            }
            lines = [json.dumps(record)]
        path = folder / ("%s.jsonl" % sid)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def run_main(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = ei.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def cli(self, *sessions, claude, extra=None):
        args = []
        if extra:
            args.extend(extra)
        args.extend(["--projects", str(self.projects), "--out", str(self.out), "--claude", str(claude)])
        args.extend(sessions)
        return self.run_main(args)

    def summary(self):
        return json.loads((self.out / "summary.json").read_text(encoding="utf-8"))

    def test_every_flag_is_pinned(self):
        """source: the resume command is forked, unsaved, given an empty tool list and hooks off."""
        command = ei.build_command("/bin/claude", SID, "sonnet")
        self.assertEqual(command, [
            "/bin/claude", "-p", "--resume", SID, "--fork-session",
            "--no-session-persistence", "--model", "sonnet", "--tools", "",
            "--strict-mcp-config", "--settings", '{"disableAllHooks":true}',
            ei.QUESTION,
        ])
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertIs(settings.get("disableAllHooks"), True)

    def test_mark_opens_the_question(self):
        """source: the after-call check searches for the sentence that opens the question."""
        self.assertEqual(ei.MARK, "EXIT INTERVIEW. This session is about to be closed.")
        self.assertTrue(ei.QUESTION.startswith(ei.MARK))
        self.assertEqual(ei.QUESTION, QUESTION)

    def test_mark_in_the_new_bytes_is_a_leak(self):
        """source: the question landing in bytes the transcript gained is a leak."""
        path = self.root / "session.jsonl"
        path.write_text("hello\n", encoding="utf-8")
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as handle:
            handle.write(ei.MARK + "\n")
        self.assertIs(ei.wrote_into(path, offset), True)

    def test_the_sessions_own_new_records_are_not_a_leak(self):
        """source: records the session writes for itself are not the interview."""
        path = self.root / "session.jsonl"
        path.write_text("hello\n", encoding="utf-8")
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as handle:
            handle.write("still drawing the map\n")
        self.assertIs(ei.wrote_into(path, offset), False)

    def test_an_earlier_interview_does_not_count(self):
        """source: an interview already in the transcript before this call does not count."""
        path = self.root / "session.jsonl"
        path.write_text(ei.MARK + "\n", encoding="utf-8")
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as handle:
            handle.write("new work after that\n")
        self.assertIs(ei.wrote_into(path, offset), False)

    def test_a_shorter_or_missing_file_cannot_be_told(self):
        """source: a transcript that shrank or disappeared cannot be judged clean or dirty."""
        path = self.root / "session.jsonl"
        path.write_text("hello\n", encoding="utf-8")
        size = path.stat().st_size
        self.assertIs(ei.wrote_into(path, size), False)
        self.assertIsNone(ei.wrote_into(path, size + 10))
        self.assertIsNone(ei.wrote_into(self.root / "gone.jsonl", 0))

    def test_new_copies_lists_only_a_file_holding_the_mark(self):
        """source: a new transcript is a saved copy only when it holds the question."""
        folder = self.root / "copies"
        folder.mkdir()
        old = folder / "old.jsonl"
        old.write_text(ei.MARK + "\n", encoding="utf-8")
        fresh = folder / "fresh.jsonl"
        fresh.write_text("a different session\n", encoding="utf-8")
        self.assertEqual(ei.new_copies(folder, {old.name}), [])
        marked = folder / "marked.jsonl"
        marked.write_text("prefix\n" + ei.MARK + "\n", encoding="utf-8")
        self.assertEqual(ei.new_copies(folder, {old.name}), ["marked.jsonl"])
        self.assertIsNone(ei.new_copies(self.root / "missing-folder", set()))

    def test_the_folder_is_taken_from_the_transcript(self):
        """source: the folder is the cwd recorded in the transcript, and lines that are not records are skipped."""
        path = self.project / ("%s.jsonl" % SID)
        path.write_text(
            "not json\n" + json.dumps(["a list"]) + "\n" + json.dumps({"cwd": str(self.work)}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(ei.session_folder(path), str(self.work))
        late = self.project / ("%s.jsonl" % NEAR)
        filler = [json.dumps({"type": "user"}) for _ in range(60)]
        filler.append(json.dumps({"cwd": str(self.work)}))
        late.write_text("\n".join(filler) + "\n", encoding="utf-8")
        self.assertIsNone(ei.session_folder(late))
        marker = self.root / "cwd.txt"
        script = self.executable(
            "cwd.py",
            "import os, sys\nopen(%r, 'w', encoding='utf-8').write(os.getcwd())\nsys.stdout.write(%r)\n"
            % (str(marker), REPLY + "\n"),
        )
        result = ei.interview(SID, self.out, self.projects, str(script), "sonnet", 30)
        self.assertEqual(result["state"], "did_work")
        self.assertEqual(Path(marker.read_text(encoding="utf-8")).resolve(), self.work.resolve())

    def test_a_stand_in_that_writes_the_transcript_exits_3(self):
        """source: a command that appends the question to the transcript is a leak and names the session."""
        path = self.write_transcript()
        script = self.executable(
            "leak.py",
            "import sys\nopen(%r, 'a', encoding='utf-8').write(sys.argv[-1] + '\\n')\n"
            "sys.stdout.write(%r)\n" % (str(path), REPLY + "\n"),
        )
        code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 3)
        self.assertNotIn("Traceback", err)
        self.assertTrue(last_line(out).startswith("LEAK:"))
        self.assertIn(SID[:8], last_line(out))
        self.assertIn("newest record", last_line(out))
        row = self.summary()["sessions"][0]
        self.assertEqual(row["id"], SID)
        self.assertIs(row["transcript_written"], True)
        self.assertEqual(row["copies_written"], [])
        self.assertIn("time", self.summary())

    def test_a_stand_in_that_saves_a_copy_exits_3(self):
        """source: a new transcript beside the original that holds the question is a leak."""
        self.write_transcript()
        copy_path = self.project / ("%s.jsonl" % NEAR)
        script = self.executable(
            "fork.py",
            "import sys\nopen(%r, 'w', encoding='utf-8').write(sys.argv[-1] + '\\n')\n"
            "sys.stdout.write(%r)\n" % (str(copy_path), REPLY + "\n"),
        )
        code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 3, out + err)
        self.assertTrue(last_line(out).startswith("LEAK:"))
        self.assertIn(SID[:8], last_line(out))
        row = self.summary()["sessions"][0]
        self.assertIs(row["transcript_written"], False)
        self.assertEqual(row["copies_written"], ["%s.jsonl" % NEAR])

    def test_a_new_session_beside_it_is_not_a_copy(self):
        """source: a new session that does not hold the question is not a saved copy."""
        self.write_transcript()
        neighbor = self.project / ("%s.jsonl" % NEAR)
        script = self.executable(
            "neighbor.py",
            "open(%r, 'w', encoding='utf-8').write('a different session\\n')\n"
            "import sys\nsys.stdout.write(%r)\n" % (str(neighbor), REPLY + "\n"),
        )
        code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 0, out + err)
        row = self.summary()["sessions"][0]
        self.assertEqual(row["copies_written"], [])
        self.assertIs(row["transcript_written"], False)

    def test_a_check_that_cannot_run_exits_4(self):
        """source: when the check cannot be told, the run is unchecked and is not reported clean."""
        path = self.write_transcript()
        script = self.executable(
            "drop.py",
            "import os, sys\nos.remove(%r)\nsys.stdout.write(%r)\n" % (str(path), REPLY + "\n"),
        )
        code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 4, out + err)
        self.assertNotIn("Traceback", err)
        self.assertTrue(last_line(out).startswith("UNCHECKED:"))
        self.assertIn(SID[:8], last_line(out))
        self.assertNotIn("notes written", last_line(out))
        row = self.summary()["sessions"][0]
        self.assertIsNone(row["transcript_written"])
        self.assertIsNot(row["transcript_written"], False)

    def test_a_timeout_still_reports_what_reached_the_transcript(self):
        """source: a timed-out command is still checked for bytes it wrote before it stopped."""
        path = self.write_transcript()
        pid_path = self.root / "slow.pid"
        script = self.executable(
            "slow.py",
            "import os, sys, time\n"
            "open(%r, 'w', encoding='utf-8').write(str(os.getpid()) + '\\n')\n"
            "handle = open(%r, 'a', encoding='utf-8')\n"
            "handle.write(sys.argv[-1] + '\\n')\n"
            "handle.flush()\n"
            "os.fsync(handle.fileno())\n"
            "handle.close()\n"
            "time.sleep(30)\n" % (str(pid_path), str(path)),
        )
        self.addCleanup(lambda: _stop_recorded(pid_path))
        result = ei.interview(SID, self.out, self.projects, str(script), "sonnet", 1)
        self.assertEqual(result["state"], "could_not")
        self.assertIn("timeout", result["reason"])
        self.assertIs(result["transcript_written"], True)
        self.assertTrue(result["called"])

    def test_a_clean_run_exits_0(self):
        """source: a command that leaves the transcript alone writes a note and exits clean."""
        self.write_transcript()
        code, out, err = self.cli(SID, claude=self.clean_script())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertEqual(last_line(out), "1 notes written, 0 already there, 0 could not")
        self.assertIn("(%s characters)" % len(REPLY), out)
        note = (self.out / ("%s.md" % SID[:8])).read_text(encoding="utf-8")
        self.assertIn(SID, note)
        self.assertIn("sonnet", note)
        self.assertIn("no tools, hooks off, fork not saved", note)
        self.assertIn("\n---\n", note)
        self.assertIn(REPLY, note)
        row = self.summary()["sessions"][0]
        self.assertEqual(row["state"], "did_work")
        self.assertEqual(row["length"], len(REPLY))
        self.assertIsNone(row["reason"])
        self.assertIs(row["transcript_written"], False)
        self.assertEqual(row["copies_written"], [])
        self.assertNotIn("called", row)

    def test_a_note_that_exists_makes_no_call(self):
        """source: an existing note means there is no work and no command, so there is nothing to check."""
        self.write_transcript()
        self.out.mkdir()
        note = self.out / ("%s.md" % SID[:8])
        note.write_text("already here\n", encoding="utf-8")
        log = self.root / "called.txt"
        script = self.executable(
            "record.py",
            "import sys\nopen(%r, 'w', encoding='utf-8').write('called')\nsys.stdout.write(%r)\n"
            % (str(log), REPLY + "\n"),
        )
        code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 0, out + err)
        self.assertIn("%s already has a note" % SID[:8], out)
        self.assertEqual(last_line(out), "0 notes written, 1 already there, 0 could not")
        self.assertFalse(log.exists())
        self.assertEqual(note.read_text(encoding="utf-8"), "already here\n")
        row = self.summary()["sessions"][0]
        self.assertEqual(row["state"], "no_work")
        self.assertIsNone(row["transcript_written"])
        self.assertIsNone(row["copies_written"])

    def test_sha256_and_folder_listing_stay_the_same(self):
        """source: a clean run leaves the transcript bytes and the folder listing unchanged."""
        path = self.write_transcript()
        extra = self.project / ("%s.jsonl" % NEAR)
        extra.write_text("{}\n", encoding="utf-8")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        listing = sorted(item.name for item in self.project.glob("*.jsonl"))
        code, out, err = self.cli(SID, claude=self.clean_script())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(item.name for item in self.project.glob("*.jsonl")), listing)

    def test_a_session_given_as_a_path_works(self):
        """source: a transcript path is interviewed the same way as the id that names it."""
        outside = self.root / "elsewhere"
        path = self.write_transcript(folder=outside)
        code, out, err = self.cli(str(path), claude=self.clean_script())
        self.assertEqual(code, 0, out + err)
        note = self.out / ("%s.md" % SID[:8])
        self.assertTrue(note.is_file())
        self.assertIn(REPLY, note.read_text(encoding="utf-8"))
        self.assertEqual(self.summary()["sessions"][0]["id"], SID)

    def test_an_unknown_id_is_could_not(self):
        """source: a session with no transcript file cannot be interviewed."""
        code, out, err = self.cli(GONE_A, claude=self.clean_script())
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("could not:", out)
        self.assertIn("no transcript for this session under", out)
        row = self.summary()["sessions"][0]
        self.assertEqual(row["state"], "could_not")
        self.assertIn("no transcript for this session under", row["reason"])
        self.assertIsNone(row["transcript_written"])

    def test_a_missing_folder_is_could_not(self):
        """source: a session whose folder is gone cannot be resumed, and the reason says so."""
        gone = self.root / "gone"
        self.write_transcript(cwd=gone)
        log = self.root / "called.txt"
        script = self.executable(
            "record.py",
            "open(%r, 'w', encoding='utf-8').write('called')\nimport sys\nsys.stdout.write(%r)\n"
            % (str(log), REPLY + "\n"),
        )
        result = ei.interview(SID, self.out, self.projects, str(script), "sonnet", 30)
        self.assertEqual(result["state"], "could_not")
        self.assertIn("folder is gone:", result["reason"])
        self.assertIn(str(gone), result["reason"])
        self.assertFalse(result["called"])
        self.assertFalse(log.exists())

    def test_a_transcript_without_a_folder_is_could_not(self):
        """source: a transcript that records no folder cannot be interviewed, and the reason says so."""
        self.write_transcript(lines=[json.dumps({"type": "user", "message": {"content": "hello"}})])
        result = ei.interview(SID, self.out, self.projects, str(self.clean_script()), "sonnet", 30)
        self.assertEqual(result["state"], "could_not")
        self.assertIn("no cwd recorded", result["reason"])
        self.assertFalse(result["called"])

    def test_a_planted_secret_does_not_reach_the_command(self):
        """source: a planted variable named like a secret does not reach the command, and the kept sign-in names do."""
        self.write_transcript()
        log = self.root / "env.json"
        script = self.executable(
            "env.py",
            "import json, os, sys\nopen(%r, 'w', encoding='utf-8').write(json.dumps(dict(os.environ)))\n"
            "sys.stdout.write(%r)\n" % (str(log), REPLY + "\n"),
        )
        overlay = {
            "ANTHROPIC_API_KEY": "key-from-the-test",
            "CLAUDE_CODE_OAUTH_TOKEN": "token-from-the-test",
            "PLANTED_SECRET": "do-not-pass",
        }
        with mock.patch.dict(os.environ, overlay, clear=False):
            env = ei.scrubbed_env()
            self.assertEqual(env.get("PATH"), os.environ.get("PATH"))
            self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), str(self.config))
            self.assertEqual(env.get("ANTHROPIC_API_KEY"), "key-from-the-test")
            self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN"), "token-from-the-test")
            self.assertNotIn("PLANTED_SECRET", env)
            result = ei.interview(SID, self.out, self.projects, str(script), "sonnet", 30)
        self.assertEqual(result["state"], "did_work")
        self.assertTrue(log.is_file())
        seen = json.loads(log.read_text(encoding="utf-8"))
        self.assertIn("PATH", seen)
        self.assertEqual(seen.get("HOME"), str(self.home))
        self.assertEqual(seen.get("CLAUDE_CONFIG_DIR"), str(self.config))
        self.assertEqual(seen.get("ANTHROPIC_API_KEY"), "key-from-the-test")
        self.assertEqual(seen.get("CLAUDE_CODE_OAUTH_TOKEN"), "token-from-the-test")
        self.assertNotIn("PLANTED_SECRET", seen)
        self.assertNotIn("do-not-pass", " ".join(seen.values()))

    def test_dry_run_runs_nothing_and_writes_nothing(self):
        """source: dry-run prints the transcript, the folder and the command, and does not run or write."""
        path = self.write_transcript()
        log = self.root / "called.txt"
        script = self.executable(
            "record.py",
            "open(%r, 'w', encoding='utf-8').write('called')\nimport sys\nsys.stdout.write(%r)\n"
            % (str(log), REPLY + "\n"),
        )
        code, out, err = self.cli(SID, claude=script, extra=["--dry-run"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertIn("transcript:", out)
        self.assertIn(str(path), out)
        self.assertIn("folder:", out)
        self.assertIn(str(self.work), out)
        self.assertIn("--fork-session", out)
        self.assertIn("--no-session-persistence", out)
        self.assertIn("--strict-mcp-config", out)
        self.assertIn("disableAllHooks", out)
        self.assertFalse(log.exists())
        self.assertFalse((self.out / "summary.json").exists())
        self.assertFalse((self.out / ("%s.md" % SID[:8])).exists())

    def test_every_usage_error_exits_2(self):
        """source: a missing session, a timeout that is not a positive number, and a missing claude each exit as a usage error."""
        missing = self.root / "no-such-claude"
        cases = [
            ["--out", str(self.out)],
            ["--out", str(self.out), "--timeout", "0", SID],
            ["--out", str(self.out), "--timeout", "-3", SID],
            ["--out", str(self.out), "--timeout", "foo", SID],
            ["--out", str(self.out), "--timeout", "inf", SID],
            ["--out", str(self.out), "--claude", str(missing), SID],
        ]
        for args in cases:
            with self.subTest(args=args):
                code, out, err = self.run_main(args)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)
                lines = [line for line in err.splitlines() if line.strip()]
                self.assertEqual(len(lines), 1)
                self.assertFalse(self.out.exists())

    def test_all_sessions_failing_exits_1(self):
        """source: when every session could not be interviewed, the run fails."""
        code, out, err = self.cli(GONE_A, GONE_B, claude=self.clean_script())
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(last_line(out), "0 notes written, 0 already there, 2 could not")
        states = [row["state"] for row in self.summary()["sessions"]]
        self.assertEqual(states, ["could_not", "could_not"])

    def test_no_temp_file_is_left_behind(self):
        """source: the note is renamed into place, and no temporary file remains."""
        self.write_transcript()
        code, out, err = self.cli(SID, claude=self.clean_script())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(list(self.out.glob("*.tmp")), [])
        note = self.out / ("%s.md" % SID[:8])
        self.assertTrue(note.is_file())
        self.assertTrue(note.read_text(encoding="utf-8").endswith(REPLY + "\n"))

    def test_the_note_is_renamed_into_place(self):
        """source: the note is written to a temporary name and then renamed into place."""
        self.write_transcript()
        script = self.clean_script()
        with mock.patch("exit_interview.os.replace", side_effect=os.replace) as replaced:
            code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(replaced.call_count, 1)
        source, dest = replaced.call_args[0]
        self.assertTrue(str(source).endswith(".md.tmp"))
        self.assertTrue(str(dest).endswith(".md"))
        self.assertEqual(list(self.out.glob("*.tmp")), [])

    def test_an_empty_reply_is_could_not(self):
        """source: a command that says nothing does not produce a note."""
        self.write_transcript()
        script = self.executable("quiet.py", "import sys\nsys.exit(0)\n")
        code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 1, out + err)
        self.assertIn("empty reply", out)
        self.assertFalse((self.out / ("%s.md" % SID[:8])).exists())
        self.assertEqual(self.summary()["sessions"][0]["state"], "could_not")

    def test_a_nonzero_exit_is_could_not(self):
        """source: a command that fails does not produce a note, and its words are kept short."""
        self.write_transcript()
        script = self.executable(
            "fail.py",
            "import sys\nsys.stderr.write('x' * 1000)\nsys.exit(7)\n",
        )
        result = ei.interview(SID, self.out, self.projects, str(script), "sonnet", 30)
        self.assertEqual(result["state"], "could_not")
        self.assertIn("exit 7", result["reason"])
        self.assertNotIn("x" * 301, result["reason"])
        self.assertIn("x" * 100, result["reason"])
        self.assertIs(result["transcript_written"], False)
        self.assertEqual(result["copies_written"], [])
        self.assertFalse((self.out / ("%s.md" % SID[:8])).exists())

    def test_a_command_that_cannot_start_is_could_not(self):
        """source: a command that cannot start is reported as could not, without a traceback."""
        self.write_transcript()
        bad = self.root / "not-executable"
        bad.write_text("nope\n", encoding="utf-8")
        bad.chmod(stat.S_IRUSR | stat.S_IWUSR)
        code, out, err = self.cli(SID, claude=bad)
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("could not start", out)
        self.assertEqual(self.summary()["sessions"][0]["state"], "could_not")

    def test_a_broken_transcript_does_not_traceback(self):
        """source: a transcript that is not valid records is could not, not a crash."""
        path = self.project / ("%s.jsonl" % SID)
        path.write_bytes(b"\xff\xfe not json\n")
        code, out, err = self.cli(SID, claude=self.clean_script())
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("could not:", out)

    def test_sessions_run_in_the_order_given(self):
        """source: sessions are handled in the order they were given."""
        self.write_transcript(SID)
        self.write_transcript(NEAR)
        code, out, err = self.cli(SID, NEAR, claude=self.clean_script())
        self.assertEqual(code, 0, out + err)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertIn(SID[:8], lines[0])
        self.assertIn(NEAR[:8], lines[1])
        self.assertEqual(lines[-1], "2 notes written, 0 already there, 0 could not")
        ids = [row["id"] for row in self.summary()["sessions"]]
        self.assertEqual(ids, [SID, NEAR])

    def test_projects_default_uses_the_config_dir_then_home(self):
        """source: with no projects folder passed, the config dir is used when set, otherwise the home folder."""
        other_home = self.root / "other-home"
        other_home.mkdir()
        config = self.root / "config-dir"
        folder = config / "projects" / "place"
        self.write_transcript(folder=folder)
        script = self.clean_script()
        kept = {key: os.environ[key] for key in ("PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "TERM") if key in os.environ}
        kept.update({"HOME": str(other_home), "CLAUDE_CONFIG_DIR": str(config)})
        with mock.patch.dict(os.environ, kept, clear=True):
            code, out, err = self.run_main(["--out", str(self.out), "--claude", str(script), SID])
        self.assertEqual(code, 0, out + err)
        note = (self.out / ("%s.md" % SID[:8])).read_text(encoding="utf-8")
        self.assertIn(REPLY, note)

        home = self.root / "only-home"
        home_folder = home / ".claude" / "projects" / "place"
        self.out2 = self.root / "notes-home"
        self.write_transcript(folder=home_folder)
        kept = {key: os.environ[key] for key in ("PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "TERM") if key in os.environ}
        kept["HOME"] = str(home)
        with mock.patch.dict(os.environ, kept, clear=True):
            code, out, err = self.run_main(["--out", str(self.out2), "--claude", str(script), SID])
        self.assertEqual(code, 0, out + err)
        home_note = (self.out2 / ("%s.md" % SID[:8])).read_text(encoding="utf-8")
        self.assertIn(REPLY, home_note)

    def test_a_relative_claude_path_still_runs(self):
        """source: the command is started by an absolute path so the session folder does not hide it."""
        self.write_transcript()
        self.clean_script()
        previous = os.getcwd()
        try:
            os.chdir(self.root)
            code, out, err = self.run_main([
                "--projects", str(self.projects), "--out", str(self.out), "--claude", "./clean.py", SID,
            ])
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, out + err)

    def test_the_time_limit_stops_the_helper(self):
        """source: at the time limit the command and every process it started are stopped."""
        self.write_transcript()
        pid_path = self.root / "child.pid"
        script = self.write_script(self.root / "sleeper.py", self.sleeper_body(pid_path))
        self.addCleanup(lambda: _stop_recorded(pid_path))
        started = time.monotonic()
        code, out, err = self.cli(SID, claude=script, extra=["--timeout", "1"])
        self.assertLess(time.monotonic() - started, 10)
        self.assertNotIn("Traceback", err)
        self.assertIn("could not:", out)
        self.assertIn("timeout after", out)
        row = self.summary()["sessions"][0]
        self.assertEqual(row["state"], "could_not")
        self.assertIn("timeout after", row["reason"])
        self.assertTrue(pid_path.is_file(), out + err)
        pids = _read_pids(pid_path)
        self.assertEqual(len(pids), 2)
        for pid in pids:
            self.assert_gone(pid)
        self.assertEqual(code, 1)

    def test_a_terminate_signal_stops_the_helper(self):
        """source: a terminate signal stops the command and every process it started, then the tool exits without a traceback."""
        self.write_transcript()
        pid_path = self.root / "child.pid"
        script = self.write_script(self.root / "sleeper.py", self.sleeper_body(pid_path))
        self.addCleanup(lambda: _stop_recorded(pid_path))
        proc = self.start_tool(
            script, [SID], preexec=self._set_signal(signal.SIGTERM, signal.SIG_DFL),
        )
        pids = _wait_for_pids(pid_path, 2, proc)
        self.assertEqual(len(pids), 2)
        os.kill(proc.pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the tool did not exit")
        self.assertEqual(proc.returncode, 143)
        self.assertNotIn("Traceback", err)
        self.assertEqual(err.strip(), "stopped")
        for pid in pids:
            self.assert_gone(pid)

    def test_the_command_gets_no_standard_input(self):
        """source: text piped into the tool does not reach the command."""
        self.write_transcript()
        count_path = self.root / "count.txt"
        script = self.write_script(
            self.root / "reader.py",
            "import sys\n"
            "data = sys.stdin.read()\n"
            "open(%r, 'w', encoding='utf-8').write(str(len(data)))\n"
            "sys.stdout.write(%r)\n" % (str(count_path), REPLY + "\n"),
        )
        proc = subprocess.run(
            [sys.executable, str(ROOT / "exit_interview.py"),
             "--projects", str(self.projects), "--out", str(self.out),
             "--claude", str(script), SID],
            input="a line of text\n", capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(count_path.read_text(encoding="utf-8"), "0")
        note = (self.out / ("%s.md" % SID[:8])).read_text(encoding="utf-8")
        self.assertIn(REPLY, note)

    def test_a_file_named_claude_in_the_current_folder_is_not_used(self):
        """source: a bare command in the current folder is not run when that command is absent from PATH."""
        self.write_transcript()
        start = self.root / "start"
        start.mkdir()
        marker = start / "marker.txt"
        self.write_script(start / "claude", "open(%r, 'w', encoding='utf-8').write('ran\\n')\n" % str(marker))
        empty = self.root / "empty-bin"
        empty.mkdir()
        code, out, err = self.run_in(start, str(empty), [
            "--projects", str(self.projects), "--out", str(self.out),
            "--claude", "claude", SID,
        ])
        self.assertEqual(code, 2, out + err)
        self.assertIn("claude not found: claude", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(marker.exists())

    def test_the_command_on_path_is_used(self):
        """source: a bare command is the one found on PATH."""
        self.write_transcript()
        start = self.root / "start"
        start.mkdir()
        marker = start / "marker.txt"
        self.write_script(start / "claude", "open(%r, 'w', encoding='utf-8').write('ran\\n')\n" % str(marker))
        good = self.root / "good-bin"
        self.write_script(good / "claude", "import sys\nsys.stdout.write(%r)\n" % (REPLY + "\n"))
        code, out, err = self.run_in(start, str(good), [
            "--projects", str(self.projects), "--out", str(self.out),
            "--claude", "claude", SID,
        ])
        self.assertEqual(code, 0, out + err)
        note = (self.out / ("%s.md" % SID[:8])).read_text(encoding="utf-8")
        self.assertIn(REPLY, note)
        self.assertFalse(marker.exists())

    def test_a_relative_path_entry_still_works(self):
        """source: a relative PATH entry still locates the command when the session folder is elsewhere."""
        self.write_transcript()
        start = self.root / "start"
        self.write_script(start / "relbin" / "claude", "import sys\nsys.stdout.write(%r)\n" % (REPLY + "\n"))
        code, out, err = self.run_in(start, "relbin", [
            "--projects", str(self.projects), "--out", str(self.out),
            "--claude", "claude", SID,
        ])
        self.assertEqual(code, 0, out + err)
        note = self.out / ("%s.md" % SID[:8])
        self.assertTrue(note.is_file())
        self.assertIn(REPLY, note.read_text(encoding="utf-8"))

    def test_a_zero_time_limit_is_refused(self):
        """source: a zero time limit is refused and the command does not run."""
        self.write_transcript()
        log = self.root / "called.txt"
        script = self.write_script(
            self.root / "record.py",
            "open(%r, 'w', encoding='utf-8').write('called')\nimport sys\nsys.stdout.write(%r)\n"
            % (str(log), REPLY + "\n"),
        )
        code, out, err = self.cli(SID, claude=script, extra=["--timeout", "0"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), "timeout must be a positive number")
        self.assertNotIn("Traceback", err)
        self.assertFalse(log.exists())

    def test_one_failure_among_several_is_not_exit_1(self):
        """source: a mix of an interviewed session and a session that could not be interviewed is not a failed run."""
        self.write_transcript(SID)
        code, out, err = self.cli(SID, GONE_A, claude=self.write_script(
            self.root / "clean.py", "import sys\nsys.stdout.write(%r)\n" % (REPLY + "\n"),
        ))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(last_line(out), "1 notes written, 0 already there, 1 could not")

    def test_a_transcript_file_that_is_not_a_session_id_is_refused(self):
        """source: a transcript file that does not carry a session id is not interviewed."""
        bad = self.root / "notes.jsonl"
        record = {"cwd": str(self.work), "type": "user", "message": {"content": "hello"}}
        bad.write_text(json.dumps(record) + "\n", encoding="utf-8")
        log = self.root / "called.txt"
        script = self.write_script(
            self.root / "record.py",
            "open(%r, 'w', encoding='utf-8').write('called')\nimport sys\nsys.stdout.write(%r)\n"
            % (str(log), REPLY + "\n"),
        )
        before = self.files_under(self.root)
        code, out, err = self.cli(str(bad), claude=script)
        self.assertEqual(code, 1)
        self.assertIn("could not:", out)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(self.summary()["sessions"][0]["state"], "could_not")
        self.assertFalse(log.exists())
        notes = self.out.resolve()
        for path in self.files_under(self.root) - before:
            resolved = Path(path).resolve()
            self.assertTrue(resolved == notes or notes in resolved.parents, path)

    def test_an_unchecked_copy_check_is_reported(self):
        """source: a call whose copy check could not be told is reported unchecked."""
        row = {
            "id": SID,
            "state": "did_work",
            "reason": None,
            "length": len(REPLY),
            "called": True,
            "transcript_written": False,
            "copies_written": None,
        }
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = ei._judge([row])
        self.assertEqual(code, 4)
        self.assertIn("UNCHECKED: %s" % SID[:8], stdout.getvalue())

    def test_a_stop_in_the_middle_keeps_the_rows(self):
        """source: a stop in the middle of a batch keeps the checks and the leak for the sessions it reached."""
        path_a = self.write_transcript(SID)
        self.write_transcript(NEAR)
        pid_path = self.root / "b.pid"
        script = self.write_script(
            self.root / "batch.py",
            "import os, sys, time\n"
            "argv = sys.argv\n"
            "session = argv[argv.index('--resume') + 1]\n"
            "if session == %r:\n"
            "    handle = open(%r, 'a', encoding='utf-8')\n"
            "    handle.write(argv[-1] + '\\n')\n"
            "    handle.close()\n"
            "    sys.stdout.write(%r)\n"
            "else:\n"
            "    open(%r, 'w', encoding='utf-8').write(str(os.getpid()) + '\\n')\n"
            "    time.sleep(30)\n"
            % (SID, str(path_a), REPLY + "\n", str(pid_path)),
        )
        self.addCleanup(lambda: _stop_recorded(pid_path))
        proc = self.start_tool(
            script, [SID, NEAR], preexec=self._set_signal(signal.SIGTERM, signal.SIG_DFL),
        )
        pids = _wait_for_pids(pid_path, 1, proc)
        self.assertEqual(len(pids), 1, proc.stdout.read() if proc.poll() is not None else "still running")
        os.kill(proc.pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the tool did not exit")
        self.assertEqual(proc.returncode, 143)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3, out)
        self.assertIn(SID[:8], lines[0])
        self.assertIn("written", lines[0])
        self.assertEqual(lines[1], "%s: could not: stopped" % NEAR[:8])
        self.assertTrue(lines[2].startswith("LEAK:"))
        self.assertIn(SID[:8], lines[2])
        self.assertNotIn("notes written", out)
        err_lines = [line for line in err.splitlines() if line.strip()]
        self.assertEqual(err_lines[-1], "stopped")
        self.assertNotIn("Traceback", err)
        self.assertTrue((self.out / "summary.json").is_file())
        payload = self.summary()
        self.assertIn("stopped", payload)
        self.assertIs(payload["stopped"], True)
        self.assertEqual(len(payload["sessions"]), 2)
        self.assertIs(payload["sessions"][0]["transcript_written"], True)
        row_b = payload["sessions"][1]
        self.assertEqual(row_b["state"], "could_not")
        self.assertEqual(row_b["reason"], "stopped")
        self.assertIsNotNone(row_b["transcript_written"])
        self.assertIsNotNone(row_b["copies_written"])
        for pid in pids:
            self.assert_gone(pid)

    def test_an_interrupt_still_checks_the_session(self):
        """source: an interrupt while a session is being asked still checks that session and records the stop."""
        self.write_transcript()
        script = self.clean_script()

        def boom(*args, **kwargs):
            raise KeyboardInterrupt

        with mock.patch("exit_interview._run", side_effect=boom):
            code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 130)
        self.assertIn("stopped", err)
        self.assertIn("%s: could not: stopped" % SID[:8], out)
        self.assertTrue((self.out / "summary.json").is_file())
        payload = self.summary()
        self.assertIn("stopped", payload)
        self.assertIs(payload["stopped"], True)
        self.assertEqual(len(payload["sessions"]), 1)
        row = payload["sessions"][0]
        self.assertEqual(row["reason"], "stopped")
        self.assertIsNotNone(row["transcript_written"])
        self.assertIsNotNone(row["copies_written"])

    def test_a_hangup_stops_the_helper_and_records_the_stop(self):
        """source: a hang-up stops the command, records the stop, and exits with the hang-up code."""
        self.write_transcript()
        pid_path = self.root / "child.pid"
        script = self.write_script(self.root / "sleeper.py", self.sleeper_body(pid_path))
        self.addCleanup(lambda: _stop_recorded(pid_path))
        proc = self.start_tool(
            script, [SID], preexec=self._set_signal(signal.SIGHUP, signal.SIG_DFL),
        )
        pids = _wait_for_pids(pid_path, 2, proc)
        self.assertEqual(len(pids), 2)
        os.kill(proc.pid, signal.SIGHUP)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the tool did not exit")
        self.assertEqual(proc.returncode, 129, out + err)
        self.assertIn("stopped", err)
        self.assertNotIn("Traceback", err)
        self.assertTrue((self.out / "summary.json").is_file())
        self.assertIs(self.summary()["stopped"], True)
        for pid in pids:
            self.assert_gone(pid)

    def test_a_hangup_with_the_terminal_gone_still_records_the_stop(self):
        """source: a hang-up after the terminal is gone still writes the summary and keeps its exit code."""
        self.write_transcript()
        pid_path = self.root / "child.pid"
        script = self.write_script(self.root / "sleeper.py", self.sleeper_body(pid_path))
        self.addCleanup(lambda: _stop_recorded(pid_path))
        proc = self.start_tool(
            script, [SID], preexec=self._set_signal(signal.SIGHUP, signal.SIG_DFL),
        )
        pids = _wait_for_pids(pid_path, 2, proc)
        self.assertEqual(len(pids), 2)
        proc.stdout.close()
        proc.stderr.close()
        os.kill(proc.pid, signal.SIGHUP)
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the tool did not exit")
        self.assertEqual(code, 129)
        self.assertTrue((self.out / "summary.json").is_file())
        payload = self.summary()
        self.assertIn("stopped", payload)
        self.assertIs(payload["stopped"], True)
        self.assertEqual(len(payload["sessions"]), 1)
        self.assertEqual(payload["sessions"][0]["reason"], "stopped")
        for pid in pids:
            self.assert_gone(pid)

    def test_a_hangup_that_was_ignored_stays_ignored(self):
        """source: a hang-up that is already ignored when the tool starts stays ignored and the run finishes."""
        self.write_transcript()
        pid_path = self.root / "ready.pid"
        script = self.write_script(
            self.root / "nap.py",
            "import os, sys, time\n"
            "open(%r, 'w', encoding='utf-8').write(str(os.getpid()) + '\\n')\n"
            "time.sleep(1)\n"
            "sys.stdout.write(%r)\n" % (str(pid_path), REPLY + "\n"),
        )
        self.addCleanup(lambda: _stop_recorded(pid_path))
        proc = self.start_tool(
            script, [SID],
            preexec=self._set_signal(signal.SIGHUP, signal.SIG_IGN),
            timeout="30",
        )
        pids = _wait_for_pids(pid_path, 1, proc)
        self.assertEqual(len(pids), 1)
        os.kill(proc.pid, signal.SIGHUP)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the tool did not exit")
        self.assertEqual(proc.returncode, 0, out + err)
        note = self.out / ("%s.md" % SID[:8])
        self.assertTrue(note.is_file())
        self.assertIn(REPLY, note.read_text(encoding="utf-8"))

    def test_a_finished_run_omits_the_stopped_key(self):
        """source: a run that ends by itself does not mark the summary as stopped."""
        self.write_transcript()
        code, out, err = self.cli(SID, claude=self.clean_script())
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("stopped", self.summary())

    def test_a_stopped_dry_run_writes_nothing(self):
        """source: a dry run that is stopped writes no output folder."""
        self.write_transcript()
        script = self.clean_script()
        with mock.patch("exit_interview._print_dry_run", side_effect=KeyboardInterrupt):
            code, out, err = self.cli(SID, claude=script, extra=["--dry-run"])
        self.assertFalse(self.out.exists())
        self.assertEqual(code, 130)
        self.assertIn("stopped", err)

    def test_pass_env_reaches_the_command_only_when_asked(self):
        """source: an extra environment variable reaches the command only when the flag asks for it."""
        self.write_transcript()
        script = self.write_script(
            self.root / "proxy.py",
            "import os, sys\n"
            "value = os.environ.get('EXAMPLE_PROXY')\n"
            "sys.stdout.write((value if value else 'absent') + '\\n')\n",
        )
        note_path = self.out / ("%s.md" % SID[:8])
        with mock.patch.dict(os.environ, {"EXAMPLE_PROXY": "proxy-from-the-test"}):
            code, out, err = self.cli(SID, claude=script)
            self.assertEqual(code, 0, out + err)
            note = note_path.read_text(encoding="utf-8")
            self.assertIn("absent", note)
            self.assertNotIn("proxy-from-the-test", note)
            note_path.unlink()
            (self.out / "summary.json").unlink()
            code, out, err = self.cli(SID, claude=script, extra=["--pass-env", "EXAMPLE_PROXY"])
            self.assertEqual(code, 0, out + err)
            note = note_path.read_text(encoding="utf-8")
            self.assertIn("proxy-from-the-test", note)
            self.assertNotIn("absent", note)

    def test_two_pass_env_flags_pass_two_names(self):
        """source: each extra flag adds a variable, and a variable that is not set is left out."""
        self.write_transcript()
        seen_path = self.root / "seen.json"
        script = self.write_script(
            self.root / "seen.py",
            "import json, os, sys\n"
            "open(%r, 'w', encoding='utf-8').write(json.dumps(dict(os.environ)))\n"
            "sys.stdout.write(%r)\n" % (str(seen_path), REPLY + "\n"),
        )
        with mock.patch.dict(os.environ, {"EXAMPLE_ONE": "one-value"}):
            os.environ.pop("EXAMPLE_MISSING", None)
            code, out, err = self.cli(
                SID, claude=script,
                extra=["--pass-env", "EXAMPLE_ONE", "--pass-env", "EXAMPLE_MISSING"],
            )
        self.assertEqual(code, 0, out + err)
        seen = json.loads(seen_path.read_text(encoding="utf-8"))
        self.assertEqual(seen.get("EXAMPLE_ONE"), "one-value")
        self.assertNotIn("EXAMPLE_MISSING", seen)

    def test_a_bad_pass_env_name_is_a_usage_error(self):
        """source: an empty extra variable or a variable holding an equals sign is a usage error and runs nothing."""
        self.write_transcript()
        marker = self.root / "called.txt"
        script = self.write_script(
            self.root / "mark.py",
            "open(%r, 'w', encoding='utf-8').write('called')\n"
            "import sys\nsys.stdout.write(%r)\n" % (str(marker), REPLY + "\n"),
        )
        for bad in ("", "A=B"):
            with self.subTest(bad=bad):
                if marker.exists():
                    marker.unlink()
                code, out, err = self.cli(SID, claude=script, extra=["--pass-env", bad])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(
                    [line for line in err.splitlines() if line.strip()],
                    ["pass-env needs a variable name"],
                )
                self.assertNotIn("Traceback", err)
                self.assertFalse(marker.exists())

    def test_a_terminate_exit_says_stopped(self):
        """source: a terminate exit says it stopped and uses the terminate code."""
        with mock.patch("exit_interview._main", side_effect=SystemExit(143)):
            code, out, err = self.run_main([])
        self.assertEqual(code, 143)
        self.assertIn("stopped", err)
        self.assertNotIn("Traceback", out + err)

    def test_a_later_run_replaces_the_summary(self):
        """source: the summary describes the latest run only, so a later run replaces an earlier record."""
        self.write_transcript()
        script = self.clean_script()
        code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.summary()["sessions"][0]["state"], "did_work")
        code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 0, out + err)
        self.assertIn("already has a note", out)
        rows = self.summary()["sessions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "no_work")
        self.assertEqual(rows[0]["id"], SID)

    def test_a_time_limit_too_large_to_wait_for_is_refused(self):
        """source: a time limit too large to wait for is refused before anything runs."""
        self.write_transcript()
        log = self.root / "called.txt"
        script = self.write_script(
            self.root / "record.py",
            "open(%r, 'w', encoding='utf-8').write('called')\nimport sys\nsys.stdout.write(%r)\n"
            % (str(log), REPLY + "\n"),
        )
        for value in ("3000000", "2147484", "1e300"):
            with self.subTest(value=value):
                if log.exists():
                    log.unlink()
                try:
                    code, out, err = self.cli(SID, claude=script, extra=["--timeout", value])
                except OverflowError:
                    self.fail("a time limit too large to wait for was not refused")
                self.assertEqual(code, 2)
                self.assertEqual(err.strip(), "timeout must be at most 2147483 seconds")
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)
                self.assertFalse(self.out.exists())
                self.assertFalse(log.exists())

    def test_the_largest_time_limit_still_runs(self):
        """source: the largest time limit the wait can accept still runs and writes the note."""
        self.write_transcript()
        code, out, err = self.cli(SID, claude=self.clean_script(), extra=["--timeout", "2147483"])
        self.assertEqual(code, 0, out + err)
        note = self.out / ("%s.md" % SID[:8])
        self.assertIn(REPLY, note.read_text(encoding="utf-8"))

    def test_a_stop_after_the_note_is_written_still_checks_the_session(self):
        """source: a stop after the reply is saved still checks the session and records the stop."""
        self.write_transcript()
        script = self.clean_script()
        real_write_note = ei._write_note

        def boom(note_path, session_id, model, reply):
            real_write_note(note_path, session_id, model, reply)
            raise KeyboardInterrupt

        with mock.patch("exit_interview._write_note", boom):
            code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 130)
        self.assertIn("stopped", err)
        self.assertIn("%s: could not: stopped" % SID[:8], out)
        payload = self.summary()
        self.assertIs(payload["stopped"], True)
        self.assertEqual(len(payload["sessions"]), 1)
        row = payload["sessions"][0]
        self.assertEqual(row["reason"], "stopped")
        self.assertIsNotNone(row["transcript_written"])
        self.assertIsNotNone(row["copies_written"])
        self.assertTrue((self.out / ("%s.md" % SID[:8])).is_file())

    def test_a_terminate_after_the_note_is_written_exits_143(self):
        """source: a terminate signal after the reply is saved records the stop and uses the terminate code."""
        self.write_transcript()
        script = self.clean_script()
        real_write_note = ei._write_note

        def boom(note_path, session_id, model, reply):
            real_write_note(note_path, session_id, model, reply)
            raise SystemExit(143)

        with mock.patch("exit_interview._write_note", boom):
            code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 143)
        self.assertIn("stopped", err)
        payload = self.summary()
        self.assertEqual(len(payload["sessions"]), 1)
        self.assertEqual(payload["sessions"][0]["reason"], "stopped")

    def test_a_stop_before_the_rename_leaves_no_temp_file(self):
        """source: a stop before the note is renamed leaves no temporary file and still checks the session."""
        self.write_transcript()
        script = self.clean_script()
        with mock.patch("exit_interview.os.replace", side_effect=KeyboardInterrupt):
            code, out, err = self.cli(SID, claude=script)
        self.assertEqual(code, 130)
        self.assertEqual(list(self.out.glob("*.tmp")), [])
        self.assertFalse((self.out / ("%s.md" % SID[:8])).exists())
        payload = self.summary()
        self.assertEqual(len(payload["sessions"]), 1)
        row = payload["sessions"][0]
        self.assertEqual(row["reason"], "stopped")
        self.assertIsNotNone(row["transcript_written"])
        self.assertIsNotNone(row["copies_written"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
