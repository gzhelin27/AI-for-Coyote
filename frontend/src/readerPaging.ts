export const READER_SLICE_MAX_CHARS = 8192;

export interface ReaderPageRange {
  start: number;
  end: number;
  hasPrevious: boolean;
  hasNext: boolean;
}

export function readerPageRange(sceneStart: number, sceneEnd: number, pageStart: number | null): ReaderPageRange {
  const safeStart = Math.max(0, sceneStart);
  const safeEnd = Math.max(safeStart, sceneEnd);
  const requested = pageStart === null ? safeStart : pageStart;
  const page = Math.max(safeStart, Math.min(requested, safeEnd - 1));
  const aligned = safeStart + Math.floor((page - safeStart) / READER_SLICE_MAX_CHARS) * READER_SLICE_MAX_CHARS;
  const end = Math.min(safeEnd, aligned + READER_SLICE_MAX_CHARS);
  return { start: aligned, end, hasPrevious: aligned > safeStart, hasNext: end < safeEnd };
}

export class ReaderSliceGate {
  private generation = 0;
  begin(_key: string): number { this.generation += 1; return this.generation; }
  isCurrent(token: number, _key: string): boolean { return token === this.generation; }
}
