# MVP2 Offline Faithful Novel Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an MVP novel mode that consumes a Codex-generated, locally validated StoryMap and never calls OpenRouter to analyze a whole novel at runtime.

**Architecture:** Preserve the existing safe source loader, immutable story models, and atomic analysis store. Replace the online `StoryAnalyzer` path with a strict offline candidate importer, then layer faithful chapter planning, novel session ownership, typed APIs, and a minimal reader UI above the accepted MVP1 timeline/session safety path.

**Tech Stack:** Python 3.12, FastAPI, immutable dataclasses, strict JSON, unittest, React, TypeScript, Vite, existing MVP1 timeline/replay components.

**Spec:** `docs/superpowers/specs/2026-09-01-mvp2-offline-story-analysis-design.md`

## Global Constraints

- Candidate and analysis files live only under Git-ignored repository-local `data/story_candidates/` and `data/story_analysis/`.
- Effective analysis identity is `source_hash` + producer `codex-offline` + analysis version `faithful-offline-v1` + current stable DLC version.
- Missing or invalid analysis never calls OpenRouter and never returns HTTP 500.
- No web candidate upload/editor, online whole-book analysis, automatic candidate repair, video, camera, microphone, live reaction, manual scene override, or replay-library expansion.
- Story analysis stores offsets, simplified-Chinese summaries, pace, and stable IDs only; runtime planning owns waveform/strength/gap choices.
- Novel playback delegates to the existing `SessionController` and GameLoop output owner. It must not create a second physical-output path.
- Push, tag, paid network calls, and real-device acceptance remain outside automatic execution.
- Every implementation task uses strict test-first development and receives a fresh implementation review before the next task.

---

### Task 3: Offline Candidate Validation and Import

**Files:**
- Create: `backend/story/offline_analysis.py`
- Create: `backend/story/import_analysis.py`
- Create: `backend/provenance.py`
- Modify: `backend/story/__init__.py`
- Modify: `backend/story/analysis_store.py`
- Modify: `backend/main.py`
- Delete after replacement tests pass: `backend/story/analyzer.py`
- Delete after replacement tests pass: `tests/test_story_analyzer.py`
- Create: `tests/test_story_offline_analysis.py`
- Create: `tests/test_story_import_analysis_cli.py`
- Modify: `config/config.example.yaml`

**Interfaces:**
- Consumes: `StorySourceLoader.load(path: Path, *, encoding: str = "auto") -> ImportedStory`, `AnalysisStore.load(key: AnalysisKey) -> StoryMap | None`, and `AnalysisStore.save(key: AnalysisKey, story_map: StoryMap) -> None`.
- Produces: shared `dlc_provenance(cfg: Mapping[str, object], *, project_root: Path, waveform_policy: str | None = None) -> str`, `OFFLINE_PRODUCER = "codex-offline"`, `OFFLINE_ANALYSIS_VERSION = "faithful-offline-v1"`, `offline_analysis_key(story: ImportedStory, dlc_version: str) -> AnalysisKey`, `OfflineAnalysisImporter.validate(source_path: Path, candidate_path: Path, *, encoding: str, dlc_version: str) -> ValidatedOfflineAnalysis`, `.import_candidate(...) -> ValidatedOfflineAnalysis`, and `AnalysisStore.inspect(key: AnalysisKey) -> AnalysisLookup`.
- `ValidatedOfflineAnalysis` contains only `key: AnalysisKey`, `story_map: StoryMap`, `chapter_count: int`, and `scene_count: int`.
- `AnalysisLookup` contains `status: Literal["ready", "missing", "invalid"]` and `story_map: StoryMap | None`; `invalid` is returned for a matching entry quarantined during that inspection, while a later inspection after quarantine is `missing`.

- [ ] **Step 1: Write failing domain tests**

Add explicit tests that build a two-chapter source and candidate dictionary, then assert exact coverage and fail-closed validation:

```python
def test_import_candidate_uses_real_source_identity_and_saves_once(self):
    result = self.importer.import_candidate(
        self.source_path,
        self.candidate_path,
        encoding="utf-8",
        dlc_version="dlc1-v1",
    )
    self.assertEqual(result.key.model, "codex-offline")
    self.assertEqual(result.key.prompt_version, "faithful-offline-v1")
    self.assertEqual(result.story_map.source_hash, self.story.source_hash)
    self.assertEqual(self.store.save_count, 1)

def test_validate_never_writes(self):
    self.importer.validate(self.source_path, self.candidate_path,
                           encoding="auto", dlc_version="dlc1-v1")
    self.assertEqual(self.store.save_count, 0)
```

