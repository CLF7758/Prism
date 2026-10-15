# Windows 发布流程

使用 Python 3.12、PyInstaller 和 Inno Setup 6。

1. 安装项目及 `requirements/build.txt`；可选媒体支持使用 `.[media]`。
2. 执行 `python tools/collect_licenses.py`，保存实际依赖版本和许可。
3. 执行 `python -m PyInstaller --noconfirm --clean Prism.spec`。
4. 执行 `iscc packaging/Prism.iss`，安装程序输出到 `release/`。
5. 在隔离目录验证安装、启动、打开项目、文件图标和卸载；再做编辑与媒体验证。
6. 为安装包生成 SHA-256。上传安装程序、校验文件和当前版本源码到同一个 GitHub Release。

安装采用当前用户范围，注册 `.prism` 图标和打开命令，创建开始菜单与可选桌面快捷方式。

## 发布许可检查

本项目从 GPL 上游派生，需提供同版本对应源码与构建脚本。保留 LICENSE、NOTICE、第三方许可及原有版权声明。

PyQt6 / PyQt-WebEngine 绑定使用 GPL 许可，不是 LGPL。Qt 原生库另有许可。PyAV wheel 同时包含 FFmpeg 和多个编解码库，不可只附 PyAV 的 BSD 声明就认定整个视频依赖已合规。

`licenses/dependencies.json` 记录本机依赖版本。完整发布前应核对 wheel 所包含原生库的对应源码、构建配置、第三方声明和获取方式，尤其 Qt/Chromium、FFmpeg、x264/x265。源码快照仅覆盖本项目，不替代这些库的对应源码义务。见 `docs/RELEASE_AUDIT.md`。

旧 winget / Scoop 模板仍需真实发布地址与散列才能启用，不视为已在软件商店发布。

## 0.3.10 完整组件打包

先用 Python 3.12 依次运行 `tools/setup_capture_component.py`、`tools/setup_visual_search_component.py --download-model`、`tools/setup_video_download_component.py`，再运行 `tools/bundle_components.py`。后者输出到全新的 `build/bundled-components`，不复制个人索引、截图、设置、下载历史或 Cookie。已有暂存目录时请另选 `--output` 或先人工核对，不会自动删除目录。

Inno Setup 将组件装到当前用户 `LOCALAPPDATA/Prism/components`，模型可直接使用，运行时优先使用随包的 `python-base/python.exe`，不依赖开发者电脑路径。组件保留在独立进程，避免 Qt5 与 Qt6 互相污染。需要准备约 4 GB 安装空间。发布使用本地完成验证的完整安装包；GitHub 构建只允许手动启动，避免打标签时覆盖已验收产物。
