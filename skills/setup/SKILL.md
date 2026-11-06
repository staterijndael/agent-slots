---
name: setup
description: Configure agent-slots pools for this machine (memory, cores, simulators, emulators, GPUs), or change capacities, limits and strategies. Use when the user runs /agent-slots:setup, asks how many simulators or builds may run at once, or wants another resource shared between agent sessions.
---

# Tune agent-slots for this machine

The command is `${CLAUDE_PLUGIN_ROOT}/scripts/slots` (written `slots` below). The config is
`~/.agent-slots/config.json` (or `$SLOTS_CONFIG`). Every waiting agent re-reads it on its next poll,
so a change takes effect at once. Show the user the proposed config and write it only after they agree.

1. Measure the machine:
   - memory: macOS `sysctl -n hw.memsize`, Linux `grep MemTotal /proc/meminfo`; cores: `sysctl -n hw.ncpu` or `nproc`;
   - now: `slots status` (free memory, swap, load, current pools);
   - resources: `xcrun simctl list devices` (iOS), `emulator -list-avds` (Android), `nvidia-smi -L` (GPUs).
2. Ask the user what the sessions do: iOS, Android, web UI tests, ML. Add only pools for those.
3. Size each pool from the memory each instance needs, leaving about 30% of memory for the agents, editors and
   the system. Rough figures: an iOS simulator 1.5-3 GB, an Android emulator 2-4 GB, an Xcode or Gradle build
   2-6 GB, a headless Chrome 0.3-1 GB. On an 18 GB Mac the tested values are 2 simulators and 2 builds.
4. Add graded limits so the pool shrinks before the machine swaps, never to zero from load alone:
   `{"when": {"free_below": 10}, "max": 0}`, `{"when": {"free_below": 20}, "max": 1}`,
   `{"when": {"load_above": 3}, "max": 1}` (load is per core). Swap alone is a weak signal: combine
   `swap_above` with `free_below`.
5. Pick strategies: `fifo` for simulators, `adaptive` for builds (fair when one session floods the queue,
   short jobs first when the queue is long).
6. Resource types: `simctl` for iOS simulators (key = udid); `process` for emulators, browsers, servers
   (`start`, `ready`, `stop` argv with `{key}`); `token` for builds and GPUs (`keys` plus
   `"env": {"CUDA_VISIBLE_DEVICES": "{key}"}`). The README in the plugin folder lists every field.
7. Write the file, run `slots status`, and show the user the result. The guard refuses direct compiles and boots
   only for pools that exist; `"guard": false` in the config turns it off.
