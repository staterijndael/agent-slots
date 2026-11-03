"""What the machine can afford right now, and whether a process is still the one we recorded."""
import os
import subprocess
import sys


def alive(pid, token=None):
    """True while pid runs. With a token (its start time), a reused pid of another process counts as dead."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return token is None or start_token(pid) in (token, None)


def start_token(pid):
    """The process start time as ps prints it; None when ps cannot tell."""
    try:
        out = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip() or None


def memory():
    """(percent of memory free, percent of swap used).

    Free means what the OS can hand out without swapping: memory_pressure on macOS, MemAvailable on Linux.
    Swap is reported but is a weak signal alone: it stays allocated long after memory frees
    (84% swap with 37% free, 2026-11-03)."""
    if os.environ.get('SLOTS_FAKE_FREE'):
        return int(os.environ['SLOTS_FAKE_FREE']), int(os.environ.get('SLOTS_FAKE_SWAP', '0'))
    if sys.platform == 'darwin':
        return _memory_macos()
    return _memory_linux()


def _memory_macos():
    try:
        out = subprocess.run(['memory_pressure', '-Q'], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ''
    free = 100
    for line in out.splitlines():
        if 'free percentage' in line:
            free = int(line.rsplit(' ', 1)[-1].rstrip('%'))
    try:
        swap = subprocess.run(['sysctl', '-n', 'vm.swapusage'], capture_output=True, text=True, timeout=5).stdout.split()
        used = round(100 * float(swap[5].rstrip('M')) / float(swap[2].rstrip('M')))
    except (OSError, subprocess.SubprocessError, IndexError, ValueError, ZeroDivisionError):
        used = 0
    return free, used


def _memory_linux():
    info = {}
    try:
        with open('/proc/meminfo') as stream:
            for line in stream:
                name, _, value = line.partition(':')
                info[name] = int(value.split()[0])
    except (OSError, ValueError, IndexError):
        return 100, 0
    total = info.get('MemTotal') or 1
    free = round(100 * info.get('MemAvailable', info.get('MemFree', total)) / total)
    swap_total = info.get('SwapTotal', 0)
    used = round(100 * (swap_total - info.get('SwapFree', 0)) / swap_total) if swap_total else 0
    return free, used


def load_per_core():
    """One-minute load average divided by the core count."""
    if os.environ.get('SLOTS_FAKE_LOAD'):
        return float(os.environ['SLOTS_FAKE_LOAD'])
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    except OSError:
        return 0.0


def readings():
    free, swap = memory()
    return {'free': free, 'swap': swap, 'load': round(load_per_core(), 2)}
