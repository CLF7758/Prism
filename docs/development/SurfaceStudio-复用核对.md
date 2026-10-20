# SurfaceStudio 复用核对

日期：2026-10-19。本文件记录源码证据，产品与开发顺序以当前整体方案为准。

## 核对范围

本地来源：`E:\eyes\SurfaceStudio-main\SurfaceStudio-main`。

已人工读取：README、素材导入说明、`public/app.js`、界面样式、`server.py` 扫描与文件访问、`blender_bridge.py` 本机交接流程。

同时使用本地 `E:\eyes\rea` 中的 REA 6.4.0 对 `public` 目录作静态分析，覆盖 13 个前端源文件，生成应用关系及语义分析证据。该分析不等于运行了整个应用，也不等于验证了 UE 插件。

REA 原始分析保存在开发临时目录；本记录不依赖该临时文件作为产品运行条件。

## 已确认可复用

- `public/app.js` 的 `drawCards`：缩略图卡片、选中标记、卡片标题、分辨率和贴图数量等。
- 同文件 `openDetail`：大预览、标签、贴图通道、资料与操作布局；参数编辑和 UE 资料按当前需求改。
- 同文件 `renderResults`：搜索、筛选和排序；其记录来源改为当前 Prism 工程。
- 既有样式、图标、列表密度与网格／列表切换。
- 扫描中的贴图命名识别和 JSON 元信息读取。
- 分类、收藏、最近浏览、集合及整理功能的现成模块。
- 本机任务与结果文件的可用思路；具体 Blender 操作由 UE 插件替换。

## 必须补齐的实际差异

1. README 与导入说明明确：当前材质球主要使用已有图片；不能当成已有“从任意贴图生成球体”的渲染器。
2. 导入说明明确：现有普通库读取外部原目录，不复制托管；Prism 必须在收存成功前取得自己的完整原件副本。
3. 当前普通库扫描一层子目录；需要接入直接选择单套目录与多层目录的发现、分组确认。
4. 现有文件名识别和按文件夹名取分辨率需要补齐 UE 命名、打包通道及实际像素读取。
5. Blender 连接中的会话、请求、结果可以参考复用，但其对象模式、面选择和节点操作不能直接当作 UE 资产接口。

## 已准备文件与未完成部分

`prism/assets/materials/surface-studio/` 已归档卡片函数、样式、图标片段、原文件和许可证，`ORIGIN.json` 记录源文件摘要。

卡片渲染、原始样式和图标现已实际运行在 Prism 的材质库中，通过 QtWebChannel 连接真实记录与右侧资料。原文件托管、Babylon 贴图套球，以及 UE 收取仍需按新方案继续。具体结果见《材质库-第2步-SurfaceStudio卡片与详情》。

项目采用 GPL-3.0-only，保留作者 Copyright (C) 2026 XXLDinosaur、许可证和派生改动说明。


## 2026-10-19 渲染器核对补充

SurfaceStudio 的 `public/app.js` 第 29–34 行，`openDetail` 与 `previewModal` 都使用 `<img>` 加载 `a.preview`；`sphere` 按钮切回素材自带图片。`server.py` 第 131–137 行选择带 preview 的文件，找不到时退回颜色图。它没有将 PBR 贴图渲染到球体的 WebGL 内核，也没有 GLB 加载器。因此复用其卡片、分类和展示结构，不能声称已经复用它的球体渲染。

本次将 Babylon.js 官方 UMD 8.26.0 与同版本 glTF 加载器打包到 `prism/assets/materials/babylon/`，附原始许可证、来源和哈希。实际使用 `PBRMaterial`、`ArcRotateCamera`、`MeshBuilder.CreateSphere`、`applyDisplacementMapFromBuffer`、`Tools.CreateScreenshotUsingRenderTargetAsync`、`SceneLoader.LoadAssetContainerAsync`，不编写 PBR 光照、光栅化、glTF 解析或散射着色器。Prism 仅做槽位数据、通道提取、引擎配置、Qt 桥接。

Prism 原有 `glb_preview.py` 是自行解析三角形并绘制的简化预览，未包含完整 glTF PBR 材质。现在交互预览改为 `GLBEngineWindow` 加载原始 GLB/glTF；旧的画布静态缩略图路径本轮保留，不再把它当作新材质库的渲染依据。离线压缩 GLB 所需的额外 Draco/KTX2 解码器尚未打包，本轮原生 GPU 验证使用官方未压缩 BoxTextured。

SSS 目前使用 Babylon 原生薄表面次表面透光（Burley transport），输入图作为散射/透光强度，不接厚度。不能声称已复现 UE 的任意 Subsurface Profile 或母材质效果；完整体积散射配置仍需专项验证。
