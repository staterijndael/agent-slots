---
name: status
description: Show who holds the machine's shared simulators, emulators and build slots, who waits, and why. Use when the user runs /agent-slots:status or asks why a build or test is waiting.
---

Run `${CLAUDE_PLUGIN_ROOT}/scripts/slots status` and summarize it for the user in a few lines: per pool, how many slots are held out of how many
are allowed now and why the number is lowered (free memory, swap, load), who holds each slot and for how long, who
waits and for how long, instances running without a slot, and instances cooling down after failed starts. If a
waiter has waited long while a slot looks idle, say so and suggest `slots release --owner <holder>` only for the
user's own leases. `slots status --json` has the same data in full.
