# Server 部署源码快照

本分支于 2026-11-11 同步主 Server 的运行源码，来源版本为
`20261111-server-list-compact-430cddad`。这是源码快照，不是生产运行目录备份。

## 同步范围

- `pipeline/server/`：API、接收、转换、质检、标注、数据集管理与 Viewer。
- `pipeline/shared/`、`pipeline/web/`、`pipeline/cli.py`：Server 依赖的公共逻辑和页面。
- 部署包包含的 Server Profile、文档、补丁、脚本、依赖锁文件和测试。
- `configs/server.deployed.example.json`：根据实际 Runtime 绑定生成的脱敏配置示例。
- `configs/agilex-aloha-agx-v2.json` 和
  `configs/g1-sonic11-brainco-approved-20261102.json`：部署额外引用的 Profile；
  保留数据语义与原审批状态，去除设备审计路径及来源指纹。

Client 实现沿用原 `server` 分支，未从部署包同步。原分支中部署包未包含的
通用安装脚本、测试和示例也保留，不应据此认为这些文件均在生产机器上执行过。

## 脱敏边界

不发布生产 `server.json`、环境变量、访问令牌、密码、API Key、设备清单、
实际存储目录、任务日志、数据库、采集数据、模型文件、虚拟环境或 Viewer 缓存。
文档中的实际内网入口及私有模型网关替换为 loopback 或示例地址。
回收站提示中的部署主机代号替换为“服务器”，不改变业务逻辑。
配置示例不保留历史采集 Profile 哈希白名单及设备鉴权绑定。

示例默认启用登录校验、关闭无限额标注与 Slurm 调度，导入根限制在本地示例目录。
部署时必须独立设置 `PIPELINE_TOKEN`、`PIPELINE_DEVICE_TOKEN` 等凭据；
如需自动标注，还需安装独立 Argus 工具链并设置 `PIPELINE_ARGUS_API_KEY`。
外部 LeTools、Argus 和机器人 Provider 依赖仍须按部署文档安装。

Profile 中的 `semantics_approved` / `clock_verified` 仅保留源部署的状态，
不代表新设备已通过现场验证，不得据此跳过接入审批。

## 启动示例

在仓库根目录准备依赖与凭据，并创建 `.runtime/imports` 后运行：

```bash
uv sync --locked
uv run embodied-pipeline server \
  --config configs/server.deployed.example.json \
  --host 127.0.0.1 --port 8081
```

代码同步及脱敏操作不修改、重启或重新部署线上服务，也不改写已有 Git 历史。

## 快照验证

Python 源码语法、JSON 配置与六个 Runtime 的 Profile 引用均已检查。
Server 相关 Python 测试两组共计 470 通过、43 跳过、11 失败；
跳过项包含需要独立数据处理工具链的场景。
11 个失败集中在 `tests/test_static_intervals.py`：部署包中的测试仍传入
`min_motion_seconds`、`jitter_amplitude_seconds`、`min_output_seconds`，
而部署实现已不接受这些策略字段。此处保留运行代码及其原测试，
未为使快照测试通过而修改生产算法；不能把这份快照视作全套测试通过的版本。
这 11 个失败也已在源部署目录中复现，并非本次同步或脱敏引入。
