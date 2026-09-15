import unittest

from backend.video.ramp import RampPolicy


class RampPolicyTests(unittest.TestCase):
    def setUp(self):
        self.ramp = RampPolicy()

    def propose(self, requested=30, confirmed=0, now=0, cap=40, step=10):
        return self.ramp.propose(requested=requested, confirmed=confirmed,
                                 cap=cap, max_step=step, now_s=now)

    def apply(self, proposal, now=0, actual=None):
        self.ramp.confirm(proposal, actual=proposal.strength if actual is None else actual,
                          now_s=now)

    def test_permitted_jump_and_decrease_are_immediate(self):
        for requested, confirmed, expected in [(30, 20, 30), (20, 30, 20), (0, 30, 0)]:
            with self.subTest(requested=requested, confirmed=confirmed):
                self.assertEqual(self.propose(requested, confirmed).strength, expected)
        self.assertIsNone(self.propose(20, 20))

    def test_twenty_to_thirty_one_finishes_after_two_seconds(self):
        first = self.propose(31, 20)
        self.assertEqual(first.strength, 30)
        self.apply(first)
        self.assertIsNone(self.propose(31, 30, 1.99))
        last = self.propose(31, 30, 2)
        self.assertEqual(last.strength, 31)
        self.apply(last, 2)
        self.assertIsNone(self.propose(31, 31, 4))

    def test_zero_to_thirty_takes_forty_real_seconds(self):
        self.apply(self.propose())
        confirmed = 10
        for now, expected in zip(range(2, 41, 2), range(11, 31)):
            self.assertIsNone(self.propose(30, confirmed, now - .01))
            proposal = self.propose(30, confirmed, now)
            self.assertEqual(proposal.strength, expected)
            self.apply(proposal, now)
            confirmed = expected
        self.assertIsNone(self.propose(30, 30, 42))

    def test_late_tick_does_not_catch_up(self):
        self.apply(self.propose())
        late = self.propose(30, 10, 10)
        self.assertEqual(late.strength, 11)
        self.apply(late, 10)
        self.assertIsNone(self.propose(30, 11, 10))
        self.assertIsNone(self.propose(30, 11, 11.99))
        self.assertEqual(self.propose(30, 11, 12).strength, 12)

    def test_unconfirmed_proposals_do_not_start_or_advance_ramp(self):
        self.assertEqual(self.propose().strength, 10)
        first = self.propose(now=50)
        self.assertEqual(first.strength, 10)
        self.apply(first, 50)
        self.assertIsNone(self.propose(30, 10, 51.99))
        self.assertEqual(self.propose(30, 10, 52).strength, 11)
        self.assertEqual(self.propose(30, 10, 53).strength, 11)

    def test_changed_excessive_target_keeps_existing_cadence(self):
        self.apply(self.propose())
        self.assertIsNone(self.propose(40, 10, 1))
        next_step = self.propose(40, 10, 2)
        self.assertEqual(next_step.strength, 11)
        self.apply(next_step, 2)
        self.assertIsNone(self.propose(40, 11, 3))
        self.assertEqual(self.propose(40, 11, 4).strength, 12)

    def test_changed_target_within_step_is_immediate_and_ends_ramp(self):
        self.apply(self.propose())
        immediate = self.propose(20, 10, 1)
        self.assertEqual(immediate.strength, 20)
        self.apply(immediate, 1)
        self.assertEqual(self.propose(35, 20, 1).strength, 30)

    def test_lower_target_immediately_retires_ramp(self):
        self.apply(self.propose())
        lower = self.propose(5, 10, .1)
        self.assertEqual(lower.strength, 5)
        self.apply(lower, .1)
        self.assertEqual(self.propose(30, 5, .2).strength, 15)

    def test_cap_clipping_and_immediate_cap_decrease(self):
        self.assertEqual(self.propose(60, 35).strength, 40)
        self.apply(self.propose(60, 0))
        reduction = self.propose(60, 10, .1, cap=5)
        self.assertEqual(reduction.strength, 5)
        self.assertEqual(reduction.target, 5)
        self.apply(reduction, .1)
        self.assertIsNone(self.propose(60, 5, 10, cap=5))

    def test_clamped_ack_uses_actual_and_does_not_advance_clock_without_increase(self):
        self.apply(self.propose())
        step = self.propose(30, 10, 2)
        self.apply(step, 3, actual=10)
        self.assertEqual(self.propose(30, 10, 3).strength, 11)

    def test_ack_time_starts_cadence_and_partial_ack_stays_ramping(self):
        self.apply(self.propose(), 5, actual=7)
        self.assertIsNone(self.propose(30, 7, 6.99))
        self.assertEqual(self.propose(30, 7, 7).strength, 8)

    def test_cancel_invalidates_pending_ack_and_resume_starts_fresh(self):
        pending = self.propose()
        self.ramp.cancel()
        with self.assertRaises(ValueError):
            self.apply(pending)
        self.assertEqual(self.propose(30, 0, 1).strength, 10)

    def test_duplicate_confirmation_is_rejected(self):
        pending = self.propose()
        self.apply(pending)
        with self.assertRaises(ValueError):
            self.apply(pending, 10)
        self.assertEqual(self.propose(30, 10, 2).strength, 11)

    def test_invalid_inputs_are_rejected(self):
        for override in ({'requested': -1}, {'confirmed': -1}, {'cap': -1},
                         {'step': 0}, {'step': 1.5}, {'now': float('nan')},
                         {'now': float('inf')}, {'requested': True}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.propose(**override)


if __name__ == '__main__':
    unittest.main()

class RampAcknowledgementBoundaryTests(unittest.TestCase):
    def test_cap_changes_between_proposal_and_ack_use_actual_result(self):
        ramp = RampPolicy()
        first = ramp.propose(requested=60, confirmed=0, cap=40, max_step=10, now_s=0)
        ramp.confirm(first, actual=6, now_s=1)
        self.assertIsNone(ramp.propose(requested=60, confirmed=6, cap=6, max_step=10, now_s=2))
        next_step = ramp.propose(requested=60, confirmed=6, cap=15, max_step=10, now_s=3)
        self.assertEqual(next_step.strength, 7)
        self.assertEqual(next_step.target, 15)

    def test_final_ramp_step_clamped_ack_does_not_end_cadence(self):
        ramp = RampPolicy()
        first = ramp.propose(requested=31, confirmed=20, cap=40, max_step=10, now_s=0)
        ramp.confirm(first, actual=30, now_s=0)
        final = ramp.propose(requested=31, confirmed=30, cap=40, max_step=10, now_s=2)
        ramp.confirm(final, actual=25, now_s=2)
        retry = ramp.propose(requested=31, confirmed=25, cap=40, max_step=10, now_s=2)
        self.assertEqual(retry.strength, 26)

    def test_foreign_policy_cannot_confirm_proposal(self):
        a, b = RampPolicy(), RampPolicy()
        proposal = a.propose(requested=30, confirmed=0, cap=40, max_step=10, now_s=0)
        with self.assertRaises(ValueError):
            b.confirm(proposal, actual=10, now_s=0)


if __name__ == '__main__':
    unittest.main()

