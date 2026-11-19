exit-interview asks a Claude Code session for what it is still carrying and has not written down, then saves the answer as a note. The conversation is already on disk, so the note is the part that never reached a file: intentions, half-made decisions, warnings, and promises still open.

## What you get

A command that resumes each session you name, asks four questions, and writes the reply to a note. After every call it checks that the saved transcript did not gain the question and that no copy of the conversation was saved beside it. A summary records which sessions were interviewed, which already had a note, and which could not be interviewed.

```
python3 exit_interview.py [--out DIR] [--projects DIR] [--claude PATH] [--model NAME] [--timeout SEC] [--dry-run] [--pass-env NAME] SESSION [SESSION ...]
```

`SESSION` is a session id, or the path of a transcript file whose name ends in that id plus `.jsonl`. Sessions run one after another, in the order you give them.

Defaults: `--out` is `exit-notes`; `--projects` is `$CLAUDE_CONFIG_DIR/projects` when that variable is set, and `$HOME/.claude/projects` otherwise; `--claude` is `claude` as found on `PATH`; `--model` is `sonnet`; `--timeout` is `300` seconds.

`--pass-env NAME` hands one more environment variable to the command and may be given more than once.

At the time limit the tool stops the command and every process it started, then runs its checks.

A bare name is looked up on PATH only, and a name with a slash in it is taken as that file. The command is given no standard input.

## Try it without Claude Code

```
sh examples/demo.sh
```

The demo runs a stand-in, so it needs no sign-in and makes no model call.

```
1. dry-run
session: 4f3a9c2e-7b10-4d55-9e88-1a2b3c4d5e6f
transcript: <temp>/home/.claude/projects/picnic/4f3a9c2e-7b10-4d55-9e88-1a2b3c4d5e6f.jsonl
folder: <temp>/work
command: <temp>/fake_claude.py -p --resume 4f3a9c2e-7b10-4d55-9e88-1a2b3c4d5e6f --fork-session --no-session-persistence --model sonnet --tools '' --strict-mcp-config --settings '{"disableAllHooks":true}' 'EXIT INTERVIEW. This session is about to be closed. Your whole conversation is already saved on disk and can be reopened at any time, so nothing recorded is at risk. Before it closes, answer four questions in your own voice.

1. What are you carrying that is not yet written to any file: intentions, half-made decisions, warnings, context you were relying on, open threads with people?
2. What is the one thing the next session, or the person you work for, must not lose?
3. Is any promise made to that person in this conversation still unmet?
4. In one line: what was this conversation really about, beneath its task?

Plain sentences, under 250 words, no headers. Do not summarize work that is already saved to disk. If everything you know is already on disk, say exactly that in one line.'
2. first run
sha256 before: 5a2743a0a179e448340968438862eee416138795484b4b7e0abb2ec607d36e8d
<temp>/notes/4f3a9c2e.md written (230 characters)
1 notes written, 0 already there, 0 could not
sha256 after: 5a2743a0a179e448340968438862eee416138795484b4b7e0abb2ec607d36e8d
3. second run
4f3a9c2e already has a note
0 notes written, 1 already there, 0 could not
4. leak
<temp>/notes/8c1d0e2f.md written (199 characters)
LEAK: 8c1d0e2f: a reader that judges a session by its newest record may now see the interview
exit code: 3
```

`<temp>` stands for the temp folder the demo makes, which changes on every run. The hash changes on every run too, because the made-up transcript records that folder. The two hashes in a single run match.

## Quick start with a real session

Transcripts are stored under the projects folder, one directory per working folder, one file per session: `$HOME/.claude/projects/<project>/<session-id>.jsonl`. The session id is that file name without `.jsonl`.

```
python3 exit_interview.py SESSION_ID
```

The note is `exit-notes/<first 8 characters of the session id>.md`. Pass `--out` to put it somewhere else. `--dry-run` prints the transcript it found, the folder the session ran in, and the exact command, and it writes nothing.

## The four questions

