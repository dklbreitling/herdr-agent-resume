#!/usr/bin/env python3
"""Agent resume for the Herdr app.

herdr-core brings a terminal that lost its program (reboot, logout, a core
restart) back as a shell in its last folder, but never runs the program again.
This brings Claude Code and Codex sessions back, the way the Herdr TUI did:

  hook claude|codex   SessionStart hook: note the session on its terminal
                      (an `agent-resume` label kept by herdr-core)
  resume              type `claude --resume <id>` / `codex resume <id>` into each
                      terminal that lost its agent and sits at a shell
  status              what `resume` would do, without typing anything
  adopt               note Claude Code sessions already running before the hook
                      was installed (from ~/.claude/sessions/<pid>.json)
  watch ...           internal: forget a session once its agent exits while the
                      terminal lives on, so a session you quit stays quit

Standard library only (Python 3.8+). `resume` and `status` print JSON.
"""
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time

LABEL = "agent-resume"
AGENTS = {
    "claude": {"names": {"claude"}, "argv": lambda session: ["claude", "--resume", session]},
    "codex": {"names": {"codex"}, "argv": lambda session: ["codex", "resume", session]},
}
AGENT_NAMES = set().union(*(a["names"] for a in AGENTS.values()))
SHELLS = {"fish", "bash", "zsh", "sh", "dash", "ksh", "mksh"}
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,127}$")
# Typed into a shell between single quotes: no quotes, backslashes or control characters.
TYPABLE_PATH_RE = re.compile(r"^/[^'\\\x00-\x1f\x7f]*$")

SEND_GAP = 0.15      # seconds between two terminals, like the Herdr TUI's spacing
SHELL_SETTLE = 1.5   # a restored shell runs this long before anything is typed into it
START_WAIT = 12.0    # how long `resume` waits for restored shells that haven't started yet
EXIT_GRACE = 2.0     # after an agent exits, wait this long before deciding it quit by itself
HOOK_HC_TIMEOUT = 3  # the agent waits for its hook: never hold it up long
LOG_LIMIT = 256 * 1024

NOT_STARTED = "shell not started yet"


class HcError(Exception):
    pass


# ---------------------------------------------------------------- processes

