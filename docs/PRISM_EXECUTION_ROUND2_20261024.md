# Prism第二轮执行总表：固定12个任务，分文件并行

日期：2026-10-24。接收人：AI-01统筹；每张AI-XX任务卡发给对应AI。用户转发即按本轮指定范围执行D1，不需要再做一轮全局停写确认；仅同文件确有在途写者时先交接。C0归档不变，D1改动不再贴C0标签。此文替代旧卡本轮执行安排，不新增产品功能；旧失败证据和数据保护仍有效。

## 1. 这一轮到底要交付什么

不是完成更多报告，而是修出可独立验收的增量：

- 保存不崩、提交事实可信；partial四类全局metadata不再静默丢失。
- 聚合范围切换无重复ID，缓存和布局真实复用，十万级不靠保留全量重型对象维持。
- 远/中/近真实图像显示，鼠标焦点优先与全视口公平恢复；诊断故障不影响显示。
- 各类计数同作用域同修订，画布结果完成主动刷新，缩放不查目录/stat。
- 材质折叠/预览无错误几何帧，相机实际统一，移除失效菜单。
- 未加载文档正文/附件正确导出，退出时后台工作安全收尾。

P0保存崩溃和数据丢失要先打最小补丁；除此之外性能优化仍为主线。先做确定修复，不以全量测试或方法报告替代代码。

## 2. 当前代码核对得到的执行依据

本轮三名并行子智能体只读核查保存/导出/退出、查询/布局、计数/材质/相机；主线核查日志/解码。未修改产品、未跑测试或应用。43文件的当前完整字节及哈希在仓库artifacts/dispatch-20261024/round2-plan/source-at-plan和source-manifest.json，不是新增产品通过证明。

| 现代码事实 | 直接安排 |
|---|---|
| SavePreparation._changed_entities对int changed_rows调用tuple；writer又可能None+complete=True | AI-02 S1优先 |
| full/partial commit之后还有可抛缓存/指纹维护，可能混淆提交与确认 | AI-02同包收口 |
| partial未覆盖tags/categories/library_queries/tag_system | AI-02 S3，不再扩大取证 |
| checkpoint浅复制items共享而offset各自变化 | AI-06 Q1最小复现后修；不把191205精确来源先认定 |
| 合法空[]被当不可复用；completed预算实际整条pop、pending只有条目上限 | AI-06 Q1/Q3 |
| 布局快照依赖list身份，发布复制list使恢复落空 | AI-06适配调用＋AI-07 G1接口 |
| _build_state.items共享；真正大量新增是全量card/tuple/索引/组 | AI-07 G2，不误删共享列表 |
| CANVAS_DRAWN未找到生产调用，计数accept仅token不够 | AI-03＋AI-07接真实paint |
| 材质AlwaysOn/190高/同步fold已有，_fit_preview仍猜父宽 | AI-04有限收尾，不重复补旧代码 |
| camera20是scene units；主view fit仍旧路径 | AI-05改policy，AI-02合view补丁 |
| 异常解码failed和service_done重复扣running | AI-08 D1现在可修，不等整套新API |
| GC fallback算了未独立落盘；open_phase end异常会跳过context恢复 | AI-09 T1/T2 |
| 导出缺content直接空串；真实存正文再延迟打开已有复现 | 移交AI-10直接开发 |
| 物化service只有executor.shutdown；decoder已有aboutToQuit非阻塞shutdown | AI-11源服务、AI-08 decoder、AI-03宿主单点接线 |

## 3. 12个AI新分工与立即开工范围

| 发给谁 | 本轮角色 | 现在做什么 | 只在何处等待 |
|---|---|---|---|
| AI-01 | 协调/集成，不写产品 | 登记43文件唯一owner，收小包diff，出C1候选 | 同文件交接/重型窗口；不等170项历史补齐 |
| AI-02 | 保存唯一写者 | S1保存崩溃/提交终态→S2失败恢复/取消/覆盖→S3四键metadata | 相机由AI-05交小补丁，你合view |
| AI-03 | 计数＋主窗口唯一写者 | C1计数scope/token/drawn接口；C2启动/树复用 | 保存timer等AI-02；退出接AI-11/08；不阻塞先做计数 |
| AI-04 | 材质实际开发 | M1 preview/槽位几何→M2同修订服务复用 | renderer超范围先登记；不再泛化调查 |
| AI-05 | 相机/菜单 | 实际screen padding/bounds/空态/旧快捷键 | 不写view，补丁由AI-02顺序合 |
| AI-06 | 查询与结果驻留 | Q1重复续传/空结果→Q2布局适配→Q3预算→Q4下推/保存复用 | receipt等AI-02、canvas接口等AI-07；其他包不等待 |
| AI-07 | 布局与有界card | G1几何token→G2标量布局/按需card→G3真实paint计数/覆盖 | 一页身份合同和AI-06对齐；不写其数据文件 |
| AI-08 | 解码与导航 | D1单次扣账/诊断隔离即可开始→D2 task/focus→D3停止屏障 | D2新API等AI-09；D1不等 |
| AI-09 | 安全日志和分析器 | T1计量最小接口→T2 fallback/context→T3新事件分析 | 分类器业务补丁交AI-06，不能越权 |
| AI-10 | 导出开发（不再只读） | E1正文/附件私有快照、后台准备、失败不空导出 | 需要SQL读取变更交AI-02合，不写sql |
| AI-11 | 源物化生命周期开发 | 停接单/协作取消/共享消费者/幂等与屏障 | 不写main，宿主补丁交AI-03；native窗口排队 |
| AI-12 | 独立验收，不写产品 | 修验收观察口径/独立trace目录，准备定向期望 | 有明确C1再验修包；最终同版C2规模矩阵 |

