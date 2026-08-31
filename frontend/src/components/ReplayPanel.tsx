import { Download, Pause, Play, Save, Square } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { invalidateStateRefresh, refreshAppState } from "../stateRefresh";
import { useApp } from "../store";
import type { ReplayHistoryItem, TimelineSessionState } from "../types";

interface Props {
  historyOnly: boolean;
}

const emptyTimeline: TimelineSessionState = {
  sessionId: null,
  status: "idle",
  mode: null,
  cursor: 0,
  adjusted: false,
  channels: {
    A: { phase: "idle", pattern: null, strength: 0, cycleIndex: 0, nextCycleAtMs: null },
    B: { phase: "idle", pattern: null, strength: 0, cycleIndex: 0, nextCycleAtMs: null },
  },
};

const phaseLabel = {
  idle: "空闲",
  cycle: "播放",
  gap: "间隔",
  paused: "已暂停",
  stopped: "已停止",
} as const;

function formatDate(value: string | null): string {
  if (!value) return "日期未提供";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

export default function ReplayPanel({ historyOnly }: Props) {
  const timeline = useApp((state) => state.state?.timeline ?? emptyTimeline);
  const [history, setHistory] = useState<ReplayHistoryItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  const refreshState = async () => {
    await refreshAppState();
  };

  const refreshHistory = async () => {
    setHistoryLoading(true);
    try {
      setHistory(await api.replays());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "历史记录加载失败");
    } finally {
      setHistoryLoading(false);
    }
  };

  useEffect(() => {
    if (historyOnly) void refreshHistory();
    // `historyOnly` is the navigation boundary that should refresh completed replays.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyOnly]);

  const transition = (action: () => Promise<unknown>, refreshReplays = false) => async () => {
    if (pending) return;
    setPending(true);
    setError("");
    invalidateStateRefresh();
    try {
      await action();
      await refreshState();
      if (refreshReplays) await refreshHistory();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "操作失败");
    } finally {
      setPending(false);
    }
  };

  const isReplay = timeline.mode === "replay";
  const isPlaybackPaused = isReplay && timeline.status === "paused";
  const controls =
    timeline.status === "idle" ? (
      <ActionButton onClick={transition(api.timelineStart)} disabled={pending} icon={<Play size={15} />}>
        开始自动运行
      </ActionButton>
    ) : timeline.status === "finishing" ? (
      <ActionButton disabled icon={<Save size={15} />}>保存中…</ActionButton>
    ) : isPlaybackPaused ? (
      <>
        <ActionButton onClick={transition(api.replayResume)} disabled={pending} icon={<Play size={15} />}>
          继续重放
        </ActionButton>
        <ActionButton onClick={transition(api.replayStop)} disabled={pending} variant="quiet" icon={<Square size={14} />}>
          停止重放
        </ActionButton>
      </>
    ) : timeline.status === "replaying" ? (
      <>
        <ActionButton onClick={transition(api.replayPause)} disabled={pending} icon={<Pause size={15} />}>
          暂停重放
        </ActionButton>
        <ActionButton onClick={transition(api.replayStop)} disabled={pending} variant="quiet" icon={<Square size={14} />}>
          停止重放
        </ActionButton>
      </>
    ) : timeline.status === "paused" ? (
      <>
        <ActionButton onClick={transition(api.timelineResume)} disabled={pending} icon={<Play size={15} />}>
          继续
        </ActionButton>
        <ActionButton onClick={transition(api.timelineFinish, true)} disabled={pending} variant="quiet" icon={<Save size={14} />}>
          结束并保存
        </ActionButton>
      </>
    ) : (
      <>
        <ActionButton onClick={transition(api.timelinePause)} disabled={pending} icon={<Pause size={15} />}>
          暂停
        </ActionButton>
        <ActionButton onClick={transition(api.timelineFinish, true)} disabled={pending} variant="quiet" icon={<Save size={14} />}>
          结束并保存
        </ActionButton>
      </>
    );

  if (historyOnly) {
    return (
      <section className="mx-auto w-full max-w-4xl">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold text-text">历史</h1>
            <p className="mt-1 text-xs text-muted">已完成的会话可精确重放或下载归档。</p>
          </div>
          <button
            onClick={() => void refreshHistory()}
            disabled={historyLoading || pending}
            className="rounded-lg border border-line bg-panel2 px-3 py-2 text-xs text-muted transition-colors hover:border-line2 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
          >
            {historyLoading ? "刷新中…" : "刷新"}
          </button>
        </div>
        <div className="overflow-hidden rounded-xl border border-line bg-panel">
          {historyLoading && history.length === 0 ? (
            <div className="p-5 text-sm text-muted">正在读取历史记录…</div>
          ) : history.length === 0 ? (
            <div className="p-5 text-sm text-muted">暂时没有已保存的会话。</div>
          ) : (
            <div className="divide-y divide-line">
              {history.map((item) => (
                <article key={item.replayId} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-text">{item.title}</div>
                    <div className="mt-1 text-xs text-muted">{formatDate(item.completedAt)} · DLC：{item.dlc}</div>
                    <div className="mt-1.5 flex flex-wrap gap-1.5 text-[11px]">
                      <span className="rounded-md border border-line bg-ink3 px-2 py-0.5 text-muted">循环数：{item.cycleCount}</span>
                      <span className={`rounded-md border px-2 py-0.5 ${item.exact ? "border-line2 bg-accent/10 text-accent" : "border-warn/50 bg-warn/10 text-warn"}`}>
                        {item.exact ? "已完成 · 精确" : "已完成 · 已调整"}
                      </span>
                    </div>
                  </div>
                  <div className="flex flex-none gap-2">
                    <ActionButton onClick={transition(() => api.replayPlay(item.replayId))} disabled={pending} icon={<Play size={14} />}>
                      重放
                    </ActionButton>
                    <a
                      href={api.replayDownloadUrl(item.replayId)}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel2 px-3 py-2 text-xs font-medium text-muted transition-colors hover:border-line2 hover:text-text"
                    >
                      <Download size={14} /> 下载
                    </a>
                  </div>
                </article>
              ))}
            </div>
          )}
        </div>
        {error && <div className="mt-3 rounded-lg border border-bad/50 bg-bad/10 px-3 py-2 text-xs text-bad">操作失败：{error}</div>}
      </section>
    );
  }

  return (
    <section className="rounded-xl border border-line bg-panel p-3.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-text">会话运行</div>
          <div className="mt-0.5 text-[11px] text-muted">
            {timeline.mode === "replay" ? "重放中" : timeline.mode === "autopilot" ? "自动运行" : "尚未开始"} · 位置 {timeline.cursor}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">{controls}</div>
      </div>
      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
        {(["A", "B"] as const).map((channel) => {
          const state = timeline.channels[channel];
          return (
            <div key={channel} className="rounded-lg border border-line bg-ink3 px-3 py-2.5">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold text-accent">通道 {channel}</span>
                <span className="text-[11px] text-muted">{phaseLabel[state.phase]}</span>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
                <span>波形：{state.pattern ?? "—"}</span>
                <span>强度：{state.strength}</span>
                <span>循环：{state.cycleIndex}</span>
              </div>
            </div>
          );
        })}
      </div>
      {error && <div className="mt-3 rounded-lg border border-bad/50 bg-bad/10 px-3 py-2 text-xs text-bad">操作失败：{error}</div>}
    </section>
  );
}

function ActionButton({
  children,
  disabled,
  icon,
  onClick,
  variant = "primary",
}: {
  children: React.ReactNode;
  disabled?: boolean;
  icon: React.ReactNode;
  onClick?: () => void;
  variant?: "primary" | "quiet";
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
        variant === "primary"
          ? "bg-accent text-ink hover:bg-[#fff0bd]"
          : "border border-line bg-panel2 text-muted hover:border-line2 hover:text-text"
      }`}
    >
      {icon}
      {children}
    </button>
  );
}
