<p align="center"><img src="prism/assets/logo.png" width="96" alt="Prism"></p>
<h1 align="center">Prism</h1>
<p align="center">用无限画布整理参考，用文档和脑图推进创作。</p>

<p align="center">
<a href="https://github.com/CLF7758/Prism/releases/download/v0.3.9/Prism-Setup-0.3.9-windows-x64.exe">下载 Windows x64 安装包 · v0.3.9</a> ·
<a href="https://github.com/CLF7758/Prism/releases/tag/v0.3.9">版本说明与源码</a> ·
<a href="README.en.md">English</a>
</p>

Prism 是面向绘画、概念设计、三维制作和内容创作的本地工作空间。把参考图放到自由画布中比较、标注和归类，把创作简报写进文档，用脑图规划结构；这些内容保存在同一个 `.prism` 项目里。

![Prism 无限画布与素材检查器](docs/images/release-canvas.png)

以上及下文截图由 v0.3.9 源码的实际界面渲染，使用程序生成的演示素材，不包含个人工程或第三方作品。演示素材不是 AI 生成，界面也不是概念设计图。

## 下载与安装

| 文件 | 用途 |
| --- | --- |
| [Prism-Setup-0.3.9-windows-x64.exe](https://github.com/CLF7758/Prism/releases/download/v0.3.9/Prism-Setup-0.3.9-windows-x64.exe) | Windows x64 安装包，内含 Python 与运行依赖，无需另装 Python |
| [Prism-0.3.9-source.zip](https://github.com/CLF7758/Prism/releases/download/v0.3.9/Prism-0.3.9-source.zip) | 与本次发布对应的项目源码及构建脚本 |
| [SHA256.txt](https://github.com/CLF7758/Prism/releases/download/v0.3.9/SHA256.txt) | 下载文件完整性校验 |

1. 下载并运行安装包，选择安装位置，可选创建桌面快捷方式。
2. 从开始菜单启动 Prism，或双击 `.prism` 项目。
3. 新建画布、拖入参考图片，按 `Ctrl+S` 保存项目。

默认安装到当前用户目录，不要求管理员权限。可通过 Windows 的“已安装的应用”卸载。当前发布目标为 Windows x64；主要开发验证环境为 Windows 11，尚未提供同等验收的 macOS / Linux 安装包。安装包目前未作代码签名，请使用本仓库的 Release 下载及校验文件。

## 在一个项目里完成参考整理

### 无限画布与资源库

移动、缩放、旋转、翻转和裁剪图片，用最优、水平、垂直或方形排列整理素材。笔画、箭头和文字可以选择与移动，但不会被素材自动排列带走。

“全部素材”“未分类”“未标签”“最近使用”、文件夹及标签结果也使用画布展示。框选、多选、属性编辑与回到所在画布组成同一套工作流程。

![全部素材的画布视图](docs/images/release-library.png)

### 标签、属性与检索

直接在右侧输入标签，管理标题、备注和评分；按标签、颜色等条件筛选。项目搜索可定位素材、文档内容和脑图节点。资源库支持画布、文档、脑图和文件夹的组织。

### 查重与回收站

按选区、当前画布或整个项目查找重复 / 相似图片，分组查看、定位到原画布后选择保留哪些。删除进入回收站；从画布删除时，查重列表也会更新，撤销删除会恢复匹配组。查重结果用于辅助判断，不会自动替你删除素材。

![查重分组与定位](docs/images/release-duplicates.png)

### 文字文档与思维导图

用富文本文档记录简报、清单和制作笔记，支持格式、列表、图片及查找替换；用脑图拆分主题，编辑分支、布局、样式和节点信息。

![项目文档](docs/images/release-document.png)

![思维导图](docs/images/release-mindmap.png)

### 视频、模型与采集

- 视频提取帧：按间隔采样、预览结果，添加到画布时自动最优排列，整批添加可一次撤销。
- 视频帧保留完整尺寸的无损 PNG，预览缩小不会替换保存的图片数据。
- GLB / glTF：缩略图与交互预览，支持旋转、缩放及恢复视角；不提供三维模型编辑。
- 剪贴板图片收集及配套 [浏览器扩展](browser-extension/README.md)。
- PSD 等格式的可用性取决于文件与解码组件；PSD 使用合成预览，不编辑图层。

## v0.3.9：大画布与数据可靠性

本轮重点是图片数量增加后的显示、查重和交互一致性：

- **按视口显示**：大画布以素材记录管理图片，按需要创建可编辑图元；多级图片预览与分块缓存减少远景绘制开销。
- **缩放质量**：不再在滚轮交互期间强制压到 192 像素，保留已加载的清晰层级和平滑缩放；新区域仍需异步加载。
- **缓存有界**：后台解码队列、显示缓存、图块和操作日志都有上限，避免无限累积。
- **查重提速**：复用只读归档连接，大图原始指纹在受限后台进程中计算，结果持久缓存；保持原指纹算法，不用降低图片分辨率来换取漏检。
- **原图保护**：导入的图片优先按原始字节嵌入工程并导出；检查和导出的范围一致，不把笔画、文字、视频误报为缺失图片。
- **操作修复**：橡皮擦、箭头、F2 重命名、左侧选中态、所在画布定位、临时标注清理与回收站同步。
- **界面统一**：移除画布 AI 打标签入口，二级窗口接入深色系统标题栏；本版不随安装包分发 AI 打标签模型。

开发机同一份 2712 张图片的工程副本中，空指纹缓存首次查重由约 **55 秒降至 21.4 秒**，同进程缓存复扫约 **0.23 秒**。逐张比对的指纹全部相同，分组均为 44 组。这是特定工程的一次实测，操作系统磁盘缓存未清空，不代表所有机器的冷盘性能。大型真实工程、不同格式和显卡仍需分别验证；首次显示与首次查重并非零成本。

## 原图与项目文件

`.prism` 是本地项目文件，可以保存图片原始数据、画布位置、素材属性、文档和脑图。普通导入图片在保存时优先保留文件原始字节；预览调整和 LOD 不应改写这些数据。视频、模型的嵌入与引用方式以导入选项为准。

“导出原始图片”“导出调整后图片”“导出整个画布”是不同操作。真实缺失的源文件仍会报告；程序无法从旧缩略图恢复已经不存在的原文件。截图、粘贴图片与视频提取帧是生成图片，不承诺保留其来源文件的 EXIF 等元数据。

公共素材视图和标签视图中的临时笔画、文字、箭头会在离开该视图时清理；需要保存标注时请在实际画布中操作。重要项目建议保留备份。

## 源码运行与开发

使用 Python 3.12，在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements/windows-lock.txt
.\.venv\Scripts\python -m pip install --no-deps -e .
.\.venv\Scripts\python -m prism
```

也可运行源码目录的 `启动Prism源码版.bat`。修改源码后需重启源码版；已安装 EXE 不会自动更新。

主要模块：

| 目录 / 模块 | 职责 |
| --- | --- |
| `prism/actions/` | 导入、导出、绘图等操作流程 |
| `asset_records.py`、`viewport_items.py` | 素材记录与可见编辑图元 |
| `lod.py`、`image_decode.py`、`canvas_tiles.py` | 多级显示、后台解码和分块缓存 |
| `source_store.py`、`fileio/` | 原始数据读取与项目持久化 |
| `duplicate_scan.py`、`duplicate_worker.py` | 查重调度、原始指纹及分组 |
| `workspace_service.py`、`project_catalog.py` | 页面目录与项目素材索引 |
| `widgets/` | 资源库、检查器、编辑器和对话框 |

架构整理仍在持续，主窗口与图元文件尚未全部拆分。测试与打包说明见 [开发工具](tools/README.md)、[Windows 构建](packaging/README.md)。问题反馈请附版本、复现步骤和相关日志，发布前去除日志中的个人文件路径。

## 许可证与来源

本项目基于 Rebecca Breu 的开源参考图工具扩展，保留原有版权声明，按 [GPL-3.0-or-later](LICENSE) 分发。来源与第三方组件说明见 [NOTICE](NOTICE)、[THIRD_PARTY.md](THIRD_PARTY.md)。二进制发布同时提供对应项目源码；依赖许可证和已知发布限制见 [发布检查](docs/RELEASE_AUDIT.md)。
