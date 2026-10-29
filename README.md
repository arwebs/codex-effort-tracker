# Codex effort tracker

A local Windows collector and standalone dashboard for understanding time and effort across Codex threads and agents. It exports CSVs and a local dashboard snapshot without sending captured activity to a server. Python 3.14+ is required; the collector uses only the Python standard library.

## Features

- Thread, parent and root identities; project names, models and reasoning effort where available.
- Own agent active time, measured idle, provisional trailing idle, work sessions and unknown gaps.
- Turns, messages by provenance, compactions, prose words and recorded token usage.
- Child-agent counts and effort, keeping parallel effort separate from elapsed tree time.
- Estimated human active, idle, away and unavailable time, attributed only when the selected thread can be identified.
- Hash-only Git checkpoints for net workspace code changes, with thread authorship left unknown.
- Daily, weekly and monthly dashboard views, search, filters, details and CSV downloads. Weeks start on Monday; the calendar uses America/New_York.

## Requirements

- Windows, running under the same user account as Codex.
- Python 3.14 or later, with `python.exe` and `pythonw.exe` in the same installation. Python's `compression.zstd` module is needed for compressed transcripts.
- Git on PATH for optional code checkpoints.
- Locally available Codex metadata and session files. These are internal formats and can change between Codex releases; unavailable fields remain unknown.
- A browser that allows an HTML file to load a script from the same local folder. No web server, CDN or third-party chart library is required.

## Quick start

Use a durable checkout rather than a temporary review worktree. Installed hooks and the Startup launcher reference absolute source and configuration paths.

### 1. Get the source and check Python

```powershell
git clone https://github.com/arwebs/codex-effort-tracker.git
Set-Location codex-effort-tracker
python --version
python -c "import compression.zstd; print('Zstandard support is available')"
```

If the repository is private, authenticate with an account that has access before cloning.

### 2. Create local settings

This PowerShell example resolves paths for the current checkout and user. It starts importing threads updated from the time the settings are created:

```powershell
$repoRoot = (Get-Location).Path
$trackerConfig = Get-Content settings.example.json -Raw | ConvertFrom-Json
$trackerConfig.codex_home = if ($env:CODEX_HOME) {
    [IO.Path]::GetFullPath($env:CODEX_HOME)
} else {
    Join-Path $env:USERPROFILE '.codex'
}
$trackerConfig.data_dir = Join-Path $repoRoot 'local/state'
$trackerConfig.output_dir = Join-Path $repoRoot 'local/exports'
$trackerConfig.initial_since = ([DateTimeOffset]::Now).ToString('o')
$trackerConfig | ConvertTo-Json | Set-Content settings.json -Encoding utf8
```

Review `settings.json` before running. Use dedicated absolute directories for state and exports. Set `initial_since` to an earlier ISO 8601 timestamp if you want older threads included immediately; you can also backfill later. A thread selected by this import window can include its older available history.

`settings.json` and the entire `local` directory are ignored by Git. Collection does not require editing Codex's own databases or transcripts.

### 3. Build the first export

```powershell
python run_tracker.py --config settings.json reconcile --skip-code
python run_tracker.py --config settings.json status
```

`--skip-code` avoids Git scans for this pass. Omit it for a full reconciliation. Use Codex and reconcile again if the selected import window has no activity yet.

The collector persists a `collection_started_at` cutoff in its own SQLite state before the first collection. Only root threads with recorded activity at or after this cutoff are eligible for code checkpoints. The history import window is independent of this cutoff: backfilling older conversations does not scan all of their old workspaces. Legacy `installed_at` settings or an existing installation receipt seed the cutoff during migration. Once set, pause/resume, backfill and reinstalling with the same state directory do not reset it.

### 4. Open the dashboard

Copy the static template next to the generated snapshot:

```powershell
$trackerConfig = Get-Content settings.json -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Force -Path $trackerConfig.output_dir | Out-Null
$dashboardPath = Join-Path $trackerConfig.output_dir 'index.html'
Copy-Item dashboard/index.html $dashboardPath
Start-Process $dashboardPath
```

