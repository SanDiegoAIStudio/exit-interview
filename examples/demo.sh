#!/bin/sh
# Four labelled parts. Exits 0 only when each part does what it should.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/exit-interview-demo.XXXXXX")
TMP=$(CDPATH= cd -- "$TMP" && pwd)
trap 'rm -rf "$TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

HOME="$TMP/home"
export HOME
unset CLAUDE_CONFIG_DIR || true

WORK="$TMP/work"
mkdir -p "$WORK"
PROJECTS="$HOME/.claude/projects"
OUT="$TMP/notes"
/bin/cp -f "$ROOT/examples/fake_claude.py" "$TMP/fake_claude.py"
/bin/cp -f "$ROOT/examples/leaky_claude.py" "$TMP/leaky_claude.py"
chmod +x "$TMP/fake_claude.py" "$TMP/leaky_claude.py"
FAKE="$TMP/fake_claude.py"
LEAKY="$TMP/leaky_claude.py"
SID1="4f3a9c2e-7b10-4d55-9e88-1a2b3c4d5e6f"
SID2="8c1d0e2f-6a77-4b33-8f19-0a1b2c3d4e5f"
SHORT1="4f3a9c2e"
SHORT2="8c1d0e2f"

run() {
  env -u CLAUDE_CONFIG_DIR HOME="$HOME" "$@"
}

fail() {
  printf '%s\n' "$1" >&2
  if [ -s "$TMP/cmd.err" ]; then
    cat "$TMP/cmd.err" >&2
  fi
  exit 1
}

capture() {
  captured_out=$(env -u CLAUDE_CONFIG_DIR HOME="$HOME" "$@" 2>"$TMP/cmd.err") && captured_rc=0 || captured_rc=$?
}

hash_of() {
  env -u CLAUDE_CONFIG_DIR HOME="$HOME" python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
}

: >"$TMP/cmd.err"
run python3 - "$PROJECTS" "$WORK" "$SID1" "$SID2" <<'PY'
import json
import sys
from pathlib import Path

projects, work, sid1, sid2 = sys.argv[1:5]
folder = Path(projects) / "picnic"
folder.mkdir(parents=True, exist_ok=True)

def write(sid, content):
    record = {
        "demo": True,
        "cwd": work,
        "type": "user",
        "message": {"role": "user", "content": content},
    }
    (folder / ("%s.jsonl" % sid)).write_text(json.dumps(record) + "\n", encoding="utf-8")

write(sid1, "Plan a picnic that never happened.")
write(sid2, "Sketch a kite the wind never flew.")
PY

TRANSCRIPT1="$PROJECTS/picnic/$SID1.jsonl"
TRANSCRIPT2="$PROJECTS/picnic/$SID2.jsonl"

printf '%s\n' "1. dry-run"
capture python3 "$ROOT/exit_interview.py" --dry-run \
  --projects "$PROJECTS" --out "$OUT" --claude "$FAKE" "$SID1"
printf '%s\n' "$captured_out"
if [ "$captured_rc" -ne 0 ]; then
  fail "dry-run exited $captured_rc"
fi
printf '%s\n' "$captured_out" | grep -q -F -- "transcript:" || fail "dry-run did not show the transcript"
printf '%s\n' "$captured_out" | grep -q -F -- "$TRANSCRIPT1" || fail "dry-run did not name the transcript file"
printf '%s\n' "$captured_out" | grep -q -F -- "folder:" || fail "dry-run did not show the folder"
printf '%s\n' "$captured_out" | grep -q -F -- "$WORK" || fail "dry-run did not name the folder"
printf '%s\n' "$captured_out" | grep -q -F -- "--fork-session" || fail "dry-run did not show the command"
printf '%s\n' "$captured_out" | grep -q -F -- "--no-session-persistence" || fail "dry-run command dropped a flag"
printf '%s\n' "$captured_out" | grep -q -F -- "disableAllHooks" || fail "dry-run command dropped the hooks setting"
if [ -d "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null || true)" ]; then
  fail "dry-run wrote files"
fi

printf '%s\n' "2. first run"
before=$(hash_of "$TRANSCRIPT1")
printf '%s\n' "sha256 before: $before"
capture python3 "$ROOT/exit_interview.py" \
  --projects "$PROJECTS" --out "$OUT" --claude "$FAKE" "$SID1"
printf '%s\n' "$captured_out"
if [ "$captured_rc" -ne 0 ]; then
  fail "first run exited $captured_rc"
fi
after=$(hash_of "$TRANSCRIPT1")
printf '%s\n' "sha256 after: $after"
if [ "$before" != "$after" ]; then
  fail "transcript hash changed on a clean run"
fi
if [ ! -f "$OUT/$SHORT1.md" ]; then
  fail "first run did not write the note"
fi

printf '%s\n' "3. second run"
note_before=$(hash_of "$OUT/$SHORT1.md")
capture python3 "$ROOT/exit_interview.py" \
  --projects "$PROJECTS" --out "$OUT" --claude "$FAKE" "$SID1"
printf '%s\n' "$captured_out"
if [ "$captured_rc" -ne 0 ]; then
  fail "second run exited $captured_rc"
fi
printf '%s\n' "$captured_out" | grep -q -F -- "already has a note" || fail "second run did not say the note was already there"
note_after=$(hash_of "$OUT/$SHORT1.md")
if [ "$note_before" != "$note_after" ]; then
  fail "second run rewrote the note"
fi
again=$(hash_of "$TRANSCRIPT1")
if [ "$before" != "$again" ]; then
  fail "second run changed the transcript"
fi
run python3 -c 'import json,sys; row=json.load(open(sys.argv[1],encoding="utf-8"))["sessions"][0]; assert row["state"]=="no_work", row; assert row["transcript_written"] is None; assert row["copies_written"] is None' "$OUT/summary.json"

printf '%s\n' "4. leak"
capture python3 "$ROOT/exit_interview.py" \
  --projects "$PROJECTS" --out "$OUT" --claude "$LEAKY" "$SID2"
printf '%s\n' "$captured_out"
printf '%s\n' "exit code: $captured_rc"
if [ "$captured_rc" -ne 3 ]; then
  fail "leak run exited $captured_rc"
fi
printf '%s\n' "$captured_out" | grep -q -e '^LEAK:' || fail "leak run did not start its last report with LEAK"
printf '%s\n' "$captured_out" | grep -q -F -- "$SHORT2" || fail "leak run did not name the session"
run python3 -c 'import sys; data=open(sys.argv[1],encoding="utf-8").read(); assert "EXIT INTERVIEW." in data' "$TRANSCRIPT2"
