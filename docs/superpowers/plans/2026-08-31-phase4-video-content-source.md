# Phase 4 Local Video Content Source Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import a local video, derive stable timecoded scenes from subtitles and sampled keyframes, generate A/B directives, and keep safe device playback synchronized to the video clock with manual timecode overrides.

**Architecture:** Implement video as another content-source adapter. Metadata/subtitle/keyframe analysis produces stable timecoded scenes; the existing planner/override/timeline packages resolve plot events; `VideoTimelineSynchronizer` advances only from the frontend video clock while the shared independent A/B cycle runners provide raw-cycle gaps inside each active scene and route output through the existing safety path.

**Tech Stack:** Python 3.12, existing optional OpenCV dependency, stdlib subtitle parser/hash support, FastAPI, MVP timeline/story/override packages, browser HTML5 video, React 19, TypeScript, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-31-randomized-timeline-novel-mode-design.md`

**Depends on:** Accepted tag `phase3-authoring-replay-library`.

## Global Constraints

- Local video files only. No live URL capture, browser scraping, DRM bypass, or camera/microphone reaction.
- Prefer subtitles for scene text; sample bounded keyframes when subtitles are absent or sparse.
- Video clock is authoritative. Wall-clock timers cannot continue after pause/seeking/disconnect.
- Pause and seek clear both channels before cursor movement.
- Resume occurs at a validated safe event boundary at or after the current timecode.
- Use Phase 3 force/prefer/random overrides keyed by stable video scene IDs.
- All generated frames/source material stay local except explicitly selected analysis payloads sent to the configured model.

---

### Task 1: Video source model, safe import, and metadata

**Files:**
- Create: `backend/video/__init__.py`
- Create: `backend/video/models.py`
- Create: `backend/video/source.py`
- Create: `tests/test_video_source.py`
- Modify: `backend/config.py`
- Modify: `config/config.example.yaml`

- [ ] **Step 1: Write import/metadata tests**

Test extension allowlist, size limit, sanitized filename, original SHA-256, duration/fps/dimensions validation, zero-duration rejection, and opaque server-owned storage. Inject a fake metadata probe so unit tests require no codec.

```python
class VideoSourceTests(unittest.TestCase):
    def test_import_uses_opaque_path_and_validated_metadata(self):
        probe = FakeVideoProbe(duration_ms=12000, fps=24.0, width=1280, height=720)
        imported = VideoSourceLoader(self.root, 1024, probe).load("../clip.mp4", b"video")
        self.assertEqual(imported.original_name, "clip.mp4")
        self.assertEqual(imported.metadata.duration_ms, 12000)
        self.assertNotIn("clip.mp4", str(imported.storage_path))
```

- [ ] **Step 2: Implement source contract**

Define `VideoSource`, `VideoMetadata`, `SubtitleCue`, `VideoScene`, and `VideoSceneMap`. Allow configured `.mp4`, `.webm`, and `.mkv`; store as `data/videos/<uuid>/source.<ext>` after streaming size/hash validation. Probe metadata through an injected interface backed by OpenCV. Validate finite positive fps/duration and bounded dimensions.

Add configuration:

```yaml
video:
  import_dir: data/videos
  analysis_dir: data/video_analysis
  max_source_mb: 1024
  keyframe_interval_s: 8
  max_keyframes: 120
  subtitle_sparse_ratio: 0.25
  archive_embed_max_mb: 200
```

- [ ] **Step 3: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_video_source -v`

Commit: `feat: import and probe local video sources`

### Task 2: SRT/VTT parsing and keyframe sampling

**Files:**
- Create: `backend/video/subtitles.py`
- Create: `backend/video/keyframes.py`
- Create: `tests/test_video_subtitles.py`
- Create: `tests/test_video_keyframes.py`

- [ ] **Step 1: Write parser and sampler tests**

Cover BOM, CRLF, SRT comma milliseconds, VTT dot milliseconds, cue tags, overlapping cues, out-of-range timestamps, empty cues, sparse coverage, deterministic sample timestamps, maximum-frame bound, and cleanup after extraction failure.

- [ ] **Step 2: Implement subtitle normalization**

Parse into ordered `SubtitleCue(start_ms, end_ms, text)`. Strip markup, normalize whitespace, clamp cues to video duration, merge only exact-adjacent duplicate text, and reject files with no usable cue. Calculate subtitle coverage as union duration divided by video duration.

