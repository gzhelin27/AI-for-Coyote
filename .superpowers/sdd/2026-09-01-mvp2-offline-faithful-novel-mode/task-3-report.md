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