“多个AI同步”不是12个同时修改一份代码。以上独占文件开发和轻量必要回归可以同时做；接口消费者只等该接口的最小稳定版本，不等对方全部工作结束。

## 4. 永远不能同时改的文件

| 文件/域 | 唯一落盘者 | 别的AI怎么接 |
|---|---|---|
| view.py、save_preparation/sql/partial | AI-02 | AI-05相机、AI-10读取需求交补丁；AI-06只消费receipt |
| __main__.py、window_workspace与计数UI | AI-03 | AI-02 timer/outcome；AI-11退出；AI-09flush提接口 |
| asset_browser/catalog/asset_records | AI-06 | AI-07给几何token，AI-09分类提案 |
| asset_canvas/布局/合成/resource_budget | AI-07 | AI-03给count签名，AI-08给焦点调度语义 |
| image_decode/canvas_navigation | AI-08 | AI-09稳定accounting，AI-11统一服务形状 |
| performance_trace/两分析器 | AI-09 | 业务文件owner接调用，不越界 |
| resource_export/export_workflow | AI-10 | AI-02负责SQL读取，AI-03仅必要UI触发 |
| source_store | AI-11 | AI-08读取依赖、AI-10导出均不并写 |
| material_details/slots/service/library | AI-04 | AI-03资源安装和关闭需求用接口 |
| camera_policy/actions/toolbar/菜单/中文表 | AI-05 | 其他AI交翻译键，别各自改zh_CN |

详细唯一owner清单见OWNER_REGISTER.json。测试各AI新增自己的文件；已有共享测试需登记，不能两个AI同时“修期望”。恢复别人文件做A/B只在隔离副本，禁止把共享树临时换回旧版污染他人运行。

## 5. 接口先定最小表，不再编完整架构论文

| 接口 | 生产者→消费者 | 最低内容 |
|---|---|---|
| SaveOutcome-v2 | AI-02→03/06/12 | operation/project/open_epoch/target/captured revision；disk commit/ack/UI delivery三轴；known实体集合与complete一致 |
| Publication | AI-06→03/07/09 | session/open_epoch/query_generation/publication_id/seq；source/overlay；结果ID顺序版本、complete；流取行/评估/建代理/发布各自计数 |
| LayoutToken | AI-07↔06 | 不透明无proxy持有；项目/query/result-order/geometry/grid/viewport/pack版本验证，失败走异步布局 |
| CountPublication | AI-03→07/06消费者 | basis/scope/revision/unknown/complete；drawn由AI-07实际paint产生；计数不是model总长别名 |
| Accounting | AI-09→08/11 | 每task固定owner/epoch与入队时刻；唯一状态转移；异常诊断不损业务；失效明确 |
| Shutdown service | AI-11/08→03 | stop_accepting/cancel_pending/is_drained或同等；用户确认后调用、运行工作安全完成/丢弃、不GUI无限join |

每个接口一页字段＋转换表＋一个示例足够。planned/current分栏；不能让消费者读取仅写在文档中未实现的属性。接收方只确认本次需要的字段即可开始。

## 6. 验收标准：正确性硬门槛＋性能工作量＋真实可见结果

所有数值是本轮目标/门槛，不是现在已达到的结果。参考机记录OS/CPU/RAM/GPU/盘/Python/Qt/DPI/窗口/log模式；原生测量与offscreen接口测试分开。

### 通用硬门槛

- 不崩溃、不静默丢内容；数据/原件附件/未知字段和保存中新编辑保持正确。
- 完成聚合结果一逻辑ID一行、顺序等独立期望；checkpoint不是完成结果。
- 不越scope/revision/generation，旧后台回调不覆盖新结果/正文/计数/相机。
- 未知与失败不显示0/空文件/已成功；完整结果与阶段准备状态区别明确。
- 用户取消关闭/保存、重试和另存后引用正确；正常行为不因性能优化改变。

### 交互/性能要求

