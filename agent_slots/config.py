"""Machine config: which pools exist and how each hands out its slots.

One config per machine, because the resources it guards (memory, cores, simulators) are the machine's, whatever
project an agent works in. It is re-read on every poll, so a capacity change reaches waiters already queued."""
import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get('SLOTS_HOME', str(Path.home() / '.agent-slots')))

POOL_DEFAULTS = {
    'type': 'token',
    'capacity': 1,
    'strategy': 'fifo',
    'limits': [],
    'keys': [],
    'env': {},
    'lease_minutes': 10,
    'max_lease_minutes': 30,
    'run_timeout': 1200,
    'stale_seconds': 600,
    'start_timeout': 180,
    'backfill_seconds': 120,
    'stop_on_release': True,
    'strays': 'stop-when-needed',
}

# The prototype's numbers (relay_sim.py, M3 Pro with 18 GB, 2026-11-03): two simulators, one under 20% free
# memory or under 25% with swap over 90%, none under 10%; load alone (compiles) lowers to one, never to zero.
# A second compile only with 25% free and load under 2 per core.
DEFAULT_CONFIG = {
    'poll_seconds': 2,
    'pools': {
        'build': {
            'type': 'token', 'capacity': 2, 'run_timeout': 3600,
            'limits': [{'when': {'free_below': 25}, 'max': 1}, {'when': {'load_above': 2}, 'max': 1}],
        },
    },
}
if sys.platform == 'darwin':
    DEFAULT_CONFIG['pools']['ios-sim'] = {
        'type': 'simctl', 'capacity': 2,
        'limits': [
            {'when': {'free_below': 10}, 'max': 0},
            {'when': {'free_below': 20}, 'max': 1},
            {'when': {'swap_above': 90, 'free_below': 25}, 'max': 1},
            {'when': {'load_above': 3}, 'max': 1},
        ],
    }


def path():
    return Path(os.environ.get('SLOTS_CONFIG', str(HOME / 'config.json')))


def load():
    file = path()
    data = json.loads(file.read_text()) if file.exists() else DEFAULT_CONFIG
    pools = {}
    for name, pool in data.get('pools', {}).items():
        merged = dict(POOL_DEFAULTS, **pool)
        merged['name'] = name
        pools[name] = merged
    return {'poll_seconds': float(os.environ.get('SLOTS_POLL', data.get('poll_seconds', 2))),
            'canonical': os.environ.get('SLOTS_CANONICAL', data.get('canonical')),
            'pools': pools, 'source': str(file) if file.exists() else 'built-in defaults'}


def allowed(pool, now_readings):
    """How many units the pool may have granted right now: its capacity, lowered by every limit whose
    conditions all hold. Checked before a grant, never against slots already held."""
    capacity = int(pool['capacity'])
    best = capacity
    reasons = []
    for limit in pool['limits']:
        if matches(limit.get('when', {}), now_readings):
            if int(limit['max']) < best:
                best = int(limit['max'])
                reasons = [describe(limit['when'], now_readings)]
    return best, '; '.join(reasons)


def matches(when, r):
    tests = {
        'free_below': lambda v: r['free'] < v,
        'free_above': lambda v: r['free'] > v,
        'swap_above': lambda v: r['swap'] > v,
        'load_above': lambda v: r['load'] > v,
    }
    for name, value in when.items():
        if name not in tests:
            raise SystemExit(f"slots: unknown limit condition {name!r} (known: {', '.join(tests)})")
        if not tests[name](value):
            return False
    return True


def describe(when, r):
    return ', '.join(f"{name.split('_')[0]} {r[name.split('_')[0]]}{'%' if name != 'load_above' else '/core'}"
                     for name in when)