def _proc_linux(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    comm = stat[stat.index("(") + 1:stat.rindex(")")]
    fields = stat[stat.rindex(")") + 2:].split()
    argv0 = ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv0 = f.read().split(b"\0", 1)[0].decode("utf-8", "replace")
    except OSError:
        pass
    # fields[0] is stat field 3 (state): ppid is field 4, starttime field 22.
    return {"ppid": int(fields[1]), "names": _names(comm, argv0), "start": int(fields[19])}


def _proc_ps(pid):
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return {"ppid": int(parts[0]), "names": _names(parts[1]), "start": None}


def _names(*raw):
    return {os.path.basename(n.strip()).lstrip("-") for n in raw if n and n.strip()}


def proc(pid):
    """{ppid, names, start} of a process, or None when it's gone."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.path.isdir("/proc/self"):
        return _proc_linux(pid)
    return _proc_ps(pid)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def find_agent(agent, pid, lookup=proc):
    """The nearest process at or above `pid` that is this agent."""
    names = AGENTS[agent]["names"]
    for _ in range(64):
        info = lookup(pid) if pid and pid > 1 else None
        if not info:
            return None
        if info["names"] & names:
            return pid
        pid = info["ppid"]
    return None


def placement(agent_pid, shell_pid, lookup=proc):
    """Where the agent runs: "direct" in the terminal's own program, "nested" under
    another agent (a one-off run from an agent's tool), "outside" when the terminal's
    program isn't above it (tmux, ssh, ...)."""
    if agent_pid == shell_pid:
        return "direct"
    info = lookup(agent_pid)
    pid = info["ppid"] if info else None
    for _ in range(64):
        if pid == shell_pid:
            return "direct"
        info = lookup(pid) if pid and pid > 1 else None
        if not info:
            return "outside"
        if info["names"] & AGENT_NAMES:
            return "nested"
        pid = info["ppid"]
    return "outside"


def shell_age(pid):
    """Seconds since the process started, or None when unknown."""
    info = proc(pid)
    if not info or info.get("start") is None:
        return None
    try:
        with open("/proc/stat") as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
        return time.time() - (btime + info["start"] / os.sysconf("SC_CLK_TCK"))
    except (OSError, StopIteration, ValueError):
        return None


def wait_exit(pid):
    """Block until `pid` exits."""
    if hasattr(os, "pidfd_open"):
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            return
        try:
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            while not poller.poll(None):
                pass
        finally:
            os.close(fd)
        return
    while alive(pid):
        time.sleep(2)


def boot_id():
    """Something that changes with every boot, or None."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip() or None
    except OSError:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
        match = re.search(r"sec = (\d+)", out)
        if match:
            return "boottime:" + match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# ---------------------------------------------------------------- herdr-core

def hc_path():
    return shutil.which("hc") or os.path.expanduser("~/.local/bin/hc")


def hc(args, plain=False, timeout=10):
    """Runs hc. `plain`: as an outsider, so `hc send` types exactly the text (from inside
    a Herdr terminal it would wrap it in <hc from="tN">, which is meant for agents)."""
    env = dict(os.environ)
    if plain:
        env.pop("HC_TERMINAL_ID", None)
        env.pop("HC_SCOPE", None)
    try:
        r = subprocess.run([hc_path(), *args], capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise HcError(f"hc {args[0]}: {error}") from error
    if r.returncode != 0:
        raise HcError(f"hc {args[0]}: {(r.stderr or r.stdout).strip() or 'exit ' + str(r.returncode)}")
    return r.stdout


def hc_json(args, timeout=10):
    try:
        return json.loads(hc([*args, "--json"], timeout=timeout))
    except ValueError as error:
        raise HcError(f"hc {args[0]}: not JSON") from error


def valid_session(value):
    return isinstance(value, str) and bool(SESSION_RE.match(value))


def read_label(labels):
    """The saved session in a terminal's labels, or None."""
    raw = (labels or {}).get(LABEL)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("agent") not in AGENTS or not valid_session(value.get("session")):
        return None
    return value


# ---------------------------------------------------------------- log

def data_dir():
    app = os.environ.get("HERDR_APP_HOME") or os.path.expanduser("~/.config/herdr-app")
    return os.path.join(app, "data", "agent-resume")


def log(event, **fields):
    try:
        os.makedirs(data_dir(), exist_ok=True)
        path = os.path.join(data_dir(), "log.jsonl")
        line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(path) > LOG_LIMIT:
            with open(path, "rb") as f:
                f.seek(-LOG_LIMIT // 2, os.SEEK_END)
                tail = f.read().split(b"\n", 1)[-1]
            with open(path, "wb") as f:
                f.write(tail)
    except OSError:
        pass


def runtime_lock(name):
    """An exclusive lock held until this process exits, or None if someone holds it."""
    import fcntl
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    folder = os.path.join(base, f"herdr-agent-resume-{os.getuid()}")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(folder, name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


# ---------------------------------------------------------------- hook

def hook(agent, raw, env=None):
    """SessionStart: note the session on this terminal. Returns what it did, for the log."""
    env = os.environ if env is None else env
    term = env.get("HC_TERMINAL_ID")
    if not term:
        return "not in a Herdr terminal"
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return "input is not JSON"
    if not isinstance(data, dict):
        return "input is not an object"
    if (data.get("hook_event_name") or "SessionStart") != "SessionStart":
        return "not SessionStart"
    if data.get("agent_id"):
        return "subagent"
    session = data.get("session_id")
    if not valid_session(session):
        return "no usable session id"
    if agent == "codex" and env.get("CODEX_THREAD_ID") not in (None, "", session):
        return "codex sub-thread"
    transcript = data.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return "no transcript: the session can't be resumed"
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else os.getcwd()

    agent_pid = find_agent(agent, os.getppid())
    if agent_pid is None and agent == "claude" and str(env.get("CLAUDE_PID", "")).isdigit():
        agent_pid = int(env["CLAUDE_PID"])
    if not agent_pid:
        return "agent process not found"
    terminal = hc_json(["get", term], timeout=HOOK_HC_TIMEOUT)
    shell = terminal.get("pid")
    if terminal.get("state") != "live" or not shell:
        return "terminal not live"
    where = placement(agent_pid, shell)
    if where != "direct":
        return f"agent runs {where}"

    remember(term, agent, session, cwd, transcript, agent_pid, shell)
    return "saved"


def remember(term, agent, session, cwd, transcript, agent_pid, shell):
    """Label the terminal with the session and start its watcher."""
    label = {"agent": agent, "session": session, "cwd": cwd, "transcript": transcript,
             "pid": agent_pid, "shell": shell, "boot": boot_id(), "at": int(time.time() * 1000)}
    hc(["label", term, f"{LABEL}={json.dumps(label, separators=(',', ':'), ensure_ascii=False)}"], timeout=HOOK_HC_TIMEOUT)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "watch", term, str(agent_pid), str(shell)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True)


def claude_session_of(pid, home=None):
    """(session, cwd, transcript) of a running interactive Claude Code process, from the
    registry it keeps in ~/.claude/sessions/<pid>.json, or None."""
    home = home or os.path.expanduser("~")
    try:
        with open(os.path.join(home, ".claude", "sessions", f"{pid}.json"), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    session, cwd = info.get("sessionId"), info.get("cwd")
    if info.get("pid") != pid or info.get("kind") not in (None, "interactive") or not valid_session(session):
        return None
    import glob
    found = glob.glob(os.path.join(home, ".claude", "projects", "*", f"{session}.jsonl"))
    if not found or not isinstance(cwd, str):
        return None
    return session, cwd, found[0]


def adopt():
    """Label Claude Code sessions that were already running before the hook was installed.
    (Codex sessions are picked up the next time they start or resume.)"""
    adopted, skipped = [], []
    for terminal in gather():
        fg, shell = terminal.get("foreground") or {}, terminal.get("pid")
        agent_pid = fg.get("pid")
        if not shell or not agent_pid or not (_names(fg.get("name") or "") & AGENTS["claude"]["names"]):
            continue
        item = {"terminal": terminal["address"], "where": (terminal.get("labels") or {}).get("where"), "agent": "claude"}
        label = read_label(terminal.get("labels"))
        if label and label.get("pid") == agent_pid:
            skipped.append({**item, "reason": "already noted"})
            continue
        found = claude_session_of(agent_pid)
        if not found:
            skipped.append({**item, "reason": "session not found"})
            continue
        if placement(agent_pid, shell) != "direct":
            skipped.append({**item, "reason": "not run by the terminal's shell"})
            continue
        session, cwd, transcript = found
        remember(terminal["address"], "claude", session, cwd, transcript, agent_pid, shell)
        adopted.append({**item, "session": session})
        log("adopt", terminal=terminal["address"], agent="claude", session=session)
    return {"adopted": adopted, "skipped": skipped}


def watch(term, agent_pid, shell):
    """Runs detached beside an agent. When the agent exits and its terminal's program is
    still the same one, the person quit it: forget the session. When the terminal went
    with it (reboot, logout, core restart, pane closed), keep it for `resume`."""
    lock = runtime_lock(f"watch-{agent_pid}")
    if lock is None:
        return "already watched"
    wait_exit(agent_pid)
    time.sleep(EXIT_GRACE)
    if not alive(shell):
        return "terminal went with it"
    terminal = hc_json(["get", term])
    if terminal.get("state") != "live" or terminal.get("pid") != shell:
        return "terminal was replaced"
    label = read_label(terminal.get("labels"))
    if not label or label.get("pid") != agent_pid:
        return "a newer session took over"
    hc(["label", term, f"{LABEL}="])
    return "forgot " + label["session"]


# ---------------------------------------------------------------- resume

def lost(terminal, label, boot):
    """True when the program the agent ran under is gone: another boot, or another process."""
    if boot and label.get("boot") and label["boot"] != boot:
        return True
    return terminal.get("pid") != label.get("shell")


def at_shell(terminal):
    fg = terminal.get("foreground") or {}
    return bool(terminal.get("pid")) and fg.get("pid") == terminal.get("pid") and _names(fg.get("name") or "") & SHELLS


def command(label):
    """What to type, or None when the folder can't be typed safely."""
    cwd = label.get("cwd")
    if not isinstance(cwd, str) or not TYPABLE_PATH_RE.match(cwd):
        return None
    return f"cd '{cwd}' && " + " ".join(AGENTS[label["agent"]]["argv"](label["session"]))


def plan(terminals, boot, folder_ok=os.path.isdir, file_ok=os.path.isfile):
    """Which terminals to resume. Pure: `terminals` are `hc get` records."""
    entries = []
    for terminal in terminals:
        if terminal.get("state") != "live":
            continue
        label = read_label(terminal.get("labels"))
        if label:
            entries.append((terminal, label, lost(terminal, label, boot)))
    still_open = {(label["agent"], label["session"]) for _, label, gone in entries if not gone}
    todo, skipped, taken = [], [], set()
    for terminal, label, gone in sorted(entries, key=lambda e: -(e[1].get("at") or 0)):
        if not gone:
            continue
        key = (label["agent"], label["session"])
        item = {"terminal": terminal["address"], "where": (terminal.get("labels") or {}).get("where"),
                "agent": label["agent"], "session": label["session"], "cwd": label.get("cwd")}
        typed = command(label)
        if key in still_open:
            reason = "open in another terminal"
        elif key in taken:
            reason = "resumed in another terminal"
        elif not terminal.get("pid"):
            reason = NOT_STARTED
        elif not at_shell(terminal):
            reason = "something else is running there"
        elif not folder_ok(label.get("cwd")):
            reason = "its folder is gone"
        elif not file_ok(label.get("transcript")):
            reason = "no saved conversation to resume"
        elif typed is None:
            reason = "its folder name can't be typed safely"
        else:
            reason = None
        if reason:
            skipped.append({**item, "reason": reason})
        else:
            taken.add(key)
            todo.append({**item, "command": typed})
    return todo, skipped


def gather():
    """`hc get` of every live local terminal."""
    from concurrent.futures import ThreadPoolExecutor
    listed = hc_json(["ls"])
    rows = listed.get("terminals", []) if isinstance(listed, dict) else listed
    live = [t["address"] for t in rows if t.get("state") == "live" and "/" not in str(t.get("address"))]

    def get(address):
        try:
            return hc_json(["get", address])
        except HcError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [t for t in pool.map(get, live) if t]


def resume(dry_run=False, wait=START_WAIT):
    lock = runtime_lock("resume")
    if lock is None:
        return {"busy": True, "resumed": [], "skipped": []}
    boot = boot_id()
    deadline = time.monotonic() + wait
    while True:
        todo, skipped = plan(gather(), boot)
        if dry_run or time.monotonic() >= deadline or not any(s["reason"] == NOT_STARTED for s in skipped):
            break
        time.sleep(0.5)
    if dry_run:
        return {"resumed": todo, "skipped": skipped, "dry_run": True}

    resumed = []
    for index, item in enumerate(todo):
        if index:
            time.sleep(SEND_GAP)
        try:
            terminal = hc_json(["get", item["terminal"]])
            if not at_shell(terminal):
                skipped.append({**item, "reason": "something else is running there"})
                continue
            age = shell_age(terminal["pid"])
            if age is not None and age < SHELL_SETTLE:
                time.sleep(SHELL_SETTLE - age)
            hc(["send", item["terminal"], item["command"]], plain=True)
        except HcError as error:
            skipped.append({**item, "reason": f"couldn't type into it: {error}"})
            continue
        resumed.append(item)
        log("resume", terminal=item["terminal"], where=item["where"], agent=item["agent"], session=item["session"])
    for item in skipped:
        log("skip", terminal=item["terminal"], agent=item["agent"], session=item["session"], reason=item["reason"])
    return {"resumed": resumed, "skipped": skipped}


# ---------------------------------------------------------------- CLI

def main(argv):
    command_name = argv[1] if len(argv) > 1 else "status"
    if command_name == "hook":
        agent = argv[2] if len(argv) > 2 else ""
        raw = sys.stdin.read()
        if agent not in AGENTS:
            return 0
        try:
            outcome = hook(agent, raw)
        except Exception as error:  # a hook must never get in the agent's way
            outcome = f"error: {error}"
        if outcome != "not in a Herdr terminal":
            log("hook", agent=agent, terminal=os.environ.get("HC_TERMINAL_ID"), outcome=outcome)
        return 0
    if command_name == "watch":
        term, agent_pid, shell = argv[2], int(argv[3]), int(argv[4])
        try:
            outcome = watch(term, agent_pid, shell)
        except Exception as error:
            outcome = f"error: {error}"
        if outcome.startswith(("forgot", "error")):
            log("watch", terminal=term, outcome=outcome)
        return 0
    if command_name in ("resume", "status", "adopt"):
        try:
            if command_name == "adopt":
                out = adopt()
            else:
                out = resume(dry_run=command_name == "status" or "--dry-run" in argv)
        except HcError as error:
            print(json.dumps({"error": str(error)}))
            return 1
        print(json.dumps(out, indent=2 if sys.stdout.isatty() else None, ensure_ascii=False))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
