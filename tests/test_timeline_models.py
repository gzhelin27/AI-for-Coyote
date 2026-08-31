import unittest

from backend.timeline.models import (
    ChannelPlaybackState,
    ChannelDirective, CycleGapPolicy, CycleRecord, DirectiveMode, PlotEvent,
    ReplayManifest, SessionState, SessionStatus, Timeline,
)


class FixedRng:
    def __init__(self, rolls: list[int], values: list[int]):
        self.rolls = iter(rolls)
        self.values = iter(values)

    def randrange(self, stop: int) -> int:
        self.asserted_stop = stop
        return next(self.rolls)

    def randint(self, low: int, high: int) -> int:
        value = next(self.values)
        if not low <= value <= high:
            raise AssertionError((low, value, high))
        return value


class TimelineModelTests(unittest.TestCase):
    def make_cycle(self, **overrides):
        values = {
            "channel": "A",
            "cycle_index": 1,
            "plot_event_id": "evt-000001",
            "pattern": "呼吸",
            "waveform_hash": "wave-hash",
            "requested_strength": 24,
            "effective_strength": 24,
            "active_start_offset_ms": 0,
            "raw_duration_ms": 1200,
            "gap_tenths": 7,
            "planned_gap_ms": 840,
            "actual_gap_ms": 840,
            "completed": True,
            "interruption_reason": None,
        }
        values.update(overrides)
        return CycleRecord(**values)

    def test_gap_policy_uses_exact_three_bands(self):
        policy = CycleGapPolicy()
        rng = FixedRng([0, 39, 40, 69, 70, 99], [1, 10, 11, 20])
        self.assertEqual([policy.sample_tenths(rng) for _ in range(6)], [0, 0, 1, 10, 11, 20])

    def test_gap_duration_uses_raw_frame_count(self):
        policy = CycleGapPolicy()
        self.assertEqual(policy.cycle_ms(frame_count=12), 1200)
        self.assertEqual(policy.gap_ms(frame_count=12, tenths=7), 840)

    def test_gap_policy_rejects_invalid_weights_and_frame_duration(self):
        invalid_policies = (
            {"zero_weight": -1, "short_weight": 51, "long_weight": 50},
            {"zero_weight": 40, "short_weight": 30, "long_weight": 29},
            {"frame_ms": 99},
        )
        for values in invalid_policies:
            with self.subTest(values=values), self.assertRaises(ValueError):
                CycleGapPolicy(**values)

    def test_cycle_record_requires_requested_and_effective_strengths(self):
        for field in ("requested_strength", "effective_strength"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.make_cycle(**{field: None})

    def test_replay_manifest_rejects_malformed_payloads(self):
        valid = {
            "schema_version": 1,
            "replay_id": "replay-1",
            "session_id": "session-1",
            "seed": 7,
            "status": "completed",
            "mode": "autopilot",
            "adjusted": False,
        }
        invalid_payloads = (
            {"random_profile": "not-a-mapping"},
            {"random_profile": ""},
            {"safety_caps": []},
            {"checksums": []},
            {"app_commit": 7},
            {"created_at": 7},
        )
        for malformed in invalid_payloads:
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                ReplayManifest.from_dict({**valid, **malformed})

    def test_replay_manifest_and_session_state_round_trip(self):
        manifest = ReplayManifest(
            schema_version=1,
            replay_id="replay-1",
            session_id="session-1",
            seed=7,
            status=SessionStatus.COMPLETED,
            mode="autopilot",
            app_commit="abc123",
            model="test-model",
            dlc_role="role",
            dlc_profile="profile",
            dlc_version="v1",
            random_profile={"strength_jitter": 4},
            safety_caps={"A": 40, "B": 40},
            created_at="2026-08-31T00:00:00Z",
            completed_at="2026-08-31T00:01:00Z",
            source_hash="source-hash",
            checksums={"timeline.json": "timeline-hash"},
        )
        state = SessionState(
            status=SessionStatus.REPLAYING,
            mode="replay",
            session_id="session-1",
            replay_id="replay-1",
            cursor=2,
            current_event_id="evt-000002",
            adjusted=True,
            channels={
                "A": ChannelPlaybackState(
                    phase="cycle",
                    pattern="呼吸",
                    strength=17,
                    cycle_index=3,
                    next_cycle_start_ms=900,
                ),
                "B": ChannelPlaybackState(),
            },
        )
        self.assertEqual(ReplayManifest.from_dict(manifest.to_dict()), manifest)
        self.assertEqual(SessionState.from_dict(state.to_dict()), state)

    def test_replay_manifest_reader_preserves_omitted_defaults(self):
        data = {
            "schema_version": 1,
            "replay_id": "replay-1",
            "session_id": "session-1",
            "seed": 7,
            "status": "completed",
            "mode": "autopilot",
        }
        self.assertEqual(
            ReplayManifest.from_dict(data),
            ReplayManifest(
                schema_version=1,
                replay_id="replay-1",
                session_id="session-1",
                seed=7,
                status=SessionStatus.COMPLETED,
                mode="autopilot",
            ),
        )

    def test_timeline_round_trip_preserves_plot_events_and_cycles(self):
        event = PlotEvent(
            event_id="evt-000001", scene_id="live-turn-1", offset_ms=0,
            channels={"A": ChannelDirective(
                channel="A", mode=DirectiveMode.SET, pattern="呼吸",
                base_strength=20, resolved_strength=24,
            )},
        )
        cycle = self.make_cycle()
        timeline = Timeline(schema_version=1, session_id="session-1", seed=7,
                            plot_events=(event,), cycles=(cycle,))
        self.assertEqual(Timeline.from_dict(timeline.to_dict()), timeline)


if __name__ == "__main__":
    unittest.main()