- [ ] **Step 3: Implement bounded keyframe sampling**

When no subtitle exists or coverage is below `subtitle_sparse_ratio`, sample at configured intervals plus subtitle-gap boundaries, deduplicate timestamps, and cap at `max_keyframes`. Save JPEGs under the source's analysis directory using timestamp-derived names. Return hash, dimensions, and timecode; never log or embed image bytes in normal state responses.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_video_subtitles tests.test_video_keyframes -v`

Commit: `feat: parse subtitles and sample video keyframes`

### Task 3: Timecoded scene analysis and planning

**Files:**
- Create: `backend/video/analyzer.py`
- Create: `backend/video/planner.py`
- Create: `backend/video/analysis_store.py`
- Create: `tests/test_video_analyzer.py`
- Create: `tests/test_video_planner.py`

- [ ] **Step 1: Write mocked analysis tests**

Test subtitle-only analysis, subtitle-plus-keyframe analysis, vision-model empty response, invalid/out-of-order timecodes, cache hit, prompt/model/DLC cache invalidation, A/B independent directives, override merge, and chapter/timeline boundary validation. No test calls OpenRouter.

- [ ] **Step 2: Implement stable timecoded scenes**

Cache key includes video hash, subtitle hash, keyframe-set hash, model, prompt version, and DLC version. Produce ordered, non-overlapping `vid-sc-000001` scene IDs with `start_ms`, `end_ms`, text/summary, pace, and major-event flag. Subtitle text is primary; vision inference fills gaps. Reject any scene outside media duration.

- [ ] **Step 3: Plan using existing directives and overrides**

For each video scene, request/validate A/B `keep/set/stop`, merge force/prefer/random overrides, resolve waveform/strength with the timeline resolver, and ensure every event offset falls inside its scene timecode. Validate the entire selected range before making it playable.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_video_analyzer tests.test_video_planner -v`

Commit: `feat: analyze and plan timecoded video scenes`

### Task 4: Video-clock synchronization state machine

**Files:**
- Create: `backend/video/synchronizer.py`
- Modify: `backend/timeline/player.py`
- Create: `tests/test_video_synchronizer.py`

- [ ] **Step 1: Write fake-clock transition tests**

Cover monotonic playback ticks, duplicate/out-of-order ticks, pause clear, seek-forward clear/cursor change, seek-back clear/cursor change, disconnect pause, stale session token rejection, clock drift tolerance, tab-stall recovery, finish, and safety-adjusted event recording.

```python
class VideoSynchronizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_seek_clears_before_reposition(self):
        sync = self.make_sync(events_at_ms=[1000, 5000, 9000])
        await sync.start(session_token="s1")
        await sync.tick("s1", 5200, playing=True, seeked=True)
        self.assertEqual(self.clear_order, ["clear"])
        self.assertEqual(sync.cursor, 2)

    async def test_stale_tick_cannot_execute(self):
        sync = self.make_sync(events_at_ms=[1000])
        await sync.start(session_token="new")
        await sync.tick("old", 1200, playing=True, seeked=False)
        self.assertEqual(self.executed, [])
```

- [ ] **Step 2: Implement authoritative tick protocol**

The frontend sends `session_token`, `current_time_ms`, `playing`, `playback_rate`, `seeked`, and a monotonically increasing tick sequence. The synchronizer accepts only the active token and increasing sequence, clears before any seek/pause cursor update, selects the first safe event at or after the timecode, and executes each event at most once per continuous playback segment. A missed range after a long stall skips stale events rather than firing a burst.

- [ ] **Step 3: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_video_synchronizer tests.test_timeline_player -v`

Commit: `feat: synchronize timeline output to video clock`

### Task 5: Video APIs and secure media serving

**Files:**
- Modify: `backend/main.py`
- Create: `tests/test_video_endpoints.py`

- [ ] **Step 1: Write endpoint tests**

Cover multipart video/subtitle upload, unsupported/oversize rejection, range requests, source-ID isolation, analyze/plan errors, clock token/replay protection, pause/seek/finish, and no arbitrary path access.

- [ ] **Step 2: Implement API surface**

