# -*- coding: utf-8 -*-
"""闭环决策：用户消息 -> 模型台词+指令 -> 安全校验 -> 执行 -> 记录反馈。

这是阶段 1 的核心闭环；阶段 3 的摄像头观察循环也复用同一执行通道。

强度模型（实测结论）：DG-LAB 4 App 对 SetTempIntensity(d=0) 支持不可靠，
AddIntensity（相对增减）是最可靠的原语。因此所有强度命令都换算成
「AddIntensity(目标 - 当前)」实现绝对控制，设备上报值即最终强度。
"""
import asyncio
from copy import deepcopy
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .config import reload_character
from .device_ops import CHANNEL, DeviceOps
from .output_coordinator import (
    DeviceOutputCoordinator,
    OutputIntentKind,
    TransportOutcome,
)
from .safety import DeviceOutputError, SafetyManager

logger = logging.getLogger("ai-for-coyote.game")


@dataclass(frozen=True)
class _AIActionOrigin:
    controller: object | None
    routing_generation: int | None
    mode: str | None
    session_id: str | None


async def _await_owned_group(awaitables):
    """Own every child through settlement before propagating an outcome."""
    tasks = tuple(asyncio.ensure_future(item) for item in awaitables)
    group = asyncio.gather(*tasks, return_exceptions=True)
    caller_cancelled = False
    while not group.done():
        try:
            await asyncio.shield(group)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is None or not current.cancelling():
                raise
            caller_cancelled = True
    results = group.result()
    if caller_cancelled:
        raise asyncio.CancelledError
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


