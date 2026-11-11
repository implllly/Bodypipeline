# 新机器人适配指南

本文说明新增机器人、机器人变体或新遥操作方式时需要修改哪些文件，以及如何证明适配没有破坏现有 AgileX 链路。

目标链路：

```text
Client 预览 → 开始 / 停止采集 → 保留封存 → 上传
→ Server 接收 → 数据集分组 → 时间对齐
→ LeRobot 转换 → deep validate / Doctor → Visualizer
```

> 当前运行时代码不是“改一个 `robot_type` 就支持任意机器人”。AgileX 对齐器固定三相机、四路关节流和 7+7 维；不同布局必须新增 Provider 或扩展对应 Provider 的 normalizer。

## 1. 先判断适配类型

| 场景 | 是否只改配置 | 主要工作 |
| --- | --- | --- |
| 同型号新设备，ROS 接口、数据格式、相机和关节布局完全相同 | 是 | 新设备身份、独立密钥、数据根和 Server 注册 |
| 同型号增加 VR / 手柄 / 新主从臂，但原始格式相同 | 通常不是 | 操作语义、action 来源、控制权、事件、Profile 和测试 |
| ROS2 service / topic / 消息不同 | 否 | 新 Adapter 或修改 Bridge |
| 相机数量、关节维度、目录布局、时间戳格式不同 | 否 | Client 封存、Manifest、Ingest、Provider staging、对齐、检查和测试 |
| 同一 Server 同时处理多个型号或操作方式 | 否 | 设备到 Profile / Provider 的可信路由 |

## 2. 必须收集的机器人事实

在写代码前，先记录以下证据。没有证据的项目写“未核实”，不能靠推测填成已通过。

### 2.1 采集接口

- recorder 的启动、停止和状态查询接口；
- ROS2 service / action / topic 类型与 `srv` / `msg` 定义；
- `start`、`stop` 是否幂等；
- stop 成功是否表示 writer 已停止或已排空；
- 是否允许多个 recorder；
- 失败、超时和重启后的恢复语义；
- 原始输出目录和 episode 编号规则。

### 2.2 观测与动作

- 每路相机 topic、消息类型、编码、分辨率和 QoS；
- 关节流 topic、消息类型、维度和顺序；
- state 来源；
- action 来源；
- 操作输入到控制命令的映射；
- 关节单位、夹爪单位和完整开度 / 单指位移的区别；
- 坐标系、控制模式、饱和、滤波和死区；
- 时间戳来源、单位、时间域和同步方式。

### 2.3 操作方式

主从臂、VR、手柄、键盘或自动策略不应只记录“设备名称”，还要记录：

- 哪个输入产生 action；
- 是否绝对位置、增量位置、速度或末端位姿；
- 是否使用 clutch、接管、重置、暂停、回放；
- 控制权如何切换；
- action 时间戳代表输入、发布还是执行；
- 哪些事件必须写入数据，哪些只属于控制日志。

参考：

- [机器人适配 Skill](../../agents/skills/adapt-robot/SKILL.md)
- [操作与动作语义](../../agents/skills/adapt-robot/references/control-semantics.md)
- [实现定位与接口契约](../../agents/skills/adapt-robot/references/implementation-map.md)
- [接入记录模板](../../agents/skills/adapt-robot/assets/robot-onboarding.template.md)

## 3. 配置改动

### 3.1 Client 配置

新增 `configs/client.<robot>.json`：

| 字段 | 检查内容 |
| --- | --- |
| `mode` | 当前支持 `mock` / `ros2`；新后端需扩展代码 |
| `root` | Client 状态与锁目录 |
| `local_roots` | 页面可选择的存储根 ID 到绝对路径 |
| `server_url` | Client 可达的 Server |
| `bridge_url` | Bridge loopback 地址 |
| `robot_id` / `project_id` | 与 Server 注册完全一致 |
| `profile_path` | 新机器人 Profile |
| `min_free_bytes` | 数据盘低水位 |
| `targets` | 允许上传的远端目标 |

密钥通过环境变量注入：

```text
PIPELINE_TOKEN
PIPELINE_DEVICE_TOKEN
PIPELINE_BRIDGE_TOKEN
```

