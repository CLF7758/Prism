<p align="center"><img src="prism/assets/logo.png" width="96" alt="Prism"></p>
<h1 align="center">Prism</h1>
<p align="center">把参考图放在一起看，整理素材，记录创作思路。</p>

<p align="center">
<a href="https://github.com/CLF7758/Prism/releases/download/v0.3.10/Prism-Setup-0.3.10-windows-x64.exe">下载 Windows 安装包</a> ·
<a href="https://github.com/CLF7758/Prism/releases/tag/v0.3.10">版本说明和源码</a> ·
<a href="README.en.md">English</a>
</p>

Prism 是给绘画、游戏美术、建模和设计用的参考图与素材管理工具。你可以把图片拖进无限画布，一边比较参考，一边画线、写字、整理分类；也可以把图片钉在桌面上，放在绘画或建模软件旁边看。

![Prism 无限画布](docs/images/release-canvas.png)

## 安装后怎么用

1. 下载并运行 [Windows x64 安装包](https://github.com/CLF7758/Prism/releases/download/v0.3.10/Prism-Setup-0.3.10-windows-x64.exe)。无需另外安装 Python、截图组件或搜索模型。
2. 打开 Prism，新建画布，把参考图片拖进去。框选图片后可以移动、缩放、自动排列。
3. 按 `Ctrl+S` 保存为 `.prism` 项目，下次打开接着整理。

安装包包含截图和本地以图搜图所需的运行环境与模型，因此体积较大。以图搜图在电脑本地运行，不需要填写 API；第一次检索会建立索引，耗时取决于图片数量和电脑性能。

目前提供 Windows x64 安装包，主要在 Windows 11 上验证。安装到当前用户目录，不需要管理员权限。安装包尚未签名；请从本仓库 Release 下载，并用同页的 `SHA256.txt` 核对文件。

## 能做什么

### 把参考图摆在一起看

画布可以自由移动、放大和缩小。图片能旋转、翻转、裁剪，也能按紧凑排列、水平或垂直排列整理。画笔、箭头和文字用来标出构图、光影或修改意见，整理图片时不会把这些标注一起排走。

### 截图直接放进画布，参考图钉在桌面

按 `Ctrl+Alt+A` 截取屏幕区域，截图可以送进当前画布。画布里的图片也可以钉在桌面上，方便对照绘画或建模。贴图支持调整透明度；需要标注时，使用与主画布相同的一套画笔工具。

### 找到想要的素材

用文件夹、画布和标签整理素材。全部素材、未分类、未标签和最近使用都能在画布中查看；右侧可以直接输入标签，也能跳到图片所在的画布。

搜索框右侧的小图片按钮可以用一张图片搜索项目中的相近图片。检索会参考画面内容、颜色和布局，结果供你挑选，不保证每张都属于相同风格。

![素材库画布](docs/images/release-library.png)

### 查找重复图片

检查当前画布或整个项目中的重复、相近图片，选中结果后可以定位到画布再决定是否删除。删除会先进入回收站，不会自动替你删除素材。

![重复图片检查](docs/images/release-duplicates.png)

### 从视频里挑参考帧

导入本地视频，预览并提取需要的画面。提取出的图片会自动排列，方便比较镜头、色彩和构图。

### 在同一个项目里写笔记、画脑图

用富文本记录需求、清单和创作笔记；用脑图整理主题、结构和分支。图片、文字和思路可以放在同一个项目里管理。

![项目文档](docs/images/release-document.png)

![思维导图](docs/images/release-mindmap.png)

还支持 GLB / glTF 模型预览，以及通过 [浏览器扩展](browser-extension/README.md) 收集网页图片。

## 文件保存在自己电脑上

项目使用本地 `.prism` 文件保存。普通导入图片保存时会保留原始文件字节，画布显示用的缩略图不会覆盖原图。老项目如果已经丢失源文件，现有缩略图不能还原出原文件；截图和视频提取帧也不会带有原照片的拍摄元数据。

全部素材、标签等汇总视图中的临时笔画和文字会在离开时清理。需要长期保存标注，请在具体画布里操作。重要项目请自行保留备份。

## 源码与开发

[下载对应版本源码](https://github.com/CLF7758/Prism/releases/download/v0.3.10/Prism-0.3.10-source.zip) · [更新说明](docs/RELEASE_0.3.10.md) · [Windows 打包说明](packaging/README.md)

源码运行需要 Python 3.12：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements/windows-lock.txt
.\.venv\Scripts\python -m pip install --no-deps -e .
.\.venv\Scripts\python -m prism
```

截图与以图搜图使用隔离的开源组件；源码环境的准备脚本在 `tools/setup_*_component.py`。安装版已经打包这些依赖，不需要用户运行脚本。架构与性能实现说明见 `docs/`，问题反馈请附版本、操作步骤和去除个人路径后的日志。

## 开源许可

本项目基于 Rebecca Breu 的开源参考图工具扩展，按 [GPL-3.0-or-later](LICENSE) 发布。截图接入 ScreenPinKit，本地以图搜图使用 OpenCLIP、FAISS 和 OpenCV。原有版权与第三方声明保留，详见 [NOTICE](NOTICE)、[THIRD_PARTY.md](THIRD_PARTY.md) 和安装目录中的许可证。仓库现有演示图片继续沿用，未在本版更换。
