"""Resource types: what it takes to bring one instance (a key) of a resource up, check it and take it down.

A pool names its type in the config: a built-in (`token`, `process`, `simctl`) or a plugin,
`"type": "/path/to/file.py:ClassName"` or `"type": "package.module:ClassName"`. A plugin subclasses
ResourceType and overrides what it needs; every hook has a safe default."""
import importlib
import importlib.util
import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

from . import system


class ResourceType:
    """The plugin interface. Keys are instance names: a simulator udid, an AVD name, a GPU index."""
    lifecycle = False  # True when the tool starts and stops instances itself

    def __init__(self, pool, home, log):
        self.pool = pool
        self.home = home
        self.log = log

    def start(self, key, timeout):
        """Brings the instance up and waits until it is usable. Returns None, or a reason it could not."""
        return None

    def stop(self, key, reason):
        """Takes the instance down."""

    def running(self):
        """Keys of instances running now, with or without a slot; None when the type cannot tell."""
        return None

    def is_running(self, key):
        found = self.running()
        return None if found is None else key in found

    def busy_elsewhere(self, key):
        """True while a tool outside the scheduler uses an instance, so it must not be stopped as a stray."""
        return False

    def names(self):
        """Human names for keys, for status."""
        return {}


class Token(ResourceType):
    """A counting semaphore: build slots, test-runner slots, anything without an instance to manage."""


class Process(ResourceType):
    """An instance is a long-running process the tool starts: an Android emulator, a browser, an IDE, a server.

    Pool config: `start` (argv, `{key}` is replaced), optional `ready` (argv polled until it exits 0) and
    `stop` (argv; default SIGTERM to the process group, then SIGKILL)."""
    lifecycle = True

    def _file(self, key):
        folder = self.home / 'procs' / self.pool['name']
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{key.replace('/', '_')}.json"

    def _record(self, key):
        try:
            return json.loads(self._file(key).read_text())
        except (OSError, ValueError):
            return None

    def _argv(self, name, key):
        return [part.replace('{key}', key) for part in self.pool[name]] if self.pool.get(name) else None

    def start(self, key, timeout):
        record = self._record(key)
        if record and system.alive(record['pid'], record.get('token')):
            return None
        logs = self.home / 'procs' / self.pool['name']
        with open(logs / f"{key.replace('/', '_')}.log", 'a') as out:
            process = subprocess.Popen(self._argv('start', key), stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                       start_new_session=True)
        self._file(key).write_text(json.dumps({'pid': process.pid, 'token': system.start_token(process.pid)}))
        ready = self._argv('ready', key)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if process.poll() is not None:
                self._file(key).unlink(missing_ok=True)
                return f"exited with {process.returncode} while starting (log: {logs}/{key}.log)"
            if not ready or subprocess.run(ready, capture_output=True).returncode == 0:
                return None
            time.sleep(min(1.0, float(os.environ.get('SLOTS_POLL', '1'))))
        self.stop(key, 'did not become ready')
        return f"not ready after {int(timeout)} s"

    def stop(self, key, reason):
        record = self._record(key)
        command = self._argv('stop', key)
        if command:
            subprocess.run(command, capture_output=True)
        if record and system.alive(record['pid'], record.get('token')):
            stop_group(record['pid'])
        self._file(key).unlink(missing_ok=True)
        self.log(f"{self.pool['name']}: stopped {key}: {reason}")

    def running(self):
        folder = self.home / 'procs' / self.pool['name']
        found = set()
        for file in folder.glob('*.json') if folder.exists() else []:
            try:
                record = json.loads(file.read_text())
            except (OSError, ValueError):
                continue
            if system.alive(record['pid'], record.get('token')):
                found.add(file.stem)
        return found


