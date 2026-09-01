# Task 5 报告：小说会话与存档生命周期

日期：2026-09-01

## 结果

实现 `NovelSessionController.start(plan, story, story_map)`、`pause()`、`resume(from_)`、`finish()`、`abort()` / `on_disconnect()` 和不可变 `NovelSessionState`。小说层只保存故事、章节、阅读位置、计划和分析身份；计划播放、A/B runner、clear、停止、完成存档与 exact replay 均由注入的同一个 `SessionController` 持有。

新增 `SessionController.start_planned(...)` / `resume_planned(cursor)` 作为最小 public adapter。它复用既有 `_lock`、`_turn_lock`、`_RunnerExecutor`、`ChannelCycleRunner`、输出 generation 与 GameLoop clear/execute 路径；没有创建第二个 player、relay、output coordinator 或 safety 路径。`mode="novel"` 会拒绝即时 chat action 注入，GameLoop 的手动接管仍先通过同一 session pause/clear。

章节计划完成验证后自动运行；事件按 Task 4 已解析的 `PlotEvent` 播放，不二次调用 resolver。章节末尾按 `chapter_duration_ms` clear 并进入 paused，不提前写 archive；正常显式 `finish()` 才保存。异常 abort / disconnect clear 后回 idle 且不保存。

## TDD 红绿证据

初始 RED（生产实现前）：

```text
D:\AI-for-Coyote\.venv\Scripts\python.exe -m unittest tests.test_novel_session -v
ModuleNotFoundError: No module named 'backend.story.session'
FAILED (errors=1)
```

Novel archive metadata RED：

```text
ReplayManifest.__init__() got an unexpected keyword argument 'metadata'
Ran 2 tests, FAILED (errors=2)
```

后续边界 RED：

```text
resume current 到 keep scene：runner unexpectedly None
底层 SessionController 直接 disconnect：NovelSessionStatus.RUNNING != IDLE
章节时长结束：SessionStatus.RUNNING != PAUSED
```

对应 GREEN：

```text
tests.test_novel_session: Ran 11 tests, OK
novel + replay_store + timeline_session: Ran 78 tests, OK
python -X dev -W error -m unittest tests.test_novel_session -v:
Ran 11 tests, OK
```

测试覆盖：唯一 planning → validated → running 启动路径、非法计划在任何输出/存档前拒绝、自动首场、立即 pause clear 与 cursor 保留、current/chapter_start/beginning 三种恢复、`keep` 场景恢复时物化此前 SET、章节末尾 clear、chat 不改变计划、disconnect 异常终止且无历史、源 bytes/scenes/metadata 存档、exact replay 零 planner/resolver 调用。

## Ownership 证明

- `backend/story/session.py` 不导入或构造 `TimelinePlayer`、`RecordedCyclePlayer`、`GameLoop`、relay 或 coordinator，只保存构造时注入的 `SessionController`；测试断言对象身份相同。
- novel running 时 `SessionController.player is None`，且 Task 4 resolver spy 为零调用；已验证的 `PlotEvent` 直接进入现有 runner activation seam。
- 所有 set/keep/stop 最终仍走 `SessionController._RunnerExecutor -> GameLoop.execute_timeline_actions/execute_actions -> SafetyManager`；pause/resume/reposition/finish/abort 都走 SessionController public lifecycle。
- `resume_planned()` 先调用权威全局 clear，再清理 retained runner、重建安全 cursor 状态并启动 scheduler；current scene 为 `keep` 时从计划前缀恢复最新有效 SET/STOP 状态。
- SessionController 自身收到 disconnect 时，novel mode 使用同一 clear/stop 锁路径异常终止；Novel 状态从权威 session 状态同步回 idle。
- exact replay 仍只由 SessionController 已有 `start_replay()` 和其唯一 `RecordedCyclePlayer` 执行，不接触 ChapterPlanner 或 resolver。

## 存档约束

Novel manifest 使用 `mode="novel"` 和严格 metadata：

- `content_type`
- `chapter_id`
- `speed`
- `source_encoding`
- `source_text_hash`
- `analysis_version`
- `dlc_version`

Novel archive 必须恰有既有必需成员 `manifest.json`、`timeline.json`，再加一个允许的 `source.txt|md|docx` 与 `scenes.json`。复用 ReplayStore 的成员路径、重复成员、压缩方法、单成员/总大小、外层 archive 大小、原子替换和完整 checksum inventory 校验；source 原始 bytes 与 scenes bytes 均有 SHA-256。Novel claim 缺 source/scenes、metadata 缺失/额外、非法 speed/encoding/hash 或 DLC 不一致均 fail closed。非 novel archive 的旧成员组合与 `metadata=None` round-trip 保持兼容。

## 修改文件

- 新增 `backend/story/session.py`
- 新增 `tests/test_novel_session.py`
- 修改 `backend/story/__init__.py`
- 修改 `backend/timeline/session.py`
- 修改 `backend/timeline/replay_store.py`
- 修改 `backend/timeline/models.py`（向后兼容的可选 manifest metadata 与 novel SessionState mode）
- 修改 `backend/game_loop.py`（novel 手动接管也先走同一 pause/clear）
- 修改 `tests/test_replay_store.py`
- 新增本报告

## 最终验证

```text
focused Task 5 gate: Ran 78 tests in 3.285s, OK
full discovery: Ran 480 tests in 40.000s, OK (skipped=5)
python -X dev -W error novel tests: Ran 11 tests in 0.808s, OK
python -m compileall -q backend tests: exit 0
git diff --check: exit 0（仅 Git 的 LF/CRLF 工作树提示）
```

## 提交

