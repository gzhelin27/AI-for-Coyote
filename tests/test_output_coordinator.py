import asyncio
from dataclasses import FrozenInstanceError
import unittest

from backend.output_coordinator import (
    DeviceOutputCoordinator,
    OutputIntentKind,
    TransportOutcome,
)


async def async_outcome(
    *,
    sent: bool,
    effective: dict[str, object] | None = None,
    simulated: bool = False,
) -> TransportOutcome:
    return TransportOutcome(sent=sent, effective=effective, simulated=simulated)


class OutputCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_transport_does_not_commit_confirmed_state(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed("A", strength=30, enabled=True)

        result = await coordinator.run(
            "A",
            OutputIntentKind.SAFETY_REDUCE,
            lambda state: async_outcome(
                sent=False,
                effective={"strength": 10},
            ),
        )

        self.assertFalse(result.sent)
        self.assertIsNone(result.effective)
        self.assertEqual(coordinator.confirmed("A").strength, 30)

    async def test_successful_transport_commits_only_returned_effective_fields(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed(
            "A",
            strength=30,
            waveform="pulse",
            waveform_mode="loop",
            enabled=True,
        )

        await coordinator.run(
            "A",
            OutputIntentKind.MANUAL,
            lambda state: async_outcome(
                sent=True,
                effective={"strength": 18},
            ),
        )

        confirmed = coordinator.confirmed("A")
        self.assertEqual(confirmed.strength, 18)
        self.assertEqual(confirmed.waveform, "pulse")
        self.assertEqual(confirmed.waveform_mode, "loop")
        self.assertTrue(confirmed.enabled)

    async def test_higher_priority_invalidation_makes_normal_generation_stale(self):
        coordinator = DeviceOutputCoordinator()
        started = coordinator.generation("A")

        coordinator.invalidate("A", OutputIntentKind.CLEAR_OR_DISABLE)

        self.assertFalse(coordinator.is_current("A", started))

    async def test_reported_strength_waits_for_active_transport_then_wins(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed("A", strength=30, enabled=True)
        transport_started = asyncio.Event()
        release_transport = asyncio.Event()

        async def reduce(state):
            transport_started.set()
            await release_transport.wait()
            return TransportOutcome(sent=True, effective={"strength": 20})

        active = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, reduce)
        )
        await asyncio.wait_for(transport_started.wait(), timeout=0.2)
        report = asyncio.create_task(
            coordinator.confirm_reported_strength("A", 40)
        )
        release_transport.set()

        await asyncio.wait_for(asyncio.gather(active, report), timeout=0.2)

        self.assertEqual(coordinator.confirmed("A").strength, 40)

    async def test_conditional_local_snapshot_cannot_overwrite_newer_transport(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed("A", strength=30, enabled=True)
        observed_revision = coordinator.revision("A")
        transport_started = asyncio.Event()
        release_transport = asyncio.Event()

        async def reduce(state):
            transport_started.set()
            await release_transport.wait()
            return TransportOutcome(sent=True, effective={"strength": 20})

        active = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, reduce)
        )
        await asyncio.wait_for(transport_started.wait(), timeout=0.2)
        local_snapshot = asyncio.create_task(
            coordinator.confirm_reported_strength(
                "A", 30, expected_revision=observed_revision
            )
        )
        release_transport.set()

        await asyncio.wait_for(
            asyncio.gather(active, local_snapshot), timeout=0.2
        )

        self.assertEqual(coordinator.confirmed("A").strength, 20)

    async def test_queued_normal_work_is_rejected_after_safety_invalidation(self):
        coordinator = DeviceOutputCoordinator()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        queued_transport_called = False

        async def first_transport(state):
            first_started.set()
            await release_first.wait()
            return TransportOutcome(sent=True, effective={"strength": 25})

        async def queued_transport(state):
            nonlocal queued_transport_called
            queued_transport_called = True
            return TransportOutcome(sent=True, effective={"strength": 35})

        first = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, first_transport)
        )
        await asyncio.wait_for(first_started.wait(), timeout=0.2)
        queued = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.TIMELINE_OR_REPLAY, queued_transport)
        )
        await asyncio.sleep(0)
        coordinator.mark_reduction("A", 20)
        release_first.set()

        first_result, queued_result = await asyncio.wait_for(
            asyncio.gather(first, queued), timeout=0.2
        )
        self.assertTrue(first_result.sent)
        self.assertFalse(queued_result.sent)
        self.assertFalse(queued_transport_called)
        self.assertEqual(coordinator.pending("A").target_strength, 20)

    async def test_failed_pending_reduction_survives_identical_retry_trigger(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.mark_reduction("A", 20)
        await coordinator.run(
            "A",
            OutputIntentKind.SAFETY_REDUCE,
            lambda state: async_outcome(sent=False),
        )

        coordinator.mark_reduction("A", 20)

        self.assertEqual(coordinator.pending("A").target_strength, 20)

    async def test_newer_reduction_target_survives_older_successful_transport(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed("A", strength=30, enabled=True)
        coordinator.mark_reduction("A", 20)
        transport_started = asyncio.Event()
        release_transport = asyncio.Event()

        async def transport(state):
            transport_started.set()
            await release_transport.wait()
            return TransportOutcome(sent=True, effective={"strength": 20})

        running = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.SAFETY_REDUCE, transport)
        )
        await asyncio.wait_for(transport_started.wait(), timeout=0.2)
        coordinator.mark_reduction("A", 10)
        release_transport.set()

        result = await asyncio.wait_for(running, timeout=0.2)
        self.assertTrue(result.sent)
        self.assertEqual(coordinator.confirmed("A").strength, 20)
        self.assertEqual(coordinator.pending("A").target_strength, 10)

    async def test_transport_exception_returns_failure_and_retains_pending_work(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed("A", strength=30, enabled=True)
        coordinator.mark_reduction("A", 20)

        async def fail(state):
            raise RuntimeError("relay unavailable")

        result = await coordinator.run("A", OutputIntentKind.SAFETY_REDUCE, fail)

        self.assertFalse(result.sent)
        self.assertIsNone(result.effective)
        self.assertEqual(result.error, "relay unavailable")
        self.assertEqual(coordinator.confirmed("A").strength, 30)
        self.assertEqual(coordinator.pending("A").target_strength, 20)

    async def test_callback_cancellation_is_reraised_with_pending_work_retained(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.mark_reduction("A", 20)

        async def cancel(state):
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await coordinator.run("A", OutputIntentKind.SAFETY_REDUCE, cancel)

        self.assertEqual(coordinator.pending("A").target_strength, 20)

    async def test_caller_cancellation_waits_for_safety_transport_to_be_represented(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.mark_reduction("A", 20)
        transport_started = asyncio.Event()
        release_transport = asyncio.Event()

        async def transport(state):
            transport_started.set()
            await release_transport.wait()
            return TransportOutcome(sent=False)

        running = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.SAFETY_REDUCE, transport)
        )
        await asyncio.wait_for(transport_started.wait(), timeout=0.2)
        running.cancel()
        await asyncio.sleep(0)
        self.assertFalse(running.done())

        release_transport.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=0.2)
        self.assertEqual(coordinator.pending("A").target_strength, 20)

    async def test_dry_run_sent_outcome_uses_normal_commit_semantics(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed("A", strength=30, enabled=True)
        coordinator.mark_reduction("A", 12)

        result = await coordinator.run(
            "A",
            OutputIntentKind.SAFETY_REDUCE,
            lambda state: async_outcome(
                sent=True,
                simulated=True,
                effective={"strength": 12},
            ),
        )

        self.assertTrue(result.sent)
        self.assertTrue(result.simulated)
        self.assertEqual(coordinator.confirmed("A").strength, 12)
        self.assertIsNone(coordinator.pending("A").target_strength)

    async def test_estop_latch_rejects_every_lower_priority_intent(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.invalidate("A", OutputIntentKind.ESTOP)
        calls: list[OutputIntentKind] = []

        async def attempt(kind):
            calls.append(kind)
            return TransportOutcome(
                sent=True,
                effective={
                    "strength": 0,
                    "waveform": None,
                    "waveform_mode": None,
                },
            )

        clear = await coordinator.run(
            "A",
            OutputIntentKind.CLEAR_OR_DISABLE,
            lambda state: attempt(OutputIntentKind.CLEAR_OR_DISABLE),
        )
        estop = await coordinator.run(
            "A",
            OutputIntentKind.ESTOP,
            lambda state: attempt(OutputIntentKind.ESTOP),
        )
        manual = await coordinator.run(
            "A",
            OutputIntentKind.MANUAL,
            lambda state: attempt(OutputIntentKind.MANUAL),
        )

        self.assertFalse(clear.sent)
        self.assertTrue(estop.sent)
        self.assertFalse(manual.sent)
        self.assertEqual(calls, [OutputIntentKind.ESTOP])

    async def test_required_clear_blocks_normal_work_until_confirmed(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed(
            "A", strength=25, waveform="pulse", waveform_mode="loop", enabled=True
        )
        coordinator.require_clear("A")
        normal_called = False

        async def normal(state):
            nonlocal normal_called
            normal_called = True
            return TransportOutcome(sent=True, effective={"strength": 30})

        blocked = await coordinator.run("A", OutputIntentKind.MANUAL, normal)
        cleared = await coordinator.run(
            "A",
            OutputIntentKind.CLEAR_OR_DISABLE,
            lambda state: async_outcome(
                sent=True,
                effective={
                    "strength": 0,
                    "waveform": None,
                    "waveform_mode": None,
                },
            ),
        )

        self.assertFalse(blocked.sent)
        self.assertFalse(normal_called)
        self.assertTrue(cleared.sent)
        self.assertFalse(coordinator.pending("A").clear_required)
        self.assertEqual(coordinator.confirmed("A").strength, 0)
        self.assertIsNone(coordinator.confirmed("A").waveform)

    async def test_channels_progress_independently(self):
        coordinator = DeviceOutputCoordinator()
        channel_a_started = asyncio.Event()
        release_channel_a = asyncio.Event()

        async def block_a(state):
            channel_a_started.set()
            await release_channel_a.wait()
            return TransportOutcome(sent=True, effective={"strength": 10})

        channel_a = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, block_a)
        )
        await asyncio.wait_for(channel_a_started.wait(), timeout=0.2)

        channel_b = await asyncio.wait_for(
            coordinator.run(
                "B",
                OutputIntentKind.MANUAL,
                lambda state: async_outcome(sent=True, effective={"strength": 15}),
            ),
            timeout=0.2,
        )
        self.assertTrue(channel_b.sent)
        self.assertFalse(channel_a.done())

        release_channel_a.set()
        await asyncio.wait_for(channel_a, timeout=0.2)

    async def test_same_channel_transports_are_serialized(self):
        coordinator = DeviceOutputCoordinator()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def first(state):
            order.append("first-start")
            first_started.set()
            await release_first.wait()
            order.append("first-end")
            return TransportOutcome(sent=True, effective={"strength": 10})

        async def second(state):
            order.append("second")
            return TransportOutcome(sent=True, effective={"strength": 20})

        first_task = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, first)
        )
        await asyncio.wait_for(first_started.wait(), timeout=0.2)
        second_task = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, second)
        )
        await asyncio.sleep(0)
        self.assertEqual(order, ["first-start"])

        release_first.set()
        await asyncio.wait_for(
            asyncio.gather(first_task, second_task), timeout=0.2
        )
        self.assertEqual(order, ["first-start", "first-end", "second"])

    async def test_global_operation_acquires_a_then_b_without_deadlock(self):
        coordinator = DeviceOutputCoordinator()
        b_started = asyncio.Event()
        release_b = asyncio.Event()
        global_started = asyncio.Event()
        a_started = asyncio.Event()

        async def block_b(state):
            b_started.set()
            await release_b.wait()
            return TransportOutcome(sent=True, effective={"strength": 10})

        b_task = asyncio.create_task(
            coordinator.run("B", OutputIntentKind.MANUAL, block_b)
        )
        await asyncio.wait_for(b_started.wait(), timeout=0.2)

        async def global_clear(states):
            global_started.set()
            return TransportOutcome(
                sent=True,
                effective={
                    "A": {
                        "strength": 0,
                        "waveform": None,
                        "waveform_mode": None,
                    },
                    "B": {
                        "strength": 0,
                        "waveform": None,
                        "waveform_mode": None,
                    },
                },
            )

        global_task = asyncio.create_task(
            coordinator.run_global(OutputIntentKind.CLEAR_OR_DISABLE, global_clear)
        )
        await asyncio.sleep(0)

        async def use_a(state):
            a_started.set()
            return TransportOutcome(sent=True, effective={"strength": 5})

        a_task = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, use_a)
        )
        await asyncio.sleep(0)
        self.assertFalse(a_started.is_set())
        self.assertFalse(global_started.is_set())

        release_b.set()
        global_result = await asyncio.wait_for(global_task, timeout=0.2)
        await asyncio.wait_for(asyncio.gather(a_task, b_task), timeout=0.2)

        self.assertTrue(global_result.sent)
        self.assertTrue(global_started.is_set())
        self.assertTrue(a_started.is_set())

    async def test_queued_estop_is_not_staled_by_later_clear(self):
        coordinator = DeviceOutputCoordinator()
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        calls: list[str] = []

        async def blocker(state):
            blocker_started.set()
            await release_blocker.wait()
            return TransportOutcome(sent=True, effective={"strength": 25})

        active = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, blocker)
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=0.2)
        estop = asyncio.create_task(
            coordinator.run(
                "A",
                OutputIntentKind.ESTOP,
                lambda state: self._record_clear(calls, "estop"),
            )
        )
        await asyncio.sleep(0)
        clear = asyncio.create_task(
            coordinator.run(
                "A",
                OutputIntentKind.CLEAR_OR_DISABLE,
                lambda state: self._record_clear(calls, "clear"),
            )
        )
        await asyncio.sleep(0)
        release_blocker.set()

        _, estop_result, clear_result = await asyncio.wait_for(
            asyncio.gather(active, estop, clear), timeout=0.2
        )
        self.assertTrue(estop_result.sent)
        self.assertFalse(clear_result.sent)
        self.assertEqual(calls, ["estop"])
        self.assertFalse(coordinator.pending("A").clear_required)

    async def test_later_estop_stales_already_queued_clear(self):
        coordinator = DeviceOutputCoordinator()
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        calls: list[str] = []

        async def blocker(state):
            blocker_started.set()
            await release_blocker.wait()
            return TransportOutcome(sent=True, effective={"strength": 25})

        active = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, blocker)
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=0.2)
        clear = asyncio.create_task(
            coordinator.run(
                "A",
                OutputIntentKind.CLEAR_OR_DISABLE,
                lambda state: self._record_clear(calls, "clear"),
            )
        )
        await asyncio.sleep(0)
        estop = asyncio.create_task(
            coordinator.run(
                "A",
                OutputIntentKind.ESTOP,
                lambda state: self._record_clear(calls, "estop"),
            )
        )
        await asyncio.sleep(0)
        release_blocker.set()

        _, clear_result, estop_result = await asyncio.wait_for(
            asyncio.gather(active, clear, estop), timeout=0.2
        )
        self.assertFalse(clear_result.sent)
        self.assertTrue(estop_result.sent)
        self.assertEqual(calls, ["estop"])
        self.assertFalse(coordinator.pending("A").clear_required)

    async def test_queued_estop_is_not_staled_by_later_reduction(self):
        coordinator = DeviceOutputCoordinator()
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        calls: list[str] = []

        async def blocker(state):
            blocker_started.set()
            await release_blocker.wait()
            return TransportOutcome(sent=True, effective={"strength": 25})

        active = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, blocker)
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=0.2)
        estop = asyncio.create_task(
            coordinator.run(
                "A",
                OutputIntentKind.ESTOP,
                lambda state: self._record_clear(calls, "estop"),
            )
        )
        await asyncio.sleep(0)
        coordinator.mark_reduction("A", 10)
        reduction = asyncio.create_task(
            coordinator.run(
                "A",
                OutputIntentKind.SAFETY_REDUCE,
                lambda state: self._record_strength(calls, "reduce", 10),
            )
        )
        await asyncio.sleep(0)
        release_blocker.set()

        _, estop_result, reduction_result = await asyncio.wait_for(
            asyncio.gather(active, estop, reduction), timeout=0.2
        )
        self.assertTrue(estop_result.sent)
        self.assertFalse(reduction_result.sent)
        self.assertEqual(calls, ["estop"])
        self.assertIsNone(coordinator.pending("A").target_strength)

    async def test_later_estop_stales_already_queued_reduction(self):
        coordinator = DeviceOutputCoordinator()
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        calls: list[str] = []

        async def blocker(state):
            blocker_started.set()
            await release_blocker.wait()
            return TransportOutcome(sent=True, effective={"strength": 25})

        active = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.MANUAL, blocker)
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=0.2)
        coordinator.mark_reduction("A", 10)
        reduction = asyncio.create_task(
            coordinator.run(
                "A",
                OutputIntentKind.SAFETY_REDUCE,
                lambda state: self._record_strength(calls, "reduce", 10),
            )
        )
        await asyncio.sleep(0)
        estop = asyncio.create_task(
            coordinator.run(
                "A",
                OutputIntentKind.ESTOP,
                lambda state: self._record_clear(calls, "estop"),
            )
        )
        await asyncio.sleep(0)
        release_blocker.set()

        _, reduction_result, estop_result = await asyncio.wait_for(
            asyncio.gather(active, reduction, estop), timeout=0.2
        )
        self.assertFalse(reduction_result.sent)
        self.assertTrue(estop_result.sent)
        self.assertEqual(calls, ["estop"])
        self.assertIsNone(coordinator.pending("A").target_strength)

    async def test_sent_clear_without_complete_clear_state_fails_closed(self):
        invalid_effective = (
            None,
            {"strength": 0},
            {"strength": 1, "waveform": None, "waveform_mode": None},
            {"strength": 0, "waveform": "pulse", "waveform_mode": None},
            {"strength": 0, "waveform": None, "waveform_mode": "loop"},
            {
                "strength": 0,
                "waveform": None,
                "waveform_mode": None,
                "enabled": "no",
            },
        )
        for effective in invalid_effective:
            with self.subTest(effective=effective):
                coordinator = DeviceOutputCoordinator()
                coordinator.seed_confirmed(
                    "A",
                    strength=25,
                    waveform="pulse",
                    waveform_mode="loop",
                    enabled=True,
                )
                coordinator.require_clear("A")

                result = await coordinator.run(
                    "A",
                    OutputIntentKind.CLEAR_OR_DISABLE,
                    lambda state, effective=effective: async_outcome(
                        sent=True, effective=effective
                    ),
                )

                self.assertFalse(result.sent)
                self.assertTrue(coordinator.pending("A").clear_required)
                self.assertEqual(coordinator.confirmed("A").strength, 25)
                blocked = await coordinator.run(
                    "A",
                    OutputIntentKind.MANUAL,
                    lambda state: async_outcome(
                        sent=True, effective={"strength": 30}
                    ),
                )
                self.assertFalse(blocked.sent)

    async def test_global_clear_rejects_missing_a_without_partial_commit(self):
        await self._assert_invalid_global_clear(
            {"B": self._clear_effective()},
        )

    async def test_global_clear_rejects_missing_b_without_partial_commit(self):
        await self._assert_invalid_global_clear(
            {"A": self._clear_effective()},
        )

    async def test_global_clear_rejects_malformed_channel_state(self):
        await self._assert_invalid_global_clear(
            {"A": "not-a-state", "B": self._clear_effective()},
        )

    async def test_global_clear_rejects_invalid_field_without_partial_commit(self):
        await self._assert_invalid_global_clear(
            {
                "A": self._clear_effective(),
                "B": {
                    "strength": 0,
                    "waveform": None,
                    "waveform_mode": None,
                    "enabled": "no",
                },
            },
        )

    async def test_global_clear_rejects_non_clear_channel_state(self):
        await self._assert_invalid_global_clear(
            {
                "A": self._clear_effective(),
                "B": {"strength": 1, "waveform": None, "waveform_mode": None},
            },
        )

    async def test_failed_strict_reduction_is_not_weakened_by_relaxed_target(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.mark_reduction("A", 10)
        failed = await coordinator.run(
            "A",
            OutputIntentKind.SAFETY_REDUCE,
            lambda state: async_outcome(sent=False, effective={"strength": 10}),
        )
        self.assertFalse(failed.sent)

        coordinator.mark_reduction("A", 20)

        self.assertEqual(coordinator.pending("A").target_strength, 10)

    async def test_confirmed_channel_clear_satisfies_pending_reduction(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed(
            "A",
            strength=30,
            waveform="pulse",
            waveform_mode="loop",
            enabled=True,
        )
        coordinator.mark_reduction("A", 10)
        coordinator.require_clear("A")

        result = await coordinator.run(
            "A",
            OutputIntentKind.CLEAR_OR_DISABLE,
            lambda state: async_outcome(sent=True, effective=self._clear_effective()),
        )

        self.assertTrue(result.sent)
        self.assertIsNone(coordinator.pending("A").target_strength)
        manual = await coordinator.run(
            "A",
            OutputIntentKind.MANUAL,
            lambda state: async_outcome(sent=True, effective={"strength": 5}),
        )
        self.assertTrue(manual.sent)

    async def test_confirmed_global_clear_satisfies_both_pending_reductions(self):
        coordinator = DeviceOutputCoordinator()
        for channel in ("A", "B"):
            coordinator.seed_confirmed(
                channel,
                strength=30,
                waveform="pulse",
                waveform_mode="loop",
                enabled=True,
            )
            coordinator.mark_reduction(channel, 10)

        result = await coordinator.run_global(
            OutputIntentKind.CLEAR_OR_DISABLE,
            lambda states: async_outcome(
                sent=True,
                effective={
                    "A": self._clear_effective(),
                    "B": self._clear_effective(),
                },
            ),
        )

        self.assertTrue(result.sent)
        self.assertIsNone(coordinator.pending("A").target_strength)
        self.assertIsNone(coordinator.pending("B").target_strength)

    async def test_channel_clear_blocks_reduction_queued_while_clear_is_in_flight(self):
        coordinator = DeviceOutputCoordinator()
        clear_started = asyncio.Event()
        release_clear = asyncio.Event()
        reduction_called = False

        async def clear(state):
            clear_started.set()
            await release_clear.wait()
            return TransportOutcome(sent=True, effective=self._clear_effective())

        clear_task = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.CLEAR_OR_DISABLE, clear)
        )
        await asyncio.wait_for(clear_started.wait(), timeout=0.2)
        coordinator.mark_reduction("A", 10)

        async def reduce(state):
            nonlocal reduction_called
            reduction_called = True
            return TransportOutcome(sent=True, effective={"strength": 10})

        reduction_task = asyncio.create_task(
            coordinator.run("A", OutputIntentKind.SAFETY_REDUCE, reduce)
        )
        await asyncio.sleep(0)
        release_clear.set()

        clear_result, reduction_result = await asyncio.wait_for(
            asyncio.gather(clear_task, reduction_task), timeout=0.2
        )
        self.assertTrue(clear_result.sent)
        self.assertFalse(reduction_result.sent)
        self.assertFalse(reduction_called)
        self.assertIsNone(coordinator.pending("A").target_strength)

    async def test_global_clear_blocks_reductions_queued_while_clear_is_in_flight(self):
        coordinator = DeviceOutputCoordinator()
        clear_started = asyncio.Event()
        release_clear = asyncio.Event()
        reduction_calls: list[str] = []

        async def clear(states):
            clear_started.set()
            await release_clear.wait()
            return TransportOutcome(
                sent=True,
                effective={
                    "A": self._clear_effective(),
                    "B": self._clear_effective(),
                },
            )

        clear_task = asyncio.create_task(
            coordinator.run_global(OutputIntentKind.CLEAR_OR_DISABLE, clear)
        )
        await asyncio.wait_for(clear_started.wait(), timeout=0.2)
        reduction_tasks = []
        for channel in ("A", "B"):
            coordinator.mark_reduction(channel, 10)
            reduction_tasks.append(
                asyncio.create_task(
                    coordinator.run(
                        channel,
                        OutputIntentKind.SAFETY_REDUCE,
                        lambda state, channel=channel: self._record_strength(
                            reduction_calls, channel, 10
                        ),
                    )
                )
            )
        await asyncio.sleep(0)
        release_clear.set()

        results = await asyncio.wait_for(
            asyncio.gather(clear_task, *reduction_tasks), timeout=0.2
        )
        self.assertTrue(results[0].sent)
        self.assertFalse(results[1].sent)
        self.assertFalse(results[2].sent)
        self.assertEqual(reduction_calls, [])
        self.assertIsNone(coordinator.pending("A").target_strength)
        self.assertIsNone(coordinator.pending("B").target_strength)

    async def _assert_invalid_global_clear(self, effective):
        coordinator = DeviceOutputCoordinator()
        for channel in ("A", "B"):
            coordinator.seed_confirmed(
                channel,
                strength=25,
                waveform="pulse",
                waveform_mode="loop",
                enabled=True,
            )

        result = await coordinator.run_global(
            OutputIntentKind.CLEAR_OR_DISABLE,
            lambda states: async_outcome(sent=True, effective=effective),
        )

        self.assertFalse(result.sent)
        for channel in ("A", "B"):
            self.assertEqual(coordinator.confirmed(channel).strength, 25)
            self.assertTrue(coordinator.pending(channel).clear_required)

    async def _record_clear(self, calls, name):
        calls.append(name)
        return TransportOutcome(sent=True, effective=self._clear_effective())

    async def _record_strength(self, calls, name, strength):
        calls.append(name)
        return TransportOutcome(sent=True, effective={"strength": strength})

    @staticmethod
    def _clear_effective():
        return {"strength": 0, "waveform": None, "waveform_mode": None}

    async def test_public_snapshots_and_transport_effective_state_are_immutable(self):
        coordinator = DeviceOutputCoordinator()
        coordinator.seed_confirmed("A", strength=10, enabled=True)
        outcome = TransportOutcome(sent=True, effective={"strength": 12})

        with self.assertRaises(FrozenInstanceError):
            coordinator.confirmed("A").strength = 99
        with self.assertRaises(FrozenInstanceError):
            coordinator.pending("A").clear_required = True
        with self.assertRaises(TypeError):
            outcome.effective["strength"] = 99


if __name__ == "__main__":
    unittest.main()
