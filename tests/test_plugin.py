#!/usr/bin/env python3
"""Tests for the Claude Code plugin hooks: python3 -m unittest discover tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class Guard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.config = Path(self.dir.name) / 'config.json'
        self.config.write_text(json.dumps({'pools': {'build': {}, 'ios-sim': {}}}))

    def tearDown(self):
        self.dir.cleanup()

    def check(self, command, tool='Bash'):
        run = subprocess.run([sys.executable, str(REPO / 'plugin' / 'guard.py')], text=True, capture_output=True,
                             input=json.dumps({'tool_name': tool, 'tool_input': {'command': command}}),
                             env=dict(os.environ, SLOTS_CONFIG=str(self.config)))
        return run.returncode, run.stderr

    def test_direct_compiles_and_boots_are_refused_with_the_slots_command(self):
        for command in ('xcodebuild -scheme App build', 'cd ios && xcodebuild test', '/usr/bin/xcodebuild build',
                        'FOO=1 time xcodebuild build', 'xcrun simctl boot ABC', './gradlew assembleDebug'):
            self.assertEqual(self.check(command)[0], 2, command)
        message = self.check('xcrun simctl boot ABC')[1]
        self.assertIn('slots run ios-sim --owner <your session name> --key <udid> -- xcrun simctl boot ABC', message)

    def test_commands_through_slots_and_harmless_ones_pass(self):
        for command in ('slots run build -- sh -c "cd x && xcodebuild build"', 'xcodebuild -list',
                        'python3 /x/relay-ios/scripts/relay_sim.py test --owner a', '$RS build -- xcodebuild build',
                        'echo "xcodebuild build"', 'git commit -m "xcodebuild && simctl boot"',
                        'xcodebuild build # slots-ok', 'xcrun simctl install ABC App.app'):
            self.assertEqual(self.check(command)[0], 0, command)

    def test_only_pools_that_exist_are_guarded(self):
        self.assertEqual(self.check('emulator -avd Pixel_8')[0], 0, 'no android-emu pool in this config')
        self.config.write_text(json.dumps({'pools': {'android-emu': {}}}))
        self.assertEqual(self.check('emulator -no-window -avd Pixel_8')[0], 2)
        self.assertEqual(self.check('xcodebuild build')[0], 0, 'no build pool now')

    def test_guard_can_be_turned_off_or_given_own_rules(self):
        self.config.write_text(json.dumps({'pools': {'build': {}}, 'guard': False}))
        self.assertEqual(self.check('xcodebuild build')[0], 0)
        self.config.write_text(json.dumps({'pools': {'build': {}}, 'guard': {'rules': [{'pool': 'build',
                                                                                        'pattern': '^make\\b'}]}}))
        self.assertEqual(self.check('make -j8')[0], 2)
        self.assertEqual(self.check('xcodebuild build')[0], 0)

    def test_no_config_or_broken_input_lets_everything_through(self):
        self.config.unlink()
        self.assertEqual(self.check('xcodebuild build')[0], 0)
        run = subprocess.run([sys.executable, str(REPO / 'plugin' / 'guard.py')], input='not json', text=True,
                             capture_output=True)
        self.assertEqual(run.returncode, 0)


class SessionStart(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(REPO), CLAUDE_PLUGIN_DATA=str(root / 'data'),
                        SLOTS_HOME=str(root / 'home'))
        self.env.pop('SLOTS_CONFIG', None)
        self.config = root / 'home' / 'config.json'

    def tearDown(self):
        self.dir.cleanup()

    def start(self):
        run = subprocess.run([sys.executable, str(REPO / 'plugin' / 'session_start.py')], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)['hookSpecificOutput']['additionalContext']

    def test_first_start_writes_a_config_with_the_data_copy_as_canonical(self):
        note = self.start()
        canonical = Path(json.loads(self.config.read_text())['canonical'])
        self.assertEqual(canonical, Path(self.env['CLAUDE_PLUGIN_DATA']) / 'agent_slots')
        self.assertTrue((canonical / 'core.py').exists())
        self.assertIn('slots run <pool>', note)
        self.assertIn('starter config', note)

    def test_an_existing_config_is_kept_and_the_copy_is_refreshed_only_on_change(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'canonical': '/elsewhere/agent_slots', 'pools': {'gpu': {'capacity': 2}}}))
        note = self.start()
        self.assertEqual(json.loads(self.config.read_text())['canonical'], '/elsewhere/agent_slots')
        self.assertIn('gpu (token, 2)', note)
        copy = Path(self.env['CLAUDE_PLUGIN_DATA']) / 'agent_slots' / 'core.py'
        before = copy.stat().st_mtime_ns
        self.start()
        self.assertEqual(copy.stat().st_mtime_ns, before, 'an unchanged package is not rewritten')

    def test_slots_runs_from_the_plugin_bin_folder(self):
        self.start()
        run = subprocess.run([str(REPO / 'scripts' / 'slots'), 'status'], text=True, capture_output=True,
                             env=dict(self.env, SLOTS_FAKE_FREE='50'))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('build (token', run.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
