# 部署与运维

本文说明 BodyPipeline 的本地演示、机器人侧 Client、中心 Server、letools 工具链和 Visualizer 的部署方式。命令默认在仓库根目录执行。

> 架构与组件边界见[架构设计](architecture.md)。本文使用通用占位路径，现场主机名、用户名、隧道端点和部署根不应写入公开仓库。

## 1. 部署拓扑

```text
机器人主机
├── 机器人驱动 / 相机 / 主从臂系统      # 由机器人厂商或现场环境管理
├── ROS2 Recorder                      # 已有采集服务
├── ROS2 Bridge                        # loopback :8090
├── BodyPipeline Client                # :8080
└── 本地数据盘                          # captures + JSON state

中心服务器
├── BodyPipeline Server                # :8081
├── Viewer Gateway                     # :8082
├── 数据根                              # incoming / raw / work
├── 历史数据导入白名单                   # import_roots
└── 独立 letools / Visualizer 环境      # .runtime/letools-venv
```

Client 与 Server 都固定单 worker。不要把同一个数据根挂载给多个写进程，也不要依赖 NFS 提供跨主机文件锁语义。

## 2. 运行环境

### 2.1 主环境

```bash
curl -LsSf https://astral.sh/uv/0.13.1/install.sh | sh
uv sync --locked
```

| 项目 | 约定 |
| --- | --- |
| uv | `>=0.13.1,<0.14` |
| Python | 默认 3.12；主项目兼容 3.10+ |
| 主虚拟环境 | `.venv/` |
| Web | 原生 HTML / CSS / JavaScript，无 Node 构建步骤 |
| 系统 | Linux 或 macOS，POSIX 文件系统 |

生产服务安装运行依赖即可：

```bash
uv sync --locked --no-dev
```

### 2.2 letools 工具环境

```bash
bash scripts/install_letools.sh
```

脚本会：

- 读取 `environments/letools/pyproject.toml` 与 `uv.lock`；
- 创建 `.runtime/letools-venv/`；
- 安装固定版本 letools；
- 校验并固定上游提交。

Server 配置中的 `letools_bin` 应指向该环境的可执行文件。工具环境与主服务环境隔离，避免 letools 的重依赖污染 Client / Server。

### 2.3 ROS2 Bridge 环境

Bridge 必须使用机器人系统 Python 和 ROS2 Humble 环境，不能把 ROS ABI 替换成 uv 下载的独立 Python。

```bash
cd /path/to/Bodypipeline
uv venv --python /usr/bin/python3 .venv-ros
uv sync --locked --no-dev --active
.venv-ros/bin/python -c 'import h5py, numpy, PIL; print(h5py.__version__)'
source /opt/ros/humble/setup.bash
source /path/to/robot_ws/install/setup.bash
```

实际 Python 路径以机器人系统为准。`rclpy` 必须来自 ROS 环境；同时，Bridge 的配置加载会导入
HDF5 契约模块，因此同一系统 Python ABI 下还必须能正常导入 `h5py`、NumPy 和 Pillow。若平台
wheel 未随同步正确安装，可使用与当前 AgileX ALOHA 相同的兼容安装方式：

```bash
.venv-ros/bin/python -m pip install --no-deps h5py==3.16.0
```

安装后必须重新执行上面的 import 检查并重启 Bridge。不要把 ROS Bridge 切到普通 `.venv`，也不要
用独立 Python 替换机器人 ROS ABI。

## 3. 配置与密钥

### 3.1 环境变量

```text
PIPELINE_TOKEN
PIPELINE_DEVICE_TOKEN
PIPELINE_BRIDGE_TOKEN
```

| 变量 | 要求 |
| --- | --- |
| `PIPELINE_TOKEN` | 至少 24 字符，用于 Server / Viewer 网页登录；Client 本地页面不要求网页密钥 |
| `PIPELINE_DEVICE_TOKEN` | 至少 24 字符，与用户密钥不同，用于 Client 向 Server 上传 |
| `PIPELINE_BRIDGE_TOKEN` | 至少 24 字符，仅用于 Client ↔ Bridge |

多设备 Server 使用 `devices` 注册表时，每台机器人应通过独立 `token_env` 注入密钥。不要把密钥写入 JSON、Git、脚本参数或前端 localStorage。

### 3.2 Client 配置

```json
{
  "mode": "ros2",
  "root": "/var/lib/bodypipeline/client",
  "local_roots": {
    "robot-data": "/var/lib/bodypipeline/captures"
  },
  "server_url": "http://127.0.0.1:8081",
  "bridge_url": "http://127.0.0.1:8090",
  "profile_path": "configs/agilex-aloha-v2.json",
  "min_free_bytes": 10737418240
}
```

| 字段 | 说明 |
| --- | --- |
| `mode` | `mock` 或 `ros2` |
| `root` | Client 状态、锁和默认目录 |
| `local_roots` | 页面可选择的本地存储根 |
| `server_url` | Client 可达的 Server 地址 |
| `bridge_url` | Bridge 地址，通常为 loopback |
| `profile_path` | 机器人 Profile |
| `min_free_bytes` | 录制低水位，低于该值时自动停止 |
| `robot_id` / `project_id` | 与 Server 设备注册一致 |
| `targets` | 远端存储目标 ID 与展示名称 |

`local_roots` 当前要求解析后的路径没有符号链接。旧 AgileX ALOHA recorder 还要求本地路径由受控 ASCII 字符组成。

### 3.3 Server 配置

