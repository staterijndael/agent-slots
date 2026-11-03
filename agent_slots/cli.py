"""slots: share scarce machine resources between parallel agent sessions.

  slots run POOL [--key K] [--units N | --exclusive] [--priority P] [--timeout S] -- COMMAND ...
        wait for a slot, start the instance if the pool manages one, run COMMAND, give the slot back.
        In COMMAND, {key} becomes the instance key; SLOTS_KEY, SLOTS_POOL, SLOTS_OWNER are in its environment.
  slots hold POOL [--key K] [--minutes M]    lease a slot for hands-on work; renew it or lose it
  slots renew [--pool POOL] [--minutes M]    extend your leases
  slots release [--pool POOL] [--key K]      give your leases back now
  slots status [--json]                      pools, holders, queue, strays, memory
  slots init [--force]                       write a starter config to SLOTS_HOME/config.json

--owner names your session (default: $SLOTS_OWNER, else the current folder's name). Exit codes: the command's
own code for run; 124 run timed out; 70 the instance failed to start; 75 the instance is cooling down after
failed starts; 2 usage or config errors.
"""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import config, core


def default_owner():
    return os.environ.get('SLOTS_OWNER') or Path.cwd().name


def parser():
    top = argparse.ArgumentParser(prog='slots', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest='command', required=True)

    def owned(name):
        p = sub.add_parser(name)
        p.add_argument('--owner', default=None)
        return p

    run = owned('run')
    run.add_argument('pool')
    run.add_argument('--key')
    run.add_argument('--units', type=int, default=1)
    run.add_argument('--exclusive', action='store_true', help='take every unit of the pool')
    run.add_argument('--priority', type=int, default=0)
    run.add_argument('--timeout', type=float, help='seconds; default: the pool run_timeout')
    hold = owned('hold')
    hold.add_argument('pool')
    hold.add_argument('--key')
    hold.add_argument('--minutes', type=float)
    renew = owned('renew')
    renew.add_argument('--pool')
    renew.add_argument('--minutes', type=float, default=10)
    release = owned('release')
    release.add_argument('--pool')
    release.add_argument('--key')
    status = sub.add_parser('status')
    status.add_argument('--json', action='store_true')
    init = sub.add_parser('init')
    init.add_argument('--force', action='store_true')
    return top


def main(argv=None):
    for number, code in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
        signal.signal(number, lambda *_, code=code: sys.exit(code))
    argv = sys.argv[1:] if argv is None else argv
    cfg = config.load()
    if core.stale_copy(cfg):
        core.reexec(cfg)  # a copy in an agent's branch may be old; the canonical copy is the one rule
    rest = argv[argv.index('--') + 1:] if '--' in argv else []
    args = parser().parse_args(argv[:argv.index('--')] if '--' in argv else argv)
    if hasattr(args, 'owner'):
        args.owner = args.owner or default_owner()
    if args.command == 'run' and not rest:
        parser().error('run needs a command after --')
    try:
        sys.exit(dispatch(args, rest))
    except core.SlotError as error:
        print(f"slots: {error}", file=sys.stderr)
        sys.exit(error.code)
    except KeyboardInterrupt:
        sys.exit(130)


def dispatch(args, rest):
    if args.command == 'init':
        return cmd_init(args)
    scheduler = core.Scheduler()
    if args.command == 'run':
        return scheduler.run(args.pool, args.owner, rest, key=args.key, units=args.units, exclusive=args.exclusive,
                             priority=args.priority, timeout=args.timeout)
    if args.command == 'hold':
        grant = scheduler.hold(args.pool, args.owner, key=args.key, minutes=args.minutes)
        until = time.strftime('%H:%M', time.localtime(grant['lease_until']))
        print(f"{args.pool}: slot held by {args.owner}{' on ' + grant['key'] if grant.get('key') else ''} until "
              f"{until}. Renew: slots renew --owner {args.owner}. Done: slots release --owner {args.owner}")
        if grant.get('key'):
            print(f"SLOTS_KEY={grant['key']}")
        return 0
    if args.command == 'renew':
        if not scheduler.renew(args.owner, args.minutes, args.pool):
            print(f"slots: {args.owner} holds no live lease (it ran out or was released); hold again", file=sys.stderr)
            return 1
        return 0
    if args.command == 'release':
        freed, starting = scheduler.release(args.owner, args.pool, args.key)
        for grant in starting:
            print(f"slots: {grant['pool']} {grant.get('key') or ''} is still starting; release it after hold "
                  f"returns (stopping an instance mid-start crash-loops it)", file=sys.stderr)
        return 0
    if args.command == 'status':
        report = scheduler.status()
        print(json.dumps(report, indent=1) if args.json else render(report))
        return 0
    raise AssertionError(args.command)


def render(report):
    r = report['readings']
    lines = [f"memory {r['free']}% free, swap {r['swap']}% used, load {r['load']}/core; config: {report['config']}"]
    for pool in report['pools']:
        held = sum(g.get('units', 1) for g in pool['grants'])
        lowered = f", lowered by {pool['lowered_by']}" if pool['lowered_by'] else ''
        strategy = pool['strategy'] + (f" now {pool['mode']}" if pool['mode'] and pool['strategy'] == 'adaptive' else '')
        lines.append(f"{pool['name']} ({pool['type']}, {strategy}): {held}/{pool['allowed']} held, "
                     f"capacity {pool['capacity']}{lowered}")
        names = pool['names']
        for grant in pool['grants']:
            how = (f"lease {grant['lease_left']} s left" if grant.get('lease_left') is not None
                   else f"pid {grant.get('pid')}")
            state = ', starting' if grant.get('starting') else ''
            key = f" on {names.get(grant['key'], grant['key'])}" if grant.get('key') else ''
            lines.append(f"  held: {grant['owner']}{key}, {grant['units']} unit(s), {grant['age']} s, {how}{state}: "
                         f"{grant.get('what', '')}")
        for ticket in pool['queue']:
            key = f" for {names.get(ticket['key'], ticket['key'])}" if ticket.get('key') else ''
            lines.append(f"  waiting: {ticket['owner']}{key}, {ticket['waited']} s"
                         f"{', priority ' + str(ticket['priority']) if ticket.get('priority') else ''}")
        if pool['strays']:
            lines.append(f"  running without a slot: {', '.join(names.get(k, k) for k in pool['strays'])}")
        if pool['running'] is not None:
            # Instances are stopped when their slot is freed, so "none running" with nobody waiting is normal.
            running = ', '.join(names.get(k, k) for k in pool['running']) or 'none'
            lines.append(f"  instances: {len(names)} known, running now: {running}")
    for name, entry in report['cooldown'].items():
        lines.append(f"cooling down: {name} after {entry['failures']} failed start(s): {entry['why']}")
    return '\n'.join(lines)


def cmd_init(args):
    target = config.path()
    if target.exists() and not args.force:
        print(f"slots: {target} exists; --force to overwrite", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    starter = dict(config.DEFAULT_CONFIG, canonical=str(core.PACKAGE))
    target.write_text(json.dumps(starter, indent=2) + '\n')
    print(f"wrote {target}; edit pools there (examples: {core.PACKAGE.parent / 'examples'})")
    return 0
