import { useRef, useState } from "react";
import { api } from "../api";
import { refreshAppState } from "../stateRefresh";
import { useApp } from "../store";
import { analysisStatusGuidance, buildMissingAnalysisCommand } from "../storyUi";
import type { StorySourceSummary } from "../types";

const encodings: { value: StorySourceSummary["encoding"]; label: string }[] = [
  { value: "auto", label: "自动识别" },
  { value: "utf-8", label: "UTF-8" },
  { value: "gb18030", label: "GB18030" },
];

export default function NovelImport() {
  const story = useApp((state) => state.state?.story);
  const inputRef = useRef<HTMLInputElement>(null);
  const [encoding, setEncoding] = useState<StorySourceSummary["encoding"]>("auto");
  const [importing, setImporting] = useState(false);
  const [notice, setNotice] = useState("");

  const importStory = async () => {
    const file = inputRef.current?.files?.[0];
    if (!file || importing) return;
    setImporting(true);
    setNotice("");
    try {
      await api.storyImport(file, encoding);
      try {
        await refreshAppState();
        setNotice("原文已导入，本地分析状态已刷新。");
      } catch {
        setNotice("原文已导入，正在等待状态刷新。");
      }
    } catch {
      setNotice("小说导入失败，请确认文件格式和编码后重试。");
    } finally {
      setImporting(false);
    }
  };

  const source = story?.selected_source;
  const analysis = story?.analysis;
  return (
    <section className="mt-3 rounded-[14px] border border-line bg-panel p-3" aria-label="小说导入">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-[13px] font-semibold text-text">忠实小说模式</h2>
        {analysis && <StatusBadge status={analysis.status} />}
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-faint">仅本地 TXT / MD / DOCX；聊天不会改变原文剧情。</p>
      <input
        ref={inputRef}
        type="file"
        accept=".txt,.md,.docx,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        className="mt-2 block w-full text-[11px] text-muted file:mr-2 file:rounded-md file:border-0 file:bg-ink3 file:px-2 file:py-1 file:text-[11px] file:text-text"
      />
      <label className="mt-2 block text-[11px] text-muted">
        原文编码
        <select
          value={encoding}
          onChange={(event) => setEncoding(event.target.value as StorySourceSummary["encoding"])}
          className="mt-1 w-full rounded-md border border-line bg-ink3 px-2 py-1.5 text-[12px] text-text"
        >
          {encodings.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
      </label>
      <button
        type="button"
        onClick={() => void importStory()}
        disabled={importing}
        className="mt-2 w-full rounded-md bg-accent px-2 py-1.5 text-[12px] font-semibold text-ink disabled:opacity-45"
      >
        {importing ? "导入中…" : "导入本地原文"}
      </button>
      {source && (
        <div className="mt-2 border-t border-line pt-2 text-[11px] leading-relaxed text-muted">
          <div className="truncate text-text">{source.filename}</div>
          <div>原文标识：{source.hash_prefix} · {source.encoding}</div>
        </div>
      )}
      {analysis && <div className="mt-2 text-[10px] leading-relaxed text-faint">{analysisStatusGuidance(analysis.status)}<br />分析版本：{analysis.analysis_version}<br />DLC：{analysis.dlc_version}</div>}
      {analysis?.status === "missing" && <MissingAnalysis hashPrefix={analysis.hash_prefix} encoding={source?.encoding ?? encoding} />}
      {analysis?.status === "invalid" && <InvalidAnalysis hashPrefix={analysis.hash_prefix} />}
      {notice && <p className="mt-2 text-[11px] leading-relaxed text-warn">{notice}</p>}
    </section>
  );
}

function StatusBadge({ status }: { status: "ready" | "missing" | "invalid" }) {
  const text = status === "ready" ? "分析就绪" : status === "missing" ? "缺少分析" : "分析无效";
  const color = status === "ready" ? "text-ok" : status === "missing" ? "text-warn" : "text-bad";
  return <span className={`rounded-md border border-line bg-ink3 px-1.5 py-0.5 text-[10px] ${color}`}>{text}</span>;
}

function MissingAnalysis({ hashPrefix, encoding }: { hashPrefix: string; encoding: StorySourceSummary["encoding"] }) {
  return (
    <div className="mt-2 rounded-md border border-warn/40 bg-warn/10 p-2 text-[11px] leading-relaxed text-warn">
      <div>缺少原文 {hashPrefix} 的离线分析。</div>
      <div className="mt-1 text-muted">请让 Codex 基于本地原文生成候选 JSON，再在项目终端运行：</div>
      <div className="mt-1 text-muted">将候选保存为 <code className="text-text">data/story_candidates/{hashPrefix}.json</code>，再运行：</div>
      <pre className="mt-1 whitespace-pre-wrap break-all text-[10px] text-text">{buildMissingAnalysisCommand(hashPrefix, encoding)}</pre>
    </div>
  );
}

function InvalidAnalysis({ hashPrefix }: { hashPrefix: string }) {
  return (
    <div className="mt-2 rounded-md border border-bad/40 bg-bad/10 p-2 text-[11px] leading-relaxed text-bad">
      原文 {hashPrefix} 的离线分析无法安全读取。请隔离损坏缓存，重新由 Codex 生成候选 JSON 并运行本地导入命令。
    </div>
  );
}
