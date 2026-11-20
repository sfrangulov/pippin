#!/usr/bin/env python3
"""pippin: a tiny agent harness for Apple's on-device model, served by apfel.

Everyday Mac chores, not coding: read a file, write a short script, run a
command, set up a routine, show a reminder. Built-in tools, skills and MCP
servers, all squeezed into the model's 8,192-token window.

Standard library only. Run `pippin --help`.
"""
from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import plistlib
import queue
import re
import shutil
import signal
import select
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("PIPPIN_HOME", "~/.pippin")).expanduser()
MODEL = "apple-foundationmodel"
MAX_OUT = 1024            # tokens reserved for one reply
TOOL_CHARS = 2400         # cap on one tool result (~800 tokens)
OLD_TOOL_CHARS = 300      # what an older tool result shrinks to
HISTORY_CHARS = 9000      # soft cap on history before old turns are dropped
MAX_STEPS = 8             # tool rounds per user turn
GATED = {"notify", "routine"}   # offered only while a skill that lists them is active
REQUEST_TIMEOUT = 60      # a runaway guided tool step can otherwise take minutes
LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()
ROUTINE_PREFIX = "local.pippin."

# ---------------------------------------------------------------- terminal

TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


dim = lambda s: _c("2", s)          # noqa: E731
yellow = lambda s: _c("33", s)      # noqa: E731
red = lambda s: _c("31", s)         # noqa: E731
bold = lambda s: _c("1", s)         # noqa: E731