```json
{
  "root": "/srv/bodypipeline/state",
  "data_root": "/srv/bodypipeline-data",
  "import_roots": [
    "/srv/bodypipeline-imports",
    "/srv/bodypipeline-data"
  ],
  "processing_mode": "real",
  "profiles": {
    "agilex_aloha": {
      "profile_path": "/opt/bodypipeline/configs/agilex-aloha-v2.json",
      "provider": "agilex",
      "raw_formats": ["agilex_joint_camera_hdf5_v1", "agilex_timestamped_directory_v1"],
      "accepted_capture_profile_hashes": []
    },
    "arx_hdf5": {
      "profile_path": "/opt/bodypipeline/configs/arx-x5-hdf5-v1.json",
      "provider": "aloha_hdf5",
      "raw_formats": ["aloha_hdf5_episodes_v1"]
    }
  },
  "default_profile": "agilex_aloha",
  "auto_process": false,
  "viewer_origin": "http://127.0.0.1:8082",
  "letools_bin": "/opt/bodypipeline/.runtime/letools-venv/bin/letools",
  "tianji_provider_bin": "/opt/bodypipeline/.runtime/letools-venv/bin/tianji-provider",
  "viewer_bun": "/opt/bodypipeline/.runtime/viewer-toolchain/node_modules/.bin/bun"
}
```

| 字段 | 说明 |
| --- | --- |
| `root` | Server 状态、锁和 Viewer 缓存 |
| `data_root` | 新上传、原始数据、任务工作区和转换结果 |
| `import_roots` | 允许原地导入的历史数据目录 |
| `processing_mode` | `demo` 或 `real` |
| `profiles` | `runtime_id` 到 Server 受信任 Provider / Profile / 原始格式的注册表 |
| `default_profile` | 未显式绑定设备的默认 Runtime ID；多个 Runtime 时必填 |
| `auto_process` | 是否自动处理单个上传；批量工作流建议 `false` |
| `viewer_origin` | 浏览器可访问的 Viewer 根地址 |
| `letools_bin` | letools CLI |
| `tianji_provider_bin` | Tianji MCAP inspect CLI，与 `letools_bin` 使用同一 Python 环境 |
| `viewer_bun` | Bun 可执行文件，留空时从 `PATH` 查找 |

旧版单 Profile 配置仍兼容：未提供 `profiles` 时，`profile_path` 会生成 Runtime `default`。新部署应使用显式 `profiles`，这样后续新增机器人只需注册新 Runtime，不需要替换所有机器人共用的 Profile。

内置 Provider：`agilex`（AgileX 时间戳流与 HDF5 采集包）、`aloha_hdf5`（上游 ALOHA 风格逐 episode HDF5，按 Profile 声明的字段做 schema 翻译）和 `tianji`（Tianji/Marvin Pro MCAP 录制）。`provider` 必须是这些已注册的 ID；未注册的取值会在启动时直接报错。

`import_roots` 也会被加入数据集预览的允许根。不要配置 `/` 或包含敏感数据的父目录。

### 3.4 多设备注册

```json
{
  "devices": [
    {
      "robot_id": "robot-001",
      "project_id": "teleop",
      "token_env": "ROBOT_DEVICE_TOKEN",
      "targets": ["default"],
      "profile": "agilex_aloha"
    },
    {
      "robot_id": "robot-002",
      "project_id": "teleop",
      "token_env": "ROBOT_002_DEVICE_TOKEN",
      "targets": ["default"],
      "profile": "new_robot_runtime"
    }
  ]
}
```

每个 `token_env` 必须提供不同的有效密钥，`(robot_id, project_id)` 不能重复。`profile` 必须指向 `profiles` 中已注册的 Runtime。Server 用认证后的设备身份选择 Provider/Profile，Client 自报的 `robot_type`、`raw_format` 和 Profile 摘要只能用于一致性校验，不能用于动态路由或加载代码。

同一 Server 的批量转换要求所有 episode 属于同一 Runtime。不同机器人应分别提交任务；不能通过修改 Manifest 把数据强制路由到另一个 Provider。


## 4. 本地演示

```bash
uv sync --locked
uv run --locked python scripts/demo.py
```

| 服务 | 地址 |
| --- | --- |
| Client | `http://127.0.0.1:8080` |
| Server | `http://127.0.0.1:8081` |
| Viewer | `http://127.0.0.1:8082/` |

首次运行生成 `.runtime/local-credentials.json`，权限为 `0600`。使用 `PIPELINE_TOKEN` 登录 Server 与 Viewer；Client 页面直接使用可信设备内网，上传仍由 `PIPELINE_DEVICE_TOKEN` 授权。

基础演示使用 `processing_mode=demo`，不会生成真实 LeRobot 数据。安装工具链后可运行：

```bash
bash scripts/install_letools.sh
uv run --locked python scripts/demo.py --real-processing
```

本地演示的数据保存在 `.runtime/client` 与 `.runtime/server`。

## 5. 机器人侧部署

以下以 systemd 管理三个进程为例。实际 service 名称和路径可按现场调整。

### 5.1 ROS2 Recorder

机器人已有 recorder 继续由机器人环境管理。BodyPipeline 不启动机械臂驱动，也不发送运动命令。

AgileX ALOHA Legacy recorder 可使用仓库中的 headless 启动脚本：

```bash
scripts/run_recorder_headless.sh
```

应先确认 service 模式、数据目录、episode 参数和状态 topic 与 Profile 一致。旧 GUI 若同时启动 recorder，会造成双 owner 和目录冲突。

### 5.2 ROS2 Bridge

```bash
source /opt/ros/humble/setup.bash
source /path/to/robot_ws/install/setup.bash
export PIPELINE_TOKEN='...'
export PIPELINE_DEVICE_TOKEN='...'
export PIPELINE_BRIDGE_TOKEN='...'
uv run --locked --active python -m pipeline.cli bridge \
  --config configs/client.agilex-aloha.json \
  --host 127.0.0.1 \
  --port 8090
```