Keep `index.html` beside `dashboard-data.js`. The page loads the latest capture when opened and refreshes its data every minute. Choose Daily, Weekly or Monthly, navigate with the arrows, jump to a date, or return to the current period. Weekly is the default. Search and filters survive data refreshes.

### 5. Choose how to collect

For a background observer without installing persistence:

```powershell
python run_tracker.py --config settings.json resume
```

For persistent hooks and automatic collection at Windows sign-in:

```powershell
python install.py --config settings.json install
```

Installation merges eight owned handlers into Codex's hook configuration, preserves unrelated handlers, retains a backup and adds a hidden per-user Windows Startup launcher. Review new hooks through Codex's normal trust UI when prompted. Installation does not grant hook trust or change sandbox permissions. Transcript reconciliation can still operate before hook receipts are available.

## Everyday commands

Run commands from the source checkout, or supply absolute script/configuration paths.

| Command | Purpose |
| --- | --- |
| `python run_tracker.py --config settings.json status` | Inspect sampler heartbeat, collection cutoff, latest export and queued hook count. |
| `python run_tracker.py --config settings.json reconcile` | Import available updates and produce a full export now. |
| `python run_tracker.py --config settings.json reconcile --skip-code` | Reconcile activity and usage without Git scans. |
| `python run_tracker.py --config settings.json resume` | Clear a persistent pause and start one hidden observer. |
| `python run_tracker.py --config settings.json pause` | Stop sampling and automatic exports after the current work stops. |
| `python run_tracker.py --config settings.json backfill --since '2026-01-01T00:00:00Z'` | Import an explicitly chosen older history window. |

The observer samples every five seconds and waits about a minute between completed export cycles. Initial catch-up and code scans can extend a cycle. Hook receipts can continue queuing while paused.

To restart, request `pause`, allow the existing collector process to exit, then run `resume`. A rapid pause/resume can leave the old process running; the single-instance lock prevents a second collector. Verify a new heartbeat and a fresh export after restarting. A heartbeat alone does not prove reconciliation succeeded.

## Files and data flow

```text
Codex hook receipts + read-only metadata/session files
                         |
                local collector state
                         |
       threads.csv + turns.csv + dashboard-data.js
                         |
               copied dashboard/index.html
```

- `data_dir` contains `metrics.sqlite`, incremental reader state, hash-only baselines, hook receipts, diagnostic files and installation backups.
- `output_dir` contains one CSV row per thread/agent, one per turn, and the script snapshot for the dashboard.
- `dashboard/index.html` in this repository is the template. Copy it into `output_dir` after an HTML update; use a full browser reload to load the new template. The Refresh button only reloads captured data.
- CSV downloads contain all captured history. Dashboard summaries and charts follow the selected period and thread filters; human host estimates follow the period but remain separate from thread filters.

## Interpret the measurements

- **Agent active:** union of a thread's own recorded turn intervals, including tool execution and waits inside a turn. This is wall time, not CPU utilization.
- **Agent idle:** confirmed gaps of up to 30 minutes between turns. Longer gaps are inactive. Missing terminal events produce lower-bound effort and unknown gaps; trailing live idle is separately marked provisional.
- **Parallel agents:** own effort is additive across agents and can exceed elapsed wall time. Descendant-inclusive fields and tree elapsed time answer different questions; do not sum inclusive fields across thread rows. Internal approval agents are excluded from the default task scope.
- **Periods:** time intervals are clipped at Eastern calendar boundaries and split across days or hours. Turns, messages, compactions, chat words and tokens are assigned to the turn's start date and represent full turn records.
- **Human estimates:** foreground/input timing only. Ambiguous ChatGPT/Codex host windows remain unassigned and may include other surfaces. Unobserved time is not reconstructed.
- **Code changes:** net workspace observations after a collection checkpoint. They cannot establish thread authorship; `code_lines_added/removed` remain unavailable. Home-directory and drive-root repositories are excluded. Failed scans clear previous measurements instead of presenting stale counts as current.
- **Tokens:** cached input and reasoning output are subsets of input and output tokens. Subscription quotas and dollar costs are not inferred.
- **Coverage:** a blank CSV cell or dashboard dash means unavailable; zero means measured zero. Timing and token coverage are displayed. Code, relationships and latest status in the details panel are explicitly labeled lifetime metadata.
- **Timezones:** UTC timestamps are authoritative. Local CSV display fields use the Windows timezone; dashboard calendar boundaries use America/New_York.

