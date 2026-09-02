# MVP2 离线忠实小说模式：extra final fix 报告

日期：2026-09-02

基线：`7836474f10d235f8ecdddd6724af926ee1b866e4`

实现提交：`6e87c1e339f7f7d2ef023f08bb2dc59b10f8274e`

范围：仅修复 extra-wave 已裁定的三个 residual；未增加依赖，未访问网络/设备，未 push、merge 或 tag。

## 结果

三个 residual 均按 RED → GREEN → REFACTOR 完成。聚焦回归、完整后端测试、Python 编译、前端测试与生产构建全部通过。REFACTOR 阶段逐项复核了身份比较边界、任务所有权与 Windows 句柄所有权；未做无关重构。

## 1. 过期分析检查的运行身份

### 根因

`_inspect_story_analysis_offloop()` 只捕获 source generation。检查完成后会无条件把 worker 捕获的旧 DLC provenance 写回 `story_dlc_version` 并发布 lookup；随后 play 又依赖这项可被旧检查覆盖的全局值复核，因此旧检查可把自身重新变成“当前”。既有 planner signature 虽覆盖 LLM、prompt、DLC、waveform 和 caps，但没有不可变、单调的运行身份世代，也没有随 inspect/play 全程携带。

### RED

先新增并在基线实现上运行六个定向测试：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest `
  tests.test_story_endpoints.StoryEndpointTests.test_stalled_state_inspection_rejects_concurrent_profile_change `
  tests.test_story_endpoints.StoryEndpointTests.test_stalled_state_inspection_rejects_concurrent_dlc_change `
  tests.test_story_endpoints.StoryEndpointTests.test_stalled_state_inspection_rejects_concurrent_llm_change `
  tests.test_story_endpoints.StoryEndpointTests.test_stalled_state_inspection_rejects_cap_change_even_after_aba `
  tests.test_story_endpoints.StoryEndpointTests.test_stalled_state_inspection_rejects_concurrent_waveform_change `
  tests.test_story_endpoints.StoryEndpointTests.test_play_rejects_stale_inspection_without_overwriting_current_dlc -v
```

预期失败被稳定观察到：6/6 失败。五个 `/api/state` 用例均得到旧 `ready`，预期为 `missing`；play 用例进入模型规划（`call_count == 1`），预期在规划前以 `story_state_changed` 拒绝。首次最小身份修复后，cap ABA 测试仍以旧 `ready` 失败，证明 ready 内存缓存也必须绑定单调 token，而不能只绑定最终相等的 signature。

### GREEN / 最小修复

- 新增冻结的 `_StoryRuntimeIdentity(revision, signature)`；revision 只单调递增，signature 沿用既有完整语义：LLM client/model、planning prompt、DLC provenance（含 profile/prompt 内容）、waveform 能力与 policy、A/B caps。
- inspect 在 worker 前捕获 identity，worker 后重新在 tracked off-loop provenance 派生中确认当前 identity；仅当 token、source generation、record identity 和 analysis key 均仍匹配时发布。
- 旧 inspect 不再写回全局 DLC，不再发布 ready；内存 lookup key 加入完整 runtime identity，阻止 ABA 后旧 ready 复活。
- state/analysis/chapters/reader 使用“仅当前 inspection，否则当前缓存”的 lookup；play 的首锁和规划后末锁均比较 inspect 捕获的 immutable identity 与当前派生 identity，不依赖可被旧任务覆盖的全局 DLC。
- profile、DLC、LLM 和 cap 的生产变更入口在实际变更边界使 identity 失效；直接的 LLM/cap/waveform 变化也由 live signature 比较捕获。

修复后的六项测试：

```text
Ran 6 tests in 0.627s
OK
```

## 2. force_after 后无限 gather

### 根因

`_stop_autopilot_task(force_after=...)` 第一次超时后再次 `cancel()`，但随后无条件 `gather()`。吞掉 `CancelledError` 的任务可使 finish、stop/estop、disconnect 和 AppState shutdown 所走的生命周期永不返回。此外，旧 autopilot loop 每轮读取可替换的 `self.autopilot_stop`，在新 owner 启动后存在 ABA 式重新入循环的可能。

### RED

先加入永久吞取消（仅在测试清理阶段显式 release）的回归：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest `
  tests.test_game_loop_timeline.GameLoopTimelineTests.test_finish_retires_permanently_cancellation_resistant_autopilot `
  tests.test_game_loop_timeline.GameLoopTimelineTests.test_shutdown_stop_retires_permanently_cancellation_resistant_autopilot -v
```

