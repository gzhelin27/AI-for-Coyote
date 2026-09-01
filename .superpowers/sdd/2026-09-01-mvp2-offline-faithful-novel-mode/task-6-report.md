# Task 6 报告：离线小说状态与运行 API

日期：2026-09-01

## 结果

`AppState` 现在只持有一个当前 `ChapterPlanner` 和一个 `NovelSessionController`。planner 按动态 runtime signature 注入当前有效结构化 LLM client/model、固定 prompt identity `faithful-offline-v1`、当前 DLC provenance、A/B effective caps、SafetyManager、waveform registry、reading speeds、cycle-gap policy 与配置 jitter；signature 在安全 idle 点变化时原子替换这一个 planner。novel adapter 注入现有同一个 `SessionController`。没有创建第二个 player、relay、SafetyManager、GameLoop、output coordinator 或物理输出 owner。

运行时来源导入只接受 multipart TXT/MD/DOCX 和 `auto|utf-8|gb18030`。每次导入分配不可预测、server-owned 的 opaque `source_id`，保存到配置的 repository-local story directory，并以内存映射持有已验证 `ImportedStory`、实际 encoding 选项和内部 storage path；任何 HTTP/WebSocket payload 都不序列化 path 或原文。`NovelSessionController.start(..., source_encoding=...)` 使用本次导入记录覆盖构造默认值，使 archive metadata 保留权威 encoding 选项。

只有 chapter `play` 路由调用 `ChapterPlanner.plan()`，且只传选中的 chapter ID。import、analysis status、chapters、reader 和 reader text 都只调用本地 source loader / `offline_analysis_key()` / `AnalysisStore.inspect()`；`missing`、`invalid` 不进入 planner。规划作为 AppState tracked task 在 `timeline_transition_lock` 外等待；完成后只在短锁内重验 source generation/selection、ready analysis identity、planner signature 和 authoritative idle，再调用 novel/session public lifecycle。没有增加输出 ownership。

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

`AppState.build_state()` 新增完整 `story` 子树：`selected_source`、`analysis`、ordered `chapters`、`session`，并在根状态发布单调 `state_revision`。full-state snapshot 与全部 send 在同一个 broadcast lock 内串行，所有成功 mutation 在 snapshot 前递增 revision，因此慢旧发送不能晚到覆盖新状态。命名使用 `selected_source`，继续遵守既有全状态禁止原始 `source`/seed/frame/API key 的 redaction contract。

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
- 磁盘 target 只使用 opaque ID + loader-validated extension；持久化层先 identity-pin trusted directory handle，再以 handle-relative、no-follow、exclusive create 建立同目录临时文件，write/flush/fsync 后以 pinned root 原子 rename，支持时 directory fsync。失败清理使用 pinned directory/已打开 child handle，不重新解析绝对路径。
- 配置目录必须是 project-root 下 repository-local path；root/symlink/junction 替换不能重定向写入。Windows 目录句柄拒绝 rename，POSIX 对 pinned identity/final path 变化 fail closed；fsync/rename 后异常测试均无 partial final。
- 各 source record 独立保存 normalized text/hash/encoding/path；同名文件得到不同 ID，unknown ID 为 404，任何 payload 不含 storage path 或 source excerpt。
- `AnalysisStore.inspect()` 是状态唯一事实来源；首次实际发现并隔离 corrupt 的调用得到 invalid，后续或并发调用得到 missing。没有跨请求共享 lookup，允许 import response 报 invalid 而紧随其后的 broadcast 已是 missing。
- reader 只有 ready cache 才能读取 bounded slice；chapters/reader/full state 永不把原文整体推入 WebSocket。
- lifecycle endpoint 仅使用既有 `timeline_transition_lock`；NovelSessionController/SessionController 的内部锁保持各自 ownership，没有从内部重新获取 AppState lock。

## 修改文件