class GameLoop:
    def __init__(self, cfg, llm, safety: SafetyManager, relay, camera=None, audio=None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.safety = safety
        self.relay = relay
        self.camera = camera
        self.audio = audio
        self.ops = DeviceOps()
        self.output_coordinator = DeviceOutputCoordinator()
        for channel in ("A", "B"):
            self.output_coordinator.seed_confirmed(
                channel,
                strength=int(self.safety.current.get(channel, 0)),
                waveform=None,
                waveform_mode=None,
                enabled=bool(self.safety.enabled.get(channel, True)),
            )

        self.history: list[dict] = []          # [{"role","content"}]
        self.notes: list[str] = []             # 反馈按钮等系统备注，注入下一轮
        self.keep = int(cfg["log"]["history_keep"])

        # 循环波形任务：channel -> (task, stop_event)
        self.loop_tasks: dict[str, asyncio.Task] = {}
        self.loop_events: dict[str, asyncio.Event] = {}
        self._loop_reset_requests: set[asyncio.Task] = set()

        # 当前播放的波形（按通道，供页面显示与 AI 上下文）
        self.patterns: dict[str, str | None] = {"A": None, "B": None}

        # 自动观察循环（阶段 3 摄像头闭环）
        self.observe_task: asyncio.Task | None = None
        self.observe_stop = asyncio.Event()
        self.turn_busy = False                 # 防止用户回合与自动观察并发

        # 自动运行（AI 自主回合：观察→描写→动作→发言，玩家不用打字）
        self.autopilot = bool(cfg.get("autopilot", {}).get("enabled", False))
        self.autopilot_interval = float(cfg.get("autopilot", {}).get("interval_s", 12))
        self.autopilot_task: asyncio.Task | None = None
        self.autopilot_stop = asyncio.Event()
        self._autopilot_transition_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()
        self._execution_context = (
            OutputIntentKind.MANUAL,
            None,
        )
        self.on_ai_turn = None                 # 由 AppState 注入：把 AI 主动回合推送到页面
        self.timeline_session = None            # 由 AppState 注入：确定性会话/重放控制器
        self._timeline_character: dict | None = None

        # 双通道保底：轮次计数 + 每通道最近一次强度/波形调整轮次
        self.turn_count = 0
        self.last_strength = {"A": 0, "B": 0}
        self.last_wave = {"A": 0, "B": 0}

        # 怒气值检测：画面持续黑暗 / 麦克风持续无声 → 触手怒气逐轮上升
        self.rage_rounds = 0
        self.rage_triggered = False

    # ---------- 状态 ----------
    def _sensor_rage(self) -> bool:
        """画面持续黑暗 或 麦克风持续无声 → 怒气积累。"""
        dark = False
        if self.camera and self.camera.enabled:
            cs = self.camera.to_state()
            dark = bool(cs.get("has_frame")) and bool(cs.get("dark"))
        silent = False
        if self.audio and self.audio.enabled:
            ast = self.audio.to_state()
            # 麦克风开关关闭（未在监听）时不计无声，避免用户主动关麦却触发怒气
            silent = bool(ast.get("running")) and bool(ast.get("silent"))
        return dark or silent

    def _note_rage(self) -> None:
        """每轮开始前更新怒气值轮数。"""
        self.rage_triggered = self._sensor_rage()
        if self.rage_triggered:
            self.rage_rounds += 1
        else:
            self.rage_rounds = 0

    def build_state(self) -> dict:
        character = self._timeline_character or self.cfg["character"]
        relay_state = self.relay.to_state()
        state = self.safety.to_state()
        state["relay_status"] = relay_state["status"]
        state["controller_id"] = relay_state["controller_id"]
        state["connected"] = relay_state["status"] == "paired"
        state["notes"] = list(self.notes)
        state["camera_enabled"] = bool(self.camera and self.camera.enabled)
        state["camera"] = self.camera.to_state() if self.camera else {}
        # 通道配件与工作状态（台词描写只落在设备位置 / 只写工作通道）
        state["device_channels"] = {
            ch: dict(self.cfg["device_channels"].get(ch) or {})
            for ch in ("A", "B")
        }
        pulse = self.safety.pulse_active()
        state["active_channels"] = {
            ch: bool(self.safety.current.get(ch))
            or bool(pulse.get(ch))
            or (ch in self.loop_tasks)
            for ch in ("A", "B")
        }
        state["patterns"] = dict(self.patterns)
        # 强度基准：跟随配件走（敏感配件基准低，如贴片15/肛塞5）
        state["baseline_strength"] = {}
        for ch in ("A", "B"):
            d = self.cfg["device_channels"].get(ch) or {}
            try:
                value = int(d.get("baseline", 15 if ch == "A" else 5))
            except (TypeError, ValueError):
                value = 15 if ch == "A" else 5
            state["baseline_strength"][ch] = max(0, min(100, value))
        state["rage_rounds"] = self.rage_rounds + int(character.get("rage_baseline") or 0)
        state["rage_triggered"] = self.rage_triggered
        # 角色与风格版本（多角色两级：角色 → 风格档），页面切换用
        state["role"] = str(character.get("role") or "触手")
        state["role_title"] = str(character.get("role_title") or "主人")
        state["roles"] = list(character.get("roles") or [])
        state["profile"] = str(character.get("profile") or "纯爱")
        state["profiles"] = list(character.get("profiles") or ["纯爱"])
        state["profile_available"] = dict(character.get("profile_available") or {})
        state["profile_level"] = str(character.get("profile_level") or "中")
        state["autopilot"] = bool(self.autopilot)
        state["autopilot_interval_s"] = self.autopilot_interval
        # 安全层内部需要原始波形帧，但 HTTP/WebSocket 状态只公开显示元数据。
        state["presets"] = [
            {key: value for key, value in preset.items() if key != "frames"}
            for preset in state.get("presets", [])
        ]
        if self.timeline_session is not None:
            session_state = self.timeline_session.to_state().to_dict()
            state["session"] = session_state
            state["runners"] = dict(session_state["channels"])
        return state

    # ---------- 用户回合 ----------
    def clear_history(self) -> None:
        """清空对话历史（模型上下文；页面消息记录由前端同步清）。"""
        self.history.clear()
        logger.info("对话历史已清空")

    def _character_for_turn(self) -> dict:
        if self._timeline_character is not None:
            if (
                self.timeline_session is not None
                and self.timeline_session.to_state().mode == "autopilot"
                and self.timeline_session.to_state().status.value != "idle"
            ):
                return self._timeline_character
            self._timeline_character = None
        reload_character(self.cfg)
        return self.cfg["character"]

    async def handle_user_message(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"line": "", "executed": [], "dropped": []}
        action_origin = self._capture_ai_action_origin()
        character = self._character_for_turn()

        self.history.append({"role": "user", "content": text})
        self.history = self.history[-self.keep:]

        self.turn_busy = True
        try:
            self._note_rage()
            state = self.build_state()
            error = None
            try:
                line, actions = await self.llm.chat(
                    character, self.history, state,
                    image_b64=self._latest_image(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("模型调用失败")
                error = str(exc)
                if "思维链泄漏" in error or "思维链" in error:
                    line = "（模型走神了：连续输出思考过程已被拦截。把刚才的话再发一次就好。）"
                else:
                    line = f"（模型调用失败：{exc}。请检查 API 配置与网络。）"
                actions = []
            self.turn_count += 1
            executed, dropped, timeline_managed = await self._execute_ai_actions(
                actions, action_origin
            )
            # 模型失败时绝不能执行通道保底，否则用户只会看到错误，
            # 设备却可能在没有有效 AI 决策的情况下自行开始输出。
            if error is None and not timeline_managed:
                await self._apply_channel_floor()
        finally:
            self.turn_busy = False

        self.history.append({"role": "assistant", "content": line})
        return {"line": line, "executed": executed, "dropped": dropped, "error": error}

    # ---------- 画面辅助 ----------
    def _latest_image(self) -> str | None:
        """取摄像头最新帧（启用时）。"""
        if self.camera and self.camera.enabled and self.camera.has_frame():
            return self.camera.base64()
        return None

    # ---------- 主动开场（配对成功后 AI 自动开口） ----------
    async def auto_open(self) -> dict:
        """场景开始：AI 主动开口挑逗并给出第一个轻微试探。"""
        action_origin = self._capture_ai_action_origin()
        character = self._character_for_turn()
        self._note_rage()
        state = self.build_state()
        prompt_msg = {
            "role": "user",
            "content": (
                "（系统提示：场景开始了，玩家刚进入你的领地。"
                "请主动开口挑逗他，并给出第一个轻微试探：低强度 + 一个持续波形（pulse_hold），"
                "不要只调强度不给波形。不要等待玩家先说话。）"
            ),
        }
        self.history.append(prompt_msg)
        self.history = self.history[-self.keep:]
        error = None
        self.turn_busy = True
        try:
            try:
                line, actions = await self.llm.chat(
                    character, self.history, state,
                    image_b64=self._latest_image(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("开场模型调用失败")
                error = str(exc)
                line = f"（开场调用失败：{exc}）"
                actions = []
            self.turn_count += 1
            executed, dropped, timeline_managed = await self._execute_ai_actions(
                actions, action_origin
            )
            if not timeline_managed:
                await self._apply_channel_floor()
        finally:
            self.turn_busy = False
        self.history.append({"role": "assistant", "content": line})
        return {
            "line": line,
            "executed": executed,
            "dropped": dropped,
            "error": error,
        }

    # ---------- 自动观察循环（阶段 3：AI 看图 → 调整策略） ----------
    def start_observe_loop(self) -> None:
        cfg = self.cfg["camera"]
        if (
            self.observe_task
            or not self.camera
            or not self.camera.enabled
            or not bool(cfg.get("auto_observe", True))
        ):
            return
        interval = float(cfg.get("observe_interval_s", 10))
        self.observe_stop = asyncio.Event()
        self.observe_task = asyncio.create_task(self._observe_loop(interval))
        logger.info("自动观察循环启动：每 %ss 看一次画面", interval)

    def stop_observe_loop(self) -> None:
        self.observe_stop.set()
        if self.observe_task:
            self.observe_task.cancel()
            self.observe_task = None

    async def _observe_loop(self, interval: float) -> None:
        while not self.observe_stop.is_set():
            try:
                await asyncio.wait_for(self.observe_stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            if self.safety.estop_active or self.turn_busy:
                continue
            if self.relay.to_state()["status"] != "paired":
                continue
            if not (self.camera and self.camera.has_frame()):
                continue
            result = await self._auto_observe_turn()
            if result and self.on_ai_turn:
                try:
                    await self.on_ai_turn(result)
                except Exception:  # noqa: BLE001
                    logger.exception("自动观察回合推送失败")

    async def _auto_observe_turn(self) -> dict | None:
        """观察最新画面，决定是否调整，并把台词推给页面（由调用方广播）。"""
        action_origin = self._capture_ai_action_origin()
        character = self._character_for_turn()
        self._note_rage()
        state = self.build_state()
        prompt_msg = {
            "role": "user",
            "content": (
                "（系统提示：观察最新画面中玩家的反应。"
                "把你观察到的玩家实时反应用（）写成身体描写，"
                "把你此刻正在做的或刚调整的触手动作也用（）写，"
                "最后说一句台词接住他的状态。不要询问玩家，保持角色。）"
            ),
        }
        self.history.append(prompt_msg)
        self.history = self.history[-self.keep:]
        self.turn_busy = True
        try:
            try:
                line, actions = await self.llm.chat(
                    character, self.history, state,
                    image_b64=self._latest_image(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("自动观察模型调用失败")
                return None
            self.turn_count += 1
            executed, dropped, timeline_managed = await self._execute_ai_actions(
                actions, action_origin
            )
            if not timeline_managed:
                await self._apply_channel_floor()
        finally:
            self.turn_busy = False
        self.history.append({"role": "assistant", "content": line})
        return {"line": line, "executed": executed, "dropped": dropped}

    # ---------- 自动运行（玩家不输入，AI 自主回合） ----------
    async def start_timeline_session(self):
        """Atomically start live recording and its automatic turn task."""
        async with self._autopilot_transition_lock:
            if self.timeline_session is None:
                raise RuntimeError("timeline session is unavailable")
            session_state = await self._start_live_session()
            self._start_autopilot_task()
            return session_state

    async def _start_live_session(self):
        """Reload once, then freeze the exact character input for this session."""
        state = self.timeline_session.to_state()
        if state.mode == "autopilot" and state.status.value == "paused":
            return await self.timeline_session.start_live()
        if state.status.value != "idle":
            return await self.timeline_session.start_live()

        previous_character = self.cfg["character"]
        reload_character(self.cfg)
        character = deepcopy(self.cfg["character"])
        try:
            result = await self.timeline_session.start_live()
        except Exception:
            self.cfg["character"] = previous_character
            self._timeline_character = None
            raise
        self._timeline_character = character
        return result

    def _clear_timeline_character_if_idle(self) -> None:
        if (
            self.timeline_session is not None
            and self.timeline_session.to_state().status.value == "idle"
        ):
            self._timeline_character = None

    async def resume_timeline_session(self, cursor: int | None = None):
        """Atomically resume live recording and its automatic turn task."""
        async with self._autopilot_transition_lock:
            if self.timeline_session is None:
                raise RuntimeError("timeline session is unavailable")
            if self.timeline_session.to_state().mode != "autopilot":
                raise RuntimeError("no live session to resume")
            session_state = await self.timeline_session.resume(cursor)
            self._start_autopilot_task()
            return session_state

    async def start_replay_session(self, replay_id: str, cursor: int = 0):
        """Start replay through the same cancellation-safe lifecycle boundary."""
        async with self._autopilot_transition_lock:
            if self.timeline_session is None:
                raise RuntimeError("timeline session is unavailable")
            return await self._await_timeline_lifecycle(
                lambda: self.timeline_session.start_replay(
                    replay_id, cursor=cursor
                )
            )

    async def pause_replay_session(self):
        """Pause replay without allowing request cancellation to skip clear."""
        async with self._autopilot_transition_lock:
            if self.timeline_session is None:
                raise RuntimeError("timeline session is unavailable")
            if self.timeline_session.to_state().mode != "replay":
                raise RuntimeError("no replay playback to pause")
            return await self._await_timeline_lifecycle(
                self.timeline_session.pause
            )

    async def resume_replay_session(self):
        """Resume replay through the owned lifecycle task."""
        async with self._autopilot_transition_lock:
            if self.timeline_session is None:
                raise RuntimeError("timeline session is unavailable")
            if self.timeline_session.to_state().mode != "replay":
                raise RuntimeError("no replay playback to resume")
            return await self._await_timeline_lifecycle(
                self.timeline_session.resume
            )

    async def execute_manual_action(self, action: dict) -> tuple[list, list]:
        """Quiesce recorded playback before issuing unrecorded manual output."""
        async with self._autopilot_transition_lock:
            session_takeover_active = False
            controller = self.timeline_session
            if controller is not None:
                session_state = controller.to_state()
                session_takeover_active = session_state.mode in (
                    "autopilot",
                    "replay",
                )
                if session_state.mode == "autopilot" and session_state.status.value in (
                    "running",
                    "paused",
                    "finishing",
                ):
                    try:
                        await self._await_timeline_lifecycle(controller.pause)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if not self._global_clear_is_confirmed():
                            raise DeviceOutputError(
                                "manual prerequisite clear was not confirmed",
                                status_code=503,
                            ) from exc
                        raise
                    finally:
                        await self._stop_autopilot_task()
                elif session_state.mode == "replay":
                    try:
                        await self._await_timeline_lifecycle(controller.stop)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if not self._global_clear_is_confirmed():
                            raise DeviceOutputError(
                                "manual prerequisite clear was not confirmed",
                                status_code=503,
                            ) from exc
                        raise
            if session_takeover_active:
                if not self._global_clear_is_confirmed():
                    await self.clear_output()
                if not self._global_clear_is_confirmed():
                    raise DeviceOutputError(
                        "manual prerequisite clear was not confirmed",
                        status_code=503,
                    )
            channels = self._action_channels((action,))
            generations = {
                channel: self.output_coordinator.invalidate(
                    channel, OutputIntentKind.MANUAL
                )
                for channel in channels
            }
            return await self.execute_actions(
                [action],
                intent=OutputIntentKind.MANUAL,
                owner_generations=generations,
            )

    def begin_timeline_output(
        self, channels=("A", "B")
    ) -> dict[str, int]:
        """Claim fresh coordinator generations for live or replay output."""
        normalized = self._normalize_output_channels(channels)
        return {
            channel: self.output_coordinator.invalidate(
                channel, OutputIntentKind.TIMELINE_OR_REPLAY
            )
            for channel in normalized
        }

    def require_output_clear(self, channels=("A", "B")) -> None:
        """Synchronously stale normal owners and block output until clear."""
        for channel in self._normalize_output_channels(channels):
            self.output_coordinator.require_clear(channel)

    async def execute_timeline_actions(
        self,
        actions: list,
        owner_generations: Mapping[str, int],
    ) -> tuple[list, list]:
        if not isinstance(owner_generations, Mapping):
            raise TypeError("timeline output generations must be a mapping")
        required = set(self._action_channels(actions))
        provided = {
            str(channel).strip().upper() for channel in owner_generations
        }
        if not required.issubset(provided):
            return [], [
                {
                    "action": action,
                    "reason": "timeline output owner generation is missing",
                    "sent": False,
                }
                for action in actions
            ]
        return await self.execute_actions(
            actions,
            intent=OutputIntentKind.TIMELINE_OR_REPLAY,
            owner_generations=owner_generations,
        )

    async def set_autopilot(self, enabled: bool) -> None:
        """开启/关闭自动运行：AI 每 interval_s 秒自主观察、描写、调整设备并发言。"""
        async with self._autopilot_transition_lock:
            if enabled:
                if self.timeline_session is not None:
                    session_state = self.timeline_session.to_state()
                    if session_state.mode not in (None, "autopilot"):
                        raise RuntimeError("replay playback is active")
                    if session_state.status.value in ("idle", "paused"):
                        if session_state.status.value == "idle":
                            await self._start_live_session()
                        else:
                            await self.timeline_session.start_live()
                    elif session_state.status.value != "running":
                        raise RuntimeError("live session transition is already in progress")
                self._start_autopilot_task()
                return

            if self.timeline_session is not None:
                session_state = self.timeline_session.to_state()
                if (
                    session_state.mode == "autopilot"
                    and session_state.status.value
                    in ("running", "paused", "finishing")
                ):
                    try:
                        await self._await_timeline_lifecycle(
                            self.timeline_session.pause
                        )
                    finally:
                        await self._stop_autopilot_task()
                    logger.info("自动运行已停止")
                    return
            await self._stop_autopilot_task()
            logger.info("自动运行已停止")

    async def finish_timeline_session(self):
        """Stop automatic turns and normally finish the current live session."""
        async with self._autopilot_transition_lock:
            if self.timeline_session is None:
                raise RuntimeError("timeline session is unavailable")
            try:
                result = await self._await_timeline_lifecycle(
                    self.timeline_session.finish
                )
                self._clear_timeline_character_if_idle()
                return result
            finally:
                await self._stop_autopilot_task()

    async def stop_timeline_session(self):
        """Abnormally stop live or replay work without creating an archive."""
        async with self._autopilot_transition_lock:
            try:
                if self.timeline_session is None:
                    raise RuntimeError("timeline session is unavailable")
                result = await self._await_timeline_lifecycle(
                    self.timeline_session.stop
                )
                self._clear_timeline_character_if_idle()
                return result
            finally:
                await self._stop_autopilot_task()

    async def _await_timeline_lifecycle(self, operation):
        """Let controller cleanup reach a safe state before propagating cancellation."""
        lifecycle = asyncio.create_task(
            operation(), name="game-loop-timeline-lifecycle"
        )
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(lifecycle)
                break
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                if lifecycle.done():
                    result = lifecycle.result()
                    break
        if cancellation is not None:
            raise cancellation
        return result

    def _start_autopilot_task(self) -> None:
        self.autopilot = True
        if self.autopilot_task is None or self.autopilot_task.done():
            self.autopilot_stop = asyncio.Event()
            self.autopilot_task = asyncio.create_task(self._autopilot_loop())
            logger.info(
                "自动运行已开启：每 %.1fs 一个自主回合",
                self.autopilot_interval,
            )

    async def _stop_autopilot_task(self) -> None:
        self.autopilot = False
        self.autopilot_stop.set()
        task = self.autopilot_task
        self.autopilot_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _autopilot_loop(self) -> None:
        while not self.autopilot_stop.is_set():
            try:
                try:
                    await asyncio.wait_for(self.autopilot_stop.wait(), timeout=self.autopilot_interval)
                    break
                except asyncio.TimeoutError:
                    pass
                if self.safety.estop_active or self.turn_busy:
                    continue
                if not (self.relay.to_state()["status"] == "paired" or self.safety.dry_run):
                    continue  # 设备未配对不自动运行（用户设定：连接设备后才开始）
                await self._autopilot_turn()
            except Exception:  # noqa: BLE001
                # 任何异常都不能杀死循环任务（曾因此静默死亡导致 AI 一直不说话）
                logger.exception("自动运行循环异常，跳过本轮继续")

    async def _autopilot_turn(self) -> dict | None:
        action_origin = self._capture_ai_action_origin()
        character = self._character_for_turn()
        self._note_rage()
        state = self.build_state()
        has_ai = any(m["role"] == "assistant" for m in self.history)
        prompt_msg = {
            "role": "user",
            "content": (
                "（系统提示：自动回合。观察当前画面与玩家状态（有画面/麦克风信号就写真实观察，"
                "没有就推进场景），按「玩家反应（）→触手动作（）→发言」写一段，"
                "并给出合适的设备动作（至少一个波形 + 强度组合，不要只调强度）。"
                "画面看不清的部分保留悬念。保持角色，不要询问玩家。）"
                if has_ai else
                "（系统提示：场景开始了，玩家刚进入你的领地。"
                "请主动开口挑逗他，并给出第一个轻微试探：低强度 + 一个持续波形（pulse_hold），"
                "不要只调强度不给波形。不要等待玩家先说话。）"
            ),
        }
        self.history.append(prompt_msg)
        self.history = self.history[-self.keep:]
        self.turn_busy = True
        try:
            try:
                line, actions = await self.llm.chat(
                    character, self.history, state,
                    image_b64=self._latest_image(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("自动回合模型调用失败: %s", exc)
                return None
            self.turn_count += 1
            executed, dropped, timeline_managed = await self._execute_ai_actions(
                actions, action_origin
            )
            if not timeline_managed:
                await self._apply_channel_floor()
        finally:
            self.turn_busy = False
        self.history.append({"role": "assistant", "content": line})
        logger.info("自动回合台词: %s", line)
        result = {"line": line, "executed": executed, "dropped": dropped}
        if self.on_ai_turn:
            try:
                await self.on_ai_turn(result)
            except Exception:  # noqa: BLE001
                logger.exception("自动回合推送失败")
        return result

    def _capture_ai_action_origin(self) -> _AIActionOrigin:
        controller = self.timeline_session
        if controller is None:
            return _AIActionOrigin(None, None, None, None)
        state = controller.to_state()
        return _AIActionOrigin(
            controller=controller,
            routing_generation=controller.routing_generation,
            mode=state.mode,
            session_id=state.session_id,
        )

    async def _execute_ai_actions(
        self,
        actions: list,
        origin: _AIActionOrigin | None = None,
    ) -> tuple[list, list, bool]:
        """Route AI intent only within the generation where its turn began."""
        if origin is None:
            origin = self._capture_ai_action_origin()
        controller = self.timeline_session
        if origin.controller is not None:
            if (
                controller is not origin.controller
                or controller.routing_generation != origin.routing_generation
            ):
                return [], [], True
            session_state = controller.to_state()
            if origin.mode is not None:
                if (
                    origin.mode == "autopilot"
                    and session_state.mode == "autopilot"
                    and session_state.session_id == origin.session_id
                    and session_state.status.value == "running"
                ):
                    await controller.process_live_turn(
                        actions,
                        expected_routing_generation=origin.routing_generation,
                    )
                return [], [], True
            if session_state.mode is not None:
                return [], [], True
        elif controller is not None:
            return [], [], True
        executed, dropped = await self.execute_actions(actions)
        return executed, dropped, False

    # ---------- 动作执行（AI 与手动共用） ----------
    async def _apply_channel_floor(self) -> None:
        """双通道保底：两轮后 A/B 都必须有波形且强度≠0；每 2 轮内强度与波形至少各调一次。"""
        if not (self.safety.enabled.get("A") and self.safety.enabled.get("B")):
            return
        if self.safety.estop_active:
            return
        client_id = self.relay.first_client_id()
        slot_id = self.relay.get_slot_id()
        ready = bool(client_id and slot_id)
        if not ready and not self.safety.dry_run:
            return
        for ch in ("A", "B"):
            pending = self.output_coordinator.pending(ch)
            if pending.clear_required or pending.target_strength is not None:
                continue
            generation = self.output_coordinator.generation(ch)
            base = int(self.cfg["device_channels"].get(ch, {}).get("baseline", 15 if ch == "A" else 5))
            wave_active = (ch in self.loop_tasks) or bool(self.safety.pulse_active().get(ch))
            strength = self.safety.current.get(ch, 0)
            fixed = False
            # 规则 1：前两轮结束后，强度必须非零且必须有波形
            if self.turn_count >= 2:
                if not wave_active:
                    sent = await self._apply_floor_default_wave(
                        ch,
                        client_id,
                        slot_id,
                        ready,
                        self.safety.dry_run,
                        generation,
                    )
                    if not sent:
                        continue
                    if not self._floor_generation_is_current(ch, generation):
                        self._cancel_loops(ch, reset_pulse=False)
                        continue
                    self.last_wave[ch] = self.turn_count
                    fixed = True
                if strength <= 0:
                    ok, reason, cmd = self.safety.validate(
                        {"op": "hold_strength", "channel": ch, "value": base}
                    )
                    if not ok or cmd is None:
                        logger.warning("通道保底强度被安全层拒绝: %s -> %s", ch, reason)
                        continue
                    sent = await self._apply_floor_strength(
                        ch, cmd, client_id, slot_id, ready, self.safety.dry_run
                    )
                    if not sent:
                        continue
                    if not self._floor_generation_is_current(ch, generation):
                        continue
                    self.last_strength[ch] = self.turn_count
                    fixed = True
            # 规则 2：每 2 轮内，强度与波形至少各调整一次
            if self.turn_count - self.last_strength.get(ch, 0) >= 2:
                delta = 5 if strength < self.safety.cap_for(ch) else -5
                ok, reason, cmd = self.safety.validate(
                    {"op": "add_strength", "channel": ch, "delta": delta}
                )
                if not ok or cmd is None:
                    logger.warning("通道保底增减被安全层拒绝: %s -> %s", ch, reason)
                    continue
                sent = await self._apply_floor_strength(
                    ch, cmd, client_id, slot_id, ready, self.safety.dry_run
                )
                if not sent:
                    continue
                if not self._floor_generation_is_current(ch, generation):
                    continue
                self.last_strength[ch] = self.turn_count
                fixed = True
            if self.turn_count - self.last_wave.get(ch, 0) >= 2:
                sent = await self._apply_floor_default_wave(
                    ch,
                    client_id,
                    slot_id,
                    ready,
                    self.safety.dry_run,
                    generation,
                )
                if not sent:
                    continue
                if not self._floor_generation_is_current(ch, generation):
                    self._cancel_loops(ch, reset_pulse=False)
                    continue
                self.last_wave[ch] = self.turn_count
                fixed = True
            if fixed:
                logger.info("通道保底：%s 通道强度/波形已自动补齐（第 %d 轮）", ch, self.turn_count)

    def _floor_generation_is_current(self, channel: str, generation: int) -> bool:
        pending = self.output_coordinator.pending(channel)
        return (
            self.output_coordinator.is_current(channel, generation)
            and not pending.clear_required
            and pending.target_strength is None
            and self.safety.desired_enabled[channel]
        )

    async def _apply_floor_strength(
        self, channel, cmd, client_id, slot_id, ready, dry_run
    ) -> bool:
        async def transport(snapshot):
            current = int(snapshot.strength or 0)
            effective_cap = self.safety.cap_for(channel)
            if cmd["kind"] == "hold":
                effective_strength = max(
                    0, min(int(cmd["value"]), effective_cap)
                )
            else:
                requested_delta = int(
                    cmd.get("requested_delta", cmd["delta"])
                )
                effective_strength = max(
                    0, min(current + requested_delta, effective_cap)
                )
            if dry_run:
                return TransportOutcome(
                    sent=True,
                    simulated=True,
                    effective={"strength": effective_strength},
                )
            if not ready:
                return TransportOutcome(sent=False, error="device is not connected")
            delta = effective_strength - current
            frames = (
                [
                    self.ops.add_strength(
                        client_id,
                        slot_id,
                        CHANNEL[channel],
                        delta,
                    )
                ]
                if delta
                else []
            )
            sent = all(await self._send_all(frames))
            return TransportOutcome(
                sent=sent,
                effective={"strength": effective_strength} if sent else None,
                error=None if sent else "channel floor strength transport failed",
            )

        outcome = await self._run_coordinated_safety(
            channel, OutputIntentKind.MANUAL, transport
        )
        if not outcome.sent:
            return False
        confirmed = self.output_coordinator.confirmed(channel)
        self.safety.requested[channel] = confirmed.strength
        return True

    async def _apply_floor_default_wave(
        self, channel, client_id, slot_id, ready, dry_run, generation
    ) -> bool:
        pattern = str(self.cfg["ui"].get("default_wave", "呼吸") or "呼吸")
        meta = self.safety.presets.get(pattern)
        if not meta and self.safety.presets:
            pattern = next(iter(self.safety.presets))
            meta = self.safety.presets[pattern]
        if not meta:
            return False
        cmd = {
            "kind": "pulse_hold",
            "channel": channel,
            "pattern": pattern,
            "wave_key": meta["waveform"],
            "frames": meta["frames"],
        }
        batch_expires_at: float | None = None

        async def transport(snapshot):
            nonlocal batch_expires_at
            if dry_run:
                return TransportOutcome(
                    sent=True,
                    simulated=True,
                    effective={
                        "waveform": pattern,
                        "waveform_mode": "loop",
                    },
                )
            if not ready:
                return TransportOutcome(sent=False, error="device is not connected")
            batch_expires_at = await self._start_pulse_loop(
                client_id,
                slot_id,
                CHANNEL[channel],
                channel,
                cmd,
                owner_generation=generation,
            )
            sent = batch_expires_at is not None
            stale = not self._floor_generation_is_current(channel, generation)
            if stale:
                self._cancel_loops(channel, reset_pulse=False)
            return TransportOutcome(
                sent=sent,
                effective=(
                    {
                        "waveform": pattern,
                        "waveform_mode": "finite" if stale else "loop",
                    }
                    if sent
                    else None
                ),
                error=None if sent else "channel floor waveform transport failed",
            )

        outcome = await self._run_coordinated_safety(
            channel, OutputIntentKind.MANUAL, transport
        )
        if not outcome.sent:
            return False
        confirmed = self.output_coordinator.confirmed(channel)
        if confirmed.waveform_mode == "loop":
            self.safety.record(cmd)
        elif (
            confirmed.waveform_mode == "finite"
            and batch_expires_at is not None
        ):
            self.safety.pulse_until[channel] = batch_expires_at
        elif confirmed.waveform is None:
            self.safety.pulse_until[channel] = 0.0
        logger.info(
            "自动挂载默认波形：%s 通道「%s」（强度需波形承载）",
            channel,
            pattern,
        )
        return True

    async def set_runtime_cap(self, channel: str, value: int) -> dict:
        """Apply desired cap policy and publish strength only after delivery."""
        channel = self.safety.norm_channel(channel)
        requested_cap = max(1, min(self.safety.caps[channel], int(value)))
        previous_effective_cap = self.safety.cap_for(channel)
        applied = self.safety.set_user_cap(channel, requested_cap)
        if self.safety.cap_for(channel) < previous_effective_cap:
            self.output_coordinator.invalidate_queued_normal(channel)
        self._sync_output_coordinator_channel(channel)
        confirmed_strength = self.output_coordinator.confirmed(channel).strength
        effective_cap = self.safety.cap_for(channel)
        if confirmed_strength is not None and confirmed_strength > effective_cap:
            self.output_coordinator.mark_reduction(channel, effective_cap)

        pending = self.output_coordinator.pending(channel)
        needs_reconciliation = (
            pending.clear_required or pending.target_strength is not None
        )
        restart = False
        controller = self.timeline_session
        if needs_reconciliation and controller is not None:
            restart = await controller.suspend_channel_for_safety(
                channel, reason="runtime_cap"
            )

        async with self._action_lock:
            self._sync_output_coordinator_channel(channel)
            confirmed_strength = self.output_coordinator.confirmed(channel).strength
            effective_cap = self.safety.cap_for(channel)
            if confirmed_strength is not None and confirmed_strength > effective_cap:
                self.output_coordinator.mark_reduction(channel, effective_cap)
            reconciled = await self._reconcile_runtime_safety_locked((channel,))

        confirmed_strength = self.output_coordinator.confirmed(channel).strength or 0
        if (
            restart
            and self.safety.desired_enabled[channel]
            and confirmed_strength <= self.safety.cap_for(channel)
        ):
            await controller.resume_channel_after_safety(channel)
        result = reconciled[channel]
        return {
            "value": applied,
            "effective_cap": self.safety.cap_for(channel),
            "executed": result["executed"],
            "dropped": result["dropped"],
        }

    async def set_channel_enabled(self, channel: str, enabled: bool) -> dict:
        """Commit enabled state only after any required clear is confirmed."""
        channel = self.safety.norm_channel(channel)
        desired = self.safety.request_channel_enabled(channel, enabled)
        self._sync_output_coordinator_channel(channel)
        confirmed = self.output_coordinator.confirmed(channel)
        was_confirmed_enabled = confirmed.enabled
        pending = self.output_coordinator.pending(channel)
        if (
            not desired
            and not pending.clear_required
            and (
                confirmed.enabled
                or confirmed.strength not in (None, 0)
                or confirmed.waveform is not None
                or confirmed.waveform_mode is not None
            )
        ):
            self.output_coordinator.require_clear(channel)

        controller = self.timeline_session
        restart = False
        pending = self.output_coordinator.pending(channel)
        if controller is not None and (
            not desired
            or pending.clear_required
            or pending.target_strength is not None
        ):
            restart = await controller.suspend_channel_for_safety(
                channel, reason="channel_disabled"
            )

        async with self._action_lock:
            self._sync_output_coordinator_channel(channel)
            reconciled = await self._reconcile_runtime_safety_locked((channel,))

        if (
            desired
            and controller is not None
            and (restart or not was_confirmed_enabled)
        ):
            await controller.resume_channel_after_safety(channel)
        result = reconciled[channel]
        return {
            "enabled": self.safety.enabled[channel],
            "executed": result["executed"],
            "dropped": result["dropped"],
            "runner_was_active": restart,
        }

    async def update_device_state(
        self, props: dict | None, slot_state: dict | None
    ) -> dict[str, dict]:
        """Apply device feedback and retry every pending safety transition."""
        policy_channels = {
            channel
            for channel, key in (("A", "channelA"), ("B", "channelB"))
            if isinstance(slot_state, dict) and key in slot_state
        }
        strength_channels = {
            channel
            for channel, key in (("A", "intensityA"), ("B", "intensityB"))
            if isinstance(props, dict) and key in props
        }
        previous_effective_caps = {
            channel: self.safety.cap_for(channel) for channel in policy_channels
        }
        self.safety.update_device_policy(slot_state)
        for channel, previous_cap in previous_effective_caps.items():
            if self.safety.cap_for(channel) < previous_cap:
                self.output_coordinator.invalidate_queued_normal(channel)
        confirmed_reports = self.safety.update_reported_strength(props)
        reported_strengths = {
            channel: int(self.safety.current[channel])
            for channel in confirmed_reports
        }
        affected = tuple(
            channel
            for channel in ("A", "B")
            if channel in policy_channels or channel in strength_channels
        )
        if not affected:
            return {}

        channel_results = await _await_owned_group(
            self._reconcile_device_report_channel(
                channel,
                reported_strength=reported_strengths.get(channel),
            )
            for channel in affected
        )
        return {
            channel: result
            for channel, result in zip(affected, channel_results)
            if result is not None
        }

    async def _reconcile_device_report_channel(
        self,
        channel: str,
        *,
        reported_strength: int | None,
    ) -> dict | None:
        cap = self.safety.cap_for(channel)
        try:
            reconciliation = await self.output_coordinator.reconcile_reported_strength(
                channel, reported_strength, cap
            )
        finally:
            self._publish_coordinator_confirmed(channel)

        pending = self.output_coordinator.pending(channel)
        if (
            not reconciliation.reduction_required
            and not pending.clear_required
            and pending.target_strength is None
        ):
            return None

        controller = self.timeline_session
        restart = False
        if controller is not None:
            restart = await controller.suspend_channel_for_safety(
                channel, reason="overheat"
            )

        try:
            result = await self._reconcile_channel_safety_locked(channel)
        except DeviceOutputError as exc:
            result = self._safety_failure_result(channel, exc)

        confirmed_strength = (
            self.output_coordinator.confirmed(channel).strength or 0
        )
        if (
            controller is not None
            and restart
            and not result["dropped"]
            and self.safety.desired_enabled[channel]
            and confirmed_strength <= self.safety.cap_for(channel)
        ):
            await controller.resume_channel_after_safety(channel)
        return result

    async def reconcile_runtime_safety(
        self, channel: str | None = None
    ) -> dict[str, dict]:
        """Retry pending clear/reduction work through dedicated transports."""
        channels = (
            ("A", "B")
            if channel is None
            else (self.safety.norm_channel(channel),)
        )
        async with self._action_lock:
            return await self._reconcile_runtime_safety_locked(channels)

    async def _reconcile_runtime_safety_locked(
        self, channels: tuple[str, ...]
    ) -> dict[str, dict]:
        results: dict[str, dict] = {}
        failures: list[DeviceOutputError] = []
        for channel in channels:
            try:
                results[channel] = await self._reconcile_channel_safety_locked(
                    channel
                )
            except DeviceOutputError as exc:
                failures.append(exc)
                results[channel] = self._safety_failure_result(channel, exc)
        if failures:
            raise failures[0]
        return results

    async def _reconcile_channel_safety_locked(self, channel: str) -> dict:
        coordinator = self.output_coordinator
        desired_enabled = self.safety.desired_enabled[channel]
        confirmed = coordinator.confirmed(channel)
        pending = coordinator.pending(channel)

        if (
            not desired_enabled
            and not pending.clear_required
            and (
                confirmed.enabled
                or confirmed.strength not in (None, 0)
                or confirmed.waveform is not None
                or confirmed.waveform_mode is not None
            )
        ):
            coordinator.require_clear(channel)
            pending = coordinator.pending(channel)

        cap = self.safety.cap_for(channel)
        if (
            not pending.clear_required
            and confirmed.strength is not None
            and confirmed.strength > cap
        ):
            coordinator.mark_reduction(channel, cap)
            pending = coordinator.pending(channel)

        executed: list[dict] = []
        if pending.clear_required:
            self._cancel_loops(channel, reset_pulse=False)
            outcome = await self._run_coordinated_safety(
                channel,
                OutputIntentKind.CLEAR_OR_DISABLE,
                lambda snapshot: self._transport_safety_clear(
                    channel, snapshot, desired_enabled
                ),
            )
            self._require_safety_outcome(channel, "clear", outcome)
            confirmed = coordinator.confirmed(channel)
            self.safety.confirm_clear(channel)
            self.safety.confirm_channel_enabled(channel, confirmed.enabled)
            self.patterns[channel] = None
            executed.append(
                self._safety_success_result(channel, "clear", outcome)
            )

        confirmed = coordinator.confirmed(channel)
        pending = coordinator.pending(channel)
        if pending.target_strength is not None:
            target = pending.target_strength
            outcome = await self._run_coordinated_safety(
                channel,
                OutputIntentKind.SAFETY_REDUCE,
                lambda snapshot: self._transport_safety_strength_delta(
                    channel, target, snapshot
                ),
            )
            self._require_safety_outcome(channel, "strength_delta", outcome)
            confirmed = coordinator.confirmed(channel)
            self.safety.confirm_strength(channel, int(confirmed.strength or 0))
            executed.append(
                self._safety_success_result(
                    channel, "strength_delta", outcome
                )
            )

        confirmed = coordinator.confirmed(channel)
        if desired_enabled and not confirmed.enabled:
            outcome = await self._run_coordinated_safety(
                channel,
                OutputIntentKind.CLEAR_OR_DISABLE,
                lambda snapshot: self._confirmed_local_enable(snapshot),
            )
            self._require_safety_outcome(channel, "enable", outcome)
            confirmed = coordinator.confirmed(channel)
            self.safety.confirm_clear(channel)
            self.safety.confirm_channel_enabled(channel, confirmed.enabled)
            self.patterns[channel] = None
            executed.append(
                self._safety_success_result(channel, "enable", outcome)
            )

        return {"executed": executed, "dropped": []}

    async def _run_coordinated_safety(self, channel, kind, operation):
        """Publish coordinator state before propagating transport cancellation."""
        try:
            return await self.output_coordinator.run(channel, kind, operation)
        finally:
            self._publish_coordinator_confirmed(channel)

    def _publish_coordinator_confirmed(self, channel: str) -> None:
        """Synchronously mirror the coordinator's committed channel snapshot."""
        confirmed = self.output_coordinator.confirmed(channel)
        if confirmed.strength is not None:
            self.safety.confirm_strength(channel, confirmed.strength)
        self.safety.confirm_channel_enabled(channel, confirmed.enabled)
        self.patterns[channel] = confirmed.waveform
        if confirmed.waveform is None and confirmed.waveform_mode is None:
            self.safety.pulse_until[channel] = 0.0

    async def _transport_safety_strength_delta(
        self, channel: str, target: int, confirmed
    ) -> TransportOutcome:
        current = int(confirmed.strength or 0)
        if current <= target:
            return TransportOutcome(
                sent=True,
                simulated=True,
                effective={"strength": current},
            )
        if self.safety.dry_run:
            return TransportOutcome(
                sent=True,
                simulated=True,
                effective={"strength": target},
            )
        client_id = self.relay.first_client_id()
        slot_id = self.relay.get_slot_id()
        if not client_id or not slot_id:
            return TransportOutcome(sent=False, error="device is not connected")
        frame = self.ops.add_strength(
            client_id,
            slot_id,
            CHANNEL[channel],
            target - current,
        )
        sent = bool(await self.relay.send_frame(frame))
        return TransportOutcome(
            sent=sent,
            effective={"strength": target} if sent else None,
            error=None if sent else "strength delta transport failed",
        )

    async def _transport_safety_clear(
        self, channel: str, confirmed, enabled: bool
    ) -> TransportOutcome:
        effective = {
            "strength": 0,
            "waveform": None,
            "waveform_mode": None,
            "enabled": enabled,
        }
        if self.safety.dry_run:
            return TransportOutcome(
                sent=True,
                simulated=True,
                effective=effective,
            )
        client_id = self.relay.first_client_id()
        slot_id = self.relay.get_slot_id()
        if not client_id or not slot_id:
            return TransportOutcome(sent=False, error="device is not connected")
        numeric_channel = CHANNEL[channel]
        frames = [self.ops.clear(client_id, slot_id, numeric_channel)]
        strength = int(confirmed.strength or 0)
        if strength:
            frames.append(
                self.ops.add_strength(
                    client_id, slot_id, numeric_channel, -strength
                )
            )
        frames.append(
            self.ops.reset_intensity(client_id, slot_id, numeric_channel)
        )
        sent = all(await self._send_all(frames))
        return TransportOutcome(
            sent=sent,
            effective=effective if sent else None,
            error=None if sent else "channel clear transport failed",
        )

    @staticmethod
    async def _confirmed_local_enable(confirmed) -> TransportOutcome:
        return TransportOutcome(
            sent=True,
            simulated=True,
            effective={
                "strength": int(confirmed.strength or 0),
                "waveform": None,
                "waveform_mode": None,
                "enabled": True,
            },
        )

    @staticmethod
    def _require_safety_outcome(
        channel: str, operation: str, outcome: TransportOutcome
    ) -> None:
        if outcome.sent:
            return
        detail = outcome.error or "device output was not confirmed"
        conflict = any(
            marker in detail
            for marker in (
                "stale output generation",
                "blocked by higher-priority output intent",
                "pending safety reduction changed",
                "channel is disabled",
            )
        )
        raise DeviceOutputError(
            f"{channel} {operation} was not confirmed",
            status_code=409 if conflict else 503,
            detail=detail,
        )

    @staticmethod
    def _safety_success_result(
        channel: str, operation: str, outcome: TransportOutcome
    ) -> dict:
        effective_strength = 0
        if outcome.effective is not None:
            effective_strength = int(outcome.effective.get("strength") or 0)
        return {
            "action": {"op": operation, "channel": channel},
            "effective": {
                "op": operation,
                "channel": channel,
                "effective_strength": effective_strength,
            },
            "reason": "runtime safety reconciled",
            "sent": not outcome.simulated,
        }

    @staticmethod
    def _safety_failure_result(
        channel: str, error: DeviceOutputError
    ) -> dict:
        return {
            "executed": [],
            "dropped": [
                {
                    "action": {"op": "safety_reconcile", "channel": channel},
                    "reason": "device output was not confirmed",
                    "sent": False,
                }
            ],
        }

    def _sync_output_coordinator_channel(self, channel: str) -> None:
        confirmed = self.output_coordinator.confirmed(channel)
        pattern = self.patterns.get(channel)
        waveform = confirmed.waveform
        waveform_mode = confirmed.waveform_mode
        if pattern is None:
            waveform = None
            waveform_mode = None
        elif channel in self.loop_tasks:
            waveform = pattern
            waveform_mode = "loop"
        elif self.safety.pulse_active().get(channel):
            waveform = pattern
            waveform_mode = waveform_mode or "finite"
        else:
            waveform = None
            waveform_mode = None
        self.output_coordinator.seed_confirmed(
            channel,
            strength=int(self.safety.current.get(channel, 0)),
            waveform=waveform,
            waveform_mode=waveform_mode,
            enabled=bool(self.safety.enabled.get(channel, True)),
        )

    async def execute_actions(
        self,
        actions: list,
        *,
        intent: OutputIntentKind = OutputIntentKind.MANUAL,
        owner_generations: Mapping[str, int] | None = None,
    ) -> tuple[list, list]:
        """Validate and execute actions through the single output coordinator.

        ``_execute_actions_locked`` deliberately keeps its historical one-argument
        shape because a few integration seams wrap it.  The surrounding action
        lock makes this short-lived execution context task-safe.
        """
        if not isinstance(intent, OutputIntentKind):
            raise ValueError("intent must be an OutputIntentKind")
        effective_intent = intent
        if (
            intent is OutputIntentKind.MANUAL
            and owner_generations is None
            and self.timeline_session is not None
            and self.timeline_session.to_state().mode is not None
        ):
            # Legacy low-level callers inside an active session are executor
            # traffic, not the user-facing manual endpoint.  The endpoint
            # always supplies an owned MANUAL generation after quiescing.
            effective_intent = OutputIntentKind.TIMELINE_OR_REPLAY
        generations = (
            None
            if owner_generations is None
            else {
                self.safety.norm_channel(channel): int(generation)
                for channel, generation in owner_generations.items()
            }
        )
        async with self._action_lock:
            previous = self._execution_context
            self._execution_context = (effective_intent, generations)
            try:
                return await self._execute_actions_locked(actions)
            finally:
                self._execution_context = previous

    async def _execute_actions_locked(self, actions: list) -> tuple[list, list]:
        """校验并执行动作列表，返回 (已执行说明列表, 被拒绝说明列表)。"""
        executed, dropped = [], []
        if not isinstance(actions, list):
            return executed, dropped

        intent, owner_generations = self._execution_context

        if owner_generations is None:
            for channel in self._action_channels(actions):
                pending = self.output_coordinator.pending(channel)
                if (
                    not pending.clear_required
                    and pending.target_strength is None
                ):
                    self._sync_output_coordinator_channel(channel)

        client_id = self.relay.first_client_id()
        slot_id = self.relay.get_slot_id()
        ready = bool(client_id and slot_id)
        dry_run = self.safety.dry_run
        explicit_cycle_channels = set()
        activation_strength_channels = set()
        continuation_cycle_channels = set()
        for candidate in actions:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("op") == "hold_strength":
                try:
                    strength_channel = self.safety.norm_channel(
                        candidate.get("channel")
                    )
                except Exception:
                    strength_channel = None
                if strength_channel in ("A", "B"):
                    activation_strength_channels.add(strength_channel)
            if candidate.get("op") != "pulse_cycle":
                continue
            wave_ok, _, wave_cmd = self.safety.validate(candidate)
            if wave_ok and wave_cmd and wave_cmd.get("channel") in ("A", "B"):
                explicit_cycle_channels.add(wave_cmd["channel"])
                if candidate.get("_strength_prerequisite_confirmed") is True:
                    continuation_cycle_channels.add(wave_cmd["channel"])
        required_cycle_prerequisites = (
            explicit_cycle_channels - continuation_cycle_channels
            if intent is OutputIntentKind.TIMELINE_OR_REPLAY
            else explicit_cycle_channels & activation_strength_channels
        )
        activation_ready = {
            channel: False for channel in required_cycle_prerequisites
        }

        for action in actions:
            if not isinstance(action, dict):
                dropped.append({"action": action, "reason": "动作必须是 JSON 对象"})
                continue
            if action.get("op") == "hold_strength":
                try:
                    prerequisite_channel = self.safety.norm_channel(
                        action.get("channel")
                    )
                except Exception:
                    prerequisite_channel = None
                if prerequisite_channel in activation_ready:
                    activation_ready[prerequisite_channel] = False
            ok, reason, cmd = self.safety.validate(action)
            if (
                not ok
                and self.safety.estop_active
                and action.get("op") in ("clear", "stop")
            ):
                channel = action.get("channel")
                if action.get("op") == "stop" or channel is None:
                    cmd = {
                        "kind": "stop" if action.get("op") == "stop" else "clear",
                        "channel": None,
                    }
                else:
                    try:
                        channel = self.safety.norm_channel(channel)
                    except Exception:
                        channel = None
                    if channel is not None:
                        cmd = {"kind": "clear", "channel": channel}
                if cmd is not None:
                    ok, reason = True, "急停期间安全清除"
            if not ok:
                dropped.append({"action": action, "reason": reason})
                logger.warning("动作被安全层拒绝: %s -> %s", action, reason)
                continue

            channel = cmd.get("channel")
            if (
                cmd["kind"] == "pulse_cycle"
                and channel in activation_ready
                and not activation_ready[channel]
            ):
                dropped.append(
                    {
                        "action": action,
                        "reason": "strength prerequisite was not confirmed",
                        "sent": False,
                    }
                )
                continue
            if (
                channel in ("A", "B")
                and cmd["kind"] not in ("clear", "stop")
            ):
                pending = self.output_coordinator.pending(channel)
                if pending.clear_required or pending.target_strength is not None:
                    dropped.append(
                        {
                            "action": action,
                            "reason": "通道安全状态仍待物理确认",
                            "sent": False,
                        }
                    )
                    continue

            if not ready and not dry_run:
                dropped.append({"action": action, "reason": "设备未连接（无 clientId/slotId）"})
                continue

            expected_generations = owner_generations or {}
            expected_generation = (
                expected_generations.get(channel)
                if channel in ("A", "B")
                else None
            )
            helper_required = (
                cmd["kind"] in ("hold", "add", "temp")
                and channel in ("A", "B")
                and channel not in explicit_cycle_channels
                and channel not in self.loop_tasks
                and not self.safety.pulse_active().get(channel)
            )

            transaction = await self._run_action_transaction(
                cmd=cmd,
                intent=intent,
                expected_generation=expected_generation,
                helper_required=helper_required,
                client_id=client_id,
                slot_id=slot_id,
                ready=ready,
                dry_run=dry_run,
            )
            if not transaction["complete"]:
                dropped.append(
                    self._transport_failure(
                        action,
                        cmd,
                        transaction["error"] or "设备发送失败",
                        sent=False,
                    )
                )
                continue

            if cmd["kind"] in ("temp", "hold", "add") and channel in ("A", "B"):
                confirmed_strength = self.output_coordinator.confirmed(
                    channel
                ).strength
                self.safety.requested[channel] = int(confirmed_strength or 0)
            else:
                self.safety.record(cmd)
            if (
                cmd["kind"] == "hold"
                and action.get("op") == "hold_strength"
                and channel in activation_ready
            ):
                activation_ready[channel] = True
            if transaction.get("helper_cmd") is not None:
                self.safety.record(transaction["helper_cmd"])
            self._record_success_turn(cmd)
            label = self._describe(cmd)
            executed.append({
                "action": action,
                "effective": self._effective_result(action, cmd),
                "reason": reason,
                "sent": transaction["sent"],
                "label": label,
            })
            logger.info(
                "%s执行动作: %s（%s）",
                "DRY-RUN " if (dry_run or not ready) else "",
                label,
                "已发送" if transaction["sent"] else "模拟",
            )
        return executed, dropped

    async def _run_action_transaction(
        self,
        *,
        cmd: dict,
        intent: OutputIntentKind,
        expected_generation: int | None,
        helper_required: bool,
        client_id: str | None,
        slot_id: str | None,
        ready: bool,
        dry_run: bool,
    ) -> dict:
        """Run one complete high-level action and expose only committed success."""
        if cmd["kind"] in ("clear", "stop"):
            return await self._run_clear_transaction(
                cmd=cmd,
                intent=(
                    OutputIntentKind.ESTOP
                    if self.safety.estop_active
                    else intent
                    if intent >= OutputIntentKind.CLEAR_OR_DISABLE
                    else OutputIntentKind.CLEAR_OR_DISABLE
                ),
                client_id=client_id,
                slot_id=slot_id,
                ready=ready,
                dry_run=dry_run,
            )

        channel = cmd["channel"]
        coordinator = self.output_coordinator
        generation = (
            coordinator.generation(channel)
            if expected_generation is None
            else expected_generation
        )
        policy_epoch = coordinator.normal_policy_epoch(channel)
        state = {
            "complete": False,
            "sent": False,
            "error": None,
            "generation": generation,
            "helper_cmd": None,
            "helper_expiry": None,
            "helper_task": None,
            "helper_event": None,
            "retired_helper_task": None,
            "retired_helper_event": None,
            "cancelled": False,
        }

        def normal_policy_is_current() -> bool:
            if coordinator.normal_policy_epoch(channel) != policy_epoch:
                return False
            return not (
                cmd["kind"] in ("temp", "hold", "add")
                and int(cmd["value"]) > self.safety.cap_for(channel)
            )

        async def transport(confirmed):
            if (
                not self._normal_output_is_current(channel, generation)
                or not normal_policy_is_current()
            ):
                state["error"] = "stale output safety policy"
                return TransportOutcome(sent=False, error=state["error"])
            helper_cmd = self._default_wave_command(channel) if helper_required else None
            if helper_required and helper_cmd is None:
                state["error"] = "默认波形不可用"
                return TransportOutcome(sent=False, error=state["error"])
            helper_sent = False
            if helper_cmd is not None:
                state["helper_cmd"] = helper_cmd
                if dry_run:
                    helper_sent = True
                else:
                    try:
                        expiry = await self._start_pulse_loop(
                            client_id,
                            slot_id,
                            CHANNEL[channel],
                            channel,
                            helper_cmd,
                            owner_generation=generation,
                            owner_intent=intent,
                        )
                    except asyncio.CancelledError:
                        state["cancelled"] = True
                        state["error"] = "默认波形发送被取消"
                        return await self._rollback_helper_transport(
                            channel, confirmed, helper_cmd, state
                        )
                    except Exception as exc:
                        state["error"] = f"默认波形发送失败: {exc}"
                        return TransportOutcome(sent=False, error=state["error"])
                    helper_sent = expiry is not None
                    state["helper_expiry"] = expiry
                    state["helper_task"] = self.loop_tasks.get(channel)
                    state["helper_event"] = self.loop_events.get(channel)
                if not helper_sent:
                    state["error"] = "默认波形发送失败"
                    return TransportOutcome(sent=False, error=state["error"])
                if (
                    not self._normal_output_is_current(channel, generation)
                    or not normal_policy_is_current()
                ):
                    state["error"] = "stale output safety policy"
                    return await self._rollback_helper_transport(
                        channel, confirmed, helper_cmd, state
                    )

            effective = self._coordinator_effective_for_command(cmd, confirmed)
            if cmd["kind"] == "pulse_hold":
                if dry_run:
                    state["complete"] = True
                    return TransportOutcome(
                        sent=True, simulated=True, effective=effective
                    )
                try:
                    expiry = await self._start_pulse_loop(
                        client_id,
                        slot_id,
                        CHANNEL[channel],
                        channel,
                        cmd,
                        owner_generation=generation,
                        owner_intent=intent,
                    )
                except Exception as exc:
                    state["error"] = f"设备发送失败: {exc}"
                    return TransportOutcome(sent=False, error=state["error"])
                if expiry is None:
                    state["error"] = "设备发送失败"
                    return TransportOutcome(sent=False, error=state["error"])
                state["complete"] = True
                state["sent"] = True
                state["helper_expiry"] = expiry
                return TransportOutcome(sent=True, effective=effective)
            if dry_run:
                if helper_cmd is not None:
                    effective.update(
                        waveform=helper_cmd["pattern"], waveform_mode="loop"
                    )
                state["complete"] = True
                return TransportOutcome(
                    sent=True, simulated=True, effective=effective
                )

            frames = self._build_frames_from_confirmed(
                cmd, client_id, slot_id, confirmed
            )
            if not frames:
                primary_sent, primary_error = True, None
                simulated = True
            else:
                try:
                    primary_sent, primary_error = await self._send_frames_complete(
                        frames
                    )
                except asyncio.CancelledError:
                    state["cancelled"] = True
                    state["error"] = "设备发送被取消"
                    if helper_cmd is not None:
                        return await self._rollback_helper_transport(
                            channel, confirmed, helper_cmd, state
                        )
                    raise
                simulated = False
            if not primary_sent:
                state["error"] = (
                    f"设备发送失败: {primary_error}"
                    if primary_error
                    else "设备发送失败"
                )
                if helper_cmd is not None:
                    return await self._rollback_helper_transport(
                        channel, confirmed, helper_cmd, state
                    )
                return TransportOutcome(sent=False, error=state["error"])

            if cmd["kind"] in ("pulse", "pulse_cycle"):
                self._cancel_loops(channel, reset_pulse=False)
            if helper_cmd is not None:
                effective.update(
                    waveform=helper_cmd["pattern"], waveform_mode="loop"
                )
            if cmd["kind"] == "temp":
                # The coordinator owns this callback through settlement even
                # when its caller is cancelled.  Install the delayed revert
                # before publishing transport completion so cancellation can
                # never strand a committed temporary strength.
                self._schedule_temp_revert(
                    client_id,
                    slot_id,
                    CHANNEL[channel],
                    channel,
                    cmd["duration_s"],
                    owner_generation=generation,
                    owner_revision=coordinator.revision(channel) + 1,
                    owner_intent=intent,
                )
            state["complete"] = True
            state["sent"] = not simulated or helper_sent
            return TransportOutcome(
                sent=True,
                simulated=simulated and not helper_sent,
                effective=effective,
            )

        try:
            outcome = await coordinator.run(channel, intent, transport)
        finally:
            self._publish_coordinator_confirmed(channel)
            retired_task = state.get("retired_helper_task")
            if isinstance(retired_task, asyncio.Task):
                await self._reap_retired_helper(
                    channel,
                    retired_task,
                    state.get("retired_helper_event"),
                )
        if not outcome.sent and state["error"] is None:
            state["error"] = outcome.error or "设备发送失败"
        if state["cancelled"]:
            raise asyncio.CancelledError
        return state

    async def _rollback_helper_transport(
        self, channel: str, confirmed, helper_cmd: dict, state: dict
    ) -> TransportOutcome:
        """Clean a sent helper before reporting its parent action as dropped."""
        helper_task = state.get("helper_task")
        helper_event = state.get("helper_event")
        self.output_coordinator.retire_helper(channel)
        if (
            isinstance(helper_task, asyncio.Task)
            and helper_task is not asyncio.current_task()
            and self.loop_tasks.get(channel) is helper_task
        ):
            if (
                isinstance(helper_event, asyncio.Event)
                and self.loop_events.get(channel) is helper_event
            ):
                helper_event.set()
            self._loop_reset_requests.add(helper_task)
            helper_task.cancel()
            state["retired_helper_task"] = helper_task
            state["retired_helper_event"] = helper_event
        frames = self._channel_clear_frames(
            channel,
            self.relay.first_client_id(),
            self.relay.get_slot_id(),
            int(confirmed.strength or 0),
        )
        try:
            cleanup_sent, cleanup_error = await self._send_frames_complete(frames)
        except asyncio.CancelledError:
            state["cancelled"] = True
            cleanup_sent = False
            cleanup_error = "helper clear was cancelled"
        if cleanup_sent:
            return TransportOutcome(
                sent=True,
                effective={
                    "strength": 0,
                    "waveform": None,
                    "waveform_mode": None,
                    "enabled": confirmed.enabled,
                },
                error=state["error"],
            )
        # This callback runs inside the coordinator-owned, cancellation-shielded
        # task.  Publish the conservative physical helper effect and block every
        # lower-priority owner here, before caller cancellation can be re-raised.
        self.output_coordinator.require_clear(channel)
        expiry = state.get("helper_expiry")
        if isinstance(expiry, (int, float)):
            self.safety.pulse_until[channel] = float(expiry)
        if cleanup_error:
            state["error"] = f"{state['error']}; helper clear failed: {cleanup_error}"
        return TransportOutcome(
            sent=True,
            effective={
                "strength": int(confirmed.strength or 0),
                "waveform": helper_cmd["pattern"],
                "waveform_mode": "finite",
                "enabled": confirmed.enabled,
            },
            error=state["error"],
        )

    async def _reap_retired_helper(
        self,
        channel: str,
        task: asyncio.Task,
        stop_event: asyncio.Event | None,
    ) -> None:
        """Retrieve one retired worker after its coordinator lock is released."""
        result = await asyncio.gather(task, return_exceptions=True)
        error = result[0]
        if isinstance(error, BaseException) and not isinstance(
            error, asyncio.CancelledError
        ):
            logger.error(
                "%s channel retired helper failed during cleanup",
                channel,
                exc_info=(type(error), error, error.__traceback__),
            )
        if self.loop_tasks.get(channel) is task:
            self.loop_tasks.pop(channel, None)
        if self.loop_events.get(channel) is stop_event:
            self.loop_events.pop(channel, None)

    async def _run_clear_transaction(
        self,
        *,
        cmd: dict,
        intent: OutputIntentKind,
        client_id: str | None,
        slot_id: str | None,
        ready: bool,
        dry_run: bool,
    ) -> dict:
        channel = cmd.get("channel")
        global_clear = cmd["kind"] == "stop" or channel is None
        target = None if global_clear else channel
        self._cancel_loops(target, reset_pulse=False)
        state = {
            "complete": False,
            "sent": False,
            "error": None,
            "generation": None,
            "helper_cmd": None,
        }

        if global_clear:
            async def transport(snapshots):
                effective = {
                    name: {
                        "strength": 0,
                        "waveform": None,
                        "waveform_mode": None,
                        "enabled": snapshots[name].enabled,
                    }
                    for name in ("A", "B")
                }
                if dry_run:
                    state["complete"] = True
                    return TransportOutcome(
                        sent=True, simulated=True, effective=effective
                    )
                if not ready:
                    state["error"] = "设备未连接（无 clientId/slotId）"
                    return TransportOutcome(sent=False, error=state["error"])
                frames = self._global_clear_frames(
                    client_id, slot_id, snapshots
                )
                sent, error = await self._send_frames_complete(frames)
                if sent:
                    state["complete"] = True
                    state["sent"] = True
                else:
                    state["error"] = (
                        f"设备发送失败: {error}" if error else "设备发送失败"
                    )
                return TransportOutcome(
                    sent=sent,
                    effective=effective if sent else None,
                    error=state["error"],
                )

            try:
                outcome = await self.output_coordinator.run_global(intent, transport)
            finally:
                for name in ("A", "B"):
                    self._publish_coordinator_confirmed(name)
        else:
            async def transport(confirmed):
                effective = {
                    "strength": 0,
                    "waveform": None,
                    "waveform_mode": None,
                    "enabled": confirmed.enabled,
                }
                if dry_run:
                    state["complete"] = True
                    return TransportOutcome(
                        sent=True, simulated=True, effective=effective
                    )
                if not ready:
                    state["error"] = "设备未连接（无 clientId/slotId）"
                    return TransportOutcome(sent=False, error=state["error"])
                frames = self._channel_clear_frames(
                    channel,
                    client_id,
                    slot_id,
                    int(confirmed.strength or 0),
                )
                sent, error = await self._send_frames_complete(frames)
                if sent:
                    state["complete"] = True
                    state["sent"] = True
                else:
                    state["error"] = (
                        f"设备发送失败: {error}" if error else "设备发送失败"
                    )
                return TransportOutcome(
                    sent=sent,
                    effective=effective if sent else None,
                    error=state["error"],
                )

            try:
                outcome = await self.output_coordinator.run(
                    channel, intent, transport
                )
            finally:
                self._publish_coordinator_confirmed(channel)

        if not outcome.sent and state["error"] is None:
            state["error"] = outcome.error or "设备发送失败"
        return state

    def _default_wave_command(self, channel: str) -> dict | None:
        pattern = str(self.cfg["ui"].get("default_wave", "呼吸") or "呼吸")
        meta = self.safety.presets.get(pattern)
        if not meta and self.safety.presets:
            pattern = next(iter(self.safety.presets))
            meta = self.safety.presets[pattern]
        if not meta:
            return None
        return {
            "kind": "pulse_hold",
            "channel": channel,
            "pattern": pattern,
            "wave_key": meta["waveform"],
            "frames": meta["frames"],
        }

    def _normal_output_is_current(self, channel: str, generation: int) -> bool:
        pending = self.output_coordinator.pending(channel)
        confirmed = self.output_coordinator.confirmed(channel)
        return (
            self.output_coordinator.is_current(channel, generation)
            and not pending.clear_required
            and pending.target_strength is None
            and confirmed.enabled
            and self.safety.desired_enabled.get(channel, False)
            and not self.safety.estop_active
        )

    @staticmethod
    def _coordinator_effective_for_command(cmd: dict, confirmed) -> dict:
        kind = cmd["kind"]
        if kind in ("hold", "add", "temp"):
            return {"strength": int(cmd["value"])}
        if kind in ("pulse", "pulse_cycle"):
            return {
                "strength": int(confirmed.strength or 0),
                "waveform": cmd["pattern"],
                "waveform_mode": "finite",
            }
        if kind == "pulse_hold":
            return {
                "strength": int(confirmed.strength or 0),
                "waveform": cmd["pattern"],
                "waveform_mode": "loop",
            }
        raise ValueError(f"unsupported coordinated command: {kind}")

    def _build_frames_from_confirmed(
        self,
        cmd: dict,
        client_id: str | None,
        slot_id: str | None,
        confirmed,
    ) -> list[dict]:
        if client_id is None or slot_id is None:
            return []
        kind = cmd["kind"]
        channel = cmd["channel"]
        if kind in ("hold", "add", "temp"):
            delta = int(cmd["value"]) - int(confirmed.strength or 0)
            return (
                [
                    self.ops.add_strength(
                        client_id, slot_id, CHANNEL[channel], delta
                    )
                ]
                if delta
                else []
            )
        return self._build_frames(cmd, client_id, slot_id)

    def _channel_clear_frames(
        self,
        channel: str,
        client_id: str | None,
        slot_id: str | None,
        strength: int,
    ) -> list[dict]:
        if client_id is None or slot_id is None:
            return []
        numeric = CHANNEL[channel]
        frames = [self.ops.clear(client_id, slot_id, numeric)]
        if strength:
            frames.append(
                self.ops.add_strength(
                    client_id, slot_id, numeric, -int(strength)
                )
            )
        frames.append(self.ops.reset_intensity(client_id, slot_id, numeric))
        return frames

    def _global_clear_frames(
        self,
        client_id: str | None,
        slot_id: str | None,
        snapshots: Mapping[str, object],
    ) -> list[dict]:
        if client_id is None or slot_id is None:
            return []
        frames = [self.ops.clear(client_id, slot_id)]
        for channel in ("A", "B"):
            strength = int(getattr(snapshots[channel], "strength", 0) or 0)
            if strength:
                frames.append(
                    self.ops.add_strength(
                        client_id,
                        slot_id,
                        CHANNEL[channel],
                        -strength,
                    )
                )
            frames.append(
                self.ops.reset_intensity(
                    client_id, slot_id, CHANNEL[channel]
                )
            )
        return frames

    async def _send_frames_complete(
        self, frames: list[dict]
    ) -> tuple[bool, str | None]:
        """Attempt every frame so safety cleanup is not truncated by one error."""
        if not frames:
            return False, "no transport frames"
        sent = True
        errors: list[str] = []
        for frame in frames:
            try:
                if not await self.relay.send_frame(frame):
                    sent = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                sent = False
                errors.append(str(exc) or type(exc).__name__)
        return sent, "; ".join(errors) or None

    @staticmethod
    def _normalize_output_channels(channels) -> tuple[str, ...]:
        if isinstance(channels, str):
            channels = (channels,)
        normalized: list[str] = []
        for channel in channels:
            value = str(channel).strip().upper()
            if value not in ("A", "B"):
                raise ValueError("channel must be A or B")
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ValueError("at least one output channel is required")
        return tuple(normalized)

    def _action_channels(self, actions) -> tuple[str, ...]:
        channels: list[str] = []
        for action in actions:
            if not isinstance(action, Mapping):
                continue
            if action.get("op") == "stop" or (
                action.get("op") == "clear" and action.get("channel") is None
            ):
                return ("A", "B")
            raw = action.get("channel")
            if raw is None:
                continue
            try:
                channel = self.safety.norm_channel(raw)
            except Exception:
                continue
            if channel not in channels:
                channels.append(channel)
        return tuple(channels)

    def _global_clear_is_confirmed(self) -> bool:
        return self.output_clear_is_confirmed(("A", "B"))

    def output_clear_is_confirmed(self, channels=("A", "B")) -> bool:
        """Return whether the coordinator has no output or pending clear work."""
        for channel in self._normalize_output_channels(channels):
            confirmed = self.output_coordinator.confirmed(channel)
            pending = self.output_coordinator.pending(channel)
            if (
                confirmed.strength not in (None, 0)
                or confirmed.waveform is not None
                or confirmed.waveform_mode is not None
                or pending.clear_required
                or pending.target_strength is not None
            ):
                return False
        return True

    def _record_success_turn(self, cmd: dict) -> None:
        channel = cmd.get("channel")
        if channel not in ("A", "B"):
            return
        if cmd["kind"] in ("hold", "add", "temp"):
            self.last_strength[channel] = self.turn_count
        if cmd["kind"] in ("pulse", "pulse_hold", "pulse_cycle"):
            self.last_wave[channel] = self.turn_count

    def _transport_failure(
        self, action: dict, cmd: dict, reason: str, *, sent: bool
    ) -> dict:
        effective = self._effective_result(action, cmd)
        channel = cmd.get("channel")
        if channel in ("A", "B") and "effective_strength" in effective:
            effective["effective_strength"] = int(
                self.safety.current.get(channel, 0)
            )
        if cmd["kind"] == "add":
            effective["effective_delta"] = 0
        return {
            "action": action,
            "effective": effective,
            "reason": reason,
            "sent": sent,
        }

    async def clear_output(self, channel=None) -> tuple[list, list]:
        """物理清除输出，不触发或改变急停状态。"""
        action = (
            {"op": "stop"}
            if channel is None
            else {"op": "clear", "channel": channel}
        )
        return await self.execute_actions([action])

    def _effective_result(self, action: dict, cmd: dict) -> dict:
        """返回适合时间线记账的生效值；设备原始帧不得泄漏到结果。"""
        effective = {"op": action["op"]}
        ch = cmd.get("channel")
        if ch in ("A", "B"):
            effective["channel"] = ch
        if "pattern" in cmd:
            effective["pattern"] = cmd["pattern"]
        if cmd["kind"] in ("temp", "hold"):
            effective["requested_strength"] = action.get("value")
            effective["effective_strength"] = cmd["value"]
        elif cmd["kind"] == "add":
            effective["requested_delta"] = action.get("delta")
            effective["effective_delta"] = cmd["delta"]
            effective["effective_strength"] = cmd["value"]
        elif cmd["kind"] in ("pulse", "pulse_hold", "pulse_cycle") and ch in ("A", "B"):
            effective["effective_strength"] = self.safety.current[ch]
        if cmd["kind"] == "pulse":
            effective["duration_ms"] = int(cmd["duration_s"] * 1000)
        elif cmd["kind"] == "pulse_cycle":
            effective["duration_ms"] = cmd["duration_ms"]
        elif cmd["kind"] in ("clear", "stop"):
            cleared = {
                "effective_strength": 0,
                "pattern": None,
                "waveform_mode": None,
            }
            if ch in ("A", "B"):
                effective.update(cleared)
            else:
                effective["channels"] = {
                    channel: dict(cleared) for channel in ("A", "B")
                }
        return effective

    def _build_frames(self, cmd: dict, client_id: str | None, slot_id: str | None) -> list[dict]:
        """内部命令 -> V4 服务器帧列表。client_id/slot_id 可能为 None（dry-run 时）。"""
        frames: list[dict] = []
        kind = cmd["kind"]
        if client_id is None or slot_id is None:
            return frames
        ch = CHANNEL.get(cmd.get("channel"))          # 数字通道（设备帧用）
        ch_name = cmd.get("channel")                   # 字母通道（safety 状态用）
        if kind == "temp":
            # 爆发：加差值到目标，到时自动归零
            delta = cmd["value"] - self.safety.current[ch_name]
            if delta:
                frames.append(self.ops.add_strength(client_id, slot_id, ch, delta))
        elif kind == "hold":
            # 持续强度：加差值到目标（AddIntensity 是实测可靠的原语）
            delta = cmd["value"] - self.safety.current[ch_name]
            if delta:
                frames.append(self.ops.add_strength(client_id, slot_id, ch, delta))
        elif kind == "add":
            frames.append(self.ops.add_strength(client_id, slot_id, ch, cmd["delta"]))
        elif kind == "pulse":
            # 波形按帧消费（每帧 100ms），帧播完即停；循环补齐到请求的时长
            base = cmd["frames"]
            total = max(1, int(round(cmd["duration_s"] * 10)))
            tiled = (base * (total // len(base) + 1))[:total] if base else []
            frames.append(
                self.ops.pulse(
                    client_id, slot_id, ch, tiled,
                    int(cmd["duration_s"] * 1000), immediate=True,
                )
            )
        elif kind == "pulse_cycle":
            frames.append(
                self.ops.pulse(
                    client_id, slot_id, ch, cmd["frames"],
                    cmd["duration_ms"], immediate=True,
                )
            )
        elif kind == "clear":
            frames.append(
                self.ops.clear(client_id, slot_id, None if cmd["channel"] is None else ch)
            )
            # 用可靠的 AddIntensity 负值归零，另发 reset 兜底
            if cmd["channel"] is None:
                for c in ("A", "B"):
                    if self.safety.current[c]:
                        frames.append(
                            self.ops.add_strength(
                                client_id, slot_id, CHANNEL[c], -self.safety.current[c]
                            )
                        )
                    frames.append(self.ops.reset_intensity(client_id, slot_id, CHANNEL[c]))
            else:
                if self.safety.current[ch_name]:
                    frames.append(
                        self.ops.add_strength(
                            client_id, slot_id, ch, -self.safety.current[ch_name]
                        )
                    )
                frames.append(self.ops.reset_intensity(client_id, slot_id, ch))
        elif kind == "stop":
            frames.append(self.ops.clear(client_id, slot_id))
            for c in ("A", "B"):
                if self.safety.current[c]:
                    frames.append(
                        self.ops.add_strength(
                            client_id, slot_id, CHANNEL[c], -self.safety.current[c]
                        )
                    )
                frames.append(self.ops.reset_intensity(client_id, slot_id, CHANNEL[c]))
        return frames

    def _schedule_temp_revert(
        self,
        client_id: str,
        slot_id: str,
        ch: int,
        ch_name: str,
        duration_s: float,
        *,
        owner_generation: int,
        owner_revision: int,
        owner_intent: OutputIntentKind,
    ) -> None:
        """爆发时长结束后自动归零（AddIntensity 负值 + reset 兜底）。"""

        async def revert() -> None:
            await asyncio.sleep(duration_s)
            if (
                not self._normal_output_is_current(
                    ch_name, owner_generation
                )
                or self.output_coordinator.revision(ch_name) != owner_revision
            ):
                return

            async def transport(confirmed):
                if (
                    not self._normal_output_is_current(
                        ch_name, owner_generation
                    )
                    or self.output_coordinator.revision(ch_name)
                    != owner_revision
                ):
                    return TransportOutcome(
                        sent=False, error="stale output generation"
                    )
                value = int(confirmed.strength or 0)
                frames = []
                if value:
                    frames.append(
                        self.ops.add_strength(
                            client_id, slot_id, ch, -value
                        )
                    )
                frames.append(
                    self.ops.reset_intensity(client_id, slot_id, ch)
                )
                sent, error = await self._send_frames_complete(frames)
                if not sent:
                    # The coordinator owns this callback past caller
                    # cancellation, so establish the retryable safety block
                    # before cancellation can resume the outer task.
                    self.output_coordinator.require_clear(ch_name)
                return TransportOutcome(
                    sent=sent,
                    effective={"strength": 0} if sent else None,
                    error=error or (None if sent else "temp revert failed"),
                )

            try:
                outcome = await self.output_coordinator.run(
                    ch_name, owner_intent, transport
                )
            finally:
                self._publish_coordinator_confirmed(ch_name)
            if outcome.sent:
                self.safety.record({"kind": "zero", "channel": ch_name})
                logger.info("爆发结束，%s 通道自动归零", ch_name)

        asyncio.create_task(revert())

    # ---------- 循环波形（无时间约束，直到清除/急停） ----------
    async def _start_pulse_loop(
        self,
        client_id: str,
        slot_id: str,
        ch: int,
        ch_name: str,
        cmd: dict,
        *,
        owner_generation: int | None = None,
        owner_intent: OutputIntentKind = OutputIntentKind.MANUAL,
    ) -> float | None:
        """分批下发波形实现无限循环，批间提前覆盖消除真空期。

        - 每批 = 波形自然周期的整数倍时长（尽量贴合循环边界）
        - 提前 loop_overlap_s 秒重发下一批（im=true 直接替换旧批），
          设备全程无「停止-重启」的空档
        - 不依赖 App 的 d=0（实测不可靠），只用普通波形帧
        """
        if owner_generation is None:
            owner_generation = self.output_coordinator.generation(ch_name)
        helper_generation = self.output_coordinator.helper_generation(ch_name)
        base = cmd["frames"]
        playback = self.cfg["playback"]
        frame_s = float(playback["frame_ms"]) / 1000.0
        natural = max(len(base) * frame_s, 0.1)
        batch_s = max(float(playback["loop_batch_s"]), natural)
        mult = max(1, round(batch_s / natural))
        batch_s = natural * mult
        overlap = min(float(playback["loop_overlap_s"]), batch_s * 0.5)
        total = max(1, int(round(batch_s * 10)))
        tiled = (base * (total // len(base) + 1))[:total] if base else []
        def next_frame() -> dict:
            # 每次重发都生成新的 reqId；复用同一帧会被 DG-LAB 4
            # 以 duplicate_request_id 拒绝，导致循环波形在首批后停止。
            return self.ops.pulse(
                client_id, slot_id, ch, tiled, int(batch_s * 1000), immediate=True
            )
        wait_s = max(0.1, batch_s - overlap)

        if (
            not self._normal_output_is_current(ch_name, owner_generation)
            or self.output_coordinator.helper_generation(ch_name)
            != helper_generation
        ):
            return None
        initial_sent, initial_error = await self._send_frames_complete(
            [next_frame()]
        )
        if not initial_sent:
            if initial_error:
                raise RuntimeError(initial_error)
            return None
        last_batch_expires_at = time.monotonic() + batch_s

        # The replacement frame is now confirmed, so the old resend owner can
        # be retired without turning a failed replacement into an output gap.
        self._cancel_loops(ch_name, reset_pulse=False)

        stop_event = asyncio.Event()
        self.loop_events[ch_name] = stop_event

        async def worker() -> None:
            nonlocal last_batch_expires_at
            current_task = asyncio.current_task()
            logger.info(
                "%s 通道循环波形开始：%s（批次 %.1fs，提前 %.2fs 覆盖）",
                ch_name, cmd["pattern"], batch_s, overlap,
            )
            try:
                while not stop_event.is_set():
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(), timeout=wait_s
                        )
                        break
                    except asyncio.TimeoutError:
                        pass
                    if not self._normal_output_is_current(
                        ch_name, owner_generation
                    ) or self.output_coordinator.helper_generation(
                        ch_name
                    ) != helper_generation:
                        break
                    async def resend(confirmed):
                        if (
                            stop_event.is_set()
                            or not self._normal_output_is_current(
                                ch_name, owner_generation
                            )
                            or self.output_coordinator.helper_generation(
                                ch_name
                            )
                            != helper_generation
                        ):
                            return TransportOutcome(
                                sent=False,
                                error="stale helper lease",
                            )
                        sent, error = await self._send_frames_complete(
                            [next_frame()]
                        )
                        return TransportOutcome(
                            sent=sent,
                            effective={
                                "waveform": cmd["pattern"],
                                "waveform_mode": "loop",
                            }
                            if sent
                            else None,
                            error=error or (
                                None if sent else "waveform resend failed"
                            ),
                        )

                    try:
                        outcome = await self.output_coordinator.run(
                            ch_name, owner_intent, resend
                        )
                    finally:
                        self._publish_coordinator_confirmed(ch_name)
                    if outcome.sent:
                        last_batch_expires_at = time.monotonic() + batch_s
                    else:
                        break
            finally:
                reset_requested = current_task in self._loop_reset_requests
                self._loop_reset_requests.discard(current_task)
                owns_task = self.loop_tasks.get(ch_name) is current_task
                owns_event = self.loop_events.get(ch_name) is stop_event
                if owns_task:
                    self.loop_tasks.pop(ch_name, None)
                if owns_event:
                    self.loop_events.pop(ch_name, None)
                if (
                    owns_task
                    and owns_event
                    and not reset_requested
                ):
                    await self._finish_floor_loop_worker(
                        ch_name,
                        str(cmd["pattern"]),
                        last_batch_expires_at,
                    )
                logger.info(
                    "%s 通道循环波形结束：%s",
                    ch_name,
                    cmd["pattern"],
                )

        worker_task = asyncio.create_task(worker())
        self.loop_tasks[ch_name] = worker_task

        def reap_unstarted_or_finished_task(task: asyncio.Task) -> None:
            self._loop_reset_requests.discard(task)
            if self.loop_tasks.get(ch_name) is task:
                self.loop_tasks.pop(ch_name, None)
            if self.loop_events.get(ch_name) is stop_event:
                self.loop_events.pop(ch_name, None)

        worker_task.add_done_callback(reap_unstarted_or_finished_task)
        return last_batch_expires_at

    async def _finish_floor_loop_worker(
        self,
        channel: str,
        waveform: str,
        batch_expires_at: float,
    ) -> None:
        """Publish a stopped floor loop without overwriting a newer owner."""
        coordinator = self.output_coordinator
        while channel not in self.loop_tasks:
            confirmed = coordinator.confirmed(channel)
            if (
                confirmed.waveform != waveform
                or confirmed.waveform_mode != "loop"
            ):
                return
            expected_revision = coordinator.revision(channel)
            committed_revision = await coordinator.confirm_loop_stopped(
                channel,
                waveform,
                expected_revision=expected_revision,
                batch_expires_at=batch_expires_at,
            )
            if committed_revision is None:
                continue
            if (
                channel in self.loop_tasks
                or coordinator.revision(channel) != committed_revision
            ):
                return
            self._publish_coordinator_confirmed(channel)
            confirmed = coordinator.confirmed(channel)
            if confirmed.waveform_mode == "finite":
                self.safety.pulse_until[channel] = batch_expires_at
            return

    def _cancel_loops(
        self, ch_name: str | None, *, reset_pulse: bool = True
    ) -> None:
        """取消循环波形；ch_name=None 时取消全部。"""
        names = (
            [ch_name]
            if ch_name
            else list(dict.fromkeys((*self.loop_events, *self.loop_tasks)))
        )
        for name in names:
            event = self.loop_events.get(name)
            if event:
                event.set()
            task = self.loop_tasks.get(name)
            if task:
                if reset_pulse:
                    self._loop_reset_requests.add(task)
                task.cancel()
            elif self.loop_events.get(name) is event:
                self.loop_events.pop(name, None)
        if reset_pulse and ch_name and ch_name in self.safety.pulse_until:
            self.safety.pulse_until[ch_name] = 0.0

    async def _send_all(self, frames: list[dict]) -> list[bool]:
        return [await self.relay.send_frame(f) for f in frames]

    @staticmethod
    def _describe(cmd: dict) -> str:
        kind = cmd["kind"]
        ch = cmd.get("channel")
        if kind == "temp":
            return f"{ch} 爆发 {cmd['value']} × {cmd['duration_s']:.1f}s（结束归零）"
        if kind == "hold":
            return f"{ch} 持续强度 {cmd['value']}（保持）"
        if kind == "add":
            return f"{ch} 增减 {cmd['delta']:+d}"
        if kind == "pulse":
            return f"{ch} 波形「{cmd['pattern']}」× {cmd['duration_s']:.1f}s"
        if kind == "pulse_hold":
            return f"{ch} 持续波形「{cmd['pattern']}」（循环）"
        if kind == "pulse_cycle":
            return f"{ch} 波形「{cmd['pattern']}」单周期 {cmd['duration_ms']}ms"
        if kind == "clear":
            return "清除全部" if ch is None else f"清除 {ch} 通道"
        return "急停清零"

    # ---------- 急停 / 恢复 ----------
    async def estop(self) -> dict:
        # Latch policy and coordinator priority synchronously, before waiting on
        # any active normal-output callback.
        self.safety.estop()
        for channel in ("A", "B"):
            self._publish_coordinator_confirmed(channel)
            self.output_coordinator.invalidate(
                channel, OutputIntentKind.ESTOP
            )
        self._cancel_loops(None, reset_pulse=False)
        client_id = self.relay.first_client_id()
        slot_id = self.relay.get_slot_id()
        transaction = await self._run_clear_transaction(
            cmd={"kind": "stop", "channel": None},
            intent=OutputIntentKind.ESTOP,
            client_id=client_id,
            slot_id=slot_id,
            ready=bool(client_id and slot_id),
            dry_run=self.safety.dry_run,
        )
        async with self._autopilot_transition_lock:
            try:
                if self.timeline_session is not None:
                    await self._await_timeline_lifecycle(
                        self.timeline_session.stop
                    )
                    self._clear_timeline_character_if_idle()
            finally:
                await self._stop_autopilot_task()
        sent = bool(transaction["sent"])
        if (
            not sent
            and not self.safety.dry_run
            and self._global_clear_is_confirmed()
        ):
            sent = True
        return {"estop": True, "sent": sent}

    async def resume(self) -> dict:
        if not self.safety.estop_active:
            return {"estop": False}
        if not self._global_clear_is_confirmed():
            await self.clear_output()
        if not self._global_clear_is_confirmed():
            raise DeviceOutputError(
                "estop clear was not confirmed",
                status_code=503,
            )
        released = await self.output_coordinator.release_estop()
        if not released:
            raise DeviceOutputError(
                "estop latch release requires confirmed clear",
                status_code=503,
            )
        self.safety.resume()
        return {"estop": False}

    async def on_client_disconnected(self) -> None:
        """APP 断开：stale owners now; publish clear only after transport."""
        self.require_output_clear(("A", "B"))
        self._cancel_loops(None, reset_pulse=False)
        async with self._autopilot_transition_lock:
            try:
                session_active = False
                if self.timeline_session is not None:
                    session_active = (
                        self.timeline_session.to_state().status.value != "idle"
                    )
                    await self._await_timeline_lifecycle(
                        self.timeline_session.on_disconnect
                    )
                if not session_active:
                    await self.clear_output()
            finally:
                await self._stop_autopilot_task()

    # ---------- 设备反馈 ----------
    async def handle_feedback(self, action: int, client_id: str) -> None:
        """APP 反馈按钮（custom.action 0-9）。"""
        self.add_note(f"玩家按下了反馈按钮 {action}")

    def add_note(self, note: str) -> None:
        """实时信号（反馈按钮/麦克风转写等）注入下一轮 AI 上下文。"""
        self.notes.append(note)
        self.notes = self.notes[-5:]
