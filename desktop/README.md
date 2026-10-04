# Desktop alpha development and acceptance

The desktop shell is **1.1.0-alpha.1**, not a published stable release. It is a
real Tauri 2 WebView window around the existing Python/curl/Mihomo engine. The
legacy CLI/Web launchers stay available. No pip package is needed at runtime.
The current native acceptance evidence is Windows only; merely providing
macOS/Linux build jobs is not proof that those packages run successfully.

## Packages and system requirements

- Windows x86-64: EXE plus `app/`, `runtime/`, `manifest.json`; portable ZIP and
  NSIS per-user installer. **Keep the complete directory**. Not a single-file
  runtime-free executable. Windows 10/11, system curl and Microsoft WebView2
  are required. The installer offers the official WebView2 downloaded
  bootstrapper if missing (Internet required). Portable/offline systems need
  WebView2 already installed. The Python runtime is bundled, not system PATH.
- macOS: separate Intel/Apple Silicon CPython runtime and app/DMG configuration;
  minimum declared desktop OS 14.0. System curl required. Native build/run and
  Gatekeeper acceptance are still pending; unsigned is not notarized.
- Linux: x86-64/aarch64 runtime and Debian packaging, built against Ubuntu 22.04.
  GTK3, WebKitGTK 4.1, appindicator, curl and compatible glibc required. Other
  distributions are not automatically compatible. Native acceptance pending.

All current packages are **unsigned**. No automatic update plugin is enabled.
Official release links support manual upgrade; there is no silent download or
installation. Settings now offers an **explicit click-to-check stable Release**
using the fixed public GitHub endpoint documented at
https://docs.github.com/en/rest/releases/releases#get-the-latest-release.
No credentials/history are sent and no draft/prerelease/body/asset URLs are
trusted. Success is cached in memory for 15 minutes (failures for 1 minute);
timeouts, unavailable/invalid metadata and rate limits mean **cannot confirm**.
An alpha ahead of stable is not "latest stable" and is never auto-downgraded.
The check does not query alpha updates, verify signatures or download a file.
Back up while all writers are stopped, choose the correct platform/architecture
on official Releases, verify SHA-256, then upgrade manually. A checksum is not
a digital signature. Unsigned packages and manual rollback remain explicit.

Runtime artifact URLs and SHA-256 are fixed in
`runtime-lock.json`. Version/platform/runtime/source revision/dirty status are
recorded in manifest and `build-provenance.json`. A source-dirty build must not
be mislabeled as an official Release. Update pinned runtimes deliberately for
security maintenance; recheck upstream hashes, tests and packages.

## Explicit Verge root selection

Shared Settings accepts a local absolute Verge configuration directory through
authenticated preview/confirm endpoints. Only the fixed `clash-verge.yaml`,
`profiles.yaml`, `profiles/` layout is checked; preview does not prove controller
connection or subscription attribution. There is no generic file read/export
endpoint and no native folder picker yet. UNC/Windows mapped network drives,
out-of-root file links and oversized fixed documents are rejected.

Controller discovery, catalogue attribution and worker configuration use the
same root; accepted jobs carry a private frozen snapshot, not a command-line
argument or persisted task setting. Active/cancelling/cleanup jobs prevent root
changes. A broken explicit root fails closed, never falling back to another
controller or directory. Changing roots clears the pending node selection;
history and favorites survive, but IDs in another root namespace are not guessed.

The choice is backend-session memory only, never localStorage or exported
preferences/history. Reloading the page preserves it; restart reads the startup
`SPEEDBENCH_VERGE_ROOT` environment override or uses automatic discovery.
Resetting to auto affects this backend only, not that environment variable.
`SPEEDBENCH_HOME` selects the SpeedBench data directory, not the Verge root.
Keep paths/configuration private when sharing diagnostics.

## Build (developer machine only)

Node 24.20, Rust 1.97 and platform Tauri build dependencies are development
tools, not end-user requirements. Dependency locks must be used. Example from
the repository root on Windows (use `npm.cmd` if PowerShell blocks npm.ps1):

```powershell
npm.cmd ci --prefix desktop --ignore-scripts
cargo fetch --locked --manifest-path desktop/src-tauri/Cargo.toml
python desktop/prepare_resources.py --target windows-x86_64
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml
npm.cmd --prefix desktop run tauri -- build --ci --no-sign -- --locked
python desktop/package_windows.py
python desktop/collect_artifacts.py --target windows-x86_64
```

