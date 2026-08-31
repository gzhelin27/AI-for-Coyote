import unittest

from tests.timeline_fakes import TimelineHarness


class MVP1IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_cycle_session_round_trips_without_resampling(self):
        harness = await TimelineHarness.create(seed=20260831, dry_run=True)
        self.addAsyncCleanup(harness.close)

        await harness.start()
        await harness.turn(
            [{"op": "hold_strength", "channel": "A", "value": 20}]
        )
        await harness.complete_cycles("A", count=3)
        await harness.turn(
            [{"op": "hold_strength", "channel": "B", "value": 12}]
        )
        saved = await harness.finish()
        replay = harness.store.load(saved.replay_id)

        result = await harness.replay(replay)

        self.assertEqual(result.requested_cycles, replay.timeline.cycles)
        self.assertEqual(result.rng_calls, 0)
        self.assertEqual(harness.relay.frames, [])


if __name__ == "__main__":
    unittest.main()
