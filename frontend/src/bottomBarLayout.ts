export function bottomBarOffsets(
  viewportWidth: number,
  sidebarWidth: number,
  controlWidth: number,
): { left: number; right: number } {
  return viewportWidth < 900
    ? { left: 0, right: 0 }
    : { left: sidebarWidth, right: controlWidth };
}
