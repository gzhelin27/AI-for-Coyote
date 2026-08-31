# AI-for-Coyote Project Memory

## Authority and scope

- The user has delegated routine Git management for this project to Codex: remotes, branches, worktrees, commits, tags, and pushes to the user's fork may be handled without repeated confirmation.
- Never push to the upstream repository or open an upstream pull request unless the user explicitly asks.
- Preserve user data and local configuration. Never commit API keys, device configuration, private DLC content, logs, generated replay data, or imported novels.
- Work incrementally. Do not implement a later phase before the previous phase passes its release gate and the user accepts it.

## Git topology

- `upstream`: `https://github.com/indhg/AI-for-Coyote.git` — original project, read/fetch only.
- `origin`: `https://github.com/gzhelin27/AI-for-Coyote.git` — user's GitHub fork.
- `main` follows upstream history. Do not develop directly on `main`.
- `local/deployed-v1.1.2` preserves the pre-fork local deployment fixes and must not be rewritten.
- Feature branches use `codex/<phase>-<topic>` and are pushed to `origin`.
- Isolated worktrees live under `.worktrees/<branch-name>`; `.worktrees/` must remain ignored.
- Before new work: `git fetch upstream --prune`, start from the accepted integration branch, and inspect `git status`, `git diff`, and recent commits.
- Prefer small conventional commits. Never use destructive reset/checkout commands to discard user work.

## Verification commands

- Python tests: `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
- Python syntax: `.venv\Scripts\python.exe -m compileall -q backend tests`
- Frontend clean install: `npm --prefix frontend ci`
- Frontend production build: `npm --prefix frontend run build`
- On this Windows deployment, Node is bundled under `.runtime/node/`; add that directory to `PATH` for npm commands if the system PATH lacks Node.
- Never run `tests/probe_llm.py` as part of the normal suite: it makes a paid external API request.
- Real-device acceptance is manual and always follows a successful `dry_run` verification.

## Product priorities

1. Plot/content intent has priority.
2. Controlled unpredictability is secondary.
3. Current safety limits, emergency stop, disconnect clearing, and overheat protection always override both.

Randomness must be resolved into a deterministic timeline before exact replay. Do not put unbounded random device commands directly in the LLM prompt or relay layer.

## Accepted architecture

- Use a lightweight timeline layer between AI decisions and the existing `SafetyManager`/`GameLoop` execution path.
- AI chooses channel intent, waveform, and base strength where the active mode requires it.
- The timeline resolver applies scene-local strength jitter. Per-channel cycle runners apply the approved waveform-cycle pause policy without changing the AI plot-turn cadence.
- The player sends resolved actions through the existing safety layer; it never bypasses safety validation.
- The recorder stores both requested and effective results so safety clamping is auditable.
- A replay is exact only when its fully resolved timeline can be executed without current safety adjustments.

## Randomization decisions

- MVP1 starts with random selection from all allowed waveform presets.
- Strength jitter is an integer in `[-4, +4]` around the scene/base target.
- The user's current runtime output ceiling is 40 per channel. Keep that value in ignored local configuration/runtime state; committed code must never raise it automatically.
- Do not add extra cross-scene smoothing in the timeline resolver. The existing safety layer remains authoritative.
- A/B channels share the same plot beat but have independent intent, waveform, base strength, and effective cap.
- The AI/autopilot plot-turn interval remains unchanged. Random cycle pauses do not trigger model calls.
- One waveform cycle is its complete raw frame sequence; every frame represents 100 ms.
- After every completed cycle, each channel independently samples a pause multiplier: 40% exactly `0`, 30% uniformly from `0.1..1.0`, and 30% uniformly from `1.1..2.0`, all in 0.1 steps.
- Pause duration is `raw cycle duration × sampled multiplier`. During the pause no waveform frame is sent, but the current strength is retained.
- Pattern and strength are selected once per plot event. Only the pause multiplier is resampled after each completed cycle.
- Normal plot changes wait for the current cycle boundary. If the channel is already in its gap, the gap ends and the new event starts immediately. Stop, pause, disconnect, and emergency stop remain immediate.
- MVP1 uses one project-wide cycle-gap policy with no UI or DLC override. Manual waveform test/continuous controls retain their existing behavior.

## Session and replay decisions

- Starting automatic play establishes a game session.
- Pausing immediately clears both channels and preserves the current event position.
- Finishing, switching DLC/source, or clearing context normally ends and saves the completed session.
- Disconnect pauses rather than completes. Abnormal or incomplete sessions are not added to permanent history.
- Every normally completed session is retained until the user deletes it.
- Replay archives use the `.coyote-replay` extension and ZIP container format.
- Archives contain a manifest, complete resolved timeline, source/scene metadata, model/DLC/app versions, and the original novel when novel mode is used.
- Exact replay uses resolved values, not random ranges. Random ranges are metadata and are reused only by the later “similar version” feature.
- Exact replay also stores every completed cycle's selected pause multiplier and actual timing. Manual/operator pause duration is omitted from replay timing, while generated cycle gaps are preserved.
- If current caps alter a replay event, clamp it and mark the run as adjusted/non-exact; never raise current caps automatically.

## Novel-mode decisions

- Implement after MVP1.
- Initial formats: TXT, Markdown, and DOCX.
- The browser displays the novel text, current scene, and reading progress.
- Analyze the full novel once and cache by source hash; if a single request fails or exceeds provider limits, automatically analyze chapter chunks and merge them.
- Generate and validate a complete chapter timeline before playback. API failure prevents playback of that chapter.
- MVP2 implements only faithful mode. The later interpretation mode may expand transitions without changing major plot events.
- In novel mode the AI generates device intent only: for A/B independently choose `keep`, `set`, or `stop`; `set` supplies waveform and base strength.
- The scheduler supplies reading-speed timing and `±4` scene-local jitter; the same per-channel cycle runner supplies waveform-cycle gaps.
- Reading speed is selected as slow/standard/fast and adjusted by scene pacing.
- No camera or microphone reaction influences novel planning in MVP2. Chat remains available but does not change the novel's main plot.
- Playback begins automatically after chapter generation and validation; no mandatory preview screen.
- Pause clears output. Resume may continue from the event, chapter start, or beginning.

## Deferred phases

- Phase 3: manual scene waveform overrides (`force`, `prefer`, `random`), optional base strength/duration locks, interpretation mode, replay search/rating/export, similar-version generation, and cross-DLC adaptation.
- Phase 4: local video content source, subtitles/keyframes, video-clock synchronization, and manual video-node overrides.
- Do not add live web-video capture, branching narrative, or real-time camera/audio reactions unless separately approved.

## Release gates

- MVP1: deterministic random timeline and exact replay pass unit tests and dry-run integration.
- MVP2: TXT/MD/DOCX import, full analysis with fallback, dual-channel chapter plan, pause clearing, archive round-trip, and one short chapter pass dry-run and real-device acceptance.
- Each later phase requires its own tests, dry-run acceptance, user review, and separate Git tag before the next phase starts.