Stage in a fresh checkout when changing platform/runtime. Scripts refuse stale
unexpected source files, changed cached hashes, traversal and external archive
links. ZIP/native outputs live under ignored `dist/`/`target/`, not Git. Portable
packaging refuses to overwrite an existing verified ZIP. Rust embeds the
manifest: changing the JSON beside the EXE cannot authorize altered code.
`--verify-package` is a Windows read-only diagnostic (exit 0/2), does not start
WebView/backend and validates the complete package next to that executable.

No user history, seed, controller/provider key, logs or subscription config is
copied into resources. Project license, CPython runtime license and locked Rust
dependency license notices are included. Five native CI build jobs coexist with
the existing six Python 3.9/3.12 jobs; they do not publish Releases.

## Lifecycle and privacy

Private inherited stdin/stdout bootstrap validates app/version/protocol,
parent/child PID, instance ID and fresh nonce, then verifies authenticated HTTP
identity on a random `127.0.0.1` port. Nonce/write token never enter argv, URLs,
logs or localStorage; public JSON identity does not return either. The shared
same-origin HTML retains its existing `sb-token` meta tag for authenticated
POSTs. That local write token is not a provider/controller API Key. Provider
credentials remain solely in Python. The renderer has **no generic shell,
filesystem or remote IPC grant**.
Navigation/new-window restrictions and a CSP apply to the shared UI; only the
two fixed ipify exit endpoints are allowed as cross-origin browser fetches.

Window close hides to tray; tray offers open/status/cancel/exit. Exit requests
cancel/cleanup before backend shutdown. Timeout is a failure, not clean success.
Windows Job Object owns the backend and descendants before bootstrap; no global
process-name kill. POSIX uses a dedicated process group and parent EOF cleanup;
platform crash/forced-signal acceptance is still pending.

A kernel-held, private `backend-owner.lock` coordinates desktop and standalone
Web using the **same data directory**, even on different ports. Metadata alone
does not prove a live owner; the lock inode is not deleted. Existing-owner
startup fails safely instead of attaching to an unverified service. Standalone
CLI now acquires ownership before controller/identity/history work, coordinating
both the explicit history parent and `SPEEDBENCH_HOME` if different. Close an idle
backend before starting a direct CLI using its directory; help/invalid arguments
do not acquire ownership. Different directories are not globally exclusive.

Backend-created benchmark children use a bounded private stdin frame matching
the actual parent PID, instance, canonical history directory and kernel-held
owner. No flag or environment variable alone bypasses this check. A separate
private `benchmark-writer.lock` remains held until child reporting/cleanup ends;
it blocks a new backend after a parent crash. Parent pipe EOF requests graceful
cancellation. Neither bootstrap frame nor credentials are logged or put in argv.
If a failed transport cannot reap the exact child handle within 8 seconds, the
backend retains that handle, blocks new tasks and reports incomplete cleanup.
There is no arbitrary PID/process-name kill. Lock files remain after release;
metadata is not a substitute for the kernel lock. On exit, unstarted Intelligence
queries are cancelled and running cache writers are joined before lease release.
In-flight service latency is still subject to provider timeouts, not a new hard
all-phase deadline. Forced termination does not
guarantee a final partial report; all-phase cancellation acceptance is pending.

Desktop theme/profile/mode/favorites and notification choice live in private
`ui-preferences.json` with an explicit non-secret whitelist. Browser Web UI
continues localStorage; the desktop cannot read another browser's preferences.
No provider/controller Key is persistable there. Notifications default off and
contain only mode/status/node count; system notification visibility still needs
native manual acceptance. Corrupt preferences are preserved, not reset silently.

Desktop OS external actions accept **only four symbolic choices**: BrowserLeaks
DNS, DNSLeakTest, official Releases, and this instance's loopback browser-audit
page. Only Rust opens these fixed URLs; there is no arbitrary URL/path/command.
The browser-audit page is the sole fixed loopback HTTP exception to HTTPS links.
Requests are bounded, expire and get a private acknowledgement. An opened URL
is not a successful leak audit. WebView audits are labeled separately from
Chrome/Edge/Firefox; missing WebRTC capabilities mean **unable to confirm**.
DNS remains guided, with no scraping/system-config claim of leak safety.

## Data, migration and rollback

Windows uses `%APPDATA%\ClashSpeedBench`, macOS uses
`~/Library/Application Support/ClashSpeedBench`, Linux uses the platform data
directory plus `ClashSpeedBench`. `SPEEDBENCH_HOME` explicitly selects another
directory; relative values are resolved before starting the backend. Testing
uses temporary data and never replaces the user's Downloads installation.

