# Codex Session Doctor

本地 Codex 会话诊断工具：扫描本机活动和归档 JSONL 日志，在命令行查看扫描结果，并通过只监听 `127.0.0.1` 的网页查看用量、覆盖情况和可审阅的改进建议。原始日志只读；本地账本存放在用户数据目录。

## 安装

安装包通过 [GitHub Releases](https://github.com/Aron-man/codex-session-doctor/releases) 发布。已有稳定版 Release 时，可用下方命令安装最新版本。

```sh
curl -fsSL https://raw.githubusercontent.com/Aron-man/codex-session-doctor/main/install.sh | sh -s -- --repo Aron-man/codex-session-doctor
```

安装器支持 Apple Silicon Mac（arm64）以及 Linux arm64/amd64，下载相应的 GitHub Release 归档，验证 SHA-256 后将 `codex-doctor` 安装到 `~/.local/bin`。Intel Mac 不在支持范围，安装器会在下载前退出。安装不需要 Python、sudo，也不会启动服务。请确保该目录在 `PATH` 中。指定版本和安装目录可用 `--version v0.3.0 --install-dir DIR`。再次运行同一安装命令即更新；验证或下载失败时保留原有可执行文件。

已有服务运行时，安装完成后执行 `codex-doctor stop`、`codex-doctor start --open` 加载新版；原有本地账本保留。

## 首次运行

```sh
codex-doctor --version
codex-doctor start --open
codex-doctor status
codex-doctor stop
```

网页地址为 [http://127.0.0.1:8768](http://127.0.0.1:8768)。`serve` 在前台运行，`start` 在后台运行；`scan` 手动扫描并输出 JSON 摘要。`--codex-home PATH` 可重复指定日志根目录，`--data-dir PATH` 选择独立账本，`--port PORT` 改变监听端口。首次导入历史日志可能耗时；之后只处理新文件和新增的完整行。

默认数据目录优先使用 `CODEX_DOCTOR_DATA_DIR`，其次是 `$XDG_STATE_HOME/codex-session-doctor`，最后是 `~/.local/state/codex-session-doctor`。卸载时停止服务，再删除 `~/.local/bin/codex-doctor`；数据目录默认保留，可按需自行删除。

“本周额度”需本机安装并登录 Codex CLI 才能读取官方账号额度。缺少或读取失败时，日志诊断仍可用。列表和详情同时显示会话自身与含子任务的估算，两者采用相同官方周窗口、快照和全库已知模型权重分母；页面时间筛选只影响历史用量展示。估算不是官方单会话扣额；其他设备、未知模型以及未落地日志不在估算内。组内任一会话含未知模型时，组估算显示无法完整估算。会话总 token 是历史统计；若该会话只有周期外的历史用量，页面显示“本周期无记录”，无用量记录则显示“未观察到用量”。

会话列表默认按父任务折叠 `source=subagent` 的子任务，父行展示自身与子孙的汇总，展开行是明细。普通 fork 保持独立；缺失父会话或父链循环会在列表标明。`GET /api/sessions?grouped=1` 返回父组分页和嵌套 `children`，省略 `grouped` 时保留平铺接口与会话自身的原字段。

首页优先展示有具体工具、skill、目标路径和证据的问题；大上下文、长输出和低缓存等宽泛信号放在折叠的运行观察中。本地配置检查只读，列出检查范围、时间、具体发现和检查缺口。详情可查看结构化工具事件；旧调用记录只是有限回退，不能完整还原批量命令或实际工作目录。诊断建议是审阅线索，生成的 Prompt 需要人工预览，不会自动修改 Codex 配置、项目代码或日志。常规账本不保存完整对话、工具输出或凭据；深度诊断会在本机数据目录保存抽样证据与结果。不可读来源、坏行、超大跳过行、缺失用量和累计重置会显示为覆盖告警；没有用量记录不代表确认零消耗。单行超过 32 MiB 会跳过并记录。

会话详情的“深度诊断”先在本机准备抽样证据，显示范围、覆盖缺口与输入量估算，并可复制分析 Prompt。准备、刷新页面和查看历史结果都不会调用模型；只有点击“开始 Codex 分析”才通过本机已安装并登录的 Codex CLI 运行，消耗账号额度。默认使用 `gpt-6-sol`、`medium` 推理档位。运行中可取消；关闭页面不会取消后台任务，重新打开详情可读取最近结果。缺少 CLI、未登录或执行失败时，已准备的 Prompt 仍可复制，结果中的实际用量可能不可得。

深度诊断给出证据与反证、建议、备选、取舍、验证及可复制的修复或复盘 Prompt；公开资料链接是评审依据，并非真人专家逐案审阅。证据包有会话数、事件数与文本长度上限，页面会标出抽样和缺口；结论须结合原始位置人工核对。深度诊断不会自动修复项目。首页及详情原有规则发现统一标为“规则线索（待核对）”；其 `high` 表示事件证据可信度，不代表问题严重性。

## 从源码运行与开发

源码运行需要 Python 3.9+，打包客户端不需要。`./run.sh start` 使用项目内的 `.data` 保存开发账本。运行测试：

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
node tests/test_prompt_ui.js
```

构建独立客户端使用 Python 3.13 隔离环境，安装 `requirements-build.txt`，然后运行 `python scripts/build_release.py`。脚本只为当前主机平台生成 `dist/codex-doctor-<os>-<arch>.tar.gz` 和对应 `.sha256`。归档内仅有可执行文件、LICENSE 和 README。

发布流程覆盖 macOS arm64、Linux amd64 和 Linux arm64；每个平台都必须通过源码测试、独立打包、启动与网页资源检查后才发布。验证环境为 macOS 14（Apple Silicon）及 Ubuntu 22.04；Linux 二进制需要 glibc 2.35 或兼容环境。各版本实际结果见 [Release 工作流](https://github.com/Aron-man/codex-session-doctor/actions/workflows/release.yml)。

项目使用 MIT License。
