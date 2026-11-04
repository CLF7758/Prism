# AI 2：F3D 读取、交互预览、封面与原生进程

日期：2026-10-26。你是实现 AI 2。当前主对话的 Codex 负责总集成、公共接口裁决和最终验收，不存在另外一个“集成 AI”。

## 1. 总体目标和你的任务边界

Prism 模型库要复用现有材质库布局、标签备注、管理和贴图槽位：导入原模型 → 中间显示当前材质效果的封面 → 上贴图／应用已有材质 → 双击交互 → 导出原件。

你负责 F3D 能力和执行环境，不负责项目数据库、原件导入导出和主程序业务 UI。直接读取 FBX、OBJ、GLB、glTF，不强制产生中间 GLB。

## 2. 当前已有和没有的东西

源码根目录：C:/Users/admin/Desktop/Prism-main/Prism-main。

- model_library/contracts.py 已定义请求身份、任务、状态、显示模式、统计和能力；ports.py 已定义 ModelPreviewPort、ModelJobController。
- model_library/f3d_backend.py 是占位。capabilities 全 false，调用会抛 ModelFeatureUnavailable。
- widgets/model_preview.py 已有工具栏：完整材质、材质＋边线、纯线框、回正、设封面、返回。viewport_host 只有占位。
- model_details 使用原有 MaterialSlot，但控件禁用。
- 没有安装或启动 F3D；没有 worker、交互宿主、封面生成或原生效果验收。
- 已有框架回归不能证明你负责的任何原生功能。

F3D 下载目录：C:/Users/admin/Downloads/f3d-master/f3d-master。该源码声明 4.0.0，没有核实其与某个稳定发行包一一对应。正式运行时不能依赖 Downloads 绝对路径。

## 3. 文件所有权

你可以创建／修改：

- prism/model_library/f3d_backend.py
- prism/model_library/f3d_parameters.py
- prism/model_library/native_jobs.py
- prism/model_library/native_protocol.py（内部实现；公共 DTO 仍不许自行改）
- prism/components/model_worker.py
- prism/components/model_preview.py
- F3D 专用补丁与构建说明目录 third_party/model-library/f3d-patches/
- tests/model_library/test_f3d_protocol.py、test_f3d_backend.py、test_native_jobs.py
- 本文执行记录。

现有 widgets/model_preview.py 的业务 UI 归 AI 3。你提供 F3DWidget 工厂和信号，不同时修改该文件。

不得修改：项目存储／保存、原件导入导出、主窗口／导航、标签备注、material_render／Babylon、共享卡片、contracts.py、ports.py、service.py、performance_trace.py、diagnostics.py、公共 conftest。

## 4. 精确复用的 F3D 位置

下列链接都指向用户下载的真实源码：

- [Qt6 示例](C:/Users/admin/Downloads/f3d-master/f3d-master/examples/libf3d/python/qt6/minimal_qt6.py:74)：外部 OpenGL 上下文、QOpenGLWidget、输入转发。原示例 PySide6，需适配 PyQt6。
- [离屏封面示例](C:/Users/admin/Downloads/f3d-master/f3d-master/examples/libf3d/python/offscreen-thumbnail/offscreen_thumbnail.py:44)：直接读取模型并 render_to_image。
- [scene.h](C:/Users/admin/Downloads/f3d-master/f3d-master/library/public/scene.h:68)：add／clear、层级、场景信息。
- [Python 绑定](C:/Users/admin/Downloads/f3d-master/f3d-master/python/F3DPythonBindings.cxx:946)：引擎、场景、相机、图片、动画。
- [FBX 导入器](C:/Users/admin/Downloads/f3d-master/f3d-master/plugins/assimp/module/vtkF3DAssimpImporter.cxx:642)：插件能力，不自己重写 FBX parser。
- [参数定义](C:/Users/admin/Downloads/f3d-master/f3d-master/library/options.json:409)：颜色、法线、材质纹理、发光、透明。
- [日志绑定](C:/Users/admin/Downloads/f3d-master/f3d-master/python/F3DPythonBindings.cxx:1207)：Log.forward、等级及 force_std_err。

不复制整个 F3D 命令行应用、配置解析和主事件循环进 Prism。

## 5. 你需要提供的接口

保留现有 ModelPreviewPort 和 ModelJobController 的公共签名，公开这些实现入口：

~~~text
F3DBackend(runtime_config)
  capabilities -> ModelCapabilities
  open(ModelJobRequest) -> job_id
  update_appearance(ModelJobRequest, ModelAppearance) -> job_id

