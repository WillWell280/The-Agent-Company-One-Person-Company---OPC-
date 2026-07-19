# Debug Session: stop-restart-hang

Status: [OPEN]

- Debug Server: http://127.0.0.1:7777/event
- Log File: `.dbg/trae-debug-log-stop-restart-hang.ndjson`

## Symptoms

1. Stopping a task and immediately starting again logs that the previous background task is still exiting.
2. The first task can remain running for a long time without producing output.
3. The top hero container has excessive empty space.

## Hypotheses

| ID | Hypothesis | Likelihood | Effort | Expected signal |
|---|---|---:|---:|---|
| A | `force_stop()` clears UI state before the worker exits | High | Low | Stop event shows `threadAlive=true`; immediate start sees the same live thread |
| B | Old worker cleanup can overwrite a newer run | High | Low | Old worker enters `finally` after a new start request |
| C | First task blocks inside a model SDK request | High | Low | Model-call entry exists without a matching exit for a long interval |
| D | Retry/backoff explains the delay | Medium | Low | Transient error events repeat with growing attempts |
| E | Hero has a stale fixed minimum height | High | Low | Computed height exceeds content height while CSS retains `min-height: 158px` |

## Evidence

Instrumentation added to start/stop, worker entry/cleanup, model call entry/exit, and exception branches.

Pre-fix log evidence:

- Line 3: immediate restart observed `threadAlive=true`, `cancel=true`, `isRunning=false`; restart returned `False`.
- Line 4: the old worker did not enter cleanup until about 2.8 seconds after the rejected restart.
- Lines 5-6: a controlled model call received cancellation after 0.15 seconds but returned only after its full 3-second blocking call.
- The affected persisted session had no model retry entries and only two "previous task still exiting" messages.
- A historical matching OpenRouter text task completed in about 71 seconds, indicating provider latency is possible but not a scheduler deadlock.

Hypothesis status:

- A: CONFIRMED.
- B: CONFIRMED as a race risk if overlapping workers were allowed.
- C: CONFIRMED.
- D: REJECTED for the affected run.
- E: CONFIRMED by CSS `min-height: 158px` after the description was removed.

## Fix

Implemented:

1. Blocking text-model SDK calls now run behind a cancellable polling wrapper.
2. Blocking image API calls, image-result downloads, and image retry waits use the same cancellation path.
3. Stop waits briefly for the workflow worker to clean up; the worker clears its thread reference.
4. Restart attempts reclaim a cancelled worker before deciding that a run is still active.
5. Long model/API calls emit a progress heartbeat every 180 seconds.
6. Hero fixed minimum height was removed; the container now follows content height.

## Verification

Pre-fix:

- Immediate restart: `False`.
- Controlled 3-second API call after stop: returned only after `3.002s`.
- Log line 3: `threadAlive=true` after cancellation.

Post-fix:

- Controlled stop completed in `0.057s`.
- Immediate restart: `True`.
- Real HTTP stop + restart completed in `0.253s`.
- The restarted Mock workflow completed all 5 tasks.
- Text-call cancellation and image-call cancellation regression tests both pass.
- Full suite: 17 tests passed.
- Hero computed `min-height: 0px`; bottom gap equals normal 16px padding.
- Browser console: no errors.

Status remains `[OPEN]` until user verification.
