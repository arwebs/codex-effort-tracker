# Codex effort tracker

A local Windows collector and standalone HTML dashboard for Codex thread and agent effort. It reads local session metadata and transcripts, records foreground host activity estimates, and exports CSVs plus a dashboard snapshot. Python 3.14 or later is required; there are no third-party Python dependencies.

## What it records

- Thread, parent and root IDs; thread/project names where available; model and reasoning effort.
- Own agent active time, measured idle, provisional idle, unknown gaps and work sessions.
- Turns, messages by provenance, compactions, prose words and recorded token usage.
- Child-agent counts and effort, with parallel effort kept separate from elapsed tree time.
- Estimated human active/idle/away/unavailable time, attributed only when evidence identifies the selected thread.
- Net workspace code-line observations from a hash-only checkpoint, with thread authorship left unknown.

The dashboard provides daily, weekly and monthly views. Weekly is the default, with Monday-Sunday weeks in America/New_York. Previous/next arrows and a date picker select a period; automatic refresh preserves the period and filters.

## Configure a checkout

Use Windows with `python` resolving to Python 3.14 or later, including the standard-library `compression.zstd` module. Git must be on PATH for optional code baselines. Run the following commands from the repository root:

```powershell
Copy-Item settings.example.json settings.json
```

Edit the ignored `settings.json` file:

- Set `codex_home` to your local Codex directory.
- Set `data_dir` and `output_dir` to dedicated absolute paths. The example uses a `local` folder inside this checkout, which is ignored by Git.
- Choose `initial_since` explicitly. Threads updated since this date are imported with their available history; a resumed older thread can include older turns.

Keep the source checkout, settings and state directory in place after installation: persistent handlers reference their absolute paths. Do not use a temporary review worktree as an installation directory.

## Run and inspect

```powershell
python run_tracker.py --config settings.json reconcile
python run_tracker.py --config settings.json status
python run_tracker.py --config settings.json resume
python run_tracker.py --config settings.json pause
python run_tracker.py --config settings.json backfill --since '2026-10-01T00:00:00-04:00'
```

`resume` clears a persistent pause and starts one hidden collector. `pause` stops sampling and automatic exports; hook receipts can still queue until reconciliation. The observer samples every five seconds and waits about a minute between completed export cycles. Initial catch-up and code scans can extend a cycle.

For a restart, request `pause`, allow the current cycle to stop, then run `resume`. Check both the heartbeat and the last export timestamp: a live sampler alone does not prove fresh exports. The private `reconcile-progress.json` identifies the last reached stage. `last_error.json` can predate a later healthy export.

## Open the dashboard

After the first export, copy the dashboard template next to the generated data:

```powershell
$trackerConfig = Get-Content settings.json -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Force -Path $trackerConfig.output_dir | Out-Null
$dashboardPath = Join-Path $trackerConfig.output_dir 'index.html'
Copy-Item dashboard/index.html $dashboardPath
Start-Process $dashboardPath
```

Keep `index.html` alongside `dashboard-data.js`. The page loads the latest local snapshot on open and refreshes every minute. No server, CDN or network connection is required. Refresh updates the data; reload the browser after replacing the HTML template. Download links export all captured history as `threads.csv` and `turns.csv`.

## Install or remove persistence

```powershell
python install.py --config settings.json install
python install.py --config settings.json uninstall
```

Installation merges eight owned handlers into Codex's hook configuration, preserving unrelated handlers and retaining a backup. It adds a hidden per-user Windows Startup launcher. Review new hooks through Codex's normal trust UI when prompted; installation does not grant hook trust or alter sandbox permissions.

Uninstall requests a pause and removes only this installation's handlers and unchanged launcher. It retains state, CSVs and backups. If the launcher changed or its receipt is missing, removal stops rather than deleting unrelated content.

## Interpret the measurements

- Agent active time is the union of the thread's own recorded turn intervals, including tool execution and waits inside a turn. Do not sum descendant-inclusive columns across threads.
- Confirmed gaps of up to 30 minutes are measured idle; longer gaps are inactive. Missing terminal events produce lower-bound effort and unknown gaps. Trailing live idle is separate and provisional.
- Time intervals are clipped to the selected Eastern calendar period and split across days or hours. Turn counts, messages, compactions, chat words and tokens are allocated to the turn's start date and represent full turn records.
- Internal approval agents are excluded from the default task scope and can be selected separately.
- Human estimates use observed foreground/input timing, never input contents. Ambiguous ChatGPT/Codex host windows remain unassigned and may include other surfaces. Unobserved time is not reconstructed.
- Code counts are net workspace observations from a checkpoint after collection begins. `code_lines_added/removed` remain unavailable because the current adapter cannot prove thread authorship. Home-directory and drive-root repositories are excluded.
- Cached input and reasoning output are subsets of input and output tokens. Subscription quotas and dollar costs are not inferred.
- A blank CSV cell or dashboard dash means unavailable; zero means measured zero. Timing and token coverage are displayed. Code, relationships and latest status in the detail panel are explicitly labeled lifetime metadata.

## Recovery behavior

Malformed hook records and invalid timestamps move intact to `quarantined-hooks` in the state directory. Transiently unreadable records stay queued for retry, without blocking exports. The adapter reads `.jsonl` and `.jsonl.zst` using one logical source identity; unchanged archives are skipped, and changed archives are rebuilt transactionally. A corrupt or truncated archive retains prior committed history and is reported without preventing other threads from exporting.

Git scans use bounded process-tree timeouts, share repository snapshots during a cycle and cache failures to avoid repeated delays. Short metadata commits keep sampling independent of slow scans. Failed code scans mark those measurements unavailable.

## Local data and version control

Runtime settings, databases, transcripts, generated snapshots, CSV exports, quarantine files and worktrees are ignored. The repository contains only source, synthetic tests, the static dashboard template and an example configuration.

Hook receipts retain lifecycle metadata, IDs, timestamps and paths, discarding prompts and assistant text. Transcript readers compute counts in memory; the journal retains counts and metadata, not conversation bodies or tool arguments. Human sampling records no keystrokes, screenshots or other application titles. Code baselines store hashes rather than source text. Thread/project names, paths and usage records are still private local metadata.

## Validate

```powershell
python -m unittest discover -s tests -v
```

The Windows CI workflow runs the same suite on Python 3.14. See [validation notes](docs/validation.md) for tested boundaries and live recovery evidence. Synthetic checks do not establish Windows sign-in, lock/suspend handling, hook trust or per-thread foreground attribution on another machine.
