# Randomized Timeline, Replay, and Novel Mode Design

**Date:** 2026-08-31
**Status:** Accepted architecture; implementation is intentionally phased
**Repository:** `gzhelin27/AI-for-Coyote` fork of `indhg/AI-for-Coyote`

## 1. Purpose

Add controlled variety and content-driven playback without replacing the project's working relay, device protocol, safety manager, or DLC system. The design inserts a deterministic timeline layer between AI output and the existing execution path.

The product priority is:

1. plot or source-content intent;
2. controlled unpredictability;
3. safety overrides everything above it.

## 2. Scope and non-goals

The complete roadmap has four independently releasable stages:

1. MVP1 — randomized live timeline and exact replay;
2. MVP2 — faithful novel mode;
3. Phase 3 — manual authoring, interpretation mode, and replay library enhancements;
4. Phase 4 — local video source.

The first release does not include novel import, video, manual scene authoring, interpretation mode, camera/microphone adaptation, branching narrative, or cross-DLC replay conversion.

## 3. Existing system boundaries

The following code remains authoritative:

- `backend/safety.py`: channel enablement, runtime/hardware caps, maximum accepted device step, pulse duration, overheat behavior, and emergency stop.
- `backend/game_loop.py`: validated action execution, relay frame creation, waveform loops, output clearing, and device-state tracking.
- `backend/device_ops.py`: DG-LAB V4 RPC frame serialization and request IDs.
- `backend/relay_client.py`: relay connection and device transport.
- `config/waveforms.yaml`: allowed waveform names and data.
- DLC/character configuration: role, profile, prompts, and future behavior defaults.

The timeline layer may request actions but cannot send relay frames directly.

## 4. Target architecture

```text
AI turn or content-source scene
        │
        ▼
ChannelDirective[A/B]
  keep | set(pattern, base_strength) | stop
        │
        ▼
TimelineResolver
  waveform policy + ±4 jitter + interval profile + duration
        │
        ▼
TimelineEvent[] (fully resolved and serializable)
        │
        ▼
TimelinePlayer ──► GameLoop.execute_actions()
                        │
                        ▼
                  SafetyManager
                        │
                        ▼
                 relay/device output
        │
        ▼
SessionRecorder
  requested action + effective result + timestamp
```

### 4.1 Modules

- `backend/timeline/models.py`: enums and dataclasses for directives, profiles, events, timelines, session state, and replay manifests.
- `backend/timeline/randomizer.py`: seeded waveform, jitter, duration, and weighted-interval resolution.
- `backend/timeline/player.py`: monotonic-clock playback, pause/resume/finish, event cursor, and callbacks.
- `backend/timeline/replay_store.py`: safe `.coyote-replay` ZIP read/write/list/delete/download.
- `backend/timeline/session.py`: live-session lifecycle, recording, exact-replay state, and integration callbacks.
- `backend/story/*` in MVP2: source interface, novel extraction, analysis cache, chapter planning, and reading timing.

Each module has one responsibility and communicates through serializable domain objects. `GameLoop` remains the only path to device execution.

## 5. Domain model

### 5.1 Channel directive

```json
{
  "channel": "A",
  "mode": "set",
  "pattern": "呼吸",
  "base_strength": 24
}
```

`mode` is one of:

- `keep`: do not change the channel at this beat;
- `set`: apply a selected waveform and base strength;
- `stop`: clear and zero the channel.

A and B share the same plot beat but are inferred independently using their configured accessory/location, baseline, enabled state, and effective cap.

### 5.2 Randomization profile

Schema version 1 contains:

- `strength_jitter: 4`;
- waveform policy `all_allowed` for MVP1;
- weighted interval bands: normal `0.70 / 6–12s`, short `0.20 / 2–5s`, long `0.10 / 15–25s`;
- per-DLC override fields;
- random seed.

The resolver chooses an integer strength inside `base_strength ± 4`, then clips to `0..effective_cap`. It does not add cross-scene smoothing. Safety validation still applies afterward.

At design time the user's selected runtime ceiling is 40 per channel. That personal value remains in ignored local configuration/runtime state; neither timeline generation nor replay may raise it automatically.

