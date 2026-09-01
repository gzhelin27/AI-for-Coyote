# Task 6 报告：离线小说状态与运行 API

日期：2026-09-01

## 结果

`AppState` 现在只构造一个进程生命周期内的 `ChapterPlanner` 和一个 `NovelSessionController`。planner 注入当前有效结构化 LLM client/model、`story.analysis_prompt_version`、DLC provenance、A/B effective caps、SafetyManager、waveform registry、reading speeds、cycle-gap policy 与固定 `±4` jitter；novel adapter 注入现有同一个 `SessionController`。没有创建第二个 player、relay、SafetyManager、GameLoop、output coordinator 或物理输出 owner。

运行时来源导入只接受 multipart TXT/MD/DOCX 和 `auto|utf-8|gb18030`。每次导入分配不可预测、server-owned 的 opaque `source_id`，保存到配置的 repository-local story directory，并以内存映射持有已验证 `ImportedStory`、实际 encoding 选项和内部 storage path；任何 HTTP/WebSocket payload 都不序列化 path 或原文。`NovelSessionController.start(..., source_encoding=...)` 使用本次导入记录覆盖构造默认值，使 archive metadata 保留权威 encoding 选项。

只有 chapter `play` 路由调用 `ChapterPlanner.plan()`，且只传选中的 chapter ID。import、analysis status、chapters、reader 和 reader text 都只调用本地 source loader / `offline_analysis_key()` / `AnalysisStore.inspect()`；`missing`、`invalid` 不进入 planner。`play/pause/resume/finish` 只在既有 `timeline_transition_lock` 下调用 novel/session public lifecycle，没有增加锁层或重复输出 ownership。

## HTTP / WebSocket 契约

实现的唯一 story HTTP surface：

```text
POST /api/story/import
GET  /api/story/{source_id}/analysis
GET  /api/story/{source_id}/chapters
POST /api/story/{source_id}/chapters/{chapter_id}/play
GET  /api/story/reader
GET  /api/story/reader/text?start=N&end=M
POST /api/story/pause
POST /api/story/resume
POST /api/story/finish
```

没有 `/api/story/.../analyze`、开始分析、重试分析或删除接口。

Public source summary：

```json
{
  "source_id": "opaque server token",
  "filename": "safe basename.txt",
  "extension": ".txt",
  "encoding": "utf-8",
  "hash_prefix": "12 hex chars",
  "text_length": 1234
}
```

Public analysis detail：

```json
{
  "status": "ready|missing|invalid",
  "hash_prefix": "12 hex chars",
  "analysis_version": "faithful-offline-v1",
  "dlc_version": "effective DLC provenance"
}
```

- import `200`：`{"source": SourceSummary, "analysis": AnalysisDetail}`；不会生成分析。
- analysis `200`：ready detail；missing 为 `409` + `code=analysis_missing`；首次发现并隔离 corrupt cache 为 `422` + `code=analysis_invalid`，下一次 inspect 为 missing。
- chapters `200`：source、ready analysis、ordered chapters；每章只含 chapter ID/index/title/summary/offsets/scene count，不含原文。missing/invalid 传播相同业务错误。
- play `200`：public novel session（status、hash prefix、filename、chapter/speed/cursor/current scene、reader bounds、progress），不暴露 plan seed、frame、source bytes 或内部 path。规划/模型失败为稳定 `chapter_plan_failed` 422，物理/transition 错误使用 story-specific 4xx/5xx code。
- reader `200`：source、ready analysis、public session。reader 与 reader text 均要求 ready offline cache；missing/invalid 传播相同 409/422，绝不调用模型。
- reader text `200`：`{"start", "end", "text_length", "text"}`；必须满足 `0 <= start <= end <= text_length` 且单次最多 8192 字符，否则 `400 code=reader_range_invalid`。
- pause/resume 返回 public novel session；resume body 为 `{"from":"current|chapter_start|beginning"}`；finish 返回 `{"replay": ReplaySummary, "session": idle-session}`。

`AppState.build_state()` 新增完整 `story` 子树：`selected_source`、`analysis`、ordered `chapters`、`session`。所有成功 story mutation 调用既有 `broadcast()`，因此与现有前端 WebSocket refresh-generation gate 走同一 full-state 路径。命名使用 `selected_source`，继续遵守既有全状态禁止原始 `source`/seed/frame/API key 的 redaction contract。

## TDD 红绿证据

初始 endpoint RED（生产实现前）：

```text
python -m unittest tests.test_story_endpoints -v
POST /api/story/import -> 404
GET /api/story/reader -> 404
AppState has no attribute ChapterPlanner
Ran 11 tests, FAILED
```

