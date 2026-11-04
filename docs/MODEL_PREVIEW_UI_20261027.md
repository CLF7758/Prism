# 模型库沿用材质库界面与通道预览

日期：2026-10-27。工作目录：`C:/Users/admin/Desktop/Prism-main/Prism-main`。

## 本次实现

1. 预览子进程加载主程序同一份 `prism/assets/theme.qss`、COLORS 调色板、字体和 `dialog_theme` 深色原生标题栏。没有另建一套浅色主题。模型列表继续复用 `MaterialCards`，右侧继续复用 `MaterialSlot`。
2. 模型预览保留材质库同样的上方通道选择、回到正面、右侧返回按钮、下方状态反馈布局，模型增加设为封面、材质边线入口；背景为黑色。原生封面和预览背景环境版本同步升至 2。
3. 双击处理使用实际被双击的模型 ID；切换选择失败时不会继续打开旧模型。
4. 主程序将窗口句柄与模型名称传给预览子进程。Windows 子进程设置 transient owner，每次打开、复用窗口及首帧完成后调用 show/raise/activate，最小化窗口先恢复。主程序仅在自身仍为前台时向预览进程转交前台权限；没有设置全局置顶。
5. 通道预览只改变显示，不改工程材质、原件或导出数据。切换保留相机；失败回到上个成功视图；通道视图禁止设为正式封面。换模型清除先前通道和外观覆盖，避免串模型。

## 通道范围与真实限制

| 格式 | 当前通道读取 | 边界 |
| --- | --- | --- |
| GLB / glTF | 颜色、法线、粗糙度、金属度、AO、透明度、自发光 | 读取真实材质绑定，支持外置与内嵌图片；缺省参数按格式默认值显示，零值可能全黑。压缩图片若不能解码，报告失败，不伪造贴图。 |
| FBX | 原有颜色；单材质二进制 FBX 中明确连接的法线、自发光贴图 | 从 OP 连接读取；ASCII、多材质分通道暂不冒充支持。旁边有 roughness 文件不等于文件内已绑定，不按文件名猜。 |
| OBJ / MTL | 颜色；明确声明的 norm/map_Kn、Pr/map_Pr、Pm/map_Pm、d/Tr/map_d、Ke/map_Ke | 派生 MTL 保留各材质名称与几何 usemtl；bump 不冒充切线法线，AO 不猜测。多文件 mtllib 单行、复杂贴图选项与 UV 变换仍需扩展验证。 |
| 显式外观覆盖 | 法线、自发光、ORM 派生图可作诊断输入 | 原有全模型覆盖边界不变；并未实现用户另行要求的逐材质编辑。 |

SSS、置换、纯线框仍未实现，菜单保留但禁用并说明原因。它们仍是用户要求的待办，没有删掉要求，也没有用透明度、透射或表面边线替代。

glTF 派生文件仅在预览私有缓存中创建，保留原几何 buffer、mesh/material 索引、贴图 texCoord、KHR_texture_transform、sampler。粗糙度取 G、金属度取 B、AO 取 R、透明度取 A，并应用相应标量。子进程正常关闭时清理本次通道临时目录。多材质只读诊断不等于逐材质编辑功能完成。

## 文件职责

- `widgets/preview_theme.py`：子进程复用正式主题。
- `widgets/model_preview.py`：正式深色预览外壳、通道可用性和封面保护。
- `model_library/preview_channels.py`：只读通道绑定解析和派生缓存，不持久化材质。
- `components/model_preview.py`：F3D 视图切换、相机、窗口所属和前置、IPC 事件。
- `model_library/native_jobs.py`：受限前台权限转交；渲染仍留在子进程。
- `model_library/service.py`、`composition.py`：窗口身份和名称传递。
- `widgets/model_library.py`：双击目标 ID。

## 验证与证据等级

全部 Qt 测试设置 `QT_QPA_PLATFORM=offscreen`。没有操作用户桌面、没有关闭正在运行的程序。

最终相关回归：146 passed，1 skipped，32.25 秒。跳过的是需要可见原生 GUI 的测试，未开启桌面操作。结果包含材质卡片与外观回归；不是整个 Prism 全量测试通过声明。

自动测试覆盖：RGBA 指定像素分通道与因子、GLB 内嵌图、原 buffer 字节、双材质 UV/材质索引、原文件哈希、OBJ bump/normal 区分、模式禁用、封面保护、主题加载、双击目标、窗口恢复/raise/activate 调用、Windows 前台权限条件。窗口测试使用 mock，只证明调用与条件，不证明真实桌面层级。

真实 F3D 4.0.0 离屏实验：01 椅子 FBX、03 石头 glTF、09 音箱 GLB、15 AlphaBlendModeTest 多材质 glTF，共 23 个可用通道渲染。直接调用生产 F3DViewport.set_mode，而非测试自建显示逻辑。四个模型均验证：通道切回完整材质逐像素一致，带覆盖/通道状态后重开清除旧状态，源目录全部文件 SHA-256 不变。椅子实际 EXR 法线图成功显示。零金属度/零自发光黑帧与源参数核对，不作为缺失模型误报。

原始证据在 `artifacts/model-preview-ui-20261027/`：

- `native-channel-results.json`：每个样本、通道、失败/禁用原因、还原和原件结果。
- `native-render.log`：真实离屏原始输出。
- `01-normal.png` 等：渲染输出，非桌面截图。
- `regression.txt`：回归原始输出。

复跑：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe -m pytest -q tests/model_library/test_preview_channels.py tests/test_model_library_scaffold.py tests/model_library/test_ui.py tests/model_library/test_management.py tests/model_library/test_integration.py tests/model_library/test_f3d_backend.py tests/model_library/test_native_jobs.py tests/model_library/test_f3d_protocol.py
.venv/Scripts/python.exe tests/model_library/acceptance/check_preview_channels.py
```

## 未证明与后续工作

真实 Windows 桌面层级和实际双击手感尚未人工验收；后台 mock 不能替代。可见原生 GUI 回归没有开启。打包版本主题路径未另行构建验证。未宣称 20 个模型全量验收，也没有本次写入用户工程。十万资产吞吐、超大模型与贴图切换耗时未测；通道首次派生仍在隔离预览进程执行，可能阻塞该预览窗口，主程序进程不执行解码/渲染。后续应将通道准备移到可取消后台工作，再处理预览线程安装。

原材质库和模型库多个材质球的编辑方案不在本次实现；见 `MODEL_MULTI_MATERIAL_DESIGN_20261027.md`。
