# Contributing

These rules apply to all code in this repository.

## Development environment

`uv sync` creates the virtual environment with the development tools; then run `uv run pytest`, `uv run ruff check .` and `uv run ruff format .`, and `shellcheck` on shell scripts. This is for contributors only: users never need `uv` or a virtual environment, and run everything through the `./steamshare` launcher with the system's `python3`.

## 0. Clean, scalable, and easy to understand

This rule overrides convenience. A reader new to the project should understand any module by reading it once.

- **Names carry the meaning.** Functions say what they do (`find_invalid_windows_names`, not `check`), and variables say what they hold. Avoid abbreviations unless they are domain terms (`vdf`, `acf`, `ntfs`).
- **Small units, one job each.** A function does one thing. If it needs an "and" to describe it, split it.
- **Comments explain why, not what.** The code shows what happens. A comment explains the reason, the constraint, or the trap. Typical examples: why NTFS rejects a name, why Steam must be closed, which Steam behavior a workaround targets. Each module starts with a docstring that explains its purpose and where it fits.
- **Domain knowledge lives in one place.** Rules such as Windows-reserved names or Steam file layouts are defined once, as named constants with a comment pointing to the source, and reused everywhere.
- **Built to grow.** New behavior is added by adding a unit (a new rule, a new adapter, a new check for `doctor`), not by adding branches to an existing function.
- **No cleverness.** Prefer the obvious version over the shortest one.

## 1. No third-party runtime dependencies

SteamOS has a read-only root filesystem. Packages installed with `pacman` or a system-wide `pip install` either fail or are wiped by the next OS update. At runtime, the tools may only use:

- Python 3.11+ standard library
- `bash`
- `systemd` and core utilities (`mount`, `modprobe`, `findmnt`)

Development and CI tools (`pytest`, `ruff`, `shellcheck`, `bats`) are allowed, but never imported or called at runtime. VDF files are parsed by our own small parser, not the `vdf` package.

## 2. Logic lives in Python; shell is a thin wrapper

Bash is only for what cannot reasonably be done elsewhere: calling `modprobe`, `mount` or `systemctl`, and bootstrapping the installer. A shell script must not contain decisions that need tests. If it grows a branch worth testing, move that logic to Python.

## 3. Light hexagonal architecture

```
src/steamshare/
  core/       pure logic: VDF parsing, Windows-name rules, which apps to force to Proton
  adapters/   I/O: filesystem, mounts, systemd, Steam process detection
  cli/        argument parsing and wiring; no business logic
```

- `core` imports nothing from `adapters` or `cli`, and does no I/O.
- Adapters implement small interfaces (`typing.Protocol`) defined next to the core code that uses them.
- Unit tests for `core` never touch the disk, the network, or real processes.

## 4. Strict TDD

1. Write a failing test.
2. Write the minimum code to make it pass.
3. Refactor with the tests green.

No new behavior without a test. A bug fix starts with a test that reproduces the bug.

- `tests/unit/`: pytest, fast, no I/O.
- `tests/integration/`: real NTFS images on loop devices, root required; runs in CI.
- Shell scripts: `bats`.

## 5. Safe by default

These tools write to Steam's config, to `/etc`, and to a disk Windows also uses.

- **Back up before writing.** Every write to a Steam or system file makes a timestamped backup first.
- **Idempotent.** Running a command twice leaves the system in the same state as running it once.
- **`--dry-run`.** Every command that changes state supports it, and it prints exactly what would change.
- **Steam must be closed.** Any command that edits Steam files refuses to run while Steam is open.
- **No deleting without confirmation.** Nothing is deleted unless the user explicitly confirms it.

## 6. Style

**Python:**
- Type hints on all public functions.
- Formatted and linted with `ruff` (`ruff format`, `ruff check`).
- Prefer `pathlib`, dataclasses and plain functions over class hierarchies.

**Bash:**
- Start with `set -euo pipefail`.
- Pass `shellcheck` with no warnings.
- Quote every expansion.

**PowerShell (Windows side):**
- Compatible with Windows PowerShell 5.1, the version every Windows ships, so users install nothing. No ternary, no `??`, no `&&` or `||` between commands, no `ForEach-Object -Parallel`, and no cmdlets that exist only in PowerShell 7. Keep files ASCII: 5.1 reads UTF-8 without a BOM as the ANSI code page.
- Start every script and module with `Set-StrictMode -Version Latest` and `$ErrorActionPreference = 'Stop'`.
- Use approved verbs only (`Get-Verb` lists them).
- Pass PSScriptAnalyzer with its default rules and no findings. Suppress a rule only on the one function that needs it, with a justification.
- Test with Pester 5. CI runs the tests under both 5.1 and PowerShell 7.
- Wrap every external effect (registry, native commands, `Get-Volume` and other CIM queries, file reads, prompts) in a small function of its own, and keep the decisions out of it. Tests mock those wrappers, never the real cmdlets, so they also run on Linux.

**Language:** code, comments, docs, and commit messages are in English.

## 7. Errors say what to do next

Every user-facing error states the cause and the next step. For example:

> `ntfs3` is blacklisted by `/etc/modprobe.d/zorin-blacklist-ntfs3.conf`. Run `sudo modprobe ntfs3`, or re-run the installer, which loads it at boot.

Never print a bare traceback for an expected failure.

## 8. Commits

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- Keep commits small, one logical change each. Tests go in the same commit as the code they cover.
- No AI attribution or `Co-Authored-By` trailers.
