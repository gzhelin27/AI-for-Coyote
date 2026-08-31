# MVP2 Faithful Novel Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for each behavior change and superpowers:verification-before-completion at the release gate.

**Goal:** Import a local TXT, MD, or DOCX novel, analyze it once, generate and validate a faithful full-chapter A/B timeline, display the original text in a reader, and automatically play the selected chapter through the accepted MVP1 timeline/safety path.

**Architecture:** Add `backend.story` as a content-source adapter above the MVP1 timeline layer. Extraction and analysis produce stable chapter/scene records; planning produces only `keep`, `set`, or `stop` channel directives; the resolver owns interval sampling and `±4` strength variation. Source bytes, scene map, and resolved timeline are embedded in the completed replay.

**Tech Stack:** Python 3.12, FastAPI, `python-docx`, stdlib hashing/JSON, existing LLM client, MVP1 timeline package, React 19, TypeScript, Zustand, Python `unittest`.

**Depends on:** Accepted tag `mvp1-randomized-timeline-replay`.

## Constraints

- Faithful mode only: preserve chapter order and source meaning; chat cannot change the main plot.
- Analyze the whole novel once. Use chapter/size fallback only when the single request fails or exceeds model limits.
- Do not send camera or microphone state to novel analysis/planning.
- Generate, parse, validate, and dry-run the full selected chapter before autoplay begins.
- AI chooses per-channel `keep`, `set(pattern, base_strength)`, or `stop`; scheduler owns interval timing and strength jitter.
- Reading speeds are slow 250, standard 400, and fast 600 normalized Chinese characters per minute.
- Pause clears output. Resume begins at a chosen safe event/chapter cursor.
- Imported source and caches stay in ignored local `data/` paths.

## File structure

- Create `backend/story/models.py`: source/chapter/scene/analysis/plan models.
- Create `backend/story/source.py`: safe import and text normalization.
- Create `backend/story/analysis_store.py`: source-hash cache.
- Create `backend/story/analyzer.py`: whole-book analysis and chapter fallback.
- Create `backend/story/planner.py`: full-chapter directive generation and timeline resolution.
- Create `backend/story/session.py`: novel lifecycle and reader progress.
- Create `backend/story/__init__.py`: public interfaces.
- Modify `backend/llm.py`: structured story calls with injected/mockable client seam.
- Modify `backend/main.py`, `backend/config.py`, `config/config.example.yaml`, `requirements.txt`.
- Create `frontend/src/components/NovelReader.tsx` and `NovelImport.tsx`; modify app state/API/navigation files.
- Add focused tests plus one end-to-end dry-run integration test.

### Task 1: Safe source import and normalized document model

**Files:**
- Modify: `requirements.txt`
- Create: `backend/story/__init__.py`
- Create: `backend/story/models.py`
- Create: `backend/story/source.py`
- Create: `tests/test_story_source.py`
- Modify: `backend/config.py`
- Modify: `config/config.example.yaml`

- [ ] **Step 1: Add failing TXT/MD/DOCX extraction tests**

```python
class StorySourceTests(unittest.TestCase):
    def test_txt_normalizes_newlines_and_hashes_original_bytes(self):
        imported = StorySourceLoader(max_bytes=1024).load("novel.txt", "甲\r\n乙".encode("utf-8"))
        self.assertEqual(imported.text, "甲\n乙")
        self.assertEqual(imported.extension, ".txt")
        self.assertEqual(imported.source_sha256, hashlib.sha256("甲\r\n乙".encode("utf-8")).hexdigest())

    def test_rejects_unsupported_extension_and_oversize_input(self):
        loader = StorySourceLoader(max_bytes=3)
        with self.assertRaises(StorySourceError):
            loader.load("novel.pdf", b"abc")
        with self.assertRaises(StorySourceError):
            loader.load("novel.txt", b"abcd")
```

Create the DOCX fixture inside the test with `docx.Document()`, add headings/paragraphs, save to `BytesIO`, and assert heading order and paragraph text are preserved.