NativeModelJobController(runtime_config, publish_event)
  submit(ModelJobRequest) -> job_id
  cancel(job_id) -> None
  close_project(project_session) -> None
  shutdown() -> None

create_f3d_widget(parent, prepared_bundle, parameter_snapshot) -> QWidget
~~~

widget 工厂只允许在交互预览辅助进程内调用，不在 Prism 主进程创建原生引擎。
准备好的包由 AI 1 提供：package_root、main_relative_path、manifest_path、lease_id、source_revision。
工厂的输入不是 live scene、数据库连接、Material 对象或主窗口。

补充结果需求：能力握手、可交互、封面文件已写入、统计、失败、关闭、进程退出、日志。先在你自己的模块定义不可变私有结果，报告确切字段，由主对话统一接线；不能自己改公共契约。

请求要带完整 ModelRequestIdentity。结果回传原身份；不自行把过期任务当成当前任务。

## 6. 按步骤做

### B1：先做独立最小实验和能力表

1. 固定源码／运行包、VTK、Python ABI、构建开关、reader、插件。列出加载来源。
2. 在项目 .venv 的 Python 3.12 环境确认绑定适配；不要无条件改 Prism 的 Python 或 Qt 版本。
3. 四种格式各至少两例起步，包含一个带真实外置贴图的 FBX 和 OBJ、一个 glTF+BIN。
4. 只在辅助进程加载，验证输出封面、大预览可旋转、相机回正和资源释放。
5. 输出能力矩阵：已实测／失败／未知；记录原文件、日志、硬件和版本。

未通过必要能力前不要扩展原有业务 UI，不要用换格式掩盖读取失败。

### B2：实现参数适配，先确保原始外观

1. 无外观覆盖时显示原模型导入的多材质与贴图，不统一替换成灰色或同一材质。
2. 有覆盖时使用现有 material 参数语义，不创造第二套业务参数。
3. 颜色用 model.color.texture/rgb；法线用 model.normal.texture/scale；发光用 model.emissive.texture/factor。
4. AO／粗糙度／金属度按后端需要生成预览 ORM；核对通道、色彩空间和乘数，不能强度乘两次。
5. 独立透明图、DirectX 法线、每通道变换有明确适配；原图不能被覆盖。
6. SSS、置换、复杂透明、UDIM 没证据就报告不支持。不能使能按钮然后忽略参数。
7. 封面和交互使用同一参数快照、相机、灯光与环境版本。

与 AI 3 的分界：AI 3 管控件／配置快照，你管快照在 F3D 的真实效果。

### B3：补齐纯线框和统计

纯线框：

1. 当前 show_edges 最终 SetEdgeVisibility，不能当作纯线框。
2. 用最小 F3D 补丁或受支持的公开扩展增加表面／边线叠加／纯线框表示方式。
3. 模式切换只影响显示，不改原始模型和持久外观。
4. 切回完整材质必须恢复原材质、透明和边线状态。

统计：

1. 当前 SceneInfo 只有文件、Actor、points、cells。
2. 不把 Cells 改中文标签当三角形数。
3. 区分表面、线、点、实例；统计加载后模型的顶点／多边形／三角形，并写 basis。
4. 使用明确的三角化统计路径时，不写回原件；记录算法和后端版本。
5. 不可证明原始 DCC 拓扑数就返回 None／unknown。
6. 用四边面、含线点、实例和多网格样本由独立工具对照。

补丁单独保留，不调用无法维护的 F3D 私有对象黑盒。提交变更文件、构建命令和补丁回退方式。

### B4：实现原生进程与任务控制

1. 默认一个离屏进程和一个交互宿主，不能每张卡片一个引擎。
2. 专用本地协议承载消息，不把混有 F3D 输出的 stdout 当 JSON。
3. 有版本／能力握手，缺插件或 ABI 不匹配要明确失败。
4. 可见卡片和当前模型优先；重复请求合并，连续改参数只保留最新修订。
5. 取消先撤销发布资格；长原生调用不响应时由监督器结束进程，不让主 GUI 等待。
6. 项目关闭停止该会话任务、关闭窗口并释放 AI 1 租约；不能清理其他项目包。
7. 结果文件先临时写，确认存在和完整后发布；主程序仍须复核身份。
8. 原生崩溃捕获退出码、任务身份和有限尾部日志；避免无限重启。

主程序不执行原生 add／render。辅助窗口加载卡住也必须能由主程序取消／隔离，不能只记录“主程序没崩”就判性能通过。

### B5：接离屏封面与交互宿主

