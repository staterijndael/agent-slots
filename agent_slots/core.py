"""The scheduler without a daemon: every waiter is its own process that polls one JSON state file under flock.

State (SLOTS_HOME/state.json): `queue` (waiting tickets), `grants` (slots held) and `cooldown` (instances that
failed to start). Every poll a waiter tidies the state (drops dead waiters, takes back slots whose holder died,
whose lease ran out or whose instance is gone), asks the pool's strategy for the order, and grants itself a slot
if the plan says it is next. Capacity is judged by live memory and load before a grant, never against a slot
already held."""
import contextlib
import datetime
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config, resources, strategies, system

PACKAGE = Path(__file__).resolve().parent
REPORT_SECONDS = 30


class SlotError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def log(message):
    print(f"slots: {message}", file=sys.stderr, flush=True)
    config.HOME.mkdir(parents=True, exist_ok=True)
    with open(config.HOME / 'log.txt', 'a') as stream:
        stream.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [{os.getpid()}] {message}\n")


@contextlib.contextmanager
def state():
    config.HOME.mkdir(parents=True, exist_ok=True)
    with open(config.HOME / 'state.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = config.HOME / 'state.json'
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            data = {}
        for name, empty in (('queue', []), ('grants', []), ('cooldown', {}), ('durations', {}), ('modes', {}), ('recent', {})):
            data.setdefault(name, empty)
        yield data
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)