提交信息：`feat: run and archive offline novel sessions`。最终 commit hash 在提交完成后由 `git rev-parse HEAD` 生成并在交付消息中报告；提交无法在自身内容中稳定自引用最终 hash。

## 风险与边界

- MVP2 一次只运行一个 `ValidatedChapterPlan`；因此 `chapter_start` 与 `beginning` 在本任务都安全映射到该计划 cursor 0。未来跨章节连续队列需要新的已批准计划模型，不能在此层猜测未规划章节。
- 章节自然结束只 clear/paused，不自动写 archive；显式 `finish()` 才定义“正常完成并永久保存”，abort/disconnect 永不保存。
- `source_encoding` 保存调用方导入时的权威选项；Task 6 组装 API 时必须传入该次导入的实际选项。
- 新 manifest metadata 是可选字段，legacy/non-novel bundle 保持读取和保存兼容；`mode="novel"` 才启用严格新约束。
- 未联网、未调用真实模型、未连接真实设备、未推送、未打标签；真实设备验收仍属于 MVP2 后续 release gate。

## Fix round 1/5（3 Important）

### 修复结果

- 所有 `NovelSessionController` public lifecycle（start/pause/resume/finish/abort/on_disconnect）现在都在 `finally` 中读取同一个 `SessionController.to_state()`，按 `mode + status + session_id` 协调 reader phase 和 retained plan。`running`、`paused`、`finishing` 保留同一物理 novel session 的计划；权威 idle、不同 mode 或不同 session ID 立即清空。`CancelledError` 原样向调用方传播。
- estop 拒绝 resume 时底层仍是同一 paused novel，上层恢复 paused 并可在 estop 解除后重试；clear 失败时上下层共同保持 finishing，权威 stop 成功后上层清空。取消发生在底层 start/pause/resume/finish/abort/disconnect 已完成之后时，上层也以物理结果为准，而不是卡在本地过渡状态。
- runner 自己检测 `device disconnected` 时，novel 分支改走现有 `_abort_live_locked("disconnect")`，因此 clear、清 plan、回 idle、不保存；显式 disconnect 与 watcher 并发时仍收敛到一次 clear 且无 scheduler/runner watcher 泄漏。autopilot 分支仍走原 pause 路径。
- 新增纯 `backend.story.story_map_codec`，encode/decode 共用 `StoryMap` / `StoryChapter` / `StoryScene` 模型不变量；ReplayStore 仅在运行时局部导入该纯 codec，避免 timeline/story 初始化环。
- novel `scenes.json` 在 save 和 checksum 验证后的 load 都执行 exact-key root/chapter/scene schema、重复 JSON key、UTF-8、稳定 ID、连续 index、完整 offset partition、非空 summary、pace `0.25..4.0` 校验；同时要求 `source_hash == metadata.source_text_hash` 且 `metadata.chapter_id` 精确命中一个 chapter。checksummed 恶意 payload、递归 payload、hash/chapter mismatch 均 fail closed；legacy/non-novel scenes 继续使用旧兼容校验。

### 本轮 TDD 红绿

初始 lifecycle/runner/archive RED：

```text
novel + replay_store: Ran 51 tests
FAILED (failures=15, errors=1)

代表性失败：
cancelled resume / pause / finish / abort -> NovelSessionStatus.FINISHING（期望权威 paused/idle/running）
runner detected disconnect -> SessionStatus.PAUSED（期望 IDLE）
checksummed malicious scenes -> ReplayStoreError not raised
non-UTF-8 scenes save -> raw UnicodeEncodeError
```

追加攻击面 RED：

```text
duplicate scenes key -> ReplayStoreError not raised
direct retained-plan read after authoritative disconnect -> plan remained non-None
non-ASCII forged source identity -> raw TypeError from compare_digest
deep recursive scenes JSON -> raw RecursionError
```

对应 GREEN：

```text
python -X dev -W error focused novel/replay/timeline/player/models/game-loop:
Ran 210 tests in 22.409s, OK
full discovery: Ran 494 tests in 65.228s, OK (skipped=5)
python -m compileall -q backend tests: exit 0
fresh replay-store-first + story-session import-order smoke: OK
git diff --check: exit 0（仅 Git 的 LF/CRLF 工作树提示）
```

### Ownership 与测试证明

- novel adapter 仍只调用注入的 `SessionController.start_planned/pause/resume_planned/finish/stop/on_disconnect` public API；没有新增 player、relay、coordinator、GameLoop 或锁。
- 新的 runner 断连分支仍在 `SessionController` 自己的 `_lock` 内复用现有异常 stop/clear/reset 路径；测试同时断言 physical idle、上层 plan 清除、无 archive、一次全局 clear、`runners == {}`、watcher 空、planned task 为 `None`。
- lifecycle cancellation 测试在真实底层 transition 完成后注入取消，断言既不吞 `CancelledError`，也不覆盖物理状态；estop/clear-failure/authoritative replacement/stop 分别覆盖 paused、finishing、不同 session 和 idle 协调。
- replay tests 使用完整手写 StoryMap JSON，不以 codec 自己生成 expected；save/load 恶意样本重新计算 `scenes.json` checksum，证明拒绝原因是语义 schema/identity，而非 checksum 偶然失败。

### 本轮提交与风险

本轮提交信息计划为 `fix: reconcile novel lifecycle and archives`；最终 hash 由交付消息报告。

保留边界：短暂设备断连会终止当前 novel chapter，并要求重新 planning；这是 ledger 明确接受的安全成本。没有联网、模型调用、真实设备、push 或 tag。真实设备验收仍留给 MVP2 release gate。