两项均失败：finish/stop 在 250ms 有界观察窗内未完成。随后加入新 owner generation 测试；在只做双重有界等待、尚未绑定 per-task stop event 时，`old_reacquired` 为 true，稳定证明旧 loop 可加入新 owner。

### GREEN / 最小修复

- stop 默认采用两个 50ms 有界等待；第二次仍未完成时不再 gather，而是把任务加入 `_retired_autopilot_tasks` 后立即返回。
- retired 任务的 done callback 总会取走 eventual exception 并移出集合，避免未处理任务异常；生命周期不会等待 retired 集合。
- 每个 autopilot task 捕获自己不可替换的 stop event。旧任务即使迟到恢复，也只看到旧 event，不能加入新 owner generation。
- session/output 原有 clear 与 routing generation 继续是物理输出最终所有权边界；迟到动作返回 `([], [], True)`，不会改变 A/B output generation。

测试证明：finish 和 shutdown 调用链有界返回；两次取消均发生；清零完成；retired 被跟踪并在退出后回收；迟到输出被拒；旧任务不恢复所有权；事件循环异常处理器未收到未处理任务警告。

## 3. Windows 原生目录句柄转交失败泄漏

### 根因

`CreateFileW` 成功后，原生 HANDLE 的所有权只有在 `msvcrt.open_osfhandle` 成功时才转交给 CRT fd。原实现未处理转交调用抛异常的分支，因而泄漏 HANDLE。

### RED

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest `
  tests.test_story_analysis_store.AnalysisStoreTests.test_windows_directory_handle_transfer_failure_closes_native_handle -v
```

预期失败：`CloseHandle` 期望调用一次，实际调用 0 次。

### GREEN / 最小修复

用跨平台 mock 令 `CreateFileW` 返回固定 HANDLE、`open_osfhandle` 抛出同一 `OSError` 实例。生产代码在转交异常分支调用 `CloseHandle`，即使 close 自身失败也用 bare `raise` 保留原异常及 traceback。完整 analysis-store 模块结果：

```text
Ran 34 tests
OK (skipped=1)
```

跳过项是当前 Windows 环境缺少创建 symlink 的权限；本次新增的 Win32 mock 用例及现有 Windows junction/native-handle 用例均执行通过。

## 变更文件

- `backend/main.py`：单调不可变 story runtime identity；inspect 发布门禁；state/play identity 复核；运行配置入口失效处理。
- `backend/game_loop.py`：双重有界取消、retired task 跟踪/回收、per-task stop event。
- `backend/story/analysis_store.py`：HANDLE 转交失败时关闭原生句柄并保留原异常。
- `tests/test_story_endpoints.py`：profile/DLC/LLM/cap ABA/waveform 并发 state 检查与 stale play 回归。
- `tests/test_game_loop_timeline.py`：永久吞取消、生命周期有界、迟到输出/owner generation/未处理异常回归。
- `tests/test_story_analysis_store.py`：Windows handle-transfer ownership mock 回归。

## 完整验证输出

### 聚焦后端回归

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest tests.test_story_endpoints tests.test_game_loop_timeline tests.test_story_analysis_store -v
```

```text
Ran 118 tests in 18.220s
OK (skipped=1)
```

### 完整后端

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_*.py'
```

```text
Ran 585 tests in 70.929s
OK (skipped=5)
exit code 0
```

