# Task 4 报告：完整章节忠实规划器

日期：2026-09-01

## 结果

实现 `ChapterPlanner.plan(story, story_map, chapter_id, *, speed, seed)`，针对一个已验证章节发起恰好一次注入的结构化请求，严格验证完整场景 A/B `keep|set|stop` 意图，再由 MVP1 `TimelineResolver` 统一解析波形与 `±4` 强度抖动。规划只返回不可变的 `ValidatedChapterPlan` / `ChapterTimelineRequest`，不创建 player、device 或输出帧。

模型请求仅包含：所选章节正文、按原顺序排列的 scene ID/offset/summary、A/B 有效 cap 与允许模式、忠实约束。没有整本正文、其它章节、聊天、相机或麦克风状态，也没有 chunk、fallback 或 retry。

## TDD 红绿证据

RED（生产实现前）：

```text
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_story_planner -v
ModuleNotFoundError: No module named 'backend.story.planner'
FAILED (errors=1)
```

GREEN（最小实现后，后续测试重构后再次新鲜执行）：

```text
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_story_planner -v
Ran 8 tests in 0.130s
OK
```

测试覆盖：所选章节请求隔离、场景顺序与全覆盖、scene pace/CPM 时序、同 seed 确定性、不同 seed 仅允许解析随机字段变化、仅 registry 波形、双通道三种 mode、base strength cap、严格额外/缺失/乱序 schema 拒绝、模型/时序/安全 typed error、无 partial plan、结构化 LLM 单次请求且畸形输出不重试。

## 接口

- `ChapterPlanner(...)` 注入：structured client、waveform registry、A/B effective caps、三档 CPM、`CycleGapPolicy`、现有 safety adapter。
- `await ChapterPlanner.plan(...) -> ValidatedChapterPlan`。
- `ChapterPlanError` 统一承载模型、schema、timing、resolver 与 safety 失败。
- `ValidatedChapterPlan`：`source_hash`、`chapter_id`、`speed`、`seed`、有序 `plot_events`、`chapter_duration_ms`、已 dry-validated `timeline_request`。
- `LLM.complete_json(system_prompt, user_content, schema_name)`：窄通用结构化 seam，恰好一次 HTTP 请求，无重试、无正文日志、无整本分析 fallback。

## 修改文件

- `backend/story/planner.py`（新增）
- `backend/llm.py`
- `tests/test_story_planner.py`（新增）
- `.superpowers/sdd/2026-09-01-mvp2-offline-faithful-novel-mode/task-4-report.md`（本报告）

未修改 `backend/timeline/models.py`；现有 `PlotEvent` 已足以表达共享事件，章节 duration/policy 保留在 story planner 的不可变 request 中。

## 验证

```text
focused planner + resolver + session: Ran 55 tests in 6.191s, OK
final full discovery: Ran 456 tests in 39.654s, OK (skipped=5)
python -m compileall -q backend tests: exit 0
git diff --check: exit 0（仅 Git 的 LF/CRLF 工作树提示）
```

计划文字中的 `tests.test_timeline_resolver` 在仓库中不存在；现有 MVP1 resolver 测试模块实际为 `tests.test_timeline_randomizer`，聚焦回归使用该模块。

## 提交

提交信息：`feat: plan faithful novel chapters offline`。最终 commit hash 由提交完成后的 `git rev-parse HEAD` 生成并在交付消息中报告；Git commit 无法在自身内容中稳定自引用最终 hash。

## 风险与边界

- `CycleGapPolicy` 随 timeline request 传给后续会话层；规划阶段不预采样 cycle gap，保持现有 cycle runner/seed stream 为唯一 gap 随机源。
- safety dry-validation 调用现有 `validate()`，不调用执行/记录/设备接口；运行时安全层仍为最终权威。
- 未联网、未调用真实模型、未调用真实设备、未推送。
- `requesting-code-review` 技能通常要求 reviewer subagent，但本 Task 明确禁止派生 subagent，因此未委派审查；改用本地 diff、聚焦回归、全量回归与 compileall 收尾。
