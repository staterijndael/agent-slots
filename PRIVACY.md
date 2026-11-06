# Privacy

Agent Slots runs entirely on your machine. It collects no data, has no telemetry, and makes no network requests.

What it reads and writes, all locally:

- `~/.agent-slots/` (or `$SLOTS_HOME`): `config.json` (your pools), `state.json` (who holds and who waits for each
  slot: session names, process ids, the commands being run, times), `log.txt` (grants, releases and reclaims),
  and pid files of instances it started.
- The Claude Code plugin's data folder: a copy of the `agent_slots` package, so every session runs one version.
- Read-only: free memory (`memory_pressure` on macOS, `/proc/meminfo` on Linux), swap (`sysctl vm.swapusage`),
  load average, the process list (`ps`), simulator state (`xcrun simctl list`), and SpringBoard crash reports in
  `~/Library/Logs/DiagnosticReports` to detect crash loops.

Nothing leaves your machine. Delete `~/.agent-slots` to remove everything it stored.

Questions: https://github.com/staterijndael/agent-slots/issues
