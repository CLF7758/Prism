# F3D 4.0.0 原生补丁与固定运行时（AI 2）

本目录只放 **补丁规格、构建与回退说明**，不放未经验证的二进制。
Prism 正式运行不读取 `C:/Users/admin/Downloads/f3d-master`，也不读取本目录以外的本地源码。

## 1. 当前固定的运行时（实测过）

| 项目 | 值 | 取证方式 |
|---|---|---|
| F3D | `4.0.0`（`Engine.get_lib_info().version_full`） | `prism/components/model_worker.py` 握手 |
| VTK（F3D 内建） | `9.7.1-1746-gdbb73e9cc8 (date: 20261016)` | 同上 |
| Python ABI | CPython 3.12（`pyf3d.cp312-win_amd64.pyd`） | 包文件名 |
| Qt | PyQt6 6.7.0 / Qt 6.7.0，OpenGL 4.6 上下文 | 预览进程实测 |
| F3D 模块 | `FFmpeg=True, ImGui=True, OpenEXR=True, Raytracing=False, WebP=True` | `get_lib_info().modules` |
| 读取器（插件来源） | OBJ（native）、GLB/GLTF（draco 插件注册的 `GLBDraco`/`GLTFDraco`）、FBX/DAE/DXF/OFF/3MF/AMF（assimp 插件） | `Engine.get_readers_info()` |
| 安装形态 | `pip install f3d==4.0.0`（wheel 37.5 MB，`f3d/bin` 内含 `f3d.dll` 与依赖 DLL；`share/f3d/plugins/*.json`） | 见下第 4 节 |
| 许可证 | BSD-3-Clause（F3D）；VTK/assimp/draco 各自许可证随 wheel | `get_lib_info().license` |

### 1.1 必须记住的一条运行时约束

**同一进程内必须先导入 `f3d`，再导入 PyQt6。** 反序会得到
`ImportError: DLL load failed while importing pyf3d: 动态链接库(DLL)初始化例程失败。`
实测：`plain / PIL / sqlite3 / numpy / vtk` 先导入都不影响，只有先导入 PyQt6 会失败。
因此：

- Prism 主进程（Qt）**绝不导入 f3d**；离屏渲染与交互预览都在独立辅助进程里；
- 预览进程入口先 `import f3d`（`prism/components/model_preview.py::_ensure_native_first`）；
- 打包时不要尝试把 F3D 直接嵌进主进程，即使只是“预热”。

## 2. 纯线框补丁（当前未打，能力保持关闭）

### 2.1 现状与证据

- F3D 4.0.0 的公共 option 清单里只有 `render.show_edges`（最终调用
  `vtkActor::GetProperty()->EdgeVisibilityOn()`，即**边线叠加**）、`render.line_width`，
  没有任何 surface/wireframe 表示开关。
- 实测：`render.show_edges=true` 只加边线（像素变化但表面仍在）；
  `model.color.opacity=0` 会把表面和边线一起隐藏（非背景像素 0），无法得到纯线框。
- `Engine`/`Scene` 的 Python 绑定没有暴露 `vtkActor`/`vtkProperty`，无法在业务层
  “顺手”改表示方式，也不允许调用 F3D 私有对象。

结论：**纯线框在未打补丁的 4.0.0 上不可实现**，因此
`wireframe=False`，`ViewMode.WIREFRAME` 请求返回 `unsupported_capability`，
UI 保持禁用（`prism/widgets/model_preview.py` 已按能力灰化）。

### 2.2 最小补丁规格（待有构建环境时实施）

目标：新增一个公共 option，列入 `library/options.json`，由渲染器按值设置 actor 表示。

~~~text
options.json
  "render.representation":
    type: string, default "surface", values ["surface", "surface_edges", "wireframe"],
    doc: "surface: 不显示边线；surface_edges: 表面 + 边线；
          wireframe: 只画边线（SetRepresentationToWireframe）"

vtkext/private/module/vtkF3DRenderer.h/.cxx
  - 新增成员 std::string Representation = "surface";
  - 新增 SetRepresentation(const std::string&)；
  - 在 ConfigureActorsProperties()（同 SetEdgeVisibility 的调用点）里：
      surface        -> EdgeVisibilityOff(), RepresentationToSurface()
      surface_edges  -> EdgeVisibilityOn(),  RepresentationToSurface()
      wireframe      -> EdgeVisibilityOff(), RepresentationToWireframe()
  - 恢复材质时必须把 Representation 还原为 Surface（对应 B3 的“切回完整材质”）

