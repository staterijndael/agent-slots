"""A fake `xcrun simctl`: python3 fake_simctl.py STATE.json <simctl args>.

Records boots and shutdowns with times, and the most simulators ever booted at once."""
import fcntl
import json
import os
import sys
import time
from pathlib import Path

state = Path(sys.argv[1])
args = sys.argv[2:]
if args[0] == 'bootstatus':
    time.sleep(float(os.environ.get('FAKE_BOOTSTATUS_SLEEP', '0')))
    sys.exit(0)
lock = open(str(state) + '.lock', 'a')
fcntl.flock(lock, fcntl.LOCK_EX)
data = json.loads(state.read_text()) if state.exists() else {}
data.setdefault('booted', [])
data.setdefault('known', [])
data.setdefault('max', 0)
data.setdefault('events', [])
if args[:2] == ['list', 'devices']:
    names = sorted(set(data['booted']) | set(data['known']))
    print(json.dumps({'devices': {'iOS': [{'udid': u, 'name': 'sim ' + u[-1],
                                           'state': 'Booted' if u in data['booted'] else 'Shutdown'} for u in names]}}))
elif args[0] == 'boot':
    if args[1] in data['booted']:
        sys.stderr.write('Unable to boot device in current state: Booted')
    else:
        data['booted'].append(args[1])
        data['events'].append(['boot', args[1], time.time()])
    data['max'] = max(data['max'], len(data['booted']))
elif args[0] == 'shutdown':
    if args[1] in data['booted']:
        data['booted'].remove(args[1])
        data['events'].append(['shutdown', args[1], time.time()])
for event in data['events']:
    if event[1] not in data['known']:
        data['known'].append(event[1])
state.write_text(json.dumps(data))