不要把密钥写入 Client JSON。

### 3.2 机器人 Profile

新增 `configs/<robot>-<operation>.json`，至少包含：

| 字段 | 要求 |
| --- | --- |
| `id` / `version` / `robot_type` | 区分硬件、操作方式和数据语义版本 |
| `control_mode` | 当前运行时只接受 `teleoperation` |
| `cameras` | 相机 ID、标签、topic |
| `service` / `status_topic` | 采集服务与状态 topic |
| `joint_topics` / `pose_topics` | 实际 ROS topic |
| `state_streams` / `action_streams` | 原始落盘流名 |
| `joint_names` / `joint_units` | 逐维顺序、名称和单位 |
| `output_fps` | 转换输出帧率 |
| `max_camera_skew_ms` | 相机时间匹配上限 |
| `max_action_age_ms` | 关节因果保持上限 |
| `minimum_frames` | 最短有效 episode |
| `semantics_approved` | 未核实保持 `false` |
| `clock_verified` | 未核实保持 `false` |

`state_limits` / `action_limits` 可以保留为硬件说明，但不是必需字段，也不得用于数据转换准入。

### 3.3 Server 配置

修改部署环境中的 Server 配置：

- `profiles`：新增 `runtime_id`，声明该机器人的 `profile_path`、静态 `provider`、允许的 `raw_formats`；
- `default_profile`：默认 Runtime；多 Runtime 时必须显式配置；
- `devices`：唯一 `robot_id`、`project_id`、`token_env`、允许目标，以及绑定到该设备的 `profile`（Runtime ID）；
- `data_root`：新机器人数据将落到 `<data_root>/<robot_type>/`；
- `import_roots`：如果需要导入历史数据；
- `accepted_capture_profile_hashes`：优先放在对应 Runtime 下，只加入已核实兼容的旧采集摘要；
- `auto_process`：批量工作流建议 `false`；
- `viewer_origin` / `viewer_bun`：Viewer 入口和工具路径。

目录中新增 `<robot_type>` 只代表存储分类，不代表 Server 已能转换该机器人。真正的转换能力由 Runtime 绑定的 Provider 决定。示例：

```json
{
  "profiles": {
    "new_robot_v1": {
      "profile_path": "configs/new-robot-v1.json",
      "provider": "new_robot",
      "raw_formats": ["new_robot_timestamped_v1"],
      "accepted_capture_profile_hashes": []
    }
  },
  "default_profile": "agilex_aloha",
  "devices": [
    {
      "robot_id": "new-robot-001",
      "project_id": "teleop",
      "token_env": "NEW_ROBOT_DEVICE_TOKEN",
      "targets": ["default"],
      "profile": "new_robot_v1"
    }
  ]
}
```


## 4. 代码改动矩阵

### 4.1 Client / ROS2

| 差异 | 文件 |
| --- | --- |
| ROS2 service、topic、消息相同，只换参数 | `configs/client.<robot>.json`、Profile |
| topic / service 名称变化 | Profile；必要时 `pipeline/client/bridge.py` |
| 相机消息或编码不同 | `pipeline/client/bridge.py` |
| 采集 HTTP / SDK / action 后端不同 | 新增 `pipeline/client/adapters/<robot>.py`，实现 `CaptureAdapter` |
| 页面需要新采集字段 | `pipeline/client/app.py`、`pipeline/web/*` |
| Mock / fixture 新布局 | `pipeline/client/adapters/mock.py`、`tests/` |

统一接口：

```text
health()
start(session_id, raw_root, instruction)
stop(session_id)
status()
frame(camera_id)
close()
```

语义要求：

- `start()` 幂等，只有 writer 真正开始后才返回成功；
- `stop()` 成功表示写入结束或排空；
- 状态未知时进入人工核验，不盲目重试；
- `frame()` 只返回新鲜图像；
- 预览失败不能影响原始录制；
- `close()` 不隐式启停机器人。

### 4.2 原始格式与封存

