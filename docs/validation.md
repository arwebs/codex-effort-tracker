# Validation and operating boundaries

The initial implementation and export-recovery changes were validated on Windows with Python 3.14. Source was transferred to this dedicated repository without moving or reinstalling the running collector.

## Automated checks

The automated suite covers:

- Inherited child history boundaries, duplicate usage records and message provenance.
- Own/parallel agent intervals, session gaps, lower bounds and missing terminal events.
- Foreground attribution ambiguity, idle thresholds, focus changes and observation gaps.
- Unknown/zero values, CSV formula safety and atomic CSV/dashboard replacement failures.
- Malformed hooks, timestamp range failures, retryable reads and preserved quarantine bytes.
- Compressed-source identity and incremental behavior; corrupted/truncated archive rollback.
- Home/drive-root exclusions and real Windows process-tree timeout cleanup.
- Shared snapshot reuse, failure caching and writer-lock release between workspaces.
- Hook merge idempotence and single-instance collector locking.
- Malformed nested transcript payload isolation, warning counts and subsequent exports.
- Persistent collection-start cutoffs across backfill, restarts and installation receipt changes.
- Clearing stale workspace measurements on direct and cached scan failures.

The PR review revision passed all 41 tests on Windows with Python 3.14. The README settings, first reconciliation, status and dashboard-copy commands were also executed in an isolated source copy against synthetic metadata and a transcript: one thread, one turn and 110 tokens exported with no read errors. This check did not install hooks, start an observer or access personal Codex data.

## Browser and live recovery checks

The standalone page was tested directly through a local file URL. Browser checks covered Monday-start weeks, day/week/month navigation, leap months, daylight-saving boundaries, cross-midnight time allocation, unknown/zero handling, filters, details, escaping, focus handling, failed loads and responsive overflow.

During the October 29, 2026 recovery, a malformed hook record was preserved in quarantine and compressed transcripts became readable. Recovery imported the backlog without transcript read errors. Subsequent full and automatic export cycles completed, and exactly one persistent collector remained running.

A browser automatically advanced to a newer background capture while retaining the selected week and search filter, with no page errors. CSV capture timestamps matched the dashboard snapshot and the hook queue was empty. These are historical validation results, not a live status display; captured activity statistics remain outside this repository.

## Limits

Human timing cannot identify a selected chat when Windows exposes only an ambiguous host window. Per-thread human values therefore remain unknown in that environment. Code changes are workspace observations, not proven authorship. Missing historical samples cannot be reconstructed.

The Startup launcher was exercised locally; actual future sign-in, lock/suspend behavior and installation on another machine remain environment-specific checks. The installer preserves Codex's normal trust boundary. Live source transcripts and databases are read only, and no captured data is included in this repository.
