# 模型库接入现有全局搜索（2026-10-29）

本轮只扩展既有项目搜索、结果导航与模型分页，不新增搜索数据库、渲染器或模型解析链路。沿用现有大小写不敏感、多个词同时匹配、标签表达式/别名、排序、结果卡片、范围与返回结果页机制。

## 已接入

- 搜索类型与结果计数增加“模型”；下拉框和结果页共用 TYPE_LABELS，避免两份类型词表。
- “全部”及“模型”搜索包含模型名称、备注、标签、格式、依赖文件相对路径和目录路径。
- 模型标签进入既有项目标签名称/计数来源，复用现有标签表达式及别名匹配；这不是新增模型标签批量编辑功能。
- 两个既有搜索入口均可定位模型；复用导航提交门、项目/打开代次检查、目录定位、模型摘要查询、卡片选择及详情加载。
- 已删除模型、已删除祖先目录、目录循环、跨工程失效结果不可定位。
- 模型定位给现有 ModelQuery 增加可选 focus_id：仓库在相同筛选和确定排序下求目标页，只返回一页摘要。UI 工作线程执行查询，不为了定位读取前面所有封面。后续分页仍使用原查询。
- 持久化后的改名、标签、备注、目录和删除等搜索字段变化，经现有 search_content_changed 防抖刷新；仅封面/渲染参数变化不触发全文重新查询。
- 搜索只读已安装的工程域元数据，不读取 FBX/OBJ/glTF 原件，不在搜索期间启动 F3D 或生成封面。结果仍使用现有搜索卡片的文字摘要；搜索卡片不新增实时三维渲染。

## 实现入口

- prism/project_search.py：模型匹配与模型可见记录入口。
- prism/material_service.py：两库复用已有祖先目录可见性规则。
- prism/search_controller.py、widgets/search_canvas.py、widgets/media_library.py：类型、计数、范围、有效性、详情文本与导航。
- prism/model_library/contracts.py、repository.py、widgets/model_library.py：可选目标页定位、异步安装及普通选择链。
- prism/window_workspace.py：模型沿用材质的 from_search 导航语义，保留返回结果页。
- prism/model_library/composition.py：已提交搜索字段变更通知。
- prism/tags.py：模型标签参与共享标签名称/计数。

## 验证范围

新增 tests/test_model_global_search.py 使用真实临时 SQLite 工程、真实 ModelRepositoryAdapter/ModelLibraryService、Qt 离屏窗口和既有搜索控制器。覆盖多词、标签别名、范围、删除祖先、失效工程、保存重开、160 条分页与 80 条真实窗口定位、持久化改名/标签备注刷新、封面变化不重查。

完整命令和结果见 artifacts/model-global-search-regression-20261029.txt；源码哈希见 artifacts/model-global-search-source-20261029.json。

用户真实桌面视觉验收、安装包及十万模型性能本轮未测。已遵守不控制用户桌面的要求，不把离屏测试作为真实桌面验收。

本轮扩大回归：206 passed，64.53 秒。原始输出保留在上述 regression 文件中。此前新增测试的三项初次失败中，两项为新测试假设错误（标签建议也计入原始命中；createFolder 返回副本），另一项暴露定位调用未同步 selected_id，已改为复用完整选择入口。随后新增持久化测试错用仅支持目录/名称/删除的批量端口，改为调用既有 rename 与 update_metadata；未放宽产品校验。