### 5.3 Resolved timeline event

```json
{
  "event_id": "evt-000042",
  "scene_id": "live-turn-12",
  "offset_ms": 18300,
  "duration_ms": 6500,
  "next_gap_ms": 3200,
  "requested_actions": [
    {"op": "hold_strength", "channel": "A", "value": 27},
    {"op": "pulse", "channel": "A", "pattern": "呼吸", "duration_s": 6.5}
  ],
  "source": {
    "base_strength": 24,
    "random_range": [-4, 4],
    "plot_event": "live-turn-12"
  }
}
```

After execution the recorder adds effective values, safety adjustments, send status, and failure reasons. Exact replay schedules `requested_actions` at the recorded offsets and compares the new effective results to the original effective results.

## 6. Session lifecycle

States are `idle`, `running`, `paused`, `finishing`, `completed`, and `replaying`.

- Starting automatic play creates a session if none exists, otherwise resumes the paused session.
- Pausing cancels scheduled waits, clears loops, zeros both channels, and keeps the event cursor.
- Finishing clears output, finalizes the manifest, writes the replay archive atomically, and returns to idle.
- Switching DLC/source or clearing context performs a normal finish first.
- Device disconnect pauses and clears but does not finish.
- Process crash or abnormal termination does not create a permanent history entry.
- Emergency stop cancels player work and remains authoritative; session resume requires the existing emergency-stop resume flow.

Only normally completed sessions are permanently saved.

## 7. Replay archive

`.coyote-replay` is a ZIP file with path traversal protection, schema validation, and atomic write/rename.

```text
<uuid>.coyote-replay
├── manifest.json
├── timeline.json
├── scenes.json          # novel/video phases
└── source.<extension>   # novel/video phases
```

The manifest records schema version, app commit, model, DLC role/profile/version, random profile, seed, safety-cap snapshot, mode, timestamps, completion status, source hash, and archive file checksums.

Replay rules:

- exact replay never samples random ranges;
- current safety rules are always re-applied;
- if current safety changes an effective event, playback continues with the clamped value and the run is marked `adjusted`, not exact;
- corrupt, unsupported, or path-unsafe archives are rejected before any device action;
- replay makes no LLM calls;
- resume supports event cursor, chapter start, or beginning.

## 8. MVP1 — randomized live timeline

MVP1 applies to the existing automatic mode.

- An AI turn remains responsible for narrative/chat output and base device actions.
- The resolver converts the turn's channel actions into a serializable live segment.
- MVP1 randomly selects from all allowed waveform presets for each channel that is set.
- Strength is resolved independently per channel inside the base target `±4`.
- The weighted interval chooses the next automatic turn delay.
- Stop/clear intent is preserved and never randomized into output.
- A/B are recorded independently.
- The UI exposes start/resume, pause, finish-and-save, current waveform/strength, and a minimal replay list.
- Debug reasoning and full parameter traces remain stored but hidden from the normal control view.

MVP1 deliberately uses one resolved segment per live AI turn. Multi-event choreography inside a single plot beat is deferred until the basic timeline/replay loop is proven.

## 9. MVP2 — faithful novel mode

### 9.1 Import and analysis

- Accept `.txt`, `.md`, and `.docx` with filename sanitization and size limits.
- Preserve original bytes for archive embedding.
- Extract normalized text and calculate SHA-256.
- Analyze the entire novel once and cache the scene map by source hash, model, prompt version, and DLC version.
- If one full request fails or exceeds the endpoint limit, split by detected chapters/size, analyze chunks, and merge stable chapter/scene IDs.
- Do not resend the full novel on every playback turn.

### 9.2 Chapter plan

Before playback, generate the entire selected chapter plan. For each scene, the AI returns independent A/B directives: `keep`, `set`, or `stop`; `set` includes an allowed waveform and base strength.

The scheduler calculates:

- scene duration from normalized character count and reading-speed preset;
- a scene pace multiplier from analysis;
- weighted event intervals;
- `±4` strength jitter;
- resolved timeline offsets and actions.

