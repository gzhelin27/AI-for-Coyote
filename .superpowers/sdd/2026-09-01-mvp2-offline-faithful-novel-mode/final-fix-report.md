# MVP2 Offline Faithful Novel Mode — Final Fix Report

Date: 2026-09-01

Branch: `codex/mvp2-faithful-novel-mode`

Implementation commit: `8b6cacac1675acbdc91b257712f273221b8fefc5`

Status: **DONE — final-review findings A–F fixed and verified**

## Scope and constraints

This wave implements the approved final-review findings without adding a second session owner or changing the MVP1 ownership model. Work was performed in the isolated worktree only. No network access, physical-device operation, push, merge, or deployment was performed.

The implementation keeps these invariants:

- `TimelineSessionController` remains the single timeline/replay owner.
- Filesystem work is tracked and runs through `asyncio.to_thread`; event-loop and transition/broadcast critical sections contain only bounded in-memory work.
- Starts follow prepare outside lock → short authoritative revalidation → activation.
- Finishes follow authoritative output disable/freeze → off-lock persistence → short finalization, with a non-runnable finishing state throughout persistence.
- Analysis cache operations remain rooted in one pinned directory handle and fail closed if that root identity changes.
- Every externally emitted full state gets a unique monotonic revision while the state is constructed under the broadcast/snapshot lock.
- Novel replay archives prove semantic identity by re-importing embedded source bytes at the archive boundary.

## Finding A — analysis I/O and formal cache limit

### RED evidence

The new tests failed against the pre-fix implementation because cache reads/writes had no single formal byte ceiling, analysis inspection could execute blocking cache/provenance reads on the event loop or while the transition lock was held, and a just-imported source could broadcast `missing` even though the import response had already validated `ready`:

- `test_inspect_quarantines_otherwise_valid_cache_above_formal_byte_limit`
- `test_save_rejects_story_map_above_formal_byte_limit_without_partial_cache`
- `test_state_snapshot_stalled_analysis_keeps_event_loop_responsive`
- `test_play_stalled_analysis_never_holds_transition_lock`
- `test_play_provenance_and_analysis_io_never_run_on_loop_or_under_lock`
- `test_import_broadcast_reuses_the_validated_ready_lookup`

### GREEN implementation and evidence

`AnalysisStore` now enforces a strict 1 MiB serialized-cache limit with bounded reads and a bounded UTF-8 writer. Oversize input is typed/quarantined and oversize output cannot leave a partial cache or temporary file. `AppState` tracks all production analysis/provenance/cache work through `asyncio.to_thread`, publishes validated lookups into an in-memory latest-value cache, and uses only that cache for safety-sensitive broadcasts. HTTP state and the initial WebSocket snapshot may refresh off-loop before entering the snapshot lock.

Novel play performs inspection before taking the transition lock, then revalidates source identity, source generation, analysis key, runtime signature, idle state, and emergency-stop state inside a short lock before starting. Planning is followed by the same off-lock inspect/short-lock revalidation boundary. The focused analysis/store and story endpoint tests, the 165-test story/timeline/session concurrency aggregate, and the full 575-test backend suite pass.

## Finding B — replay ZIP I/O, start, and finish lifecycle

### RED evidence

The added probes exposed blocking replay list/download/load/save work, replay-player construction inside the global transition section (the OS-thread probe stalled for approximately 250 ms), cancellation that could leave finishing stuck, and prepared replays that could still activate after disconnect, idle stop, or shutdown:

- `test_stalled_replay_list_and_download_keep_event_loop_responsive`
- `test_stalled_replay_load_never_holds_global_transition_lock`
- `test_replay_player_preparation_runs_before_the_short_start_lock`
- `test_stalled_finish_save_clears_then_releases_global_transition_lock`
- `test_cancelled_stalled_finish_still_finalizes_after_archive_save`
- `test_history_finish_archive_save_never_holds_global_lock`
- `test_disconnect_invalidates_a_replay_prepared_while_idle`
- `test_idle_stop_invalidates_an_already_prepared_replay`
- `test_shutdown_state_rejects_replay_activation_after_prepare`

### GREEN implementation and evidence

Replay list, validated download/read, load, and save now run as tracked off-loop store operations. Start prepares a fully validated immutable bundle, player, cursor, provenance result, and routing generation outside the global lock; activation is a short critical section that checks idle/estop/shutdown/generation before transferring the already-prepared player to the existing owner. Disconnect and stop always advance routing generation, including from idle, so an older prepared bundle cannot activate.

Finish first quiesces the runner, authoritatively clears/disables output, freezes the archive payload, and marks persistence pending. It then releases the global lock for the tracked save and reacquires only a short finalization lock. Cancellation is shielded through this lifecycle, failures leave output disabled and controller state safe, and atomic store semantics prevent partial archives. Estop/disconnect/shutdown tests remain responsive under stalled store probes. Timeline, game-loop, application-state, session endpoint, and full backend suites pass with no task-leak assertions.

## Finding C — AnalysisStore TOCTOU closure

### RED evidence

Controlled root replacement tests demonstrated that validating an analysis pathname and reopening it later could cross into a same-name replacement directory. Additional probes covered stale-temp enumeration and directory durability on Windows:

- `test_pinned_analysis_root_rejects_same_name_swap_during_read`
- `test_pinned_analysis_root_rejects_same_name_swap_during_save`
- `test_windows_stale_cleanup_never_reopens_the_analysis_root_path`
- `test_windows_save_flushes_the_pinned_directory_handle`
- existing symlink/junction, collision, concurrent-writer, and quarantine tests

### GREEN implementation and evidence

The store pins the analysis-root handle and identity once, accepts only direct cache-child names, and uses no-follow handle-relative operations for bounded read, quarantine, temporary creation, cleanup, and commit. Windows uses relative native handle operations; POSIX uses `dir_fd`/no-follow operations. Commit is atomic, uses the required no-replace/replace behavior, fsyncs the file and pinned directory, and cleans up only the transaction's own temporary file. Root same-name/junction/symlink swaps fail closed instead of reopening a checked pathname. The focused store suite ran 34 tests with 33 passing and one privilege-dependent symlink skip; the Windows root-swap and junction tests passed.

## Finding D — unique snapshot revisions

### RED evidence

Interleaving an older blocked WebSocket send with a newer HTTP state read showed that different full-state bodies could previously share a revision, so the client could not reliably reject the late old frame:

- `test_full_state_broadcasts_are_serialized_with_monotonic_revisions`
- `test_http_snapshot_gets_unique_revision_while_older_ws_send_is_blocked`
- frontend `state synchronization rejects stale HTTP and WebSocket snapshots across reconnect epochs`

### GREEN implementation and evidence

HTTP `/api/state`, the initial WebSocket state, and every broadcast now allocate one unique monotonic revision and build the corresponding body under the same broadcast/snapshot lock. Network sending remains outside the lock. Read-only GETs intentionally consume revisions. The backend interleaving tests and frontend stale-frame/reconnect tests pass; a blocked old WebSocket frame is rejected after the newer HTTP snapshot is accepted.

## Finding E — archive semantic source identity

### RED evidence

Archives could retain valid raw ZIP checksums while replacing embedded source bytes and recomputing manifest checksums, because the source was not re-imported and compared to StoryMap identity:

- `test_novel_save_reimports_source_and_requires_semantic_identity`
- `test_novel_save_enforces_docx_auto_encoding_boundary`
- `test_load_rejects_replaced_source_after_raw_checksums_are_recomputed`

### GREEN implementation and evidence

Novel save/load now pass embedded raw bytes, metadata `source_encoding`, and the source extension through the bounded `StorySourceLoader` boundary. DOCX requires `auto`. The normalized text hash must equal both metadata `source_text_hash` and StoryMap `source_hash`, and normalized length must equal StoryMap `text_length`. Raw manifest member checksums remain an independent integrity layer. Validation is local to the archive boundary to avoid an import cycle. Legacy non-novel replay loading remains compatible. All 37 replay-store tests and the full backend suite pass.

## Finding F — frontend safe error mapping

### RED evidence

The production mapper tests initially reported two failures: newly required story codes fell back incorrectly, and already-safe mapped strings were passed through a second whitelist that erased them.

### GREEN implementation and evidence

`frontend/src/api.ts` now maps the complete backend story-code enum, including `story_output_failed`, `story_state_changed`, and `story_planning_cancelled`, to stable user-safe copy. A typed story API error preserves the mapped message directly; unknown codes and raw backend detail still collapse to the generic safe fallback. The production frontend test suite passes all 9 tests and the TypeScript/Vite production build succeeds.

## Final verification

All commands were run after the final production changes:

| Check | Result |
| --- | --- |
| `D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -q` | `Ran 575 tests in 69.409s` — `OK (skipped=5)` |
| Focused story/timeline/session concurrency and resource tests | 165 passed |
| Focused AnalysisStore security/resource tests | 33 passed, 1 privilege-dependent skip |
| Focused game-loop tests | 22 passed |
| Focused application-state/timeline tests | 20 passed |
| `python -m compileall -q backend tests` | exit 0 |
| `npm test` | 9 passed, 0 failed |
| `npm run build` | TypeScript and Vite build succeeded; 1,608 modules transformed |
| `git diff --check` | clean (only Git informational LF/CRLF conversion warnings) |

## Compatibility and residual risk

- MVP1 ownership and legacy non-novel replay behavior are unchanged and covered by the full regression suite.
- Five existing platform/environment tests were skipped by the full suite. One is the Windows symlink-creation privilege path (`WinError 1314`); the corresponding junction and controlled root-swap paths ran and passed. No skipped test represents a known functional failure.
- The native Windows handle-relative implementation is necessarily platform-specific. Its same-name swap, junction, stale-temp, atomic commit, and pinned-directory flush paths are covered locally; cross-platform CI remains useful additional assurance.
- No known task leak, partial archive/cache, unbounded cache read/write, event-loop blocking production store I/O, or unsafe frontend error-detail exposure remains in the covered paths.

## Delivery

Implementation commit: `8b6cacac1675acbdc91b257712f273221b8fefc5` (`fix: close final MVP2 safety review findings`).

This report is intentionally committed separately so it can cite the immutable implementation commit.