Bridge 必须监听 loopback。它负责调用 ROS2 CaptureService、等待 recorder 就绪、订阅状态和相机图像。采集写入完成后才允许 Client 进入保留阶段。

适配多机器人或修改共享 Server 时，发布流程使用 [`agents/skills/deploy-robot-adapter/SKILL.md`](../agents/skills/deploy-robot-adapter/SKILL.md)。该 Skill 要求优先为目标机器人启动独立 Server 实例，并验证 service、端口、`root`、`data_root`、Profile 和 Viewer 资源均不重叠；无法证明共享改动完全向后兼容时，不得覆盖现有 Server。

### 5.3 Client

```bash
export PIPELINE_TOKEN='...'
export PIPELINE_DEVICE_TOKEN='...'
export PIPELINE_BRIDGE_TOKEN='...'
uv run --locked python -m pipeline.cli client \
  --config configs/client.agilex-aloha.json \
  --host 0.0.0.0 \
  --port 8080
```

如果 Client 不直接暴露给操作者，可以保持 loopback 并通过 SSH 隧道访问。systemd 模板见 `deploy/pipeline-client.service.example`。

### 5.4 Client 启动检查

```bash
curl -sS http://127.0.0.1:8090/health
curl -sS -H "Authorization: Bearer $PIPELINE_TOKEN" \
  http://127.0.0.1:8080/api/health
```

应确认：

- Bridge 能找到 recorder service；
- 相机 topic 有新鲜帧；
- 四路关节 topic 连续；
- Client 数据盘高于 `min_free_bytes`；
- 没有旧 GUI / 旧 recorder 同时占用采集服务。

## 6. Server 部署

```bash
export PIPELINE_TOKEN='...'
export PIPELINE_DEVICE_TOKEN='...'
export ROBOT_DEVICE_TOKEN='...'
uv run --locked python -m pipeline.cli server \
  --config configs/server.json \
  --host 127.0.0.1 \
  --port 8081
```

生产建议让 Server 只监听 loopback，由反向代理或 SSH 隧道提供访问。Server 启动时会恢复上传、任务和预览状态；未完成的转换任务会标为 `INTERRUPTED`，不会自动重复执行外部工具。

### 6.1 数据根

```text
<data_root>/<robot_type>/
├── incoming/                    # 单文件 HDF5 分块暂存
├── raw/                         # 已提交的 episode.hdf5
└── work/                        # aligned HDF5 + LeRobot 数据集
```

`data_root` 必须存在且可写。`robot_type` 只能包含字母、数字、下划线和连字符。目录分类不等于转换能力：新增机器人类型仍需在 `profiles` 注册 Provider、Profile 和 `raw_formats`，并完成适配测试。

Server 自动将上传的数据、Manifest、转换产物及转换报告开放给所有本机用户：普通文件为
`0666`（所有用户可读写），目录为 `0777`（所有用户可进入、读写），已有可执行文件保留执行权限。
该策略不依赖服务进程或 Slurm 的 `umask`；转换工具和 Provider 完成元数据写入后，会在开放
数据集预览前再次设置权限，避免 `meta/info.json` 等原子写入文件恢复成 `0600`。
仅处理 Pipeline 管理的数据目录，不修改导入的外部源数据，也不沿对齐符号链接修改其目标。
`state`、账号凭据和服务配置继续使用原有私有权限。

部署时应保证 `data_root` 以上各级父目录允许其他用户进入（`o+x`）；Server 只自动处理
数据根及其内部目录，不会修改上级部署目录。已有数据不会在启动时全盘补权；新上传、续传提交
及新转换会自动应用。若现有数据目录属于其他账号且尚未共享，需由所有者或管理员先授权。

当前 AgileX ALOHA 保留时只上传一个自包含 `episode.hdf5`，Server 不解包原始目录。一次批量转换会为每个
来源生成 `work/<job_id>/aligned/episodeN.hdf5`，再调用一次 letools 生成单个
`work/<job_id>/dataset`。单文件 HDF5 主要减少小文件、目录遍历、RPC 和元数据开销；相机仍保留
原始 JPEG 编码，因此不能承诺上传字节数会显著下降。

批量任务最多包含 1000 个 episodes，并要求所有来源属于同一 Runtime、Profile、collection 和采集
模式。Server 会保留逐 episode 诊断；只要任一 episode 失败，就不会生成正式可预览数据集。

### 6.2 历史数据导入

Server 管理员可在页面导入：

- 含 `manifest.json` 和 `episode.hdf5` 的当前采集，或含 `data/episode0` / `raw/episode0` 的旧采集；
- 含 `meta/info.json` 的 LeRobot v2.1 / v3.0 数据集。

导入路径必须位于 `import_roots` 或 `data_root` 下。导入是原地登记，不复制、不移动、不改写。Server 只保存路径、摘要和任务引用。

### 6.3 转换工具检查

```bash
<letools_bin> doctor environment
```

转换时 Server 还会执行：

```text
letools convert <aligned> <dataset> --source-format hdf5 --preset <generated-preset> --to v2.1|v3.0
letools convert <source> <dataset> --source-format lerobot --to v2.1|v3.0
letools validate <dataset>
letools doctor check <dataset> --json ...
```

第二行用于“已有 LeRobot 版本转换”。该路径不读取机器人 Profile，不执行 HDF5 对齐；
来源必须是已登记的 v2.1 / v3.0 数据集，来源和目标版本必须不同。已有 MP4 只做 Letools
版本 remux / 切片，不传额外视频编码参数。来源文件保持只读，结果写入新的
`work/<job_id>/dataset` 并应用共享权限。

