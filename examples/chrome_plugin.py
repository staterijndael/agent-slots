"""Example resource type: one headless Chrome per key, each with its own profile and debugging port.

Config: {"type": "/path/to/chrome_plugin.py:Chrome", "capacity": 2, "keys": ["9222", "9223"],
         "env": {"CHROME_DEBUG_PORT": "{key}"}}
An agent then runs `slots run browser -- my-ui-check --cdp http://127.0.0.1:$CHROME_DEBUG_PORT`."""
import os
import time
import urllib.request

from agent_slots.resources import Process

CHROME = os.environ.get('CHROME', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')


class Chrome(Process):
    def __init__(self, pool, home, log):
        profile = str(home / 'chrome-{key}')
        pool = dict(pool, start=[CHROME, '--headless=new', '--remote-debugging-port={key}',
                                 f'--user-data-dir={profile}', 'about:blank'])
        super().__init__(pool, home, log)

    def start(self, key, timeout):
        problem = super().start(key, timeout)
        if problem:
            return problem
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{key}/json/version', timeout=2)
                return None
            except OSError:
                time.sleep(0.5)
        self.stop(key, 'debugging port never answered')
        return f'port {key} did not answer in {int(timeout)} s'