Use subtests for duplicate/extra keys, malformed/deep/oversize JSON, forged source hash, wrong text length/ID, gap, overlap, disorder, out-of-range offsets, empty summary, and invalid pace. Assert failures raise `OfflineAnalysisError` and leave the formal cache absent.

- [ ] **Step 2: Run focused tests and record the red state**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_offline_analysis -v`

Expected: FAIL because `backend.story.offline_analysis` does not exist.

- [ ] **Step 3: Implement the bounded strict importer**

Implement a small immutable result and importer. Reuse model constructors as the final invariant boundary; parse candidates with duplicate-key rejection and explicit byte/depth/member limits before constructing models.

```python
@dataclass(frozen=True, slots=True)
class ValidatedOfflineAnalysis:
    key: AnalysisKey
    story_map: StoryMap
    chapter_count: int
    scene_count: int

def offline_analysis_key(story: ImportedStory, dlc_version: str) -> AnalysisKey:
    return AnalysisKey(
        source_hash=story.source_hash,
        model=OFFLINE_PRODUCER,
        prompt_version=OFFLINE_ANALYSIS_VERSION,
        dlc_version=_required_identity(dlc_version, "DLC version"),
    )
```

The candidate schema must be exactly `source_hash`, `text_length`, and `chapters`; nested chapter/scene fields must exactly match `StoryMap`. Recompute and compare every persistent ID from the real source hash and zero-based indexes. `import_candidate()` calls `validate()` and then exactly one `AnalysisStore.save()`.

Add `AnalysisStore.inspect()` without weakening `load()`: it reports `invalid` when the target existed but failed validation and was quarantined during this call, `missing` when no target existed, and `ready` with a validated map otherwise. This gives Task 6 a stable one-request invalid state without retaining corrupt bytes.

- [ ] **Step 4: Add CLI red tests and implement validate/import commands**

Test `main(argv: Sequence[str] | None = None) -> int` directly with temporary paths and captured stdout/stderr. Cover `validate`, `import`, all three encodings, missing arguments, ambiguity, invalid DLC version, and redacted errors.

```text
python -m backend.story.import_analysis validate --source FILE --map FILE --encoding auto
python -m backend.story.import_analysis import   --source FILE --map FILE --encoding auto
```

The command loads the current local config and derives DLC provenance through the same shared pure helper used by `AppState`; it does not accept a caller-supplied DLC identity. Extract the existing `_dlc_provenance` logic from `backend/main.py` into `backend/provenance.py` without changing its digest. Success prints only source-hash prefix, identity versions, chapter count, scene count, and `validated` or `imported`. Errors print no source excerpt and return a nonzero status.

- [ ] **Step 5: Remove the online whole-book path**

After offline tests pass, remove `StoryAnalyzer` exports, online analyzer implementation, and its dedicated tests. Keep `LLMClient.complete_json()` only if another runtime consumer uses it; otherwise remove only dead story-specific classification helpers proven unreferenced by `rg`. Do not refactor general chat behavior.

- [ ] **Step 6: Verify and commit**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_story_offline_analysis tests.test_story_import_analysis_cli tests.test_story_source tests.test_story_analysis_store -v
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.venv\Scripts\python.exe -m compileall -q backend tests
git diff --check
```

Commit: `feat: import Codex story analysis offline`

### Task 4: Full-Chapter Faithful Planner

**Files:**
- Create: `backend/story/planner.py`
- Create: `tests/test_story_planner.py`
- Modify only if a shared serializable event field is required: `backend/timeline/models.py`

**Interfaces:**
- Consumes: validated `StoryMap`, one selected `StoryChapter`, existing waveform registry, effective A/B caps, `CycleGapPolicy`, and MVP1 seeded resolver.
- Produces: `ChapterPlanner.plan(story: ImportedStory, story_map: StoryMap, chapter_id: str, *, speed: Literal["slow", "standard", "fast"], seed: int) -> ValidatedChapterPlan` and `ChapterPlanError`.
- `ValidatedChapterPlan` contains source/chapter identity, speed, seed, ordered resolved plot events, chapter duration, and a dry-validated timeline request; it owns no player or device.

