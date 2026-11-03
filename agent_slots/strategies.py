"""Who goes next. A strategy only orders the waiters of one pool; the planner in core.py then grants in that order,
skipping waiters it cannot serve yet, so one waiter for a busy key never blocks the rest (no head-of-line blocking).

Each strategy returns (ordered tickets, mode). The mode says which rule ordered them; `adaptive` picks its mode
from the situation, and status and the log show it with the reason."""
import os
import statistics


def fifo(tickets, grants, now, ctx):
    return sorted(tickets, key=lambda t: t['since']), 'fifo'


def priority(tickets, grants, now, ctx):
    """Higher --priority first; a waiter gains one step per `aging_minutes` waited, so low ones are not starved."""
    step = float(ctx['pool'].get('aging_minutes', 5)) * 60
    return sorted(tickets, key=lambda t: (-(t.get('priority', 0) + int((now - t['since']) // step)), t['since'])), \
        'priority'


def fair(tickets, grants, now, ctx):
    """Fair share per session: the owner using the least of this pool goes first, counting units held now plus
    slots granted within `fair_window_minutes`, so one chat that queues ten short test runs cannot take every
    slot in turn while another chat waits for one."""
    use = _held(grants)
    for owner, count in ctx.get('recent', {}).items():
        use[owner] = use.get(owner, 0) + count
    return sorted(tickets, key=lambda t: (use.get(t['owner'], 0), t['since'])), 'fair'


def adaptive(tickets, grants, now, ctx):
    """Switches between rules by what the queue looks like, checked on every poll:
    - fifo while every waiter fits in the free units (order does not matter then);
    - fair while one session holds, recently got or waits for more than half of the pool's work and others wait;
    - short-first while waiters outnumber the free units and run times are known from earlier runs
      (lowers the average wait); a waiter that waited `starve_minutes` goes first regardless;
    - fifo otherwise. Explicit --priority always comes first."""
    if len(tickets) <= ctx['free']:
        return _by_priority(fifo(tickets, grants, now, ctx)[0]), 'fifo: every waiter fits'
    work = _held(grants)
    for owner, count in ctx.get('recent', {}).items():
        work[owner] = work.get(owner, 0) + count
    for ticket in tickets:
        work[ticket['owner']] = work.get(ticket['owner'], 0) + ticket.get('units', 1)
    total = sum(work.values())
    owner, most = max(work.items(), key=lambda item: item[1])
    if total >= 3 and most * 2 > total and len({t['owner'] for t in tickets} - {owner}):
        return _by_priority(fair(tickets, grants, now, ctx)[0]), f'fair: {owner} has {most} of {total} units'
    expected = {t['id']: _expected(ctx['durations'], t) for t in tickets}
    known = [v for v in expected.values() if v is not None]
    if len(known) >= 2:
        middle = statistics.median(known)
        starve = float(ctx['pool'].get('starve_minutes', 10)) * 60
        ordered = sorted(tickets, key=lambda t: (now - t['since'] < starve,
                                                 expected[t['id']] if expected[t['id']] is not None else middle,
                                                 t['since']))
        return _by_priority(ordered), f'short-first: {len(tickets)} waiting for {ctx["free"]} free'
    return _by_priority(fifo(tickets, grants, now, ctx)[0]), 'fifo'


def _by_priority(ordered):
    return sorted(ordered, key=lambda t: -t.get('priority', 0))  # stable: keeps the order within a priority


def _held(grants):
    held = {}
    for grant in grants:
        held[grant['owner']] = held.get(grant['owner'], 0) + grant.get('units', 1)
    return held


def _expected(durations, ticket):
    runs = durations.get(signature(ticket.get('what', '')))
    return statistics.median(runs) if runs else None


def signature(what):
    """What kind of command this is, so run times can be learned: the program and its last plain word
    (`xcodebuild ... test-without-building`), without paths, options and settings that differ per session."""
    words = what.split()
    if not words:
        return ''
    plain = [w for w in words[1:] if not w.startswith(('-', '/')) and '=' not in w]
    return f"{os.path.basename(words[0])} {plain[-1] if plain else ''}".strip()


STRATEGIES = {'fifo': fifo, 'priority': priority, 'fair': fair, 'adaptive': adaptive}


def order(name, tickets, grants, now, ctx):
    if name not in STRATEGIES:
        raise SystemExit(f"slots: unknown strategy {name!r} (known: {', '.join(STRATEGIES)})")
    return STRATEGIES[name](tickets, grants, now, ctx)