Close every writer before upgrade/backup. Preserve JSONL, SQLite (or a consistent
SQLite backup including in-flight WAL), `identity-seed` and `ui-preferences.json`.
Do not upload the seed or credentials as diagnostics. New tables/columns are
additive; old raw JSONL/`runs.raw` are not rewritten. Stable IDs need the same
private seed. Old-name-only rows stay legacy/unknown, not guessed subscription
history. Settings now expose the current data directory and the existence of
JSONL/SQLite, without opening raw history or automatically importing other
directories. A detected source-directory history is a hint, not permission to
copy it. Settings now also accepts an explicitly chosen local absolute history
directory. Close all programs using that source, preview the counts/conflicts,
then confirm the merge. It reads only the fixed JSONL/SQLite filenames; JSONL
is authoritative when both exist. The original source is unchanged. Matching
timestamps/task IDs with different content block import; repeated identical
imports are no-ops. Old active task snapshots become interrupted/partial.

Before merging, private `history-import-backups` saves the destination JSONL,
a consistent SQLite snapshot, preferences and seed. Source preferences, seed
and provider caches are excluded. Raw text and existing database IDs survive;
time-based history queries sort by actual instant, with naive legacy timestamps
interpreted in the current machine's timezone. Keep all private backups local.
Preview expires after ten minutes or any observed data change. Settings can
undo the latest import only while destination files still match the completed
transaction; it refuses to erase newer data. Startup reconciles a known pending
transaction before other data writes. Unknown intervening changes or missing
backup files stop recovery; preserve the directory/backup and resolve the
reported conflict before restarting. Do not delete the pending receipt to
bypass the guard. Native package and power-loss acceptance remain pending.

For UI preferences, open Settings in the original browser at its original
SpeedBench address, choose **Export non-secret preferences**, copy the JSON,
then paste it into desktop Settings, preview and explicitly confirm import.
Only theme/profile/mode/target, subscription time range, notification choice
and favorites are accepted. Favorites merge rather than replace; unknown
names/IDs stay pending. Source directory identities depend on the private
seed: exporting favorites does not copy that seed or prove an ID in a different
data directory. API keys/tokens, paths, controller/subscription configuration,
raw history and identity seeds cannot be transferred with this JSON.

Older versions without an export button must first be backed up, then load the
new shared Web UI in the same browser/host/port to expose the existing local
preferences. Do not inspect or copy arbitrary browser profile files. Desktop
imports use an authenticated, atomic backend patch; browser storage failures
attempt to restore the old whitelist values and never report partial success.
History import has temporary-directory, real HTTP and browser acceptance;
full native first-run migration acceptance remains pending. Do not copy whole
browser profiles or imply automatic migration.

Use tray Exit and wait for cleanup **before** reinstalling. NSIS replaces its
default force-close policy with a read-only Restart Manager check: running app
or unverifiable ownership aborts install/uninstall, including silent mode. The
hook has been compiled; interactive installer/race acceptance remains pending.
Portable replacement likewise must be done only after exit. Rollback restores
the old installation after a backup without deleting newer raw history. Never
run old/new writers simultaneously; legacy source entry remains a fallback,
not evidence of native platform acceptance.

Registered worker reaping is now bounded to at most 16 concurrent cleanup
attempts; existing per-process terminate/kill waits overlap instead of summing.
Every attempt is joined and persistent errors are propagated, including dynamic
shard failures. OS calls and directory removal do not have a hard total deadline.
Generated worker directories are explicitly owned: garbage collection cannot
delete a configuration before its process is confirmed reaped. A failed reap
leaves private configuration for explicit recovery, never serial fallback.
CLI exit code 3 denotes incomplete worker cleanup. The backend marks that task
failed even when cancellation was requested, retains already accepted partial
results, and refuses new tasks/root changes in that session. Exit and verify
owned leftover resources before restart; do not kill processes by name or broadly
delete temp directories. Complete all-phase cancellation remains a pending gate.

CLI cancellation (130), cleanup failure (3), and unexpected failure (1) now
attempt to export completed per-node snapshots under the writer lease. This
does not remeasure or auto-switch: a later-round interruption retains earlier
samples, and an IPv6 interruption retains completed IPv4. Partial scope/status
is explicit; missing measurements are not unreachable and unknown IP is N/A.
Failed CSV export still attempts JSONL unless `--no-history` was requested.
Committed history is never rewritten or duplicated; if both exports fail, the
console explicitly says results were not persisted. Forced termination, an
unwritable disk, or repeated interruptions cannot guarantee final persistence.
The packaged fixture checks backend-to-CLI failed partial JSONL/SQLite/task
retention, not real network performance or native GUI lifecycle.

