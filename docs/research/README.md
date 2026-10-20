# 材质库改造调研证据（2026-10-19）

本次按用户指定的 `E:\eyes\Agent-Reach` 使用其 v1.5.0 doctor 与文档所列 Exa/mcporter 路由搜索，之后读取上游仓库网页、raw 许可证、源码文件及 npm 官方包元数据。

- `material-library-upstreams-2026-10-19.json`：GitHub 公开 API 元数据查询的失败记录，本机出口遇到 403 限流，不能据此声称仓库不存在或未维护。
- `license-evidence.json`：raw 许可证读取状态、原始地址及短摘要；分支内容为当时快照，不等于固定版本承诺。
- `upstream-licenses/`：成功取得的许可证文本。
- `../../third_party/material-library/upstream-lock.json`：实际取得源码/制品的固定版本、内容校验与用途状态。

Trilium 的 develop/LICENSE 首次请求返回 404；补查后以 main 或固定提交 LICENSE 为准，失败记录仍保留说明。

后续用户重新明确要求 UE 收取插件并把三种材质列为必做：当前主方案已据此重写。`material-collection-research-2026-10-19.json` 记录本次补查的仓库与固定提交；`material-collection-evidence/` 保存补查结果。HyperVault 源码与 AssetPort 三种母材质、默认纹理已取得并记录 hash，尚未编译/导入验证。PBR-Viewer 的许可未确认，因此未复制其应用代码。旧文档的“UE 延期”说明只保留为历史范围，不代表当前方案。
