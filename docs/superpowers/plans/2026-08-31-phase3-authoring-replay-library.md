# Phase 3 Authoring, Interpretation, and Replay Library Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user override selected novel scenes, opt into an interpretation mode, and manage/search/derive completed replays without weakening exact-replay semantics.

**Architecture:** Keep original sources immutable. Store user decisions as versioned sidecar overrides keyed by source/chapter/scene IDs, merge them into the planner before resolution, and store the merged inputs plus resolved output in a new replay schema. A local SQLite replay index provides metadata queries while `.coyote-replay` remains the portable source of truth.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, FastAPI, MVP1 timeline/MVP2 story packages, React 19, TypeScript, Zustand, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-31-randomized-timeline-novel-mode-design.md`

**Depends on:** Accepted tag `mvp2-faithful-novel-mode`.

## Global Constraints

- Scene override modes are `force`, `prefer`, and `random`.
- Optional locks apply to base strength and duration independently.
- Never edit or overwrite original novel bytes/text.
- Faithful mode remains the default. Interpretation mode is explicit and preserves major plot order.
- Exact replay never resamples. Similar-version generation always creates a new derived replay and is never labeled exact.
- Cross-DLC conversion validates patterns and caps, reports every substitution, and creates a new archive.
- Every normally completed session stays permanent until the user explicitly deletes it.

---

### Task 1: Versioned sidecar override model and migration

**Files:**
- Create: `backend/story/overrides.py`
- Modify: `backend/story/models.py`
- Modify: `backend/timeline/models.py`
- Modify: `backend/timeline/replay_store.py`
- Create: `tests/test_story_overrides.py`
- Create: `tests/test_replay_schema_migration.py`

- [ ] **Step 1: Write model/invariant tests**

Test valid `force/prefer/random`, channel-specific directives, optional base-strength lock, optional duration lock, source/chapter/scene ownership, rejection of stale IDs, and schema-v1 replay loading into schema-v2 in-memory defaults.

```python
class StoryOverrideTests(unittest.TestCase):
    def test_force_requires_at_least_one_channel_or_duration_lock(self):
        with self.assertRaises(OverrideValidationError):
            SceneOverride(
                source_hash="abc", chapter_id="ch-0001", scene_id="ch-0001-sc-0001",
                mode="force", channels={}, duration_ms=None,
            )

    def test_random_cannot_claim_exact_parameters(self):
        with self.assertRaises(OverrideValidationError):
            SceneOverride(
                source_hash="abc", chapter_id="ch-0001", scene_id="ch-0001-sc-0001",
                mode="random", channels={}, duration_ms=3000, lock_duration=True,
            )
```

- [ ] **Step 2: Implement immutable sidecar storage**

Store overrides as `data/story_overrides/<source_hash>.json` with schema version, source hash, timestamps, and scene entries. Write atomically. Validate the complete document before replace. A stale scene ID is reported and ignored only after explicit user confirmation through the API; it is not silently remapped.

- [ ] **Step 3: Add replay schema v2 reader/writer**

Schema v2 adds planning mode, applied overrides, derivation parent ID, derivation kind, substitutions, and optional user metadata. Keep a dedicated v1 reader that fills neutral defaults; never rewrite a v1 archive merely by viewing it.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_overrides tests.test_replay_schema_migration -v`

Commit: `feat: add versioned scene override sidecars`

### Task 2: Override merge and interpretation planner

**Files:**
- Modify: `backend/story/planner.py`
- Create: `backend/story/interpreter.py`
- Create: `tests/test_override_merge.py`
- Create: `tests/test_interpretation_planner.py`

- [ ] **Step 1: Define and test precedence**

Use this exact order: safety caps > `force` locks > validated AI directive > `prefer` hint > DLC defaults > random allowed choice. `random` discards AI pattern choice for the selected fields but retains source scene identity and cap. A duration lock controls scene duration only; the shared per-channel cycle runners continue generating raw cycles and fixed-policy gaps inside that duration.

