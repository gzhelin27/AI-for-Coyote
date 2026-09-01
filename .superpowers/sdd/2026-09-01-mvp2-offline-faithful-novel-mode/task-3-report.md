# Task 3 implementation report — offline candidate validation and import

## Status

Implemented and committed as `feat: import Codex story analysis offline`.

## RED evidence

1. `D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_story_offline_analysis -v` failed before implementation with `ModuleNotFoundError: No module named 'backend.story.offline_analysis'`.
2. `D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_story_analysis_store.AnalysisStoreTests.test_inspect_reports_invalid_only_while_it_quarantines_matching_cache -v` failed before the store change with `AttributeError: 'AnalysisStore' object has no attribute 'inspect'`.
3. `D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_story_import_analysis_cli -v` failed before CLI/provenance extraction with `ImportError: cannot import name 'dlc_provenance' from 'backend.provenance'`.
4. The public-domain export test failed before the package export update with `AttributeError: module 'backend.story' has no attribute 'OfflineAnalysisImporter'`.

## GREEN evidence

- Focused required suite: 61 tests passed, 1 Windows symlink test skipped because symlink creation is unavailable in this environment.
- AppState provenance regression suite: 20 tests passed.
- Full discovery suite: 432 tests passed, 1 skipped, in 43.537s.
- `D:\AI-for-Coyote\.venv\Scripts\python.exe -m compileall -q backend tests` exited 0.
- `git diff --check` exited 0.

## Changes

- Added strict offline JSON candidate validation, real-source identity verification, stable-ID checks, bounded parsing, and atomic import in `backend/story/offline_analysis.py`.
- Added local `validate` and `import` CLI commands in `backend/story/import_analysis.py`; DLC identity is always derived from local configuration.
- Added `AnalysisStore.inspect()` with one-observation `invalid` status when it quarantines a corrupt matching cache, followed by `missing`.
- Extracted the byte-compatible shared `dlc_provenance()` helper; AppState and CLI now call it.
- Removed the online whole-book analyzer, its exports, and its dedicated tests. Removed the now-unreferenced structured story-analysis LLM path and context-classification helpers while leaving normal chat/image behavior untouched.
- Added domain/CLI/cache-state regression tests and updated the example story analysis identity.

## Modified files

- `backend/llm.py`
- `backend/main.py`
- `backend/provenance.py`
- `backend/story/__init__.py`
- `backend/story/analysis_store.py`
- `backend/story/offline_analysis.py`
- `backend/story/import_analysis.py`
- `config/config.example.yaml`
- `tests/test_story_analysis_store.py`
- `tests/test_story_offline_analysis.py`
- `tests/test_story_import_analysis_cli.py`
- Deleted: `backend/story/analyzer.py`, `tests/test_story_analyzer.py`

## Commit

`feat: import Codex story analysis offline`

## Self-review and residual risk

- Validation is fail-closed: malformed, duplicate-key, deep, oversized, extra-field, forged identity, malformed partition, blank summary, and invalid pace candidates cannot create a formal cache.
- CLI success output contains identity/count fields only; errors use a fixed redacted message and do not emit source text.
- No external model, network, device action, push, or tag was invoked.
- Candidate JSON limits are deliberately fixed at 1 MiB, depth 32, and 10,000 aggregate members. Very large but otherwise valid analysis maps must be split into a smaller candidate representation rather than weakening the import boundary.

## Fix round 1/5

### RED evidence

- New source-identity tests failed while `source_sha256` used original bytes: LF/CRLF, BOM, GB18030, and DOCX metadata-equivalent content produced different identities.
- Candidate-directory tests failed because `OfflineAnalysisImporter` did not accept or enforce `candidate_directory`; CLI accepted arbitrary files.
- Huge integer pace tests exposed `OverflowError` from `math.isfinite`; cache inspection did not quarantine the corrupt entry.
- Escaped surrogate and model-string tests showed Python strings could pass validation without strict UTF-8 round-tripping.
- Pace tests showed 0.249 and 4.001 were accepted before the shared model range was imposed.

### GREEN evidence

- `tests.test_story_source tests.test_story_offline_analysis tests.test_story_analysis_store tests.test_story_import_analysis_cli`: 70 passed, 2 skipped only where symlink privilege is unavailable.
- `tests.test_app_state_timeline`: 20 passed.
- `compileall -q backend tests` and `git diff --check` exited 0.

### Fixes

- `ImportedStory.source_sha256` now documents and computes SHA-256 over normalized UTF-8 `text`; raw source bytes remain intact.
- Added and validated `story.candidate_dir`, with importer- and CLI-level regular-file, containment, symlink, junction, reparse-point, and read-race checks.
- Bounded shared `StoryScene.pace` to inclusive 0.25–4.0 and converted untrusted Unicode/numeric/decode exceptions into typed importer, CLI, or cache-inspection outcomes.
- Enforced strict UTF-8 round-tripping for candidate and model strings.

### Residual risk

- Candidate files must now be copied beneath `data/story_candidates/`; external paths are intentionally rejected. Windows symlink tests remain environment-skipped when the OS denies symlink creation, while the junction test passes.

## Fix round 2/5

### RED evidence