### Python 编译

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m compileall -q backend tests
```

```text
exit code 0
(no stdout)
```

### 前端测试

使用仓库已有 bundled Node `D:\AI-for-Coyote\.runtime\node\node-v24.19.0-win-x64` 与既有依赖树；未安装依赖、未访问网络。

```powershell
& 'D:\AI-for-Coyote\.runtime\node\node-v24.19.0-win-x64\npm.cmd' --prefix frontend test -- --run
```

```text
tests 9
suites 0
pass 9
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 141.52
exit code 0
```

最初一次 launcher 命令把 PATH 指向 bundled Node 的父目录，PowerShell 报 `npm` 未识别；定位实际 `npm.cmd` 后按上面的绝对路径重跑通过。这是本地门禁启动路径错误，没有产品代码失败，也没有触发安装或网络访问。

### 前端生产构建

```powershell
& 'D:\AI-for-Coyote\.runtime\node\node-v24.19.0-win-x64\npm.cmd' --prefix frontend run build
```

```text
vite v6.4.3 building for production...
✓ 1608 modules transformed.
dist/index.html                               0.42 kB │ gzip:  0.31 kB
dist/assets/theme-cushou-CHvRhc1C.png       532.71 kB
dist/assets/theme-pingpinghui-BWDEkUrz.png  728.78 kB
dist/assets/index-4HIDINJO.css               30.99 kB │ gzip:  6.63 kB
dist/assets/index-Doxnx5Pn.js               275.92 kB │ gzip: 84.82 kB
✓ built in 2.76s
exit code 0
```

### Git 检查

```powershell
git diff --check
```

```text
exit code 0
仅输出 Windows autocrlf 的 “LF will be replaced by CRLF” warning；无 whitespace error。
```

实现提交完成后、报告创建前：

```powershell
git status --short
```

```text
(empty)
```

## 自审

- 身份 token 是 frozen value object；revision 单调且参与缓存 key，因此 signature 最终恢复相同值时也不能复用变更前的 ready。
- inspect 的旧结果仍可返回给发起它的内部调用对象，但所有发布和消费路径都必须通过当前 identity 校验；旧结果无法改变全局 DLC/ready。
- play 两个短锁均比较捕获 token 与当前 token；规划期间的任何运行身份变化都会在 start 前拒绝。
- retired asyncio task 无法被 Python 强制杀死，因此实现选择隔离、取消输出所有权、持续跟踪并回收 eventual result；关键安全生命周期不等待它。
- Windows ownership 只在 `open_osfhandle` 成功时转给 CRT；失败分支恰好关闭一次原 HANDLE，原异常对象保持不变。
- diff 范围严格为三个生产文件及对应测试，没有功能扩展、依赖或无关格式化。

## 剩余风险 / concerns

- 如果第三方/故障 coroutine 永远不退出，retired task 会留在进程内集合直至事件循环终止；这是 asyncio 无法强制终止协作式 coroutine 的固有限制。它不再拥有 output/session generation，且 finish/stop/disconnect/estop/shutdown 不等待它。
- 运行身份变更会让旧 identity 的 process-local analysis lookup 留在字典中但永远不可达；运行时设置变更通常低频，当前 extra-wave 不引入额外淘汰策略，以避免超出授权范围。
- 完整后端的 5 项 skip 均为既有平台/权限条件；未执行网络、真实设备或外部安全审计。

## Extra-wave fix round 1：retired 回合的 busy 所有权

日期：2026-09-02

复审起点：`0deb5b60d5c9155c6f731bec2844df02f0ee488f`

修复提交：`036653f29809e0eda1acfa1a607907702c6d8281`

### 复审结论与根因

定向复审确认前述 A（stale runtime identity）和 C（Windows HANDLE transfer）已解决，但 B 仍有一个 Important：真实 `_autopilot_turn()` 在进入 LLM await 前把共享 `turn_busy` 设为 `True`，只有该 coroutine 最终离开 `finally` 才设回 `False`。双有界 stop 可以 retire 一个永久吞 `CancelledError` 的旧任务，却没有释放这个共享 busy 值；新 autopilot loop 因此一直跳过回合。若只在 retire 时直接清布尔值，旧任务迟到的 `finally` 又会误清新 owner 的 busy。

此前的 owner-generation 测试 mock 了整个 `_autopilot_turn()`，绕过了真实 busy 写入点，因而没有覆盖该缺陷。

### RED

先新增走真实 `_autopilot_loop()` → `_autopilot_turn()` → `llm.chat()` 的测试；只在外部 LLM seam 注入永久吞取消的首个 await，第二个 owner 的 LLM await 独立阻塞：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest tests.test_game_loop_timeline.GameLoopTimelineTests.test_retired_real_autopilot_turn_cannot_starve_or_clear_new_owner -v
```

