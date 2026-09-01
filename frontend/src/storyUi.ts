import type { StoryAnalysisStatus, StorySourceSummary } from "./types";

export type StoryResumeFrom = "current" | "chapter_start" | "beginning";

export function isStoryReady(status: StoryAnalysisStatus | null | undefined): boolean { return status === "ready"; }
export function resumePayload(from: StoryResumeFrom): { from: StoryResumeFrom } { return { from }; }
export function analysisStatusGuidance(status: StoryAnalysisStatus): string {
  return status === "ready" ? "离线分析已验证" : status === "missing" ? "尚未导入匹配的离线分析" : "离线分析无效，请重新生成并导入";
}
export function buildMissingAnalysisCommand(hashPrefix: string, encoding: StorySourceSummary["encoding"]): string {
  const safeHash = /^[a-f0-9]{12}$/.test(hashPrefix) ? hashPrefix : "unknown-hash";
  const safeEncoding = ["auto", "utf-8", "gb18030"].includes(encoding) ? encoding : "auto";
  return `$storyPath = Read-Host '请输入小说完整路径'\npython -m backend.story.import_analysis import --source "$storyPath" --map "data/story_candidates/${safeHash}.json" --encoding ${safeEncoding}`;
}