class Simctl(ResourceType):
    """iOS simulators through `xcrun simctl`. Keys are device udids. Every lesson here was an incident:
    - boot only a fully shut down device: booting one still shutting down left SpringBoard crash-looping;
    - stop at the first SpringBoard crash report for the device, shut down cleanly, retry once;
    - give up a boot that does not finish in start_timeout instead of holding the slot forever."""
    lifecycle = True

    def __init__(self, pool, home, log):
        super().__init__(pool, home, log)
        self.simctl = shlex.split(os.environ.get('SLOTS_SIMCTL', pool.get('simctl', 'xcrun simctl')))
        self.crash_dir = Path(os.path.expanduser(os.environ.get(
            'SLOTS_CRASH_DIR', pool.get('crash_dir', '~/Library/Logs/DiagnosticReports'))))
        self.poll = float(os.environ.get('SLOTS_POLL', '2'))

    def _run(self, *args, timeout=60):
        try:
            return subprocess.run(self.simctl + list(args), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 1, '', 'simctl timed out')

    def _devices(self):
        try:
            return [d for entries in json.loads(self._run('list', 'devices', '-j').stdout or '{}')
                    .get('devices', {}).values() for d in entries]
        except ValueError:
            return []

    def running(self):
        return {d['udid'] for d in self._devices() if d.get('state') == 'Booted'}

    def names(self):
        return {d['udid']: d.get('name', d['udid']) for d in self._devices()}

    def _state(self, udid):
        return next((d.get('state', '') for d in self._devices() if d['udid'] == udid), '')

    def _wait_shutdown(self, udid, seconds=60):
        deadline = time.time() + seconds
        while self._state(udid) not in ('Shutdown', 'Booted', '') and time.time() < deadline:
            time.sleep(self.poll)

    def _crashed(self, udid, since):
        for report in self.crash_dir.glob('SpringBoard-*.ips'):
            try:
                if report.stat().st_mtime >= since and udid in report.read_text(errors='ignore'):
                    return True
            except OSError:
                pass
        return False

    def start(self, key, timeout):
        for attempt in range(2):
            self._wait_shutdown(key)
            started = time.time()
            boot = self._run('boot', key)
            if boot.returncode and 'Booted' not in boot.stderr:
                if attempt:
                    return f"cannot boot: {boot.stderr.strip()}"
                continue
            status = subprocess.Popen(self.simctl + ['bootstatus', key, '-b'],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while status.poll() is None and not self._crashed(key, started) and time.time() - started < timeout:
                time.sleep(self.poll)
            if status.poll() is None:
                status.kill()
                status.wait()
            crashed = self._crashed(key, started)
            if status.returncode == 0 and not crashed:
                return None
            self.log(f"{key}: {'SpringBoard crashed' if crashed else 'boot did not finish'} on attempt "
                     f"{attempt + 1}; shutting down")
            self._run('shutdown', key)
            self._wait_shutdown(key)
        return (f"SpringBoard kept crashing or the boot did not finish in {int(timeout)} s "
                f"(see {self.crash_dir}/SpringBoard-*)")

    def stop(self, key, reason):
        self._run('shutdown', key)
        self.log(f"{self.pool['name']}: shut down {key}: {reason}")

    def busy_elsewhere(self, key):
        """Only tools that drive a simulator count; log viewers and queued shells with the udid in their text don't."""
        tools = {'xcodebuild', 'simctl', 'xcrun', 'xctest', 'lldb'}
        ps = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout
        for line in ps.splitlines():
            pid, _, command = line.strip().partition(' ')
            if (key in command and int(pid) != os.getpid() and os.path.basename(command.split(' ')[0]) in tools
                    and 'log stream' not in command):
                return True
        return False


BUILT_IN = {'token': Token, 'process': Process, 'simctl': Simctl}


def load(pool, home, log):
    kind = pool['type']
    if kind in BUILT_IN:
        return BUILT_IN[kind](pool, home, log)
    target, _, cls = kind.rpartition(':')
    if not target:
        raise SystemExit(f"slots: pool {pool['name']}: unknown type {kind!r}; use a built-in "
                         f"({', '.join(BUILT_IN)}) or /path/file.py:Class or module:Class")
    if target.endswith('.py'):
        spec = importlib.util.spec_from_file_location(f"slots_plugin_{pool['name']}", os.path.expanduser(target))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    return getattr(module, cls)(pool, home, log)


def stop_group(pid, grace=10):
    """SIGTERM to the process group, SIGKILL after grace seconds."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