基线实现稳定失败，且与复审探针一致：

```text
AssertionError: Tuples differ: (True, False) != (False, True)

First differing element 0:
True
False

- (True, False)
+ (False, True)

Ran 1 test in 0.475s
FAILED (failures=1)
exit code 1
```

元组分别表示 `(busy_after_retire, new_started_before_old_release)`：旧任务 retire 后仍占 busy；在旧 LLM 未 release 前，新 owner 没有进入真实 LLM 执行边界。

### GREEN / 最小修复

- 用冻结的 `_TurnBusyToken(task, generation)` 代替无所有权的共享布尔写入；每个真实 AI 回合 claim 自己的单调 token，并在 `finally` 只 discard 自己的 token。
- `turn_busy` 成为当前非 retired token 集合是否非空的只读派生值，保持 autopilot/观察循环现有判断接口。
- `_retire_autopilot_task(task)` 只删除属于该旧 task 的 token，使新 owner 可立即进入；旧 task 迟到的 `finally` 再 discard 旧 token，不会影响新 token。
- 用户消息、主动开场、自动观察和 autopilot 四条真实 AI 回合路径统一采用同一 token contract，避免共享 gate 的任一路径继续无条件清除别的 owner。
- 保持既有 output routing generation 防护不变，没有改变公共 API、物理输出或 session 生命周期。

同一测试在最小修复后：

```text
Ran 1 test in 0.282s
OK
exit code 0
```

测试随后释放旧 LLM、让旧真实 `_autopilot_turn` 经过迟到 `finally` 完成，并断言新 owner 仍保持 `turn_busy=True`，覆盖“旧 finally 不得清新 owner”。

### 相关测试稳定性复核

第一次完整门禁中，上一轮的 `test_shutdown_stop_retires_permanently_cancellation_resistant_autopilot` 在等待“进入取消阶段”的 200ms 条件上限处超时；同一用例立即单跑通过，但耗时 0.183s，确认是 full-suite 负载下接近上限的测试观察窗口，而非生产生命周期回归：

```text
Ran 1 test in 0.183s
OK
```

仅把该相关测试等待 `cancellation_seen` 条件出现的保护上限从 0.2s 调整为 0.5s；取消发生后的关键 bounded-return 断言仍保持 0.25s，不放宽生产要求。随后 GameLoop 模块与完整后端均 fresh 通过。

### 本轮变更文件

- `backend/game_loop.py`：generation-scoped busy token claim/release；retire 按 task 释放旧 token。
- `tests/test_game_loop_timeline.py`：真实生产回合的 cancellation-resistant LLM 回归；相关条件等待去负载抖动。

### 本轮完整验证输出

生产路径定向 GREEN：

```text
Ran 1 test in 0.282s
OK
```

GameLoop、session endpoints 与 AppState timeline 相关聚焦：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest tests.test_game_loop_timeline tests.test_session_endpoints tests.test_app_state_timeline -v
```

```text
Ran 118 tests in 32.663s
OK
exit code 0
```

GameLoop 模块在测试等待调整后 fresh 复核：

```text
Ran 26 tests in 3.541s
OK
exit code 0
```

完整后端 authoritative rerun：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_*.py'
```

```text
Ran 586 tests in 71.343s
OK (skipped=5)
exit code 0
```

