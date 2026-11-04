# AI 2：模型多材质局部渲染与原生运行时

## 1. 项目与边界

Prism 是 Python/PyQt6 桌面软件，已有 F3D 独立预览进程、材质库和模型库。你只负责多材质显示控制，不重做资产管理。用户要看到原材质，只改指定材质/部位，同时要求法线覆盖、SSS、置换、纯线框。

目录 `C:/Users/admin/Desktop/Prism-main/Prism-main`，Python `.venv/Scripts/python.exe`。先读 `docs/MODEL_MULTI_MATERIAL_FRAMEWORK_20261027.md`。主对话集成，你交可运行端口与运行时，不另设集成角色。

## 2. 当前状态

- components/model_preview.py：Qt 外壳与 F3D 外部 OpenGL 上下文、IPC、相机、窗口前置；先 F3D 后 Qt。
- components/model_worker.py：离屏封面运行时，现有环境版本 2。
- f3d_parameters.py：全模型覆盖，不是局部编辑 API。
- preview_channels.py：只读派生诊断；不证明局部编辑或低延迟更新已实现。
- multimaterial/ports.py：MaterialPreviewPort 是新边界，尚无实现。
- 下载源码：`C:/Users/admin/Downloads/f3d-master/f3d-master`；安装版先核实，既往记录为 4.0.0，不能假设 master 相同。

## 3. 文件所有权

新增实现放 `prism/model_library/multimaterial/runtime.py`、`runtime_protocol.py`、`native/`、`preview_artifacts.py`。原生源补丁、版本锁定和构建说明放任务明确目录，不直接覆盖用户 Downloads 原始源码。测试放 `tests/model_library/multimaterial/test_runtime_*.py`。

共享 contracts/ports 只读，变更提 RFC。现有 components/model_preview.py/model_worker.py/native_protocol 和全局参数入口由主对话装配，你提供小补丁。不能修改 AI 1 存储、AI 3 UI、AI 4 期望。

## 4. B0 核实与可否决实验，先交这一阶段

记录安装版本、Python/Qt/VTK、插件和源码摘要；枚举公共接口。查清 source material、primitive/面组、node 实例与渲染对象的对应关系。不能凭 actor 数和名字认定材质身份。

实验至少包含：三个不同材质；一个材质共享于四个部位；同 mesh 两个节点实例；同名不同材质。只改第二个材质，再仅改共享材质的一处，其他部位必须保持。

优先 F3D 稳定局部端口。若需 C++/绑定扩展，提供最小补丁、Windows 构建步骤和可安装产物。不要私有指针，不在主进程导入另一套 VTK。源码中能 SetProperty 不等于部署成功。

备选派生预览文件或 Babylon 模型端口须比较原材质保真、坐标/层级/单位/实例、加载成本与维护。FBX 转换不能只凭截图；禁止用 GLB 代替原件导出。主对话确定路线后再扩大，失败就交阻断，不造假 API。

交付 B0：每条路线证据、丢失项、可复现命令、最小可安装运行时、推荐及否决条件。这个阶段可以和 AI 1/3/4 并行。

## 5. B1 接稳定映射与能力报告

输入采用冻结 MaterialTarget、MaterialCatalog/State 与 AI 1 提供的来源映射；保持工作材质与源定义区别。按实例使用点进行分配，支持共享材质更新和仅此部位重绑。渲染句柄只留子进程，不进入项目 JSON。

MaterialPreviewPort.capabilities 必须按目标/工作材质实际报告。读取、编辑、分配、拾取、法线、SSS、置换、线框分别核验；不因能加载 glTF 就全部 true。不能支持的属性保留源数据并提供原因，不静默映射到其他效果。

原材质未编辑时保持原效果；新材质解释与材质球参数体系核对。至少核对 RGB/数据图颜色空间、ORM 通道、法线方向、UV 集/变换、双面、alphaMode/cutoff、自发光。

## 6. B2 准备与安装

prepare 在可取消后台执行，产 PreparedMaterialView；install 只在预览进程的所属渲染线程。回执包含 request_id、完整 target、draft_generation 和实际 frame_id。旧代次/切项目/源变化结果必须拒绝。

加载模型一次后优先局部更新；不能每次滑条重开进程或重读全模型。必须重建的情况下记录原因、成本、相机恢复和最后正确画面保留，交性能对照。

