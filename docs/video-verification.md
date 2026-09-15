# 视频版本离线验证记录

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