Python 编译：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m compileall -q backend tests
```

```text
exit code 0
(no stdout)
```

前端测试：

```text
tests 9
suites 0
pass 9
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 131.3258
exit code 0
```

前端生产构建：

```text
vite v6.4.3 building for production...
✓ 1608 modules transformed.
dist/index.html                               0.42 kB │ gzip:  0.31 kB
dist/assets/theme-cushou-CHvRhc1C.png       532.71 kB
dist/assets/theme-pingpinghui-BWDEkUrz.png  728.78 kB
dist/assets/index-4HIDINJO.css               30.99 kB │ gzip:  6.63 kB
dist/assets/index-Doxnx5Pn.js               275.92 kB │ gzip: 84.82 kB
✓ built in 1.32s
exit code 0
```

Git whitespace 检查：

```text
git diff --check
exit code 0
仅有 Windows autocrlf 的 LF→CRLF warning；无 whitespace error。
```

### 本轮自审与剩余风险

- token 集合而非单一 owner 值可正确表示重叠 AI 回合；任一回合完成都不会提前把仍在运行的另一个回合标为 idle。
- retire 仅按 asyncio task identity 移除 token；generation 使同一 task 的不同回合 token 仍保持唯一，迟到 release 幂等。
- mutation check：若恢复共享布尔，RED 元组失败；若 retire 不释放 token，新 owner 无法开始；若旧 finally 无条件清 busy，旧 LLM release 后的最终 `assertTrue(turn_busy)` 失败。
- 永远不退出的第三方 coroutine 仍可能作为 retired task 占用内存，这是 asyncio 的协作式取消限制；本轮保证它不再占用共享 busy、output 或 session owner。
- 本轮没有新依赖、网络、设备、push、merge 或 tag；完整后端的 5 项 skip 仍是既有平台/权限条件。

## Extra-wave fix round 2：retired 回合的逻辑状态隔离

日期：2026-09-02

复审起点：`5aaa70c87e31324486082e577286dd9e0791ecc4`

修复提交：`6fc2366675422459a07534214123671f0bbab96c`

### 复审结论与根因

fix round 1 已把共享 busy 改成 task/generation scoped token，使双有界 retire 后新 owner 可以进入真实 LLM，且旧 `finally` 不会清除新 owner 的 busy。但真实 `_autopilot_turn()` 仍在 LLM await 前把自动 prompt 直接追加到共享 `history`；取消抵抗的旧 LLM 在 retire 后恢复时，又无条件递增 `turn_count`、追加 assistant reply 并调用 `on_ai_turn`。既有 session routing generation 只拒绝迟到的物理动作，不能阻止这些逻辑状态写入。

因此 reviewer 探针在旧 LLM release 前观察到 `(turn_count=0, history_len=2, chat=[], A=0)`，release 后变成 `(1, 3, ['retired timeline line'], 0)`：两条 prompt 分别来自旧、新 owner，而迟到旧 reply 又污染了未来模型上下文和页面 chat。

### RED

先扩展上一轮的真实生产路径测试；测试仍经过 `_autopilot_loop()` → `_autopilot_turn()`，只在外部 `llm.chat` seam 让旧调用永久吞 `CancelledError`，不 mock `_autopilot_turn`。新 owner 在旧 LLM 未 release 前必须已经进入第二个 LLM await 并持有 busy；旧任务 release 前后都断言共享状态为 `(turn_count=0, history=[], chat=[], A=0, busy=True)`。

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest tests.test_game_loop_timeline.GameLoopTimelineTests.test_retired_real_autopilot_turn_cannot_starve_clear_or_mutate_new_owner -v
```

基线实现稳定失败（以下仅把很长的固定 prompt 正文归一化为 `<old prompt>` / `<new prompt>`，状态值与失败结果保持原样）：

```text
test_retired_real_autopilot_turn_cannot_starve_clear_or_mutate_new_owner ... FAIL

AssertionError: Tuples differ:
((0, [<old prompt>, <new prompt>], [], 0, True),
 (1, [<old prompt>, <new prompt>,
      {'role': 'assistant', 'content': 'retired timeline line'}],
     ['retired timeline line'], 0, True))
!=
((0, [], [], 0, True), (0, [], [], 0, True))

Ran 1 test in 0.285s
FAILED (failures=1)
exit code 1
```

这个 RED 同时证明：旧 prompt 在 retire 前已经污染共享 history；旧 LLM 恢复后又污染 counter、reply 和 chat callback；物理 A 输出虽然已由旧 routing generation 拒绝，仍不足以隔离完整回合。

### GREEN / 最小修复