class Spinner:
    """A dim 'thinking' marker while the model works."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def __enter__(self):
        if TTY:
            self._t = threading.Thread(target=self._spin, daemon=True)
            self._t.start()
        return self

    def _spin(self) -> None:
        frames, i = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 0
        while not self._stop.wait(0.1):
            sys.stdout.write("\r" + dim(frames[i % len(frames)] + " thinking"))
            sys.stdout.flush()
            i += 1

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


def ask_user(question: str) -> str:
    """Read an answer from the terminal even when stdin is a pipe."""
    try:
        if sys.stdin.isatty():
            return input(question)
        with open("/dev/tty", "r+") as tty:
            tty.write(question)
            tty.flush()
            return tty.readline()
    except (OSError, EOFError):
        return ""


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... cut {len(text) - limit} chars]"


def expand_str(text: str) -> str:
    """~ and $VARS, plus $PIPPIN_ROOT = this repo (for bundled MCP servers)."""
    return os.path.expanduser(os.path.expandvars(text.replace("$PIPPIN_ROOT", str(ROOT))))


def expand(path: str) -> Path:
    return Path(expand_str(path or "."))


# ------------------------------------------------------------------ config


def load_config() -> dict:
    cfg: dict = {}
    for f in (ROOT / "config.json", HOME / "config.json"):
        if f.exists():
            try:
                cfg.update(json.loads(f.read_text()))
            except json.JSONDecodeError as e:
                print(red(f"bad JSON in {f}: {e}"), file=sys.stderr)
    return cfg


# ------------------------------------------------------------- apfel server


class Apfel:
    """Talks to `apfel --serve`; starts a private one when none is running."""

    def __init__(self, url: str | None, debug: bool = False, permissive: bool = True) -> None:
        self.debug, self.permissive = debug, permissive
        self.proc: subprocess.Popen | None = None
        candidates = [u for u in (url, os.environ.get("APFEL_URL"), "http://127.0.0.1:11434") if u]
        for u in candidates:
            if self._alive(u):
                self.url = u.rstrip("/")
                return
        self.url = self._spawn()

    @staticmethod
    def _alive(url: str) -> bool:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/v1/models", timeout=1.5) as r:
                return any(m.get("id") == MODEL for m in json.load(r).get("data", []))
        except Exception:
            return False

    def _spawn(self) -> str:
        apfel = shutil.which("apfel")
        if not apfel:
            sys.exit("apfel not found: brew install apfel")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        HOME.mkdir(parents=True, exist_ok=True)
        log = open(HOME / "apfel-server.log", "ab")
        self.proc = subprocess.Popen(
            [apfel, "--serve", "--port", str(port), *(["--permissive"] if self.permissive else [])],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        atexit.register(self.close)
        url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            if self.proc.poll() is not None:
                sys.exit(f"apfel server exited; see {HOME / 'apfel-server.log'}")
            if self._alive(url):
                return url
            time.sleep(0.25)
        sys.exit("apfel server did not come up in 15 s")

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def chat(self, messages: list, tools: list | None = None, tool_choice: str | None = None,
             **extra) -> dict:
        body: dict = {"model": MODEL, "messages": messages, "max_tokens": MAX_OUT, **extra}
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        req = urllib.request.Request(
            self.url + "/v1/chat/completions",
            json.dumps(body).encode(), {"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.load(r)
        except TimeoutError:
            raise RuntimeError(f"the model got stuck (no reply in {REQUEST_TIMEOUT} s); try rephrasing") from None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except Exception:
                pass
            raise RuntimeError(f"apfel {e.code}: {detail}") from None


# ------------------------------------------------------------------ skills


class Skill:
    def __init__(self, path: Path) -> None:
        text = path.read_text()
        meta, body = {}, text
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        if m:
            body = m.group(2)
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
        self.name = meta.get("name", path.parent.name)
        self.description = meta.get("description", "")
        listed = lambda key: [t.strip() for t in meta.get(key, "").strip("[]").split(",") if t.strip()]  # noqa: E731
        self.triggers = [re.compile(t, re.I) for t in listed("triggers")]   # regexes
        self.tools = listed("tools")          # gated tools this skill switches on
        self.shadows = listed("shadows")      # skills to switch off when this one fires
        self.body = body.strip()

    def matches(self, text: str) -> bool:
        return any(t.search(text) for t in self.triggers)


def load_skills() -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for base in (ROOT / "skills", HOME / "skills"):   # user skills override bundled ones
        for f in sorted(base.glob("*/SKILL.md")):
            s = Skill(f)
            skills[s.name] = s
    return skills


# --------------------------------------------------------------------- MCP


class MCPServer:
    """Minimal MCP client over stdio (newline-delimited JSON-RPC)."""

    def __init__(self, name: str, spec: dict) -> None:
        self.name = name
        self.allow = spec.get("tools")
        self.auto_approve = set(spec.get("auto_approve", []))      # tool names that never ask
        self.triggers = [re.compile(t, re.I) for t in spec.get("triggers", [])]  # offer tools only on these
        cmd = [expand_str(x) for x in (spec["command"], *spec.get("args", []))]
        env = {**os.environ, **spec.get("env", {})}
        HOME.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(HOME / f"mcp-{name}.log", "ab"), text=True, bufsize=1, env=env,
        )
        atexit.register(self.close)
        self._id = 0
        self._inbox: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "pippin", "version": "0.1"},
        }, timeout=float(spec.get("startup_timeout", 30)))
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = self.rpc("tools/list", {}).get("tools", [])
        self.tools = [t for t in tools if not self.allow or t["name"] in self.allow]

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in msg and "id" in msg:         # server -> client request
                reply = {} if msg["method"] == "ping" else None
                out = {"jsonrpc": "2.0", "id": msg["id"]}
                out.update({"result": reply} if reply is not None
                           else {"error": {"code": -32601, "message": "not supported"}})
                self._send(out)
            elif "id" in msg:
                self._inbox.put(msg)
        self._inbox.put(None)

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method: str, params: dict, timeout: float = 60) -> dict:
        self._id += 1
        my_id = self._id
        self._send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            try:
                msg = self._inbox.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                raise RuntimeError(f"MCP {self.name}: {method} timed out") from None
            if msg is None:
                raise RuntimeError(f"MCP {self.name}: server exited")
            if msg.get("id") != my_id:
                continue
            if "error" in msg:
                raise RuntimeError(f"MCP {self.name}: {msg['error'].get('message')}")
            return msg.get("result", {})

    def call(self, tool: str, args: dict) -> str:
        res = self.rpc("tools/call", {"name": tool, "arguments": args})
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c["text"])
            else:
                parts.append(f"[{c.get('type')} content]")
        if not parts and "structuredContent" in res:
            parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
        text = "\n".join(parts) or "(empty result)"
        return ("ERROR: " + text) if res.get("isError") else text

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()


def slim_schema(schema):
    """Drop schema noise that costs tokens and teaches the model nothing."""
    drop = {"$schema", "title", "additionalProperties", "examples", "$id"}
    if isinstance(schema, dict):
        out = {}
        for k, v in schema.items():
            if k in drop:
                continue
            if k == "description" and isinstance(v, str):
                v = clip(v, 120).split("\n[...")[0]
            out[k] = slim_schema(v) if isinstance(v, (dict, list)) else v
        return out
    if isinstance(schema, list):
        return [slim_schema(x) for x in schema]
    return schema


# ------------------------------------------------------------- tool helpers


def fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


S = lambda d: {"type": "string", "description": d}   # noqa: E731
I = lambda d: {"type": "integer", "description": d}  # noqa: E731

SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "date", "cal", "pwd", "whoami", "uname", "sw_vers",
    "df", "du", "which", "file", "stat", "uptime", "echo", "grep", "mdfind", "pmset",
    "pbpaste", "sort", "uniq", "basename", "dirname", "hostname", "system_profiler",
    "networksetup", "defaults", "osascript", "open", "say", "sysctl", "vm_stat", "top",
}
UNSAFE_FLAGS = re.compile(r"(-delete|-exec|\bwrite\b|\bdelete\b|\bset\b|-setairport|kill)")


def is_safe(command: str) -> bool:
    """Read-only one-liners run without asking; everything else needs a yes."""
    if re.search(r"[;&|<>`]|\$\(", command):
        return False
    first = command.strip().split()[0] if command.strip() else ""
    if first not in SAFE_COMMANDS:
        return False
    if first in {"defaults", "osascript", "open", "say", "pmset", "networksetup"}:
        # side effects are possible; only obvious reads pass
        reads = {"defaults": " read", "pmset": " -g", "networksetup": " -get"}
        return first in reads and reads[first] in command and not UNSAFE_FLAGS.search(command)
    return not UNSAFE_FLAGS.search(command)


# ------------------------------------------------------------------- agent


class Agent:
    def __init__(self, cfg: dict, apfel: Apfel, yes: bool = False, debug: bool = False) -> None:
        self.cfg, self.apfel, self.yes, self.debug = cfg, apfel, yes, debug
        self.scripts_dir = expand(cfg.get("scripts_dir", str(HOME / "scripts")))
        self.skills = load_skills()
        self.active: list[str] = []        # skills whose text is in the prompt, newest last
        self.fired: dict[str, int] = {}    # skill -> turn it last fired
        self.turns = 0
        self.history: list[dict] = []
        self.request = ""                  # the user's latest message
        self.last_usage: dict = {}
        self.mcp: dict[str, MCPServer] = {}
        self.mcp_routes: dict[str, tuple[MCPServer, str]] = {}
        for name, spec in cfg.get("mcpServers", {}).items():
            if spec.get("disabled"):
                continue
            try:
                srv = MCPServer(name, spec)
                self.mcp[name] = srv
                for t in srv.tools:
                    exposed = re.sub(r"[^A-Za-z0-9_-]", "_", f"{name}_{t['name']}")[:64]
                    self.mcp_routes[exposed] = (srv, t["name"])
            except Exception as e:
                print(red(f"MCP '{name}' failed: {e}"), file=sys.stderr)
        self.tools = self._tool_specs()

    # -- prompt -----------------------------------------------------------

    def _tool_specs(self) -> list[dict]:
        """Every tool pippin knows. Gated ones are sent only while a skill unlocks them."""
        specs = [
            fn("read_file", "Read a text file. Long files come in parts: pass offset (a line number) to read on.",
               {"path": S("file path"), "offset": I("first line to read, default 1")}, ["path"]),
            fn("list_dir", "List what is in a folder with sizes and dates.",
               {"path": S("folder path"),
                "sort": {"type": "string", "enum": ["name", "size", "date"], "description": "size = biggest first, date = newest first"}},
               ["path"]),
            fn("write_file", "Create or overwrite a text file (the user confirms).",
               {"path": S("file path"), "content": S("full file text")}, ["path", "content"]),
            fn("run", "Run a zsh command on this Mac and get its output.",
               {"command": S("one shell command line")}, ["command"]),
            fn("notify", "Show a macOS notification now, or after some minutes (a one-off reminder).",
               {"text": S("message"), "in_minutes": I("delay in minutes, 0 = now")}, ["text"]),
            fn("routine", "Manage repeating jobs: add, list or remove.",
               {"action": {"type": "string", "enum": ["add", "list", "remove"]},
                "name": S("short job name, e.g. tidy-downloads"),
                "text": S("notification to show each time (use this OR command)"),
                "command": S("shell command to run each time"),
                "at": S("daily time HH:MM (needed unless every_minutes)"),
                "every_minutes": I("run every N minutes instead of daily")}, ["action"]),
        ]
        if self.skills and self.cfg.get("skill_tool"):
            specs.append(fn("skill", "Load step-by-step instructions for a kind of task.",
                            {"name": {"type": "string", "enum": sorted(self.skills)}}, ["name"]))
        for exposed, (srv, tname) in self.mcp_routes.items():
            t = next(x for x in srv.tools if x["name"] == tname)
            desc = clip((t.get("description") or tname).strip().split("\n")[0], 160).split("\n[...")[0]
            specs.append({"type": "function", "function": {
                "name": exposed, "description": desc,
                "parameters": slim_schema(t.get("inputSchema") or {"type": "object", "properties": {}}),
            }})
        return specs

    def offered_tools(self) -> list[dict]:
        unlocked = {t for name in self.active for t in self.skills[name].tools}
        ask = self.request.split("</input>")[-1]
        offered = []
        for t in self.tools:
            name = t["function"]["name"]
            if name in GATED and name not in unlocked:
                continue
            srv = self.mcp_routes.get(name, (None, ""))[0]
            if srv and srv.triggers and not any(rx.search(ask) for rx in srv.triggers):
                continue
            offered.append(t)
        return offered

    def system_prompt(self) -> str:
        user_prompt = HOME / "system.md"
        template = (user_prompt if user_prompt.exists() else ROOT / "prompts" / "system.md").read_text()
        skills = "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values()) or "(none)"
        prompt = template.format(
            date=dt.datetime.now().strftime("%A, %d %B %Y, %H:%M"),
            cwd=os.getcwd(), home=str(Path.home()), scripts_dir=str(self.scripts_dir), skills=skills,
        )
        memory = HOME / "memory.md"
        if memory.exists() and memory.read_text().strip():
            prompt += "\n\nAbout the user:\n" + clip(memory.read_text().strip(), 1200)
        for name in self.active:
            prompt += f"\n\n# Skill: {name}\n{self.skills[name].body}"
        last = next((m["content"] for m in reversed(self.history) if m["role"] == "user"), "")
        if re.search(r"[А-Яа-яЁё]", last.split("<input>")[0]):
            prompt += "\n\nThe user writes in Russian. Write your final answer in Russian."
        return prompt

    def _route(self, text: str) -> list[Skill]:
        """No trigger fired: let the model pick instruction-only skills (guided JSON, ~0.4 s)."""
        pool = {n: s for n, s in self.skills.items() if not s.tools}
        if not pool or not self.cfg.get("router", True):
            return []
        listing = "\n".join(f"- {n}: {s.description}" for n, s in pool.items())
        schema = {"type": "object", "required": ["skills"], "properties": {
            "skills": {"type": "array", "items": {"type": "string", "enum": [*pool, "none"]}}}}
        try:
            r = self.apfel.chat([
                {"role": "system", "content": "You route requests for a Mac helper. Pick the skills the "
                 "request clearly needs, or none. Most requests need none.\nSkills:\n" + listing},
                {"role": "user", "content": text}],
                response_format={"type": "json_schema", "json_schema": {"name": "route", "schema": schema}},
                max_tokens=60)
            picked = json.loads(r["choices"][0]["message"]["content"]).get("skills", [])
        except Exception:
            return []
        return [pool[n] for n in picked if n in pool]

    def _activate_skills(self, text: str) -> None:
        ask = text.split("<input>")[0] if "<input>" not in text else text.split("</input>")[-1]
        self.turns += 1
        hits = [s for s in self.skills.values() if s.matches(ask)]
        if not hits and len(ask) < 600:
            hits = self._route(ask)
        shadowed = {x for s in hits for x in s.shadows}
        for s in hits:
            if s.name in shadowed:
                continue
            if s.name not in self.active:
                self.active.append(s.name)
                print(dim(f"  ✦ skill {s.name}"))
            self.fired[s.name] = self.turns
        # a skill stays for the turn it fired and one follow-up, then leaves the prompt
        self.active = [n for n in self.active if n not in shadowed
                       and self.turns - self.fired.get(n, self.turns) <= 1][-3:]

    # -- history ------------------------------------------------------------

    def _trim(self) -> None:
        """Shrink old tool results, then drop whole old turns, to stay small."""
        last_user = max((i for i, m in enumerate(self.history) if m["role"] == "user"), default=0)
        for m in self.history[:last_user]:
            if m["role"] == "tool" and len(m["content"]) > OLD_TOOL_CHARS:
                m["content"] = clip(m["content"], OLD_TOOL_CHARS)
        size = lambda: sum(len(json.dumps(m, ensure_ascii=False)) for m in self.history)  # noqa: E731
        while size() > HISTORY_CHARS:
            starts = [i for i, m in enumerate(self.history) if m["role"] == "user"]
            if len(starts) < 2:
                break
            self.history = self.history[starts[1]:]

    # -- loop ---------------------------------------------------------------

    def turn(self, text: str) -> str:
        self._activate_skills(text)
        self.history.append({"role": "user", "content": text})
        self.request = text
        seen: dict[str, str] = {}
        for step in range(MAX_STEPS):
            self._trim()
            messages = [{"role": "system", "content": self.system_prompt()}] + self.history
            force_answer = step == MAX_STEPS - 1
            try:
                with Spinner():
                    resp = self.apfel.chat(messages, self.offered_tools(), "none" if force_answer else None)
            except RuntimeError as e:
                self.history.pop() if self.history and self.history[-1]["role"] == "user" else None
                return red(str(e))
            self.last_usage = resp.get("usage", {})
            if self.debug:
                print(dim(f"  [step {step + 1}: prompt {self.last_usage.get('prompt_tokens')} tok, "
                          f"reply {self.last_usage.get('completion_tokens')} tok]"))
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            entry = {"role": "assistant", "content": msg.get("content")}
            if calls:
                entry["tool_calls"] = calls
            self.history.append(entry)
            if not calls:
                return (msg.get("content") or "").strip()
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                key = name + json.dumps(args, sort_keys=True)
                shown = ", ".join(f"{k}={clip(str(v), 60)}" for k, v in args.items())
                print(dim(f"  → {name}({shown})"))
                if key in seen:
                    result = (f"You made this exact call already and nothing changed. Its result was:\n"
                              f"{clip(seen[key], 600)}\nDo not repeat it. Change the arguments, or tell "
                              f"the user honestly what happened.")
                else:
                    try:
                        result = self.dispatch(name, args)
                    except Exception as e:  # tool errors go back to the model
                        result = f"ERROR: {e}"
                    seen[key] = str(result)
                first = str(result).strip().split("\n")[0]
                if first.startswith("ERROR") or "declined" in first:
                    print(red(f"    ✗ {clip(first, 100)}"))
                elif name in GATED or name == "write_file":
                    print(dim(f"    ✓ {clip(first, 100)}"))
                self.history.append({"role": "tool", "tool_call_id": call.get("id", name),
                                     "content": clip(str(result), TOOL_CHARS)})
        return dim("(stopped: too many steps)")

    # -- tools --------------------------------------------------------------

    def confirm(self, what: str, always_ask: bool = False) -> bool:
        """y/N in the terminal. always_ask: neither -y nor an earlier 'a' covers it."""
        if self.yes and not always_ask:
            return True
        choices = "[y/N]" if always_ask else "[y/N/a=always]"
        answer = ask_user(f"  {yellow('?')} {what}\n    allow? {choices} ").strip().lower()
        if answer in ("a", "always") and not always_ask:
            self.yes = True
            return True
        return answer in ("y", "yes", "д", "да")

    def dispatch(self, name: str, a: dict) -> str:
        if name in self.mcp_routes:
            srv, tool = self.mcp_routes[name]
            if name not in {t["function"]["name"] for t in self.offered_tools()}:
                return f"ERROR: {name} is not available right now"
            spec = next(x for x in srv.tools if x["name"] == tool)
            read_only = (spec.get("annotations") or {}).get("readOnlyHint") or tool in srv.auto_approve
            if not read_only:
                shown = ", ".join(f"{k}={clip(str(v), 80)}" for k, v in a.items())
                if not self.confirm(f"MCP {srv.name}.{tool}({shown})"):
                    return "The user declined. Do not retry; ask what to do instead."
            return srv.call(tool, a)
        handler = getattr(self, "t_" + name, None)
        if not handler:
            return f"ERROR: no tool named {name}"
        if name in GATED and name not in {t["function"]["name"] for t in self.offered_tools()}:
            return f"ERROR: {name} is not available right now"
        return handler(**{k: v for k, v in a.items() if v is not None})

    def t_read_file(self, path: str, offset: int = 1, **_) -> str:
        p = expand(path)
        if p.is_dir():
            return self.t_list_dir(path)
        if p.stat().st_size > 50_000_000:
            return "ERROR: file is too big to read here"
        raw = p.read_bytes()
        if b"\0" in raw[:4096]:
            return f"{p} is a binary file ({len(raw)} bytes). Try `file` or `mdls` via run."
        lines = raw.decode(errors="replace").splitlines()
        start = max(1, int(offset or 1))
        out, used = [], 0
        for i in range(start - 1, len(lines)):
            chunk = lines[i] + "\n"
            if used + len(chunk) > TOOL_CHARS - 200:
                out.append(f"[lines {start}-{i} of {len(lines)}; call read_file with offset={i + 1} for more]")
                break
            out.append(chunk.rstrip("\n"))
            used += len(chunk)
        return "\n".join(out) or "(empty file)"

    def t_list_dir(self, path: str = ".", sort: str = "name", **_) -> str:
        p = expand(path)
        items = [x for x in p.iterdir() if not x.name.startswith(".")]
        st = {x: x.stat() for x in items}
        if sort == "size":
            items.sort(key=lambda x: -st[x].st_size)
        elif sort == "date":
            items.sort(key=lambda x: -st[x].st_mtime)
        else:
            items.sort(key=lambda x: (not x.is_dir(), x.name.lower()))
        rows = []
        for x in items[:120]:
            when = dt.datetime.fromtimestamp(st[x].st_mtime).strftime("%Y-%m-%d")
            if x.is_dir():
                rows.append(f"{x.name}/  folder  {when}")
            else:
                rows.append(f"{x.name}  {human_size(st[x].st_size)}  {when}")
        order = {"size": ", biggest first", "date": ", newest first"}.get(sort, "")
        more = f"\n[+{len(items) - 120} more]" if len(items) > 120 else ""
        return f"{p}: {len(items)} items{order}\n" + "\n".join(rows) + more if rows else f"{p} is empty"

    def t_write_file(self, path: str, content: str, **_) -> str:
        p = expand(path)
        verb = "overwrite" if p.exists() else "create"
        preview = "\n".join("    │ " + line for line in content.splitlines()[:25])
        more = f"\n    │ … {len(content.splitlines()) - 25} more lines" if len(content.splitlines()) > 25 else ""
        if not self.confirm(f"{verb} {p}\n{dim(preview + more)}"):
            return "The user declined. Do not retry; ask what to change."
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if content.endswith("\n") else content + "\n")
        if content.startswith("#!"):
            p.chmod(0o755)
        return f"Saved {p} ({len(content)} chars)" + (", made executable" if content.startswith("#!") else "")

    def t_run(self, command: str, **_) -> str:
        if not is_safe(command) and not self.confirm(f"run: {bold(command)}"):
            return "The user declined to run this. Do not retry; ask what to do instead."
        try:
            r = subprocess.run(["/bin/zsh", "-c", command], capture_output=True, text=True,
                               timeout=120, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return "ERROR: timed out after 120 s"
        out = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
        return f"exit {r.returncode}\n{out or '(no output)'}"

    def t_notify(self, text: str, in_minutes: int = 0, **_) -> str:
        if getattr(self, "_notified", None) == self.request:
            return "A notification for this request is already set. Do not add another; answer the user."
        self._notified = self.request
        script = f'display notification {json.dumps(text, ensure_ascii=False)} with title "Pippin" sound name "Glass"'
        minutes = int(in_minutes or 0) or infer_delay(self.request)
        if minutes <= 0:
            subprocess.run(["osascript", "-e", script], capture_output=True)
            return "Notification shown."
        subprocess.Popen(
            ["/bin/zsh", "-c", f"sleep {minutes * 60}; osascript -e {sh_quote(script)}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        at = (dt.datetime.now() + dt.timedelta(minutes=minutes)).strftime("%H:%M")
        return f"Reminder set for {at} ({minutes} min). It lives until the Mac sleeps or restarts."

    def t_routine(self, action: str, name: str = "", command: str = "", at: str = "",
                  every_minutes: int = 0, text: str = "", **_) -> str:
        dry = bool(os.environ.get("PIPPIN_DRY_ROUTINES"))
        folder = HOME / "routines-dry-run" if dry else LAUNCH_AGENTS
        if action == "list":
            rows = []
            for f in sorted(folder.glob(ROUTINE_PREFIX + "*.plist")):
                pl = plistlib.loads(f.read_bytes())
                cal = pl.get("StartCalendarInterval")
                if isinstance(cal, list):
                    when = f"{', '.join(DAY_NAMES[c['Weekday']] for c in cal)} {cal[0]['Hour']:02d}:{cal[0]['Minute']:02d}"
                elif cal:
                    when = f"daily {cal['Hour']:02d}:{cal['Minute']:02d}"
                else:
                    when = f"every {pl.get('StartInterval', 0) // 60} min"
                cmd = pl["ProgramArguments"][-1]
                note = re.search(r'display notification "(.*?)" with title', cmd)
                rows.append(f"{f.stem.removeprefix(ROUTINE_PREFIX)}: {when}: "
                            + (f"notification '{note.group(1)}'" if note else cmd))
            return "\n".join(rows) or "No routines yet."
        slug = slugify(name or text or command)
        if not slug:
            return "ERROR: give the routine a short name in Latin letters"
        label = ROUTINE_PREFIX + slug
        plist = folder / f"{label}.plist"
        domain = f"gui/{os.getuid()}"
        if action == "remove":
            if not plist.exists():
                guess = self._guess_routine(folder)
                if guess:
                    slug, plist = guess.stem.removeprefix(ROUTINE_PREFIX), guess
            if not plist.exists():
                names = [f.stem.removeprefix(ROUTINE_PREFIX) for f in folder.glob(ROUTINE_PREFIX + "*.plist")]
                return (f"ERROR: no routine named {slug}. Existing: {', '.join(names)}. Call remove with one of these."
                        if names else "There are no routines to remove.")
            if not self.confirm(f"remove routine {slug} (LaunchAgent {plist.name})", always_ask=not dry):
                return "The user declined."
            if not dry:
                subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
            plist.unlink()
            return f"Removed routine {slug}."
        if action != "add":
            return "ERROR: action must be add, list or remove"
        if text and not command:
            command = "osascript -e " + sh_quote(f'display notification {json.dumps(text, ensure_ascii=False)} with title "Pippin" sound name "Glass"')
        if not command:
            return "ERROR: add needs text or a command"
        job: dict = {
            "Label": label,
            "ProgramArguments": ["/bin/zsh", "-c", command],
            "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(os.environ.get("PATH", "/usr/bin:/bin").split(":")))},
            "StandardOutPath": str(HOME / "logs" / f"{slug}.log"),
            "StandardErrorPath": str(HOME / "logs" / f"{slug}.log"),
        }
        if not every_minutes and not at:
            at, every_minutes = infer_schedule(self.request)
        if every_minutes:
            job["StartInterval"] = int(every_minutes) * 60
            when = f"every {int(every_minutes)} min"
        else:
            m = re.match(r"^(\d{1,2})[:.](\d{2})$", at.strip())
            if not m:
                return "ERROR: no schedule. Pass at='HH:MM' for daily, or every_minutes=N."
            hour, minute = int(m.group(1)), int(m.group(2))
            days = infer_days(self.request)
            if days:
                job["StartCalendarInterval"] = [{"Weekday": d, "Hour": hour, "Minute": minute} for d in days]
                when = f"{', '.join(DAY_NAMES[d] for d in days)} at {hour:02d}:{minute:02d}"
            else:
                job["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
                when = f"daily at {hour:02d}:{minute:02d}"
        if not self.confirm(f"add routine {bold(slug)} ({when}): {command}\n"
                            f"    {dim('installs a LaunchAgent that runs until removed; corporate EDR may log it')}",
                            always_ask=not dry):
            return "The user declined."
        folder.mkdir(parents=True, exist_ok=True)
        (HOME / "logs").mkdir(parents=True, exist_ok=True)
        if plist.exists() and not dry:
            subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
        plist.write_bytes(plistlib.dumps(job))
        if not dry:
            r = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
            if r.returncode:
                return f"ERROR: launchctl: {r.stderr.strip()}"
        return f"Routine {slug} added: {when}. Log: {job['StandardOutPath']}" + (" (dry run)" if dry else "")

    def _guess_routine(self, folder: Path) -> Path | None:
        """The one routine whose name or command shares a word with the user's request."""
        words = {w[:5] for w in re.findall(r"\w{4,}", self.request.lower())} - {"рутин", "routi", "удали", "убери", "remov", "delet"}
        scored = []
        for f in folder.glob(ROUTINE_PREFIX + "*.plist"):
            hay = (f.stem + " " + plistlib.loads(f.read_bytes())["ProgramArguments"][-1]).lower()
            hay_words = {w[:5] for w in re.findall(r"\w{4,}", hay)} | {w[:5] for w in hay.replace("-", " ").split()}
            scored.append((len(words & hay_words), f))
        best = [f for n, f in scored if n and n == max(x for x, _ in scored)]
        return best[0] if len(best) == 1 else None

    def t_skill(self, name: str, **_) -> str:
        if name not in self.skills:
            return f"ERROR: no skill {name}; have: {', '.join(self.skills)}"
        if name in self.active:
            return f"Skill {name} is already in your instructions above. Follow it now."
        self.active = (self.active + [name])[-2:]
        return f"Loaded skill {name}: its steps are now at the end of your instructions. Follow them."