## Troubleshooting

### Dashboard is stale or empty

Check `status`, its last export timestamp and its read errors/hook warnings. Confirm the selected period and filters include activity. If the sampler heartbeat is fresh but the export is old, inspect `reconcile-progress.json` in `data_dir` and run a manual reconciliation. `--skip-code` helps isolate Git scanning delays.

The page must remain beside `dashboard-data.js`; it retains the last successful view if a reload fails. Confirm both paths match `output_dir`. Old captures can include incomplete coverage even when the collector is healthy.

### A hook or transcript is malformed

Invalid hook JSON/schema/timestamps are retained intact in `quarantined-hooks`; temporary read failures stay queued for retry. Preserve these local files for diagnosis. Plain `.jsonl` and compressed `.jsonl.zst` transcripts share one logical identity. Changed archives rebuild transactionally; corrupt/truncated archives retain prior committed history while other threads can still export. Malformed nested transcript records are skipped, counted as warnings and do not block subsequent records.

`last_error.json` records the most recent fatal reconciliation exception and can predate a later healthy export. Also check `last_sample_error.json`, the latest heartbeat and the stage timestamp in `reconcile-progress.json`.

### Metadata or code checkpoints are unavailable

Run under the account that owns the local Codex files and verify `codex_home`. Python must be 3.14+ to read Zstandard archives. Internal Codex formats can change; unsupported fields and interrupted history remain unknown.

Git must be on PATH for checkpoints. Git scans have bounded process-tree timeouts, share successful and failed repository attempts within a cycle, and release writer locks between workspaces. A scan failure marks the code measurements unavailable while activity exports can continue. Set `code_baselines` to `false` to disable optional code observations.

## Upgrade or uninstall

For an in-place upgrade at the same durable path: pause, wait for the observer to exit, update the checkout, run the tests, copy the new HTML template if needed, then resume. Keep `settings.json` and `data_dir`; they hold installation-specific configuration and the persisted cutoff.

If source/configuration/Python paths will change, uninstall using the old checkout and configuration before installing from the new path. Reuse the existing state directory to retain history and the cutoff. Do not install from a temporary review worktree.

```powershell
python install.py --config settings.json uninstall
```

Uninstall requests a pause and removes only this installation's handlers and unchanged Startup launcher. Collected data and backups are retained. A modified launcher or missing receipt stops removal rather than deleting unrelated content. Remove retained data only after confirming the configured paths and stopping collection.

## Privacy and development

Runtime settings, databases, transcripts, snapshots, CSVs, quarantine files and worktrees are ignored by Git. The repository contains source, synthetic tests, the static dashboard template and an example configuration.

Hook receipts retain lifecycle metadata, IDs, timestamps and paths, discarding prompts and assistant text. Readers compute counts in memory; the journal stores counts and metadata rather than conversation bodies or tool arguments. Human sampling stores no keystrokes, screenshots or other application titles. Code checkpoints store line hashes rather than source text. Thread/project names, paths and usage metadata are still private: do not publish your state or exports with the source.

```powershell
python -m unittest discover -s tests -v
```

Windows CI runs the same suite on Python 3.14. See [validation notes](docs/validation.md) for evidence and limits. Synthetic tests do not establish future sign-in, lock/suspend handling, hook trust or selected-thread attribution on another machine.

## License

[MIT License](LICENSE), copyright 2026 Andrew Webster.
