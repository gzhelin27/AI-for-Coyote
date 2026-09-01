import { useEffect, useState } from "react";
import { api } from "../api";
import { refreshAppState } from "../stateRefresh";
import { useApp } from "../store";
import type { ReaderTextSlice, StoryChapterSummary } from "../types";

const speeds = [
  { value: "slow", label: "慢速" },
  { value: "standard", label: "标准" },
  { value: "fast", label: "快速" },
] as const;

export default function NovelReader() {
  const state = useApp((item) => item.state);
  const story = state?.story;
  const [chapterId, setChapterId] = useState("");
  const [speed, setSpeed] = useState<"slow" | "standard" | "fast">("standard");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [slice, setSlice] = useState<ReaderTextSlice | null>(null);
  const session = story?.session;

  useEffect(() => {
    if (story?.chapters.length && !story.chapters.some((chapter) => chapter.chapter_id === chapterId)) {
      setChapterId(story.chapters[0].chapter_id);
    }
  }, [story?.chapters, chapterId]);

  useEffect(() => {
    if (
      !session ||
      (session.status !== "running" && session.status !== "paused") ||
      session.reader_start_offset === null ||
      session.reader_end_offset === null
    ) {
      setSlice(null);
      return;
    }
    const start = session.reader_start_offset;
    const end = Math.min(session.reader_end_offset, start + 8192);
    let cancelled = false;
    api.storyReaderText(start, end)
      .then((next) => { if (!cancelled) setSlice(next); })
      .catch(() => { if (!cancelled) setError("无法读取当前正文片段"); });
    return () => { cancelled = true; };
  }, [session?.status, session?.reader_start_offset, session?.reader_end_offset, session?.current_scene_id]);

  const run = async (action: () => Promise<unknown>) => {
    if (pending) return;
    setPending(true);
    setError("");
    try {
      await action();
      await refreshAppState();
    } catch {
      setError("小说操作未完成，请检查当前离线分析和会话状态。");
    } finally {
      setPending(false);
    }
  };

  if (!story?.selected_source) {
    return <ReaderShell><p className="text-sm text-muted">请先在左侧导入本地小说原文。</p></ReaderShell>;
  }
  if (story.analysis?.status !== "ready") {
    return <ReaderShell><p className="text-sm text-muted">离线分析就绪后才可选择章节并启动忠实阅读。</p></ReaderShell>;
  }

  const activeChapter = story.chapters.find((chapter) => chapter.chapter_id === (session?.chapter_id ?? chapterId));
  return (
    <ReaderShell>
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line pb-3">
        <div>
          <h1 className="text-base font-semibold text-text">{story.selected_source.filename}</h1>
          <p className="mt-1 text-[11px] text-muted">分析版本 {story.analysis.analysis_version} · 原文标识 {story.analysis.hash_prefix}</p>
        </div>
        <span className="rounded-md border border-line bg-ink3 px-2 py-1 text-[11px] text-ok">离线分析已验证</span>
      </header>

      {session?.status === "idle" && <StartControls chapters={story.chapters} chapterId={chapterId} speed={speed} pending={pending} onChapter={setChapterId} onSpeed={setSpeed} onStart={() => void run(() => api.storyPlay(story.selected_source!.source_id, chapterId, speed))} />}
      {session?.status === "planning" && <div className="mt-4 rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm text-warn">正在为所选章节生成并验证忠实播放计划…</div>}
      {(session?.status === "validated" || session?.status === "finishing") && <div className="mt-4 rounded-lg border border-line bg-ink2 p-3 text-sm text-muted">正在安全切换小说会话…</div>}
      {(session?.status === "running" || session?.status === "paused") && (
        <>
          <SessionStatus chapter={activeChapter} progress={session.progress} sceneId={session.current_scene_id} status={session.status} />
          <ReaderText slice={slice} />
          <ChannelPublicState current={state?.current} patterns={state?.patterns} />
          <div className="mt-3 flex flex-wrap gap-2">
            {session.status === "running" && <ActionButton disabled={pending} onClick={() => void run(() => api.storyPause())}>暂停</ActionButton>}
            {session.status === "paused" && (
              <>
                <ActionButton disabled={pending} onClick={() => void run(() => api.storyResume("current"))}>从当前位置继续</ActionButton>
                <ActionButton disabled={pending} onClick={() => void run(() => api.storyResume("chapter_start"))}>从本章开头继续</ActionButton>
                <ActionButton disabled={pending} onClick={() => void run(() => api.storyResume("beginning"))}>从全书开头继续</ActionButton>
              </>
            )}
            <ActionButton disabled={pending} danger onClick={() => void run(() => api.storyFinish())}>结束并保存</ActionButton>
          </div>
        </>
      )}
      {error && <p className="mt-3 text-[12px] text-bad">{error}</p>}
    </ReaderShell>
  );
}

