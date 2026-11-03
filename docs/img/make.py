import sys
from pathlib import Path
out = Path(sys.argv[1]); style = (Path(__file__).parent / '_style.txt').read_text()

def svg(name, w, h, body):
    (out / f'{name}.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img">\n{style}\n<rect class="bg" width="{w}" height="{h}" rx="12"/>\n{body}\n</svg>\n')

def box(x, y, w, h, cls='card', rx=8):
    return f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}"/>'

def text(x, y, s, cls='t', anchor='start'):
    s = s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{s}</text>'

def arrow(x1, y1, x2, y2):
    return f'<path class="line" d="M{x1},{y1} L{x2},{y2}" marker-end="url(#a)"/>'

def curve(x1, y1, x2, y2):
    mx = (x1 + x2) / 2
    return f'<path class="line" d="M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}" marker-end="url(#a)"/>'

# 1. overview -------------------------------------------------------------------------------------------
b = [text(24, 36, 'N agent sessions, M scarce resources, one machine', 'h')]
agents = [('Claude Code', 'slots run build -- xcodebuild'), ('Claude Code', 'slots run ios-sim --key A …'),
          ('Codex', 'slots hold ios-sim --key B'), ('Cursor agent', 'slots run android-emu …'),
          ('any CLI agent', 'slots run gpu -- train.py')]
for i, (name, cmd) in enumerate(agents):
    y = 64 + i * 66
    b += [box(24, y, 230, 52), text(38, y + 21, name), text(38, y + 40, cmd, 'm')]
    b.append(curve(254, y + 26, 318, 64 + 2 * 66 + 26 + (i - 2) * 22))
b += [box(320, 64, 260, 316, 'panel', 12), text(450, 90, 'slots', 'h', 'middle'),
      text(450, 108, 'no daemon: every waiter is a process', 's', 'middle')]
inner = [('blue', 'queue', 'tickets, first come first'), ('violet', 'strategy', 'fifo · priority · fair · adaptive'),
         ('amber', 'limits', 'live free memory, swap, load'), ('green', 'self-healing', 'leases, reclaim, cool-down'),
         ('card', 'state.json + flock', 'one file, shared by all')]
for i, (cls, title, sub) in enumerate(inner):
    y = 122 + i * 50
    b += [box(336, y, 228, 42, cls), text(350, y + 18, title), text(350, y + 34, sub, 's')]
pools = [('ios-sim', 'simctl', 2, 'iOS simulators'), ('build', 'token', 2, 'compiles'),
         ('android-emu', 'process', 1, 'emulators'), ('browser', 'plugin', 3, 'headless Chrome'),
         ('gpu', 'token + keys', 2, 'GPUs by index')]
for i, (name, kind, cap, sub) in enumerate(pools):
    y = 64 + i * 66
    b.append(curve(580, 64 + 2 * 66 + 26 + (i - 2) * 22, 644, y + 26))
    b += [box(646, y, 230, 52), text(660, y + 21, f'{name}'), text(862, y + 21, kind, 's', 'end'), text(660, y + 40, sub, 's')]
    for k in range(cap):
        b.append(f'<rect class="green" x="{862 - (cap - k) * 16}" y="{y + 30}" width="12" height="12" rx="3"/>')
b.append(text(450, 404, 'a slot is granted when one is free AND the machine can afford it', 's', 'middle'))
svg('overview', 900, 420, '\n'.join(b))

# 2. life of a request ----------------------------------------------------------------------------------
b = [text(24, 36, 'The life of one request', 'h'),
     text(24, 58, 'slots run ios-sim --key A -- xcodebuild test-without-building …', 'm')]
steps = [('blue', '1  queue', 'a ticket in state.json'), ('green', '2  tidy', 'take back dead slots'),
         ('amber', '3  allowed?', 'capacity lowered by limits'), ('violet', '4  plan', 'strategy orders waiters'),
         ('blue', '5  grant', 'marked "starting"'), ('green', '6  start', 'boot, watch for crashes'),
         ('card', '7  run', 'your command, with timeout'), ('card', '8  release', 'stop it if nobody waits')]
for i, (cls, title, sub) in enumerate(steps):
    col, row = i % 4, i // 4
    x, y = 24 + col * 218, 104 + row * 150
    b += [box(x, y, 196, 58, cls), text(x + 14, y + 24, title), text(x + 14, y + 44, sub, 's')]
    if col < 3:
        b.append(arrow(x + 196, y + 29, x + 216, y + 29))
