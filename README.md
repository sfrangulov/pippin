# pippin

A tiny agent harness for Apple's on-device model (AFM 3 Core Advanced via [apfel](https://github.com/Arthur-Ficial/apfel)).
Like opencode, but for everyday Mac chores instead of coding: read a file, write a short script, run a command,
set a reminder or a routine. Tools, skills and MCP servers all fit into the model's 8,192-token window.
One Python file, standard library only, everything stays on the Mac.

## Install

Needs macOS 26+ with Apple Intelligence turned on, [apfel](https://apfel.franzai.com) and Python 3.9+.

```sh
brew install apfel
git clone https://github.com/sfrangulov/pippin ~/pippin
ln -s ~/pippin/pippin.py ~/.local/bin/pippin            # any folder on your PATH
mkdir -p ~/.pippin && cp ~/pippin/config.example.json ~/.pippin/config.json   # optional: MCP servers
pippin --doctor
```

## Use

```
pippin                                   # chat
pippin "что лежит в Downloads и что там самое большое?"
cat notes.txt | pippin "translate to English"
pippin -y "каждые 45 минут напоминай попить воды"
pippin --doctor                          # server, tools, skills, MCP, prompt size
```

Chat commands: `/ctx` (token use), `/tools`, `/skills`, `/yes` (auto-approve), `/reset`, `/exit`.

## How it works

```
your words ─┬─ skill triggers (regex) ──┐
            └─ router (guided JSON, 0.4 s, only when no trigger fired)
                                        ▼
system prompt (≈300 tok) + active skills + tools offered this turn
                                        ▼
apfel --serve  (OpenAI API, native tool calls via guided generation)
                                        ▼
tool loop (≤ 8 steps): read_file · list_dir · write_file · run · notify* · routine* · MCP tools
```

- **apfel server.** Uses one on `127.0.0.1:11434` if running, otherwise starts a private
  `apfel --serve --permissive` on a free port and stops it on exit (~1 s extra).
- **Context budget.** Base prompt with all tools is ~1.7k tokens; ~5.5k is left for the chat.
  Tool results are capped at 2.4k chars, older ones shrink to 300 chars, old turns drop first.
  apfel also trims history newest-first on its own and keeps the system prompt.
- **Skills are picked by the harness, not the model.** A 3B model misuses a `skill` tool (it loaded
  "remind" to translate a todo list), so skills fire on regex triggers in the user's words. A
  guided-JSON router fills the gaps for instruction-only skills. A skill stays for its turn and one
  follow-up. The `skill` tool still exists: `"skill_tool": true` in config.
- **Gated tools.** `notify` and `routine` are offered only while the `remind` / `routine` skill is
  active. Without this the model turned "remind me in 1 minute" into a daily `df -h` job and made
  reminders out of any list with dates.
- **The harness fills in what the model fumbles.** Schedules ("каждые 45 минут", "every weekday at
  9am", "в 18:30") and delays ("через 20 минут") are parsed from the user's words; routine names are
  transliterated; "убери рутину про воду" finds `pit-voda` by word overlap.
- **Honest trace.** Every tool call is printed, plus ✓/✗ for side effects and errors, because the
  model sometimes says "done" after a failed call.
- **Safety.** Read-only one-liners (`ls`, `du`, `pmset -g`, …) run without asking. Everything else asks
  `[y/N/a]`. Routines are LaunchAgents (they persist and EDR sees them), so adding or removing one
  asks every time: neither `-y` nor `a` covers it. Without a terminal the answer is "no".
  `PIPPIN_DRY_ROUTINES=1` writes plists to `~/.pippin/routines-dry-run/` without loading them.

## Files

| Path | What |
|---|---|
| `pippin.py` | the harness |
| `prompts/system.md` | base prompt; override with `~/.pippin/system.md` |
| `skills/<name>/SKILL.md` | bundled skills; add your own in `~/.pippin/skills/` |
| `mcp/clipboard_server.py` | example MCP server (clipboard get/set), stdlib only |
| `config.example.json` | config template; the live one is `~/.pippin/config.json` |
| `~/.pippin/` | config, scripts, logs, routine logs, chat history, `memory.md` (facts about you, added to the prompt) |

## Skills

```markdown
---
name: routine
description: repeat a job on a schedule
triggers: кажд(ый|ое|ые), ежедневн, every (day|weekday|\d), schedule      # regexes, case-insensitive
tools: routine            # gated tools this skill unlocks
shadows: remind           # skills to switch off when this one fires
---
Steps for the model, short and imperative.
```

Bundled: `files` (find, sizes, summaries), `mac` (dark mode, volume, battery, Wi-Fi, apps, say),
`script` (small zsh/python scripts, run once to check), `remind` (one-off notification),
`routine` (LaunchAgent on a schedule).

## MCP

Stdio servers, same shape as Claude Desktop's `mcpServers`, plus optional keys:
`tools` (allowlist; each tool costs ~75 tokens), `triggers` (regexes: offer this server's tools only when the
request matches), `auto_approve` (tool names that never ask) and `disabled`. A call asks `[y/N/a]` unless
the tool declares `readOnlyHint` or is in `auto_approve`. Without `triggers`, the model reached for
`clipboard_set` to "translate" piped text and overwrote the clipboard.

```json
{"mcpServers": {"clipboard": {"command": "python3", "args": ["$PIPPIN_ROOT/mcp/clipboard_server.py"],
                              "triggers": ["буфер", "clipboard", "copy", "paste"]},
                "time": {"command": "uvx", "args": ["mcp-server-time"], "tools": ["get_current_time"]}}}
```

Tools show up as `<server>_<tool>`. Remote (HTTP) MCP servers are not supported yet.

## What the model is like (tested 2026-11-20)

- Fast: one tool step 0.5–2 s, a whole task 4–8 s.
- Single-step tasks work: list a folder sorted by size, read and summarize a file, battery,
  clipboard, reminders, routines, translate piped text.
- Russian works but reads like a translation ("пойте воду" for "drink water"). English is better.
  A Cyrillic message adds "answer in Russian" to the prompt, otherwise it often replies in English.
- Multi-step work is weak: a "write a script and run it" task wrote a script with broken awk quoting
  and then went in circles. Compound asks ("translate the clipboard and put it back") lose a step.
- Sometimes it ignores format ("3 bullets") and claims success after a failed call (see the trace).
- apfel does not apply `max_tokens` to the guided tool step. A runaway tool call (nested quotes)
  can spin until the context fills (150 s). pippin gives up after 60 s.
- Apple's guardrails trip on harmless text ("встань и разомнись") unless the server runs with `--permissive`.

## Ideas

- `remember` tool writing to `~/.pippin/memory.md`.
- Image input (the model has vision): "what's in this screenshot".
- Shortcuts.app bridge: `shortcuts run` as a tool, so any Shortcut becomes a skill.
- A menu-bar or Raycast front end on top of the same loop.
