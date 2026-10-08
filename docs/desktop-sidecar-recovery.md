# Owned desktop sidecar recovery

The Tauri shell starts and supervises only the process created by its own
`CommandChild` handle. It does not adopt a loopback listener, search for a
process by port, or issue a restart through another desktop instance. Every
start receives a fresh instance ID and ownership token.

Windows supervision has two independent exit signals: the shell consumes the
Tauri child event immediately after ownership registration, and a 250 ms
watcher waits on the retained launcher process handle. Both start before the
first `/health` wait, so an early launcher exit cannot be hidden by a
PyInstaller worker that continues serving HTTP. Health acceptance checks the
token, instance ID, port, backend PID, and exact launcher PID, then verifies
the launcher handle is still live. The verified worker must be a direct child
with the same executable path and is assigned to the launcher's Windows Job
Object before the shell publishes `ready`.

On exit, cleanup first reuses verified Job Object membership. If the launcher
dies before the first internal health poll, the shell retries the
token-authenticated health request to identify and attach that exact worker.
The Job Object uses kill-on-close. Cleanup waits until the retained launcher
and worker process handles are signaled, then confirms
`ActiveProcesses == 0` through `QueryInformationJobObject`.
Only after that proof does recovery start a replacement, preventing an orphan
worker from competing for the same database. If worker identity or Job Object
termination cannot be proven, the UI remains degraded and further automatic
or manual starts are blocked for that app instance; it will not select another
port and launch a competing process.

An initial retryable start failure, unexpected owned-child exit, or three
consecutive health/ownership misses schedules one automatic recovery worker
when the old process tree can be proven empty. It permits at
most three attempts with delays of 1, 2, then 4 seconds. The UI reports
`recovering` and the current attempt; after exhaustion it remains `degraded`
with a manual-restart reason. If ownership cannot be proved, restart remains
blocked and the degraded status includes that reason. Recovery uses a single-flight guard. Manual
restart and explicit stop/exit invalidate the old generation, so stale health
waiters and retry workers cannot publish `ready` or compete with a new child.
Lifecycle locks cover only spawn/registration and generation/ownership state
transitions; health checks, backoff, and process termination run outside them.

Recovery restarts the backend process only. It does not call
`/monitoring/resume`, `/monitoring/start`, or any AI-session control route.
Backend startup continues to apply existing persisted opt-in/resume rules; an
explicit Stop clears resume eligibility, so a `STOPPED` session remains
stopped.

Shutdown uses `CommandChild.kill()` for the exact launcher handle and the
Windows Job Object for verified descendants. There is no bare `taskkill /PID`
command, so no numeric PID is used as a kill target. The launcher is assigned
to its Job Object immediately after spawn; PyInstaller workers are attached
after their authenticated health identity is verified. If that short interval
cannot be reconciled safely, recovery fails closed rather than killing an
unverified process. No foreign listener is adopted or terminated.

Rust unit tests cover bounded retry timing, cancellation, stale-generation
fencing, and exact health-launcher identity. Desktop security tests check that
exit monitoring starts before health polling and that worker cleanup is gated
by process ownership. These tests do not replace an installed-package smoke
test; no Live trading action is part of this lifecycle.