| 差异 | 文件 |
| --- | --- |
| Recorder 目录布局不同 | `pipeline/shared/hdf5.py`、Client Adapter / Bridge、新 raw format |
| 相机或关节流名不同 | HDF5 packager、Profiles、Provider mapping、Ingest 规则 |
| 时间戳来源或单位不同 | Packager、Provider 对齐、Profile 时间语义和测试 |
| 输入已经是 MCAP / rosbag / 视频 / Parquet | 新 raw format、Ingest、ImportService、Provider |
| 新机器人也输出自包含 HDF5 | 优先复用 HDF5 schema 思路并新版本化 `raw_format`；不要复用 AgileX 名称掩盖语义差异 |
| Manifest 增加字段 | Client、Ingest、collection、lineage 和测试 |

当前 AgileX ALOHA 封存链路：

```text
recorder: episode0/camera/color/<id>/*.jpg
          episode0/arm/jointState/<stream>/*.json
    ↓ package_agilex_capture()
upload:   episode.hdf5
    ↓ AgileX Provider from-HDF5 alignment
convert:  aligned/episodeN.hdf5 + letools-hdf5-preset.json
```

新机器人的 packager 必须校验自身流、维度、编码和时间戳，并生成单文件 HDF5 或明确版本化的其他
`raw_format`。Server 接收层按 `raw_format` 校验文件清单；新格式不能只改 Server 而假设旧 Client
会产出正确文件。

如果复用一个通用 HDF5 容器，也必须为新机器人的 schema 使用新的版本化 `raw_format` 和根属性。
相机关数量、流名称、时间戳单位或 state/action 语义变化时，不能为了复用 AgileX normalizer 而
改名、补零或截断数据。Client 只有在 Server 持久化 ACK 和回执校验通过后，才能清理已封存的
recorder 临时目录；上传失败必须保留暂存源。

### 4.3 Provider 与转换

| 差异 | 文件 |
| --- | --- |
| 输入布局与 AgileX 兼容 | 新 Provider 可复用 `prepare_agilex()`，但仍需验证维度和时间语义 |
| 相机数量、关节维度、流名不同 | 新增 Provider 的 `prepare()` / `quality()` / `conversion_args()`，不要修改 `jobs.py` 的机器人分支 |
| 已是 HDF5 且字段可对齐 | Provider 可直接生成 Letools HDF5 mapping preset，避免解包和中间目录 |
| letools 没有对应输入 provider | 在 Provider 内映射到已有 HDF5 provider，或新增 staging adapter，必要时扩展 letools |
| 目标格式仍是 v2.1 / v3.0 | 复用 `pipeline/server/datasets.py` |
| Profile schema / 维度 / 单位不同 | Provider 的 `validate_profile()` 和 `profile_blockers()` 声明自己的契约 |
| 任务幂等键或工具版本变化 | 通用 `JobRunner` + Provider lineage 元数据 |

Provider 扩展点：

```text
pipeline/server/providers/base.py
pipeline/server/providers/registry.py
pipeline/server/providers/<robot>.py
tests/test_multi_runtime.py
```

AgileX 的布局约束仍保持在：

```text
pipeline/server/alignment.py
pipeline/server/providers/agilex.py
configs/agilex-aloha*.json
```

它们是 `AgileXProvider` 的内部实现，不是通用 JobRunner 的全局假设。新增 Provider 必须显式注册到静态注册表，不能被 Manifest、设备输入或环境变量动态导入。

内容分析与自动标注共用 Provider 的上下文扩展点：

```text
analysis_context(profile)        # 相机安装关系、关节/单位、控制方式等确定性信息
annotation_context(profile)      # Argus rig（annotation_rig，缺省 teleop_arms）+ 上述相机信息
stationary_trim_policy(profile)  # 首尾静止帧检测信号与阈值；缺省读取 Profile 的 trim 块
stationary_interval_policy(profile) # 静止帧检测信号与阈值；缺省复用 trim 信号 + static_intervals 控制项
```

