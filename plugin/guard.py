"""PreToolUse hook for Bash: stops heavy commands that bypass the `slots` queue.

Reads the hook input on stdin. A command segment that starts a compile, boots a simulator or starts an emulator
directly, for a pool that exists in the machine config, is refused with exit 2 and a message telling the agent how
to run it through `slots`. Everything else, and any error here, lets the command through: a broken guard must
never block an agent's work.

Config (optional, in ~/.agent-slots/config.json): "guard": false turns it off; "guard": {"rules": [{"pool": "build",
"pattern": "^make\\b"}]} replaces the built-in rules. A command that ends with `# slots-ok` is let through, for
when the user asked to bypass the queue."""
import json
import os
import re
import shlex
import sys

RULES = [
    ('build', r'^xcodebuild\b(?!.*\s-(list|showsdks|version|showBuildSettings|showdestinations|help)\b)'),
    ('build', r'^gradlew\s+\S*(assemble|build|test|bundle|install)'),
    ('build', r'^bazel\s+(build|test|run)\b'),
    ('ios-sim', r'^(xcrun\s+)?simctl\s+boot\b'),
    ('android-emu', r'^emulator\b.*\s-avd\b'),
]
OPERATORS = {'&&', '||', ';', '|', '&', ';;', '|&', '(', ')'}
PREFIXES = {'time', 'nice', 'exec', 'command', 'env', 'nohup', 'caffeinate', 'sudo'}


def segments(command):
    """Simple commands of a shell line. Quoted text stays inside its word, so `slots run x -- sh -c "a && b"`
    is one segment that starts with slots."""
    for line in command.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = '#'
        try:
            tokens = list(lexer)
        except ValueError:
            tokens = line.split()
        words = []
        for token in tokens + [';']:
            if token in OPERATORS:
                while words and (re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0]) or words[0] in PREFIXES):
                    words = words[1:]
                if words:
                    yield words
                words = []
            else:
                words.append(token)


def through_slots(words):
    name = os.path.basename(words[0])
    return name in ('slots', 'relay_sim.py') or (name.startswith('python') and len(words) > 1
                                                  and ('agent_slots' in words[1:3] or words[1].endswith('relay_sim.py')))


def violation(command, rules, pools):
    if re.search(r'#\s*slots-ok\s*$', command.strip()):
        return None
    for words in segments(command):
        if through_slots(words):
            continue
        words[0] = os.path.basename(words[0])
        text = ' '.join(words)
        for pool, pattern in rules:
            if pool in pools and re.search(pattern, text):
                return pool, text
    return None


def main():
    data = json.load(sys.stdin)
    if data.get('tool_name') != 'Bash':
        return 0
    command = data.get('tool_input', {}).get('command', '')
    home = os.environ.get('SLOTS_HOME', os.path.expanduser('~/.agent-slots'))
    path = os.environ.get('SLOTS_CONFIG', os.path.join(home, 'config.json'))
    try:
        with open(path) as stream:
            config = json.load(stream)
    except (OSError, ValueError):
        return 0  # no machine config yet: nothing is shared, nothing to guard
    guard = config.get('guard', {})
    if guard is False:
        return 0
    rules = [(r['pool'], r['pattern']) for r in guard.get('rules', [])] if isinstance(guard, dict) and \
        guard.get('rules') else RULES
    found = violation(command, rules, set(config.get('pools', {})))
    if not found:
        return 0
    pool, text = found
    tool = text.split()[0]
    key = ' --key <udid>' if pool == 'ios-sim' else ''
    print(f"agent-slots: `{tool}` runs outside the queue. Several agent sessions share this machine's '{pool}' "
          f"pool, so run it as:\n  slots run {pool} --owner <your session name>{key} -- {text[:200]}\n"
          f"For a hands-on check use `slots hold {pool}{key}`. `slots status` shows the queue. If the user asked to "
          f"bypass the queue, end the command with `# slots-ok`.", file=sys.stderr)
    return 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a broken guard lets the command through
        sys.exit(0)
