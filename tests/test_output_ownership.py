import unittest

from backend.output_coordinator import DeviceOutputCoordinator, OutputIntentKind, OutputOwnership, TransportOutcome


class OutputOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_clear_preparation_preserves_token_but_external_policy_retires_it(self):
        coordinator = DeviceOutputCoordinator()
        token = OutputOwnership(coordinator)
        token.advance(coordinator.require_clear, 'A')
        async def clear(snapshot):
            self.assertTrue(token.is_current())
            return TransportOutcome(True, {'strength':0, 'waveform':None, 'waveform_mode':None})
        self.assertTrue((await coordinator.run('A',OutputIntentKind.CLEAR_OR_DISABLE,clear,ownership=token)).sent)
        self.assertTrue(token.is_current())
        coordinator.invalidate_queued_normal('B')
        token.advance(coordinator.require_clear, 'A')
        self.assertFalse(token.is_current(), 'own work after a policy change cannot adopt the new owner')

    async def test_global_clear_does_not_hide_intervening_external_generation(self):
        coordinator = DeviceOutputCoordinator()
        token = OutputOwnership(coordinator)
        async def clear(snapshots):
            self.assertTrue(token.is_current())
            coordinator.require_clear('B')
            return TransportOutcome(True, {c:{'strength':0,'waveform':None,'waveform_mode':None} for c in ('A','B')})
        self.assertTrue((await coordinator.run_global(OutputIntentKind.CLEAR_OR_DISABLE,clear,ownership=token)).sent)
        self.assertFalse(token.is_current())
        token.advance(coordinator.require_clear, 'A')
        self.assertFalse(token.is_current())

    async def test_tracking_token_cannot_bypass_emergency_priority(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.invalidate('A',OutputIntentKind.ESTOP)
        token = OutputOwnership(coordinator)
        async def forbidden(snapshot):
            self.fail('ownership observation cannot grant permission to execute normal output')
        result = await coordinator.run('A',OutputIntentKind.TIMELINE_OR_REPLAY,forbidden,ownership=token)
        self.assertFalse(result.sent)

    async def test_foreign_coordinator_token_is_rejected_without_mutation(self):
        coordinator = DeviceOutputCoordinator()
        token = OutputOwnership(DeviceOutputCoordinator())
        async def forbidden(snapshot):
            self.fail('a foreign coordinator token cannot identify local mutations')
        with self.assertRaises(ValueError):
            await coordinator.run('A',OutputIntentKind.CLEAR_OR_DISABLE,forbidden,ownership=token)
        self.assertFalse(coordinator.pending('A').clear_required)