```text
POST /api/video/import                              multipart video -> VideoSourceSummary
POST /api/video/{source_id}/subtitles               multipart SRT|VTT -> SubtitleSummary
POST /api/video/{source_id}/analyze                 {} -> VideoAnalysisStatus
GET  /api/video/{source_id}/scenes                  -> VideoSceneMapSummary
POST /api/video/{source_id}/play                    {"start_ms":0,"end_ms":null} -> VideoSessionState
POST /api/video/clock                               VideoClockTick -> VideoSessionState
POST /api/video/pause                               {} -> VideoSessionState
POST /api/video/seek                                {"time_ms":integer} -> VideoSessionState
POST /api/video/finish                              {} -> ReplaySummary
GET  /api/video/{source_id}/media                   -> range-capable media response
```

Media responses resolve only through opaque source IDs. Support bounded single-range requests needed by HTML5 video and set no-store/private headers.

- [ ] **Step 3: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_video_endpoints -v`

Commit: `feat: expose local video analysis and clock APIs`

### Task 6: Video player and manual timecode authoring UI

**Files:**
- Create: `frontend/src/components/VideoImport.tsx`
- Create: `frontend/src/components/VideoPlayer.tsx`
- Create: `frontend/src/components/VideoTimeline.tsx`
- Modify: `frontend/src/components/SceneEditor.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/store.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/Sidebar.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Build import/analysis flow**

Choose a local video and optional SRT/VTT, show metadata/hash/analysis status, and explain whether subtitle-only or subtitle-plus-keyframes will be sent to the configured model. Require explicit analysis start.

- [ ] **Step 2: Implement video clock publisher**

Use the HTML5 video element's time, play/pause/seeking/ended events, plus a bounded active-playback tick. Generate one session token per backend playback start and increasing tick sequence. Stop ticks immediately on pause, unmount, network/API failure, or session replacement; call backend pause before allowing local playback to continue after an API failure.

- [ ] **Step 3: Add timecoded timeline/editor**

Show analyzed scene ranges and current event. Clicking a scene seeks through the backend seek route first, then updates the video element. Reuse SceneEditor to set force/prefer/random A/B directives and optional strength/duration locks against video scene IDs.

- [ ] **Step 4: Verify and commit**

Run: `npm --prefix frontend run build`

Commit: `feat: add synchronized local video player`

### Task 7: Video replay archive policy

**Files:**
- Modify: `backend/timeline/models.py`
- Modify: `backend/timeline/replay_store.py`
- Modify: `backend/timeline/replay_index.py`
- Create: `tests/test_video_replay_archive.py`

- [ ] **Step 1: Test embedded and referenced archives**

At or below `archive_embed_max_mb`, embed the source and verify checksum. Above it, store a normalized local reference plus hash, label the replay non-portable, and require the referenced file/hash before playback. Missing/mismatched references are rejected before device output. Export warns and offers an explicit large embedded copy.

- [ ] **Step 2: Implement schema v3 compatibility**

Add `source_storage=embedded|local_reference`, video metadata, subtitle checksum, scene map, and synchronization version. Maintain v1/v2 readers. Exact video replay uses stored resolved events and the video clock; it makes no analysis/model calls.

- [ ] **Step 3: Verify and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_video_replay_archive tests.test_replay_schema_migration -v`

Commit: `feat: archive portable and referenced video replays`

### Task 8: Phase 4 integration and release gate

**Files:**
- Create: `tests/test_phase4_video_integration.py`
- Modify: `README.md`

- [ ] **Step 1: Add dry-run integration**

Use a fake video probe/keyframe extractor, generated SRT, fake structured/vision client, fixed seed, fake relay, and simulated video ticks. Verify import, analysis, full-range validation, play, pause, forward/back seek, manual override, finish, exact replay, stale tick rejection, and zero relay frames.

- [ ] **Step 2: Run complete verification**

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.venv\Scripts\python.exe -m compileall -q backend tests
npm --prefix frontend ci
npm --prefix frontend audit --audit-level=high
npm --prefix frontend run build
```

- [ ] **Step 3: Push and perform real-device acceptance**

Push `codex/phase4-video-source`. With a short local video and cap 40 or lower, verify start sync, A/B output, pause clearing, forward/back seek clearing, no stale burst after tab stall, finish, and exact replay.

- [ ] **Step 4: Tag only after acceptance**

```bash
git tag -a phase4-video-content-source -m "Phase 4 local video content source accepted"
git push origin phase4-video-content-source
```
