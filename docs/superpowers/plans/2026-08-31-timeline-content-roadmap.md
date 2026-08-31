# Timeline and Content Modes Delivery Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to run one phase at a time. For each code task, use superpowers:test-driven-development; before declaring a phase complete, use superpowers:verification-before-completion.

**Goal:** Deliver the accepted lightweight timeline architecture through four independently usable releases while keeping the deployed checkout stable and every device command behind the existing safety layer.

**Architecture:** The roadmap builds one deterministic timeline core first, then adds novel, authoring/library, and video adapters. Later phases consume the same `ChannelDirective -> TimelineResolver -> TimelinePlayer -> GameLoop.execute_actions() -> SafetyManager` path; they do not create alternate device paths.

**Tech Stack:** Python 3.12, FastAPI, React 19, TypeScript, Zustand, stdlib ZIP/JSON/hash support, `python-docx` in MVP2, and the existing optional OpenCV stack in Phase 4.

**Accepted design:** `docs/superpowers/specs/2026-08-31-randomized-timeline-novel-mode-design.md`

## Git delivery model

- Upstream source: `upstream = https://github.com/indhg/AI-for-Coyote.git`.
- Maintained fork: `origin = https://github.com/gzhelin27/AI-for-Coyote.git`.
- Deployment branch remains `local/deployed-v1.1.2` until a release gate is accepted.
- Planning branch is `codex/mvp-timeline-roadmap` and contains only memory/design/plan artifacts.
- Implementation branches are created from the latest accepted release tag, never from the dirty deployment checkout.
- Expected sequence:
  1. `codex/mvp1-randomized-timeline-replay` from the accepted planning base;
  2. `codex/mvp2-faithful-novel-mode` from the accepted MVP1 tag;
  3. `codex/phase3-authoring-replay-library` from the accepted MVP2 tag;
  4. `codex/phase4-video-source` from the accepted Phase 3 tag.
- Each task is one reviewable commit. Each phase ends with a documentation/integration-test commit.
- Push feature branches only to `origin`. Add no user API key, runtime configuration, imported source, or replay archive to Git.
- Before incorporating a new upstream release: fetch `upstream`, inspect the diff and changelog, merge into a temporary integration branch, run the complete gate, then update the deployment branch.

## Release sequence

| Gate | User-visible outcome | Explicitly deferred | Detailed plan |
|---|---|---|---|
| MVP1 | Random waveform, strength `base ±4`, weighted interval, completed-session archive, exact replay | Novel/video, authoring, search/rating | `2026-08-31-mvp1-randomized-timeline-replay.md` |
| MVP2 | TXT/MD/DOCX import, one-time analysis, faithful chapter autoplay, built-in reader | Interpretation, manual nodes, video | `2026-08-31-mvp2-faithful-novel-mode.md` |
| Phase 3 | Force/prefer/random scene overrides, interpretation mode, replay library | Local video | `2026-08-31-phase3-authoring-replay-library.md` |
| Phase 4 | Local video/subtitle/keyframe analysis synchronized to video clock | Live web capture, camera/mic reaction | `2026-08-31-phase4-video-content-source.md` |

## Cross-phase invariants

1. The safety manager may clamp or reject any requested event; the timeline layer never overrides it.
2. A/B are inferred independently but share one source scene/plot beat.
3. Runtime strength caps are user-owned local state. The code never raises them automatically.
4. Pause, source switch, disconnect, and finish clear/zero both channels.
5. Only normally completed sessions become permanent replays.
6. Exact replay reuses the resolved event sequence and makes no model calls; safety-adjusted playback is labeled adjusted.
7. Full source text and credentials are omitted from logs and Git.
8. A later phase may extend schemas only through versioned, backward-compatible readers and migrations.

## Phase execution checklist

For each detailed phase plan:

- [ ] Create the named branch in an isolated worktree.
- [ ] Confirm the base commit/tag and a clean worktree.
- [ ] Execute tasks in order with a failing test before implementation.
- [ ] Run focused tests after each task and commit only that task's files.
- [ ] Run complete Python tests, compileall, dependency audit, and frontend production build.
- [ ] Perform dry-run acceptance with no relay frames.
- [ ] Review the branch diff against this roadmap and the accepted design.
- [ ] Push to `origin` and record the commit used for acceptance.
- [ ] Perform the short real-device gate only with current cap at 40 or below.
- [ ] Create and push the annotated phase tag only after user acceptance.
- [ ] Update the deployment checkout by fast-forward or reviewed merge; never overwrite local runtime configuration.

## Stop conditions

Stop a phase and keep the preceding release deployed when any of these occurs:

- a command path bypasses `GameLoop.execute_actions()` or `SafetyManager`;
- pause/disconnect cannot prove output was cleared;
- archive validation occurs after device playback begins;
- a novel chapter starts before its entire plan validates;
- video seek resumes at a stale event instead of a safe boundary;
- a verification command fails or real-device behavior differs from dry-run expectations.

Record the failure, preserve the branch for diagnosis, and return to the last accepted tag. Do not compensate by loosening safety limits or skipping the release gate.

## Definition of roadmap completion

The roadmap is complete only when all four tags are accepted and the deployment checkout points to the accepted Phase 4 commit. Until then, each accepted tag is a fully supported stopping point; the project does not depend on unfinished later phases.
