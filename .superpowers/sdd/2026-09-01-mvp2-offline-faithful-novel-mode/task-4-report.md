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

## Fix round 1/5（2 Important）

### 修复内容

1. `ChapterPlanner` 新增显式非空 `model_identity`、`prompt_version`、`dlc_version` 构造身份，并拒绝与实际 structured client model 不一致或运行时漂移的身份。
2. planner 内保存 process-local validated-intent cache。key 包含 normalized source hash、chapter ID、effective model identity、planning prompt version、DLC provenance、排序后的 waveform capability set、A/B caps；明确不包含 timeline seed 或 reading speed。
3. concurrent same-key 调用共享一个 `asyncio.Task`，caller 通过 `asyncio.shield` 等待；caller 取消不会取消共享请求。成功写入 validated intent cache，模型/schema 失败清理 flight，下一次可重试。
4. `LLM.complete_json` 只审计有 256 KiB 原始字节上限且可严格 UTF-8 编码的 raw `content`/reasoning text；使用 `object_pairs_hook` 拒绝任意深度重复键。移除 `message.parsed` 信任路径，parsed-only provider 响应 fail closed。仍只有一次 HTTP 请求且没有 retry/fallback，chat 路径未改。

### TDD 证据

精确 RED（加入 constructor identity 后重新执行，排除 API 缺参噪声）：

```text
tests.test_story_planner
FAILED (failures=9, errors=1)
- alternating valid responses: 3 calls, expected 1
- concurrent same-key: 2 calls, expected 1
- cancelled waiter path: 3 calls, expected 1
- failed flight had no cache recovery
- duplicate scenes/channels/mode/base_strength were accepted
- parsed-only provider response was accepted
- oversized raw content was accepted
```

分步 GREEN：

```text
cache/single-flight/same-seed focused: Ran 5 tests, OK
duplicate/raw/parsed-only focused: Ran 5 tests, OK
identity mismatch RED: ValueError not raised
identity mismatch GREEN + planner: Ran 18 tests, OK
raw whitespace bound RED: StructuredResponseError not raised
raw whitespace bound GREEN + planner: Ran 18 tests, OK
```

### Fix-round 验证

```text
PYTHONASYNCIODEBUG=1 + -W error concurrency/cancel/failure: Ran 3 tests, OK
planner + timeline_randomizer + timeline_session: Ran 64 tests in 2.368s, OK
full discovery: Ran 465 tests in 37.275s, OK (skipped=5)
```

本轮没有联网、真实模型、设备、player、chunk/fallback、push；exact replay 的跨进程稳定性仍由后续 archive 路径负责，intent cache 按裁决只在当前 AppState-owned planner 进程内有效。

## Fix round 2/5（1 Important）

### 修复内容

1. `_IntentCacheKey` 新增 `request_fingerprint`。指纹以有界 canonical JSON（UTF-8、对象键排序、紧凑分隔、禁止 NaN）为输入，使用 SHA-256；不使用 Python process-randomized `hash()`。
2. 被指纹覆盖且发送给模型的同一 request document 现在显式包含 chapter ID/index/bounds/title/summary、精确所选章节正文，以及每个有序 scene ID/index/bounds/summary。原有 model/prompt/DLC/waveform/caps 身份仍保留，timeline seed 与 reading speed 仍明确排除。
3. canonical request 设置 128 MiB UTF-8 上限；该上限覆盖配置允许的 20 MiB 源文本在 JSON 控制字符最坏转义下的体积。序列化、Unicode、递归和尺寸失败全部映射为 `ChapterPlanError`，且发生在模型请求前。
4. resolver 前显式核对 scene/intent/timing 长度，并改为验证后按 index 读取，避免 `zip(strict=True)` 将内部错配泄漏为原始 `ValueError`；失败没有 partial plan 或输出。

### TDD 证据

RED（生产代码修改前）：

```text
focused 3 tests:
- request payload missing chapter metadata/index: FAIL
- same source hash/chapter ID with changed title, chapter summary/bounds/text,
  scene offsets/summary: 1 request instead of 2 (6 FAIL)
- changed scene count: leaked ValueError from zip(strict=True) (ERROR)
- injected internal intent length mismatch: leaked ValueError (ERROR)
Ran 3 tests, FAILED (failures=7, errors=2)
```

GREEN（最小实现后）：

```text
focused payload/fingerprint/typed-length tests: Ran 3 tests, OK
planner module: Ran 19 tests, OK
PYTHONASYNCIODEBUG=1 + -W error planner module: Ran 19 tests, OK
```

新增 request-aware legal fake 会根据本次请求的有序 scene ID 返回完整 A/B `keep` intent，因此 scene-count 变更可以验证确实建立新 flight，而不是依赖固定响应或泄漏长度错误。原 `test_validated_intent_is_cached_across_seed_and_speed` 继续证明完全相同 request 在不同 seed/speed 下只调用一次模型。

### Fix-round 验证

```text
full discovery: Ran 467 tests in 38.170s, OK (skipped=5)
python -m compileall -q backend tests: exit 0
git diff --check: exit 0（仅 Git 的 LF/CRLF 工作树提示）
```

修改文件：`backend/story/planner.py`、`tests/test_story_planner.py`、本报告。未修改 whole-book analyzer/fallback、player、device、timeline randomizer 或 chat 路径；未联网、未调用真实模型/设备、未推送。