- 修改 `backend/main.py`
- 修改 `backend/config.py`
- 修改 `backend/story/planner.py`（取消当前 planner 所有 shielded model flights）
- 修改 `backend/story/session.py`（向后兼容地允许 start-time source encoding/DLC snapshot override）
- 新增 `backend/story/source_store.py`
- 新增 `tests/test_story_endpoints.py`
- 新增 `tests/test_story_source_store.py`
- 修改 `tests/test_app_state_timeline.py`、`tests/test_session_endpoints.py`、`tests/test_story_source.py`
- 新增本报告

## 最终验证

最终交付前执行：

```text
focused story/source/planner/session gate: Ran 106 tests, OK
developer-mode ResourceWarning gate: Ran 37 tests, OK
full discovery: Ran 531 tests in 54.328s, OK (platform skips=5)
python -m compileall -q backend tests: exit 0
git diff --check: exit 0（仅 Git 的 LF/CRLF 工作树提示）
```

全程未运行 `tests/probe_llm.py`，未联网、未调用真实模型、未连接真实设备、未 push/tag。

## 提交

初始提交：`5cc2b51 feat: expose offline novel runtime APIs`。本轮提交信息计划为 `fix: harden offline story runtime APIs`；最终 hash 在提交完成后由 `git rev-parse HEAD` 生成并在交付消息报告。

## 风险与边界

- Opaque source registry 是当前 AppState 进程内索引；原始文件保留在 server-owned import directory，但重启后不自动枚举或恢复旧 source ID。MVP2 没有批准持久 source catalog 或删除 API。
- 单个 AppState planner 按当前 model/client/prompt/DLC/waveform/caps identity 缓存 intent；身份热变更会取消或拒绝并发规划，并在下一个安全 idle 点重建唯一 planner。旧 cache/client 不复用。
- 每个 source 受 `max_source_mb` 限制，reader slice 受 8192 字符限制；MVP2 未增加 source 总数/总磁盘配额或清理 UI。
- HTTP reader 暴露的是用户已导入且 ready 的本地原文 bounded slice；WebSocket/full state 永远不包含正文。
- 真实设备 acceptance、前端消费、跨重启 source catalog 与 operator 文档分别属于后续 Task 7/8 或显式新范围。

## Review fix round 1/5（2026-09-01）

### RED → GREEN

规划/生命周期并发 RED：blocking planner 使 disconnect 超时、shutdown 等待 transition lock、source 改变后旧结果仍可能 start、并发 play 无稳定冲突、import 会在 planning/running 时切换 selection。实现 tracked outer plan task + planner-owned inner-flight cancellation、锁外 await、锁内 identity/idle 重验和无隐式 finish/archive 后，6 个并发用例全绿；补充 cap 与 DLC 热变更竞态后，阻塞规划均能按 200ms 测试期限收敛且无 `story-plan-*` task leak。

广播/动态身份 RED：状态无 `state_revision`，阻塞首个 send 可使旧 snapshot 晚到；DLC/cap/LLM 变化仍复用旧 analysis/planner/client，默认 prompt 为 `faithful-v1`。实现单 broadcast lock、mutation-before-snapshot revision、动态 signature 与 hot-mutation reservation 后，blocked-first-send、LLM replace/close、cap、DLC cache-key 变化全部转绿。

source storage RED：安全 store 模块缺失；最初 Windows rename ABI 不支持 pinned-root relative rename，失败清理还会重新打开 absolute path。实现 POSIX dirfd 与 Windows `NtCreateFile`/`NtSetInformationFile` 的 root/child-handle 相对 create、rename、delete 后，root replacement、exclusive temp、file/dir fsync failure、post-rename cleanup 和无绝对路径重开共 5 个测试全绿。

analysis/error RED：import 首次 invalid 被 shared lookup 重放给 broadcast/GET；并发 corrupt GET 不满足单次 invalid；unexpected `RuntimeError|TypeError|ValueError` 会把内部 path/token 回显为 4xx。移除 published lookup，并仅枚举明确 business exceptions 后，import/concurrent quarantine 与 generic logged 500 测试全绿，所有非-play route 的 LLM spy 仍为零调用。