`stationary_trim_policy` 只描述机器人自身运动信号的语义：action 是速度、动作 token
或需要换用其他特征时由 Provider 声明，Server 不按型号猜测。未声明时使用
`observation.state` 和按单位配置的默认阈值。该策略同时用于独立的首尾裁剪预览和原始
数据转换表单中的“转换时自动删除首尾静止帧”；Provider 应在写出的 `meta/info.json`
中声明 `units`，这样单位阈值才能按机器人语义解析；缺少单位时回退到 `default_threshold`。

`stationary_interval_policy` 用于“转换时自动删除静止帧”。默认从 Profile 的
`trim` 块继承运动特征、单位和阈值，再叠加 `static_intervals` 块中的抖动过滤与最短
输出参数（`min_motion_seconds`、`jitter_amplitude_seconds`、`min_output_seconds`）。
任何被判为静止的帧都直接删除，没有最短静止时长，也不保留动作边界余量。阈值或信号
语义无法沿用 `trim` 的 Provider（例如按帧解释阈值）应显式实现该钩子，而不是让通用
任务回退到其他机器人的假设。裁剪会把同一 Episode 的保留片段按顺序前后直接相连；
动作列按区间切片后拼接，因此切点处会出现一次来源动作到来源状态的直接衔接。接入新
机器人时需要确认其 action 语义允许这种拼接。

Profile 只声明确知事实；未声明相机的安装位置时，Argus 会把依赖它的检查标为"未评估"，不会按型号猜。采集方式不是遥操作时，在 Profile 中声明 `annotation_rig`（`teleop_arms` / `handheld_gripper` / `ego_head`），不要在上层页面或 API 里按型号分支。

### 4.4 质检与预览

| 差异 | 文件 |
| --- | --- |
| 新特征名、维度或单位 | `pipeline/server/quality.py`、转换后的 `meta/info.json` 检查和测试 |
| 新事件 / 控制权字段 | Manifest、Provider staging、LeRobot feature、Doctor 覆盖 |
| Viewer 需要显示新相机或特征 | `pipeline/server/viewer.py`、Visualizer 配置与浏览器验收 |
| 历史数据导入新格式 | `pipeline/server/imports.py` |
| 任务结果路径规则变化 | `pipeline/server/datasets.py`、`pipeline/server/storage.py` |

不得增加：

- 关节 / 夹爪软件限位门禁；
- URDF 运动学准入；
- 为让数据通过而自动裁剪 action / state；
- 自动改写原始或转换数据。

### 4.5 多机器人路由

阶段二已经实现 Server 端 Runtime/Provider 路由：

```text
认证设备身份
→ Server 静态 RuntimeCatalog
→ Runtime Profile + raw_formats + Provider
→ Provider 对齐 / 质量策略 / letools 参数
→ 统一 JobRunner
```

新增机器人时通常修改：

- `pipeline/shared/config.py`：仅在 Runtime 配置模型需要新字段时修改；
- `pipeline/server/providers/<robot>.py`：实现机器人差异；
- `pipeline/server/providers/registry.py`：静态注册 Provider（或多机器人适配在独立模块中集中注册）；
- `configs/server*.json`：新增 `profiles` 与设备绑定；
- `pipeline/server/app.py` / `jobs.py` / `quality.py`：只有通用契约变化时才修改；
- `tests/test_multi_runtime.py`：新增设备隔离、格式拒绝、Provider 路由和回归测试。

不能信任 Client 自报的任意 `robot_type` 来选择代码路径。Provider 只能来自静态注册表，上传记录保存认证时绑定的 `runtime_id`；旧上传才按 `robot_type + Profile hash` 做兼容解析。

## 5. 测试

至少运行：

```bash
uv sync --locked
uv lock --check
uv run --locked ruff check pipeline scripts tests
uv run --locked ruff format --check pipeline scripts tests
uv run --locked pytest -q
node --check pipeline/web/app.js
git diff --check
```

新增测试应覆盖：

