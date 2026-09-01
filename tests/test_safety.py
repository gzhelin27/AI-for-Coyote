from copy import deepcopy
import unittest

from backend.config import DEFAULTS
from backend.safety import SafetyManager


def make_safety(*, cap: int = 30, step: int = 10) -> SafetyManager:
    cfg = deepcopy(DEFAULTS)
    cfg["safety"]["channels"]["A"]["max_strength"] = cap
    cfg["safety"]["channels"]["B"]["max_strength"] = cap
    cfg["safety"]["max_strength_step"] = step
    return SafetyManager(cfg)


class SafetyTargetTests(unittest.TestCase):
    def test_hold_strength_cannot_jump_above_step(self):
        safety = make_safety()
        ok, _, cmd = safety.validate({"op": "hold_strength", "channel": "A", "value": 30})
        self.assertTrue(ok)
        self.assertEqual(cmd["value"], 10)

    def test_temp_strength_cannot_jump_above_step(self):
        safety = make_safety()
        ok, _, cmd = safety.validate(
            {"op": "temp_strength", "channel": "B", "value": 30, "duration_s": 3}
        )
        self.assertTrue(ok)
        self.assertEqual(cmd["value"], 10)

    def test_repeated_targets_ramp_by_step_and_respect_cap(self):
        safety = make_safety()
        values = []
        for _ in range(4):
            ok, _, cmd = safety.validate({"op": "hold_strength", "channel": "A", "value": 99})
            self.assertTrue(ok)
            safety.record(cmd)
            values.append(cmd["value"])
        self.assertEqual(values, [10, 20, 30, 30])

    def test_reduction_is_immediate_for_safety(self):
        safety = make_safety()
        safety.current["A"] = 30
        ok, _, cmd = safety.validate({"op": "hold_strength", "channel": "A", "value": 0})
        self.assertTrue(ok)
        self.assertEqual(cmd["value"], 0)

    def test_runtime_cap_is_always_enforced(self):
        safety = make_safety()
        safety.set_user_cap("A", 5)
        ok, _, cmd = safety.validate({"op": "temp_strength", "channel": "A", "value": 30})
        self.assertTrue(ok)
        self.assertEqual(cmd["value"], 5)

    def test_app_user_overheat_and_absolute_caps_use_strictest_limit(self):
        safety = make_safety(cap=100)
        safety.set_user_cap("A", 60)
        safety.update_device_policy(
            {
                "channelA": {
                    "comfortLimit": {"comfortMax": 50, "absoluteMax": 40}
                }
            }
        )

        self.assertEqual(safety.cap_for("A"), 40)

        safety.overheat["A"] = True
        self.assertEqual(safety.cap_for("A"), 20)

        safety.set_user_cap("A", 5)
        self.assertEqual(safety.cap_for("A"), 5)

    def test_missing_absolute_policy_does_not_relax_last_absolute_cap(self):
        safety = make_safety(cap=100)
        safety.update_device_policy(
            {
                "channelA": {
                    "comfortLimit": {"comfortMax": 25, "absoluteMax": 10}
                }
            }
        )

        safety.update_device_policy(
            {"channelA": {"comfortLimit": {"comfortMax": 50}}}
        )

        self.assertEqual(safety.app_caps["A"], 10)
        self.assertEqual(safety.cap_for("A"), 10)

    def test_nonfinite_app_caps_are_ignored_without_losing_overheat(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                safety = make_safety(cap=100)

                safety.update_device_policy(
                    {
                        "channelA": {
                            "comfortLimit": {
                                "overheat": True,
                                "comfortMax": value,
                            }
                        }
                    }
                )

                self.assertTrue(safety.overheat["A"])
                self.assertIsNone(safety.app_caps["A"])
                self.assertEqual(safety.cap_for("A"), 20)

    def test_invalid_app_caps_are_ignored_and_large_finite_caps_are_clamped(self):
        for value in (True, "50", -1):
            with self.subTest(value=value):
                safety = make_safety(cap=100)
                safety.update_device_policy(
                    {
                        "channelA": {
                            "comfortLimit": {"comfortMax": value}
                        }
                    }
                )
                self.assertIsNone(safety.app_caps["A"])

        safety = make_safety(cap=100)
        safety.update_device_policy(
            {
                "channelA": {
                    "comfortLimit": {"comfortMax": 10_000}
                }
            }
        )
        self.assertEqual(safety.app_caps["A"], 100)
        self.assertEqual(safety.cap_for("A"), 100)


if __name__ == "__main__":
    unittest.main()
