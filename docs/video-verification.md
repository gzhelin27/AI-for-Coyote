# 视频版本离线验证记录

## 2026-09-16：本地视频无需上传

视频使用浏览器本地文件播放，`POST /api/video/local-sources` 仅接收不超过 4096 字节
的注册元数据；选择 10 GiB 视频不会走旧上传大小限制，不创建 `.media` 或读取视频内容。
CSV 和预解析时间轴仍通过注册 ID 绑定。每次选文件建立新 ID，旧上传记录继续兼容。

Chrome 实际选择了逻辑大小 10,737,418,240 字节的合成稀疏 MP4，注册 JSON 为 109 字节，
未调用视频上传接口。该素材用文件空洞扩大尺寸，验证大文件选择和本地解码路径，
不代表对所有真实 10 GiB 视频编码的兼容承诺。
实际播放验证了时钟同步、30/15 直达、60 裁到 40、暂停/定位/恢复、自然区间切换、
空档归零、停止重准备、换视频要求新 CSV、刷新后重新选择。页面无异常，真实设备帧为 0。
截图：Windows Temp 下 `coyote-video-acceptance/video-local-preview.png`。
浏览器验证日志：`work/video-local-browser-green.log`。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| 完整 unittest discover | 1,198 项，7 项环境跳过，196.024 秒，退出 0 | `work/video-local-full-tests.log` |
| 视频 API | 21 项全部通过，退出 0 | `work/video-local-api-green.log` |
| 前端测试 | 59 项全部通过，退出 0 | `work/video-local-frontend-tests.log` |
| 前端构建 | 退出 0 | `work/video-local-frontend-build.log` |
| Python compileall | 退出 0 | `work/video-local-compileall.log` |

独立审查未发现阻断问题。本轮改动集中于视频模块和前端导入流程，未改变视频输出调度、安全上限或原有模式的行为。

本轮只修改隔离开发工作树；用于用户试用的 `127.0.0.1:18127` 是临时存储和假设备的
dry-run 预览。正式服务和本地设备配置未改动。

## 2026-09-15：视频强度直接执行修订

本次替代原视频缓升策略：合法独占视频会话直接提交 `min(CSV 目标, 当前有效上限)`，
包括开始播放、切换区间、定位和恢复；未确认前不更新实际强度。
`RampPolicy` 及视频缓升状态已移除。普通手动、AI、小说和回放路径仍使用原 `max_step`。

新增回归验证了单次目标 30 的假手机发送与确认、确认前降 cap 到 5 的抢占清零、
外部 JSON 伪造标记和伪造内部令牌不能取得豁免、暂停后的旧令牌不能恢复输出。
正常波形片段等待锁跨过 30 秒节点时，仅丢弃过期帧，播放继续；
强度命令、失效所有权及租约仍严格拒绝，不能把迟到强度当作成功确认。

母任务独立验证四项直达/上限/跳转恢复/普通模式边界均通过。
真实 Chrome 合成 MP4 在隔离 `127.0.0.1:18126` dry-run 预览中验证：6 秒处直接 30/15、等待 2.1 秒保持；
暂停和暂停定位双零，恢复至 CSV 60 直接裁到 40；正常下降、播放定位、尾部空档与停止重准备通过，
界面无缓升状态和页面异常，真实中继帧为 0。
浏览器原始日志：Windows Temp 下 `coyote-video-instant-browser-final.log`；
母任务独立边界日志：`coyote-video-instant-independent-green.log` 和 `coyote-video-deadline-review-green.log`。

本次完整集成补丁仍保存在原 delivery 路径。新增
`docs/superpowers/deliveries/2026-09-15-video-direct-integration.patch` 仅包含
已部署 v1 到本次修订的 `backend/game_loop.py` 增量，并附规范化基线与结果哈希。
它的基线已与 v1 交付身份核对，不能和完整视频补丁同时重复应用。
其他视频独占文件由本次提交交付。本次修订未操作正式部署、端口或设备配置。

本轮首次完整回归发现原小说规划测试深拷贝安全状态时，不透明令牌对象被复制为
不同身份，触发四项状态不变断言。已将令牌实现为无可变字段、深拷贝保留身份的
内部能力对象，保持 `is` 授权比较；未修改原小说测试。规划和视频直达专项 47 项通过。

本次最终自动化结果（原始日志在工作树 `work/`）：

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| 完整 unittest discover | 1,190 项，7 项跳过，208.213 秒，退出 0 | `video-direct-full-tests-green.log` |
| 视频专项 | 67 项，1 项符号链接权限跳过，退出 0 | `video-direct-video-suite.log` |
| 规划与视频直达回归 | 47 项，退出 0 | `video-direct-planner-green.log` |
| 前端测试 | 55 项全部通过，退出 0 | `video-direct-frontend-tests.log` |
| 前端生产构建 | 退出 0 | `video-direct-frontend-build.log` |
| Python compileall | 退出 0 | `video-direct-compileall.log` |