- [ ] **Step 2: Run and observe the missing-module failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_source -v`

- [ ] **Step 3: Implement strict import**

Add `python-docx>=1.1,<2` to `requirements.txt`. Define immutable `ImportedStory(filename, extension, original_bytes, text, source_sha256)` and `StorySourceLoader(max_bytes)`. Sanitize filenames with `Path(name).name`, allow only `.txt`, `.md`, `.docx`, detect UTF-8/UTF-8-BOM first and GB18030 second for plain text, normalize CRLF/CR to LF, strip NULs, and reject empty extracted text. Never write uploads using the supplied filename.

Add configuration:

```yaml
story:
  import_dir: data/stories
  analysis_dir: data/story_analysis
  max_source_mb: 20
  analysis_prompt_version: faithful-v1
  reading_speed_cpm: {slow: 250, standard: 400, fast: 600}
```

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_source -v`

Commit: `feat: import local novel sources safely`

### Task 2: Stable chapter/scene model and analysis cache

**Files:**
- Modify: `backend/story/models.py`
- Create: `backend/story/analysis_store.py`
- Create: `tests/test_story_analysis_store.py`

- [ ] **Step 1: Write round-trip/cache-key tests**

```python
class AnalysisStoreTests(unittest.TestCase):
    def test_cache_key_changes_with_model_prompt_or_dlc(self):
        base = AnalysisKey("source-hash", "model-a", "faithful-v1", "dlc-v1")
        self.assertNotEqual(base.digest(), replace(base, model="model-b").digest())
        self.assertNotEqual(base.digest(), replace(base, prompt_version="faithful-v2").digest())
        self.assertNotEqual(base.digest(), replace(base, dlc_version="dlc-v2").digest())

    def test_scene_map_round_trip_preserves_stable_ids(self):
        store = AnalysisStore(self.temp_path)
        store.save(self.key, self.scene_map)
        self.assertEqual(store.load(self.key), self.scene_map)
```

- [ ] **Step 2: Implement versioned models and atomic cache**

Define `StoryChapter`, `StoryScene`, `StoryMap`, and `AnalysisKey`. Stable IDs derive from source hash plus zero-based chapter/scene indices (`ch-0001`, `ch-0001-sc-0001`); they do not depend on generated summaries. Store schema-versioned JSON using a temporary file and `Path.replace()`. Validate source hash and IDs on read; a corrupt cache is quarantined by renaming it with `.invalid` and treated as a miss.

- [ ] **Step 3: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_analysis_store -v`

Commit: `feat: cache versioned novel scene analysis`

### Task 3: One-time whole-book analyzer with chapter fallback

**Files:**
- Create: `backend/story/analyzer.py`
- Modify: `backend/llm.py`
- Create: `tests/test_story_analyzer.py`

- [ ] **Step 1: Write mocked analyzer tests**

Test these exact branches without network access:

1. cache hit makes zero LLM calls;
2. whole-book response succeeds and yields stable chapter/scene IDs;
3. context-limit error triggers detected-heading chunks;
4. a heading-free source falls back to bounded character chunks;
5. one invalid chunk fails the entire analysis and writes no cache.

```python
class StoryAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_limit_uses_chapter_fallback(self):
        client = FakeStructuredClient([
            ContextLimitError("too long"),
            {"scenes": [{"start": 0, "end": 2, "summary": "第一幕", "pace": 1.0}]},
            {"scenes": [{"start": 0, "end": 2, "summary": "第二幕", "pace": 1.0}]},
        ])
        result = await self.analyzer(client).analyze(self.two_chapter_story)
        self.assertEqual([chapter.chapter_id for chapter in result.chapters], ["ch-0001", "ch-0002"])
        self.assertEqual(client.call_count, 3)
