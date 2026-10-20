# Prism 材质库：现成项目与资料清单

更新日期：2026-10-19。以[当前整体方案](Prism-整体开发方案-材质展示库与UE收取.md)为准。

**主体直接改造 SurfaceStudio，面向 UE 用户；Prism 不另写一套同类材质库。**

## 现在已经有的，不用重复下载

| 内容 | 实际本地源码目录 | 负责什么 |
| --- | --- | --- |
| SurfaceStudio | `E:\eyes\SurfaceStudio-main\SurfaceStudio-main` | 卡片、详情、扫描、搜索、分类、收藏、最近、集合、整理等已有材质库功能 |
| Babylon.js | `E:\eyes\Babylon.js-master\Babylon.js-master` | 用贴图显示球体，覆盖透明、金属、粗糙、法线、自发光、SSS，并生成卡片缩略图 |
| HyperVault | `E:\eyes\HyperVault-master\HyperVault-master` | UE 原件收集、依赖整理、交接和取回的可用代码 |
| REA | `E:\eyes\rea` | 开发时拆解现成代码和功能，不随材质库作为运行依赖安装 |
| Agent-Reach | `E:\eyes\Agent-Reach` | 开发时查找、核对项目，不作为材质库运行依赖 |

SurfaceStudio 用现有网页界面接进 Prism 的页面；Prism 保留自己的启动入口、工程和左侧资源树。Blender 插件由 UE 连接流程接替。

Babylon 负责贴图套球。这里不做粗糙度等实时调参滑条，也不需要为了参数编辑再接 Inspector 或 Babylon Editor。

三个源码目录均多嵌套一层，使用表里的实际内层路径。当前是下载的源码归档；固定提交／版本及配套构建依赖由开发接入时记录，不能只因目录名含 master 就宣称与以前选定的标签完全一致。

## 已准备的环境素材

[Prism-Babylon-environment-assets.zip](../dependency-downloads/Prism-Babylon-environment-assets.zip)，约 203 KiB。

包含 `studio.env`、来源、许可和校验记录，为金属反射等球体展示提供离线环境。无需下载整个 Babylon 素材仓库。

## 后续接 UE 时使用你提供的内容

这部分不是让你重新去找 Git 项目：

- 真实 UE 材质、实例与必要依赖，用于收集和原样取回验证。
- 普通贴图送到 UE 时所用的默认材质模板，包括适合不透明、裁切、半透明、SSS 等实际用途的内容。
- 对应的 UE 版本、模板版本；如依赖额外插件，注明其名称和版本。
- 开发读取你提供的模板，整理颜色、法线、粗糙度、金属、自发光等参数对应表，完成实例绑定，不重新设计母材质。

现在的三个项目足够开始，暂不再增加其他相似材质库、另一套渲染器或完整 UE 源码下载任务。

项目来源：[SurfaceStudio](https://github.com/XXLDinosaur/SurfaceStudio)、[Babylon.js](https://github.com/BabylonJS/Babylon.js)、[HyperVault](https://github.com/nsynkde/HyperVault)。保留各自许可证、原作者信息和修改说明。