测试总数相对 v1 减少 8：移除 17 项旧 RampPolicy 测试，增加 8 项直接强度测试
和 1 项自然波形截止回归。两个集成补丁在当前工作树的反向应用检查均通过。

## 原 v1 验证记录（历史）

下列缓升行为和测试数量是原 v1 的历史结果，不代表本次直接强度规则。

验证环境：Windows / Python 3.12 / Node 24 / Chrome；独立工作树，临时源存储、合成 MP4、假中继。
母目录的运行服务、真实设备配置与私有小说未被更改。

## 覆盖范围

- 精确四列 CSV、UTF-8/BOM、时间范围与重叠校验、空档双通道零输出。
- 每行独立锚定 30 秒块，30/30/10 尾块截断，A/B 独立随机、重复定位复用已解析波形。
- 真实 SafetyManager/GameLoop 的 dry-run 集成：0→30 的确认时间为假时钟 0、2、…、40 秒；20→30 立即，20→31 先30、再2秒31。
- 正常波形边界保留强度和缓升；在途强度不确定时优先清零。
- 延迟 ACK、降上限、锁等待超出节点/时钟授权、回跳后重复经过空档边界、清零失败重试。
- 暂停、定位、等待、播放结束、失联和急停的输出退休与清零。
- API 排他控制、非视频请求并发、视频启动与旧模式取消的双向竞争。
- 前端旧 sequence 回显过滤，正常 waiting/seeking 保留原生播放意图，服务端安全暂停仍优先。
- 大文件磁盘操作在线程执行；存储失败/CSV版本变化拒绝启动，解析计划原子保存。

## 真实浏览器合成素材验收

母任务在专用 `127.0.0.1:18123` dry-run 服务实际运行 Chrome MP4：

- 开头空档为0；播放中定位到6秒自然继续；2.4秒后的强度从10进入11。
- 暂停双0，暂停定位保持0；继续时选中相应块；未覆盖尾部双0。
- 自然跨越35秒的随机波形边界，两通道55个连续采样保持10，波形均切换。
- 回跳34秒复用同一会话原波形；自然75秒区间结束双0；实际媒体 ended 双0。
- 停止后可再次准备；播放时重新加载页面导致失联清零。
- 两轮最终脚本均退出0，无页面错误，`dry_run=true`、真实中继发送帧数0。

原始脚本与最终日志由母任务保留在 Windows Temp：
`coyote-video-browser-check.cjs`、`coyote-video-boundary-check.cjs`、
`coyote-video-browser-final.log`、`coyote-video-boundary-final.log`。
合成媒体与截图位于 Temp 下 `coyote-video-acceptance/`。

假时钟测试验证调度规则；浏览器采样验证端到端状态转换，未建立真实设备延迟上界。
本版本不声称精确恢复波形内部相位或已经完成实机验收。

## 自动化结果

最终结果如下，原始日志保留于工作树 `work/`：

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| 完整 unittest discover | 1,198项，7项跳过，194.422秒，退出0 | `video-full-tests-green.log` |
| 视频专项 | 75项，1项符号链接权限跳过，退出0 | `video-focused-tests-final.log` |
| 前端测试 | 54项全部通过，退出0 | `video-frontend-tests.log` |
| npm ci | 退出0 | `video-frontend-ci.log` |
| 前端生产构建 | 退出0 | `video-frontend-build.log` |
| Python compileall | 退出0 | `video-compileall.log` |

首次全回归发现并修复了非视频并发死锁。另有三项原测试将导入/分析准备时间计入
动作响应或锁等待时间，继承展开产生11项计时断言失败；修正计时起点后完整重跑通过。
原250ms响应和100ms锁等待阈值均保留。视频帧数测试使用注入时钟，避免真实执行耗时
改变100ms帧截断断言；生产截止检查保持严格。
独立9081测试入口已实际启动检查：首页200、`dry_run=true`、真实发送帧0，检查后已停止。

## 集成边界

新增视频模块和原本干净文件的修改单独提交。
`backend/main.py`、`backend/game_loop.py`、`frontend/src/App.tsx` 和 `tests/test_story_endpoints.py` 原先已有未提交开发内容，
因此只将视频增量输出为 `work/video-integration.patch`，以 `work/video-baseline/` 为基线，
并在 `work/video-integration-identities.json` 保存规范化内容哈希。
同一补丁与哈希还保存在 `docs/superpowers/deliveries/2026-09-15-video-integration.patch`
及其 `-identities.json` 配套文件，便于随提交交付。
交付时应检查并应用该增量，不能用整份旧工作树文件覆盖母目录。

实机接受、部署和版本标签待用户验收，本次未执行。