```

- [ ] **Step 2: Add a structured LLM seam**

Expose an injected async `complete_json(system_prompt, user_content, schema_name)` method in `backend/llm.py`. Preserve existing chat behavior. Classify HTTP/model context-length errors into `ContextLimitError`; classify all other failures as retryable `StoryAnalysisError`. Never log `user_content`.

- [ ] **Step 3: Implement analyzer and merge validation**

Attempt one full-source request. On context-limit failure, detect common Chinese/Arabic chapter headings; if none exist, split on paragraph boundaries under configured character size. Require each result to cover ordered, non-overlapping offsets within its chunk. Merge by original offsets, assign stable IDs, and reject gaps, overlaps, out-of-range values, invalid pace, or empty chapters. Cache only the validated complete map.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_analyzer -v`

Commit: `feat: analyze novels once with chapter fallback`

### Task 4: Full-chapter faithful planner

**Files:**
- Create: `backend/story/planner.py`
- Create: `tests/test_story_planner.py`
- Modify: `backend/timeline/models.py`

- [ ] **Step 1: Write validation and deterministic timing tests**

```python
class StoryPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_waveform_rejects_entire_chapter(self):
        client = FakeStructuredClient([{
            "scenes": [{"scene_id": "ch-0001-sc-0001", "channels": {
                "A": {"mode": "set", "pattern": "不存在", "base_strength": 20},
                "B": {"mode": "keep"},
            }}]
        }])
        with self.assertRaisesRegex(ChapterPlanError, "ch-0001-sc-0001"):
            await self.planner(client).plan(self.chapter)
        self.assertEqual(self.player.started_count, 0)

    async def test_same_seed_and_speed_produce_same_timeline(self):
        first = await self.planner(self.client, seed=88, speed="standard").plan(self.chapter)
        second = await self.planner(self.client, seed=88, speed="standard").plan(self.chapter)
        self.assertEqual(first.timeline, second.timeline)
```

- [ ] **Step 2: Implement strict response schema**

The planner request includes scene text/summaries, ordered scene IDs, allowed waveform names, per-channel accessory/location/baseline/cap, and faithful-mode constraints. It excludes camera/microphone data and chat history. Require exactly one entry per scene and exactly A/B directives. `keep` and `stop` reject pattern/strength fields; `set` requires an allowed pattern and integer base strength in `0..effective_cap`.

- [ ] **Step 3: Resolve chapter timing before playback**

Calculate scene duration as `normalized_character_count / cpm * 60`, multiply by validated scene pace, then resolve events with the MVP1 seeded resolver and interval profile. Ensure offsets are monotonic and inside the scene duration. Perform a frame-free dry validation through the safety adapter. Return `ValidatedChapterPlan` only after every event succeeds; do not expose a partial timeline.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_planner -v`

Commit: `feat: build validated faithful chapter timelines`

### Task 5: Novel session, archive embedding, and resume semantics

**Files:**
- Create: `backend/story/session.py`
- Modify: `backend/timeline/session.py`
- Modify: `backend/timeline/replay_store.py`
- Create: `tests/test_novel_session.py`

- [ ] **Step 1: Write session tests**

Cover: successful plan autostarts; failed plan never starts; pause clears and retains cursor; resume from beginning/current event/chapter start; chat events do not mutate timeline; finish embeds source and scenes; disconnect pauses without saving; completed replay makes zero LLM calls.

- [ ] **Step 2: Implement lifecycle**

Create `NovelSessionController` that owns import/analyze/plan status and delegates playback to MVP1 `TimelinePlayer`. The only valid autoplay transition is `planning -> validated -> running`. On finish, call `ReplayStore.save()` with original source bytes, sanitized extension, and `scenes.json`. Store reading speed and chapter ID in the manifest. On resume, convert the chosen location to a validated event cursor and clear output before repositioning.

- [ ] **Step 3: Extend archive validation**

Allow exactly one source file with `.txt`, `.md`, or `.docx`; validate checksum and configured size before loading. For a novel replay, require both `scenes.json` and source entry. Reject a manifest that claims novel mode without both files.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_novel_session tests.test_replay_store -v`

Commit: `feat: run and archive faithful novel sessions`