function ReaderShell({ children }: { children: React.ReactNode }) {
  return <section className="mx-auto flex max-w-3xl flex-col gap-3 rounded-[14px] border border-line bg-panel p-4">{children}</section>;
}

function StartControls({ chapters, chapterId, speed, pending, onChapter, onSpeed, onStart }: { chapters: StoryChapterSummary[]; chapterId: string; speed: "slow" | "standard" | "fast"; pending: boolean; onChapter: (value: string) => void; onSpeed: (value: "slow" | "standard" | "fast") => void; onStart: () => void }) {
  return <div className="mt-2 grid gap-3 sm:grid-cols-2">
    <label className="text-[12px] text-muted">章节<select value={chapterId} onChange={(event) => onChapter(event.target.value)} className="mt-1 w-full rounded-md border border-line bg-ink3 px-2 py-2 text-text">{chapters.map((chapter) => <option key={chapter.chapter_id} value={chapter.chapter_id}>第 {chapter.index + 1} 章 {chapter.title || chapter.summary}</option>)}</select></label>
    <label className="text-[12px] text-muted">阅读速度<select value={speed} onChange={(event) => onSpeed(event.target.value as "slow" | "standard" | "fast")} className="mt-1 w-full rounded-md border border-line bg-ink3 px-2 py-2 text-text">{speeds.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
    <div className="sm:col-span-2"><ActionButton disabled={pending || !chapterId} onClick={onStart}>{pending ? "正在启动…" : "生成计划并开始忠实阅读"}</ActionButton></div>
  </div>;
}

function SessionStatus({ chapter, progress, sceneId, status }: { chapter: StoryChapterSummary | undefined; progress: number; sceneId: string | null; status: "running" | "paused" }) {
  const percent = Math.max(0, Math.min(100, Math.round(progress * 100)));
  return <div className="mt-3 rounded-lg border border-line bg-ink2 p-3"><div className="flex flex-wrap justify-between gap-2 text-[12px]"><span>{status === "paused" ? "阅读已暂停" : "忠实阅读中"}</span><span className="text-muted">{chapter ? `第 ${chapter.index + 1} 章` : "章节加载中"} · {percent}%</span></div><div className="mt-2 h-1.5 overflow-hidden rounded bg-ink3"><div className="h-full bg-accent" style={{ width: `${percent}%` }} /></div><p className="mt-2 text-[11px] text-muted">当前场景：{sceneId ?? "等待场景"}</p></div>;
}

function ReaderText({ slice }: { slice: ReaderTextSlice | null }) {
  if (!slice) return <div className="mt-3 rounded-lg border border-line bg-ink2 p-3 text-sm text-muted">正在加载当前原文片段…</div>;
  return <article className="reader-text mt-3 rounded-lg border border-line bg-ink2 p-4 text-[14px] leading-7 text-text"><p className="mb-2 text-[10px] text-faint">原文片段 {slice.start}–{slice.end} / {slice.text_length}</p>{slice.text}</article>;
}

function ChannelPublicState({ current, patterns }: { current: Record<"A" | "B", number> | undefined; patterns: Record<"A" | "B", string | null> | undefined }) {
  return <div className="mt-3 grid grid-cols-2 gap-2">{(["A", "B"] as const).map((channel) => <div key={channel} className="rounded-lg border border-line bg-ink2 p-2 text-[12px]"><div className="font-semibold text-accent">通道 {channel}</div><div className="mt-1 text-muted">{patterns?.[channel] ?? "无波形"} · 强度 {current?.[channel] ?? 0}</div></div>)}</div>;
}

function ActionButton({ children, disabled, danger, onClick }: { children: React.ReactNode; disabled?: boolean; danger?: boolean; onClick: () => void }) {
  return <button type="button" disabled={disabled} onClick={onClick} className={`rounded-md px-3 py-2 text-[12px] font-semibold disabled:opacity-45 ${danger ? "bg-bad text-white" : "bg-accent text-ink"}`}>{children}</button>;
}