Test A/B independently, unavailable forced pattern rejection, cap-clamped strength, duration lock bounds, deterministic seed behavior, and no mutation of input models.

- [ ] **Step 2: Implement interpretation mode**

Interpretation mode may expand transition density, scene duration, and directive detail. It must preserve ordered chapter/scene IDs, original text spans, and major-event flags from analysis. Validate that every output scene maps to one original scene and offsets remain ordered. Faithful mode continues using the MVP2 prompt and behavior unchanged.

- [ ] **Step 3: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_override_merge tests.test_interpretation_planner tests.test_story_planner -v`

Commit: `feat: merge scene overrides and interpretation plans`

### Task 3: Scene authoring APIs

**Files:**
- Modify: `backend/main.py`
- Create: `tests/test_story_override_endpoints.py`

- [ ] **Step 1: Test authorization-by-local-ID and validation**

Cover list/get/upsert/delete, source mismatch, missing scene, stale scene, invalid pattern, cap overflow, optimistic revision conflict, and plan preview. Use temporary storage and fake planners.

- [ ] **Step 2: Implement API surface**

```text
GET    /api/story/{source_id}/overrides                         -> OverrideDocument
PUT    /api/story/{source_id}/overrides/{scene_id}              -> SceneOverride
DELETE /api/story/{source_id}/overrides/{scene_id}?revision=N   -> OverrideDocument
POST   /api/story/{source_id}/chapters/{chapter_id}/preview     -> PlanPreview
POST   /api/story/{source_id}/chapters/{chapter_id}/play        accepts mode faithful|interpretation
```

Require the caller's last-seen revision for writes. Preview resolves a frame-free timeline and returns scene/event summaries; it never starts playback or writes a permanent replay.

- [ ] **Step 3: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_override_endpoints -v`

Commit: `feat: expose scene authoring and preview APIs`

### Task 4: Scene authoring UI

**Files:**
- Create: `frontend/src/components/SceneEditor.tsx`
- Create: `frontend/src/components/PlanPreview.tsx`
- Modify: `frontend/src/components/NovelReader.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/store.ts`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Build a scene-focused editor**

From the reader, open the selected scene and choose `强制 / 优先 / 随机`. Configure A/B separately with waveform and optional base-strength lock; configure optional duration lock. Display effective per-channel cap. Save using revision and show conflicts without overwriting newer data.

- [ ] **Step 2: Add faithful/interpretation and preview**

Default to faithful. Interpretation requires an explicit mode selection each session. Preview shows ordered scene cards, A/B directive summaries, duration, and which fields came from override versus AI/randomizer. It does not expose hidden model reasoning.

- [ ] **Step 3: Verify and commit**

Run: `npm --prefix frontend run build`

Commit: `feat: add scene override editor and plan preview`

### Task 5: Replay metadata index and library API

**Files:**
- Create: `backend/timeline/replay_index.py`
- Modify: `backend/timeline/replay_store.py`
- Modify: `backend/main.py`
- Create: `tests/test_replay_index.py`
- Create: `tests/test_replay_library_endpoints.py`

- [ ] **Step 1: Write index consistency tests**

Test initial scan, insert/update/delete, archive missing from disk, corrupt archive quarantine, duplicate ID, filtering by DLC/source/date/rating/exact-adjusted, stable sort, pagination, rename, and rating bounds 1–5.

- [ ] **Step 2: Implement SQLite index as rebuildable metadata**

Use `data/replays/index.sqlite3` with WAL mode and parameterized queries. The archive remains authoritative; on startup, reconcile files to rows and mark corrupt/unreadable entries without playing them. Deleting a replay first moves the archive to `data/replays/.trash/`, commits index removal, and reports the recoverable trash path.

- [ ] **Step 3: Implement library API**