- start / stop 幂等、超时、owner 冲突和重启恢复；
- 预览新鲜度、503 和预览失败不影响录制；
- Manifest 路径、大小、SHA-256、符号链接和空文件；
- 上传断点续传、损坏块、回执和恢复；
- HDF5 单文件封存、Manifest 文件清单、ACK 后才清理 `raw/episode0/`；
- collection 分组、Runtime 绑定和机器人类型筛选；
- 设备 A 不能上传设备 B 的 Runtime / raw format；同一批次不能混 Runtime；
- 相机数量、分辨率、关节维度、时间戳异常；
- state / action 来源、单位、顺序和操作语义；
- 超出标称限位的数值仍能转换，不被裁剪或拒绝；
- 批量转换上限、同 collection 约束、逐 episode 诊断、任一失败即整体失败、deep validate 和 Doctor；
- 历史原始数据与 LeRobot 数据导入；
- Visualizer 相机和 feature 显示；
- AgileX 原链路回归。

## 6. 真机验收

自动化测试通过不等于机器人数据可用。至少完成：

1. 真实 recorder 启动、停止和写入排空；
2. 所有相机预览与原始录制逐路核对；
3. 四路关节流连续性和时间戳检查；
4. 真实主从臂 / VR / 手柄操作，而不是静态数据；
5. state 与 action 的对应关系、单位、顺序和夹爪标定；
6. 上传、断点续传和 Server 接收；
7. 时间对齐、letools 转换、deep validate、Doctor；
8. Visualizer 逐 episode 预览；
9. 记录未核实项及其对训练语义的影响。

## 7. 文档与 GitHub 协作

复制[接入记录模板](../../agents/skills/adapt-robot/assets/robot-onboarding.template.md)到：

```text
docs/robots/<robot-key>-<operation-key>.md
```

记录：

- 机器人型号、固件、ROS2 版本；
- 采集接口版本；
- topic / service / msg 定义；
- 原始样本目录和脱敏 fixture；
- state / action 逐维映射与单位证据；
- 时间戳来源与同步方式；
- 已核实和未核实项；
- 真实转换、Doctor、Visualizer 结果。

建议每个机器人拆成独立 PR：

```text
feat(robot): add <robot_type> <operation> capture adapter
feat(server): add <raw_format> provider and quality policy
docs(robot): record <robot_type> interface and acceptance evidence
```

PR 至少包含：

- 适配说明和接口证据；
- 脱敏样本或可复现 fixture；
- Adapter / Provider 单元测试；
- 转换、Doctor、Visualizer 的实际输出；
- AgileX 原链路回归；
- 未核实项和风险说明。

## 8. 最小结论

| 目标 | 必须修改 |
| --- | --- |
| 同型号新设备 | Client 配置、设备凭据、Server `devices` |
| 新采集接口但原始格式兼容 | 新 Adapter / Bridge + 测试 |
| 新相机 / 关节布局 | Client 封存 / Manifest + 新 Provider 的对齐、质量、转换参数 + 测试 |
| 多型号共用 Server | 新增 Runtime/Provider、设备绑定、格式白名单和授权/隔离测试 |
| VR / 手柄 / 自动控制 | 操作语义、action 来源、事件、Profile、Provider、转换、QC + 真机验收 |

若 state / action 的语义、单位、顺序、坐标系或时间域无法确认，可以继续做采集和样本管线，但不能宣称数据已经具备可靠训练语义。机器人限位始终由控制器与安全层负责，不属于数据转换准入门禁。

## Client 数据回放适配

需要运动回放时，另行实现 [ReplayProvider 契约](replay-adaptation.md)。它与 CaptureAdapter、Server 转换 Provider 分开注册、共用机器人 Profile；采集或格式转换支持某型号不代表它已能执行回放。通用 Client 页面、任务、鉴权、上传和 Server 不新增型号判断。
## 不合格采集的批量处理契约

Server 的 `invalid_episode_policy=fail|skip` 是通用任务策略，不应在 Client 或通用业务流程中增加机器人型号分支。
Provider 在 `prepare` / `quality` 中确认单条录制的数据不合格时，应抛出
`pipeline.server.episode_validation.EpisodeValidationError`，可附带 `code` 和 JSON 可序列化的 `details`。
配置、部署、文件完整性、I/O、未知程序异常应保留其普通异常类型，不能统一包装成可跳过的数据错误。
外部工具须通过结构化诊断区分数据拒绝和工具故障，不能仅依赖非零退出码或错误文本。