- 新增冻结的 `_AutopilotTurnToken`，捕获当前 asyncio task、单调 `_autopilot_generation`、本回合 busy token，以及已有的 session action origin（controller identity、routing generation、mode、session ID）。
- 每次创建真实 autopilot owner 时递增 generation；校验同时要求 task 仍是当前 `autopilot_task`、generation 未变、autopilot 仍启用、busy 所有权仍有效，且 session provenance 仍匹配并处于 running。
- 自动 prompt 和进入 LLM 的 history 使用本回合局部深拷贝 staging，不再在 await 前写共享 history。LLM 返回后、动作 await 后、channel floor 后和最终 publish/callback 前重新校验 token；任一校验失败即返回 `None`，完全丢弃旧 prompt/result。
- 只有仍为 current 的回合才写 `turn_count/history`、执行/保底动作和调用 `on_ai_turn`。回调继续在 busy token 释放后运行，保持正常回合既有可见行为；用户消息、观察和主动开场路径未改。

同一生产路径测试在最终实现上：

```text
test_retired_real_autopilot_turn_cannot_starve_clear_or_mutate_new_owner ... ok

Ran 1 test in 0.332s
OK
exit code 0
```

### 本轮变更文件

- `backend/game_loop.py`：immutable autopilot/session turn token、单调 owner generation、局部 history staging，以及 publish 前 identity 复核。
- `tests/test_game_loop_timeline.py`：扩展真实 cancellation-resistant LLM 回归，覆盖新 owner 已开始、旧任务 release 前后逻辑状态不变、无旧 chat/output、旧 `finally` 不清新 busy。

### 本轮完整验证输出

GameLoop 模块 fresh 复核：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest tests.test_game_loop_timeline
```

```text
Ran 26 tests in 3.587s
OK
exit code 0
```

GameLoop、session endpoints 与 AppState timeline 聚焦：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest tests.test_game_loop_timeline tests.test_session_endpoints tests.test_app_state_timeline -v
```

```text
Ran 118 tests in 28.002s
OK
exit code 0
```

完整后端 authoritative run：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_*.py'
```

```text
Ran 586 tests in 71.142s
OK (skipped=5)
exit code 0
```

Python 编译：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m compileall -q backend tests
```

```text
exit code 0
(no stdout)
```

前端测试（当前 shell 没有 npm，因此用桌面环境提供的 Node 直接执行 `package.json` 的同一 `node --test tests/*.test.mjs` 脚本）：

```text
tests 9
suites 0
pass 9
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 120.5285
exit code 0
```

前端生产构建（同一 Node 依次执行现有 `tsc` 与 `vite build`）：

```text
tsc: exit code 0 (no stdout)

vite v6.4.3 building for production...
✓ 1608 modules transformed.
dist/index.html                               0.42 kB │ gzip:  0.31 kB
dist/assets/theme-cushou-CHvRhc1C.png       532.71 kB
dist/assets/theme-pingpinghui-BWDEkUrz.png  728.78 kB
dist/assets/index-4HIDINJO.css               30.99 kB │ gzip:  6.63 kB
dist/assets/index-Doxnx5Pn.js               275.92 kB │ gzip: 84.82 kB
✓ built in 1.50s
exit code 0
```

Git whitespace 检查：

```text
git diff --check
exit code 0
仅有 Windows autocrlf 的 LF→CRLF warning；无 whitespace error。
```

环境说明：首次 `npm.cmd test` 因当前 shell 无 npm 而未启动；随后尝试的 bundled pnpm 启动器在执行项目脚本前因其 `ERR_PNPM_IGNORED_BUILDS` 策略退出，并生成两个未跟踪 workspace/lock 文件。两个确定由本轮启动器生成的文件已删除，未保留依赖、lockfile 或 workspace 变更；authoritative frontend test/build 均直接使用现有安装树完成。

### 本轮自审与剩余风险