兼容回归 RED：旧 endpoint fake 缺少新 broadcast/planning state；sync AppState 测试未关闭 pinned directory handle。补齐真实 AppState fixture contract 和显式 handle cleanup 后，65 个既有 session endpoint 测试、60 个 planner/novel/AppState timeline 测试、37 个 timeline session 测试均恢复全绿。

### API schema 与状态隔离增量

- Full state 根字段新增 `state_revision: int`，从 0 起、每次 mutation broadcast 前递增；initial WebSocket send 使用当前 revision 且与 broadcast 同锁。
- Planning 期间 `story.session.status="planning"`，只公开 source hash prefix、safe filename、chapter ID 与 speed；不公开 prompt、plan、seed、原文或模型响应。
- 稳定冲突：`story_planning_active`、`story_runtime_busy`、`story_state_changed`、`story_planning_cancelled` 均为 409；unexpected transition 固定 `500 code=story_transition_failed`，内部异常只进日志。
- Import/source registry 保持进程内隔离；新的 source 在 novel/autopilot/replay/planning/runtime hot-change 任一活跃时不落盘、不注册、不切 selection、不归档。
- DLC/profile/LLM/cap 变化与 planning/session 通过同一 transition lock 和 runtime reservation 协调；每次 analysis/play 都重新计算当前 DLC provenance，play 只在 exact runtime signature 下消费 ready cache。

### 最终验证证据

```text
python -m unittest tests.test_story_endpoints tests.test_story_source_store \
  tests.test_story_source tests.test_story_planner tests.test_novel_session -q
Ran 106 tests in 8.188s, OK

python -X dev -W ignore::DeprecationWarning -W error::ResourceWarning \
  -m unittest tests.test_story_endpoints tests.test_story_source_store -q
Ran 37 tests in 3.282s, OK

python -m unittest tests.test_session_endpoints -q
Ran 65 tests in 17.017s, OK

python -m unittest discover -s tests -q
Ran 531 tests in 54.328s, OK (skipped=5)
```

`-W error` 未直接全开，因为仓库既有 FastAPI `on_event` 会产生已知 `DeprecationWarning`；本轮以仅提升 `ResourceWarning` 为 error 的开发模式门专门验证异步 task/handle 清理。全程无网络、真实模型、真实设备或 push。

### 剩余边界

- Windows 安全存储使用 NT native handle-relative primitives；POSIX 分支依赖 `dir_fd + O_NOFOLLOW`，缺失这些能力的平台按裁决 fail closed。当前 CI 主机验证 Windows 分支；5 个全套平台 skip 为仓库既有/平台条件测试。
- Pinned source handle 生命周期属于 AppState，并在 shutdown 关闭；直接构造 AppState 的测试/工具也必须显式走 shutdown 或关闭该 owner。
- Task 7 必须以 `state_revision` 丢弃更低 revision；这不在 Task 6 前端范围。

## Review fix round 2/5（2026-09-01）

### RED → GREEN

Owner token RED：受控挂起 DLC import 的末次 broadcast 后，并发 LLM save 仍返回 200，证明共享布尔 reservation 可被另一路径错误穿透/清除。将 reservation 改为 `eq=False` 的唯一 owner object，并规定所有 acquire/check/release 只在 `timeline_transition_lock` 下执行后，挂起 DLC 期间 LLM、cap、story import 均稳定 `409 story_runtime_busy`；它们的 `finally` 只能 identity-match 清除自己的 token，DLC 完成后下一次 import 正常成功。Planning 也持有同一类 owner，既有并发 play 仍保留 `story_planning_active` 的稳定优先级。

No-replace RED：POSIX commit mock 证明旧 `os.replace` 会覆盖已存在 final；store 也没有 pinned delete API。改为 pinned dir-fd 上 `os.link(temp, final, follow_symlinks=False)` 原子创建 final、再相对 unlink temp，`FileExistsError` 直接触发 opaque ID retry；Windows 继续使用 `ReplaceIfExists=False`。重复 token 测试验证第一份 bytes 不变、第二份取得新 ID，delete 只接受 store 生成的 opaque identity 并只删除对应 final。