_RU = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_LAT = "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch - y - e yu ya".split()
TRANSLIT = {ch: _LAT[i] for i, ch in enumerate(_RU)}


def slugify(name: str) -> str:
    name = "".join(TRANSLIT.get(ch, ch) for ch in name.lower())
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:40].strip("-")


def infer_schedule(text: str) -> tuple[str, int]:
    """Pull a schedule out of the user's words: ('HH:MM', 0) or ('', minutes)."""
    t = text.lower()
    m = re.search(r"(?:кажд\w*|every)\s+(\d+)\s*(мин|min|час|h)", t)
    if m:
        n = int(m.group(1))
        return "", n * 60 if m.group(2) in ("час", "h") else n
    if re.search(r"(?:кажд\w*|every)\s+(?:полчаса|half an hour)", t):
        return "", 30
    if re.search(r"(?:кажд\w*|every)\s+(?:час|hour)", t):
        return "", 60
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if m and int(m.group(1)) < 24:
        return f"{int(m.group(1)):02d}:{m.group(2)}", 0
    m = re.search(r"\b(\d{1,2})\s*(утра|вечера|дня|am|pm)\b", t)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(2) in ("вечера", "дня", "pm") else 0)
        return f"{h:02d}:00", 0
    return "", 0


DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
DAY_WORDS = [(1, r"понедельн|monday"), (2, r"вторник|tuesday"), (3, r"сред[аеу]|wednesday"),
             (4, r"четверг|thursday"), (5, r"пятниц|friday"), (6, r"суббот|saturday"),
             (7, r"воскресен|sunday")]


