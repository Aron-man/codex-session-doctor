# Codex Session Doctor

本地 Codex 会话诊断工具：扫描本机活动和归档 JSONL 日志，在命令行查看扫描结果，并通过只监听 `127.0.0.1` 的网页查看用量、覆盖情况和可审阅的改进建议。原始日志只读；本地账本存放在用户数据目录。

## 安装

发布 Release 后可使用下方安装入口。当前仓库尚未发布可下载的 Release，此命令须在发布后使用。

```sh
curl -fsSL https://raw.githubusercontent.com/Aron-man/codex-session-doctor/main/install.sh | sh -s -- --repo Aron-man/codex-session-doctor
```

安装器根据 macOS/Linux 和 arm64/amd64 下载相应的 GitHub Release 归档，验证 SHA-256 后将 `codex-doctor` 安装到 `~/.local/bin`；安装不需要 Python、sudo，也不会启动服务。请确保该目录在 `PATH` 中。指定版本和安装目录可用 `--version v0.1.0 --install-dir DIR`。再次运行同一安装命令即更新；验证或下载失败时保留原有可执行文件。

## 首次运行

```sh
codex-doctor --version
codex-doctor start --open
codex-doctor status
codex-doctor stop
```

网页地址为 [http://127.0.0.1:8768](http://127.0.0.1:8768)。`serve` 在前台运行，`start` 在后台运行；`scan` 手动扫描并输出 JSON 摘要。`--codex-home PATH` 可重复指定日志根目录，`--data-dir PATH` 选择独立账本，`--port PORT` 改变监听端口。首次导入历史日志可能耗时；之后只处理新文件和新增的完整行。

默认数据目录优先使用 `CODEX_DOCTOR_DATA_DIR`，其次是 `$XDG_STATE_HOME/codex-session-doctor`，最后是 `~/.local/state/codex-session-doctor`。卸载时停止服务，再删除 `~/.local/bin/codex-doctor`；数据目录默认保留，可按需自行删除。

“本周额度”需本机安装并登录 Codex CLI 才能读取官方账号额度。缺少或读取失败时，日志诊断仍可用。页面的单会话“本周额度占用”根据本机日志中的已知模型用量及官方快照作相对估算，不是官方单会话扣额；其他设备、未知模型以及未落地日志不在估算内。

诊断建议是审阅线索，不会自动修改 Codex 配置、项目代码或日志。工具不保存完整对话、工具输出或凭据。不可读来源、坏行、超大跳过行、缺失用量和累计重置会显示为覆盖告警；没有用量记录不代表确认零消耗。单行超过 32 MiB 会跳过并记录。

## 从源码运行与开发

源码运行需要 Python 3.9+，打包客户端不需要。`./run.sh start` 使用项目内的 `.data` 保存开发账本。运行测试：

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
node tests/test_prompt_ui.js
```

构建独立客户端使用 Python 3.13 隔离环境，安装 `requirements-build.txt`，然后运行 `python scripts/build_release.py`。脚本只为当前主机平台生成 `dist/codex-doctor-<os>-<arch>.tar.gz` 和对应 `.sha256`。归档内仅有可执行文件、LICENSE 和 README。

macOS arm64 需完成本机独立客户端构建、隔离安装与启动冒烟；macOS amd64、Linux amd64 和 Linux arm64 的工作流已配置，实际构建与运行结果需以 GitHub CI 为准。

项目使用 MIT License。