Observed download counters include returned warmup, single-stream, and
same-node multi-stream samples even without a progress transport. An interrupted
curl invocation counts as an attempt, not a success; unreported bytes are never
replaced with its request budget. Serial fallback now emits delay, download,
restore and whole CSV/JSONL summary spans, while worker fallback probes have
separate counters. Overlapping cumulative spans are not additive elapsed time.
The packaged failed-task fixture verifies metric persistence alongside raw
history; this is not a same-coverage real bandwidth performance comparison.

Probes now retain each completed sample, including within an interrupted group.
Optional JSON `probe_sources` separates main-controller, worker-fallback and
serial observations. Requested, invoked and completed counts are distinct:
one success, one failure and a third interrupted call mean two completed
samples and 50% application-level failure, not three failures/attempts in that
denominator. Phase-span attempts still count all invoked calls. Expanded shared
UI details explain this difference; the CSV primary counters keep their existing
shape. The job-local legacy row key uses the frozen unique runtime name so
Shadowsocks/ss spelling does not duplicate a row; it is not a stable identity or
permission to switch. No SQLite schema or runtime dependency is added.

Main probe-pool cancellation stops unstarted nodes and subsequent samples, then
joins active calls. Scoped TCP/Unix controller probe/readiness reads now poll
their own nonblocking socket without losing buffered HTTP/TLS data or repeating
requests. Restoration writes outside that scope are not blocked by cancellation.
DNS interruption stops queued domains, propagates local stop to owned curl
calls and joins them; the initiating ordinary error is not masked by sibling
abort. Worker startup/readiness check cancellation before claiming ready.
Closing intelligence skips subsequent providers while retaining and joining
current query/cache writers. The development branch now uses per-operation
overlapped I/O for scoped Windows pipe requests, cancelling and confirming
completion before releasing native storage/events. Portable mock tests pass;
real Windows fixture and new packaged acceptance are still pending. Unscoped
pipe requests, connection/TLS establishment and provider urllib retain original
timeouts/system behavior; no universal hard cancellation deadline is claimed.
The portable-package fixture
also exercises the actual bundled probe primitive with synthetic values and
an interrupt, checking cancelled partial JSONL/SQLite/task metrics, raw-history
preservation and ownership release without contacting Clash or third parties.

Task snapshots and history details now preserve numeric-only provider/cache
counters and five first-observed, parent-monotonic milestones. Actual transport
invocations count as API attempts; HTTP 2xx is not usable intelligence or clean
reputation. Key-missing/disabled/cooldown skips do not spend a counted API call.
Adapter signatures are selected before invocation; a body TypeError does not
retry paid I/O. Cache hits/misses count physical lookups, including the race
recheck; unique exit tasks and single-flight reuse are separate counters. Worker
count is cumulative ready starts, not peak concurrency. Provider service time
and final provider_wait overlap other work and cannot be added as elapsed time.

Milestones cover first result, first usable recommendation candidate in measured
scope/current target, network completion, intelligence completion and confirmed
cleanup. They are not a new ranking or a global fastest-node claim. Unreached
milestones remain absent/N/A; failed cleanup does not get a completion marker.
The existing task_metrics counters_json also holds a milestones phase row;
no schema change or runs.raw rewrite occurs, and stale checkpoints cannot drop
or overwrite first observations. The packaged fixture runs the real coordinator,
cache, provider parser and report through backend-to-CLI delegation with a fake
transport: two nodes/same exit, cold then hot cache, one API call, persisted
milestones and no canary Key in public state/history/cache/CSV. It does not
exercise native GUI lifecycle, real bandwidth performance or paid APIs.

The packaged socket fixture uses an ephemeral loopback HTTP observer (not Verge)
and the actual CLI cancel file. A stalled Connection: close body is interrupted,
restoration PUT remains possible, and task/JSONL/SQLite retain a partial invoked
probe with zero completed samples (failure rate N/A), confirmed cleanup and
unchanged original raw. The owned server/threads are joined, no real node or
external network is queried. Implementation uses standard-library nonblocking
socket I/O and Executor shutdown, per the Python documentation:
https://docs.python.org/3/library/socket.html and
https://docs.python.org/3/library/concurrent.futures.html .