def infer_days(text: str) -> list[int]:
    """Weekdays for launchd (1 = Monday, 7 = Sunday); [] means every day."""
    t = text.lower()
    if re.search(r"будн|weekday|рабоч\w* дн", t):
        return [1, 2, 3, 4, 5]
    if re.search(r"выходн|weekend", t):
        return [6, 7]
    return [d for d, rx in DAY_WORDS if re.search(rx, t)]


def infer_delay(text: str) -> int:
    """Minutes in 'через 20 минут' / 'in 2 hours'; 0 if none."""
    m = re.search(r"(?:через|in)\s+(\d+)\s*(мин|min|час|h)", text.lower())
    if not m:
        return 1 if re.search(r"через минуту|in a minute", text.lower()) else 0
    return int(m.group(1)) * (60 if m.group(2) in ("час", "h") else 1)


def human_size(n: int) -> str:
    for k, unit in ((3, "GB"), (2, "MB"), (1, "KB")):
        if n >= 1024 ** k:
            return f"{n / 1024 ** k:.1f} {unit}"
    return f"{n} B"


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------- CLI

HELP = """commands: /reset  /ctx  /tools  /skills  /yes  /exit"""


def show_ctx(agent: Agent) -> None:
    u = agent.last_usage
    est_tools = len(json.dumps(agent.offered_tools(), ensure_ascii=False)) // 3
    est_sys = len(agent.system_prompt()) // 3
    print(dim(f"last request: {u.get('prompt_tokens', '?')} prompt + {u.get('completion_tokens', '?')} reply "
              f"of 8192 tokens | estimate: system ~{est_sys}, tools ~{est_tools}, "
              f"history {len(agent.history)} messages | skills on: {', '.join(agent.active) or 'none'}"))