异步 store RED：250ms stall 下旧同步 import 令 event loop/disconnect 延迟约 0.53s；取消仍会注册 source；shutdown safety 也延迟约 0.55s。实现锁内 owner/generation reservation、锁外 tracked `asyncio.to_thread(store)`、短锁内 identity/runtime revalidation 后，callback/disconnect 与 shutdown safety 均在 250ms 门内完成。取消或 shutdown race 等待不可中断的当前文件操作收敛，再用 pinned `delete()` 清除未注册 final；shutdown 在关闭 root handle 前 settle 所有 tracked import/store/delete。额外 RED 覆盖 cleanup 自身抛 unexpected exception 和 store unexpected exception，GREEN 后 owner/task/request set 总是清理，公开响应固定 generic 500 且不含内部路径。

聚焦回归曾捕获 owner 检查顺序把第二个 play 从既有 `story_planning_active` 改成 generic `story_runtime_busy`；调整 planning-task 检查优先级后恢复既有 API contract。

### API schema、并发与安全隔离增量

- HTTP/WS schema 无新增或删除字段；本轮只强化既有 `409 story_runtime_busy|story_planning_active` 与 `500 story_import_failed` 的稳定语义。
- `story_runtime_owner` 是 process-local、不可序列化的唯一 token；DLC、LLM、cap、profile、import 与 play reservation 不能互相偷清，active novel/autopilot/replay 和 shutdown 仍拒绝新 import/play。
- Store worker 只接触 `PinnedStorySourceStore.store/delete` 和 immutable `ImportedStory/StoredStorySource`；它不读取或修改 AppState 异步状态。AppState registry、selection、generation 与 owner 只在 event-loop transition lock 内变更。
- POSIX/Windows final commit 都是 no-replace；source ID collision 不覆盖 bytes。临时文件 exclusive/no-follow，write/flush/fsync、handle-relative commit、目录 fsync、异常 cleanup 和 root identity checks 保持 round 1 的安全边界。
- 未注册 source 永不进入 registry/selection；取消、shutdown race、registration revalidation failure 都通过 pinned opaque identity 删除 final，不接受 caller path。

### 最终验证证据

```text
python -X dev -W ignore::DeprecationWarning -W error::ResourceWarning \
  -m unittest tests.test_story_endpoints tests.test_story_source_store \
  tests.test_session_endpoints tests.test_app_state_timeline \
  tests.test_game_loop_timeline tests.test_timeline_session -q
Ran 190 tests in 22.778s, OK

python -m unittest discover -s tests -q
Ran 540 tests in 54.630s, OK (skipped=5)

python -m compileall -q backend tests: exit 0
git diff --check: exit 0（仅 Git 的 LF/CRLF 工作树提示）
```

全程未运行 `tests/probe_llm.py`，未联网、未调用真实模型、未连接真实设备、未 push/tag。本轮提交信息为 `fix: serialize and offload story imports`；最终 hash 在提交完成后由交付消息报告。

### 剩余边界

- 当前开发主机直接执行 Windows no-replace/delete 分支；POSIX hard-link no-replace 分支由 capability/mock 单测覆盖。缺失安全 `dir_fd`/`follow_symlinks` 能力的平台继续 fail closed。
- 文件系统操作不能被 Python 强制中断；取消和 shutdown 会先完成当前 bounded source store/delete，再释放 owner 并关闭 pinned handle。这是 ledger 明确接受的等待边界，安全 cleanup 与 event loop 不在该线程等待期间被 transition lock 阻塞。
- 进程崩溃时可能留下同目录 dot-temp（final 仍不被覆盖）；MVP2 没有批准 startup scavenger。正常异常、取消与 shutdown 路径均清理 temp/final，并由测试验证。
