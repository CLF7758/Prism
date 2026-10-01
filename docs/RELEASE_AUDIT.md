# 发布检查 / Release audit

日期：2026-10-01。范围：当前工作区源码、锁定的 Windows 运行依赖与本机安装流程。此记录不是完整法律或安全认证。

## 已检查和修正

- 保留 GPL-3.0-or-later 与上游版权声明；NOTICE 明确为代码派生项目。
- 修正 PyQt-WebEngine 许可：GPL v3 / 商业双许可，不能写成 LGPL。原生 Qt 的许可另行处理。
- 收集 18 个实际运行依赖的许可与版本，安装包附带 `licenses`。
- 实测 PyAV 19.0.0 随包 FFmpeg 报告版本 9.0.2、LGPL v3-or-later；其目录还包含 x264、x265、libvpx、libopus 等库，不能只用 PyAV 的 BSD 许可概括全部内容。
- `lxml 5.1.0` 命中 PYSEC-2026-87，升级至 6.1.0；锁定运行依赖复扫未发现已知漏洞。数据库可能更新，此结论只对应扫描时点。
- 276 个源码／配置文件的常见 GitHub、AWS、OpenAI 密钥及私钥模式扫描无命中；不等同于排除所有形式的秘密。
- Python 编译与 flake8 致命错误规则检查通过。
- 完整 flake8 扫描还存在历史样式／未使用导入告警（首次扫描 134 条）。CI 只把正确性错误作为阻断项，不宣称全量风格检查已通过。
- 134 项导入、导出、保存、浏览器桥接、剪贴板隐私、图标和版本等相关回归测试通过。本轮不是完整全量测试。
- 追加的模型预览与中文翻译测试 37 项通过，总计 171 项相关测试通过。
- 最终安装包升级安装成功，安装后的 0.3.4 程序启动保持响应，项目文件关联指向安装目录中的新图标。
- 给 Pinterest 页面下载补充 20 秒超时和 5 MiB 大小上限。
- 测试工具遇到失败、超时或异常退出会返回失败状态，避免 CI 假通过。
- 发布脚本改为安装版与同版本源码，先创建草稿 Release。
- README 中英文使用相对链接和真实程序截图；移除已失效旧仓库推广入口。

## 发布二进制前仍需完成

1. 核对每个随包原生库（尤其 Qt/Chromium、FFmpeg 与 x264/x265）的完整许可、版权、精确源码版本、构建配置和源码取得方式。`packaging/licenses` 是已收集的声明，不代表供应链对应源码核查已完成。
2. 同一 Release 提供本项目对应源码与构建脚本。项目源码 ZIP 不替代第三方库的对应源码义务。
3. 新公开仓库为 https://github.com/CLF7758/Prism 。安装包上传为草稿 Release，原生依赖源码核查完成前不公开发布二进制附件。
4. 未在干净虚拟机验证全部格式、中文输入、外部软件拖放和所有 DPI 配置；安装程序尚未代码签名。

## 安全边界

- 浏览器桥接仅绑定 127.0.0.1；限制请求体、URL 协议和来源。它没有完整的用户身份认证，任意本地进程或符合请求条件的扩展可能发起请求；不要把它视为强认证接口。
- 剪贴板敏感来源过滤是尽力识别，不能保证覆盖所有密码管理器与敏感数据。
- 搜索关键模式未发现 Python 动态执行、pickle 反序列化、shell=True 或关闭 TLS 验证的调用；不是完整渗透测试。
- ZIP 导入已有条目数、解压大小和路径安全限制；相关导入回归测试已通过。

## 官方依据

- PyQt-WebEngine： https://www.riverbankcomputing.com/software/pyqtwebengine/intro
- Qt WebEngine： https://doc.qt.io/qt-6/qtwebengine-licensing.html
- GPL 对应源码： https://www.gnu.org/licenses/gpl-faq.html
- PyAV： https://github.com/PyAV-Org/PyAV

English: This is a scoped release-readiness review, not legal/security certification. Runtime dependency scanning, focused regression tests and common secret-pattern scanning passed after fixes. Complete native dependency source/license verification, clean-VM validation and code signing remain outstanding. The new public repository is https://github.com/CLF7758/Prism; binary assets are kept in a draft release pending native dependency verification.
