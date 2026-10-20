# Prism 存储与离线更新技术附录（保留的上一版技术细则）

当前唯一整体实施主方案为 [Prism材质展示库与UE收取](Prism-整体开发方案-材质展示库与UE收取.md)。本附录仅供后续大库、保存与更新包设计参考，不包含当前下载清单，也不要求开发替用户制作UE母材质。

本文保留上一版的存储、保存、迁移和离线更新细则供开发参考。当前产品流程、三种必做材质、UE 插件范围、开源选型和开发顺序，统一以 [大白话版主方案](Prism-工作库与材质管理-完整改造方案.md) 为准。本文中“UE 延期”等历史范围说明不再作为当前主方案的要求。

2026-10-19 最新范围：先完成 Prism 材质球库，SSS 等八项能力均为第一期要求。第一期沿用现有 `.prism` 保存体系；本文的托管目录、增量更新和百 GB 存储重构留待后续，不是第一期前置条件。

版本：1.0 · 编写与资料核对：2026-10-19

适用基线：当前工作目录的 Prism 0.3.11、Python 3.12、PyQt6 6.7。本文是后续开发的需求与架构基线；源码调查结果与规划功能分开标注。当前目录是源码副本，没有 `.git`，不能据此给出 Prism 提交号。

本文件承接分享对话中最后的要求，重新整理为可执行的完整方案。分享页没有提供原先所谓“15 章文档”的可读取附件，不能声称已在那份原文件上逐行修订。

## 1. 最终交付物与用户实际得到什么

用户在一个 Prism 工作库中管理图片、视频、画布、文档、脑图和 PBR 材质。大库只需要完整搬迁一次，之后通过 U 盘、聊天软件传更新包。依据最新范围要求，本轮仅制定 Prism 改造方案；UE 插件、UE 资产传输与引擎兼容不属于本轮实施阶段。

必须交付以下五项：

| 交付物 | 用户看到的能力 | 完成判据 |
|---|---|---|
| 可靠工作库 | 自动保存，异常退出可恢复 | 所有内容类型可恢复，明确显示保存失败 |
| 材质管理 | 文件夹、标签、搜索、贴图槽位、球体预览 | 一套贴图作为一个材质管理，保持原始文件 |
| 完整导出包 | 首次搬库、备份、独立分享 | 断网导入到新电脑，引用完整 |
| 增量更新包 | 只传变化，自动找到旧库更新 | 重复、乱序、漏传和双方编辑均有确定处理 |
| 修复与诊断 | 缺文件、冲突、磁盘不足、任务中断可处理 | 错误不伪装为成功，不覆盖唯一有效副本 |

**产品决策：日常采用 Prism 托管的库文件夹；完整包和更新包均是用户可传输的文件。** 用户只看到一个工作库，无须管理内部碎片。现有 `.prism` SQLite 工程继续可读，提供迁移入口。这个选择是结合当前规模的工程决策，不是已经测得的性能结论。

首版不提供多人实时协作、着色器互转或云端常驻服务。先实现保存、材质管理与离线交接闭环。文中已有 UE 开源调研仅保留作后续参考，不要求当前接入，也不作为 Prism 阶段的验收条件。

## 2. 当前程序实况与必须改变的地方

以下路径相对仓库根目录，均在本次调查中读取过。

| 当前文件/能力 | 已确认的现状 | 下一步修改要求 |
|---|---|---|
| `prism/fileio/sql.py`、`schema.py` | `.prism` 使用 SQLite，`items` 保存图元，`sqlar` 保存原始资源，`USER_VERSION=4` | 保留旧格式读取器；新增工作库格式及迁移，不能直接把新格式冒充旧文件 |
| `SQLiteIO._write_data_batch()` | 保存遍历图元；部分元数据先提交，随后写资源、页面等；取消路径存在部分提交 | 新库以单次业务提交为原子边界；不能把现有整次保存描述成一个原子事务 |
| `prism/__main__.py` | `_perform_autosave()` 按定时器保存，已命名项目写原文件 | 新库改为事件驱动提交；旧工程仍保留现行保存入口 |
| `prism/config/settings.py` | 自动保存默认启用，默认间隔 5 分钟 | 新库默认短间隔提交；明确和旧格式设置的区别 |
| `_write_recovery_snapshot()` | QSettings 快照保存文件名、文档、脑图、workspace_pages 等 | 无法作为完整素材库的恢复保证，须改为磁盘上的未命名草稿库 |
| `prism/view.py` | `do_save()` 用 `ThreadedIO`，保存前调用当前编辑器 `store_to_scene()` | 引入统一保存协调器、内容代次与并发保存控制；工作线程不访问活 Qt 对象 |
| `document_panel.py` | 有 400 ms 延迟回写与 Web 编辑器 flush 机制 | 页面回写完成只是内存更新；必须等磁盘提交才显示“已保存” |
| `mindmap_panel.py` | `store_to_scene()` 回写树数据 | 增加编辑器确认序号，覆盖页面切换与退出前 flush |
| `source_store.py` | 导入快照使用临时目录，也有 SQLite 只读资源访问 | 新库导入先写持久对象库，不能依赖进程临时目录生存 |
| `workspace_service.py`、`window_workspace.py` | 画布/文档/脑图资源树、稳定节点 ID、软删除等 | 扩展材质入口；复用已有规则与撤销，不额外造另一套目录树 |
| `project_catalog.py`、`asset_records.py` | 全项目素材查询、虚拟记录/显示对象分离 | 材质应接入数据目录，搜索和数量统计不得依赖创建全部图元 |
| `widgets/asset_browser.py`、`detail_panel.py` | 中部素材浏览、右侧属性检查 | 复用卡片、筛选、主题、选择；增加材质专用属性与预览 |
| `glb_preview.py` | 标准库解析三角网格，Qt OpenGL/QPainter 预览 | 已有模型预览不等于完整 PBR；新增 Three.js 材质预览并保留无 GPU 降级 |
| `tests/test_material_features.py` | 这里的 material 是素材组织、标签评分、外部拖出测试 | 不能当作 PBR 材质或 UE 桥接已完成的证据 |
| `fileio/resource_export.py`、`resource_import.py` | 已有子树导入导出 | 复用范围选择和用户交互；增量协议需独立实现，不能直接把 ZIP 当增量同步 |

旧版保存路径和新工作库路径通过存储接口并存。不得在一个发布中同时替换全部历史 SQL、全部画布与全部导入器。

## 3. 开源选型：拿什么、用在哪、什么不能直接拿

本次使用指定的 `E:\eyes\Agent-Reach` 1.5.0 执行 doctor，并通过其文档列出的 `mcporter call exa.web_search_exa` 检索。doctor 显示 gh CLI 未安装；Exa 真实搜索成功。GitHub 公开 API 返回 403 限流，因此改读仓库网页、raw 文件与源码归档。API 失败记录保存在 `docs/research/material-library-upstreams-2026-10-19.json`，不把失败记录包装成已验证的提交信息。

### 3.1 决策表

