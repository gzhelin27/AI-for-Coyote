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
