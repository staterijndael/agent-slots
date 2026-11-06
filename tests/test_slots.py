#!/usr/bin/env python3
"""Self-test against a fake simctl and fake memory readings: python3 -m unittest discover tests

Each case is a real incident from the prototype (relay_sim.py, 2026-10-30..11-03) or a strategy rule."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).with_name('fake_simctl.py')
SIM_LIMITS = [{'when': {'free_below': 10}, 'max': 0}, {'when': {'free_below': 20}, 'max': 1},
              {'when': {'swap_above': 90, 'free_below': 25}, 'max': 1}, {'when': {'load_above': 3}, 'max': 1}]


def udid(letter):
    return f"0000000{letter}-AGEN-TSLO-TEST-00000000000{letter}"


def sleep(seconds):
    return [sys.executable, '-c', f'import time; time.sleep({seconds})']


class Base(unittest.TestCase):
    pools = {'sim': {'type': 'simctl', 'capacity': 2, 'limits': SIM_LIMITS},
             'build': {'type': 'token', 'capacity': 2}}

    def setUp(self):
        warnings.simplefilter('ignore', ResourceWarning)
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.sims = self.root / 'sims.json'
        self.env = dict(os.environ, SLOTS_HOME=str(self.root / 'home'), SLOTS_CONFIG=str(self.root / 'config.json'),
                        SLOTS_SIMCTL=f"{sys.executable} {FAKE} {self.sims}", SLOTS_POLL='0.05',
                        SLOTS_CRASH_DIR=str(self.root / 'crashes'), SLOTS_FAKE_FREE='80', SLOTS_FAKE_SWAP='0',
                        SLOTS_FAKE_LOAD='0', PYTHONPATH=str(REPO))
        for name in ('SLOTS_OWNER', 'SLOTS_REEXEC', 'SLOTS_SINCE', 'SLOTS_CANONICAL'):
            self.env.pop(name, None)
        self.configure(self.pools)
        self.jobs = []

    def tearDown(self):
        for job in self.jobs:
            if job.poll() is None:
                job.kill()
                job.wait()
        self.dir.cleanup()

    def configure(self, pools, **extra):
        (self.root / 'config.json').write_text(json.dumps(dict({'pools': pools}, **extra)))

    def start(self, *args, env=None, **overrides):
        env = dict(env or self.env, **overrides)
        job = subprocess.Popen([sys.executable, '-m', 'agent_slots', *args], env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
        self.jobs.append(job)
        return job

    def ok(self, job, timeout=20):
        out, err = job.communicate(timeout=timeout)
        self.assertEqual(job.returncode, 0, err)
        return out

    def wait_entered(self, count, timeout=10):
        """Waits until `count` requests have queued or been granted, so a slow machine keeps the arrival order."""
        path = self.root / 'home' / 'state.json'
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = json.loads(path.read_text())
                log = (self.root / 'home' / 'log.txt').read_text() if (self.root / 'home' / 'log.txt').exists() else ''
                if len(data.get('queue', [])) + log.count('slot -> ') >= count:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        self.fail(f'{count} requests did not queue in {timeout} s')

    def sim(self):
        return json.loads(self.sims.read_text()) if self.sims.exists() else {'booted': [], 'max': 0, 'events': []}

    def events(self):
        return [e[:2] for e in self.sim()['events']]

    def status(self):
        return json.loads(self.ok(self.start('status', '--json')))

    def pool_status(self, name):
        return next(p for p in self.status()['pools'] if p['name'] == name)


class Simulators(Base):
    def test_two_slots_stay_busy_never_three_boot_first_come_first_served(self):
        jobs = []
        for n, letter in enumerate('ABCDE'):
            jobs.append(self.start('run', 'sim', '--owner', f'chat-{letter}', '--key', udid(letter), '--', *sleep(0.6)))
            self.wait_entered(n + 1)  # arrival order is queue order, not process start order
        started = time.time()
        for job in jobs:
            self.ok(job, 30)
        sim = self.sim()
        self.assertEqual(sim['max'], 2, 'never more than two booted')
        self.assertEqual(sim['booted'], [], 'every simulator is shut down when its work ends')
        # Grant order is the queue order; boots of two slots granted together may start in either order.
        log = (self.root / 'home' / 'log.txt').read_text()
        granted = [line.split('slot -> ')[1].split(' ')[0] for line in log.splitlines() if 'slot -> ' in line]
        # Two slots freed together are planned for the next two waiters at once; either may log its grant first.
        self.assertEqual([set(granted[0:2]), set(granted[2:4]), granted[4]],
                         [{'chat-A', 'chat-B'}, {'chat-C', 'chat-D'}, 'chat-E'], 'first come, first served')
        self.assertEqual(sorted(e[1] for e in self.events() if e[0] == 'boot'), [udid(x) for x in 'ABCDE'])
        self.assertLess(time.time() - started, 4.0, 'three rounds on two slots; idle slots would take longer')

    def test_stray_is_shut_down_when_room_is_needed(self):
        self.sims.write_text(json.dumps({'booted': [udid('S')], 'max': 1, 'events': []}))
        first = self.start('run', 'sim', '--owner', 'a', '--key', udid('A'), '--', *sleep(0.6))
        time.sleep(0.3)
        second = self.start('run', 'sim', '--owner', 'b', '--key', udid('B'), '--', *sleep(0.2))
        self.ok(first)
        self.ok(second)
        self.assertIn(['shutdown', udid('S')], self.events())
        self.assertEqual(self.sim()['max'], 2)

    def test_lease_runs_out_and_the_slot_passes_on(self):
        self.configure({'sim': {'type': 'simctl', 'capacity': 1}})
        self.assertIn('slot held by live', self.ok(self.start('hold', 'sim', '--owner', 'live', '--key', udid('L'),
                                                              '--minutes', '0.02')))
        self.ok(self.start('run', 'sim', '--owner', 'tests', '--key', udid('T'), '--', sys.executable, '-c', 'pass'))
        events = self.events()
        self.assertLess(events.index(['shutdown', udid('L')]), events.index(['boot', udid('T')]))

    def test_renew_keeps_and_release_frees_a_lease(self):
        self.configure({'sim': {'type': 'simctl', 'capacity': 1}})
        self.ok(self.start('hold', 'sim', '--owner', 'live', '--key', udid('L'), '--minutes', '0.05'))
        waiter = self.start('run', 'sim', '--owner', 'tests', '--key', udid('T'), '--', sys.executable, '-c', 'pass')
        for _ in range(4):
            time.sleep(0.5)
            self.ok(self.start('renew', '--owner', 'live', '--minutes', '0.05'))
        self.assertIsNone(waiter.poll(), 'a renewed lease keeps its slot')
        self.ok(self.start('release', '--owner', 'live'))
        self.ok(waiter)
        self.assertEqual(self.start('renew', '--owner', 'live').wait(10), 1, 'renewing after release fails')

    def test_holding_again_renews_instead_of_queueing_behind_itself(self):
        self.ok(self.start('hold', 'sim', '--owner', 'live', '--key', udid('L'), '--minutes', '1'))
        self.ok(self.start('hold', 'sim', '--owner', 'live', '--key', udid('L'), '--minutes', '5'), timeout=5)
        grants = self.pool_status('sim')['grants']
        self.assertEqual(len(grants), 1)
        self.assertGreater(grants[0]['lease_left'], 200)

    def test_killed_holder_frees_its_slot_and_its_orphaned_command_is_stopped(self):
        self.configure({'sim': {'type': 'simctl', 'capacity': 1}})
        marker = self.root / 'orphan-alive'
        holder = self.start('run', 'sim', '--owner', 'gone', '--key', udid('G'), '--', sys.executable, '-c',
                            f"import time, pathlib\nwhile True: pathlib.Path({str(marker)!r}).touch(); time.sleep(0.1)")
        time.sleep(1.0)
        waiter = self.start('run', 'sim', '--owner', 'next', '--key', udid('N'), '--', sys.executable, '-c', 'pass')
        holder.kill()
        holder.wait()
        self.ok(waiter)
        time.sleep(0.5)
        touched = marker.stat().st_mtime
        time.sleep(0.5)
        self.assertEqual(marker.stat().st_mtime, touched, 'the orphaned command was stopped')

    def test_release_during_start_waits_for_the_start(self):
        env = dict(self.env, FAKE_BOOTSTATUS_SLEEP='1.0')
        hold = self.start('hold', 'sim', '--owner', 'live', '--key', udid('L'), env=env)
        time.sleep(0.5)
        early = self.start('release', '--owner', 'live', env=env)
        self.assertIn('still starting', early.communicate(timeout=10)[1])
        self.assertNotIn(['shutdown', udid('L')], self.events(), 'never shut down mid-start')
        self.ok(hold)
        self.ok(self.start('release', '--owner', 'live'))
        self.assertIn(['shutdown', udid('L')], self.events())

    def test_boot_that_never_finishes_is_stopped_and_cools_down(self):
        self.configure({'sim': {'type': 'simctl', 'capacity': 2, 'start_timeout': 0.5}})
        hold = self.start('hold', 'sim', '--owner', 'live', '--key', udid('L'), FAKE_BOOTSTATUS_SLEEP='5')
        hold.communicate(timeout=20)
        self.assertEqual(hold.returncode, 70)
        self.assertEqual(self.sim()['booted'], [])
        self.assertEqual(self.pool_status('sim')['grants'], [])
        again = self.start('hold', 'sim', '--owner', 'live', '--key', udid('L'))
        self.assertIn('next try after', again.communicate(timeout=10)[1])
        self.assertEqual(again.returncode, 75, 'a failed instance is not booted again at once')
        self.assertIn(f'sim/{udid("L")}', self.status()['cooldown'])

    def test_springboard_crash_stops_the_boot_at_once(self):
        crashes = self.root / 'crashes'
        crashes.mkdir()
        hold = self.start('hold', 'sim', '--owner', 'live', '--key', udid('C'), FAKE_BOOTSTATUS_SLEEP='30')
        started = time.time()
        for n in range(40):
            time.sleep(0.25)
            (crashes / f'SpringBoard-{n}.ips').write_text('{"coalition":"SimDevice.%s"}' % udid('C'))
            if hold.poll() is not None:
                break
        hold.communicate(timeout=20)
        self.assertEqual(hold.returncode, 70)
        self.assertLess(time.time() - started, 10, 'stops at the first crash, not after the timeout')
        self.assertEqual(self.sim()['booted'], [])
        subprocess.run(['pkill', '-f', 'bootstatus'])

    def test_hung_run_is_stopped_and_frees_its_slot(self):
        self.configure({'sim': {'type': 'simctl', 'capacity': 1, 'run_timeout': 1}})
        hung = self.start('run', 'sim', '--owner', 'hung', '--key', udid('H'), '--', *sleep(60))
        time.sleep(0.3)
        nxt = self.start('run', 'sim', '--owner', 'next', '--key', udid('N'), '--', sys.executable, '-c', 'pass')
        self.assertEqual(hung.wait(timeout=20), 124)
        self.ok(nxt)

    def test_stuck_holder_whose_instance_is_gone_loses_its_slot(self):
        self.configure({'sim': {'type': 'simctl', 'capacity': 1, 'stale_seconds': 1}})
        fake = self.env['SLOTS_SIMCTL'].split()
        # holds the slot, then its simulator is shut down under it (a crash, a person, another tool)
        stuck = self.start('run', 'sim', '--owner', 'stuck', '--key', udid('S'), '--', *sleep(30))
        time.sleep(0.6)
        subprocess.run([*fake, 'shutdown', udid('S')])
        nxt = self.start('run', 'sim', '--owner', 'next', '--key', udid('N'), '--', sys.executable, '-c', 'pass')
        self.ok(nxt, 20)
        self.assertIsNone(stuck.poll(), 'the slot was taken back without waiting for the stuck holder')

    def test_same_key_waits_but_does_not_block_others(self):
        self.ok(self.start('hold', 'sim', '--owner', 'a', '--key', udid('A'), '--minutes', '1'))
        blocked = self.start('run', 'sim', '--owner', 'b', '--key', udid('A'), '--', sys.executable, '-c', 'pass')
        time.sleep(0.3)
        later = self.start('run', 'sim', '--owner', 'c', '--key', udid('C'), '--', sys.executable, '-c', 'pass')
        self.ok(later, 10)
        self.assertIsNone(blocked.poll(), 'the waiter for a held key keeps waiting')
        self.ok(self.start('release', '--owner', 'a'))
        self.ok(blocked)

    def test_status_reports_holders_queue_and_strays(self):
        self.sims.write_text(json.dumps({'booted': [udid('S')], 'known': [udid('S')], 'max': 1, 'events': []}))
        self.ok(self.start('hold', 'sim', '--owner', 'live', '--key', udid('L')))
        text = self.ok(self.start('status'))
        self.assertIn('sim (simctl, fifo): 1/2 held', text)
        self.assertIn('held: live on sim L', text)
        self.assertIn('running without a slot: sim S', text)
        self.assertIn('instances: 2 known, running now: sim L, sim S', text)


class Memory(Base):
    def test_low_memory_runs_one_simulator_not_none(self):
        env = dict(self.env, SLOTS_FAKE_FREE='15', SLOTS_FAKE_SWAP='95')
        jobs = [self.start('run', 'sim', '--owner', f'c{x}', '--key', udid(x), '--', *sleep(0.4), env=env)
                for x in 'ABC']
        for job in jobs:
            self.ok(job, 30)
        self.assertEqual(self.sim()['max'], 1)

    def test_load_alone_lowers_to_one_never_zero(self):
        self.assertEqual(self.pool_status('sim')['allowed'], 2)
        busy = dict(self.env, SLOTS_FAKE_LOAD='6.5')
        report = json.loads(self.ok(self.start('status', '--json', env=busy)))
        sim = next(p for p in report['pools'] if p['name'] == 'sim')
        self.assertEqual(sim['allowed'], 1)
        self.assertIn('load 6.5/core', sim['lowered_by'])

    def test_memory_is_checked_before_a_grant_and_never_revokes_a_holder(self):
        env = dict(self.env, SLOTS_FAKE_FREE='50')
        self.ok(self.start('hold', 'sim', '--owner', 'early', '--key', udid('E'), '--minutes', '1', env=env))
        low = dict(self.env, SLOTS_FAKE_FREE='5')
        waiter = self.start('run', 'sim', '--owner', 'late', '--key', udid('W'), '--', sys.executable, '-c', 'pass',
                            env=low)
        time.sleep(1.0)
        self.assertIsNone(waiter.poll(), 'no grant while under 10% free')
        report = json.loads(self.ok(self.start('status', '--json', env=low)))
        sim = next(p for p in report['pools'] if p['name'] == 'sim')
        self.assertEqual([g['owner'] for g in sim['grants']], ['early'], 'the holder keeps its slot')
        self.assertEqual(sim['allowed'], 0)


class Builds(Base):
    def test_two_compiles_at_once_and_the_third_waits(self):
        started = time.time()
        jobs = [self.start('run', 'build', '--owner', f'b{n}', '--', *sleep(0.6)) for n in range(3)]
        for job in jobs:
            self.ok(job)
        self.assertGreaterEqual(time.time() - started, 1.2, 'the third compile waits')
        self.assertLess(time.time() - started, 2.6, 'two compiles run side by side')

    def test_compiling_does_not_hold_a_simulator(self):
        build = self.start('run', 'build', '--owner', 'b', '--', *sleep(1.5))
        time.sleep(0.3)
        started = time.time()
        self.ok(self.start('run', 'sim', '--owner', 't', '--key', udid('A'), '--', sys.executable, '-c', 'pass'))
        self.assertLess(time.time() - started, 1.2, 'a simulator is free while someone compiles')
        self.assertIsNone(build.poll())
        self.ok(build)

    def test_exclusive_build_runs_alone(self):
        exclusive = self.start('run', 'build', '--owner', 'bazel', '--exclusive', '--', *sleep(0.8))
        time.sleep(0.3)
        small = self.start('run', 'build', '--owner', 'small', '--', sys.executable, '-c',
                           'import time; print(time.time())')
        out = self.ok(small)
        self.ok(exclusive)
        log = (self.root / 'home' / 'log.txt').read_text()
        self.assertLess(log.index('slot released by bazel'), log.index('slot -> small'))
        self.assertTrue(out.strip())

    def test_big_waiter_is_backfilled_then_reserved(self):
        self.configure({'build': {'type': 'token', 'capacity': 2, 'backfill_seconds': 1.0}})
        first = self.start('run', 'build', '--owner', 'one', '--', *sleep(3))
        time.sleep(0.4)
        big = self.start('run', 'build', '--owner', 'bazel', '--exclusive', '--', sys.executable, '-c', 'pass')
        time.sleep(0.3)
        quick = self.start('run', 'build', '--owner', 'quick', '--', sys.executable, '-c', 'pass')
        self.ok(quick, 5)  # backfilled: fits in the free unit while the big one is young
        time.sleep(1.2)
        late = self.start('run', 'build', '--owner', 'late', '--', sys.executable, '-c', 'pass')
        time.sleep(0.8)
        self.assertIsNone(late.poll(), 'once the big waiter is old enough, its room is kept')
        self.ok(first)
        self.ok(big)
        self.ok(late)
        log = (self.root / 'home' / 'log.txt').read_text()
        self.assertLess(log.index('slot -> bazel'), log.index('slot -> late'))


    def test_big_waiter_keeps_its_room_while_a_limit_lowers_the_pool(self):
        self.configure({'build': {'type': 'token', 'capacity': 2, 'backfill_seconds': 0.5,
                                  'limits': [{'when': {'load_above': 2}, 'max': 1}]}})
        busy = dict(self.env, SLOTS_FAKE_LOAD='5')
        first = self.start('run', 'build', '--owner', 'one', '--', *sleep(1.5), env=busy)
        time.sleep(0.4)
        big = self.start('run', 'build', '--owner', 'bazel', '--exclusive', '--', sys.executable, '-c', 'pass',
                         env=busy)
        time.sleep(0.8)
        small = self.start('run', 'build', '--owner', 'small', '--', sys.executable, '-c', 'pass', env=busy)
        self.ok(first)
        time.sleep(0.6)
        self.assertIsNone(small.poll(), 'small compiles do not pass a big one that waited long enough')
        self.assertIsNone(big.poll(), 'the big one still waits for the load to drop')
        for job in (big, small):
            job.kill()
            job.wait()


class Strategies(Base):
    def blocker(self):
        self.ok(self.start('hold', 'gpu', '--owner', 'blocker', '--minutes', '1'))

    def order(self):
        log = (self.root / 'home' / 'log.txt').read_text().splitlines()
        return [line.split('slot -> ')[1].split(' ')[0] for line in log if 'slot -> ' in line and 'blocker' not in line]

    def queue(self, *entries):
        jobs = []
        for owner, extra in entries:
            jobs.append(self.start('run', 'gpu', '--owner', owner, *extra, '--', sys.executable, '-c', 'pass'))
            time.sleep(0.15)
        self.ok(self.start('release', '--owner', 'blocker'))
        for job in jobs:
            self.ok(job)

    def test_priority_goes_first(self):
        self.configure({'gpu': {'type': 'token', 'capacity': 1, 'strategy': 'priority'}})
        self.blocker()
        self.queue(('low', []), ('high', ['--priority', '5']), ('mid', ['--priority', '1']))
        self.assertEqual(self.order(), ['high', 'mid', 'low'])

    def test_fair_share_serves_the_session_holding_least(self):
        self.configure({'gpu': {'type': 'token', 'capacity': 2, 'strategy': 'fair'}})
        self.ok(self.start('hold', 'gpu', '--owner', 'greedy', '--minutes', '1'))
        self.ok(self.start('hold', 'gpu', '--owner', 'blocker', '--minutes', '1'))
        jobs = [self.start('run', 'gpu', '--owner', 'greedy', '--', sys.executable, '-c', 'pass')]
        time.sleep(0.15)
        jobs.append(self.start('run', 'gpu', '--owner', 'polite', '--', sys.executable, '-c', 'pass'))
        time.sleep(0.15)
        self.ok(self.start('release', '--owner', 'blocker'))
        jobs[1].communicate(timeout=10)
        self.assertIsNone(jobs[0].poll(), 'greedy already holds a unit, so polite goes first though it came later')
        self.ok(self.start('release', '--owner', 'greedy'))
        self.ok(jobs[0])

    def test_priority_ages_so_low_waiters_are_not_starved(self):
        self.configure({'gpu': {'type': 'token', 'capacity': 1, 'strategy': 'priority', 'aging_minutes': 0.01}})
        self.blocker()
        old = self.start('run', 'gpu', '--owner', 'old-low', '--', sys.executable, '-c', 'pass')
        time.sleep(1.6)  # two steps of 0.6 s
        new = self.start('run', 'gpu', '--owner', 'new-high', '--priority', '1', '--', sys.executable, '-c', 'pass')
        time.sleep(0.2)
        self.ok(self.start('release', '--owner', 'blocker'))
        self.ok(old)
        self.ok(new)
        self.assertEqual(self.order(), ['old-low', 'new-high'], 'two steps of waiting outrank +1')

    def test_adaptive_turns_fair_when_one_session_floods_the_queue(self):
        self.configure({'gpu': {'type': 'token', 'capacity': 1, 'strategy': 'adaptive'}})
        self.blocker()
        self.queue(('greedy', []), ('greedy', []), ('greedy', []), ('polite', []))
        self.assertEqual(self.order(), ['greedy', 'polite', 'greedy', 'greedy'])
        self.assertIn('switched fifo -> fair: greedy has', (self.root / 'home' / 'log.txt').read_text())

    def test_adaptive_runs_short_jobs_first_once_it_knows_run_times(self):
        self.configure({'gpu': {'type': 'token', 'capacity': 1, 'strategy': 'adaptive'}})
        long, short = sleep(0.8), sleep(0.01)
        self.ok(self.start('run', 'gpu', '--owner', 'learn', '--', *long))
        self.ok(self.start('run', 'gpu', '--owner', 'learn', '--', *short))
        self.blocker()
        jobs = []
        for owner, command in (('a', long), ('b', short), ('c', long), ('d', short)):
            jobs.append(self.start('run', 'gpu', '--owner', owner, '--', *command))
            time.sleep(0.15)
        self.assertIn('now short-first', self.ok(self.start('status')))
        self.ok(self.start('release', '--owner', 'blocker'))
        for job in jobs:
            self.ok(job)
        self.assertEqual(self.order()[2:], ['b', 'd', 'a', 'c'])

    def test_adaptive_short_first_still_serves_a_starved_waiter(self):
        self.configure({'gpu': {'type': 'token', 'capacity': 1, 'strategy': 'adaptive', 'starve_minutes': 0.01}})
        long, short = sleep(0.5), sleep(0.01)
        self.ok(self.start('run', 'gpu', '--owner', 'learn', '--', *long))
        self.ok(self.start('run', 'gpu', '--owner', 'learn', '--', *short))
        self.blocker()
        jobs = [self.start('run', 'gpu', '--owner', 'a', '--', *long)]
        time.sleep(0.8)
        jobs.append(self.start('run', 'gpu', '--owner', 'b', '--', *short))
        time.sleep(0.3)
        self.ok(self.start('release', '--owner', 'blocker'))
        for job in jobs:
            self.ok(job)
        self.assertEqual(self.order()[2:], ['a', 'b'])

    def test_keys_are_picked_and_passed_to_the_command(self):
        self.configure({'gpu': {'type': 'token', 'capacity': 2, 'keys': ['0', '1'],
                                'env': {'CUDA_VISIBLE_DEVICES': '{key}'}}})
        self.ok(self.start('hold', 'gpu', '--owner', 'a', '--minutes', '1'))
        out = self.ok(self.start('run', 'gpu', '--owner', 'b', '--', 'sh', '-c',
                                 'echo "$CUDA_VISIBLE_DEVICES $SLOTS_KEY {key}"'))
        self.assertEqual(out.strip(), '1 1 1')


class Processes(Base):
    def test_process_pool_starts_waits_for_ready_and_stops(self):
        ready = self.root / 'ready-{key}'
        server = [sys.executable, '-c', f"import pathlib, time; time.sleep(0.3); "
                                        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"]
        self.configure({'emu': {'type': 'process', 'capacity': 1, 'start': server,
                                'ready': ['test', '-e', str(ready)], 'start_timeout': 10}})
        out = self.ok(self.start('run', 'emu', '--owner', 'a', '--key', 'pixel', '--', 'sh', '-c',
                                 f'test -e {self.root}/ready-pixel && echo up'))
        self.assertEqual(out.strip(), 'up', 'the command runs once the instance is ready')
        self.assertEqual(self.pool_status('emu')['strays'], [], 'the instance is stopped after release')

    def test_process_that_dies_while_starting_fails_cleanly(self):
        self.configure({'emu': {'type': 'process', 'capacity': 1, 'start': ['false'], 'ready': ['false']}})
        job = self.start('run', 'emu', '--owner', 'a', '--key', 'bad', '--', 'true')
        self.assertIn('exited with 1 while starting', job.communicate(timeout=10)[1])
        self.assertEqual(job.returncode, 70)

    def test_plugin_type_from_a_file(self):
        plugin = self.root / 'browser.py'
        plugin.write_text(
            'from agent_slots.resources import ResourceType\n'
            'import pathlib\n'
            'class Browser(ResourceType):\n'
            '    lifecycle = True\n'
            '    def start(self, key, timeout):\n'
            '        (self.home / f"up-{key}").touch()\n'
            '    def stop(self, key, reason):\n'
            '        (self.home / f"up-{key}").unlink()\n'
            '    def running(self):\n'
            '        return {p.name[3:] for p in self.home.glob("up-*")}\n')
        self.configure({'browser': {'type': f'{plugin}:Browser', 'capacity': 1}})
        out = self.ok(self.start('run', 'browser', '--owner', 'a', '--key', 'chrome', '--', 'sh', '-c',
                                 f'ls {self.root}/home'))
        self.assertIn('up-chrome', out)
        self.assertFalse((self.root / 'home' / 'up-chrome').exists())


class Canonical(Base):
    def copy(self, where):
        target = self.root / where / 'agent_slots'
        shutil.copytree(REPO / 'agent_slots', target, ignore=shutil.ignore_patterns('__pycache__'))
        return target

    def test_stale_copy_runs_the_canonical_one(self):
        canonical = self.copy('canonical')
        (canonical / 'cli.py').write_text((canonical / 'cli.py').read_text().replace(
            "def dispatch(args, rest):", "def dispatch(args, rest):\n    print('canonical copy')"))
        self.configure(self.pools, canonical=str(canonical))
        out = self.ok(self.start('status'))
        self.assertTrue(out.startswith('canonical copy'), out)

    def test_waiter_restarts_with_an_updated_canonical_copy_and_keeps_its_place(self):
        canonical = self.copy('canonical')
        self.configure({'gpu': {'type': 'token', 'capacity': 1}}, canonical=str(canonical))
        env = dict(self.env, PYTHONPATH=str(canonical.parent))
        self.ok(self.start('hold', 'gpu', '--owner', 'blocker', '--minutes', '1', env=env))
        first = self.start('run', 'gpu', '--owner', 'first', '--', sys.executable, '-c', 'pass', env=env)
        time.sleep(0.4)
        second = self.start('run', 'gpu', '--owner', 'second', '--', sys.executable, '-c', 'pass', env=env)
        time.sleep(0.4)
        time.sleep(1.1)  # file times have one-second resolution on some file systems
        (canonical / 'strategies.py').write_text((canonical / 'strategies.py').read_text() + '\n# updated\n')
        time.sleep(0.8)
        self.ok(self.start('release', '--owner', 'blocker', env=env))
        self.ok(first)
        self.ok(second)
        log = (self.root / 'home' / 'log.txt').read_text()
        self.assertIn('restarting this waiter with it, place kept', log)
        self.assertLess(log.index('slot -> first'), log.index('slot -> second'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