```
EXIT INTERVIEW. This session is about to be closed. Your whole conversation is already saved on disk and can be reopened at any time, so nothing recorded is at risk. Before it closes, answer four questions in your own voice.

1. What are you carrying that is not yet written to any file: intentions, half-made decisions, warnings, context you were relying on, open threads with people?
2. What is the one thing the next session, or the person you work for, must not lose?
3. Is any promise made to that person in this conversation still unmet?
4. In one line: what was this conversation really about, beneath its task?

Plain sentences, under 250 words, no headers. Do not summarize work that is already saved to disk. If everything you know is already on disk, say exactly that in one line.
```

## What the call is allowed to do

The command is fixed. An empty tool list and a strict server config leave the resumed model able to answer and without a tool to act with. Only the model's tools, the extra servers and the hooks are turned off. Anything else the `claude` command does when it starts in that folder, such as reading the folder's settings and instruction files, still happens. The fork flags are what keep the session's own transcript untouched and write no second one.

| Flag | What it is for |
| --- | --- |
| `-p` | Ask without opening a screen |
| `--resume` | Continue the named session |
| `--fork-session` | Work on a fork, so the original transcript is not the one being written |
| `--no-session-persistence` | Do not save that fork |
| `--model` | Choose the model that answers |
| `--tools` with an empty value | Leave the model able to speak and unable to act |
| `--strict-mcp-config` | Do not load servers that could act |
| `--settings {"disableAllHooks":true}` | Turn hooks off |

## How it proves it left the transcript alone

Before the call, the tool records the transcript's size and the `.jsonl` names in its folder. After every call, including a failure and a timeout, it does two checks.

The transcript check reads only the bytes gained since that size. `transcript_written` is true when those bytes contain `EXIT INTERVIEW. This session is about to be closed.` It is false when they do not. It is null when the file is gone or shorter than it was, because the result cannot be told. The session's own new records do not contain that sentence, so they are not a leak. A copy of the sentence that was already in the file before the call is before the recorded size, so it does not count.

The copy check lists `.jsonl` files that appeared in the folder after the call and hold that sentence in their last million bytes. A new session that opened beside it and does not hold the sentence is not listed. `copies_written` is null when the folder, or a file that appeared in it, could not be read.

Exit 3 prints a line starting with `LEAK:` and names each session by its first 8 characters. A leak costs you this: any reader that judges a session by its newest record may now see the interview.

Exit 4 prints a line starting with `UNCHECKED:` and names every session whose call could not be checked. That run is never reported as clean.

## Prove it on your machine

Use a throwaway session.

1. Start one in an empty folder, send it a message, and quit it.

```
mkdir /tmp/exit-interview-proof
cd /tmp/exit-interview-proof
claude
```

2. Print the transcript's SHA-256 and list its folder. The new file is under `$HOME/.claude/projects/`, and its name is the session id plus `.jsonl`.

```
python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$HOME/.claude/projects/PROJECT/SESSION_ID.jsonl"
ls "$HOME/.claude/projects/PROJECT"
```

3. Go back to the folder that holds `exit_interview.py` and run the tool on that session.

```
python3 exit_interview.py SESSION_ID
```

4. Print the SHA-256 and list the folder again, with the same two commands. The hash matches, and the `.jsonl` names are the same.

## The three states

Each session ends in one state.

| State | Meaning |
| --- | --- |
| `did_work` | The note was written. `length` is the reply's length. |
| `no_work` | A note for this session was already there, so no call was made and no check was needed. |
| `could_not` | There was no transcript, the session's folder was missing or was not recorded, the command could not start, it timed out, it exited non-zero, the reply was empty, the note could not be written, or the run was stopped while that session was being asked. |