- [ ] **Step 1: Write failing faithful planning tests**

Cover exact scene order, full selected-chapter coverage, deterministic same-seed output, different-seed permitted variation, only allowed waveforms, per-channel `keep|set|stop`, base strength within effective cap, scene pace timing, and all-or-nothing rejection.

```python
def test_same_seed_and_speed_produce_same_plan(self):
    first = self.planner.plan(self.story, self.story_map, self.chapter_id,
                              speed="standard", seed=88)
    second = self.planner.plan(self.story, self.story_map, self.chapter_id,
                               speed="standard", seed=88)
    self.assertEqual(first, second)
```

- [ ] **Step 2: Run the focused red test**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_planner -v`

Expected: FAIL because the planner module does not exist.

- [ ] **Step 3: Implement pure planning and dry validation**

Compute scene duration from normalized character count, configured CPM, and scene pace. Produce one ordered plot event per scene. Reuse the accepted MVP1 resolver for seeded waveform, strength jitter, and cycle-relative gaps; do not add a second random engine. Validate every event through the existing safety adapter without emitting frames. Return no partial plan on any failure.

- [ ] **Step 4: Verify and commit**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_story_planner tests.test_timeline_resolver tests.test_timeline_session -v
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Commit: `feat: plan faithful novel chapters offline`

### Task 5: Novel Session and Archive Lifecycle

**Files:**
- Create: `backend/story/session.py`
- Create: `tests/test_novel_session.py`
- Modify: `backend/timeline/session.py`
- Modify: `backend/timeline/replay_store.py`
- Modify: `tests/test_replay_store.py`

**Interfaces:**
- Consumes: `ValidatedChapterPlan`, `SessionController`, `ImportedStory`, and validated `StoryMap`.
- Produces: `NovelSessionController.start(plan, story, story_map)`, `.pause()`, `.resume(from_: Literal["current", "chapter_start", "beginning"])`, `.finish()`, `.abort()`, and immutable `NovelSessionState`.
- Physical output lifecycle is delegated exclusively through existing `SessionController`/GameLoop methods.

- [ ] **Step 1: Write failing lifecycle and ownership tests**

Cover the only start transition `planning -> validated -> running`, failed plan never starts, pause clears output and preserves cursor, all three resume positions, disconnect aborts without archive, finish embeds source plus `scenes.json`, chat does not mutate the plan, and exact replay makes zero planning/model calls.

- [ ] **Step 2: Run the focused red test**

Run: `.venv\Scripts\python.exe -m unittest tests.test_novel_session -v`

- [ ] **Step 3: Implement the controller as an adapter**

Keep story/reader metadata in `NovelSessionController`, but invoke only public lifecycle operations on the injected `SessionController`. Never instantiate `TimelinePlayer`, relay adapters, or an alternate output coordinator. Every reposition first clears output through the accepted session path.

- [ ] **Step 4: Extend archive validation minimally**

For novel archives require exactly one bounded `.txt`, `.md`, or `.docx` source plus bounded `scenes.json`; store their hashes, chapter ID, speed, source encoding, analysis version, and DLC version in the manifest metadata. Reject novel claims missing either member. Preserve non-novel archive compatibility.

- [ ] **Step 5: Verify and commit**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_novel_session tests.test_replay_store tests.test_timeline_session -v
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Commit: `feat: run and archive offline novel sessions`

### Task 6: Offline Story Status and Runtime APIs

**Files:**
- Modify: `backend/main.py`
- Create: `tests/test_story_endpoints.py`

**Interfaces:**
- Consumes: source loader, `offline_analysis_key()`, `AnalysisStore`, `ChapterPlanner`, and `NovelSessionController`.
- Produces the exact HTTP surface below and adds story state to the existing WebSocket full-state payload.

- [ ] **Step 1: Write failing endpoint tests**

Use temporary storage and an LLM spy that raises if called. Test TXT/MD/DOCX import with `auto|utf-8|gb18030`, unsupported/oversize rejection, source isolation, `ready|missing|invalid`, chapter listing, reader bounds, play/pause/resume/finish, and generation-gated WebSocket state.

```python
def test_missing_analysis_never_calls_llm(self):
    response = self.client.get(f"/api/story/{self.source_id}/analysis")
    self.assertEqual(response.status_code, 409)
    self.assertEqual(response.json()["code"], "analysis_missing")
    self.assertEqual(self.llm.call_count, 0)