Server 固定排除 Doctor 的 kinematics 检查，执行其余 11 项非运动学检查。关节限位不参与转换准入。

## 6.4 Argus 内容分析（可选）

内容分析需要独立的 Python 3.11 环境和固定提交的 Argus 源码：

```bash
bash scripts/install_argus.sh
```

脚本会安装 ffmpeg 依赖检查、克隆固定提交、应用网关补丁并**提交为一次 commit**
（Argus 拒绝在工作区有未提交改动时执行付费运行），然后把路径写进 Server 配置：

```json
{
  "argus_src": "/opt/bodypipeline/.runtime/argus/src",
  "argus_python": "/opt/bodypipeline/.runtime/argus/venv/bin/python",
  "argus_model": "openai/gpt-6-astra",
  "argus_per_episode_budget_usd": 1.0,
  "argus_max_tokens": 12000
}
```

付费标注需要模型密钥：`PIPELINE_ARGUS_API_KEY`，或 Argus 原生的
`OPENROUTER_API_KEYS` / `OPENAI_API_KEY`；不配置时只有本地检查可用。
`PIPELINE_ARGUS_API_KEY` 也可直接作为网关密钥。`argus_base_url` 可指向任意
Chat Completions 兼容网关，此时服务会自动设置
`PIPELINE_ARGUS_GENERIC_GATEWAY=1`，补丁会去掉网关不接受的字段。设置后它也是
自动标注页面 `baseURL` 的默认值：

```json
{
  "argus_base_url": "https://aiproxy.mintibot.com:8888/v1",
  "argus_models": ["gpt-6.1-sol"]
}
```

网关补丁 v4 允许直接使用网关返回的模型 ID（不要求 `openai/` 前缀，也不要求
OpenRouter 形状的密钥）；已知模型按 OpenAI 列表价计入费用上限，未知模型在设置
预算上限时会明确拒绝。补丁 v5 还会让 Argus 的分辨率路由调用使用本任务选定的
模型，而不是上游固定的 `openai/gpt-6-sol`。补丁 v6 让 Server 通过
`PIPELINE_ARGUS_PRICES` 注入自定义单价（见下节），并把 dry-run 的
“最终调用最少预留”也扩展到网关模型。

费用上限要求每个模型都有已知单价：Argus 在发请求前按最坏情况预留，预留不出来
就直接拒绝。Argus 固定版本自带的价目表只有 `gpt-6-astra`、`gpt-6-sol`、
`gpt-6.1-sol`，网关上的其他模型需要在 Server 配置里补充单价（USD/token，
顺序是 `[输入, 缓存输入, 输出]`，可只写一档，也可写 `[标准档, 长上下文档]`）：

```json
{
  "argus_prices": {
    "gpt-6.1-terra": [[1e-6, 5e-8, 5e-6], [2e-6, 1e-7, 7.5e-6]],
    "gpt-6-luna": [[1e-7, 1e-8, 5e-7], [2e-7, 2e-8, 7.5e-7]],
    "gpt-5.6-sol": [[4e-6, 4e-7, 2e-5], [8e-6, 8e-7, 3e-5]],
    "gpt-5.6-terra": [[2e-6, 2e-7, 1.2e-5], [4e-6, 4e-7, 1.8e-5]],
    "gpt-5.6-luna": [[2e-7, 2e-8, 1.2e-6], [4e-7, 4e-8, 1.8e-6]]
  }
}
```

也可以写成命名字段（`{"input": ..., "cached_input": ..., "output": ...}`），
键名可带 `openai/` 前缀，Argus 只按最后一段取用。价目表在配置加载时校验，
写错会直接启动失败，而不是等到付费任务被拒。未声明单价的模型在提交时就会返回
400 并列出可用模型，不会先跑完准备、检查和 clips 才在 dispatch 前失败。

跨模态检查依赖 Profile 声明的相机安装关系：

```json
{"id": "left", "topic": "...", "mounting": "wrist_left"}
```

未声明时该检查为"未评估"，不会被当作通过。安装补丁会把声明按 Argus 的视角槽位
（`left` / `right` / `exo` / `extraN`）或录制相机名匹配，匹配时会去掉
`observation.images.` 前缀并按 `_` 分段，因此 Profile 的相机 id
（如 `left_wrist`、`head_left`、`egocentric`）也能对应到实际视频流。

### 6.4.1 自动标注任务

侧栏“自动标注”直接用同一个 Argus 环境调用官方 `review` 流程，由 Server
子进程执行。模型访问凭据可在页面按任务填写：每次提交或续跑都可以指定
`baseURL` 与 `API Key`，留空时使用服务器配置或环境变量中的默认值。配置
`argus_base_url` 后，页面会自动把该地址填入 `baseURL`。密钥只保存在
Server 内存中，任务结束、取消或 Server 重启后清除；不会写入任务记录、工作目录
`context.json`、运行日志或导出结果。安装脚本会在安装结束时执行
`python -m review --help`，确认 `--rig`、`--inspection` 等开关存在；若检出目录
中的集成补丁版本过旧，脚本会重置到固定提交并重新应用，不会叠加补丁。

```json
{
  "argus_model": "openai/gpt-6-astra",
  "argus_models": ["openai/gpt-6-astra", "openai/gpt-6.1-sol"],
  "argus_task_budget_usd": 20.0,
  "argus_daily_budget_usd": 100.0,
  "argus_per_episode_budget_usd": 1.0,
  "argus_annotation_concurrency": 8,
  "argus_annotation_timeout": 86400,
  "argus_annotation_reasoning": "",
  "argus_annotation_sample_fps": 0,
  "argus_annotation_inspection": "auto",
  "argus_annotation_field_dictionary": false,
  "argus_annotation_note": ""
}
```

