# resource-broker

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

- **It never inspects a resource.** No `nvidia-smi`, no serial-port probing, nothing.
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
designed for — CPU cores, a serial port (LiDAR), an external API rate limit, a SQLite file,
a TCP port, a git working tree — all landed on the board unmodified.

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

**Ask Claude Code to do it.** Paste this:

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

Hooks are a snapshot taken at session start, so **already-running sessions are unaffected**
until they restart.

Requirements: Python 3.13+ and [uv](https://docs.astral.sh/uv/). There are no dependencies.

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
  optimization; correctness is identical whether or not it is acquired. Verified by racing
  12 real processes: exactly one wins.
- **One file per resource.** Damage stays local and `O_EXCL` settles acquisition races.

Design rationale lives in [DESIGN.md](DESIGN.md); the record of failures actually hit — which
is arguably more useful than the code — lives in [EXPERIMENTS.md](EXPERIMENTS.md).
Both are in Japanese.

## Trust boundary and security

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

## License

Apache License 2.0 ([LICENSE](LICENSE)).