失败不清空正确场景后留下假成功。草稿取消恢复已提交材质；准备租约只在 native 释放后回收。close/cancel/crash 有明确资源所有者。主 GUI 不等待原生读取。

## 7. B3 高亮、拾取与通道

高亮/孤立/选择为瞬态，不能写正式材质或封面。拾取回传稳定 binding_id，不回传裸 actor 序号；透明重叠提供候选/明确策略，旋转与拾取不抢手势。

通道按当前有效工作状态读取，支持整模型与明确选中材质范围；缺省/缺失/未支持区别显示。返回完整材质后不残留 diagnostic/unlit/全局覆盖。截图不替代状态还原核对。

## 8. B4 高级效果，用户明确要求

- 法线覆盖：局部正确，方向/强度/UV 不影响其他材质。bump 不冒充 normal。
- SSS：写明实际算法、厚度/尺度/遮罩语义，以真实节点/参数样本验收。薄透光、alpha、玻璃不能计数；来源仅 blend 存 SSS 而目标没保留时如实报告。
- 置换：说明真实几何/细分策略、边界、预算和裂缝处理。切换可恢复；原件、原几何统计与派生统计分开。不是 normal/bump。
- 纯线框：不填充面，区分源多边形边与三角化边；show_edges 不是纯线框。

无法完成的项标阻断及可行后续，不删要求、不把禁用菜单交成完成。扩展确需换渲染路线，先向主对话交具体兼容和部署成本。

## 9. 验收、性能与运行时交付

四格式独立映射对照；非目标材质参数/绑定不变；独立实例不串；取消与恢复正确；原件全部哈希不变。固定相机/灯光做图像对照，并结合绑定和材质值，不能仅看截图。

记录准备/安装/首帧/最大停顿与内存显存，冷暖分开。连续 50 次替换/切换后资源趋稳，缓存/租约释放。1/10/100/1000 材质覆盖，参考机和预算见总方案，与 AI 4 冻结。

默认离屏测试，禁止控制用户鼠标/窗口或抢桌面。需要可见 GUI 的项记未测，交主对话安排；不偷偷启用 `PRISM_TEST_MODEL_NATIVE_UI=1`。

交付版本固定、插件依赖、构建/安装/升级/回退说明、原始证据、最小接线补丁。许可证事实写来源，无法确认不臆测。Windows 单机能部署才算运行时交付，不只交源代码。

## 10. 汇报模板

```text
角色：AI 2；B0/B1/B2/B3/B4 各项状态：
实际运行时/源码摘要/构建版本：
采用路线及被否决路线证据：
修改与新增文件：
每格式/每高级效果支持边界：
局部范围、实例、恢复、哈希证据：
实际命令、原始日志、性能条件：
失败/受阻/跳过/未测：
接口 RFC、主对话接线补丁：
Windows 产物、安装与回退：
```

执行记录：本任务书建立时 B0—B4 均未验收，只有端口框架。

### B0 执行记录（2026-10-27，已交）

B0 已完成并交付证据包 `artifacts/model-multimaterial-b0-20261027/`（报告 `REPORT.md`，
原始日志 `logs/`，离屏 PNG `renders/`）。要点：

- 运行时核实：Python 3.12.8 / Qt 6.7.1 / vtk 9.7.1 / f3d 4.0.0（内置 VTK
  `9.7.1-1746-gdbb73e9cc8`，MSVC 19.44）；下载源码 `CMakeLists.txt` 自述 4.0.0，与安装版一致；
  `pyf3d.pyd`、`f3d.dll`、源码 zip 的 sha256 已记录。
- **A 路线（F3D 稳定局部端口）否决**：公共 Python API 无材质/actor 访问（`Scene` 只有层级、
  可见性、灯光、动画；模块内匹配 `materi` 的符号为 0）；98 个选项只有 4 个全局
  `model.material.*`；`library/public` 无 material 头，`vtkF3DImporter.h` 无 `NodeActors`/
  `Properties` 访问器。内部确实“每材质一个 vtkProperty、每节点每 mesh 一个 actor”，但没有公共入口。
- **A′（C++/绑定补丁）未交付可安装产物**：本机有 VS18/2022 与自带 CMake，但 pip `vtk` wheel
  无头文件/`.lib`/CMake config，必须先自建 VTK 才能链接 F3D 或 pyf3d。成本与阻断已写入报告。