- 提交单位是整个已登记数据集；候选只包含可读取的 LeRobot v2.1 / v3.0
  结果，包括转换、导入、合并和版本转换产物。
- `argus_models` 是页面可选模型的允许列表；`argus_model` 不在其中时，页面默认
  选中列表第一项，提交时再次校验。`argus_annotation_sample_fps` 取 0–10，0 表示
  使用 Argus 默认采样。
- 标注使用独立后台队列，不占用转换、Doctor 或内容分析的队列。任务产物写入
  `annotations/<annotation_id>/`，Argus 原始输出保留在其中的 `argus*` 子目录；
  数据集本身只读。
- 取消会终止整个 Argus 进程组。Server 重启时运行中和排队中的任务都会标记为中断
  （内存中的密钥无法恢复），失败、中断、取消和部分完成均可续跑，已完成的 Episode
  和费用保留。续跑时需重新填写 API Key，除非服务器配置了默认密钥。若续跑时 Argus
  拒绝上一轮的准备产物，旧输出会归档到 `argus-attempt-*` 后重新开始，已结算费用
  仍计入任务与每日账本。
- 页面填写的 `baseURL` 必须是 `http://` 或 `https://` 的 Chat Completions 网关
  根地址，不能包含用户名、密码、查询参数或片段；填写后会启用
  `PIPELINE_ARGUS_GENERIC_GATEWAY=1`。未填写且服务器也没有默认值的 baseURL 时，
  Argus 使用其上游默认地址。
- 填写 baseURL 与 API Key 后，页面会自动请求 `<baseURL>/models` 获取模型列表；
  也可点“获取模型列表”手动刷新。`/models` 只用于读取目录，返回的模型 ID 直接作为
  Argus 的 `--model`。使用按任务填写的 baseURL 时，模型不受 `argus_models`
  允许列表限制；使用服务器默认上游时仍按允许列表校验。
- 模型必须能算出单价，否则费用上限无法保证：`argus_prices` 与 Argus 自带价目表
  都没有的模型，页面在下拉里标“无单价”、禁用提交并说明原因，Server 端同样返回
  400。页面默认模型优先选一个已有单价的；`gpt-image-*`、`codex-auto-review`
  这类非对话模型没有文本单价，因此不会被选中。
- `/api/annotations/models` 在 Server 侧代理该请求：密钥只作为
  `Authorization: Bearer` 发送，不写入日志或响应，错误信息也会脱敏。
- Argus 退出码非 0 但已产生部分标注时记为“部分完成”，逐 Episode 结果仍可查看
  和导出。费用账本与内容分析共用每日预算，按已结算费用统计。
- 每个成功 Episode 在页面上有“查看预览”：播放 Argus 生成的各相机片段
  （`argus/clips/`，主相机、腕部、其他相机与深度按目录自动发现），并把
  `event_labels`、`key_events`、`state_changes` 和完成判定画在时间线上；
  点击时间线或事件行会跳转到对应时刻。`原始结果` 仍显示完整 JSON。
  预览视频通过 `/api/annotations/<id>/episodes/<episode>/clips[..]` 提供，
  只接受已发现的相机名，返回 `video/mp4` 并支持 HTTP Range 拖动进度；
  clips 阶段未生成视频时页面只展示时间线。
- 机器人差异只来自 Provider / Profile：`annotation_context()` 提供 Argus rig 与
  相机安装关系，页面和 API 不按机器人型号分支。当前 Profile 默认使用
  `teleop_arms`；其他采集方式应在 Profile 中声明 `annotation_rig`
  （`teleop_arms` / `handheld_gripper` / `ego_head`）。声明了不支持的值时提交会
  直接报错，不会静默按遥操作标注。
- 标注任务运行期间不能永久删除来源数据集；任务结束后标注结果独立保留，不随
  数据集删除而移除。Doctor 质检逻辑和结论不受影响。

### 6.4.2 从标注生成 Subtask 数据集

标注完成后，详情页的“Subtask 数据集”面板
（`GET /api/annotations/{id}/subtask-export-preview`）会显示可导出 Episode、逐帧覆盖率、
词表和被跳过的标注，底部“生成 Subtask 数据集”按钮
（`POST /api/annotations/{id}/subtask-exports`，202）把标注写成带逐帧
`subtask_index` 的新 LeRobot 数据集。

- 输出是**独立副本**，默认命名 `<原数据集名>-subtasks`，可在提交前改名；
  原数据集只读，导出前后按 `source_snapshot` 校验，不会被就地修改。
- 统一输出 LeRobot **v3.0**：v3.0 来源用 reflink/复制镜像，v2.1 来源先由
  LeTools `convert` 转成 v3.0 再增补，因此同一个标注能覆盖两种格式的来源。
- 写入 `data/**/*.parquet` 的 `subtask_index`（int64）、`meta/subtasks.parquet`
  （`subtask` 为索引列，`subtask_index` 为取值）以及 `meta/info.json` 的
  `features.subtask_index`；`meta/stats.json` 同步补充该字段的统计。
- 帧边界按 `episode_index + frame_index/fps` 计算（`ceil(t*fps - 1e-6)`），不比较
  Parquet 里的 float32 `timestamp`。左右臂重叠的事件按起止时间切分成不重叠区间，
  同一帧上多条事件用 `" | "` 合成一条描述并去重（限 400 字符），相邻同文本合并。
- 没有被任何标注步骤覆盖的帧保留 `subtask_index = -1`，词表文本为
  `未标注 / Unannotated`，页面展示覆盖率。导出不会把整体任务描述当成子任务，
  也不会凭空补一个“空闲”步骤。