初始 GREEN：

```text
tests.test_story_endpoints: Ran 11 tests in 0.511s, OK
```

兼容性 RED / GREEN：

```text
旧 AppState fake 缺 structured-client model/interface -> 13 errors
full-state redaction 检出新增 key "source" -> 1 failure

补齐真实 LLM fake shape，并将 WS story key 固定为 selected_source 后：
targeted AppState + HTTP/WS redaction: Ran 22 tests, OK
```

安全与信任边界 RED / GREEN：

```text
ancestor junction storage redirect: import returned 200（期望 fail closed）
加入全祖先 reparse/symlink 检查后：Ran 1 test, OK

reader missing analysis: returned 200（spec 要求只消费 validated cache）
传播 analysis_missing/analysis_invalid 后：story endpoint Ran 13 tests, OK
```

测试覆盖 TXT/MD/DOCX、三种 encoding 选项、unsupported/oversize、opaque ID 和同名来源隔离、ancestor junction、ready/missing/invalid 与一次性 quarantine、章节列表、reader trust boundary/严格 offsets/8192 上限、play/pause/resume/finish、archive encoding、单 planner/session wiring、WebSocket full-state broadcast，以及非-play 路由零模型调用。

## Security 与状态隔离

- 客户端只提交 filename/bytes/encoding，从不提交或解析本地 path；filename 先由 `StorySourceLoader` basename/extension 校验。
- `source_id` 由 `secrets.token_urlsafe(18)` 生成，只查 server-owned dictionary；调用者字符串从不拼接到路径。
- 磁盘 target 只使用 opaque ID + loader-validated extension，以 exclusive create 打开，flush + fsync；冲突重试，失败删除 partial regular target。
- 配置目录必须是 project-root 下 repository-local path；初始化和每次写入都拒绝 symlink / Windows reparse ancestor，target resolved parent 必须等于已验证 storage root。测试使用无需管理员权限的 Windows junction 验证祖先重定向 fail closed。
- 各 source record 独立保存 normalized text/hash/encoding/path；同名文件得到不同 ID，unknown ID 为 404，任何 payload 不含 storage path 或 source excerpt。
- `AnalysisStore.inspect()` 是状态唯一事实来源；首次 corrupt=invalid 且隔离，后续 missing。为保证同一次 import response 与其 broadcast 看到一致的首次状态，只在该 broadcast 生命周期内复用同一个 lookup，随后恢复逐次 inspect 语义。
- reader 只有 ready cache 才能读取 bounded slice；chapters/reader/full state 永不把原文整体推入 WebSocket。
- lifecycle endpoint 仅使用既有 `timeline_transition_lock`；NovelSessionController/SessionController 的内部锁保持各自 ownership，没有从内部重新获取 AppState lock。

## 修改文件

- 修改 `backend/main.py`
- 修改 `backend/story/session.py`（向后兼容地允许 start-time source encoding override）
- 新增 `tests/test_story_endpoints.py`
- 修改 `tests/test_app_state_timeline.py`（既有 LLM fake 补齐真实 structured interface）
- 新增本报告

## 最终验证

最终交付前执行：

```text
focused story endpoint gate: Ran 13 tests, OK
full discovery: Ran 507 tests, OK (platform skips=5)
python -m compileall -q backend tests: exit 0
git diff --check: exit 0（仅 Git 的 LF/CRLF 工作树提示）
```

全程未运行 `tests/probe_llm.py`，未联网、未调用真实模型、未连接真实设备、未 push/tag。

## 提交

提交信息：`feat: expose offline novel runtime APIs`。最终 commit hash 在提交完成后由 `git rev-parse HEAD` 生成并在交付消息报告；commit 无法在自身内容中稳定自引用最终 hash。

## 风险与边界

- Opaque source registry 是当前 AppState 进程内索引；原始文件保留在 server-owned import directory，但重启后不自动枚举或恢复旧 source ID。MVP2 没有批准持久 source catalog 或删除 API。
- 单个 AppState planner 按批准裁决冻结构造时的 model/prompt/DLC/waveform/caps identity，保证 process-local intent cache identity稳定。运行时修改这些配置后，应重启 AppState 再开始新 novel planning；本任务没有悄悄替换第二个 planner。
- 每个 source 受 `max_source_mb` 限制，reader slice 受 8192 字符限制；MVP2 未增加 source 总数/总磁盘配额或清理 UI。
- HTTP reader 暴露的是用户已导入且 ready 的本地原文 bounded slice；WebSocket/full state 永远不包含正文。
- 真实设备 acceptance、前端消费、跨重启 source catalog 与 operator 文档分别属于后续 Task 7/8 或显式新范围。
