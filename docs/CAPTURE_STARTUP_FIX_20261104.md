# 截图组件启动与失败收尾修复

当前暂停打包和 GitHub 发布。已有 0.5.0 安装包早于本次修复，不能当作修复版使用。

## 确认的问题

- 主界面忙时，就绪消息可以已经到达系统管道，但 Qt 尚未处理读取通知。旧超时回调直接判失败并终止组件。真实 QProcess 故障注入已复现；仅读 Qt 缓冲仍然失败。
- 旧超时回调先发送 failed（界面打开模态提示框），后 kill。模态事件循环内会继续收到 ready、errorOccurred、finished；一次超时会产生多次错误提示，甚至先执行迟到任务再杀进程。

这两项是已复现的缺陷，不能证明它们是先前那次启动超时的唯一原因。相同组件、相同打包脚本路径及已读取的打包进程 Qt 环境，独立握手检查通过。

## 修改

- `prism/capture_service.py`：超时前以 `waitForReadyRead(0)` 非阻塞检查系统管道，再消费就绪消息；不延长超时时间。
- 终止和清空待处理任务先于界面通知；一次启动失败只通知一次，拒绝迟到 ready，允许进程停止后重试。重试清空上次诊断缓冲。
- 在现有日志中记录启动、阶段、就绪耗时、失败原因和退出状态，不记录截图内容或原始诊断输出。
- `prism/components/capture_helper.py`：报告 imports/application/bridge 三个启动阶段，区分卡在加载依赖还是 Qt 初始化。

## 验证

命令：

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_capture_startup_lifecycle.py tests/test_capture_inbox_cleanup.py tests/test_component_runtime.py tests/test_capture_integration.py -k 'not real_qt5_helper_capture_roundtrip'
```

结果：19 passed, 1 deselected，7.55 秒。

覆盖真实管道已就绪但 GUI 未读取的情况、模态回调重入、一次错误通知、真实子进程失败后重试，以及本机已安装 Qt5 截图组件的真实握手和正常退出。未执行选区截图、导入画布及撤销的桌面链路，未重新打包，未发布。
