"""Pure, all-or-nothing planning for one faithful story chapter."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from collections.abc import Mapping
from typing import Any, Literal

from backend.timeline.models import CycleGapPolicy, DirectiveMode, PlotEvent
from backend.timeline.randomizer import TimelineResolver

from .models import ImportedStory, StoryChapter, StoryMap, StoryScene


_CHANNELS = ("A", "B")
_SPEEDS = ("slow", "standard", "fast")
_SYSTEM_PROMPT = """\
You are a faithful chapter device-intent planner. Use only the supplied selected
chapter text and its ordered scene metadata. Return exactly one JSON object whose
`scenes` array has exactly one entry per supplied scene in the same order. Each
entry must repeat `scene_id` and contain exactly `A` and `B` channel directives.
A directive is exactly one of: {"mode":"keep"}, {"mode":"stop"}, or
{"mode":"set","base_strength":INTEGER}. Do not choose a waveform; waveform
selection and strength jitter belong exclusively to the seeded runtime resolver.
Preserve the source plot, scene coverage, and scene order without invention.
"""


class ChapterPlanError(RuntimeError):
    """A chapter could not be completely planned and dry-validated."""


@dataclass(frozen=True, slots=True)
class ChapterTimelineRequest:
    """A frame-free request ready for the existing timeline/session adapter."""

    plot_events: tuple[PlotEvent, ...]
    chapter_duration_ms: int
    cycle_gap_policy: CycleGapPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.plot_events, tuple) or not self.plot_events:
            raise ValueError("plot_events must be a non-empty tuple")
        if not all(isinstance(event, PlotEvent) for event in self.plot_events):
            raise ValueError("plot_events must contain PlotEvent values")
        if (
            isinstance(self.chapter_duration_ms, bool)
            or not isinstance(self.chapter_duration_ms, int)
            or self.chapter_duration_ms <= 0
        ):
            raise ValueError("chapter_duration_ms must be positive")
        if not isinstance(self.cycle_gap_policy, CycleGapPolicy):
            raise TypeError("cycle_gap_policy must be a CycleGapPolicy")

    @property
    def duration_ms(self) -> int:
        return self.chapter_duration_ms


@dataclass(frozen=True, slots=True)
class ValidatedChapterPlan:
    """Complete chapter identity, resolved events, timing, and dry request."""

    source_hash: str
    chapter_id: str
    speed: Literal["slow", "standard", "fast"]
    seed: int
    plot_events: tuple[PlotEvent, ...]
    chapter_duration_ms: int
    timeline_request: ChapterTimelineRequest

    def __post_init__(self) -> None:
        if not isinstance(self.source_hash, str) or not self.source_hash:
            raise ValueError("source_hash must be non-empty")
        if not isinstance(self.chapter_id, str) or not self.chapter_id:
            raise ValueError("chapter_id must be non-empty")
        if self.speed not in _SPEEDS:
            raise ValueError("speed must be slow, standard, or fast")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        if self.plot_events != self.timeline_request.plot_events:
            raise ValueError("timeline request must contain the resolved plot events")
        if self.chapter_duration_ms != self.timeline_request.chapter_duration_ms:
            raise ValueError("timeline request must contain the chapter duration")

    @property
    def duration_ms(self) -> int:
        return self.chapter_duration_ms

    @property
    def timeline(self) -> ChapterTimelineRequest:
        return self.timeline_request


@dataclass(frozen=True, slots=True)
class _ChannelIntent:
    mode: DirectiveMode
    base_strength: int | None = None


@dataclass(frozen=True, slots=True)
class _SceneIntent:
    scene_id: str
    channels: Mapping[str, _ChannelIntent]


class ChapterPlanner:
    """Plan one selected chapter without creating playback or device objects."""

    def __init__(
        self,
        client: Any,
        *,
        waveform_registry: Mapping[str, object],
        effective_caps: Mapping[str, int],
        reading_speed_cpm: Mapping[str, int | float],
        cycle_gap_policy: CycleGapPolicy,
        safety_adapter: Any,
        strength_jitter: int = 4,
    ) -> None:
        if not hasattr(client, "complete_json"):
            raise TypeError("client must provide complete_json")
        if not isinstance(waveform_registry, Mapping):
            raise TypeError("waveform_registry must be a mapping")
        waveforms = tuple(waveform_registry)
        if not waveforms or not all(
            isinstance(name, str) and name.strip() for name in waveforms
        ):
            raise ValueError("waveform_registry must contain named waveforms")
        if not isinstance(effective_caps, Mapping) or set(effective_caps) != set(_CHANNELS):
            raise ValueError("effective_caps must provide exactly A and B")
        caps: dict[str, int] = {}
        for channel in _CHANNELS:
            cap = effective_caps[channel]
            if isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= 200:
                raise ValueError("effective caps must be integer strengths in 0..200")
            caps[channel] = cap
        if not isinstance(reading_speed_cpm, Mapping) or set(reading_speed_cpm) != set(_SPEEDS):
            raise ValueError("reading_speed_cpm must provide slow, standard, and fast")
        speeds: dict[str, float] = {}
        for speed in _SPEEDS:
            value = reading_speed_cpm[speed]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("reading speeds must be positive finite numbers")
            speeds[speed] = float(value)
        if not speeds["slow"] < speeds["standard"] < speeds["fast"]:
            raise ValueError("reading speeds must increase from slow to fast")
        if not isinstance(cycle_gap_policy, CycleGapPolicy):
            raise TypeError("cycle_gap_policy must be a CycleGapPolicy")
        if not hasattr(safety_adapter, "validate"):
            raise TypeError("safety_adapter must provide validate(action)")
        if (
            isinstance(strength_jitter, bool)
            or not isinstance(strength_jitter, int)
            or strength_jitter != 4
        ):
            raise ValueError("strength_jitter must remain 4")

        self._client = client
        self._waveforms = waveforms
        self._caps = caps
        self._speeds = speeds
        self._policy = cycle_gap_policy
        self._safety = safety_adapter
        self._strength_jitter = strength_jitter

    async def plan(
        self,
        story: ImportedStory,
        story_map: StoryMap,
        chapter_id: str,
        *,
        speed: Literal["slow", "standard", "fast"],
        seed: int,
    ) -> ValidatedChapterPlan:
        chapter = self._select_chapter(story, story_map, chapter_id, speed, seed)
        offsets, duration_ms = self._scene_timing(chapter, story.text, speed)
        request = self._request_document(chapter, story.text)
        try:
            response = await self._client.complete_json(
                _SYSTEM_PROMPT,
                json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                "chapter_plan",
            )
        except Exception as exc:
            raise ChapterPlanError("chapter plan model request failed") from exc

        intents = self._parse_response(response, chapter)
        resolver = TimelineResolver(
            strength_jitter=self._strength_jitter,
            session_seed=seed,
        )
        events: list[PlotEvent] = []
        for index, (scene, intent, offset_ms) in enumerate(
            zip(chapter.scenes, intents, offsets, strict=True)
        ):
            actions = self._resolver_actions(intent)
            try:
                event = resolver.resolve_plot_event(
                    actions=actions,
                    current={"A": 0, "B": 0},
                    caps=self._caps,
                    enabled={"A": True, "B": True},
                    presets=self._waveforms,
                    event_id=f"{chapter.id}-evt-{index + 1:04d}",
                    scene_id=scene.id,
                    offset_ms=offset_ms,
                )
            except Exception as exc:
                raise ChapterPlanError(
                    f"chapter plan resolution failed for {scene.id}"
                ) from exc
            self._dry_validate_event(event)
            events.append(event)

        resolved_events = tuple(events)
        timeline_request = ChapterTimelineRequest(
            plot_events=resolved_events,
            chapter_duration_ms=duration_ms,
            cycle_gap_policy=self._policy,
        )
        return ValidatedChapterPlan(
            source_hash=story.source_sha256,
            chapter_id=chapter.id,
            speed=speed,
            seed=seed,
            plot_events=resolved_events,
            chapter_duration_ms=duration_ms,
            timeline_request=timeline_request,
        )

    def _select_chapter(
        self,
        story: object,
        story_map: object,
        chapter_id: object,
        speed: object,
        seed: object,
    ) -> StoryChapter:
        if not isinstance(story, ImportedStory):
            raise ChapterPlanError("story must be an ImportedStory")
        if not isinstance(story_map, StoryMap):
            raise ChapterPlanError("story_map must be a StoryMap")
        if speed not in _SPEEDS:
            raise ChapterPlanError("chapter plan speed must be slow, standard, or fast")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ChapterPlanError("chapter plan seed must be an integer")
        if story.source_sha256 != story_map.source_hash:
            raise ChapterPlanError("story source does not match the story map")
        if len(story.text) != story_map.text_length:
            raise ChapterPlanError("story length does not match the story map")
        if not isinstance(chapter_id, str) or not chapter_id:
            raise ChapterPlanError("chapter id must be non-empty")
        chapter = next(
            (item for item in story_map.chapters if item.id == chapter_id), None
        )
        if chapter is None:
            raise ChapterPlanError("chapter id is not present in the story map")
        return chapter

    def _scene_timing(
        self,
        chapter: StoryChapter,
        story_text: str,
        speed: str,
    ) -> tuple[tuple[int, ...], int]:
        cpm = self._speeds[speed]
        offsets: list[int] = []
        elapsed_ms = 0
        for scene in chapter.scenes:
            offsets.append(elapsed_ms)
            character_count = len(story_text[scene.start_offset : scene.end_offset])
            duration = character_count / cpm * 60_000 * scene.pace
            if not math.isfinite(duration) or duration <= 0:
                raise ChapterPlanError(
                    f"chapter plan timing failed for {scene.id}"
                )
            elapsed_ms += math.ceil(duration)
        if elapsed_ms <= 0:
            raise ChapterPlanError("chapter plan timing produced an empty chapter")
        return tuple(offsets), elapsed_ms

    def _request_document(
        self, chapter: StoryChapter, story_text: str
    ) -> dict[str, object]:
        return {
            "chapter_text": story_text[chapter.start_offset : chapter.end_offset],
            "scenes": [
                {
                    "scene_id": scene.id,
                    "start_offset": scene.start_offset,
                    "end_offset": scene.end_offset,
                    "summary": scene.summary,
                }
                for scene in chapter.scenes
            ],
            "capabilities": {
                channel: {
                    "modes": ["keep", "set", "stop"],
                    "set_fields": ["base_strength"],
                    "effective_cap": self._caps[channel],
                }
                for channel in _CHANNELS
            },
            "constraints": {
                "faithful_mode": True,
                "preserve_scene_order": True,
                "one_entry_per_scene": True,
                "waveform_source": "seeded_resolver",
            },
        }

    def _parse_response(
        self, response: object, chapter: StoryChapter
    ) -> tuple[_SceneIntent, ...]:
        if not isinstance(response, dict) or set(response) != {"scenes"}:
            raise ChapterPlanError("chapter plan response schema is invalid")
        raw_scenes = response["scenes"]
        if not isinstance(raw_scenes, list) or len(raw_scenes) != len(chapter.scenes):
            raise ChapterPlanError("chapter plan must contain exactly one entry per scene")

        parsed: list[_SceneIntent] = []
        for scene, raw_scene in zip(chapter.scenes, raw_scenes, strict=True):
            if not isinstance(raw_scene, dict) or set(raw_scene) != {"scene_id", "channels"}:
                raise ChapterPlanError(
                    f"chapter plan response schema is invalid for {scene.id}"
                )
            if raw_scene["scene_id"] != scene.id:
                raise ChapterPlanError(
                    f"chapter plan scene order is invalid at {scene.id}"
                )
            raw_channels = raw_scene["channels"]
            if not isinstance(raw_channels, dict) or set(raw_channels) != set(_CHANNELS):
                raise ChapterPlanError(
                    f"chapter plan must provide exactly A and B for {scene.id}"
                )
            channels = {
                channel: self._parse_directive(
                    raw_channels[channel], channel, scene, self._caps[channel]
                )
                for channel in _CHANNELS
            }
            parsed.append(_SceneIntent(scene_id=scene.id, channels=channels))
        return tuple(parsed)

    @staticmethod
    def _parse_directive(
        raw: object,
        channel: str,
        scene: StoryScene,
        cap: int,
    ) -> _ChannelIntent:
        if not isinstance(raw, dict):
            raise ChapterPlanError(
                f"chapter plan {channel} directive is invalid for {scene.id}"
            )
        mode_value = raw.get("mode")
        try:
            mode = DirectiveMode(mode_value)
        except (TypeError, ValueError) as exc:
            raise ChapterPlanError(
                f"chapter plan {channel} mode is invalid for {scene.id}"
            ) from exc
        expected_fields = {"mode", "base_strength"} if mode is DirectiveMode.SET else {"mode"}
        if set(raw) != expected_fields:
            raise ChapterPlanError(
                f"chapter plan {channel} fields are invalid for {scene.id}"
            )
        if mode is not DirectiveMode.SET:
            return _ChannelIntent(mode=mode)
        strength = raw["base_strength"]
        if (
            isinstance(strength, bool)
            or not isinstance(strength, int)
            or not 0 <= strength <= cap
        ):
            raise ChapterPlanError(
                f"chapter plan {channel} base strength exceeds its effective cap for {scene.id}"
            )
        return _ChannelIntent(mode=mode, base_strength=strength)

    @staticmethod
    def _resolver_actions(intent: _SceneIntent) -> list[dict[str, object]]:
        actions: list[dict[str, object]] = []
        for channel in _CHANNELS:
            directive = intent.channels[channel]
            if directive.mode is DirectiveMode.STOP:
                actions.append({"op": "clear", "channel": channel})
            elif directive.mode is DirectiveMode.SET:
                actions.append(
                    {
                        "op": "hold_strength",
                        "channel": channel,
                        "value": directive.base_strength,
                    }
                )
        return actions

    def _dry_validate_event(self, event: PlotEvent) -> None:
        for channel in _CHANNELS:
            directive = event.channels[channel]
            if directive.mode is DirectiveMode.KEEP:
                continue
            if directive.mode is DirectiveMode.STOP:
                actions = ({"op": "clear", "channel": channel},)
            else:
                actions = (
                    {
                        "op": "hold_strength",
                        "channel": channel,
                        "value": directive.resolved_strength,
                    },
                    {
                        "op": "pulse_cycle",
                        "channel": channel,
                        "pattern": directive.pattern,
                    },
                )
            for action in actions:
                try:
                    ok, reason, command = self._safety.validate(action)
                except Exception as exc:
                    raise ChapterPlanError(
                        f"chapter plan safety validation failed for {event.scene_id}"
                    ) from exc
                if not ok or command is None:
                    raise ChapterPlanError(
                        f"chapter plan safety validation failed for {event.scene_id}: {reason}"
                    )