- 失败、未返回、无法对应到数据集 Episode 或缺少帧率/帧数的标注会被跳过并在面板里
  列出原因，不会静默丢弃。相同 `subtask` 文本共享编号，按规划时首次出现的顺序编号，
  跨 Episode 共用同一份词表。
- 生成**不需要**再调用模型，也不需要 API Key；它走现有 `JobRunner` 串行队列
  （`operation == "subtask_export"`），复用 `dataset_lock`、Slurm allocation 与
  `share_data_tree`，发布前跑 `letools validate --deep`。
- 成功后自动登记为可预览、可质检、可删除的数据集结果；`meta/bodypipeline-subtask-export.json`
  记录来源标注、来源任务与 `plan_digest`，提交后标注结果若发生变化会拒绝发布而不是
  生成错位的标签。
- 表结构与校验约束：`subtask_index` 在 Arrow schema 里声明为可空，输出前统一
  `cast` 到该 schema，否则 `ParquetWriter` 会因 nullable 不一致失败；worker 的
  spec 文件名固定为 `subtask-spec.json`，避免与队列写到 `<name>.json` 的 stdout
  冲突。

验收命令（在生产机执行，`$AID` 为标注 id）：

```bash
curl -s -b cookie.txt "http://100.64.0.71:8081/api/annotations/$AID/subtask-export-preview" | jq '.summary'
curl -s -b cookie.txt -X POST "http://100.64.0.71:8081/api/annotations/$AID/subtask-exports" \
  -H 'content-type: application/json' -d '{"name": null}' | jq '{id,dataset_name,state}'
# 任务完成后
letools validate "/path/to/example-subtasks" --deep
```

### 6.4.3 在数据预览中查看和修订 Subtask

从数据集管理打开预览后，进入原有的“标注”标签页，可对照视频查看
`subtask_index + meta/subtasks.parquet` 还原的逐段文本与区间。保留原有 composer、
标注列表、Inspector、时间线、视频框选/点选、VQA 和语音入口；AI Subtask 作为普通
Subtask 标注显示和编辑，不另设替代工作台。此页不依赖
`NEXT_PUBLIC_ANNOTATE_BACKEND_URL`，也不会调用预览器自带的 language 导出后端。

- 开始、结束边界使用显式 `[start_frame, end_frame)`；原 Inspector 可修改文本和
  起止秒数（按帧对齐），也可拖动时间线两端、新增或删除区间。同文本不同区间独立编辑。
  Quick-add 在已有覆盖区间内部插入时拆分该区间，不会延长前段跨越间隙；删除后保持 `-1`。
- **保存 Episode**：通过 Viewer gateway 转发给 Server，Subtask 与手动标注一起持久化在
  `state/subtask_draft`，每次保存的审计快照在 `state/subtask_draft_history`。
  草稿版本以数据集为单位；多窗口或其他 Episode 更新后，旧版本保存会返回 409，
  不覆盖新草稿。来源快照变化也会拒绝套用旧草稿。`segments: []` 表示删除全部区间；
  可选的 `atoms` 保存非 Subtask 标注，旧客户端省略此字段不会清空手动标注。
- **保存数据集**：必须先保存当前 Episode 修改，生成独立修订副本，使用整个数据集的最新草稿；没有草稿的
  Episode 保留来源标注。默认名称为 `<原名称>-revised`，仍输出 LeRobot v3.0、逐帧
  `subtask_index`、词表和统计，经深度验证后登记。入队时冻结修订计划，后续草稿保存
  不改变已提交任务；原数据集、Argus 标注结果不改写，不需要 API Key。
- 手动标注以权威 `meta/bodypipeline-manual-annotations.json` sidecar（按 Episode 分组）
  随修订副本携带，重新预览时优先读取；源 `language_persistent` / `language_events`
  Parquet 列保留原样，**本功能尚未把人工修改重新物化到这些列**。外部训练工具若仅读取
  language 列而忽略 sidecar，不会看到手动修订；逐帧 Subtask 列则按修订结果重写。
- 本地未保存备份只用于本窗口恢复提示，不能代替服务端保存。刷新、关闭后可恢复的
  正式版本以 Server 草稿为准。存在回收站 Episode 时，先还原或永久清理再导出。

Server 与 Viewer 均需升级。Gateway 使用部署配置 `server_url` 访问 Server
（默认 `http://127.0.0.1:8081`，分机部署需显式配置），使用服务端凭据转发；浏览器
只访问同源 `/api/subtask-review/{dataset_id}/...`。接口只接受未过期且与已登记 job
匹配的 `bp/<job_id>`，不接受任意本地路径。Parquet 读取在隔离 LeTools Python 中执行。

