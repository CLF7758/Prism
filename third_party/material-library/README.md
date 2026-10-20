# Prism 材质库改造的开源源码区

当前边界以 [整体主方案](../../docs/feature-plans/Prism-整体开发方案-材质展示库与UE收取.md) 为准：真实UE母材质由用户提供，Prism只做展示/保存/分类，插件收取原生资产。必需源码为Babylon.js与HyperVault；AssetPort不再必需，原有归档不加载。一次性资料见 [总清单](../../docs/feature-plans/Prism-一次性下载总清单.md)。`BabylonAssets/` 是已归档的环境素材，来源、许可和校验记录在其ORIGIN.json中。

这些文件用于计划中的功能接入，目前没有注册进 Prism 运行时。需求基线见 [完整改造方案](../../docs/feature-plans/Prism-工作库与材质管理-完整改造方案.md)。

最新范围说明：Prism 第一期已改为使用 Babylon.js 的现成 PBR/SSS 与 Inspector，下载要求见 [第一期资料清单](../../docs/feature-plans/Prism-第一期需要下载的开源资料.md)。下方 Three.js 为旧选型归档，AssetPort/HyperVault 为后续 UE 参考；它们均不是当前第一期必需依赖。本轮只核对 Babylon 来源和固定版本，未把完整引擎自动加入本目录或 Prism 运行时。

| 目录 | 状态 | 用途 |
|---|---|---|
| `threejs/` | 已下载 npm three 0.170.0 制品，校验 registry SHA-512 | Prism 离线 PBR 预览候选；实际接入前测试 QtWebEngine 6.7 |
| `AssetPort/` | 已取得固定提交的 Python 源码子集、三种母材质和四张默认纹理 | 计划用于三种基础材质生成，尚未加载或在 UE 验证 |
| `HyperVault/` | 已取得固定提交的 40 个源码/说明/许可证文件 | 计划复用资产收集与取回流程，尚未编译或接入 |

每个目录保留上游 LICENSE。`upstream-lock.json` 保存版本/提交、源地址及每个文件的 SHA-256。不得把文件存在表述成已适配/已测试。Three.js r170 是固定原型版本，不是当前最新版本；发布前检查维护版本、依赖与兼容性。

AssetPort 不是完整的可安装发行版：没有 Widgets、Binaries/Intermediate 和自动启动脚本。`Materials/` 已取得三种母材质和四张默认纹理候选，但依赖完整性、参数与引擎兼容仍需检查；不能直接复制目录就声称插件可用。HyperVault 仅取得代码和描述文件，没有获取或安装其完整插件内容。

其余候选只归档研究结论与许可证证据。MaterialVault 不在此目录提供插件；PBR-Viewer 的复制许可本次未确认，未复制其应用代码。

更新上游时先选择不可变版本，重新核对许可证与内容 hash，再运行集成验证；禁止在安装或首次启动时自动获取 main/master。
