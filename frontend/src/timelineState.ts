import type { TimelineSessionState } from "./types";

/** States whose device/replay progress can change without a route transition. */
export function isTimelineStateActive(
  state: Pick<TimelineSessionState, "status"> | null | undefined,
): boolean {
  return state?.status === "running" || state?.status === "paused" || state?.status === "replaying";
}