def repl(agent: Agent) -> None:
    try:
        import readline  # noqa: F401  (line editing + history)
        hist = HOME / "history"
        HOME.mkdir(parents=True, exist_ok=True)
        if hist.exists():
            readline.read_history_file(hist)
        atexit.register(readline.write_history_file, hist)
    except Exception:
        pass
    mcp = f", MCP: {', '.join(agent.mcp)}" if agent.mcp else ""
    print(dim(f"pippin · Apple on-device model · {len(agent.offered_tools())} tools, "
              f"{len(agent.skills)} skills{mcp} · {HELP}"))
    while True:
        try:
            line = input(bold("› ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("/exit", "/quit", "exit"):
            return
        if line == "/reset":
            agent.history.clear()
            agent.active.clear()
            agent.fired.clear()
            print(dim("fresh start"))
            continue
        if line == "/ctx":
            show_ctx(agent)
            continue
        if line == "/tools":
            print("\n".join(f"  {t['function']['name']}: {t['function']['description']}" for t in agent.offered_tools()))
            continue
        if line == "/skills":
            print("\n".join(f"  {s.name}: {s.description}" for s in agent.skills.values()) or "  none")
            continue
        if line == "/yes":
            agent.yes = not agent.yes
            print(dim(f"auto-approve {'on' if agent.yes else 'off'}"))
            continue
        try:
            print(agent.turn(line))
        except KeyboardInterrupt:
            print(dim("\n(interrupted)"))


def read_piped_stdin() -> str:
    """Text piped in (`cat notes.txt | pippin ...`); never block on an idle stdin."""
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError):
        return ""
    if stat.S_ISREG(mode):
        return sys.stdin.read()
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        ready, _, _ = select.select([sys.stdin], [], [], 1.0)
        return sys.stdin.read() if ready else ""
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="pippin", description="Tiny agent harness for Apple's on-device model (via apfel).",
        epilog="examples:\n  pippin\n  pippin 'what is eating my disk space?'\n"
               "  pbpaste | pippin 'turn this into a todo list'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("prompt", nargs="*", help="one-shot request; omit for a chat")
    ap.add_argument("-y", "--yes", action="store_true", help="approve every action without asking")
    ap.add_argument("--debug", action="store_true", help="print token use per step")
    ap.add_argument("--doctor", action="store_true", help="check the setup and exit")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    cfg = load_config()
    apfel = Apfel(cfg.get("url"), args.debug, cfg.get("permissive", True))
    agent = Agent(cfg, apfel, yes=args.yes, debug=args.debug)

    if args.doctor:
        print(f"server: {apfel.url} ({'private' if apfel.proc else 'shared'})")
        print(f"home:   {HOME}")
        print(f"tools:  {', '.join(t['function']['name'] for t in agent.offered_tools())}"
              f" (+ {', '.join(sorted(GATED))} when a skill unlocks them)")
        print(f"skills: {', '.join(agent.skills) or 'none'}")
        print(f"mcp:    {', '.join(f'{n} ({len(s.tools)} tools)' for n, s in agent.mcp.items()) or 'none'}")
        r = apfel.chat([{"role": "system", "content": agent.system_prompt()},
                        {"role": "user", "content": "Say OK."}], agent.offered_tools())
        base = r["usage"]["prompt_tokens"]
        print(f"base prompt incl. tools: {base} tokens of 8192 ({8192 - base - MAX_OUT} left for the chat)")
        return

    prompt = " ".join(args.prompt).strip()
    piped = read_piped_stdin()
    if piped.strip():
        prompt = ("<input>\n" + clip(piped, 9000) + "\n</input>\n\n"
                  + (prompt or "What is this? Sum it up briefly."))
    if prompt:
        print(agent.turn(prompt))
    else:
        repl(agent)


if __name__ == "__main__":
    main()