- `D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_story_offline_analysis tests.test_story_import_analysis_cli -v` failed before the implementation: a `data/story_candidates/candidate.json` map was rejected after changing CWD, and a controlled pathname replacement raised `OfflineAnalysisError("candidate file changed while reading")` rather than retaining the already-opened candidate.

### GREEN evidence

- Focused source/store/importer/CLI/AppState-provenance suite: 93 passed, 4 skipped only for unavailable Windows symlink privilege or Windows' non-replaceable opened-file semantics.
- Full `unittest discover -s tests -q` suite passed (444 tests, 4 platform skips).
- `D:\AI-for-Coyote\.venv\Scripts\python.exe -m compileall -q backend tests` and `git diff --check` exited 0.

### Fixes

- Resolve relative candidate map paths lexically from the explicit repository `project_root`, so CLI `--map data/story_candidates/<hash>.json` is independent of the calling CWD.
- Open a candidate exactly once, validate its `fstat` regular-file type and the kernel-resolved opened-handle target against a stable trusted candidate-root snapshot, then read only that handle with a fixed byte limit. Redirected or changed roots fail closed; unavailable opened-handle resolution is surfaced as the typed offline-import error.
- Added deterministic CWD and pathname-swap regressions, including a symlink-swap case when the platform permits it.

### Self-review and residual risk

- The importer no longer relies on a check/read/check identity comparison. On this Windows host the C runtime denies replacement of an already-opened file, so the two true replacement-race tests skip after documenting that OS behavior; on replace-capable platforms they exercise the verified-handle read path. The existing Windows junction regression passes and static symlink coverage remains privilege-gated.
- No external model, network, device action, push, or tag was invoked.

## Fix round 3/5

### RED evidence

- `D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_story_offline_analysis.OfflineAnalysisImporterTests.test_validation_rejects_nested_candidate_path tests.test_story_offline_analysis.OfflineAnalysisImporterTests.test_validation_rejects_candidate_path_traversal tests.test_story_offline_analysis.OfflineAnalysisImporterTests.test_validation_fails_closed_when_pinned_candidate_root_is_replaced -v` failed before this implementation: all three inputs were accepted.

### GREEN evidence

- Focused importer/CLI suite: 20 passed, 4 skipped only for unavailable Windows symlink privilege or Windows' non-replaceable opened-file/directory semantics.
- Source/store/importer/CLI/AppState-provenance regression suite: 96 passed, 5 platform skips.
- Full command `D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest discover -s tests -q`: 447 passed, 5 skipped, in 43.517s.
- `D:\AI-for-Coyote\.venv\Scripts\python.exe -m compileall -q backend tests` and `git diff --check` exited 0.

### Fixes and modified files

- `backend/story/offline_analysis.py`: restrict candidates to direct basename children; pin the candidate-root directory as an opened kernel handle (identity and final path); use POSIX `openat`/`O_NOFOLLOW`, and Windows `CreateFileW` directory handles without delete sharing before opening the basename. Candidate and root handles are re-verified before and after the bounded read.
- `tests/test_story_offline_analysis.py`: added nested-path, traversal, root-replacement, and corrected moved-open-file fail-closed regressions.

### Self-review and residual risk

- No post-open verification relies on resolving the configured root pathname: POSIX reads the basename through the pinned directory descriptor; Windows keeps the root handle open without delete sharing, then opens the basename while that root cannot be replaced. Platforms without required safe directory primitives return the typed offline-import failure.
- The replace-race tests skip on this Windows host because the platform blocks the attempted replacement while the verified handle is open; they run on replace-capable platforms. No external model, network, device action, push, or tag was invoked.

## Fix round 4/5

### RED evidence

- The new cross-platform regression that simulates the POSIX `os.open("candidate.json", dir_fd=pinned_root_fd)` call shape failed against the old absolute-path-only controlled-swap predicate with `AssertionError: False is not true`.

### GREEN evidence

- The exact controlled-swap regression passes and proves that the basename is accepted only with a directory descriptor whose `(st_dev, st_ino)` matches the pinned candidate root; the same basename under a different directory descriptor and an unrelated basename are rejected.
- Focused source/store/importer/CLI/AppState-provenance suite: 97 passed, 5 skipped only for unavailable Windows symlink privilege or Windows' non-replaceable opened-file/directory semantics.
- Full `unittest discover -s tests -p "test_*.py"` suite: 448 passed, 5 skipped, in 34.428s.

### Fixes and modified files

- `tests/test_story_offline_analysis.py`: make the controlled race hook recognize both the Windows absolute candidate open and the POSIX basename plus verified pinned-root `dir_fd`, without matching another basename or directory; assert typed `OfflineAnalysisError` and confirm the swap occurred for both moved-file and unlink-to-symlink races.
- No production implementation was weakened or changed; the existing opened-handle containment checks remain the behavior under test.

### Self-review and residual risk

- This Windows host has no installed WSL runtime and denies replacement of the opened candidate with `WinError 32`, so both true filesystem races retain explicit platform skips. The new non-skipped regression uses real directory handles to cover the exact POSIX `dir_fd` hook shape locally; replace-capable POSIX hosts execute the full swap and typed fail-closed assertions.
- No external model, network, device action, push, or tag was invoked.