- mutation check：移除任一 LLM 后 identity gate，旧任务会恢复 RED 中的 counter/history/chat 污染；恢复 await 前共享 prompt 写入，则 release 前 history 断言失败；移除 generation/task 检查，则旧 task 可在新 session 中发布；移除 busy token 归属检查，则 retire 隔离不完整。
- token 为冻结值，generation 单调递增；session action origin 复用既有 controller routing generation，避免只比较可能被旧回合覆盖的全局布尔/值。
- 新 owner 的 prompt 也保持局部，因而在其 LLM 尚未完成时共享 history 仍为空；旧 owner 恢复前后测试均以完整 history 列表断言，不只是检查输出强度。
- 正常回合仍在 busy 释放后 publish/callback；26 项 GameLoop 与 118 项相关聚焦验证覆盖用户消息、观察、主动开场和正常 timeline autopilot，未观察到非 autopilot 路径行为变化。
- 永久不退出的第三方 LLM coroutine 仍会作为 retired task 占用内存直到自身退出，这是 asyncio 协作式取消的既有边界；本轮保证它不再拥有 busy、session/output 或逻辑 history/chat/counter 发布权。
- 完整后端的 5 项 skip 仍为既有平台/权限条件；本轮无新依赖、网络、设备、push、merge 或 tag。

## Extra-wave fix round 3：并发用户 history 的 optimistic commit

日期：2026-09-02

复审起点：`a4789140936d334306a5ac856652a89637a5f93e`

修复提交：`7e514cd217529127a2af194566d2e3a802b9e6a4`

### 复审结论与根因

fix round 2 把 autopilot prompt/history 保持为局部 staging，并用 task/session generation 阻止 retired owner 发布；但一个仍属于当前 session 的 autopilot 在 LLM await 期间允许正常 `/api/chat` 完成。task、autopilot generation、busy token 与 session routing generation 此时都仍匹配，因此最终 `self.history = turn_history` 会用 LLM 开始前的整表快照覆盖刚提交的用户消息和回复。

这是 optimistic snapshot 缺少 logical-history version 的 last-writer-wins：session identity 能回答“是不是当前会话”，不能回答“模型上下文在 await 期间是否已被别的正常回合推进”。用锁包住 LLM 会让不协作调用阻塞用户回合与安全生命周期，因此不符合 ledger 裁定。

### RED

新增真实生产并发回归：通过 `/api/session/start` 启动真实 autopilot loop，让第一笔真实 `_autopilot_turn` 阻塞在外部 LLM seam；随后通过真实 `/api/chat` 完成用户回合，再释放当前（非 retired）autopilot LLM。测试不 mock `_autopilot_turn`、`handle_user_message` 或 HTTP route，并让迟到 autopilot 返回一个非空 A 强度动作，以同时观察 history、counter、chat 与 action 边界。

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest tests.test_session_endpoints.SessionEndpointTests.test_chat_commit_invalidates_a_blocked_current_autopilot_turn -v
```

基线实现稳定失败：

```text
test_chat_commit_invalidates_a_blocked_current_autopilot_turn ... FAIL

AssertionError: Tuples differ:
(2,
 [{'role': 'user', 'content': '<staged auto prompt>'},
  {'role': 'assistant', 'content': 'stale autopilot line'}],
 ['stale autopilot line'],
 40)
!=
(1,
 [{'role': 'user', 'content': 'user survives'},
  {'role': 'assistant', 'content': 'user line'}],
 [],
 0)

Ran 1 test in 0.114s
FAILED (failures=1)
exit code 1
```

失败证明用户回合完成后，当前 autopilot 仍覆盖了两条用户 history，额外增加一次 `turn_count`，发布 stale chat，并实际执行 A=40；不是只存在不可见的内部 snapshot 差异。

### GREEN / 最小修复

- `GameLoop` 新增从 0 单调递增的 `_history_revision`，以及唯一的 `_commit_history()` 写入边界；每次逻辑 history commit 先替换完整列表，再推进 revision。
- 用户 prompt/reply、主动开场 prompt/reply、观察 prompt/reply、clear，以及 autopilot 成功/失败的 history commit 全部统一经过该边界；生产文件中不再存在绕过 helper 的 `self.history` append/clear/assignment。
- 冻结的 `_AutopilotTurnToken` 增加捕获时的 `history_revision`。捕获 revision、复制 history 和创建 token 之间没有 await，也不持有跨 LLM 的锁。
- `_autopilot_turn_is_current()` 同时校验 logical-history revision。LLM 返回后的第一道 gate 位于 `turn_count += 1` 和 `_execute_ai_actions()` 之前；revision 有任何推进时直接返回 `None`，不 merge、不覆盖、不执行 action、不写 chat。后续既有 await 后 gates 也复用同一 revision 校验。
- 无竞争 autopilot 的 revision 保持一致并按原顺序提交 prompt+reply；普通 user/opening/observe/clear 的可见 history 内容与回合行为保持不变。

同一 HTTP 生产路径测试在最终实现上：

```text
test_chat_commit_invalidates_a_blocked_current_autopilot_turn ... ok

