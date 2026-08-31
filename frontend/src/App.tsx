import { useEffect, useRef, useState } from "react";
import TopBar, { type ViewName } from "./components/TopBar";
import Sidebar from "./components/Sidebar";
import DeviceStatus from "./components/DeviceStatus";
import ChannelControl from "./components/ChannelControl";
import PresetPanel from "./components/PresetPanel";
import BottomBar from "./components/BottomBar";
import ChatPanel from "./components/ChatPanel";
import ReplayPanel from "./components/ReplayPanel";
import { PairView, SettingsView } from "./components/views";
import { api, mapFullState } from "./api";
import type { BackendFullState } from "./types";
import { useApp, useChat, useLayout } from "./store";
import { doEstop } from "./commands";
import { isTimelineStateActive } from "./timelineState";

/** 空格长按触发急停的时长（毫秒，与进度条动画同步） */
const ESTOP_HOLD_MS = 1000;
const ACTIVE_STATE_POLL_MS = 1000;

export default function App() {
  const [view, setView] = useState<ViewName>("control");
  const sidebarW = useLayout((s) => s.sidebarW);
  const controlW = useLayout((s) => s.controlW);
  const updateLayout = useLayout((s) => s.updateLayout);
  // 全局缩放：按窗口宽度缩放整页布局（0.8 ~ 1.3 倍）
  const [zoom, setZoom] = useState(1);
  const [compact, setCompact] = useState(false);
  useEffect(() => {
    const calc = () => {
      const isCompact = window.innerWidth < 900;
      setCompact(isCompact);
      setZoom(isCompact ? 1 : Math.min(1.3, Math.max(0.8, window.innerWidth / 1600)));
      updateLayout(); // 三栏按固定比例随窗口宽度重算
    };
    calc();
    window.addEventListener("resize", calc);
    return () => window.removeEventListener("resize", calc);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 上报三栏布局给后端（调试/监测用，300ms 节流）
  useEffect(() => {
    const t = window.setTimeout(() => {
      api
        .reportLayout({
          sidebar_w: sidebarW,
          control_w: controlW,
          inner_width: window.innerWidth,
          zoom,
        })
        .catch(() => {});
    }, 300);
    return () => window.clearTimeout(t);
  }, [sidebarW, controlW, zoom]);

  // 空格长按急停的进行中状态与计时器
  const [holding, setHolding] = useState(false);
  const holdTimer = useRef<number | null>(null);
  const cancelHold = () => {
    if (holdTimer.current !== null) {
      window.clearTimeout(holdTimer.current);
      holdTimer.current = null;
    }
    setHolding(false);
  };

  useEffect(() => {
    let pollTimer: number | null = null;
    let polling = false;
    let closed = false;
    const clearPollTimer = () => {
      if (pollTimer !== null) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
      }
    };
    const canPoll = () =>
      !closed &&
      document.visibilityState === "visible" &&
      isTimelineStateActive(useApp.getState().state?.timeline);
    const schedulePoll = () => {
      clearPollTimer();
      if (canPoll()) pollTimer = window.setTimeout(pollState, ACTIVE_STATE_POLL_MS);
    };
    const refreshState = async () => {
      const state = await api.state();
      useApp.getState().setState(state);
      if (state.config_info?.title) document.title = state.config_info.title;
      return state;
    };
    const pollState = async () => {
      pollTimer = null;
      if (!canPoll()) return;
      if (polling) {
        schedulePoll();
        return;
      }
      polling = true;
      try {
        await refreshState();
      } catch {
        // Polling is passive; transition errors remain visible in their initiating control.
      } finally {
        polling = false;
        schedulePoll();
      }
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") void pollState();
      else clearPollTimer();
    };

    // 初始状态
    refreshState().then(schedulePoll).catch(() => {});
    // WebSocket 实时同步
    let ws: WebSocket;
    const connect = () => {
      if (closed) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      ws = new WebSocket(proto + "//" + location.host + "/ws");
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") {
          useApp.getState().setState(mapFullState(msg.data as BackendFullState));
          schedulePoll();
        } else if (msg.type === "chat") {
          const extra: string[] = [];
          for (const e of msg.executed ?? []) extra.push("▶ " + e.label);
          for (const x of msg.dropped ?? []) extra.push("✖ " + x.reason);
          useChat.getState().push({ role: "ai", text: msg.line ?? "", actions: extra.join("\n") });
        }
      };
      ws.onclose = () => setTimeout(connect, 2000);
    };
    connect();
    document.addEventListener("visibilitychange", onVisibilityChange);
    // 空格长按 1s 急停（防误触：松手 / 窗口失焦即取消；已急停时不重复触发）
    const onKeyDown = (e: KeyboardEvent) => {
      const tag = (document.activeElement?.tagName ?? "").toUpperCase();
      if (e.code !== "Space" || ["INPUT", "TEXTAREA"].includes(tag)) return;
      e.preventDefault();
      if (e.repeat) return;
      if (useApp.getState().state?.estop) return;
      setHolding(true);
      holdTimer.current = window.setTimeout(() => {
        holdTimer.current = null;
        setHolding(false);
        void doEstop();
      }, ESTOP_HOLD_MS);
    };
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.code === "Space") cancelHold();
    };
    const onBlur = () => cancelHold();
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      closed = true;
      ws?.close();
      clearPollTimer();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
    };
  }, []);

  return (
    <div className="flex h-full flex-col" style={{ zoom }}>
      <TopBar view={view} onView={setView} />
      <div
        className={`grid min-h-0 flex-1 ${compact ? "grid-cols-1 overflow-y-auto" : ""}`}
        style={{ gridTemplateColumns: compact ? "minmax(0, 1fr)" : `${sidebarW}px 1fr ${controlW}px` }}
      >
        <Sidebar view={view} onView={setView} />
        <ChatPanel />
        <main className="min-h-0 overflow-y-auto border-l border-line px-4 pb-14 pt-3">
          {view === "control" && (
            <div className="flex h-full min-h-0 flex-col gap-2">
              <div className="min-h-0 flex-none">
                <DeviceStatus />
              </div>
              <ChannelControl />
              <PresetPanel />
              <ReplayPanel historyOnly={false} />
            </div>
          )}
          {view === "history" && <ReplayPanel historyOnly />}
          {view === "pair" && <PairView />}
          {view === "settings" && <SettingsView />}
        </main>
      </div>
      <BottomBar />
      {holding && (
        <div className="pointer-events-none fixed inset-x-0 bottom-20 z-50 flex justify-center">
          <div className="w-64 rounded-[10px] border border-line bg-panel2 px-4 py-3 shadow-lg">
            <div className="flex items-baseline justify-between">
              <span className="text-sm font-semibold text-text">「急停中…」</span>
              <span className="text-xs text-muted">松开取消</span>
            </div>
            <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-ink3">
              <div
                className="h-full rounded-full bg-bad"
                style={{
                  transformOrigin: "left",
                  animation: `estop-fill ${ESTOP_HOLD_MS}ms linear forwards`,
                }}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
