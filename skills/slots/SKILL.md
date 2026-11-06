---
name: slots
description: Use before compiling, running tests on an iOS simulator or Android emulator, booting a simulator, starting an emulator or headless browser, or using the GPU, on a machine where several agent sessions run at once; also when a build seems to wait, `slots status` shows a queue, or a command was refused by the agent-slots guard.
---

# Heavy resources go through `slots`

Several agent sessions share this machine's memory and cores. `slots` queues heavy work and starts it when a slot
is free and the machine can afford it.

The command is `${CLAUDE_PLUGIN_ROOT}/scripts/slots`; below it is written `slots`. Use the full path in every
command unless `command -v slots` finds it on your PATH.

## Commands
- Compile: `slots run build --owner <name> -- <build command>`. A build that alone fills the machine (Bazel, a
  full clean build): add `--exclusive`.
- Tests on a simulator: build first in `build`, then
  `slots run ios-sim --owner <name> --key <simulator udid> -- <test command>`. `{key}` in the command is replaced
  with the udid, and `$SLOTS_KEY` holds it. The simulator is booted for you and shut down when nobody waits for it.
- Hands-on or visual check: `slots hold ios-sim --owner <name> --key <udid> --minutes 10`; it returns once the
  simulator is booted. `slots renew --owner <name>` while you work, `slots release --owner <name>` when done.
  Release before you wait for the user; hold again when they are ready.
- `slots status`: holders, the queue, memory, and why a slot is not granted (for example "lowered by free 18%").

`<name>` is your session's name, the same in every call, so leases and fair sharing work. Use your worktree or
branch name if you have no other.

## Waiting
A `slots run` can wait minutes for its turn. Run it in the background (`run_in_background`) rather than letting
the shell tool time out, or `slots hold` first and run your command after it returns. While you wait, do other
work; do not start the same command again, which would queue it twice.

## Exit codes
- the command's own code: it ran;
- 124: it hit the pool's run timeout and was stopped;
- 70: the simulator or emulator failed to start (crash loop or boot timeout); it is shut down again;
- 75: that instance failed to start recently and cools down: use another key or wait for the time printed;
- 2: usage or config error, for example a pool that does not exist (`slots status` lists the pools).

## Do not
- run `xcodebuild`, `xcrun simctl boot`, `emulator -avd` or `./gradlew` builds directly; the guard refuses them.
  Only if the user explicitly asks to bypass the queue, end the command with `# slots-ok`;
- kill other sessions' simulators or builds to make room: `slots` stops idle instances itself;
- edit `~/.agent-slots/config.json` unless the user asks; `/agent-slots:setup` tunes it.