### Task 6: Novel APIs

**Files:**
- Modify: `backend/main.py`
- Create: `tests/test_story_endpoints.py`

- [ ] **Step 1: Write endpoint tests with temporary storage and fake LLM**

Test multipart upload, unsupported/oversize rejection, analysis cache hit, analysis failure, chapter list, plan/start, pause/resume/finish, reader slice bounds, and source isolation between sessions.

- [ ] **Step 2: Implement exact API surface**

```text
POST /api/story/import                         multipart file -> StorySourceSummary
POST /api/story/{source_id}/analyze            {} -> StoryAnalysisStatus
GET  /api/story/{source_id}/analysis           -> StoryMapSummary
POST /api/story/{source_id}/chapters/{id}/play {"speed":"standard"} -> NovelSessionState
GET  /api/story/reader                         -> ReaderState
GET  /api/story/reader/text?start=N&end=M      -> ReaderTextSlice
POST /api/story/pause                          {} -> NovelSessionState
POST /api/story/resume                         {"from":"current|chapter_start|beginning"} -> NovelSessionState
POST /api/story/finish                         {} -> ReplaySummary
```

Use opaque UUID source IDs mapped to server-owned paths. Bound reader slices and never return an arbitrary filesystem path. Broadcast progress and current scene in the existing WebSocket state.

- [ ] **Step 3: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_story_endpoints -v`

Commit: `feat: expose novel import and playback APIs`

### Task 7: Built-in reader UI

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

- [ ] **Step 1: Add typed source/analysis/reader/session contracts**

Model import, progress, chapter list, planning error, current text slice, current scene, timeline progress, reading speed, and resume choice. API methods must return these contracts rather than `unknown`.

- [ ] **Step 2: Build import and analysis states**

Provide file picker/drag-drop for TXT/MD/DOCX, upload progress, one analysis action, cached status, retryable error, and chapter selection. Never show autoplay until analysis is complete.

- [ ] **Step 3: Build reader and autoplay flow**

Selecting a chapter and speed starts planning; show `正在生成并校验本章计划`. When the backend returns `running`, render original text, chapter/scene/progress, speed, current A/B state, pause, finish, and resume choices. Keep chat visible in a side panel and label it `不改变原文剧情`.

- [ ] **Step 4: Verify and commit**

Run: `npm --prefix frontend run build`

Commit: `feat: add faithful novel reader experience`

### Task 8: MVP2 integration and release gate

**Files:**
- Create: `tests/test_mvp2_novel_integration.py`
- Modify: `README.md`

- [ ] **Step 1: Add dry-run integration**

Use a generated two-chapter DOCX, fake structured LLM, fixed seed, fake relay, and temporary storage. Import, analyze, plan one chapter, verify autoplay, pause/resume, finish, load archive, exact replay, and assert source/scene hashes plus zero relay frames in dry-run mode.

- [ ] **Step 2: Document privacy and operation**

Document supported formats/size, one-time analysis/cache key, OpenRouter data implications, faithful behavior, speeds, pause/resume, archive contents, and how to delete local imported content.

- [ ] **Step 3: Run complete verification**

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.venv\Scripts\python.exe -m compileall -q backend tests
npm --prefix frontend ci
npm --prefix frontend audit --audit-level=high
npm --prefix frontend run build
```

Expected: all commands exit 0; no test calls an external model or relay.

- [ ] **Step 4: Push the reviewed branch**

```bash
git push -u origin codex/mvp2-faithful-novel-mode
```

- [ ] **Step 5: Run short real-device acceptance**

With a non-sensitive short chapter and cap 40 or lower, verify plan completes before output, A/B independently follow scene intent, pause immediately clears, resume uses the selected cursor, finish saves, and exact replay follows the same requested timeline.

- [ ] **Step 6: Tag only after acceptance**

```bash
git tag -a mvp2-faithful-novel-mode -m "MVP2 faithful novel mode accepted"
git push origin mvp2-faithful-novel-mode
```