Ran 1 test in 0.105s
OK
exit code 0
```

上一轮 retired owner 回归与本轮用户并发回归组合复核：

```text
Ran 2 tests in 0.385s
OK
exit code 0
```

### 本轮变更文件

- `backend/game_loop.py`：单调 logical-history revision、统一 commit ledger、autopilot token revision 捕获/校验。
- `tests/test_session_endpoints.py`：真实 session start + blocked autopilot LLM + `/api/chat` 并发回归。

### 本轮完整验证输出

GameLoop、session endpoints 与 AppState timeline 聚焦：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest tests.test_game_loop_timeline tests.test_session_endpoints tests.test_app_state_timeline -v
```

```text
Ran 119 tests in 28.136s
OK
exit code 0
```

完整后端 authoritative run：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_*.py'
```

```text
Ran 587 tests in 72.834s
OK (skipped=5)
exit code 0
```

Python 编译：

```powershell
& 'D:\AI-for-Coyote\.venv\Scripts\python.exe' -m compileall -q backend tests
```

```text
exit code 0
(no stdout)
```

前端测试（桌面环境 Node 直接执行现有 `node --test tests/*.test.mjs` 脚本）：

```text
tests 9
suites 0
pass 9
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 118.9374
exit code 0
```

前端生产构建（同一 Node 依次执行现有 `tsc` 与 `vite build`）：

```text
tsc: exit code 0 (no stdout)

vite v6.4.3 building for production...
✓ 1608 modules transformed.
dist/index.html                               0.42 kB │ gzip:  0.31 kB
dist/assets/theme-cushou-CHvRhc1C.png       532.71 kB
dist/assets/theme-pingpinghui-BWDEkUrz.png  728.78 kB
dist/assets/index-4HIDINJO.css               30.99 kB │ gzip:  6.63 kB
dist/assets/index-Doxnx5Pn.js               275.92 kB │ gzip: 84.82 kB
✓ built in 1.42s
exit code 0
```

Git whitespace 检查：

```text
git diff --check
exit code 0
仅有 Windows autocrlf 的 LF→CRLF warning；无 whitespace error。
```

### 本轮自审与剩余风险

- 数据流审计：`rg` 显示生产中的 `self.history =` 仅存在于 `_commit_history`；user/opening/observe/clear/autopilot 的每个 prompt/reply commit 均推进 revision。
- mutation check：移除 token 的 revision 字段或 compare，本轮 RED 恢复；把首次 compare 移到 `turn_count`/action 后，只检查 history 的测试仍可能通过，但 count/A 断言会失败；若尝试 merge staged auto 结果，完整 literal history 断言失败。
- 测试在释放 autopilot 前先断言 `/api/chat` 已得到 `user line` 且共享 history 为完整两条用户记录；释放后再次断言相同列表、`turn_count=1`、chat 为空、A=0，覆盖“保留用户 + 丢弃 stale auto”的双边契约。
- revision check 是同步整数比较，没有锁或 await；不协作 LLM 不会阻塞用户 history commit。竞争时 autopilot 结果按 ledger 丢弃，下一正常 interval 可基于新 history 重试。
- `_history_revision` 是进程内单调整数；未来任何新增直接 history 写入必须继续经过 `_commit_history`。当前生产搜索无旁路。
- 完整后端的 5 项 skip 仍为既有平台/权限条件；本轮无新依赖、网络、设备、push、merge 或 tag。