`exit-notes/summary.json` holds the time of the run and, for each session, its id, state, reason, length, `transcript_written`, and `copies_written`. Reason and length are null when they do not apply. The two check fields are null when no call was made, or when a call was made and that check could not be told. When the command exits non-zero or times out, the reason gives the exit code or the time limit, then up to 300 characters of what it printed, standard error first.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | At least one session was interviewed or already had a note, and nothing leaked. A dry run that is not a usage error also exits 0. |
| 1 | Every session could not be interviewed. It is also the exit when the output folder cannot be created or summary.json cannot be written, with a line on stderr that starts `could not create the output folder:` or `could not write summary:`. |
| 2 | Usage error: no session, an unknown option, a timeout that is not a positive number or is above 2147483 seconds, a `--pass-env` value that is empty or holds `=`, or `--claude` was not found. One line on stderr. |
| 3 | A leak. The question landed in a transcript or in a new copy. |
| 4 | A call was made and a check could not be told. |

A stop (Ctrl-C, a terminate signal or a closed terminal) exits 130, 143 or 129. Before it exits the tool stops the command, checks the session that was being asked, writes summary.json for the sessions it reached with `"stopped": true`, and prints the LEAK or UNCHECKED line when one applies. A stopped run is never reported as clean. A stop that lands after the command answered still records that session as `could_not` with the reason `stopped`. When its note was already in place, the note stays, and the next run reads that session as already having a note.

A leak outranks an unchecked session. An unchecked session outranks the ordinary counts. When every session could not, and none of those checks failed open, the exit is 1.

## Limits

A real run needs the `claude` command installed and signed in. The command runs with a short list of environment variables and nothing else, PATH, HOME, USER, LOGNAME, SHELL, TMPDIR, LANG, LC_ALL, TERM, CLAUDE_CONFIG_DIR, ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN. A setup that signs in or reaches the network through any other variable, a proxy in HTTPS_PROXY for example, needs `--pass-env HTTPS_PROXY`, once for each name. This page names no CLI version. The after-call check is what tells you about yours.

A session that is open and quotes the question while the call runs reads as a leak, and that false alarm is the cost of failing loudly. The session must be resumed from the folder it ran in, so a session whose folder is gone cannot be interviewed. Each interview reads the session's whole conversation once, so a long session is one long model call. Sessions run one at a time.

A note is named after the first eight characters of the session id, so two sessions that share those characters share one note name, and the second one reads as already having a note. A process that moved itself into a session of its own is out of reach of the time limit.

A note can hold anything the session knew, so read it before you share it, and `exit-notes/` is in `.gitignore` for that reason.

summary.json describes the latest run only, so a later run replaces the record of an earlier one. The folder a session is resumed from is the first working folder recorded near the start of its transcript, so a session that moved to another folder later is still resumed from the first.

When the same session id is found in more than one project folder, the first in sorted order is used, and only that folder is watched. A session named by the path of its transcript is watched at that path, so when the path is a copy outside the folder where `claude` keeps the session, the checks watch the copy and a leak into the session's own transcript is not seen. When the transcript's size or its folder cannot be read before the call, the call is still made and the session reads as unchecked. When the program reading the tool's output has already closed it, the notes and summary.json are still written and Python exits 120 with a broken pipe message.

It needs a POSIX system, because it uses process groups and the hang-up signal. It was tested on macOS with Python 3.9 and Python 3.14.

## Tests

From this folder:

```
python3 -m unittest discover -s tests -v
python3 tests/mutate.py
```

The first command runs 65 tests. Every test runs a stand-in for `claude`, never the real command, and a test that names `claude` without a path gives it a PATH that holds only a stand-in. The tests stop only processes they started or recorded.

`python3 tests/mutate.py` plants one bug at a time in a copy of `exit_interview.py` and runs the tests against each one, with the same Python that runs it. A bug counts as caught only when a test fails on an assertion. A real run ends with:

```
caught 51 of 51
```

It exits 0 when every bug is caught and 1 when one is not. It exits 2 when a planted edit is not in the tool exactly once, or when the tests fail on the unchanged copy.

## License

MIT. The terms are in the LICENSE file.

## Built at San Diego AI Studio

This tool comes out of San Diego AI Studio, the builders' community in San Diego, where builders show the systems they run. More projects: https://sandiegoaistudio.com/projects