```

- [ ] **Step 2: Run the endpoint red tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_endpoints -v`

- [ ] **Step 3: Implement the minimal API surface**

```text
POST /api/story/import
GET  /api/story/{source_id}/analysis
GET  /api/story/{source_id}/chapters
POST /api/story/{source_id}/chapters/{chapter_id}/play
GET  /api/story/reader
GET  /api/story/reader/text?start=N&end=M
POST /api/story/pause
POST /api/story/resume
POST /api/story/finish
```

Map source IDs to server-owned paths; never return arbitrary filesystem paths. Return 409 with `analysis_missing`, 422 with `analysis_invalid`, and stable public details containing hash prefix/version/DLC but no source excerpt. Delete the old planned `/analyze` route entirely.

- [ ] **Step 4: Verify and commit**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_story_endpoints tests.test_main_timeline_api -v
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Commit: `feat: expose offline novel runtime APIs`

### Task 7: Minimal Built-In Reader UI

**Files:**
- Create: `frontend/src/components/NovelImport.tsx`
- Create: `frontend/src/components/NovelReader.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/store.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/Sidebar.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/styles.css`
- Create or modify matching frontend tests under: `frontend/src/**/*.test.tsx`

**Interfaces:**
- Consumes Task 6 JSON contracts and existing generation-gated state refresh patterns.
- Produces import/status/chapter-selection UI and an original-text reader with current scene/progress and session controls.

- [ ] **Step 1: Add failing typed-contract and state tests**

Model `StoryAnalysisStatus = "ready" | "missing" | "invalid"`, source summary, chapter summary, reader slice, and novel session state. Test that a stale HTTP response cannot overwrite newer WebSocket generation state.

- [ ] **Step 2: Implement import and offline status states**

Provide TXT/MD/DOCX file selection and an explicit encoding selector. For `missing`, show the source hash prefix and local Codex/import instructions; for `invalid`, show regeneration guidance; for `ready`, show chapters. Do not add candidate upload, analysis, or retry buttons.

- [ ] **Step 3: Implement the reader and controls**

After chapter/speed selection, show planning status; after validated start, render bounded original-text slices, chapter/scene/progress, current A/B public state, pause, finish, and three resume choices. Keep chat visible and label it `不改变原文剧情`.

- [ ] **Step 4: Verify and commit**

Run:

```powershell
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Commit: `feat: add offline faithful novel reader`

### Task 8: Local MVP2 Integration Gate and Documentation

**Files:**
- Create: `tests/test_mvp2_novel_integration.py`
- Modify: `README.md`
- Modify: `.gitignore` only if candidate/analysis paths are not already covered.

**Interfaces:**
- Consumes every Task 3–7 public interface.
- Produces a fully local, automated import-to-replay acceptance test and operator instructions; it performs no external calls or release mutations.

- [ ] **Step 1: Write the end-to-end dry-run test**

Generate a two-chapter local source and candidate JSON, validate/import it, import the source through the API, assert `ready`, plan and play one chapter with fake relay, pause/resume/finish, load the archive, and exact replay. Assert source/scenes hashes and zero physical frames in dry-run mode. Inject an LLM spy and assert zero whole-book analysis calls.

- [ ] **Step 2: Document the personal-project workflow**

Document supported formats/encodings/size, the Codex candidate location, exact validate/import commands, identity invalidation rules, `ready|missing|invalid`, faithful behavior, random runtime output, pause/resume, archive contents, and local deletion/backup. State that `data/story_candidates/` and `data/story_analysis/` are not committed.

- [ ] **Step 3: Run the complete local verification gate**

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.venv\Scripts\python.exe -m compileall -q backend tests
npm --prefix frontend ci
npm --prefix frontend audit --audit-level=high
npm --prefix frontend test -- --run
npm --prefix frontend run build
git diff --check
git status --short
```

Expected: every command exits 0, no test calls an external model or relay, and only intentional commits exist. Do not push, tag, or run a real-device test.

- [ ] **Step 4: Commit the local integration gate**

Commit: `test: gate offline faithful novel MVP`