- 沿既定目标：参考机原生输入→首个真实可见反馈P95≤100ms、max≤250ms，保存确认→终态可见P95≤100ms；提示文本有API调用不算用户看到。小包先接口回归，原生目标由AI-12固定候选验，不逼每个AI独自跑全应用。
- 暖已完成查询的ID层命中不重新扫描全目录/整表过滤；详细proxy若按预算退役可仅为可见范围恢复，不能强求“零建proxy”而常驻十万对象。
- 有效布局快照回切不启动新全量pack；几何变化拒绝旧快照。热切换仍需测model/索引/绘制，不以read=0当不卡。
- 首屏工作不随全库图片BLOB量增长：先当前范围/有界metadata，不能等待全库目录；提供冷/热首个可操作、全库ready分阶段。不能用tab落位、非空bounds或len(model)冒充可操作/绘制。
- 详细代理/card驻留受可见/预取与字节预算控制，允许必要全结果O(N)紧凑几何/ID；历史查询不线性堆重对象。预算估算不是RSS。主线程gen2/dispatch自然长尾在独占before/after明显下降；仅微基准快不算100k完成。≥250ms交互停顿若仍复现必须列剩余阻塞，不能把原因改名就通过。
- 暖态可见区域清晰恢复以当前revision的实际绘制覆盖判定，含真实overview/composite/tile。逐项记录焦点先清晰时间/全视口恢复时间；冷读、格式极端和错误源单列，不拿暖小图时间承诺所有情况。停止后长期未恢复/空白仍失败。目标阈值沿总计划已定；未定场景先声明参考工作量与有界等待，不在测试后为通过改阈值。
- 只要求相关负载的真实改善和目标满足；不凭对象数下降自动声称GC已解决、不以RSS短平台证伪泄漏。剩余性能缺口不能写成完成。

### 真实功能特别核验

保存：磁盘commit、GUI ack、dirty/新编辑三个轴分别判定。导出：核正文和附件关系/内容，不只产物存在。相机：真实screen变换/中心/留白。材质：自然paint错误帧与真实鼠标。退出：相关任务/资源安全收尾和真实进程退出，不只session_end。日志：真实flush解析、缺证据标unknown，自己不能成为业务故障源。

## 7. 实验并行规则

可同时：不同文件编写、只读代码审查、轻量纯函数/小夹具测试、整理已有raw。不能同时：100k安装/GC、原生退出/绘制性能、磁盘冷读/大哈希。由AI-01发实际start/end窗口，自己的任务结束立即交还。外部用户应用保留，无法独占则标干扰，不假装冷盘/空闲。

不要让所有人各自跑全test_view/全部目录测试。作者跑最小相关回归，AI-12固定候选独立核准；同类低级失败先修包，不能明知保存Qt槽崩溃还反复跑所有GUIcase。

## 8. 集成顺序与什么时候算这一轮完成

1. S1保存P0、Q1重复行/空结果、T1/T2诊断安全最先各交独立小包；不等重型验收。
2. 接Publication/Geometry/Count，查询与布局共同优化；AI-10导出、AI-04材质、AI-05相机可同时推进。
3. 最小接口到位立即接AI-08任务/AI-03 timer与关闭/AI-11源物化，文件owner不变。
4. AI-01出C1-S/C1-Q/C1-L固定候选，AI-12只复验相关包。修复失败再交新候选，不改旧归档。
5. 全部产品包集成C2，最终100/1000/1万/10万同版矩阵及必要长循环/保存导出退出，不用跨版本拼通过。若一档失败先定位最小回归，不反复跑其他已通过无关档。

结束标准：本轮明确缺陷与性能主线各有独立验收结果；所有BLOCKED明确原因及下一动作，用户没同意不能用“暂时跳过”宣布全部完成。历史SIP访问冲突未根因定位如实保留，不把确定性生命周期改善冒充历史故障根治。新增功能/新架构研究归后续BACKLOG，不纳入本轮。

## 9. 汇报格式：以后只看这六行

> AI编号／包号／当前版本：
> 本包修复的具体问题与正式入口：
> 实际改动文件与diff：
> 必要回归、原始日志、独立验收范围与结果：
> 未完成项／阻塞接口／下一函数：
> 自己进程状态／已释放文件与哈希：

没有新代码或新验收只写“状态无变化”，不再以文档数、自检数、千份证据数量当开发进度。不再自动把各审查意见扩成新的研究任务。

## 10. 发卡方法

本目录AI-01至AI-12各有一张独立卡，把对应卡发给对应AI；README是总表，OWNER_REGISTER是唯一归属。不要把AI-06卡发给AI-11。第一次回复请各AI只确认接手文件、首包与前置是否齐，并开始可做部分；不再要求重写总计划。

本轮总监仅制作方案/代码快照和任务卡；未写产品、未启动应用或测试。三名并行审查子智能体仅只读，未让它们代替这些AI开工。用户仍通过转发卡片实际分派。
