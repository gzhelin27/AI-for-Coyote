import unittest

from backend.timeline.models import DirectiveMode
from backend.timeline.randomizer import TimelineResolver, derive_stream_seed


class TimelineResolverTests(unittest.TestCase):
    def setUp(self):
        self.event_args = {
            "actions": [{"op": "hold_strength", "channel": "A", "value": 20}],
            "current": {"A": 0, "B": 0}, "caps": {"A": 40, "B": 40},
            "enabled": {"A": True, "B": True}, "presets": ("呼吸", "潮汐"),
            "event_id": "evt-000001", "scene_id": "live-turn-1", "offset_ms": 0,
        }

    def resolver(self, seed: int):
        return TimelineResolver(strength_jitter=4, session_seed=seed)

    def test_stream_seed_is_stable_and_distinct(self):
        self.assertEqual(derive_stream_seed(99, "plot:A"), derive_stream_seed(99, "plot:A"))
        seeds = {derive_stream_seed(99, name) for name in ("plot:A", "plot:B", "cycle:A", "cycle:B")}
        self.assertEqual(len(seeds), 4)

    def test_stream_seed_rejects_unknown_stream(self):
        with self.assertRaisesRegex(ValueError, "unsupported random stream"):
            derive_stream_seed(99, "plot:C")

    def test_same_seed_resolves_same_waveform_and_strength(self):
        first = self.resolver(7).resolve_plot_event(**self.event_args)
        second = self.resolver(7).resolve_plot_event(**self.event_args)
        self.assertEqual(first, second)

    def test_strength_stays_within_jitter_and_cap(self):
        event = self.resolver(8).resolve_plot_event(
            actions=[{"op": "hold_strength", "channel": "A", "value": 39}],
            current={"A": 0, "B": 0}, caps={"A": 40, "B": 40},
            enabled={"A": True, "B": True}, presets=("呼吸", "潮汐"),
            event_id="evt-000002", scene_id="live-turn-2", offset_ms=12000,
        )
        self.assertGreaterEqual(event.channels["A"].resolved_strength, 35)
        self.assertLessEqual(event.channels["A"].resolved_strength, 40)

    def test_stop_is_never_randomized_into_output(self):
        event = self.resolver(9).resolve_plot_event(
            actions=[{"op": "clear", "channel": "B"}],
            current={"A": 10, "B": 10}, caps={"A": 40, "B": 40},
            enabled={"A": True, "B": True}, presets=("呼吸",),
            event_id="evt-000003", scene_id="live-turn-3", offset_ms=24000,
        )
        self.assertEqual(event.channels["B"].mode, DirectiveMode.STOP)
        self.assertIsNone(event.channels["B"].pattern)
        self.assertIsNone(event.channels["B"].resolved_strength)

    def test_add_strength_uses_current_strength_as_the_base_target(self):
        event = self.resolver(11).resolve_plot_event(
            actions=[{"op": "add_strength", "channel": "A", "delta": 8}],
            current={"A": 12, "B": 0}, caps={"A": 40, "B": 40},
            enabled={"A": True, "B": True}, presets=("呼吸",),
            event_id="evt-000004", scene_id="live-turn-4", offset_ms=36000,
        )
        directive = event.channels["A"]
        self.assertEqual(directive.base_strength, 20)
        self.assertGreaterEqual(directive.resolved_strength, 16)
        self.assertLessEqual(directive.resolved_strength, 24)

    def test_disabled_channel_is_ignored_without_advancing_the_other_stream(self):
        resolver = self.resolver(12)
        event = resolver.resolve_plot_event(
            actions=[
                {"op": "hold_strength", "channel": "A", "value": 20},
                {"op": "hold_strength", "channel": "B", "value": 20},
            ],
            current={"A": 0, "B": 0}, caps={"A": 40, "B": 40},
            enabled={"A": True, "B": False}, presets=("呼吸", "潮汐"),
            event_id="evt-000005", scene_id="live-turn-5", offset_ms=48000,
        )
        expected = self.resolver(12).resolve_plot_event(
            actions=[{"op": "hold_strength", "channel": "A", "value": 20}],
            current={"A": 0, "B": 0}, caps={"A": 40, "B": 40},
            enabled={"A": True, "B": True}, presets=("呼吸", "潮汐"),
            event_id="evt-000005", scene_id="live-turn-5", offset_ms=48000,
        )
        self.assertEqual(event.channels, {"A": expected.channels["A"]})


if __name__ == "__main__":
    unittest.main()
