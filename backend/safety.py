# -*- coding: utf-8 -*-
"""安全层：所有设备命令的唯一出口。

规则（任何来源——AI 或手动——都必须经过这里）：
1. 每通道独立强度上限（配置，默认 100），超出的值一律钳制；
2. 单条指令强度变化量不超过 max_strength_step（防跳变）；
3. 单次波形/临时强度时长不超过配置上限（默认 10s），到点自动归零；
4. 设备过热时，该通道上限临时降到 overheat_reduce_to；
5. 急停（estop）：清零全部通道 + 清波形 + 暂停 AI 循环；
6. op 白名单：强度、波形（含单周期）、清除和停止操作。
"""
import logging
import math
import time

logger = logging.getLogger("ai-for-coyote.safety")

VALID_OPS = {
    "temp_strength", "hold_strength", "add_strength", "pulse", "pulse_hold",
    "pulse_cycle", "clear", "stop",
}


class SafetyError(Exception):
    """命令被安全层拒绝。"""


class DeviceOutputError(RuntimeError):
    """A requested safety transition was not confirmed by device transport."""

    def __init__(
        self,
        message: str = "device output reconciliation failed",
        *,
        status_code: int = 503,
        detail: str | None = None,
    ) -> None:
        if status_code not in (409, 503):
            raise ValueError("device output status must be 409 or 503")
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class SafetyManager:
    def __init__(self, cfg) -> None:
        s = cfg["safety"]
        p = cfg["playback"]
        self.caps = {
            "A": int(s["channels"]["A"]["max_strength"]),
            "B": int(s["channels"]["B"]["max_strength"]),
        }
        # 运行时强度上限（页面可调 1~硬上限；不持久化；默认 100，用户可在界面往上调到 200）
        self.user_caps = {ch: min(100, self.caps[ch]) for ch in ("A", "B")}
        self.max_pulse_s = float(p["max_duration_s"])
        self.min_pulse_s = float(p["min_duration_s"])
        self.max_temp_s = float(s["max_temp_duration_s"])
        self.max_step = int(s["max_strength_step"])
        self.overheat_reduce_to = int(s["overheat_reduce_to"])
        self.presets = dict(cfg["presets"])  # 中文波形名 -> {waveform, frames, default/max_duration_s}
        self.playback = dict(p)
        self.ui = dict(cfg.get("ui", {}))

        self.current = {"A": 0, "B": 0}       # 本地跟踪的通道基础强度（跟随设备上报）
        self.requested = {"A": None, "B": None}  # 最近一次请求的强度值（用于对照显示）
        self.app_caps = {"A": None, "B": None}   # App 舒适强度上限（设备实际允许的最大值）
        self._app_policy_caps = {
            ch: {"comfortMax": None, "absoluteMax": None}
            for ch in ("A", "B")
        }
        self.pulse_until = {"A": 0.0, "B": 0.0}  # 波形播放结束时刻（monotonic）
        self.overheat = {"A": False, "B": False}
        self.enabled = {"A": True, "B": True}    # 通道开关（页面可手动开闭）
        # 从 device_channels 配置读通道开关
        for ch in ("A", "B"):
            d = cfg.get("device_channels", {}).get(ch) or {}
            if isinstance(d, dict) and "enabled" in d:
                self.enabled[ch] = bool(d["enabled"])
        self.desired_enabled = dict(self.enabled)
        self.estop_active = False             # 急停中（AI 循环暂停）
        self.dry_run = bool(cfg["app"].get("dry_run", True))

    # ---------- 工具 ----------
    @staticmethod
    def norm_channel(value) -> str:
        if value in (0, "0", "a", "A"):
            return "A"
        if value in (1, "1", "b", "B"):
            return "B"
        raise SafetyError(f"非法通道: {value!r}（只允许 A/B）")

    def cap_for(self, ch: str) -> int:
        cap = min(self.caps[ch], self.user_caps.get(ch, self.caps[ch]))
        app_cap = self.app_caps.get(ch)
        if isinstance(app_cap, int) and not isinstance(app_cap, bool) and app_cap > 0:
            cap = min(cap, app_cap)
        if self.overheat[ch]:
            return min(cap, self.overheat_reduce_to)
        return cap

    def set_user_cap(self, ch: str, value: int) -> int:
        """设置通道运行时强度上限，保留设备跟踪值供物理降档使用。"""
        ch = self.norm_channel(ch)
        v = max(1, min(self.caps[ch], int(value)))
        self.user_caps[ch] = v
        return v

    # ---------- 校验入口 ----------
    def validate(self, action) -> tuple[bool, str, dict | None]:
        """校验一条动作指令。

        返回 (是否通过, 说明, 内部命令)。
        内部命令 kind: temp / add / pulse / clear / stop
        """
        if not isinstance(action, dict):
            return False, "动作必须是 JSON 对象", None
        op = action.get("op")
        if op not in VALID_OPS:
            return False, f"未知 op: {op!r}", None
        if self.estop_active:
            return False, "急停中，拒绝一切设备动作", None

        try:
            if op == "temp_strength":
                return self._validate_temp(action)
            if op == "hold_strength":
                return self._validate_hold(action)
            if op == "add_strength":
                return self._validate_add(action)
            if op == "pulse":
                return self._validate_pulse(action)
            if op == "pulse_hold":
                return self._validate_pulse_hold(action)
            if op == "pulse_cycle":
                return self._validate_pulse_cycle(action)
            if op == "clear":
                return self._validate_clear(action)
            if op == "stop":
                return True, "全部清零并清除", {"kind": "stop"}
        except SafetyError as exc:
            return False, str(exc), None
        return False, "未知错误", None

    def set_channel_enabled(self, ch: str, on: bool) -> None:
        """Set desired and confirmed state for non-transport initialization."""
        ch = self.norm_channel(ch)
        enabled = bool(on)
        self.desired_enabled[ch] = enabled
        self.enabled[ch] = enabled

    def request_channel_enabled(self, ch: str, on: bool) -> bool:
        """Set desired policy without publishing an unconfirmed device state."""
        ch = self.norm_channel(ch)
        enabled = bool(on)
        self.desired_enabled[ch] = enabled
        return enabled

    def confirm_channel_enabled(self, ch: str, on: bool) -> None:
        """Publish a channel state only after coordinator confirmation."""
        ch = self.norm_channel(ch)
        self.enabled[ch] = bool(on)

    def confirm_strength(self, ch: str, value: int) -> None:
        """Publish transport-confirmed physical strength without changing intent."""
        ch = self.norm_channel(ch)
        self.current[ch] = max(0, min(200, int(value)))

    def confirm_clear(self, ch: str) -> None:
        """Publish a transport-confirmed per-channel clear."""
        ch = self.norm_channel(ch)
        self.current[ch] = 0
        self.pulse_until[ch] = 0.0

    def _check_enabled(self, ch: str) -> str | None:
        if not self.enabled.get(ch, True):
            return f"{ch} 通道已手动关闭，拒绝动作"
        return None

    def _safe_target(self, ch: str, raw_value) -> tuple[int, int]:
        """统一钳制目标强度。

        所有“设到某个值”的入口都必须走这里：先受通道有效上限约束，
        再限制单条指令的上升幅度。降低强度不限制步长，确保清零、急停
        和过热降档可以立即生效。

        返回 (实际目标值, 当前有效上限)。
        """
        try:
            requested = int(float(raw_value))
        except (TypeError, ValueError):
            requested = 0
        cap = self.cap_for(ch)
        target = max(0, min(requested, cap))
        current = max(0, min(int(self.current.get(ch, 0)), cap))
        if target > current:
            target = min(target, current + self.max_step)
        return target, cap

    def _validate_temp(self, a: dict):
        ch = self.norm_channel(a.get("channel"))
        if (reason := self._check_enabled(ch)):
            raise SafetyError(reason)
        value, cap = self._safe_target(ch, a.get("value", 0))
        try:
            duration_s = float(a.get("duration_s", 1))
        except (TypeError, ValueError):
            duration_s = 1
        duration_s = max(1.0, min(duration_s, self.max_temp_s))
        return (
            True,
            f"{ch} 通道临时强度 {value}（上限 {cap}），持续 {duration_s:.1f}s",
            {"kind": "temp", "channel": ch, "value": value, "duration_s": duration_s},
        )

    def _validate_hold(self, a: dict):
        """持续强度：保持设定值，直到清除/急停/停止。"""
        ch = self.norm_channel(a.get("channel"))
        if (reason := self._check_enabled(ch)):
            raise SafetyError(reason)
        value, cap = self._safe_target(ch, a.get("value", 0))
        return (
            True,
            f"{ch} 通道持续强度 {value}（上限 {cap}，保持到清除）",
            {"kind": "hold", "channel": ch, "value": value},
        )

    def _validate_add(self, a: dict):
        ch = self.norm_channel(a.get("channel"))
        if (reason := self._check_enabled(ch)):
            raise SafetyError(reason)
        requested_delta = int(float(a.get("delta", 0)))
        requested_delta = max(
            -self.max_step, min(requested_delta, self.max_step)
        )  # 步长钳制
        cap = self.cap_for(ch)
        new_value = max(0, min(self.current[ch] + requested_delta, cap))
        actual_delta = new_value - self.current[ch]
        return (
            True,
            f"{ch} 通道增减 {actual_delta}（当前 {self.current[ch]} -> {new_value}）",
            {
                "kind": "add",
                "channel": ch,
                "delta": actual_delta,
                "requested_delta": requested_delta,
                "value": new_value,
            },
        )

    def _preset_meta(self, pattern: str) -> dict:
        """取波形元数据；未知波形抛 SafetyError。"""
        meta = self.presets.get(pattern)
        if not meta:
            known = "、".join(list(self.presets)[:10])
            raise SafetyError(f"未知波形 {pattern!r}（可用：{known}…）")
        return meta

    def _validate_pulse(self, a: dict):
        ch = self.norm_channel(a.get("channel"))
        if (reason := self._check_enabled(ch)):
            raise SafetyError(reason)
        pattern = str(a.get("pattern", "")).strip()
        meta = self._preset_meta(pattern)
        try:
            duration_s = float(a.get("duration_s", meta["default_duration_s"]))
        except (TypeError, ValueError):
            duration_s = meta["default_duration_s"]
        # 时长钳制：下限 min_pulse_s，上限 = min(全局上限, 该波形上限)
        upper = min(self.max_pulse_s, meta["max_duration_s"])
        duration_s = max(self.min_pulse_s, min(duration_s, upper))
        return (
            True,
            f"{ch} 通道波形「{pattern}」({meta['waveform']})，{duration_s:.1f}s",
            {
                "kind": "pulse",
                "channel": ch,
                "pattern": pattern,
                "wave_key": meta["waveform"],
                "frames": meta["frames"],
                "duration_s": duration_s,
            },
        )

    def _validate_pulse_hold(self, a: dict):
        """持续波形：循环播放，直到清除/急停/停止。"""
        ch = self.norm_channel(a.get("channel"))
        if (reason := self._check_enabled(ch)):
            raise SafetyError(reason)
        pattern = str(a.get("pattern", "")).strip()
        meta = self._preset_meta(pattern)
        return (
            True,
            f"{ch} 通道持续波形「{pattern}」({meta['waveform']})，循环到清除",
            {
                "kind": "pulse_hold",
                "channel": ch,
                "pattern": pattern,
                "wave_key": meta["waveform"],
                "frames": meta["frames"],
            },
        )

    def _validate_pulse_cycle(self, a: dict):
        """单个原始波形周期：不按预设时长补帧或循环。"""
        ch = self.norm_channel(a.get("channel"))
        if (reason := self._check_enabled(ch)):
            raise SafetyError(reason)
        pattern = str(a.get("pattern", "")).strip()
        meta = self._preset_meta(pattern)
        frames = meta.get("frames")
        if not isinstance(frames, list) or not frames:
            raise SafetyError(f"波形 {pattern!r} 没有可播放帧")
        return (
            True,
            f"{ch} 通道波形「{pattern}」({meta['waveform']})，单周期 {len(frames) * 100}ms",
            {
                "kind": "pulse_cycle",
                "channel": ch,
                "pattern": pattern,
                "wave_key": meta["waveform"],
                "frames": frames,
                "duration_ms": len(frames) * 100,
            },
        )

    def _validate_clear(self, a: dict):
        ch = None
        if a.get("channel") is not None:
            ch = self.norm_channel(a["channel"])
        return True, f"清除{'全部' if ch is None else ch + ' 通道'}", {"kind": "clear", "channel": ch}

    # ---------- 急停 ----------
    def estop(self) -> list[dict]:
        """急停：清零 + 清波形 + 暂停。返回需要执行的一组内部命令。"""
        self.estop_active = True
        self.current = {"A": 0, "B": 0}
        self.pulse_until = {"A": 0.0, "B": 0.0}
        logger.warning("急停触发：全部通道清零并清除波形")
        return [{"kind": "clear", "channel": None}, {"kind": "stop"}]

    def resume(self) -> None:
        self.estop_active = False
        logger.info("急停解除，循环恢复")

    # ---------- 执行记录 ----------
    def record(self, cmd: dict) -> None:
        """命令真正发出后更新本地强度跟踪。"""
        kind = cmd["kind"]
        ch = cmd.get("channel")
        if kind in ("temp", "hold") and ch:
            self.current[ch] = cmd["value"]
            self.requested[ch] = cmd["value"]
        elif kind == "add" and ch:
            self.current[ch] = max(0, self.current[ch] + cmd["delta"])
            self.requested[ch] = self.current[ch]
        elif kind == "pulse" and ch:
            # 波形播放期间锁定基础强度显示，忽略设备上报的帧振幅
            self.pulse_until[ch] = time.monotonic() + cmd["duration_s"]
        elif kind == "pulse_hold" and ch:
            # 持续波形：视为长期播放，直到清除
            self.pulse_until[ch] = time.monotonic() + 24 * 3600
        elif kind == "pulse_cycle" and ch:
            self.pulse_until[ch] = time.monotonic() + cmd["duration_ms"] / 1000.0
        elif kind == "zero" and ch:
            # 爆发结束自动归零
            self.current[ch] = 0
            self.requested[ch] = 0
        elif kind == "clear":
            if ch is None:
                self.current = {"A": 0, "B": 0}
                self.pulse_until = {"A": 0.0, "B": 0.0}
            else:
                self.current[ch] = 0
                self.pulse_until[ch] = 0.0
        elif kind == "stop":
            self.current = {"A": 0, "B": 0}
            self.pulse_until = {"A": 0.0, "B": 0.0}

    # ---------- 设备状态同步 ----------
    def update_reported_strength(self, props: dict | None) -> set[str]:
        """Commit strength values directly confirmed by a device report."""
        if not isinstance(props, dict):
            props = {}
        updated: set[str] = set()
        now = time.monotonic()
        for ch, key in (("A", "intensityA"), ("B", "intensityB")):
            if key not in props or now < self.pulse_until[ch]:
                continue
            try:
                self.current[ch] = int(props[key])
            except (TypeError, ValueError):
                continue
            updated.add(ch)
        return updated

    def update_device_policy(self, slot_state: dict | None) -> set[str]:
        """Apply desired overheat/app-cap policy without changing output state."""
        if not isinstance(slot_state, dict):
            slot_state = {}
        changed: set[str] = set()
        for ch, key in (("A", "channelA"), ("B", "channelB")):
            ch_state = slot_state.get(key)
            if not isinstance(ch_state, dict):
                continue
            comfort = ch_state.get("comfortLimit")
            if not isinstance(comfort, dict):
                continue
            if isinstance(comfort.get("overheat"), bool):
                overheat = comfort["overheat"]
                if self.overheat[ch] != overheat:
                    changed.add(ch)
                self.overheat[ch] = overheat
            # Both values are upper bounds. Preserve the last confirmed bound
            # when a report omits them or contains malformed values.
            for field in ("comfortMax", "absoluteMax"):
                value = comfort.get(field)
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and (not isinstance(value, float) or math.isfinite(value))
                    and value > 0
                ):
                    parsed = max(1, int(min(value, self.caps[ch])))
                    self._app_policy_caps[ch][field] = parsed
            confirmed_caps = [
                value
                for value in self._app_policy_caps[ch].values()
                if value is not None
            ]
            if confirmed_caps:
                app_cap = min(confirmed_caps)
                if self.app_caps[ch] != app_cap:
                    changed.add(ch)
                self.app_caps[ch] = app_cap
        return changed

    def update_device_state(self, props: dict | None, slot_state: dict | None) -> None:
        """用设备上报的 props / slotState 同步强度与过热状态。

        波形播放期间设备会上报当前帧振幅，因此该通道的强度值被忽略，
        避免页面数值跟着波形帧乱跳。
        """
        self.update_reported_strength(props)
        self.update_device_policy(slot_state)

    def pulse_active(self) -> dict:
        now = time.monotonic()
        return {ch: now < self.pulse_until[ch] for ch in ("A", "B")}

    def to_state(self) -> dict:
        return {
            "estop": self.estop_active,
            "caps": dict(self.caps),
            "user_caps": dict(self.user_caps),
            "effective_caps": {ch: self.cap_for(ch) for ch in ("A", "B")},
            "app_caps": dict(self.app_caps),
            "current": dict(self.current),
            "requested": dict(self.requested),
            "pulse_active": self.pulse_active(),
            "enabled_channels": dict(self.enabled),
            "overheat": dict(self.overheat),
            "max_pulse_s": self.max_pulse_s,
            "max_temp_s": self.max_temp_s,
            "max_step": self.max_step,
            "presets": [
                {
                    "name": name,
                    "waveform": meta["waveform"],
                    "label": meta["label"],
                    "default_duration_s": meta["default_duration_s"],
                    "max_duration_s": meta["max_duration_s"],
                    "category": meta.get("category", "经典波形"),
                    "frames": meta["frames"],
                }
                for name, meta in self.presets.items()
            ],
            "playback": dict(self.playback),
            "ui": dict(self.ui),
            "dry_run": self.dry_run,
        }
