# 工作区目录说明

本次整理日期：2026-11-03。整理文件位置，不改变程序架构、导入路径或启动方式。

| 目录 | 用途 |
|---|---|
| `prism/` | 应用源码、界面、内置资源与渲染代码 |
| `tests/` | 回归测试及测试所需的固定样本 |
| `tools/` | 可以重复使用的开发、诊断和维护工具 |
| `benchmarks/` | 性能基准入口 |
| `docs/` | 使用、设计、开发和发布文档 |
| `coordination/` | 继续工作文档、调查结论及验收记录 |
| `coordination/archive/` | 已归档的散落记录；本次两份根目录临时记录保存在其 `root-notes-20261103/` 中 |
| `packaging/` | 安装器和发布配套配置 |
| `requirements/` | 依赖清单和锁定版本 |
| `third_party/` | 第三方配套文件 |
| `browser-extension/` | 浏览器扩展 |
| `.github/` | 仓库自动化配置 |
| `.venv/`、`Prism.egg-info/` | 当前开发环境及可编辑安装元数据，保留原位 |
| `_bmad/`、`_bmad-output/`、`.reasonix/` | 开发辅助工具、配置指定的输出位置和任务记录，保留原位 |
| `coordination/archive/pyscn-20261028/` | 从根目录 `.pyscn/` 归档的架构扫描报告，5 个文件均已校验 SHA-256；扫描工具以后仍可能重新生成 `.pyscn/` |
| `cache/`、`artifacts/`、`build/`、`dist/`、`release/` | 缓存、测试和构建输出；运行工具时可能重新生成，不是源码 |

根目录保留启动批处理、README、许可证、项目配置、测试入口和 `Prism.spec`。这些文件由启动、测试或打包工具按现有位置使用。

67 个一次性诊断脚本已按用户指定移到根目录的 `SHANCHU/`，逐个 SHA-256 校验一致，未删除。`coordination/待手动删除的67个脚本.txt` 和 `coordination/cleanup-unused-diagnostics-20261103.json` 保留移动前的位置与大小，作为历史清单。清单不包括 `setup.py` 或 `conftest.py`。

真实工程的清理前备份位于工作区之外：`C:/Users/admin/Desktop/Prism-precious/cleanup-20261103-120505/`。

后续可重复使用的工具放 `tools/`，工作记录放 `coordination/`，长期文档放 `docs/`；避免继续往根目录写一次性调查文件。

## 全目录复查结果

2026-11-03 再次检查：`.venv/` 约 1078 MiB，`tests/` 约 122 MiB，`prism/` 约 29 MiB。这些是运行环境、测试及样本、应用源码和资源，不能按垃圾清除。`third_party/` 中的样本被测试直接引用，`packaging/licenses/` 是打包合规材料；两者均保留。

`_bmad/config.toml` 明确把 `_bmad-output/` 指定为开发工具输出目录，因此不搬动工具配置。`.reasonix/` 保存任务及附件记录。它们不属于 Prism 应用的业务模块，也不需要混入源码目录。

本轮缓存删除操作被自动审批以 `blocked by policy` 拒绝，相关命令未执行。15 个新生成的 `prism/**/__pycache__/`、空的 `cache/` 和 `_bmad-output/` 仍保留，没有通过其他方式绕过删除限制。随后独立完成的仅为扫描报告归档。