- **B 路线（独立受控预览进程，vtk 9.7.1）核心实验通过**：五项结构夹具（3 材质、1 材质共用
  4 部位、同 mesh 2 实例、同名不同材质）上，“只改共享材质”使 4 个部位全变且其他部位 0 像素，
  “仅此部位独立”只改一个部位且其余 0 像素，两条均 pass；局部改动到出帧 0.24—0.61 ms，
  整文件重开 75.3 ms。**FBX 阻断**：pip vtk 无 FBX/assimp 读取器。
- 对应关系实测：glTF/GLB 一节点实例一 actor、每个 actor 独立 vtkProperty（共享语义未被
  importer 保留，运行时须按 assignment 扇出）；OBJ 只能按材质分组，`g` 组与实例变换不可恢复；
  FBX 在 F3D 侧层级标签全是 `<object>`，且两个样本默认被内容检查拒绝（需
  `scene.skip_content_check`）；`EXT_mesh_gpu_instancing` 被并成单 actor 且名字丢失。
- 未做：B1—B4 全部；SSS/置换/纯线框/拾取/通道/撤销/保存/规模与性能验收；可见 GUI（保持离屏）；
  Babylon 路线未测。未新增 `test_runtime_*` 测试（路线未裁决），框架基线仍 52 passed。
- 待主对话裁决：FBX 路线三选一（自建 FBX 读取 / 构建 assimp 并做经保真验证的派生转换 /
  返工 A′），裁决前 AI 2 不写 `runtime.py` / `runtime_protocol.py` / `native/` 生产实现。

### B0.1 执行记录（2026-10-27，已交）

证据包 `artifacts/model-multimaterial-b0_1-20261027/`（报告 `REPORT.md`，日志 `logs/`，
离屏 PNG `renders/`）。用户要求按"先 A′、再 VTK+Assimp、再稳定映射"的顺序推进，结论如下：

- **A′ 实测受阻，无可运行产物**：`f3d.dll` 导出 2482 个符号，全部是 libf3d 的 C++ mangled 名
  （`??0engine@f3d@@…`），**不导出任何 `vtk*`**；pip `vtk` wheel 无头文件/`.lib`/CMake config；
  全盘常见根目录无 VTK SDK、无 vcpkg、无构建缓存。VTK 9.7.1 源码已取得并实际配置 6 次，前 5 次
  失败原因已定位（Ninja 路径空格被拆、`cl` 不在 PATH、脚本语法、`StandAlone=NO` 与
  `TestingRendering` 依赖冲突），第 6 次配置通过后进入构建，**未在本轮完成**。
  Assimp v5.4.3 已用同一套 MSVC/Ninja 成功构建（`assimp-vc145-mt.dll`，sha256 `B3A1…8347`）。
- **独立 VTK + Assimp 的 FBX 链路通过**：7 个真实 FBX 样本中 6 个读取成功，几何/层级/单位
  （`UnitScaleFactor`）/变换/实例/材质分配/UV/法线/贴图槽/透明/自发光逐项有原始值；
  `transparentTest.fbx` 读取失败（assimp `FBX-Tokenize` offset 越界），已如实记录。
  切线在全部样本上缺失、部分样本被三角化（影响纯线框区分源边）。
- **真实 FBX 上的局部编辑三步骤全部 pass**：共享材质改动影响 2 个使用部位（各 19 600 像素），
  另 2 个部位 0 像素；单部位独立只有该部位变化；恢复后与编辑前整帧 0 像素差异。
  归因改为逐部位孤立渲染（四立方体空间重叠，整帧掩码会把遮挡误判成泄漏）。
- **稳定映射已实现并测试**：`native/bindings.py` 用"结构键 + 几何内容指纹 + 实例变换指纹"，
  覆盖相同位置相同颜色、同名节点同名材质、多 primitive、mesh 实例与 GPU instancing、
  数组顺序变化；歧义一律拒绝。16 个测试通过。需要 RFC：`BindingSite` 增加
  `instance_key` / `geometry_fingerprint`（未自行修改共享契约）。
- **双链路代价已量化**：同一 PBR 模型同相机下平均通道差 5.63/255、15.4% 像素差 >8；
  F3D 与 pip VTK **同进程共存实测崩溃**，双链路必须双进程；`vtkGLTFImporter` 的
  `vtkProperty` 不反映源 PBR 值，材质仍需自己解析。