class Scheduler:
    def __init__(self):
        self.reload()
        self.kinds = {}
        self.mode = ''

    def reload(self):
        self.cfg = config.load()

    def pool(self, name):
        if name not in self.cfg['pools']:
            raise SlotError(f"no pool {name!r} in {self.cfg['source']} (pools: {', '.join(self.cfg['pools'])})", 2)
        return self.cfg['pools'][name]

    def kind(self, name):
        pool = self.pool(name)
        cached = self.kinds.get(name)
        if not cached or cached.pool != pool:
            cached = self.kinds[name] = resources.load(pool, config.HOME, log)
        return cached

    # -- tidying ------------------------------------------------------------------------------------------------

    def tidy(self, data, now):
        """Takes back slots nobody uses any more; returns (pool, key) of each freed slot."""
        data['queue'] = [t for t in data['queue'] if system.alive(t['pid'])]
        freed, keep = [], []
        stale_checked = {}
        for grant in data['grants']:
            why = self._dead_reason(grant, now, stale_checked)
            if why:
                orphan = not system.alive(grant.get('pid'), grant.get('token'))
                if orphan and grant.get('child') and system.alive(grant['child'], grant.get('child_token')):
                    resources.stop_group(grant['child'])  # an orphaned command would keep using the instance
                log(f"{grant['pool']}: slot of {grant['owner']}{' on ' + grant['key'] if grant.get('key') else ''} "
                    f"freed: {why}")
                freed.append((grant['pool'], grant.get('key')))
            else:
                keep.append(grant)
        data['grants'] = keep
        return freed

    def _dead_reason(self, grant, now, stale_checked):
        if grant.get('starting'):
            if system.alive(grant['starting']):
                return None  # never take back, or stop, an instance in the middle of its start
            return 'holder died while starting the instance'
        if grant.get('pid'):
            if not system.alive(grant['pid'], grant.get('token')):
                return 'holder exited'
        elif grant.get('lease_until', 0) <= now:
            return 'lease ran out'
        pool = self.cfg['pools'].get(grant['pool'])
        if not pool or not grant.get('key') or now - grant['since'] <= pool['stale_seconds']:
            return None
        kind = self.kind(grant['pool'])
        if not kind.lifecycle:
            return None
        if grant['pool'] not in stale_checked:
            stale_checked[grant['pool']] = kind.running()
        running = stale_checked[grant['pool']]
        if running is not None and grant['key'] not in running:
            return f"held {int(now - grant['since'])} s with its instance not running"
        return None

    def stop_unused(self, data, freed, reason):
        """Stops freed instances unless another slot holds them or a waiter wants the same one."""
        for pool_name, key in set(freed):
            pool = self.cfg['pools'].get(pool_name)
            if not key or not pool or not pool['stop_on_release'] or not self.kind(pool_name).lifecycle:
                continue
            wanted = {g.get('key') for g in data['grants'] if g['pool'] == pool_name}
            wanted |= {t.get('key') for t in data['queue'] if t['pool'] == pool_name}
            if key not in wanted:
                self.kind(pool_name).stop(key, reason)

    # -- planning -----------------------------------------------------------------------------------------------

    def plan(self, pool, data, allowed, now):
        """Tickets of the pool that may be granted now, in order, each with the key it gets."""
        grants = [g for g in data['grants'] if g['pool'] == pool['name']]
        waiting = [t for t in data['queue'] if t['pool'] == pool['name']]
        free = allowed - sum(g.get('units', 1) for g in grants)
        taken = {g.get('key') for g in grants if g.get('key')}
        prefix = pool['name'] + '/'
        cooling = {name[len(prefix):] for name, entry in data['cooldown'].items()
                   if name.startswith(prefix) and entry['until'] > now}
        planned = []
        window = float(pool.get('fair_window_minutes', 10)) * 60
        recent = {}
        for owner, at in data['recent'].get(pool['name'], []):
            if now - at < window:
                recent[owner] = recent.get(owner, 0) + 1
        ctx = {'pool': pool, 'free': free, 'durations': data['durations'], 'recent': recent}
        self.mode = strategies.order(pool['strategy'], waiting, grants, now, ctx)[1] if waiting else ''
        while waiting:
            chosen = None
            for ticket in strategies.order(pool['strategy'], waiting, grants + [p for p, _ in planned], now, ctx)[0]:
                key = ticket.get('key') or self._free_key(pool, taken | cooling)
                if pool['keys'] and not ticket.get('key') and not key:
                    continue
                if key and key in taken:
                    continue  # this one waits for its key; the rest are not blocked behind it
                units = self.units(pool, ticket)
                if units <= free:
                    chosen = (ticket, key)
                    break
                if now - ticket['since'] > pool['backfill_seconds']:
                    # Keep the room for a big waiter that has waited long enough, also while a limit holds the pool
                    # below its size: the small ones it holds back are often what keeps the load high.
                    return planned
            if not chosen:
                break
            ticket, key = chosen
            planned.append((dict(ticket, key=key), key))
            waiting.remove(ticket)
            free -= self.units(pool, ticket)
            if key:
                taken.add(key)
        return planned

    def note_mode(self, data, pool):
        """Logs when a pool's strategy changes the rule it orders by (adaptive switches on its own)."""
        rule = self.mode.split(':')[0]
        previous = data['modes'].get(pool['name'], {})
        if self.mode and rule != previous.get('rule'):
            if previous:
                log(f"{pool['name']}: {pool['strategy']} switched {previous['rule']} -> {self.mode}")
            data['modes'][pool['name']] = {'rule': rule, 'why': self.mode, 'since': time.time()}

    @staticmethod
    def _free_key(pool, taken):
        return next((k for k in pool['keys'] if k not in taken), None)

    @staticmethod
    def units(pool, ticket):
        return int(pool['capacity']) if ticket.get('exclusive') else int(ticket.get('units', 1))

    def allowed(self, pool):
        if not pool['limits']:
            return int(pool['capacity']), '', None
        readings = system.readings()
        count, why = config.allowed(pool, readings)
        return count, why, readings

    def make_room(self, pool, data, key, allowed):
        """Stops instances running without a slot when the new one would not fit; False if it still won't."""
        if not key or pool['strays'] == 'ignore' or not self.kind(pool['name']).lifecycle:
            return True
        kind = self.kind(pool['name'])
        running = kind.running() or set()
        held = {g.get('key') for g in data['grants'] if g['pool'] == pool['name']}
        over = len(running | held | {key}) - allowed
        for stray in sorted(running - held - {key}):
            if over <= 0:
                break
            if pool['strays'] == 'stop-when-needed' and not kind.busy_elsewhere(stray):
                kind.stop(stray, 'running without a slot, room needed')
                over -= 1
        return over <= 0

    # -- acquiring ----------------------------------------------------------------------------------------------

    def acquire(self, pool_name, owner, key=None, units=1, exclusive=False, priority=0, lease_seconds=0,
                what='', holder_pid=None):
        pool = self.pool(pool_name)
        if self.kind(pool_name).lifecycle and not key and not pool['keys']:
            raise SlotError(f"pool {pool_name} manages instances; name one with --key", 2)
        if not exclusive and units > int(pool['capacity']):
            raise SlotError(f"pool {pool_name} has {pool['capacity']} units; asked for {units}", 2)
        since = float(os.environ.pop('SLOTS_SINCE', time.time()))
        ticket = {'id': uuid.uuid4().hex, 'pid': os.getpid(), 'owner': owner, 'pool': pool_name, 'key': key,
                  'units': units, 'exclusive': exclusive, 'priority': priority, 'since': since, 'what': what}
        started_version = canonical_version(self.cfg)
        with state() as data:
            self.check_cooldown(data, pool_name, key)
            data['queue'].append(ticket)
        reported = 0.0
        while True:
            self.reload()
            pool = self.pool(pool_name)
            with state() as data:
                now = time.time()
                self.stop_unused(data, self.tidy(data, now), 'its slot was freed')
                allowed, why, readings = self.allowed(pool)
                plan = self.plan(pool, data, allowed, now)
                self.note_mode(data, pool)
                mine = next((key for t, key in plan if t['id'] == ticket['id']), False)
                if mine is not False and self.make_room(pool, data, mine, allowed):
                    grant = {'id': ticket['id'], 'pool': pool_name, 'owner': owner, 'key': mine,
                             'units': self.units(pool, ticket), 'since': now, 'what': what,
                             'pid': holder_pid, 'token': system.start_token(holder_pid) if holder_pid else None,
                             'lease_until': now + lease_seconds if lease_seconds else 0}
                    if mine and self.kind(pool_name).lifecycle:
                        grant['starting'] = os.getpid()
                    data['grants'].append(grant)
                    data['queue'] = [t for t in data['queue'] if t['id'] != ticket['id']]
                    window = float(pool.get('fair_window_minutes', 10)) * 60
                    data['recent'][pool_name] = [r for r in data['recent'].get(pool_name, [])
                                                 if now - r[1] < window][-200:] + [[owner, now]]
                    log(f"{pool_name}: slot -> {owner}{' on ' + mine if mine else ''} "
                        f"after {int(now - since)} s: {what}")
                    break
                if now - reported > REPORT_SECONDS:
                    print(f"slots: waiting for {pool_name}: "
                          f"{self.explain(pool, data, ticket, allowed, why, readings)}", file=sys.stderr, flush=True)
                    reported = now
            if canonical_version(self.cfg) != started_version:
                with state() as data:
                    data['queue'] = [t for t in data['queue'] if t['id'] != ticket['id']]
                log(f"{owner}: the canonical copy changed; restarting this waiter with it, place kept")
                reexec(self.cfg, since)
            time.sleep(self.cfg['poll_seconds'])
        if grant.get('starting'):
            self._start(pool, grant)
        return grant

    def _start(self, pool, grant):
        problem = self.kind(pool['name']).start(grant['key'], float(pool['start_timeout']))
        with state() as data:
            current = next((g for g in data['grants'] if g['id'] == grant['id']), None)
            name = f"{pool['name']}/{grant['key']}"
            if problem:
                data['grants'] = [g for g in data['grants'] if g['id'] != grant['id']]
                failures = data['cooldown'].get(name, {}).get('failures', 0) + 1
                pause = min(60 * 2 ** (failures - 1), 900)
                data['cooldown'][name] = {'failures': failures, 'until': time.time() + pause, 'why': problem}
                self.kind(pool['name']).stop(grant['key'], 'failed to start')
                log(f"{name}: {problem}; slot freed, no new start for {pause} s")
            else:
                data['cooldown'].pop(name, None)
                if current:
                    current.pop('starting', None)
        if problem:
            raise SlotError(f"{name}: {problem}. It is stopped and the slot is free; it may be tried again "
                            f"in {pause} s.", 70)
        grant.pop('starting', None)

    @staticmethod
    def check_cooldown(data, pool_name, key):
        entry = data['cooldown'].get(f"{pool_name}/{key}")
        if key and entry and entry['until'] > time.time():
            until = datetime.datetime.fromtimestamp(entry['until'])
            raise SlotError(f"{pool_name}/{key} failed to start {entry['failures']} time(s) in a row "
                            f"({entry['why']}); next try after {until:%H:%M:%S}. Use another key or wait.", 75)

    def explain(self, pool, data, ticket, allowed, why, readings):
        grants = [g for g in data['grants'] if g['pool'] == pool['name']]
        held = sum(g.get('units', 1) for g in grants)
        parts = [f"{held}/{allowed} units held" + (f" (capacity {pool['capacity']} lowered: {why})" if why else '')]
        if ticket.get('key') and any(g.get('key') == ticket['key'] for g in grants):
            parts.append(f"{ticket['key']} is held by "
                         f"{next(g['owner'] for g in grants if g.get('key') == ticket['key'])}")
        ahead = [t['owner'] for t in data['queue'] if t['pool'] == pool['name'] and t['since'] < ticket['since']]
        if ahead:
            parts.append(f"{len(ahead)} ahead ({', '.join(ahead[:5])})")
        if grants:
            parts.append('holders: ' + ', '.join(g['owner'] for g in grants))
        return '; '.join(parts)

    # -- giving back --------------------------------------------------------------------------------------------

    def release_grant(self, grant_id, finished=False):
        with state() as data:
            grant = next((g for g in data['grants'] if g['id'] == grant_id), None)
            if grant and finished:
                # Run times teach the adaptive strategy which waiters are short.
                runs = data['durations'].setdefault(strategies.signature(grant.get('what', '')), [])
                runs[:] = (runs + [round(time.time() - grant['since'], 1)])[-5:]
                if len(data['durations']) > 200:
                    data['durations'].pop(next(iter(data['durations'])))
            if grant:
                data['grants'].remove(grant)
                log(f"{grant['pool']}: slot released by {grant['owner']}")
                self.stop_unused(data, [(grant['pool'], grant.get('key'))], 'released')

    def release(self, owner, pool=None, key=None):
        """Gives back an owner's leases (slots held by `hold`). Slots of a running `run` go back when it ends."""
        freed, waiting = [], []
        with state() as data:
            for grant in list(data['grants']):
                if grant['owner'] != owner or (pool and grant['pool'] != pool) or (key and grant.get('key') != key):
                    continue
                if grant.get('pid') and system.alive(grant['pid'], grant.get('token')):
                    continue
                if system.alive(grant.get('starting')):
                    waiting.append(grant)
                    continue
                data['grants'].remove(grant)
                freed.append((grant['pool'], grant.get('key')))
                log(f"{grant['pool']}: slot released by {owner}")
            self.stop_unused(data, freed, 'released')
        return freed, waiting

    def renew(self, owner, minutes, pool=None):
        renewed = []
        with state() as data:
            for grant in data['grants']:
                if (grant['owner'] == owner and not grant.get('pid') and (not pool or grant['pool'] == pool)
                        and grant.get('lease_until', 0) > time.time()):
                    limit = float(self.cfg['pools'].get(grant['pool'], config.POOL_DEFAULTS)['max_lease_minutes'])
                    grant['lease_until'] = time.time() + min(minutes, limit) * 60
                    renewed.append(grant)
        return renewed

    def set_child(self, grant_id, pid):
        with state() as data:
            for grant in data['grants']:
                if grant['id'] == grant_id:
                    grant['child'] = pid
                    grant['child_token'] = system.start_token(pid)

    # -- running a command in a slot ----------------------------------------------------------------------------

    def run(self, pool_name, owner, command, key=None, units=1, exclusive=False, priority=0, timeout=None):
        pool = self.pool(pool_name)
        grant = self.acquire(pool_name, owner, key=key, units=units, exclusive=exclusive, priority=priority,
                             what=' '.join(command)[:160], holder_pid=os.getpid())
        key = grant.get('key') or ''
        env = dict(os.environ, SLOTS_POOL=pool_name, SLOTS_KEY=key, SLOTS_OWNER=owner)
        env.update({name: str(value).replace('{key}', key) for name, value in pool['env'].items()})
        env.pop('SLOTS_REEXEC', None)
        argv = [part.replace('{key}', key) for part in command]
        limit = float(timeout or pool['run_timeout'])
        process = None
        finished = False
        try:
            process = subprocess.Popen(argv, env=env, start_new_session=True)
            self.set_child(grant['id'], process.pid)
            try:
                code = process.wait(timeout=limit)
                finished = True
                return code
            except subprocess.TimeoutExpired:
                log(f"{pool_name}: {owner}'s command stopped after {int(limit)} s (hung?); slot freed")
                return 124
        finally:
            if process and process.poll() is None:
                resources.stop_group(process.pid)
            self.release_grant(grant['id'], finished)

    def hold(self, pool_name, owner, key=None, minutes=None):
        pool = self.pool(pool_name)
        minutes = min(float(minutes or pool['lease_minutes']), float(pool['max_lease_minutes']))
        with state() as data:
            for grant in data['grants']:
                if (grant['owner'] == owner and grant['pool'] == pool_name and not grant.get('pid')
                        and (not key or grant.get('key') == key) and grant.get('lease_until', 0) > time.time()):
                    grant['lease_until'] = time.time() + minutes * 60  # holding again renews the lease
                    return grant
        return self.acquire(pool_name, owner, key=key, lease_seconds=minutes * 60,
                            what=f'lease {minutes:g} min')

    # -- status -------------------------------------------------------------------------------------------------

    def status(self):
        with state() as data:
            now = time.time()
            self.stop_unused(data, self.tidy(data, now), 'its slot was freed')
            snapshot = json.loads(json.dumps(data))
        pools = []
        for name, pool in self.cfg['pools'].items():
            allowed, why, readings = self.allowed(pool)
            kind = self.kind(name)
            running = kind.running() if kind.lifecycle else None
            held = {g.get('key') for g in snapshot['grants'] if g['pool'] == name}
            self.plan(pool, snapshot, allowed, now)
            pools.append({
                'mode': self.mode,
                'name': name, 'type': pool['type'], 'strategy': pool['strategy'], 'capacity': pool['capacity'],
                'allowed': allowed, 'lowered_by': why, 'readings': readings,
                'grants': [dict(g, age=int(now - g['since']),
                                lease_left=int(g['lease_until'] - now) if g.get('lease_until') else None)
                           for g in snapshot['grants'] if g['pool'] == name],
                'queue': [dict(t, waited=int(now - t['since'])) for t in snapshot['queue'] if t['pool'] == name],
                'strays': sorted(running - held) if running is not None else [],
                'running': sorted(running) if running is not None else None,
                'names': kind.names() if kind.lifecycle else {},
            })
        cooldown = {k: v for k, v in snapshot['cooldown'].items() if v['until'] > now}
        return {'config': self.cfg['source'], 'pools': pools, 'cooldown': cooldown, 'readings': system.readings()}