API（Server 和 Viewer gateway 均提供；沿用登录及 Origin 校验）：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/subtask-review/{id}/episodes/{index}` | 读取原标注或服务端草稿及版本/来源签名 |
| POST | 同上 | `{revision, source_signature, segments: [{start_frame,end_frame,text}]}` 保存 |
| POST | `/api/subtask-review/{id}/exports` | `{revision, source_signature, name?}` 提交修订导出 |
| GET | `/api/subtask-review/{id}/exports/{export_id}` | 查询该数据集的修订导出状态 |

## 6.5 Slurm 转换资源（可选）

转换是 CPU 密集型（每个 episode、每路相机都要重新编码），在计算节点上应通过
Slurm 申请资源，而不是在登录节点上与其他人争抢。

```json
{
  "slurm": {
    "enabled": true,
    "partition": "dev",
    "job_name": "bodypipeline-convert",
    "cpus_per_task": 32,
    "mem": "128G",
    "time": "04:00:00",
    "conversion_workers": 8
  }
}
```

行为：

- 每个转换任务用 `salloc --no-shell` 申请**一个**分配，`doctor environment`、
  `convert`、`validate`、`doctor` 全部通过 `srun --jobid` 在该分配内执行，
  因此只排队一次。
- 分配在任务结束、失败或中断时都会 `scancel`，不会残留占用。
- `--workers` / `--video-workers` 取 `conversion_workers`，
  `--video-codec-threads` 按可用 CPU 均分；`sched_getaffinity` 反映分配内的实际核数。
- 未配置 `slurm` 或 `enabled=false` 时，命令直接在本机执行，行为与之前完全一致。

### 6.5.1 计算节点可见性

Slurm 可以把作业调度到任意分区节点，而转换依赖的路径必须在该节点上真实存在：

| 路径 | 要求 |
| --- | --- |
| `letools_bin` 指向的独立工具环境 | 每个可能被调度的节点都必须可见 |
| `data_root` | 同上 |
| Server 安装目录（`.venv`、`configs`） | 只需 Server 所在节点可见，但若作业读取其中文件则同样要求 |
| `argus_src` / `argus_python` | 用到内容分析或自动标注时同样要求 |

**典型故障**：节点级共享目录只挂载在一部分节点上，作业被调度到其余节点后报

```text
slurmstepd: error: execve(): <toolchain>/bin/letools: No such file or directory
srun: error: <node>: task 0: Exited with exit code 2
```

这不是路径配置错误，而是该节点没有挂载这个文件系统。诊断方法是在目标节点上直接检查：

```bash
srun -p <partition> -w <node> -c 1 --mem=1G -t 00:01:00 --exact \
  bash -c 'test -d <toolchain-dir> && echo OK || echo MISSING'
```

两种处理方式：

1. **把工具链放到所有计算节点都挂载的共享文件系统**（推荐，可解除节点限制）；
2. **临时用 `nodelist` 固定节点**（见下），代价是该节点繁忙时转换会排队。

```json
{
  "slurm": {
    "enabled": true,
    "nodelist": "<node-with-the-toolchain>"
  }
}
```

`nodelist` 会以 `--nodelist` 传给 `salloc`。它只应作为权宜之计：一旦该节点
drain、维护或被长期占满，转换就无法运行。

### 6.5.2 输出重定向的陷阱

`srun` 的 `--output` / `--error` 会把任务自身的标准输出改写到指定文件，
**不会**转发给父进程。Doctor 在 `--ci` 模式下把 JSON 报告写到 stdout，
因此这两个参数会让报告文件变成 0 字节，任务被判为"未生成有效报告"。

不要给 `srun` 加 `--output` / `--error`；父进程已经把它重定向到任务工作目录。

## 7. Viewer

Visualizer 由 Server 随服务常驻启动（只启动一个 Next.js 进程，不绑定任何数据集），
Viewer Gateway 负责代理页面并按数据集提供文件：

```bash
uv run --locked python -m pipeline.cli viewer \
  --config configs/server.json \
  --host 127.0.0.1 \
  --port 8082
