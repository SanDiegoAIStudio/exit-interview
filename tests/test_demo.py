#!/usr/bin/env python3
"""Tests for the demo and its stand-ins. Fixtures are written into a temp folder."""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import exit_interview as ei

FAKE = ROOT / "examples" / "fake_claude.py"
LEAKY = ROOT / "examples" / "leaky_claude.py"
DEMO = ROOT / "examples" / "demo.sh"
SID = "6d6d6d6d-7e7e-4f4f-8a8a-1b1b1b1b1b1b"


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.home / ".claude"
        self.projects = self.config / "projects"
        self.projects.mkdir(parents=True)
        self.work = self.root / "work"
        self.work.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.config),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.temp.cleanup()

    def env_for(self, home, config=None):
        env = {"PATH": os.environ["PATH"], "HOME": str(home)}
        if config is not None:
            env["CLAUDE_CONFIG_DIR"] = str(config)
        for key in ("LANG", "LC_ALL", "TMPDIR"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        return env

    def write_transcript(self, folder, sid, demo):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("%s.jsonl" % sid)
        record = {"demo": demo, "cwd": str(self.work), "type": "user",
                  "message": {"content": "Invent a picnic."}}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return path

    def run_leaky(self, env, sid):
        return subprocess.run(
            [sys.executable, str(LEAKY), "-p", "--resume", sid, "--tools", "", ei.QUESTION],
            env=env, capture_output=True, text=True, cwd=str(self.work),
        )

    def test_fake_claude_prints_a_note_and_touches_nothing(self):
        """source: the demo stand-in answers with a made-up note and writes no file."""
        folder = self.root / "scratch"
        folder.mkdir()
        transcript = folder / "session.jsonl"
        transcript.write_text('{"demo": true}\n', encoding="utf-8")
        before = transcript.read_bytes()
        proc = subprocess.run(
            [sys.executable, str(FAKE), "-p", "--resume", SID, "--fork-session", "--tools", ""],
            cwd=str(folder), env=self.env_for(self.home, self.config),
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.strip())
        self.assertNotIn("EXIT INTERVIEW.", proc.stdout)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(transcript.read_bytes(), before)
        self.assertEqual(list(folder.iterdir()), [transcript])

    def test_leaky_appends_only_when_the_config_dir_transcript_is_a_demo(self):
        """source: the leaky stand-in writes the question only into a demo transcript under the config dir."""
        home = self.root / "real-home"
        config = self.root / "config"
        home_file = self.write_transcript(home / ".claude" / "projects" / "place", SID, False)
        config_file = self.write_transcript(config / "projects" / "place", SID, True)
        home_before = home_file.read_bytes()
        proc = self.run_leaky(self.env_for(home, config), SID)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip())
        self.assertEqual(proc.stderr, "")
        self.assertEqual(home_file.read_bytes(), home_before)
        written = config_file.read_text(encoding="utf-8")
        self.assertIn(ei.MARK, written)
        self.assertEqual(len(written.splitlines()), 2)

    def test_leaky_uses_home_when_the_config_dir_is_unset(self):
        """source: without a config dir, the leaky stand-in looks under the home projects folder."""
        home = self.root / "only-home"
        path = self.write_transcript(home / ".claude" / "projects" / "place", SID, True)
        proc = self.run_leaky(self.env_for(home, None), SID)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(ei.MARK, path.read_text(encoding="utf-8"))

    def test_leaky_refuses_a_transcript_that_is_not_a_demo(self):
        """source: the leaky stand-in exits with one plain line and writes nothing unless the transcript is a demo."""
        home = self.root / "only-home"
        path = self.write_transcript(home / ".claude" / "projects" / "place", SID, False)
        before = path.read_bytes()
        proc = self.run_leaky(self.env_for(home, None), SID)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("Traceback", proc.stderr)
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)

        missing = self.run_leaky(self.env_for(home, None), "abababab-cdcd-4efe-8f8f-121212121212")
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(missing.stdout, "")
        self.assertNotIn("Traceback", missing.stderr)
        self.assertEqual(len([line for line in missing.stderr.splitlines() if line.strip()]), 1)

    def test_the_demo_runs_all_four_parts(self):
        """source: the demo shows a dry run, a clean note with an unchanged transcript, a second run that does not call, and a leak."""
        before = sorted(self.projects.rglob("*"))
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CLAUDE_CONFIG_DIR"] = str(self.config)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        proc = subprocess.run(
            ["/bin/sh", str(DEMO)], cwd=str(ROOT), env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)
        self.assertEqual(proc.stderr, "")
        out = proc.stdout
        for label in ("1. dry-run", "2. first run", "3. second run", "4. leak"):
            self.assertIn(label, out)
        hashes = re.findall(r"sha256 (before|after): ([0-9a-f]{64})", out)
        self.assertGreaterEqual(len(hashes), 2)
        self.assertEqual(hashes[0][1], hashes[1][1])
        self.assertIn("already has a note", out)
        self.assertRegex(out, r"(?m)^LEAK:.*8c1d0e2f")
        self.assertIn("exit code: 3", out)
        self.assertEqual(sorted(self.projects.rglob("*")), before)
        self.assertTrue(os.access(str(FAKE), os.X_OK))
        self.assertTrue(os.access(str(LEAKY), os.X_OK))
        self.assertTrue(os.access(str(DEMO), os.X_OK))
        self.assertEqual(stat.S_IMODE(FAKE.stat().st_mode) & stat.S_IXUSR, stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main(verbosity=2)
