import unittest

from backend.timeline.models import (
    ChannelDirective, CycleGapPolicy, CycleRecord, DirectiveMode, PlotEvent, Timeline,
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
    def test_gap_policy_uses_exact_three_bands(self):
        policy = CycleGapPolicy()
        rng = FixedRng([0, 39, 40, 69, 70, 99], [1, 10, 11, 20])
        self.assertEqual([policy.sample_tenths(rng) for _ in range(6)], [0, 0, 1, 10, 11, 20])

    def test_gap_duration_uses_raw_frame_count(self):
        policy = CycleGapPolicy()
        self.assertEqual(policy.cycle_ms(frame_count=12), 1200)
        self.assertEqual(policy.gap_ms(frame_count=12, tenths=7), 840)

    def test_timeline_round_trip_preserves_plot_events_and_cycles(self):
        event = PlotEvent(
            event_id="evt-000001", scene_id="live-turn-1", offset_ms=0,
            channels={"A": ChannelDirective(
                channel="A", mode=DirectiveMode.SET, pattern="呼吸",
                base_strength=20, resolved_strength=24,
            )},
        )
        cycle = CycleRecord(
            channel="A", cycle_index=1, plot_event_id="evt-000001",
            pattern="呼吸", waveform_hash="wave-hash", requested_strength=24,
            effective_strength=24, active_start_offset_ms=0, raw_duration_ms=1200,
            gap_tenths=7, planned_gap_ms=840, actual_gap_ms=840,
            completed=True, interruption_reason=None,
        )
        timeline = Timeline(schema_version=1, session_id="session-1", seed=7,
                            plot_events=(event,), cycles=(cycle,))
        self.assertEqual(Timeline.from_dict(timeline.to_dict()), timeline)


if __name__ == "__main__":
    unittest.main()