1. 离屏示例改为长驻、可清空、可复用的 worker；同一个已加载模型改贴图不重新读全部文件。
2. 缓存签名至少包含原件／外观／相机／后端／环境修订。
3. 模型换了或修订过期，旧封面输出必须丢弃。
4. Qt6 示例改为 PyQt6，在专用宿主内使用现有 ModelPreviewWindow 的 viewport_host。
5. 工具栏接模式、回正、设封面；不会的控件保留禁用并报告。
6. 窗口缩放、DPI、焦点、鼠标、滚轮、关闭、OpenGL 上下文销毁都验证。
7. 静止／隐藏时控制渲染，不照搬一直运行的 30ms timer 来消耗资源。

UI 文件由 AI 3 修改：把你工厂和信号清单交主对话，再由主对话发出确认版本。不要直接越界改对方文件。

### B6：打包和交接

准备固定依赖位置、离线资源定位、Python／Qt／VTK／插件版本清单和第三方许可证。
正式运行不从 Downloads 读取库。提供启动命令、参数、协议例子、退出方法和最短工厂调用。

## 7. 并行与依赖

- B1～B3 可以与 AI 1 的 repository、AI 3 的 UI、AI 4 的期望集并行。
- AI 1 的 PreparedBundle 接口冻结后再正式联调读取；实验阶段可用专用测试模型路径，不用于生产绕过资源管理。
- 你不实现现有库的标签／备注／导出，AI 1 不实现你的原生渲染。
- AI 4 独立核对统计与参数，不能用你自己返回的数字生成期望。
- 公共契约变更发主对话，不安排其他实现 AI替你改。

## 8. 验收

至少覆盖：

- 四格式直接读取；正常原始多材质、外置资源、骨骼／动画文件的读取结果和限制。
- 参数修改真实生效，封面与大预览一致；原文件哈希不变。
- 真正纯线框、边线叠加和恢复材质；统计包含非三角、点线与实例对照。
- 快速切换／连续修改／项目重开后旧结果不发布。
- 超时、取消、插件缺失、坏文件、原生崩溃和重启上限。
- 主进程仍可导航与保存；窗口和进程关闭后释放资源。
- 正式程序启动不导入 F3D、不预热模型引擎。

机器已知：i9-13900K、约 128GiB、RTX4080 SUPER 16376MiB、驱动595.79、2560×1440。SSD 与 HDD、冷暖缓存分别报告。测 P50/P95/P99、最长停顿、RAM／VRAM 峰值和重复切换趋势；未测不能填估计值。

~~~text
.venv/Scripts/python.exe -m pytest -q tests/model_library/test_f3d_protocol.py tests/model_library/test_f3d_backend.py tests/model_library/test_native_jobs.py
.venv/Scripts/python.exe -m pytest -q tests/test_model_library_scaffold.py tests/test_material_cards.py
~~~

原生实测必须真实运行，不以 mock、截图或仅 import 成功替代。

## 9. 汇报给当前主对话

用户会把你的回复交回集成者；不另找集成 AI，也不擅自给其他聊天发消息。

~~~text
角色：AI 2
状态：B1…B6 分别为通过／失败／未测／受阻
工作目录、提交／补丁：
文件清单：
固定运行时：F3D／VTK／Python ABI／Qt／reader／插件／构建方式
接口：工厂、方法、信号／事件、协议、结果文件、关闭方式、调用例子
能力表：格式、通道、纯线框、统计逐项真实状态
原生补丁：文件、理由、复现构建与回退
真实样本和独立对照证据路径：
命令、结果、失败、原始日志：
性能／资源：环境、样本数、分位数与原始数据
异常隔离／取消／过期结果验证：
需要主对话接入的位置和步骤：
明确未实现和不支持项：
~~~

中途接口请求：

~~~text
编号：B-RFC-01
当前接口／能力缺口：
为什么不能在本模块解决：
最小变更与消费者：
替代方案：
仍可继续的独立工作：
~~~

## 10. 执行记录

2026-10-26 本轮（AI 2）。所有“通过”都有真实运行证据，路径在
`artifacts/model-library-f3d/`。未测项写“未测”，受阻项写“受阻”。

- 当前：B1 通过、B2 部分通过（SSS/置换/贴图变换不支持）、B3 部分通过
  （纯线框受阻、FBX 统计未知）、B4 通过、B5 通过（交互预览真实跑通）、
  B6 部分通过（固定运行时与许可证清单已写，离线安装包未做）。