The chapter must parse, validate, and pass a dry timeline validation before playback starts automatically. Partial chapter output is never played.

### 9.3 Reader behavior

- Display original text, chapter, current scene, and progress.
- Reading speeds: slow `250`, standard `400`, fast `600` normalized Chinese characters per minute.
- Chat remains available but does not change the main plot.
- Camera/microphone data is not sent to the novel planner.
- Pause clears output; resume starts from a selected safe cursor.
- MVP2 provides faithful mode only.

Novel replay embeds the original source, scene map, and complete resolved timeline in the archive.

## 10. Phase 3 — authoring and replay library

- Add scene override modes `force`, `prefer`, and `random`.
- Allow optional locks for base strength and duration.
- Store overrides in a sidecar structure; never modify the original novel.
- Add interpretation mode: major plot order remains fixed while transition density and scene duration may expand.
- Add replay search/filter by DLC, source, date, rating, and exact/adjusted status.
- Add rename, rating, delete, export, exact replay, and similar-version generation.
- Similar versions reuse base targets/random ranges and resample; they are never labeled exact.
- Cross-DLC copy validates waveform availability and caps, reports substitutions, and creates a new derived archive.

## 11. Phase 4 — local video source

- Implement the existing content-source interface for local video, not live web capture.
- Parse SRT/VTT subtitles and sample keyframes when subtitles are insufficient.
- Build scene nodes with stable timecodes and produce channel directives using the same planner schema.
- Drive timeline progress from the video clock.
- Pausing/seeking clears output and repositions to a safe event boundary.
- Embed or reference the source according to archive size policy; checksums are mandatory.
- Reuse Phase 3 manual overrides on video timecode nodes.

## 12. API and state surface

MVP1 adds:

- `POST /api/session/start`
- `POST /api/session/pause`
- `POST /api/session/resume`
- `POST /api/session/finish`
- `GET /api/replays`
- `GET /api/replays/{replay_id}/download`
- `POST /api/replays/{replay_id}/play`
- `POST /api/replays/playback/pause`
- `POST /api/replays/playback/resume`
- `POST /api/replays/playback/stop`

`/api/state` and WebSocket state include session mode/status, cursor, current event, current waveform/strength, replay exact/adjusted state, and next event time. No API response exposes private source text unless the active reader endpoint requests it.

MVP2 adds source upload, analysis status, chapter selection, reader text/progress, and chapter start endpoints.

## 13. Storage and privacy

- Runtime data lives under ignored `data/` directories.
- API keys and personal device/DLC configuration remain in existing ignored files.
- Imported novels and replay archives are local only and are never committed.
- Logs redact source text and API credentials; log IDs/hashes instead.
- Upload/archive extraction enforces type, size, entry count, normalized paths, and target-directory containment.

## 14. Error handling

- LLM generation failure: no chapter playback; retain a retryable analysis/plan error.
- Invalid waveform or channel directive: reject the chapter plan, report the exact scene, and do not partially play.
- Relay disconnect: clear and pause.
- Safety rejection: record it; continue only when the remaining event is still meaningful, and mark replay adjusted.
- Corrupt replay: reject before player creation.
- Pause/finish timeout: issue the existing clear/zero fallback and report the failure.
- App restart: incomplete temporary sessions are discarded from permanent history.

## 15. Verification and release gates

Every phase uses unit tests, Python compile checks, frontend production build, dry-run acceptance, and small commits.

MVP1 gate:

- seeded resolution is deterministic;
- strength always stays in `base ±4` and effective caps;
- interval selection follows configured bands;
- archive round-trip preserves resolved events;
- pause and finish clear output;
- exact replay reproduces the same requested timeline;
- safety differences mark adjusted replay.

MVP2 gate:

- TXT/MD/DOCX extraction works;
- full analysis and chunk fallback yield stable IDs;
- A/B planning validates waveform and strength independently;
- chapter playback never starts from a partial plan;
- reader progress and pause/resume remain synchronized;
- one short chapter passes dry-run and real-device acceptance.

No later phase begins until the preceding gate is accepted by the user.