- **性能口径**：0.80 / 0.59 ms 是"赋值→渲染完成→回读像素"的中位数（5 次取样），
  不含 DLL 加载、assimp 导入与首帧分配，只代表小样本，不作验收。
- **回归现状**：AI 2 的 16 个测试全通过；与框架基线联跑为 37 passed / 31 failed，
  失败点全部落在 AI 3 正在改的 `widgets/model_material_panel.py`、`model_details.py`、
  `model_library.py`（并发中间态，非本轮引入）。需在 UI 落地后由主对话复跑。
- 待裁决：FBX 路线是否定为本轮方案；`BindingSite` 字段扩展；双链路是否接受；
  `transparentTest.fbx` 是否纳入验收夹具。裁决前不接生产装配。

### B1 执行记录（2026-10-27，硬门交付）

裁决已收到（Assimp+受控 VTK 进入 B1/B2；A′ 暂缓记为"构建链尚未完成、当前未交付"；
不批准双链路；同意 instance_key/geometry_fingerprint 方向但契约由主对话改；
`transparentTest.fbx` 记为已确认损坏负例）。本轮按五道硬门交结果：

- **硬门一（映射一对一）已修复**：`reconcile`/`build_slots` 校验 `binding_id`/`native_id` 唯一
  （输入违约抛 `MappingContractError`），两个使用点争用同一原生对象 → `SHARED_NATIVE_OBJECT`；
  `carry_over` 同 ID 也必须重校身份与内容（`IDENTITY_CHANGED`/`CONTENT_CHANGED`），
  且**不再携带 native_id**：跨修订只迁移工作材质与分配。反例测试 23 个全通过。
- **硬门二（身份/证据分离）**：`instance_key` 改为源实例身份（glTF `node:<i>/instance:<k>`、
  FBX `model:<uid>/instance:<k>`）；变换与几何指纹只作证据；新增 `content_fingerprint`
  （UV/法线/材质分组/双面/alphaMode），能抓到"位置相同但 UV 或材质分组变了"。
  规则版本 `MATCH_RULES_VERSION=2`。RFC 见 `docs/model-multimaterial-tasks/RFC-0001-BINDING-EVIDENCE.md`。
- **硬门三（真实 PBR）**：6 个真实样本按**名字**配对（实测 assimp 会重排材质顺序，按索引配会误报），
  材质数值与贴图槽全部对上；AO 在 assimp 侧以 `lightmap` 出现（glTF 导入器行为），已按语义处理；
  颜色空间可判定（同一张 sRGB 128 贴图：关 sRGB 渲染 128、开 sRGB 渲染 55）；
  切线需生成，算法为 UV 差分 + Gram-Schmidt（非 MikkTSpace），14 556 顶点正交化后最大 |dot|=4.2e-5、
  12 个退化顶点、手性 7354/7190；**非 1.0 单位样本已造出**（ASCII FBX 补 `UnitScaleFactor=100`，
  读回 100.0、包围盒不变 → 换算必须运行时做）。未验证项：alpha 裁切/混合像素效果、多 UV 实际采样、
  嵌入贴图取像素、法线绿通道方向、真实 FBX mesh 实例样本。
- **硬门四（显示目标）**：同模型同相机的双路径差异 lit 5.63/255 → unlit 1.14/255，
  说明大头是光照模型而不是材质解释；立场为"封面与编辑必须共用同一套有效材质状态与渲染实现"，
  旧 F3D 只能保留为兼容查看。
- **硬门五（回归归因）**：快照 773 个文件 sha256 + 该快照下 75 passed；
  上一轮 32 条失败逐项归因为并发中间态（**判据含"当前快照 0 失败"**，3 条无调用帧的标注为推断）。
- **A′ 表述更正**：不再用"未生成 VTK.sln"当判据（Ninja 本不产 sln），也不再从"零 vtk 符号导出"
  推断静态链接；状态按裁决记为"构建链尚未完成、当前未交付、不判技术不可行"。
- 未交付：`runtime.py` / `runtime_protocol.py` / prepare-install-恢复的端口验证 / 接线补丁——
  按裁决"先交端口验证再接生产装配"，需契约与显示路线定案后开工。生产能力继续关闭。