b.append(f'<path class="line" d="M{24 + 3 * 218 + 98},{104 + 58} L{24 + 3 * 218 + 98},{208} L{24 + 98},{208} L{24 + 98},{252}" marker-end="url(#a)"/>')
b.append(f'<path class="line" d="M{24 + 2 * 218 + 98},{104} L{24 + 2 * 218 + 98},{90} L{24 + 98},{90} L{24 + 98},{102}" marker-end="url(#a)" stroke-dasharray="4 4"/>')
b.append(text(24 + 218 + 98, 84, 'not yet: poll again in 2 s; a waiter for a busy key blocks no one', 's', 'middle'))
notes = [('2  tidy frees a slot when', ['the holder process died (pid + start time)', 'a lease ran out', 'the holder died mid-start',
                                         'its instance is gone 10 min after grant', '…and stops the orphaned command']),
         ('6  a failed start', ['stops at the first crash report', 'shuts down cleanly, retries once', 'frees the slot',
                                'cools the key down 1, 2, 4… 15 min']),
         ('never', ['stops an instance mid-start', 'revokes a holder when memory drops', 'runs a stale copy: all re-exec',
                    'the one canonical version'])]
for i, (title, lines) in enumerate(notes):
    x, y = 24 + i * 290, 336
    b += [box(x, y, 270, 132, 'panel'), text(x + 14, y + 24, title)]
    b += [text(x + 14, y + 46 + j * 19, '· ' + line, 's') for j, line in enumerate(lines)]
svg('lifecycle', 900, 490, '\n'.join(b))

# 3. graded capacity ------------------------------------------------------------------------------------
b = [text(24, 36, 'Capacity follows live free memory, not a fixed number', 'h'),
     text(24, 58, 'default ios-sim limits on an 18 GB Mac; checked before every grant, never against a slot already held', 's')]
x0, w, y0 = 40, 820, 92
bands = [(0, 10, 'red', '0 simulators', 'under 10% free'), (10, 20, 'amber', '1 simulator', '10-20% free'),
         (20, 100, 'green', '2 simulators', '20% free and more')]
pos = {0: 0, 10: 170, 20: 340, 100: w}
for lo, hi, cls, title, sub in bands:
    bx, bw = x0 + pos[lo], pos[hi] - pos[lo]
    b += [f'<rect class="{cls}" x="{bx}" y="{y0}" width="{bw}" height="44"/>',
          text(bx + bw / 2, y0 + 20, title, 't', 'middle'), text(bx + bw / 2, y0 + 36, sub, 's', 'middle')]
b.append(f'<rect class="line" x="{x0}" y="{y0}" width="{w}" height="44" rx="4"/>')
for p in (0, 10, 20, 100):
    b.append(text(x0 + pos[p], y0 + 62, f'{p}%', 's', 'middle'))
b.append(text(x0 + w / 2, y0 + 82, 'free memory (memory_pressure on macOS, MemAvailable on Linux); scale not linear', 's', 'middle'))
rules = [('swap over 90% and under 25% free', '→ 1', 'swap alone stays allocated long after memory frees'),
         ('load over 3 per core', '→ 1', 'compiles alone never stop simulators: never 0 from load'),
         ('build pool: under 25% free or load over 2', '→ 1 compile', 'a second compile only when there is room')]
for i, (cond, res, why) in enumerate(rules):
    y = 210 + i * 40
    b += [box(40, y, 820, 32, 'panel'), text(56, y + 21, cond), text(400, y + 21, res, 'h'), text(500, y + 21, why, 's')]
svg('capacity', 900, 340, '\n'.join(b))

# 4. adaptive strategy ----------------------------------------------------------------------------------
b = [text(24, 36, 'The adaptive strategy picks a rule on every poll', 'h'),
     text(24, 58, 'explicit --priority always first; every switch is logged and shown in slots status', 's')]
modes = [(60, 'blue', 'fifo', ['every waiter fits in the', 'free units: order does', 'not matter']),
         (330, 'violet', 'fair', ['one session has over half', 'of the work (held, recent,', 'waiting) and others wait']),
         (600, 'green', 'short-first', ['more waiters than free units', 'and run times are known;', 'starved waiters go first'])]
for x, cls, title, lines in modes:
    b += [box(x, 90, 240, 110, cls, 14), text(x + 120, 120, title, 'h', 'middle')]
    b += [text(x + 120, 144 + j * 18, line, 's', 'middle') for j, line in enumerate(lines)]
b += [arrow(300, 130, 328, 130), arrow(328, 160, 302, 160), arrow(570, 130, 598, 130), arrow(598, 160, 572, 160)]
b += [box(60, 226, 780, 84, 'panel'), text(76, 250, 'short-first learns run times by itself'),
      text(76, 272, 'the median of the last five runs of each kind of command: program + last plain word', 's'),
      text(76, 292, 'xcodebuild … build-for-testing   ·   xcodebuild … test-without-building   ·   bazel build', 'm')]
svg('adaptive', 900, 330, '\n'.join(b))
