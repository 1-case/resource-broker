# resource-broker

[![CI](https://github.com/1-case/resource-broker/actions/workflows/ci.yml/badge.svg)](https://github.com/1-case/resource-broker/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

*[日本語版はこちら / Japanese version](README.md) — the Japanese README is canonical.*

**A bulletin board that keeps concurrent Claude Code sessions on one machine from
fighting over finite resources.**

Session A is training on the GPU. Session B, knowing nothing about A, grabs the same GPU.
Both die with OOM. resource-broker avoids this by making sessions **declare before they use,
and by making those declarations visible to everyone else**.

```console
$ rb status
GPU0                     使用中    実測で使用を確認、または宣言が有効
                         宣言   folnet / E061 training: 3 arm × 3 seed
                         since  2026-08-13T19:05:58+09:00 (2h17m elapsed)
                         ETA    9h (around 2026-08-14T04:05:58+09:00)  ※ a claim, not a promise
                         見積   peak VRAM 1.4GB / avg VRAM 1.3GB
                         相乗り allowed (up to 5GB VRAM remaining)
```

> **Note on language.** The CLI, the hook messages and all design documents are in Japanese.
> This file is a summary for readers who do not read Japanese; it is not a full translation.

## What it does *not* do

The character of this tool is easiest to see from what it refuses to do.

- **It never inspects a resource.** No `nvidia-smi`, no serial-port probing — there is no
  branch on resource ID anywhere in the source. (It does read OS-level facts about the
  *declarer*: machine boot time and whether a PID is alive. Those are how a stale declaration
  is proven stale; they are not probes of any resource.)
  Investigating is the session's job. The tool only makes the session *declare what it saw*
  and stamps a machine-generated timestamp on it.
- **It never blocks a command.** The hooks print a notice; they never `deny`.
  A tool cannot know whether a given command touches a resource, and guessing produces
  false positives (it once blocked the author's own documentation edit).
- **It never interprets a declaration.** ETA, usage estimates and sharing policy are all
  free text carried verbatim. Units and scales differ per resource — GB of VRAM, CPU cores,
  API requests per minute — so the tool refuses to add them up or compare them.
- **It never stops your work when it breaks.** Internal errors, a corrupted board, missing
  information: all exit 0. It reports "busy" only when it positively confirmed busy.

The consequence: **adding a resource requires no code change.** In practice, resources nobody
designed for — CPU cores, a serial port (LiDAR), an external API rate limit,
a git working tree — all landed on the board unmodified.

**But it cannot stop anyone who does not declare.** A session that grabs a resource without
going through the board — or any non-Claude-Code process — is invisible to this tool and
cannot be stopped. **This has been observed** (a job held the GPU while the board was empty).
All that can be claimed is "collisions are avoidable if everyone follows the convention".
It does not make accidents impossible. Having chosen not to enforce, that is by design.

## What counts as a resource

**One criterion only:**

> If the work could collide with another session, and a collision would have **serious
> consequences** (a failed job, corrupted data, hours of rework), declare it as a resource.

The kind of resource does not matter. The examples above are things that *happened* to be
declared, not a list. **Enumerating resources would make the listed ones first-class citizens
and render the rest invisible** — the same failure mode as hard-coding how to probe them.

Conversely, do not declare things whose collision is cheap (read-only work, short operations
you can simply redo). A board full of noise dulls the reader's judgement.

## Install

**Installing it as a plugin is the shortest path** — two lines inside Claude Code:

```
/plugin marketplace add 1-case/resource-broker
/plugin install resource-broker@resource-broker
```

That brings in the three hooks and the `rb` command together. **No `uv tool install` needed**:
the package has zero dependencies, so the launcher in `bin/` just works.

Hooks are a snapshot taken at session start, so **already-running sessions are unaffected**
until they restart. The only requirement is Python 3.11+.

**If you installed by hand before, remove the three rb hooks from `~/.claude/settings.json` first.**
Otherwise every hook fires twice, doubling the per-turn injection and printing the board twice
in a row — which reads as two separate resources. (Observed, not theoretical.)

<details>
<summary>Installing by hand instead</summary>

Paste this to Claude Code:

```
Install https://github.com/1-case/resource-broker for me.

1. uv tool install git+https://github.com/1-case/resource-broker
   (this puts the `rb` command on PATH)
2. Clone the repo and register the three hooks in ~/.claude/settings.json:
   - SessionStart      -> hooks/sessionstart_notice.py
   - UserPromptSubmit  -> hooks/prompt_board_reminder.py
   - PreToolUse(Bash)  -> hooks/pretooluse_notice.py
   Do not delete existing hooks. **Merge, do not overwrite**, and back the file up first.
3. Verify that `rb status` works.
```

Requires [uv](https://docs.astral.sh/uv/) for this path.
</details>

## Use

`--job`, `--observed` and `--eta` are all mandatory. **That requirement is the only thing this
tool enforces.**

```console
# Wrapper: declares, logs, and always releases on exit (recommended)
$ rb run --res GPU0 --job "E059 eval" --observed "nvidia-smi: 0 compute processes" \
         --eta 40m --found free -- python train.py

# Manual
$ rb claim GPU0 --job "..." --observed "..." --eta 40m
$ rb release GPU0

# Share a resource someone else holds (read their --sharing first)
$ rb run --res GPU0 --join --job "..." --observed "..." --eta 10m -- python small.py

# Wait until the set of holders shrinks (not only until it is fully free)
$ rb wait GPU0

# Compare what you predicted against what actually happened
$ rb history GPU0
```

`--eta` is mandatory **not because an accurate number is wanted, but to force one moment of
thought**. The tool never acts on it — not for ghost detection, not for cutting off a wait.

**Always check the board with bare `rb status` (all entries). Never query a single name.**
A bulletin board is something you read in full; it is not an index you look names up in.
One machine does not have many resources, so reading everything is free. Resource IDs are
free text, so spellings drift — `GPU0` and `gpu0` are *different resources* — and a name
lookup silently answers "free" while someone holds the other spelling. This actually happened:
a job held `gpu0` for 7.3 hours while `rb status GPU0` reported it free.

## The cost (it eats context every turn)

This tool **interposes on every prompt and every Bash call**. Here is what you pay, not just
what you get.

| Hook | Fires | Injection |
|---|---|---|
| `UserPromptSubmit` | **every prompt** | ~40 chars when nothing is declared; one line per declaration otherwise, capped at 8 entries / 1200 bytes |
| `PreToolUse`(Bash) | only Bash commands matching a small pattern table | exits immediately on no match |
| `SessionStart` | once per session | the longest, since it carries the usage explanation |

**Only `UserPromptSubmit` accumulates turn after turn.** It is deliberately minimal — the usage
explanation was moved to `SessionStart`, cutting it from 182 to 45 characters — but it is not
zero, and a long conversation pays it once per turn.

You pay latency too. A hook's floor is Python's startup (measured 68-75 ms on an idle machine);
the hook's own work is lost in the noise. Every Bash call costs that.

**Whether it is worth it depends entirely on how often your resources actually collide.**
If you run one session at a time, you pay and get nothing. It pays off only when several
sessions share one machine's resources and a collision costs you hours of rework.

## Why it still matters once agents can talk to each other

If Claude Code sessions could message each other directly, would this board become obsolete?
**I don't think so.**

- **Communication needs something to talk *about*.** Knowing who currently holds the GPU
  requires shared state somewhere. The board is that state; messages flow on top of it.
- **It works without a live counterpart.** The board is readable whether the other session
  crashed, or hasn't started yet. Messaging assumes someone is alive to answer.
- **It persists asynchronously.** "Started at 19:05, expected to take 9 hours" stays there
  without anyone asking. A conversation only exists for the two parties who had it.
- **Humans can read it.** `rb status` is for people too.

What changes when messaging arrives is how you *wait*: instead of `rb wait` polling for the
holder set to shrink, you identify the holder from the board and ask them directly.
**The board isn't replaced — it becomes what supplies the reason to reach out.**

`PreToolUse` **prints nothing unless a pattern table (`guard.json`) exists**, and none is
shipped — so out of the box the third hook stays silent. You decide what deserves a notice:

```jsonc
// %LOCALAPPDATA%
esource-broker\guard.json  (elsewhere: ~/.resource-broker/guard.json)
{ "schema": 1, "patterns": [
    { "pattern": "run_e\d+\.py", "resource": "GPU0", "note": "training scripts" } ] }
```

A stale table is harmless: it stops matching, so notices stop — nothing is ever blocked.

## Design notes

- **Measurement is treated asymmetrically.** "Busy" is decisive on its own. "Free" **never**
  proves a declaration is stale — declarations are made *before* the job grabs the resource
  (model loading takes minutes). Treating the two symmetrically means being robbed the instant
  you declare.
- **Only three grounds may displace a declaration:** it predates the last boot
  (`since < boot`), or (grace elapsed **and** measured free **and** the declaring process is
  dead) all three together, or an explicit `release` / `--force`.
- **Every timestamp is machine-generated.** An LLM is never asked to write or estimate a time.
- **Exclusion rests on a nonce compare-and-swap, not on a lock.** The lock is a performance
  optimization; correctness is identical whether or not it is acquired. Verified end-to-end by racing
  5 real processes against one board: exactly one wins.
- **One file per resource.** Damage stays local and `O_EXCL` settles acquisition races.

Design rationale — and the failures that produced it — lives in
[docs/DESIGN.md](docs/DESIGN.md) (Japanese). Most of its paragraphs were written after
hitting the problem in production, not before.

## Trust boundary and security

The board and the audit log live in **`%LOCALAPPDATA%
esource-broker\`** (`~/.resource-broker/`
elsewhere); `RESOURCE_BROKER_HOME` moves them. They hold `board/` (declarations), `audit/`
(the audit log) and `logs/` (`rb run` output), and are **never created inside the repository**.
`logs/` is pruned after 7 days; **`audit/` is never pruned** (`rb wait` appends a line every
10 seconds, so it grows monotonically). Delete it by hand when you no longer need it.

**The board is plaintext and is not encrypted.** Every session that reads or writes it runs as
the same OS user on the same machine, so any process that can read the ciphertext can read the
key too. There is nothing to protect, while a key problem would mean "the board is unreadable",
i.e. every session stops, and the audit log would no longer be readable with ordinary tools.

The real attack surface is **prompt injection**: free text from other sessions flows directly
into your context. Injected lines are marked as data rather than instructions, truncated by
byte length, and stripped of newlines and control characters.

Two things are on you:

- Do not put secrets in `--job` or `--observed`.
- Do not paste the board or the audit log somewhere public — they contain project names and
  working paths.

The board itself (`board/`, `audit/`, `*.log`) is excluded via `.gitignore`.

## How this was built

**The problem framing and the design calls are the author's
([satorunnlg](https://github.com/satorunnlg)); the code, tests and documents were drafted by
Claude Code (Claude Opus) under the author's direction, review and selection.**
The author is responsible for what ships.

Framing the problem as "concurrent sessions fighting over resources", insisting on resource
agnosticism, abandoning `deny` in favour of notices, and the reframing that a bulletin board
is read in full — all of those were the author's calls. Claude Code (Claude Opus) did the
implementation; review ran as a double loop of an independent Claude agent and Codex.

Most of the design was settled **only after hitting real failures in production use**:
a display name that hid which resource was held; a session that gave up on a GPU whose holder
had explicitly allowed sharing; a forgotten release that made another session wait 2 h 48 m;
`gpu0` and `GPU0` silently becoming different resources. Each of those is recorded in [docs/DESIGN.md](docs/DESIGN.md) as *why the design is what it is*.

## License

This is an unofficial, third-party tool and is not affiliated with or endorsed by
Anthropic. Claude and Claude Code are trademarks of Anthropic, PBC.

Apache License 2.0 ([LICENSE](LICENSE)).