```text
GET    /api/replays?query=&dlc=&source=&rating=&status=&cursor=  -> ReplayPage
PATCH  /api/replays/{id} {"title":string,"rating":1..5|null}   -> ReplaySummary
DELETE /api/replays/{id}                                        -> DeleteResult
GET    /api/replays/{id}/download                               -> FileResponse
POST   /api/replays/import                                      -> ReplaySummary
```

Import validates archive/schema/checksums before copying it atomically into the store. Reject an ID collision with different content.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_replay_index tests.test_replay_library_endpoints -v`

Commit: `feat: add searchable replay library`

### Task 6: Similar-version and cross-DLC derivation

**Files:**
- Create: `backend/timeline/derive.py`
- Modify: `backend/main.py`
- Create: `tests/test_replay_derivation.py`
- Create: `tests/test_replay_derivation_endpoints.py`

- [ ] **Step 1: Test derivation semantics**

Similar version: retain scene order, scene durations, base targets, random ranges, and source; use a new seed to resample waveform/strength and the independent A/B cycle-gap schedules over the same active-duration boundaries; set `exact=false`, parent ID, and `derivation_kind=similar`.

Cross-DLC: map available pattern names, clamp to target caps, collect explicit substitutions, require user confirmation if any substitution exists, and set `derivation_kind=cross_dlc`. The source archive is unchanged.

- [ ] **Step 2: Implement preview-before-create APIs**

```text
POST /api/replays/{id}/derive/similar/preview  -> DerivationPreview
POST /api/replays/{id}/derive/similar          -> ReplaySummary
POST /api/replays/{id}/derive/cross-dlc/preview {"role":string,"profile":string} -> DerivationPreview
POST /api/replays/{id}/derive/cross-dlc         {"role":string,"profile":string,"confirm":true} -> ReplaySummary
```

Preview makes no permanent write. Create writes a new complete archive atomically and indexes it.

- [ ] **Step 3: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_replay_derivation tests.test_replay_derivation_endpoints -v`

Commit: `feat: derive similar and cross-dlc replays`

### Task 7: Replay library UI

**Files:**
- Modify: `frontend/src/components/ReplayPanel.tsx`
- Create: `frontend/src/components/ReplayDetails.tsx`
- Create: `frontend/src/components/ReplayDeriveDialog.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/store.ts`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Add search, filters, metadata actions**

Provide query, DLC/source/date/rating/exact-adjusted filters, pagination, rename, rating, download/export, and delete. Delete confirmation states that the file moves to local recoverable trash.

- [ ] **Step 2: Add derivation previews**

Show new seed and changed-event count for similar versions. For cross-DLC, list each waveform substitution and strength clamp before enabling confirm. Clearly label all derived results `非精确版本`.

- [ ] **Step 3: Verify and commit**

Run: `npm --prefix frontend run build`

Commit: `feat: expand replay library controls`

### Task 8: Phase 3 integration and release gate

**Files:**
- Create: `tests/test_phase3_integration.py`
- Modify: `README.md`

- [ ] **Step 1: Add end-to-end dry-run tests**

Exercise faithful playback with a forced A override, interpretation playback with duration lock, exact replay, similar derivation, cross-DLC preview/confirm, search/rating/export/delete, v1 archive compatibility, and zero real relay frames.

- [ ] **Step 2: Run complete verification**

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.venv\Scripts\python.exe -m compileall -q backend tests
npm --prefix frontend ci
npm --prefix frontend audit --audit-level=high
npm --prefix frontend run build
```

- [ ] **Step 3: Push and run real-device acceptance**

Push `codex/phase3-authoring-replay-library`. With cap 40 or lower, verify one forced scene, one random scene, pause clearing, exact replay, and one derived version. Confirm derivation never changes the source archive.

- [ ] **Step 4: Tag after acceptance**

```bash
git tag -a phase3-authoring-replay-library -m "Phase 3 authoring and replay library accepted"
git push origin phase3-authoring-replay-library
```
