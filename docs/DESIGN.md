# Design notes

`slots` generalizes `relay_sim.py`, a scheduler written for one project (an iOS messenger app) while fifteen agent
chats shared one MacBook (M3 Pro, 18 GB RAM, 12 cores). Every rule below was an incident there between
2026-10-30 and 2026-11-03, and every one has a test in `tests/test_slots.py`.

| Incident | Rule | Where |
|---|---|---|
| One global `lockf` covered compiles, test runs and boots: both simulators sat idle while one chat compiled, 16 commands queued (10-30) | separate pools; a compile never holds a simulator | `build` and `ios-sim` pools; `test_compiling_does_not_hold_a_simulator` |
| More than two booted simulators made the Mac swap (10-30) | capacity per pool, strays stopped when room is needed | `make_room`; `test_stray_is_shut_down_when_room_is_needed` |
| Hands-on checks kept simulators booted while the agent waited for a human (10-30) | leases with renew and expiry | `hold`, `renew`; `test_lease_runs_out...` |
| A hold and a release in the same second shut a simulator down mid-boot; SpringBoard crash-looped 12 times in two minutes and `bootstatus` waited forever (11-02) | an instance marked "starting" is never stopped or reclaimed; a start has a timeout | `starting` in a grant; `test_release_during_start_waits_for_the_start` |
| SpringBoard crash-looped under swap 14.5 of 15 GB and load 234 (11-02) | stop at the first crash report, retry once, then cool down | `Simctl.start`, `cooldown`; `test_springboard_crash_stops_the_boot_at_once` |
| A hung test run kept its slot 47 minutes (11-02) | run timeout stops the command and frees the slot | `run_timeout`; `test_hung_run_is_stopped...` |
| Swap read 84% while 37% of memory was free; the check ran after the grant, so two holders waited two hours on both slots and five chats queued with no simulator booted (11-03) | judge by free memory, check before granting, never against a holder | `limits`, `Scheduler.allowed`; `test_memory_is_checked_before_a_grant...` |
| Zero simulators under load from two compiles while 56% of memory was free (11-03) | graded limits (2/1/0); load alone lowers to one, never to zero | default `ios-sim` limits; `test_load_alone_lowers_to_one_never_zero` |
| Old copies of the script in chats' branches held both slots for hours (11-03) | one canonical copy; stale copies and queued waiters re-execute it | `canonical`, `reexec`; `Canonical` tests |
| Holders that never booted their simulator kept slots (11-03) | a holder whose instance is not running after `stale_seconds` loses the slot | `_dead_reason`; `test_stuck_holder...` |
| A waiter for a busy device made everyone behind it wait | no head-of-line blocking: skip waiters whose key is held | `plan`; `test_same_key_waits_but_does_not_block_others` |
| Agents run in zsh, which does not word-split `$RS` holding "python3 path", and expands unquoted `[...]` and `$VAR` in the caller | one executable (`bin/slots`, works through a symlink); `{key}` replaced in argv by `slots` and `SLOTS_KEY` in the environment, with no shell between `slots` and the command | `bin/slots`, `Scheduler.run`; `test_keys_are_picked_and_passed_to_the_command` |

## Choices
- **No daemon.** A daemon would need installing, starting, upgrading and watching, and an agent cannot tell a
  dead daemon from a long queue. Files under `flock` survive crashes of any participant, and every waiter tidies
  for everyone. Cost: each waiter polls (default every 2 s) and reads memory; with 15 waiters that is a few
  subprocess calls per second, which measured as noise next to one compile.
- **One machine config, not one per project.** Memory and cores belong to the machine. Two projects with their
  own schedulers would each think they own the two simulators.
- **Grants are planned, not raced.** Every waiter computes the same plan from the same state under the lock, and
  only the waiter the plan names grants itself, so the strategy decides, not whoever polls first.
- **Limits only gate new grants.** Taking a slot back from a running holder because memory dropped would kill
  work in progress and free nothing quickly; it waits for the holder instead.
- **PID reuse.** A grant stores the holder's start time (`ps -o lstart`); a live pid with another start time
  counts as dead.
- **Reservation with backfill.** An `--exclusive` build needs every unit. Smaller waiters may pass it for
  `backfill_seconds`; after that the room is kept, so it cannot starve. It keeps the room also while a limit
  lowers the pool below its size: under load from compiles, letting small compiles pass forever would keep the
  load high and the big one waiting forever.

- **Adaptive, not clever.** `adaptive` only switches among rules that are simple to explain, and says which
  one ordered the queue and why. Short-first needs run times, so it stays off until two waiting kinds of
  command have history; `starve_minutes` bounds how long a long job can be passed. Fairness counts recent
  grants, not only slots held: with short jobs a flooding session holds nothing at the moment of each decision
  (a test caught this in the first version).

## Not done yet
- Coordination across machines (a shared store instead of one local file).
- An MCP server front end, for agents whose shell tool times out on long waits (Block's agent-task-queue chose MCP for this reason).
- A hook that refuses heavy commands not wrapped in `slots` (mic-lock ships one for Claude Code).
- Linux-specific types (Docker containers, GPUs via `nvidia-smi` health).