library/src/options.cxx（或该版本中 option 的分发处）
  - 把 render.representation 接到 vtkF3DRenderer::SetRepresentation，
    并保留 render.show_edges 的既有语义不变（向后兼容）。

library/testing/TestSDKOptions*.cxx
  - 增加三种取值的渲染结果对比测试（可用 f3d 的 image compare）。
~~~

补丁**只**增加一个 option 和一条 actor 设置路径，不复制 reader、不改 importer、
不引入新依赖。Python 绑定无需改动（`Options.__setitem__` 已支持 string）。

### 2.3 构建与回退

- 构建：`f3d-master` 需要 MSVC 2022 + CMake + Ninja + 由源码构建的 VTK（含
  `vtkIOImport`/`vtkIOGeometry` 与 assimp/draco 插件）。本机有 VS2022，
  但没有可复用的 VTK 开发树，且 F3D wheel 不含头文件，因此**本轮无法产出打过
  补丁的 `f3d.dll`**；这条能力按“受阻”上报，不按“通过”上报。
- 交付形态：补丁目录保存 `.patch`（`git format-patch` 风格）与
  `BUILD.md`（完整 cmake 参数、版本钉死值、产物哈希）。
- 回退：补丁只新增 option；回退即 `git checkout -- vtkext library/options.json`，
  并删除 `render.representation` 用法。业务层已经把它当作可选能力
  （`handshake.wireframe`），所以回退不需要改 Prism 代码。
- 一旦补丁可用：在 `build_handshake()` 里用“真的画过一次纯线框”的结果把
  `wireframe` 置 True，并新增黄金样本像素对比，再允许 UI 启用。

## 3. 几何统计补丁（当前用替代路径，未打补丁）

F3D 4.0.0 的 `SceneInfo` 只有 `number_of_files/actors/points/cells`。本轮做法：

1. **加载口径**（主）：`SceneInfo` 的 points/cells + 后端自身输出的逐 actor 明细
   （`model_worker.parse_actor_blocks`），basis 记 `f3d-loaded`；
2. **文件口径**（独立对照，`--mode statistics` 进程）：`vtkOBJReader`/`vtkGLTFReader`
   + `vtkTriangleFilter`，basis 记 `file+vtk-triangulation`；
3. FBX 没有独立几何读取器：`vertices/triangles/polygons` 记 `None`，只报
   `f3d-loaded` 的 points/cells，**绝不把 cells 叫三角形**。

若要改成 F3D 内建，最小补丁为：

~~~text
library/public/scene.h / types.h
  scene_info_t 增加 number_of_triangles / number_of_polygons /
  number_of_line_elements / number_of_point_elements / number_of_instances
vtkext/private/module/vtkF3DMetaImporter.cxx
  汇总时按 vtkCellTypes 分类统计，并对 vtkTriangleFilter 结果单独计数
python/F3DPythonBindings.cxx
  绑定新字段（SceneInfo 是只读 property）
~~~

## 4. 依赖与许可证清单（打包用）

- 依赖位置：`.venv/Lib/site-packages/f3d/`（`pyf3d.cp312-win_amd64.pyd` + `bin/*.dll`
  + `share/f3d/plugins/*.json`）。运行时不从 Downloads 或源码树加载任何库。
- 额外依赖：`vtk` wheel **仅**用于 `--mode statistics` 进程；缺失时统计降级为
  `basis=unavailable`，渲染不受影响。
- Python 与 Qt：与 Prism 主程序同一 `.venv`（PyQt6 6.7.0 / Python 3.12）。
- 许可证：F3D BSD-3-Clause；随 wheel 的 VTK(BSD-3)、assimp(BSD-3)、draco(Apache-2.0)、
  FFmpeg(LGPL/GPL 取决于构建)、OpenEXR(BSD-3)、WebP(BSD) 需在 `THIRD_PARTY.md` 中列明。
- 已知 DLL 名字冲突：`f3d/bin` 内含 `tiff.dll`、`zlib.dll`、`sqlite3.dll`、
  `libcrypto-3-x64.dll`。同进程若先加载同名系统/Pillow DLL，会得到
  “DLL 初始化例程失败”。这也是主进程不得导入 F3D 的第二个理由。