```

要求：

- `viewer_origin` 必须是操作者浏览器可访问的地址；
- Bun 位于 `viewer_bun` 或服务 `PATH` 中；
- `viewer_ui_port` 不被其他进程占用（`viewer_data_port` 已不再使用，旧配置中保留无害）；
- 预览链接默认 30 分钟过期（`viewer_ttl`），过期后文件与 Doctor 返回 404；
- Viewer 入口与 Server 共用 `PIPELINE_TOKEN`：浏览器未登录时返回登录页，
  `/_data/*`、Doctor 与视频 Range 未授权时返回 401；仍建议只绑定内网或 Tailscale 地址。

Server 页面点击“预览数据”只会把该数据集登记到网关并打开
`<viewer_origin>/bp/<job_id>/episode_0`，不会启动或停止任何进程，因此可以同时打开多个数据集。
Visualizer 退出后 Server 会在约 30 秒内自动拉起；Visualizer 尚未就绪时网关显示等待页，
链接过期或服务不可用时返回不可用状态，而不是登录页。

首次启动会构建一次 Visualizer（数据源地址会编译进浏览器包），之后仅在源码补丁或
`viewer_origin` 变化时才重新构建。

## 8. 网络与隧道

开发机可通过 SSH 本地端口转发访问受控网络中的服务。下面的主机和用户均为占位符：

```bash
ssh -N -L 18080:127.0.0.1:8080 <robot-host>
ssh -N -L 18081:127.0.0.1:8081 <server-host>
ssh -N -L 18082:127.0.0.1:8082 <server-host>
```

| 页面 | 本地地址 |
| --- | --- |
| Client | `http://127.0.0.1:18080` |
| Server | `http://127.0.0.1:18081` |
| Viewer | `http://127.0.0.1:18082/` |

关闭开发机浏览器隧道不会中断机器人到 Server 的上传隧道。若端口在监听但页面无响应，应先检查 SSH 转发日志，不能只看本地端口。

## 9. API 边界

页面不需要展示接口文档。服务 API 仅作为前后端和部署脚本契约使用，主要分组如下：

| 分组 | 路径 |
| --- | --- |
| Client 采集 | `/api/captures*`、`/api/cameras/*/frame` |
| Client 数据回放 | `/api/replay/*`，页面 `/#replay` |
| Client 健康与目标 | `/api/health`、`/api/targets` |
| Server 设备上传 | `/device/uploads*`、`/device/targets` |
| Server 数据集与任务 | `/api/collections*`、`/api/jobs*` |
| Server 导入 | `/api/imports` |
| Server 自动标注 | `/api/annotations*`、`/api/annotations/{id}/subtask-exports` |
| Server 质检 | `/api/jobs/{id}/checks`、`/api/checks/*` |
| Server Viewer | `POST/DELETE /api/jobs/{id}/viewer`、`GET /api/viewer` |

Server 与 Viewer 管理 API 使用 `PIPELINE_TOKEN` 登录后的 Cookie；Client 本地管理 API 不要求网页密钥；设备上传 API 使用独立设备密钥；Bridge API 使用 `PIPELINE_BRIDGE_TOKEN` 且只允许 loopback。

## 10. 备份、迁移与清理

- 备份 Server 的 `root/state`、`data_root/raw`、`data_root/work` 和导入数据源目录。
- 备份 Client 的 `root/state` 与 `local_roots`。
- 不要只备份 JSON 状态；Manifest、原始文件和报告必须一起备份。
- 旧版本状态迁移使用 `scripts/migrate_state.py` 或 `scripts/migrate_server_data.py`，执行前先做完整文件系统备份。
- 丢弃数据当前隔离在 `discarded/`，不会自动物理删除。
- Server 返回持久化 ACK 后，Client 只清理已成功封存到 HDF5 的 recorder 临时目录
  `raw/episode0/`；`episode.hdf5` 与 `manifest.json` 保留。上传失败、提交失败或 ACK 校验失败
  时不清理任何原始暂存目录。
- 如现场需要删除已上传的 `episode.hdf5`，必须另行走经批准的数据保留和备份流程。
- Doctor 不自动修复、裁剪或删除数据。

### 10.1 迁移安装位置

把 Server 或工具链从一处文件系统迁到另一处时，按顺序处理，避免出现半迁移状态：

1. **先确认目标路径在所有计算节点可见**（见 6.5.1）。只迁移登录节点可见的目录，
   会让 Slurm 作业在其余节点上失败，而且失败是间歇性的 —— 取决于调度结果。
2. **停止 Server 并确认没有运行中的任务**。`RUNNING` 任务在重启后会转为
   `INTERRUPTED`，但已启动的外部工具子进程不会被自动回收。
3. **复制而不是移动**。`cp -a` 到新位置，验证后再切换配置，确认无误才删除旧目录。
   Python 虚拟环境包含绝对路径，跨目录复制后必须重新创建或用 `venv --upgrade` 修正。
4. **更新所有引用该路径的配置项**：`letools_bin`、`argus_src`、`argus_python`、
   `root`、`data_root`、`viewer_bun`，以及 systemd 单元的 `WorkingDirectory` / `ExecStart`。
5. **备份旧配置与单元**，迁移后重启并验证：`/healthz`、一次导入、一次转换、
   一次 Doctor、一次内容分析。
6. **权限按最小可用设置**。共享文件系统常对所有人可写；项目目录、状态目录和凭据
   不应沿用这种默认值。

迁移后旧任务的记录里仍保存着旧路径。Server 会按配置允许的根目录只读解析这些路径，
不会自动搬迁历史结果；确认不再需要旧目录后再手工归档。

## 11. 故障排查

### Client 无法连接

```bash
curl -sS http://127.0.0.1:8090/health
curl -sS http://127.0.0.1:8080/healthz
systemctl --user status bodypipeline-bridge bodypipeline-client
```

检查 Bridge 是否加载了 ROS 环境、`PIPELINE_BRIDGE_TOKEN` 是否一致、Client 配置中的 `bridge_url` 是否正确。

### 实时预览没有画面

先区分三种情况：

1. `/api/cameras/<id>/frame` 返回 503：没有新鲜帧，检查相机 topic、Bridge 和驱动。
2. 返回有效 JPEG 但接近全黑：链路正常，检查曝光、光照、镜头遮挡和相机输出。
3. 浏览器不显示但 curl 能拿到图片：检查浏览器控制台、缓存和页面静态资源。

录制的原始文件与低分辨率预览是两条路径，不能用预览结果替代录制完整性验收。

### 转换报时间对齐越界

查看任务中的 `episode_diagnostics`，确认具体 episode、流、帧号和误差。先检查 recorder 启动瞬态、topic 连续性、时间戳单位和磁盘写入，不要直接放宽 `max_camera_skew_ms` / `max_action_age_ms`。AgileX ALOHA 已知启动空洞见[recorder 修复文档](robots/agilex-recorder-fix.md)。

### 转换作业在 Slurm 节点上找不到工具链

```text
slurmstepd: error: execve(): <toolchain>/bin/letools: No such file or directory
srun: error: <node>: task 0: Exited with exit code 2
```

该节点没有挂载工具链所在文件系统。按 6.5.1 在目标节点上确认可见性，并把工具链
放到全节点共享的路径；临时可用 `slurm.nodelist` 固定到可见节点。

### Doctor 报告为空、任务报"未生成有效报告"

若任务日志里只有一行 `srun: error: ... Exited with exit code 1`，而报告文件是 0 字节，
检查是否为 `srun` 添加了 `--output` / `--error`：两者会把 Doctor 写到 stdout 的 JSON
丢弃（见 6.5.2）。Doctor 以 `--fail-on warn` 运行时 exit 1 属于正常的质量结论，
报告仍然有效。

### Doctor 不可用

```bash
<letools_bin> doctor environment
<letools_bin> --version
```

确认 `letools_bin` 指向独立工具环境、固定提交未变化、Bun 可用于 Visualizer，且 Server 进程有权限读取数据集。

### 页面显示转换结果不存在

转换结果只在该任务达到可预览状态且 `work/<job_id>/dataset` 存在时可用。时间对齐失败、配置阻断或转换中断的任务只有诊断目录，没有可预览数据集。历史任务可通过任务记录中的只读路径兼容访问，但不会自动迁移。
