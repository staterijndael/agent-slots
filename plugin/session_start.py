"""SessionStart hook: makes the plugin's copy of agent_slots the machine's canonical one and tells the session
that heavy resources go through `slots`.

The plugin's own folder moves on every plugin update, and old folders are removed, so the canonical copy lives in
the plugin's data folder (CLAUDE_PLUGIN_DATA), which survives updates: this hook copies the package there when
it changed, and every waiting `slots` process re-executes the new copy within one poll. A machine config that
names another canonical copy (a git checkout, say) is left alone. Any error here is silent: a session must start."""
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(os.environ.get('CLAUDE_PLUGIN_ROOT', Path(__file__).resolve().parent.parent))
DATA = Path(os.environ.get('CLAUDE_PLUGIN_DATA', Path.home() / '.agent-slots' / 'plugin'))


def sync_package():
    source, target = ROOT / 'agent_slots', DATA / 'agent_slots'
    new = {p.name: p.read_bytes() for p in source.glob('*.py')}
    old = {p.name: p.read_bytes() for p in target.glob('*.py')} if target.exists() else {}
    if new == old:
        return target
    staging = DATA / 'agent_slots.new'
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for name, content in new.items():
        (staging / name).write_bytes(content)
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    return target


def main():
    sys.path.insert(0, str(ROOT))
    from agent_slots import config  # noqa: E402 - needs ROOT on the path

    canonical = sync_package()
    path = config.path()
    created = False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(config.DEFAULT_CONFIG, canonical=str(canonical)), indent=2) + '\n')
        created = True
    cfg = config.load()
    pools = ', '.join(f"{name} ({pool['type']}, {pool['capacity']})" for name, pool in cfg['pools'].items())
    slots = shutil.which('slots') and 'slots' or str(ROOT / 'scripts' / 'slots')
    note = (f"This machine shares heavy resources between agent sessions through the agent-slots plugin. "
            f"Pools: {pools or 'none configured'}. Compiles, simulator and emulator boots go through "
            f"`{slots} run <pool> --owner <session name> -- <command>`; `{slots} status` shows the queue. "
            f"The agent-slots:slots skill has the details.")
    if created:
        note += f" A starter config was written to {path}; /agent-slots:setup tunes it for this machine."
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': note}}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never block a session from starting
        sys.exit(0)
