#!/usr/bin/env python3
"""Stand-in that writes the question into a demo transcript, then prints a note.

It looks under $CLAUDE_CONFIG_DIR/projects when that variable is set, otherwise
under $HOME/.claude/projects. It writes only when the transcript's first record
carries "demo": true. Any other transcript is left untouched.
"""

import json
import os
import sys
from pathlib import Path

NOTE = (
    "I had a warning about the kite string that never reached a file. "
    "The next session must not lose the field we were going to use. "
    "No promise is open. "
    "This conversation was about a kite that never flew."
)


def _refuse(message):
    print(message, file=sys.stderr)
    return 1


def _projects_dir():
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config:
        return Path(config) / "projects"
    return Path.home() / ".claude" / "projects"


def main(argv):
    if "--resume" not in argv:
        return _refuse("no session to resume")
    index = argv.index("--resume")
    if index + 1 >= len(argv):
        return _refuse("no session to resume")
    session_id = argv[index + 1]
    if not argv or argv[-1] == session_id:
        return _refuse("no question was given")
    question = argv[-1]
    root = _projects_dir()
    try:
        matches = sorted(path for path in root.glob("*/%s.jsonl" % session_id) if path.is_file())
    except OSError:
        return _refuse("no demo transcript for that session")
    if len(matches) != 1:
        return _refuse("no demo transcript for that session")
    path = matches[0]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0]) if lines else None
    except (OSError, json.JSONDecodeError):
        return _refuse("refusing to write into a transcript that is not marked as a demo")
    if not isinstance(record, dict) or record.get("demo") is not True:
        return _refuse("refusing to write into a transcript that is not marked as a demo")
    extra = {"type": "user", "message": {"role": "user", "content": question}}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(extra, ensure_ascii=False) + "\n")
    sys.stdout.write(NOTE + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        print("refusing to write into a transcript that is not marked as a demo", file=sys.stderr)
        sys.exit(1)