# -- one canonical copy ---------------------------------------------------------------------------------------
# Agents copy and branch tools; old copies kept both simulator slots for hours (2026-11-03). With `canonical`
# set in the config, every other copy re-executes the canonical one, and a waiter re-executes when it changes.

def canonical_dir(cfg):
    return Path(os.path.expanduser(cfg['canonical'])).resolve() if cfg.get('canonical') else None


def canonical_version(cfg):
    folder = canonical_dir(cfg)
    try:
        return max(p.stat().st_mtime for p in folder.glob('*.py')) if folder else None
    except (OSError, ValueError):
        return None


def stale_copy(cfg):
    folder = canonical_dir(cfg)
    return bool(folder and (folder / 'core.py').exists() and folder != PACKAGE and not os.environ.get('SLOTS_REEXEC'))


def reexec(cfg, since=None):
    folder = canonical_dir(cfg)
    env = dict(os.environ, SLOTS_REEXEC='1')
    if since is not None:
        env['SLOTS_SINCE'] = str(since)
    sys.stdout.flush()
    sys.stderr.flush()
    # Not `-m`: that would find a copy in the current folder before the canonical one.
    boot = (f"import sys; sys.path.insert(0, {str(folder.parent)!r}); "
            f"from {folder.name}.cli import main; main()")
    os.execve(sys.executable, [sys.executable, '-c', boot] + sys.argv[1:], env)
