#!/usr/bin/env python3
"""Install Agent resume: the mod, its shortcut and the SessionStart hooks.

- copies mods/agent-resume into the Herdr app's mods folder;
- adds Ctrl+Shift+R (`mod.open:agent-resume`) to keys.json, refusing a key already in use;
- appends a SessionStart hook to ~/.claude/settings.json and ~/.codex/hooks.json,
  next to the hooks already there (each file is backed up first);
- notes Claude Code sessions that are already running (`agent_resume.py adopt`).

No layout change is needed. `--uninstall` takes all of it out again.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
MOD_NAME = "agent-resume"
PACKAGE = "herdr-agent-resume"
VERSION = "0.1.0"
MARKER = ".agent-resume-install.json"
FILES = ("main.js", "summary.js", "mod.json", "agent_resume.py")
GENERATED = {"gpui-kit.d.ts", "jsconfig.json", "__pycache__"}
HOOK_TAG = "agent_resume.py' hook "


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".agent-resume-", delete=False) as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        temp = Path(stream.name)
    if path.exists():
        os.chmod(temp, path.stat().st_mode & 0o777)
    temp.replace(path)


def merge_keys(existing, snippet):
    merged = json.loads(json.dumps(existing))
    for section, bindings in snippet.items():
        target = merged.setdefault(section, {})
        for key, action in bindings.items():
            if key in target and target[key] != action:
                raise ValueError(f"Shortcut conflict in {section}: {key} is {target[key]!r}. No files changed.")
            target[key] = action
    return merged


def remove_keys(existing, snippet):
    merged = json.loads(json.dumps(existing))
    for section, bindings in snippet.items():
        target = merged.get(section, {})
        for key, action in bindings.items():
            if target.get(key) == action:
                del target[key]
    return merged


def hook_command(script, agent):
    return f"python3 '{script}' hook {agent}"


def is_ours(group):
    return any(HOOK_TAG in str(h.get("command", "")) for h in group.get("hooks", []) if isinstance(h, dict))


def add_hook(config, command):
    """`config` with one SessionStart group running `command` at the end; existing groups
    keep their places (Codex remembers which hooks you trusted by position)."""
    merged = json.loads(json.dumps(config))
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("`hooks` is not an object")
    groups = hooks.setdefault("SessionStart", [])
    ours = [g for g in groups if isinstance(g, dict) and is_ours(g)]
    if ours:
        for group in ours:
            for h in group.get("hooks", []):
                if HOOK_TAG in str(h.get("command", "")):
                    h["command"] = command
        return merged
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    return merged


def remove_hook(config):
    merged = json.loads(json.dumps(config))
    groups = merged.get("hooks", {}).get("SessionStart")
    if isinstance(groups, list):
        merged["hooks"]["SessionStart"] = [g for g in groups if not (isinstance(g, dict) and is_ours(g))]
        if not merged["hooks"]["SessionStart"]:
            del merged["hooks"]["SessionStart"]
    return merged


def verify_installed(destination, marker):
    if marker.get("package") != PACKAGE:
        raise ValueError("The existing mod belongs to another package")
    known = set(marker["files"]) | GENERATED | {MARKER}
    for path in destination.iterdir():
        if path.name not in known:
            raise ValueError(f"Extra installed file would be replaced: {path}. Preserve it before updating.")
    for filename, expected in marker["files"].items():
        path = destination / filename
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"Installed file was edited: {path}. Preserve that edit before updating.")


def agent_configs(home):
    """(name, path, existing config) for each agent that is set up on this machine."""
    out = []
    for name, folder, file in (("claude", ".claude", "settings.json"), ("codex", ".codex", "hooks.json")):
        if not (home / folder).is_dir():
            continue
        path = home / folder / file
        config = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError(f"{path} is not a JSON object")
        out.append((name, path, config))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app-home", type=Path, default=Path(os.environ.get("HERDR_APP_HOME", Path.home() / ".config/herdr-app")))
    parser.add_argument("--home", type=Path, default=Path.home(), help="where ~/.claude and ~/.codex live")
    parser.add_argument("--update", action="store_true", help="update files previously installed by this script")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--no-adopt", action="store_true", help="don't note Claude Code sessions already running")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    app = args.app_home.expanduser().resolve()
    home = args.home.expanduser().resolve()
    env = {**os.environ, "HERDR_APP_HOME": str(app)}
    keys_path = app / "keys.json"
    snippet = json.loads((ROOT / "keys.json").read_text())
    source = ROOT / "mods" / MOD_NAME
    destination = app / "mods" / MOD_NAME
    script = destination / "agent_resume.py"
    configs = agent_configs(home)

    if args.uninstall:
        keys = remove_keys(json.loads(keys_path.read_text()), snippet)
        changes = [(path, remove_hook(config)) for _, path, config in configs]
        changes = [(path, new) for (path, new), (_, _, old) in zip(changes, configs) if new != old]
    else:
        keys = merge_keys(json.loads(keys_path.read_text()), snippet)
        changes = [(path, add_hook(config, hook_command(script, name))) for name, path, config in configs]
        changes = [(path, new) for (path, new), (_, _, old) in zip(changes, configs) if new != old]
        if destination.exists():
            marker_path = destination / MARKER
            if not args.update or not marker_path.exists():
                raise ValueError(f"{destination} already exists. Use --update only for this installer's own files.")
            verify_installed(destination, json.loads(marker_path.read_text()))
        subprocess.run(["herdr-app", "check", str(source)], env=env, check=True)

    print(f"Mod: {destination}\nShortcut: Ctrl+Shift+R")
    for path, _ in changes:
        print(f"Hook: {path}")
    hooked = {name for name, _, _ in configs}
    if args.dry_run:
        print("Dry run: no files changed.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = app / "backups" / MOD_NAME / stamp
    backup.mkdir(parents=True)
    shutil.copy2(keys_path, backup / "keys.json")
    for name, path, _ in configs:
        if path.exists():
            shutil.copy2(path, backup / f"{name}-{path.name}")
    if destination.exists():
        shutil.copytree(destination, backup / MOD_NAME, ignore=shutil.ignore_patterns("__pycache__"))

    if args.uninstall:
        for path, config in changes:
            atomic_json(path, config)
        atomic_json(keys_path, keys)
        if destination.exists():
            shutil.rmtree(destination)
        print(f"Uninstalled. Backup: {backup}")
        return

    stage = Path(tempfile.mkdtemp(prefix=".agent-resume-stage-", dir=app))
    previous = app / f".agent-resume-previous-{stamp}"
    replaced = False
    try:
        for filename in FILES:
            shutil.copy2(source / filename, stage / filename)
        atomic_json(stage / MARKER, {
            "package": PACKAGE, "version": VERSION,
            "files": {filename: digest(source / filename) for filename in FILES},
        })
        if destination.exists():
            destination.replace(previous)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage.replace(destination)
        replaced = True
        atomic_json(keys_path, keys)
        for path, config in changes:
            atomic_json(path, config)
    except Exception:
        if replaced and destination.exists():
            shutil.rmtree(destination)
        if previous.exists():
            previous.replace(destination)
        shutil.copy2(backup / "keys.json", keys_path)
        for name, path, _ in configs:
            saved = backup / f"{name}-{path.name}"
            if saved.exists():
                shutil.copy2(saved, path)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if previous.exists():
            shutil.rmtree(previous)
    print(f"Installed. Backup: {backup}")

    if not args.no_adopt:
        adopted = subprocess.run(["python3", str(script), "adopt"], env={k: v for k, v in env.items() if k not in ("HC_TERMINAL_ID", "HC_SCOPE")},
                                 capture_output=True, text=True)
        try:
            for item in json.loads(adopted.stdout).get("adopted", []):
                print(f"Noted the running Claude Code session in {item['where'] or item['terminal']}")
        except ValueError:
            print(f"Couldn't note running sessions: {(adopted.stderr or adopted.stdout).strip()}")
    if "codex" in hooked and any(path.parent.name == ".codex" for path, _ in changes):
        print("Codex asks you to review the new hook the next time it starts: choose to trust it.")
    print("New Claude Code sessions pick up the hook when they start; running ones keep their old hooks.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