- B1：固定运行时 `f3d==4.0.0` / VTK `9.7.1-1746-gdbb73e9cc8` / CPython 3.12
  ABI / PyQt6 6.7.0（OpenGL 4.6 上下文）。四格式实测：OBJ、glTF(+外部 BIN)、
  GLB、二进制 FBX 都能直接读取并出封面；带真实外置贴图的 OBJ
  (`obj_multimaterial.obj`) 与原多材质保留可见；FBX 走 assimp 插件
  (`assimp_box.fbx` 36 点/12 面、`spider.fbx` 19 actor/4104 点/1368 面)。
  ASCII FBX 被内容检查拒绝（只认 `Kaydara FBX Binary` 魔数），报
  `unsupported_format` 而不是降级。**关键环境结论**：同一进程必须先 import
  f3d 再 import PyQt6，反序 `pyf3d` DLL 初始化失败（`.tmp` 探针 7 组对照）。
- B2：颜色贴图/颜色值/发光贴图与强度/粗糙度/金属度/ORM 三通道/透明度/边线
  全部逐项渲染差分取证（`b2_report.json`、`b2_orm_report.json`、
  `b2_normal_report.json`）。三条“不能想当然”的实测结论：① ORM 是
  R=AO、G=粗糙度、B=金属度；② `model.material.roughness/metallic` 会**替换**
  G/B 通道而不是相乘，所以有贴图时强度必须烘焙进通道；③ 法线贴图只在源文件
  带切线（TANGENT）时生效，OBJ/FBX 无切线时外观不变。
  `model.textures_transform` 在 Python 绑定里设不进去（TypeError），改为把
  每通道翻转烘焙进派生贴图。SSS 与置换无对应参数，报告不生效。
- B3：边线叠加通过（`render.show_edges` + `render.line_width`，模式切回后恢复）。
  **纯线框受阻**：4.0.0 公共接口只有 `SetEdgeVisibility`，`opacity=0` 会把边线
  一起隐藏；本机没有可复用的 VTK 开发树，无法产出打过补丁的 `f3d.dll`。
  补丁规格、构建参数与回退方式写在
  `third_party/model-library/f3d-patches/README.md`，能力保持 `False`。
  统计做成两条明确口径：`f3d-loaded`（SceneInfo + 后端逐 actor 明细）与
  `file+vtk-triangulation`（独立 VTK 读取 + `vtkTriangleFilter`），FBX 无独立
  读取器时 `triangles=None`。与 AI 4 独立期望集对照：30 个字段一致、7 个不同，
  差异全部有解释（三角化复制顶点、line/fan/strip 展开、OBJ 的 l/p 元素口径）。
- B4：`NativeModelJobController` 实现进程监督、握手（版本＋格式＋能力）、
  按 `(session, model, kind)` 合并并只保留最新修订、取消先撤销发布资格再升级
  结束进程、超时结束进程、崩溃捕获退出码与有界日志尾部、重启上限、结果
  先临时写再校验哈希后原子改名发布、项目关闭只清理本会话输出。
  10 连击实测：9 个 stale、只发布最新 1 个（`perf_report.json#burst`）。
- B5：离屏 long-lived worker 支持同引擎换模型与换参数（同模型改参数
  `load.reused=true`），封面由参数快照＋相机＋`ENVIRONMENT_REVISION` 共同签名。
  交互宿主在独立进程起 PyQt6 窗口，用 `Engine.create_external` 接入 Qt 的
  OpenGL 上下文，`create_f3d_widget` 安装进现有 `ModelPreviewWindow`
  （不可用时用内置 fallback 壳）。实测：open（7 actor/24 点）、模式切换
  `material_edges` 成功、`wireframe` 明确返回 `unsupported_capability`、
  回正返回相机、设封面输出 978×708 PNG（2093 B，哈希校验后原子发布）、
  关闭后进程退出（`preview_report.json`）。
- B6：`third_party/model-library/f3d-patches/README.md` 记录固定运行时、
  reader/插件来源、许可证清单、DLL 名字冲突风险、启动命令与协议示例。
  离线安装包与 installer 脚本未做（未测）。
- 待集成事项：见本轮汇报的“需要主对话接入的位置和步骤”；公共
  `contracts.py`/`ports.py`/`service.py` 未改，能力开关需要主对话按实测值接线。
- 未测／不能判断：真实 10 万级规模、HDD 冷缓存、VRAM 峰值（未插桩）、
  动画播放、UV 每通道变换的组合效果、`testline.obj` 触发的统计进程原生崩溃
  根因（已隔离但未定位）。