| 项目与上游 | 本次查到的作用 | 接入决策 | 限制/成本 |
|---|---|---|---|
| [Three.js](https://github.com/mrdoob/three.js) · MIT | 浏览器 3D/PBR 渲染 | 直接复用离线模块，材质预览核心；本次选 r170 作为原型固定标签 | QtWebEngine 6.7 的 GPU/驱动需验证；该旧标签不是“最新版”或安全认证；发布前重新核对维护版本与兼容性 |
| [ScottRaffertyCG/Material-Vault](https://github.com/ScottRaffertyCG/Material-Vault) · MIT | UE 材质浏览、Asset Registry、Slate 界面 | 源码复用候选：编辑器面板和资产查询，接入 PrismBridge 前先编译测试 | README 面向 UE5.6+；不是 Prism 双向传输实现；标签持久化及代码完整度以源码/测试为准 |
| [Colosyn/Asset-Port](https://github.com/Colosyn/Asset-Port) · MIT | Python 批量贴图识别、配置纹理、建立材质实例 | 优先抽取 detector、models、materials 的相关逻辑，写 Prism 适配层 | 当前导入依赖自己的母材质与配置；不能直接复制函数就运行；替换现有资产的删除行为必须改掉 |
| [RodZill4/material-maker](https://github.com/RodZill4/material-maker) · MIT | Godot 程序化材质制作与绘制 | 可选外部制作工具；先以输出贴图接入，保留原工程附件 | 不把整套 Godot 节点编辑器塞进 PyQt；不是首版工作库的存储引擎 |
| [williamchange/material-maker-threejs](https://github.com/williamchange/material-maker-threejs) | Material Maker 到 Three.js 的实验预览 | 可研究槽位映射；首版不作为必需依赖 | README 明确实验性质，未单独完成许可证/全链路验证，暂不复制其源码 |
| [OpenImageIO](https://github.com/AcademySoftwareFoundation/OpenImageIO) · 当前上游 LICENSE 为 Apache-2.0 | EXR/HDR、高位深图像读写 | Prism 已有可选依赖，直接沿用读图路径；准确版本遵守现有打包锁文件 | 不同历史版本与编解码依赖许可证需按实际制品归档；不能只看主仓库 LICENSE |
| [MaterialX](https://github.com/AcademySoftwareFoundation/MaterialX) · Apache-2.0 | 跨 DCC 材质描述与处理 | 二期作为中间描述候选，不替换首版 PBR JSON | 不是任意 UE 材质图的无损转码器，节点/着色模型需逐项映射 |
| [iscc/fastcdc-py](https://github.com/iscc/fastcdc-py) · MIT | 内容定义分块 | 二期大资源去重试验候选 | Windows Python3.12 wheel、吞吐、分块参数与确定性须实测；首版整文件哈希去重即可 |
| [facebook/zstd](https://github.com/facebook/zstd) · 本次读取 BSD LICENSE | 快速压缩 | 二期更新包可选 codec；Python 绑定另外选型锁定 | 首版用标准库 ZIP64，避免新编译依赖；PNG/JPEG 等通常不再高压缩 |
| [restic/restic](https://github.com/restic/restic) · BSD-2-Clause | 内容寻址、快照、备份 | 参考不可变对象、快照与回收原则；可选外部备份 | 不把 Go 整套备份仓库改成 Prism 数据库或多人合并器 |
| [TriliumNext/Trilium](https://github.com/TriliumNext/Trilium) · AGPL-3.0 | SQLite、实体/Blob、应用层同步 | 只参考文档中的架构思想，自行实现 Prism 协议 | 不整仓搬入 Python/Qt，也不把 AGPL 代码带进 UE 插件 |
| [Unreal-Vault/Vault-Dev](https://github.com/Unreal-Vault/Vault-Dev) · 仓库标注 MIT | UE 资产库与分享 | 作为旧方案比较，不作为主基础 | README 指向 UE4.25.4，直接移植成本高于当前候选 |

选择依据为代码适配范围、许可证、现有技术栈及可验证性，不能以星数代替测试。上述“可用”指有具体复用路径，不等于已在 Prism 或本机 UE 中运行通过。

### 3.2 本次实际带入的东西

源码放在 `third_party/material-library/`，这是隔离的上游源码区，不会被 Prism 自动导入，也不放进当前安装包。每个成功下载的上游保留 LICENSE、README、文件 SHA-256；归档若携带提交号则写入 `upstream-lock.json`。以该锁文件和实存文件为准，不记录猜测的 SHA。当前仅 Three.js 属于 Prism 预览接入阶段；已经取得的 AssetPort 源码归档为后续参考，不要求本轮改造加载它。

AssetPort 保留已取得的 Python 源码与配置，排除 UE 二进制资产、截图、Binaries/Intermediate；MaterialVault 仅保留研究条目，暂停拉取与移植。Three.js 保留固定标签的离线模块与 MIT 许可证。

独立许可证证据位于 `docs/research/upstream-licenses/`，核对结果见 `license-evidence.json`。分支上的许可证是核对当时的内容；真正发布时必须根据所用锁定快照重新归档。

Prism 本身继续遵守现有 GPL-3.0-or-later 及第三方许可，复制源码保留原始版权。UE 候选代码只作为延期调研资料，本轮不加载、不编译、不分发 UE 插件。后续做插件时单独确定其许可证与独立通信边界，不把 Prism 的 GPL 实现随意复制进 UE 模块。

### 3.3 复用的具体改法

1. Three.js：使用 `MeshStandardMaterial`，统一显式槽位/色彩空间；参数只从 Prism 材质 manifest 读取，禁用任意 JS 注入与外部 URL。
2. OpenImageIO：复用现有读图与可选依赖；贴图识别只生成候选，不能覆盖手工用途。高位深原图始终保留，预览降采样另存缓存。
3. SQLite：复用 Python 标准库 sqlite3，不另造事务引擎；新库 schema 独立演进，旧格式只读适配/受控保存。
4. 外部 Material Maker：后续可让用户配置可执行路径，使用参数数组启动，独立进程运行；首版只接收它导出的贴图，不嵌入其节点编辑器。
5. UE 候选暂缓。已经检查到 AssetPort 依赖自己的母材质且有删除旧资产再重建分支，将来不能未经改造直接调用；这不是本轮 Prism 的实施任务。

## 4. 工作库、完整包、更新包与跨库分享的定义

| 概念 | 建议名称/扩展名 | 规则 |
|---|---|---|
| 托管工作库 | `场景设计.prismlib/` | 活跃编辑目标，包含索引和不可变资源 |
| 完整导出 | `.prismpack` | 一个文件，自包含；首次搬库默认保留 library_id |
| 更新包 | `.prismdelta` | 属于指定 library_id 和祖先历史，应用到本机已注册的工作库 |
| 收件状态回执 | `.prismreceipt` | 可选，只有确认收件状态后才更新收件基线 |
| 选中内容分享 | `.prismpack` 中 `mode=share` | 跨库复制，分配目标实体 ID，保留 origin 字段，不能冒充同库同步 |
| 老工程 | `.prism` | 旧 SQLite，继续读取；迁移后原文件保留 |

这些新扩展名是方案提案，尚未注册到系统。完整包并不是普通 ZIP 改名就结束；必须有格式版本、清单、引用闭包和校验。

```text
场景设计.prismlib/
  library.json                # 格式、library_id、创建信息；原子替换
  index.sqlite                # 权威元数据、实体修订、提交、任务、接收记录
  objects/sha256/ab/cd/<hash>  # 原始资源，不修改已有对象
  staging/<job_id>/           # 未完成写入，禁止用于已提交引用
  recovery/                  # 异常诊断与恢复上下文，非第二份权威库
  backups/                   # 元数据在线备份、迁移前快照
  cache/                     # 缩略图、球预览、搜索索引，可重建
```

`library_id` 标识同一条库历史；`device_id` 保存在设备应用数据中，不从完整包复用；首次搬库保留 library_id 但生成新的 replica_id。用户“复制为独立库”时生成新 library_id。两份同 library_id 的库在同一电脑出现时必须选定默认接收副本，不能随机应用更新。

不允许两进程写同一工作库，使用本机库锁和进程存活核对；不要仅依据锁文件年龄抢锁。网络盘上的实时 SQLite 写入不是首版支持范围；同步盘应放已完成导出包，不能直接同步打开中的 index.sqlite。

## 5. 内容模型、引用关系与版本

### 5.1 三种身份必须分开

| 身份 | 例子 | 作用 |
|---|---|---|
| entity_id | 文档、画布、图片资源、材质、材质版本 | 跨设备稳定身份，不使用数据库自增行号 |
| placement_id | 某图片在某画布上的一次摆放 | 同图在多画布出现，可独立位置和裁切 |
| blob_hash | 原始 PNG/EXR/UE 包字节的 SHA-256 | 资源去重与完整性检查，内容变化即产生新哈希 |

资产描述与画布摆放拆开：改某次摆放只修改 placement，替换整个资产资源才修改 asset。文档附件引用资产 ID；脑图链接引用实体 ID，不能保存另一台电脑不存在的绝对路径。

### 5.2 建议逻辑表

| 表 | 核心字段与约束 |
|---|---|
| library_state | library_id、schema_version、head_set、local_generation |
| entities | entity_id、kind、current_revision、deleted_revision；实体类型明确 |
| revisions | revision_id、entity_id、parent_revision_ids、payload_hash、author_replica；不可变 |
| blobs | hash、size、media_type、availability、relative_path；hash 唯一 |
| entity_blobs | revision_id、blob_hash、role；修订引用的完整资源闭包 |
| placements | placement_id、asset_id、canvas_id、transform、crop；作为可修订实体实现 |
| commits | commit_id、parent_commit_ids、change_manifest_hash、origin_replica、counter |
| commit_changes | commit_id、entity_id、before_revision、after_revision、operation |
| received_packages | package_id、manifest_hash、applied_commit、status；防重放/记录结果 |
| peers | peer_id、confirmed_heads、confirmed_inventory_hash、last_receipt |
| jobs | job_id、kind、state、progress_checkpoint、retry_at、error、cancel_requested |
| conflicts | conflict_id、base、local、incoming、resolution_revision、state |
| external_bindings（预留，不强制建表） | provider、external_project_id、entity_id、revision、locator；以后通过扩展迁移增加 |
| licenses | license_id、origin_url、license_text_hash、redistribution_flag |

SQL 实现时每表补齐外键与索引；实体查找、标签搜索、任务状态与引用追踪不能扫描全部 Blob。不要把所有页面 JSON 塞进一条库总记录，否则改一篇文档也会放大同步范围。

首版采用不可变整实体 JSON 修订，不实现字符级 CRDT。画布布局按 placement 分实体，脑图首版按整张脑图保留冲突版本；后续按节点拆分必须升级协议并补迁移。

### 5.3 数据与设备偏好

内容、材质参数、标签、分类、画布摆放和删除记录参与同步。窗口位置、当前选择、最近打开页面、预览相机、临时筛选与缓存默认只在本机保存。导出一份“当前展示布局”可以另作为用户内容操作，避免切换视角不断制造版本。

提交时间只用于展示。版本先后由祖先关系和修订关系决定，不使用两台电脑的系统时间决定覆盖。

## 6. 保存：什么时候保存、保存什么、何时才算成功

### 6.1 新库的默认时机

下表为新方案目标值，需在性能测试中确认；不是当前程序已实现的行为。

| 用户行为 | 内存/草稿处理 | 磁盘提交时机 |
|---|---|---|
| 文档输入 | Web 编辑器变更带递增编辑序号 | 停顿 800 ms 后提交；持续输入最长 5 s 提交一次 |
| 脑图编辑 | 节点操作形成有效树快照 | 停顿 800 ms；持续操作最长 5 s |
| 拖拽、缩放、旋转 | 按帧更新屏幕，不逐像素存盘 | 鼠标释放后 500 ms 合并提交；长时间未结束手势每 5 s 写有效草稿检查点 |
| 修改标签、评分、标题 | 可撤销业务命令 | 500 ms 合并；离开页面立即 flush |
| 调材质参数 | 滑条移动只更新预览 | 释放后 500 ms 提交；连续编辑最长 5 s |
| 新建、移动、删除、恢复页面 | 验证树结构成功后记录命令 | 操作完成即排队提交，通常 1 s 内完成 |
| 导入图片/视频/材质 | 先持久化源文件并校验 | 资源就绪才发布实体，不能先显示“导入完成” |
| Ctrl+S | 等待全部编辑器 flush | 作为显式提交屏障，等待当时的目标代次提交完成 |
| 切换页面/库 | 当前编辑器 flush | 提交旧库快照，完成后释放旧状态；新库打开不丢草稿 |
| 应用失焦 | 有界 flush 并排队 | 不反复阻塞界面，不保证失焦事件必然发生 |
| 退出 | 停止接收新编辑，执行屏障 | 成功才退出；失败显示具体失败与恢复位置 |
| Windows 休眠/关机 | 尽早触发短时屏障 | 仅作为补充，可靠性依靠日常提交，不能依靠收到关机通知 |

未命名项目启动后创建 `AppLocalDataLocation/Drafts/<uuid>.prismlib/`，导入素材也存这里。用户首次保存时执行受控搬迁/复制并切换库注册记录。未命名不等于只存在内存，重启后在“恢复未命名工作”中可找到它。

### 6.2 保存协调器与线程规则

新增 `SaveCoordinator`，每个库只有一个写入队列和一个 SQLite 写连接。GUI 线程负责从编辑器拿到已确认的纯数据快照；后台只接收不可变 DTO、文件路径、字节流和任务 ID，不读取 QGraphicsItem、QPixmap 或活跃 QWidget。

```text
编辑 → GUI/JS 生成有效快照并标 generation
    → 对象写 staging，校验后进入 objects
    → BEGIN IMMEDIATE
    → 写实体修订、引用、提交、任务结果
    → COMMIT
    → 发回 committed_generation
    → UI 仅在当前 generation <= committed_generation 时显示已保存
```

保存期间用户继续编辑会增加 generation；旧保存完成不能清除新编辑的 dirty 标志。撤销/重做也产生新的内容修订，不能擦掉已发送的历史。纯选择/平移相机不增加内容代次。

文档和脑图均采用 `flush(request_id) -> snapshot + editor_sequence` 确认；3 秒内未得到有效响应时保留最后已确认草稿并提示该页面仍待保存，不能用空文档代替失败结果。不要通过后台线程调用 `store_to_scene()` 读取 Qt 对象。

### 6.3 原子性与耐久性

元数据使用 SQLite WAL，初始采用 `foreign_keys=ON`、`synchronous=FULL`、有界 busy_timeout。WAL 不是备份；导出不能只复制一个打开中的数据库文件。SQLite 对 WAL 的共享内存及同机要求见[官方说明](https://sqlite.org/wal.html)。

大资源写入采用同卷 staging：流式写 `.partial`、flush、`os.fsync`、重新校验 hash、原子重命名为最终对象，再提交引用。若对象已存在必须验证大小/摘要，不能盲信同名。对象先落盘而事务失败产生孤儿，后续可回收；反过来先提交引用会产生丢失内容，禁止。

Windows 电源故障下的耐久性仍受文件系统、驱动和硬件缓存影响；必须通过进程强杀与虚拟机断电测试确定可声明的恢复边界。应用承诺只针对已确认成功的提交，不承诺尚未输入到编辑器快照中的每个按键。

Checkpoint 在空闲或 WAL 达配置阈值时尝试，被读者占用时延期；初始阈值可设 64 MiB，最终以测试决定。跨库/退出前停止新任务，关闭读连接，再做有界 checkpoint。Checkpoint 失败不等于已提交事务丢失，不能因此自动重建数据库。

### 6.4 用户看到的保存状态

状态仅用“有未保存更改”“正在保存…”“已保存 14:32:18”“保存失败：磁盘空间不足”。同步状态独立显示“尚未发送/等待回执/已接收”，不能把“已保存”写成“所有电脑已更新”。

Ctrl+S 不弹无意义成功对话框。失败给出“重试”“另存工作库”“查看恢复位置”；没有写入权限时可只读打开。库磁盘被拔出后停止写入，保留当前已确认内存草稿到设备恢复目录，并显示它是未合入原库的恢复副本。

## 7. 后台任务、导入、缓存与退出

### 7.1 一套可恢复任务队列

`queued → running → waiting_resource / retry_wait → succeeded / failed / canceled`。任务状态和已完成资源哈希落入 jobs 表；重启时将无进程持有的 running 转回可恢复状态。任务不能仅存在于 QTimer/QThread 内存对象中。

优先级：交互保存 > 用户当前导入/桥接 > 更新包导入导出 > 缩略图 > 搜索索引 > 垃圾回收。首版建议单 SQLite writer、2 个文件 I/O worker、1 个高内存图像转换 worker；配置应根据机器性能调整，不同时解码上千张 8K 贴图。

失败采用有界退避；权限错误、版本不兼容、缺依赖、用户取消不自动反复重试。任务面板保留错误码、可读原因、重试操作与摘要。日志默认不记 token、原始文档正文或完整敏感路径。

### 7.2 导入一个材质文件夹

1. 先枚举与分组，显示将生成几个材质、每组包含哪些贴图、总大小及需要的磁盘空间。
2. 用户修正槽位；未知贴图作为附件保留。没有命中识别规则的文件不能静默丢掉。
3. 流式复制原文件到持久 staging，同时计算 SHA-256；源路径变化/断连则该组失败，不发布不完整材质。
4. 重新核对源文件尺寸和时间，必要时重新读取，防止复制过程中外部编辑器修改。
5. 发布不可变原始对象并在一个事务中建立该组资产、贴图引用和初始修订。
6. 导入完成后才能显示持久可用，缩略图和球预览可继续后台生成。
7. 默认按材质组原子导入；批次中每组有独立结果，用户可选“全批次成功才发布”。

用户取消时在组边界停止，未提交临时文件清理；已提交组保留并可撤销。撤销导入形成软删除，不立即物理删除资源。

### 7.3 原始数据、派生数据、备份分别处理

原始文件与实体修订是权威内容。缩略图、ORM 派生图、预览球截图和 AI 索引是可重建数据，记录生成器版本与输入 hash。缓存损坏只能重建缓存，不能改变材质原始贴图。

备份默认每 10 分钟有变更时备份元数据，日保留 7 份、周保留 4 份，迁移前强制生成一份。备份引用的对象纳入 GC 根集合；只备份 SQLite 又允许对象被删，不构成有效备份。缓存可设独立容量上限，退出时不强制生成预览。

关闭窗口默认结束 Prism 进程，已排队任务写入磁盘后下次续跑。用户可主动启用“关闭后在托盘继续任务”；该模式明确提示，只有真正退出时停止进程，不默认安装系统服务。

## 8. 增量更新包：完整协议与交接交互

### 8.1 一个 100 GB 库的真实例子

A、B 电脑都通过完整包得到共同基线 C0。A 今天移动 20 个画布图元、修改 1 篇文档、删除 1 张脑图、新增 12 MB 贴图并修改一个材质。更新包包含这些实体的新修订、删除墓碑、必须的历史关系和新增贴图；原有 100 GB 不重传。

新增 2 GB 的不同素材，包至少需要携带这些新增有效资源，压缩不保证明显变小。仅调整粗糙度参数不重传旧贴图。修改 PNG 后首版携带整个新 PNG；不能把“整文件增量”宣传成“只传 PNG 改过的像素”。

### 8.2 发送方必须知道相对谁的基线

“上次导出”不代表对方已收到。peers 中只有“确认接收的提交集合/回执”才能推进 confirmed_heads。

首版默认采用**相对共同基线的累积更新包**：A 导出 U1 后对方没导入，A 再导出 U2，U2 仍覆盖 C0 至当前的完整必要变化。这样 B 只收到 U2 也能更新。收到 B 的回执后才把基线推进到 B 确认拥有的状态，让后续包缩小。

若没有任何已知共同基线，不允许猜测对方拥有资源；提示首次导出完整包，或让对方生成状态回执。已拥有 C0 的用户无需每次重新选择 C0 文件。

UI：库菜单 → **发送更新** → 选择“家里电脑 / 另一位成员 / 相对初始完整库” → 显示“自对方已确认版本以来：2 张画布、1 篇文档、新增 5 个资源、28 MB” → **生成更新包**。无回执时显示“包含尚未确认收件的变化”，不打断导出。

### 8.3 包结构与 manifest

```text
update.prismdelta             # ZIP64，完成前使用 .partial 后缀
  manifest.json
  commits/<commit_id>.json
  revisions/<revision_id>.json
  objects/sha256/<hash>
  inventory.json
```

```json
{
  "format": "prism.delta",
  "format_version": 1,
  "min_reader_version": 1,
  "package_id": "uuid",
  "library_id": "uuid",
  "origin_replica_id": "uuid",
  "required_base_heads": ["C0"],
  "target_heads": ["C9"],
  "created_at": "2026-10-19T06:30:00Z",
  "mode": "cumulative",
  "revision_count": 42,
  "object_count": 5,
  "uncompressed_bytes": 29360128,
  "inventory_sha256": "sha256-of-canonical-inventory"
}
```

示例 C0/C9 是说明用标识，不是实际 hash。协议实现必须定义 UTF-8、确定性 JSON 序列化、字段顺序/数字格式、SHA-256 输入范围。清单摘要不包含自身，外层 package_digest 在回执或传输层计算。文件 hash 保证完整性，不等于证明发送者身份；需要可信发送者时增加签名和显式信任机制。

inventory 列每个路径、未压缩大小、hash、codec。首版元数据用 deflate，大部分已压缩资源用 store。ZIP64 与流式读写必需，不把整个更新包读进 RAM。

### 8.4 导出算法

1. 提交当前编辑，固定目标 head_set；导出过程中允许继续编辑，但新的修改进入下一包。
2. 读取目标快照和所选 peer 已确认基线，用 DAG 祖先关系计算必要提交和实体修订。
3. 收集接收方未被确认拥有的资源闭包；不能因“曾经导出过”就排除一个对象。
4. 临时持有快照与资源 pin，防止 GC 在打包时清理对象。
5. 写目标盘临时文件，校验清单与每个对象，然后发布最终文件；失败保留任务检查点，绝不留下可误识别为完成的包。
6. 写“已导出、待确认”，不修改 peer 的 confirmed_heads。

对方确实已有但未确认的对象可能重复传输，这是可靠性优先的首版取舍。状态回执可减少冗余；不能为了包小而产生隐含缺件。

### 8.5 接收算法与原子边界

1. 双击、拖入 Prism 或文件菜单打开更新包，读取 manifest 并查询本机库注册表。
2. 检查 library_id、格式版本、大小限制、路径、hash、基线与当前库状态。当前有编辑时先提交并固定 local_head。
3. 读取该库以独占写任务方式执行更新，编辑可在预检阶段继续，但应用前必须重新确认 local_head；变化则重新生成合并计划。
4. 解包对象到 staging，流式校验，再发布不可变对象。核对目标状态引用的对象在本机或包中全部存在。
5. 计算三方合并/冲突计划，在一次元数据事务中写入来历、修订、墓碑、冲突记录、合并提交和 received_packages。
6. COMMIT 成功后刷新受影响的资源树和页面，保留当前选择/滚动位置，后台重建必要索引。
7. 给出“已更新 / 已更新，3 项待处理冲突”，可导出回执。只有落盘成功才生成确认状态。

对象阶段断电：旧 head 不变，最多多出孤儿对象。事务阶段断电：SQLite 恢复到提交前或提交后的一致元数据。应用后的 UI 刷新失败不能再次重复写入包，重启按 received_packages 识别已提交结果。

### 8.6 接收交互与特殊情况

| 情况 | 必须表现 |
|---|---|
| 正常已有库 | “场景设计库有更新，来自公司电脑” → 应用更新，不创建第二个项目 |
| 库未打开但已注册 | 定位同 library_id，确认后打开/更新该库 |
| 本机没有对应库 | “需要先接收完整库”，可定位已有库；不能要求同时导入大文件和小文件 |
| 库移动了 | 让用户定位一次，核验 library_id 后修复注册记录 |
| 同一 library_id 有两份本地副本 | 展示完整路径和最后打开时间，选目标并记住；不能自动猜 |
| 重复同包 | 提示“此更新已应用”，不重复创建实体 |
| package_id 相同但内容摘要不同 | 拒绝，标记 ID 冲突或文件损坏 |
| 收到较旧包 | 引入尚缺历史，绝不回滚较新的 current revision |
| 收到乱序包 | 累积包可从已知基线应用；窄差量缺前置则等待缺件或请求新累积包 |
| 基线已被遗忘/压缩 | 请求状态回执并重建累积包；无法证明共同历史时要求完整快照 |
| 程序版本太旧 | 明确升级要求，不尝试忽略未知关键字段 |
| 目标盘空间不足 | 应用前估算新增对象+临时文件+元数据余量，保留旧库 |
| U 盘 FAT32 单文件限制 | 导出前检查；可选显式分卷或换盘格式，不能写到 4 GB 才静默失败 |

“插 U 盘即发现更新”放二期，默认不全盘扫描 U 盘、不自动应用文件。微信等聊天软件的传输大小限制不能写死；允许用户按需要选择包分卷大小。分卷的每卷包含 package_id、序号、总卷数与 hash，全部到齐才应用。

### 8.7 回执不等于第三方资源全量盘点

回执只记录接收方在某个成功应用之后拥有的 head_set 和被校验的对象集合摘要，采用 `receipt_id` 防重。离线回执可以双击/拖回 Prism。发送方验证同库与来源后推进该 peer 基线，不能推进其他接收者的基线。

若接收方后来手动删掉 objects 文件，回执已不能代表当前健康状态；此时通过完整性检查和补件清单修复。普通 UI 不需要用户理解 DAG、hash 或基线文件。

## 9. 双方修改、删除、冲突与历史回收

### 9.1 首版合并规则

采用共同祖先 B、本地 L、传入 R 三方比较。不用系统时间或“最后收到者获胜”。

| B/L/R 关系 | 处理 |
|---|---|
| L=B，R 有变 | 应用 R |
| R=B，L 有变 | 保留 L |
| L=R | 去重，不产生冲突 |
| 改不同实体 | 自动合并，例如 A 改文档、B 改材质 |
| 同一材质双方改动 | 首版保留两个版本，禁止按字段盲拼贴图与参数 |
| 同一文档/脑图双方改 | 保留本地当前版及传入冲突版，支持并排对照、另存为、手工决定 |
| 同一画布不同 placement | 可自动合并独立 placement 的修改 |
| 同一 placement 双方改 | 保留冲突候选，用户选本地/传入/复制为新摆放 |
| 一边删除、一边修改 | 保留修改分支并标记“删除与修改冲突”，不得直接清空 |
| 目录循环或父节点已删除 | 不能按非法层级提交，暂放“待整理内容”并给出修复选项 |

冲突的传入资源照样校验保存，引用计入 GC 根。整包可以提交为“已应用，包含待决冲突”，但回执必须描述实际保留的历史/head_set；不能假装传入版覆盖了全部本地状态。

冲突解决生成新修订，其父修订包含双方，后续同步不会把相同冲突再弹一次。双方都独立解决又产生不同结果时属于新的分叉，照规则处理。多共同祖先没有确定实现时保留版本交给用户，不能随意选一个祖先误合并。

### 9.2 删除、恢复与“清空回收站”

删除首先写墓碑，回收站默认保留 30 天；用户恢复产生新修订，原 ID 保持。删除库内材质不删除已发往 UE 的项目资产，删除 UE 材质也不自动删除 Prism 库资产。

GC 标记根包含：所有当前头、未解决冲突、保留的历史、回收站、有效元数据备份、进行中导出/桥接任务与未超期 peer 基线。只删除不可达对象，先移至隔离区，经过下一次成功完整性检查及安全期后才物理移除。

“清空回收站”前显示可回收估计值和受历史引用影响的实际值。墓碑不是资源数据；旧设备可能重现已删除内容，所以不能跟着图片一起随便清除。设备退役后可显式忘记该 peer，并生成新协议检查点；旧设备再加入需要重新接收快照。

历史保留策略应控制体积：默认近 30 天可恢复版本，重要版本可固定；保留祖先摘要/检查点以证明同步关系。清理内容前确保不破坏已确认的基线更新能力，否则下次接收必须明确要求完整重新建立基线。

## 10. 材质库本身怎么用

### 10.1 材质种类

| 类型 | 本轮 Prism 能做什么 | 范围 |
|---|---|---|
| `pbr_material` | 编辑贴图槽位、常用参数、预览球、版本、导出原贴图 | 首版正式支持 |
| `material_source` | 保存源工程为附件；不解析的格式明确只归档 | 普通附件能力支持，调用外部工具放后续阶段 |
| `ue_native_material` | 仅预留 kind/extension 位置 | 延期，不提供本轮导入、解析或预览承诺 |

普通图片不能因为文件名包含 normal 就自动替换成材质。用户拖入多个关联贴图时，显示“作为图片导入 / 组成材质”的选择；材质入口拖入则默认分组预览。

### 10.2 PBR manifest 最小规范

```json
{
  "schema": "prism.pbr/1",
  "entity_id": "uuid",
  "revision_id": "immutable-revision-id",
  "name": "旧铜",
  "workflow": "metallic_roughness",
  "blend_mode": "opaque",
  "normal_convention": "DirectX",
  "tile_size_m": [1.0, 1.0],
  "maps": {
    "base_color": {"blob": "sha256", "color_space": "srgb"},
    "normal": {"blob": "sha256", "color_space": "linear", "convention": "DirectX"},
    "orm": {"blob": "sha256", "color_space": "linear", "channels": {"ao": "r", "roughness": "g", "metallic": "b"}}
  },
  "parameters": {
    "base_color_factor": [1, 1, 1, 1],
    "roughness_factor": 1.0,
    "metallic_factor": 1.0,
    "normal_strength": 1.0,
    "uv_scale": [1, 1],
    "uv_rotation_degrees": 0,
    "opacity_cutoff": 0.5
  },
  "source": {"provider": "user", "url": null, "license_id": "user-declared"}
}
```

hash 占位符在实际文件中必须为完整摘要。缺贴图时使用规范定义的中性常量：BaseColor 白、Roughness 0.5、Metallic 0、AO 1、Normal 平面；factor 与默认常量的合成公式写进协议，不能两个渲染端各猜一套。

### 10.3 槽位、色彩空间与识别

| 槽位 | 常见后缀候选 | 解码/UE 设置 |
|---|---|---|
| Base Color | basecolor、albedo、diffuse | 通常 sRGB；diffuse 来自 specular 工作流需提示确认 |
| Normal | normal、nor、n、normaldx、normalgl | 线性、Normal 压缩；明确 DirectX/OpenGL，不对 JPEG 颜色纹理做法线猜测 |
| Roughness | roughness、rough、r | 线性；Glossiness 需显式 `roughness=1-glossiness` 转换 |
| Metallic | metallic、metalness、m | 线性、mask/data |
| AO | ao、ambientocclusion | 线性、mask/data |
| ORM/RMA | orm、rma、arm | 各通道映射必须写入 manifest，不能仅按文件名永久认定 |
| Height | height、displacement、disp | 线性、高位深保留；首版归档/平面预览，UE 默认不自动启用位移 |
| Opacity | opacity、alpha、mask | 线性；opaque/masked/translucent 是显式材质选择 |
| Emissive | emissive、emission | manifest 声明色彩空间与强度；EXR 不当普通 sRGB PNG 处理 |

一个材质内有 1K/2K/4K 多套时，以 resolution_variant 分开，不随机混用。用户手工选择优先级高于自动识别。相同哈希不意味着相同用途：同一个 PNG 可作为颜色也可作为数据槽位，导入 UE 时其纹理设置不同，不能错误共用一份 UTexture 设置。

首版不自动支持 UDIM 和虚拟纹理：识别到 `.1001` 等序列时归组并标“需要 UDIM 支持”，可归档/导出原文件。二期通过 capabilities 明确开启，避免只导入第一块假装完整。

### 10.4 与 Prism 现有交互适配

左侧现有画布、文档、脑图结构保留，在资源浏览的类型筛选中增加“材质”，并提供材质集合入口；是否将材质做独立树 section 由 ResourceTreeService 统一支持，不能在 UI 写一个绕开服务的假目录。

中部继续使用素材卡片/列表，材质卡显示球预览、名称、分辨率、贴图数、类型与资源完整性状态。选中材质后右侧 detail_panel 切换为预览、贴图槽位、参数、版本与来源。常用按钮为“编辑材质”“替换贴图”“导出贴图”，兼容现有颜色、标签、评分筛选；当前界面不放置未实现的 UE 按钮。

双击材质打开详情预览；拖到 Prism 画布创建一个引用同材质 ID 的卡片，而不是复制全部贴图。右键提供复制参数、导出贴图、版本历史、移动、删除。批量选中支持共享标签/导出；不同材质参数混合值显示“多个值”，不能用第一项覆盖其余内容。

从 Prism 拖到普通外部软件默认导出用户选定的普通文件；拖到 UE 内容浏览器不能靠普通 QDrag 自动创建 UE 材质图。首版必须通过插件按钮/收件面板触发，跨进程拖放留二期验证。

快捷键复用 Ctrl+S、Delete、Ctrl+Z，不占用现有截图、贴图、画布缩放快捷键。搜索结果可跳转材质及引用画布，删除被文档/脑图引用的材质时给出引用情况与保留占位选项。

### 10.5 预览与性能

Three.js 在 QtWebEngine 本地页运行，复用 Prism 已有 WebEngine 基础设施；固定球体、平面、立方体和用户模型预览。原始法线约定先转换为预览渲染器需要的方向，不能修改原文件后再导入 UE 导致双重翻转。

默认使用 1K 派生贴图预览，选中时按需升到 2K；8K 原图不直接全量常驻。材质切换后释放纹理/GPU 资源；不可见时暂停渲染，滚动列表仅显示静态缩略图。GPU 不可用时显示贴图拼图/已有球截图，并明确“3D 预览不可用”。

环境光初版用程序生成或许可证明确的内置资源；下载的 HDRI 同时记录来源和许可。Web 页只允许访问当前授权对象，无任意 file:// 访问、远程脚本或 CDN 依赖。Three.js 的显示用于辅助判断，不承诺与 UE 光照、色调映射和 Substrate 像素一致。

### 10.6 在线材质来源

首版优先本地文件夹和 UE 回收；Poly Haven/ambientCG 可作为后续在线源。Poly Haven 资产声明见[其许可证页](https://polyhaven.com/license)；资产许可不自动等于 API 任意抓取许可。供应商 API 条款、限流、缓存和归属分别核对，不能直接用一个旧 loader 批量镜像全站。

在线下载必须经用户选择，展示预估体积、分辨率与来源；失败可续传并核对摘要。Fab、Megascans、付费素材默认不做公开再分发，来源权利标记跟着完整包/更新包/UE 回收走。代码的 MIT 许可不覆盖第三方材质图片。

## 11. Prism 的模块怎么改：逐文件实施

### 11.1 新增目录与职责

```text
prism/
  library/
    models.py                 # 纯数据实体、修订、placement、材质 DTO
    repository.py             # 统一读写接口，不依赖 QWidget/QGraphicsItem
    schema.py                 # 新库 schema/migrations，与旧 .prism schema 分开
    sqlite_repository.py      # 事务、索引、元数据持久化
    object_store.py           # streaming、hash、staging、发布、读取、对象 pin
    save_coordinator.py       # generation、flush barrier、单 writer
    commands.py               # 新内容变更命令；适配现有 undo
    draft_registry.py         # 未命名草稿、恢复入口、最近工作库
    migration.py              # 旧 .prism → 新库，持久检查点
    maintenance.py            # health、备份、mark/sweep GC
    job_queue.py              # 可恢复任务与队列调度
  materials/
    models.py                 # PBR manifest 与字段约束
    detector.py               # 贴图候选分组、置信度、手工覆盖
    import_service.py         # 导入预检、持久资源、发布材质
    preview_service.py        # 生成预览输入和缩略图任务
    derived_maps.py           # 通道组合、法线翻转等派生，不覆盖原文件
  exchange/
    manifest.py               # 格式版本、规范序列化、清单校验
    snapshot.py               # 固定提交快照与引用闭包
    full_package.py           # 完整导出、首次接收、share 导入
    delta_package.py          # 累积更新、幂等收件、回执
    merge.py                  # DAG/祖先、三方计划、冲突记录
    path_safety.py            # 解包路径与体积限制
  widgets/
    material_inspector.py     # 复用 detail_panel 入口
    material_import_dialog.py # 槽位/分组确认
    library_status.py         # 保存状态、任务入口
    exchange_dialog.py        # 发送/接收摘要与进度
    conflict_panel.py         # 冲突并排与解决
  assets/material_preview/
    index.html
    viewer.js                 # 只接收协议数据
    vendor/                   # 发布时从锁定的 Three.js 源码区复制
```

目录名是实施建议，可按现有风格调整，但职责边界不能合并回一个超大的 view.py。新代码先通过 repository 接口使用旧存储适配器，随后接入新库；旧 `.prism` 不需要为新材质实体硬塞额外字段。

### 11.2 必改的既有文件

| 文件 | 具体改动 | 必须保留的行为 |
|---|---|---|
| `__main__.py` | 新库打开/恢复入口、保存协调器初始化、退出屏障、文件关联分发 | 旧文件启动、现有截图热键与多窗口生命周期 |
| `view.py` | 新库保存委托给 SaveCoordinator，dirty 由 generation 适配；旧格式仍走 do_save | Ctrl+S、另存为、撤销、旧格式错误提示 |
| `fileio/__init__.py` | 格式识别与入口路由；把存储类型显式传入，拒绝未知版本 | 老 `.prism`、图片/视频/文档导入能力 |
| `fileio/sql.py` | 隔离 legacy reader/writer，提供只读流式迁移访问，不承担新库同步 | 原始文件可取回、错误图元的原始资源可保留 |
| `source_store.py` | 增加资源引用抽象，legacy SQLite ref 与新 blob ref 统一读取 | 不能把旧工程的错误资源转换成空图 |
| `asset_records.py`、`items.py` | entity_id 与 placement_id 适配，材质引用卡；save_id 仅作旧格式本地键 | 现有图片裁切、翻转、置顶、虚拟化 |
| `project_catalog.py` | 增加材质类型、资源完整性查询；优先元数据查询 | 全项目统计不重复、被删除素材不计入活跃列表 |
| `workspace_service.py` | 材质集合/文件夹统一校验、移动/删除/恢复事务 | 不形成目录循环、相同命名规则、稳定 ID |
| `window_workspace.py` | 页面切换 flush、库级服务生命周期、恢复位置注册 | 返回原页面、拖动层级、选中状态 |
| `widgets/media_library.py`、`resource_tree_model.py` | 材质入口、计数、局部刷新、冲突和待整理入口 | 同一查询口径，不为了计数加载原图 |
| `widgets/asset_browser.py` | 材质卡片、按类型/完整性筛选、批量导出 | 普通素材浏览临时摆放不污染真实画布 |
| `widgets/detail_panel.py` | 按资产类型切换 inspector，材质扩展独立文件 | 图片信息、评分、标签、原文件导出 |
| `document_panel.py`、`mindmap_panel.py` | 明确异步 flush ACK 与 DTO，附件实体化 | 文本样式、脑图主题和折叠状态可回读 |
| `commands.py`、`undo.py` | 内容命令与修订提交桥接；撤销不直接删历史 | 用户一次操作对应一次可理解的撤销 |
| `config/settings.py`、`widgets/settings.py` | 新库保存间隔/缓存/备份/任务并发设置，显示默认值 | 旧格式设置迁移不改变已保存用户偏好 |
| 打包脚本及 `pyproject.toml` | 显式包含新 HTML/JS/LICENSE、manifest schema，新增依赖锁版本 | 安装包完全离线运行，不在用户首次启动 pip/npm install |

新增公开接口时不要让 Widget 直接执行 SQL、删除 objects 文件或判断冲突先后。导入、保存、更新和材质编辑共用同一套写入服务，避免四条互相绕开的保存路径。

### 11.3 最小服务契约

```text
repository.open_library(path, readonly=False) -> LibraryHandle
repository.read_entity(entity_id, revision=None) -> immutable DTO
save.capture(editor_sequences, target_generation) -> ContentSnapshot
save.commit(snapshot) -> CommitResult(commit_id, committed_generation)
save.flush_barrier(target_generation, timeout) -> success | pending | error
objects.put_stream(stream, expected_hash=None) -> BlobRef
materials.plan_import(files, overrides) -> MaterialImportPlan
materials.apply_import(plan) -> persisted JobHandle
exchange.plan_export(peer_id, snapshot) -> PackagePlan
exchange.inspect_package(path) -> PackageInspection
exchange.plan_apply(inspection, local_heads) -> MergePlan
exchange.apply(plan) -> ApplyResult
maintenance.check(level=metadata|objects) -> HealthReport
```

所有写操作返回 request_id/commit_id，失败带稳定错误码；UI 不按异常文本猜状态。取消使用任务状态，不靠强行终止写线程。进入数据库事务后取消延迟到事务结束，避免半事务处理。

## 12. 旧工程怎么迁移、升级失败怎么办

### 12.1 不破坏旧文件的迁移流程

1. 只读打开老 `.prism`，检测 application_id、user_version、表与资源数量，生成迁移预检报告。
2. 选择新库位置，估算原始资源总量、额外空间与目标盘文件系统；预检失败不修改旧文件。
3. 在 `<目标>.migrating/` 建新 schema，记录 source_fingerprint 与 migration_job_id。
4. 旧 items 行号映射为新稳定 UUID；映射表落盘，续跑复用同一 ID，不反复新增实体。
5. 从 sqlar 流式抽取原始内容，计算哈希并去重；大 Blob 优先增量读取，不能 SELECT 全部 Blob 一次装进内存。
6. 转换画布、文档、脑图、标签、文件夹、已删除记录、嵌入附件与错误占位，并重写内部引用。
7. 建立初始提交，验证每个引用能解析、计数一致、关键样本原文件 hash 一致。
8. 生成迁移报告并原子发布新库，更新本机最近工作库记录，再打开新库；原 `.prism` 原位保留。
9. 迁移中断后可继续或放弃临时目标，不能把 `.migrating` 误列为正常库。

源文件发生变化时指纹失效，要求从新快照重新迁移；不能悄悄把不同版本的页面与资源混进同一初始提交。源库若损坏先导出可恢复范围和错误清单，不自动把它重建为空库。

### 12.2 导出回老格式

新库里的老类型内容可以“导出兼容 `.prism`”，但 PBR 材质、版本历史和更新记录没有完整旧格式表达，导出前列出降级损失。默认推荐完整 `.prismpack`。旧版程序打开新格式时应明确拒绝，不能通过复用扩展名诱导旧程序覆盖新库。

### 12.3 新库 schema 升级

维护 schema_version 与 min_reader_version，打开前检查。升级先做在线元数据备份并 pin 对象；短迁移在事务中进行，长迁移使用影子表和恢复检查点，成功才切换。迁移失败关闭写入，保留原备份、迁移状态与日志；“重装软件”不能是唯一恢复方式。

应用降级不等于数据库降级。旧程序不能写它不理解的库；允许只读导出通用资源。更新包格式版本独立于数据库 schema 版本，通过显式转换层接入，避免每次 schema 微调都迫使所有设备重建 100 GB 基线。

## 13. 交互页面与具体文案

### 13.1 首次打开与主窗口

欢迎页提供“新建工作库”“打开工作库”“打开旧工程”“接收完整包”“恢复未命名工作”。创建时只问名称和位置，默认开启自动保存。内部对象存储、hash、数据库配置不进入新手流程。

```text
场景设计库                                  已保存 14:32:18   [任务 2]
左：现有资源树         中：画布 / 素材 / 材质浏览       右：当前对象属性
  画布                 搜索… [类型：材质]             旧铜
  文档                 [球预览] [球预览]             [球体/平面]
  脑图                 旧铜      水泥                 BaseColor  已就绪
  材质                 4K·5贴图  2K·4贴图             Normal     已就绪
  标签                                                [替换贴图]
  回收站                                              [导出贴图]
库菜单：[发送更新] [接收更新] [导出完整库] [检查与修复]
```

此为信息布局示意，需沿用 Prism 的 ui_tokens、字号、间距和现有深浅色主题，不再引入一套网页管理后台风格。局部改造右侧 inspector，普通画布用户不必每次看到材质槽位。

### 13.2 “导出完整库”和“发送更新”不能混淆

完整库导出显示“用于首次搬到另一台电脑或备份，预计 100 GB”；发送更新显示“用于已有相同工作库的电脑，预计 28 MB”。范围选择提供整库/选中集合；选中集合跨库发送走 share 模式，不能写“整库已同步”。

用户生成文件后可“打开所在文件夹”，发送由用户自己使用 U 盘或聊天软件完成。本轮不自动发送消息，不集成账号/联系人。

### 13.3 恢复、失败、冲突界面

异常启动显示：“找到上次未正常关闭的工作：角色设定，最后成功保存 14:32。打开恢复内容 / 查看位置。”健康检查发现对象缺失时显示哪些内容受影响，允许从更新包、完整包或原文件补齐。

冲突面板列“内容名称、类型、本地修改摘要、传入修改摘要”，逐条支持保留本地、保留传入、保留两份。对文档/脑图“保留两份”生成新实体并修复相关链接，而不是两个项目共用相同 ID。

后台错误优先显示在任务中心和状态栏；会导致数据丢失的退出失败可用对话框。缓存重建失败不弹阻塞式对话框，也不改变保存状态。

## 14. 必须提前覆盖的异常与边界

| 场景 | 设计要求 | 验证方式 |
|---|---|---|
| 保存时又编辑 | generation 边界清楚，旧完成回调不能清 dirty | 人为延迟提交后连续输入 |
| 同时 Ctrl+S、自动保存、导入 | 单 writer 排队和相同目标代次合并 | 并发触发三个入口 |
| 突然杀进程 | 重启可恢复最后已确认提交，未完成任务可识别 | 在资源发布/BEGIN/COMMIT 后分别终止 |
| 系统断电 | 有效提交与对象一致，检查点可恢复 | VM 断电演练，记录存储环境 |
| 磁盘满/写保护/断盘 | 返回真实失败，保留原库与可恢复草稿 | 注入 I/O 错误、可移动盘测试 |
| 源素材导入途中变化 | 检测源指纹变化并重试/拒绝该组 | 边复制边重写源文件 |
| 工作库打开两次 | 第二个实例只读或切回已打开窗口 | 双启动并同时编辑 |
| 恶意/损坏包 | 拒绝路径穿越、绝对路径、symlink/reparse、超限膨胀、重复路径与 hash 不符 | 构造小体积攻击包集 |
| 大小写/保留文件名 | 库内用 hash 路径，导出显示名进行 Windows 合法化且记录映射 | CON、尾点、大小写重名、中文长路径 |
| JPEG/PNG 解压炸弹 | 读取元信息和像素预算，子进程/worker 有内存上限 | 超大尺寸头、损坏图文件 |
| 相同原图不同色彩用途 | 原始 Blob 可复用，派生/纹理设置按用途分别标识 | 同图进颜色与 mask 槽位 |
| 相同库独立复制后分叉 | 明确同库副本/独立库，不能只改路径就假装新库 | 两份副本不同编辑后互传 |
| 手工删除 objects | 标缺件，不自动把相关实体删掉 | 校验后用补件包修复 |
| 材质替换贴图后撤销 | 旧对象仍由历史引用保留 | 替换、撤销、重启、再恢复 |
| 预览显卡崩溃 | 降级静态预览，数据保存继续可用 | 禁用 GPU / 远程桌面 |
| 卸载与升级 | 不清理用户库和草稿，保留可查恢复位置 | 安装卸载回归 |
| 库路径更改/盘符变化 | 用 library_id 重新定位，引用不用绝对路径 | 改盘符/移动整个库 |
| 完整导出时继续编辑 | 导出固定快照，有明确截止版本 | 导出中改文档并核对目标快照 |

包校验的体积阈值必须在解包前后都检查，不能只信 manifest。默认设置最大元数据文件体积、实体数、总展开体积和压缩比；大库合法超限时由用户明确放宽当前任务，不能直接禁止 100 GB 库。外部数据不得使用 pickle、eval 或自动执行包内脚本。

本地包默认不加密，发送窗口说明内容包含原始素材；若用户选择密码加密，必须采用成熟容器/库并单独评审格式与恢复方案，首版不自创加密。不因更新包 hash 正确就认为发送者可信。

## 15. 大库性能、分块与空间策略

### 15.1 首版先做整文件 CAS

先用 SHA-256 的不可变文件对象去重，元数据增量按实体修订。导入时计算一次 hash，之后未变化的资源不每次保存重读；完整性检查是独立任务。普通小修改不会遍历 100 GB 原文件，也不会重新写一个 100 GB 巨型数据库。

图片列表和材质列表分页/虚拟化读取，缩略图按可见区生成。文档和脑图按页面加载，100 张画布不同时物化全部 Qt 图元。清晰度、图元数量与资源字节数分别统计，不能拿某个小样本的快速加载推断百 GB 性能。

### 15.2 二期是否做内容定义分块

当实际发现大型原生资产/视频频繁局部修改，且整文件重传成为主要成本时，才评估 fastcdc。初始实验参数可用最小 1 MiB、目标 4 MiB、最大 8 MiB；须固定算法版本与参数，资源 manifest 保存有序 chunk 列表及整个文件摘要。

压缩文件变化可能使大量块变化，因此 FastCDC 不保证对 PNG/JPEG/UE 包总能节省流量。验证同一数据集的去重收益、CPU/内存、文件数量与恢复速度后决定是否上线。首版不开启文件内部块，避免一次同时实现新库、同步、分块三套复杂协议。

对象过多时可二期做 packfile，但目录索引、校验和 GC 必须可恢复；不能把 packfile 改造变成再次整库重写。原始对象格式与压缩 codec 有版本字段，未识别算法拒绝写入并允许导出已知资源。

### 15.3 性能验收目标与测量口径

以下是待测目标，不是本轮已完成的跑分。建议基准机：Windows11、8 核 CPU、32 GB RAM、本地 NVMe；另测 16 GB/集显及机械盘，报告硬件和冷/热缓存。

标准大库样本至少包含 100 张画布、1 万张图片、100 篇文档、100 张脑图、1000 套材质，总原始资源约 100 GB；同时加入多处引用、回收站与冲突版本。文件总数、分辨率分布、每套贴图数量、正文长度与视频占比均写入基准报告，避免只凑字节数。

| 场景 | 初始验收目标 |
|---|---|
| 100 GB、1 万图片、1000 材质库打开 | 元数据首屏 3 s 内可交互，不等待所有球预览 |
| 已加载页面只改一行文字 | 本地 NVMe 停顿后通常 1 s 内显示已保存；不读写大素材 |
| 输入与画布拖动 | 保存过程中 p95 交互阻塞 <50 ms，长任务可取消 |
| 材质库滚动 | 只加载可视卡片，显存随滚动不持续增长 |
| 4K/8K 导入 | 流式处理，内存随 worker 限额而非素材总量增长 |
| 只改布局/文档的更新包 | 无新资源时包主要由实体修订构成；记录实际字节与变化数 |
| 12 MB 贴图替换 | 包包含新增贴图及必要元数据，不能额外重传旧库 |
| 同一包导入两次 | 第二次只校验必要摘要/记录，实体与资源数不增长 |
| 长时间运行 | 8 小时编辑/导入后无队列丢失，缓存与内存有上限 |

如果基准不达标，先记录瓶颈并调分页/任务预算；不得为了首屏时间不加载数据却不给用户资源未就绪状态。

## 16. 明确开发先后顺序与每阶段交付

阶段按依赖推进，不用拍脑袋日期承诺。每阶段代码应可单独回归；未通过阶段门槛时不启动依赖它的数据迁移或同步上线。

### P0：冻结接口与建立基线

任务：确认现有文件格式与恢复行为；收集脱敏旧工程；建立新 repository/DTO 契约；准备 1 万图片/1000 材质合成与真实样本；核对开源源码锁和许可证。记录现有图片、视频、文档、脑图、资源树测试结果，失败先分为既有/新增。

交付：schema 草案、接口契约、基准数据清单、迁移映射规则和开源锁。门槛：每种现有类型都有可回读样本，确认旧文件原始字节可保留。

### P1：新库底座与原始资源持久化

任务：实现 object_store、sqlite_repository、单 writer、任务状态、草稿库与库注册。先只连接图片和少量元数据，验证一个资源多次引用只存一份。实现独占写、只读模式、健康检查和原子对象发布。

交付：可创建/重开 `.prismlib`，导入图片后关机重启不依赖原路径。门槛：对象发布与元数据提交边界故障注入通过；不能“图片还在临时目录就显示成功”。

### P2：保存协调器与全部旧内容接入

任务：改造 view/__main__ 的新库存取路由；文档/脑图 flush ACK；画布 placement 实体化；标签/目录/附件接入；自动保存、Ctrl+S、退出屏障与恢复入口。保留旧 `.prism` 路径。

交付：用户可在新库正常完成当前 Prism 创作工作。门槛：保存过程中继续编辑、撤销/重做、多页面切换、未命名草稿、强杀恢复全部通过，保存失败状态准确。

### P3：旧库迁移与兼容导出

任务：流式迁移、ID 映射检查点、引用重写、迁移报告、元数据备份。补齐错误素材、文档附件、视频原始资源和软删除。原文件不动。

交付：现有真实旧工程可迁移、可重开、资源 hash 可对照。门槛：页面/素材/引用计数一致；中断恢复没有重复实体；格式升级失败可回退。

### P4：完整包与独立分享

任务：固定快照、引用闭包、ZIP64、manifest、hash、流式导入、包预检；share 模式重新分配实体 ID。实现完整包导出到新电脑首次打开。

交付：无原素材路径、无网络的新环境仍能打开完整库。门槛：完整往返后内容/关系/哈希一致；导出中继续编辑不污染固定快照。

### P5：Prism 材质管理与预览

任务：定义 PBR manifest，贴图自动分组与手动覆盖，材质卡、右侧 inspector、参数修订、Three.js 离线预览、派生图缓存、普通贴图导出。先 local-only，不做在线市场和 UE。

交付：导入贴图文件夹 → 组成材质 → 球预览 → 修改粗糙度 → 自动保存 → 重启 → 导出贴图。门槛：原图不被修改、法线与色彩空间正确、无 GPU 可降级、引用同材质不重复拷贝。

### P6：增量交接与冲突处理

任务：实现提交历史、共同祖先、累积包、peer 回执、幂等应用、墓碑、冲突保留与解决。先两设备，再三个以上副本。更新包应用必须通过同一 repository 写服务。

交付：A/B 共有完整库后只传更新包，接收端自动匹配工作库。门槛：跳过 U1 只导 U2、乱序、重复、修改/删除冲突、双向修改和三副本分叉全部有确定结果。

### P7：恢复、回收、大库与发布

任务：GC/历史 pin/备份保留、补件包、升级/降级限制、磁盘/权限/移动库演练、100 GB 性能、安装包离线资源与许可证。完善任务中心与用户帮助。

交付：可发布 Prism 版本与数据修复工具。门槛：没有未解决的数据完整性 P0/P1 问题；大库指标与已知限制公开记录；能在干净 Windows 环境离线安装使用。

### P8：有证据再做的增强

FastCDC/zstd、在线材质源、U 盘更新发现、外部 Material Maker 工作流、加密包和云同步按真实需求分别评估。UE 桥接独立另立方案，不在 P0–P7 中夹带。

最短依赖链为 `P0 → P1 → P2 → P3 → P4 → P5 → P6 → P7`。P5 可以在 P2 之后提前做独立 UI 原型，但正式发布必须建立在可靠持久化上。不要先做漂亮球预览，再让它继续依赖进程临时目录。

## 17. 逐项验收与测试清单

以下是后续开发必须实现的行为测试，不是本轮已经运行过的结果。涉及资源完整性和数据恢复的测试必须比较内容与引用，不能只断言“函数返回成功”。

| 编号 | 输入/操作 | 必须验证的输出 |
|---|---|---|
| SAVE-01 | 新建未命名草稿，导入图片，编辑文档，终止进程 | 重启可恢复文档与原始图片字节，不依赖源文件路径 |
| SAVE-02 | 文档持续输入 30 s，无停顿 | 按最长 5 s 周期提交有效快照，结束后最后内容全部保存 |
| SAVE-03 | 提交 generation=10 时继续改到 11 | 提交 10 完成不能显示 11 已保存，11 最终另行提交 |
| SAVE-04 | JS flush 超时/错误 | 旧内容不被空数据覆盖，当前页面显示待保存/失败 |
| SAVE-05 | Ctrl+S、自动保存、导入同时触发 | 无双写异常、无丢命令，屏障等待正确目标代次 |
| SAVE-06 | 保存中撤销、重做、切页、关闭 | 每次内容状态均有确定提交序列，退出屏障不误清新修改 |
| STORE-01 | 同一 PNG 复制 10 次、放入 3 张画布 | 一份原始 Blob，多个独立 placement，位置互不影响 |
| STORE-02 | 对象已发布后事务回滚 | 当前库不引用无效实体，孤儿可安全回收 |
| STORE-03 | commit 成功后强杀，未执行 checkpoint | SQLite 重启可读取已确认实体和对象 |
| MIG-01 | 带多画布/脑图/文档附件/视频/已删除项的旧工程 | 原文件 hash 不变，迁移后数量与引用对照一致 |
| MIG-02 | 迁移到一半终止再继续 | 不重复 ID/对象/页面，最终报告与一次完成一致 |
| MIG-03 | 迁移前后源文件发生变化 | 拒绝不一致续跑，不能混合两份源状态 |
| PACK-01 | 完整导出后在新路径、断网环境导入 | 所有承诺可用内容均可打开，library_id 与模式一致 |
| PACK-02 | 导出期间持续改文档 | 导出对应固定快照，后续修改只在下一包 |
| PACK-03 | 选中材质与引用文档跨库分享两次 | 可选择复制/去重策略，目标 ID 不碰撞，内部引用完整 |
| DELTA-01 | A/B 共享 C0，A 只修改文字/位置 | 包不携带旧图，B 原库更新，不生成第二库 |
| DELTA-02 | A 导出 U1、继续改后导 U2，B 只收到 U2 | 累积包覆盖遗漏变化与资源，无需 U1 |
| DELTA-03 | B 依次导入 U2、U1、U2 | 无回滚、无重复实体、没有历史复活的删除项 |
| DELTA-04 | 同 package_id 换不同内容 | 明确拒绝并记录错误，不污染已接收记录 |
| DELTA-05 | 没有共同基线或包缺关键对象 | 不部分应用有效 head，给出补件/完整包指引 |
| MERGE-01 | A 改文档、B 改独立材质 | 自动合并，不丢任何一方修改 |
| MERGE-02 | A/B 都改同文档或同材质 | 保留双方版本与资源，用户解决后形成合并修订 |
| MERGE-03 | 一边删除、一边修改 | 不静默删除修改；冲突版本可恢复 |
| MERGE-04 | 三个副本互相乱序传包 | 历史/head_set 一致或明确保留冲突，不按时间覆盖 |
| MAT-01 | 混合 1K/2K、normaldx/gl、ORM/RMA 贴图 | 分组/槽位预览准确，不确定项可手工修改 |
| MAT-02 | 修改粗糙度、替换法线、撤销、重启 | 正确恢复各版本，原图 hash 不变 |
| MAT-03 | 断网且 GPU 被禁用 | 材质元数据/槽位仍可编辑，静态预览降级 |
| MAT-04 | 反复切换 1000 材质/页面 | 不持续泄漏 Web/GPU 资源，缓存可被重建 |
| GC-01 | 删除材质但历史/冲突/备份仍引用贴图 | 不物理删除该对象，恢复可取到原字节 |
| GC-02 | 任务导出时同时触发 GC | pin 保护快照，导出完整，无文件突然缺失 |
| FAIL-01 | 磁盘满、只读、断盘分别发生 | 真实失败提示、旧提交有效、可定位恢复草稿 |
| SEC-01 | 路径穿越、重复路径、损坏摘要、超限解压 | 应用前拒绝，工作库外无写入 |
| RELEASE-01 | 干净 Windows 安装、断网启动 | HTML/JS/许可证齐全，无需下载依赖 |

对应建议测试文件：`tests/test_library_object_store.py`、`test_save_coordinator.py`、`test_library_migration.py`、`test_exchange_packages.py`、`test_exchange_merge.py`、`test_pbr_materials.py`。文件名是实施建议，本轮未创建这些测试。

已有回归至少覆盖 `test_project_load_service.py`、`test_project_catalog.py`、`test_unsaved_triggers.py`、`test_library_canvas.py`、`test_library_regressions.py`、`test_material_features.py`、`test_material_canvas_context.py` 与文档/脑图现有测试。只改文档时不冒充跑过全部程序回归，正式改代码再按阶段运行。

## 18. 已决定、后续实测与延期事项

| 问题 | 本方案决定 | 还需要验证什么 |
|---|---|---|
| 日常是不是一个 100 GB 文件 | 日常托管目录，用户仍只管理一个库；导出可以单文件 | 目录迁移与大库随机 I/O |
| 何时记录变化 | 内容操作触发，短延迟合并，最长间隔检查点 | 慢机器 flush 与 p95 提交延迟 |
| 是录鼠标操作还是存内容 | 保存有效实体快照与修订，手势只记录最终业务内容 | 现有命令/撤销适配 |
| 小包怎么找到大库 | library_id + 本机注册表，不让用户反复导入原版 | 多副本选择、移动路径 |
| 漏了上一个包怎么办 | 默认累积包，按对方确认的共同基线生成 | DAG 遍历和闭包正确性 |
| 两边都改怎么办 | 三方判定，独立实体合并，同实体保留冲突 | 多副本、重复解决冲突 |
| 材质管理拿哪套项目 | Prism UI/数据模型继续自身实现，Three.js 做预览，现有 OpenImageIO 读图 | QtWebEngine 兼容、许可证/离线打包 |
| 是否整仓照搬一个素材管理器 | 不替换现有 Prism；复用具体组件 | 每个组件的源码边界与适配测试 |
| 是否首版就做分块 | 不做，先整文件 hash；FastCDC 二期实测 | 对真实修改后文件的流量收益 |
| 云同步/实时协作/UE | 都延期，协议仅保留 provider 扩展位置 | 另立方案，不作为当前交付前置 |

当前尚未确定产品正式版本号、每阶段工期和新扩展名的最终名称。这些不阻塞 P0/P1 技术实施；具体性能参数在试验完成前必须保持“目标”标记。

## 19. 开始写代码时的第一批任务

第一批只做底座，避免一次改完整个 UI：

- [ ] 新增 `prism/library/models.py` 和 repository 契约，定义 library/entity/revision/blob/placement ID。
- [ ] 新增 `.prismlib` 的 schema、库创建、只读打开、独占写锁与 registry；不修改旧 `.prism` schema。
- [ ] 完成 object_store 的流式导入、持久 staging、hash 校验、不可变发布和 pin。
- [ ] 写一个不依赖 QWidget 的 metadata writer，提交实体、引用和 commit 必须同事务。
- [ ] 实现 DraftRegistry，未命名工程先拥有真实落盘位置。
- [ ] 让一张图片可导入新库、关闭、删除原路径、再重开并导出原始字节。
- [ ] 在对象发布后/事务中/COMMIT 后注入进程中断，检查旧状态和新状态恢复。
- [ ] 通过上述检查后再接文档/脑图 flush 与 SaveCoordinator。

上述第一批通过后再启动 P2；Three.js 可独立验证，但不作为存储底座完成的代替品。第一轮不得边迁移真实用户库边试验未验证的 schema。

## 20. 资料证据与本轮交付边界

主要外部依据已经就近附链接：Three.js、Material Maker、OpenImageIO、FastCDC、restic、MaterialX 等上游及实际 LICENSE；SQLite WAL 官方文档。分享对话作为需求上下文，不作为功能已经存在的证据。

本轮交付的是完整 Prism 改造方案、开源选型和隔离的可复用源码，不是已经完成的 Prism 功能实现。没有把外部 Python 包注册到 Prism 启动，没有改写用户工作库，没有执行 UE 插件构建。下载/校验结果、文件数与快照信息以同目录研究清单和 `third_party/material-library/upstream-lock.json` 为准。

正式开发时将本方案每个阶段对应实现记录与验收报告，报告必须写清测试环境、实际数据规模、失败项和已知限制，不能把计划目标换成“已支持”。
