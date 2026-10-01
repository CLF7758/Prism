# 开发工具 / Development tools

- `run_tests_safe.py`：按文件隔离运行回归测试。
- `collect_licenses.py`：收集安装版实际运行依赖的许可证和版本。
- `preview_native_ui.py`：以合成演示内容截取程序界面，不打开用户工程。
- `ui_shots.py`：抓取 Qt 窗口及面板。
- `build_theme.py` / `theme_template.qss`：由共享 token 生成主题。
- `check_dependencies.py`：检查可选依赖。

生成的截图和报告不进入 tools 目录的版本控制；README 使用的截图位于 `docs/images`。
