# G1 SONIC Server Provider

`G1SONICProvider` 的注册 ID 是 `g1_sonic`，原始格式为
`g1_sonic_raw_v1`，仅接受 `g1_brainco`、SONIC 1.1、每手 6 电机的
BrainCo 配置。未知手型、缺失配置或 SONIC 版本不匹配时拒绝转换。

## 转换路径

```text
packets.raw + episode.json + recorded robot_config
  -> CRC/SHA256 核验、数值解析、时间对齐
  -> G1SONICSource: Arrow 数值帧 + raw 中的 JPEG 图像序列
  -> LeTools DatasetSource API
  -> LeRobot v2.1 / v3.0 + H.264 视频
```

不创建 HDF5、临时 Parquet 数据集或临时视频数据集。Server 的 `aligned`
目录仅包含可信来源描述文件，引用仍保持只读的原始 raw。转换前后校验摘要，
最终发布前写入语义元信息。原始文件在转换完成前必须保留且可被执行节点读取。

## 输入与批量

Provider 每次 `prepare` 接收一段 episode，可以是原生 episode 目录、
`packets.raw` 文件，或现有上传契约的包装目录：

```text
episode0/
  packets.raw
  episode.json
  robot_config.json    # 如 raw 已记录 robot_config，可省略此副本
```

`episode.json` 必须来自原始采集，包含已保存状态、开始/结束单调时间、
文件名、大小、SHA256、普通记录数及 context 记录数。不能根据文件长度猜测边界。
原生 `episodes/episode_*/` 布局也可读取，其会话配置按 `capture_directory` 查找。
Server 一条上传对应一段 episode，多个上传沿用现有批量队列及逐片段诊断。
多个原生 episode 目录不能作为单次 `prepare` 隐式合并。

Server 上传/原始导入仍遵循现有 Manifest 契约；本 Provider 不增加客户端采集器、
原生目录自动登记或机器人控制功能。

## 字段语义

| 字段 | 维度 | 来源/单位 |
| --- | --- | --- |
| `observation.state` | 41 | `body_q` 29 rad + 双手反馈 12 normalized |
| `action` | 76 | `token_state` 64 latent + 双手目标 12 normalized |
| `action.wbc` | 41 | `last_action` 29 rad + 双手目标 12 normalized |
| `source.monotonic_ns` | 标量 | 当前输出帧的原始采样时刻，int64 |
| `source.*_monotonic_ns` | 标量 | 选中样本的来源单调时间，int64 |
| `source.*_age_ms` | 标量 | 输出采样时刻与来源时刻的差值 |

相机必须在 Profile 显式绑定源名称、输出名称和分辨率。独立相机包和合并包均支持。
图像以原始 JPEG 字节直接提供给 LeTools 视频编码器。

默认输出 native 布局。将 `g1_sonic.model_layout` 设为 `psi0_45_80`，可额外生成
`observation.model_state`、`action.model` 和 `action.model_mask`。
state padding 为 `[35,42,43,44]`，action padding 为 `[70,77,78,79]`；
padding 填零，mask 对应位置为零，其余为一。这只是显式的数据布局，不代表模型训练验收。
语义同时保存在标准 `meta/info.json` feature 元信息和 `meta/modality.json` 中。

## 时间与质量

机器人使用接收单调时间。相机优先使用 `host_capture_monotonic_ns`；仅当 Profile
明确允许 `camera_time_fallback=mapped_wall` 时，才使用相机 wall time 与本机接收时间
之间的映射，并在报告中记录回退次数。这不是硬件同步证明。

从每段有效录制区间的开始时刻按 output_fps 采样，帧数取区间时长乘 FPS 的整数部分；
不会为匹配已有导出结果而隐式裁剪首帧。选择不晚于目标时刻的最近样本，
不插值、不借用未来数据；超过 Profile
声明的 age budget 时拒绝。暂停区间从输出时间轴中移除，禁止跨暂停复用旧样本。
保留 raw context 供片段首帧使用，不把 context 计为新录制包。

原生采集器先处理 PICO 开始指令，再写入触发指令的原始包，因此允许首个
非 context 包为早于 episode 起点的 `pico:manager_state` 控制包。该包仍参与
CRC、SHA256 和记录数核验，但不参与数值或图像对齐；报告中的
`pre_start_control_lead_ms` 记录它提前的毫秒数。后续控制包及所有机器人、
相机包仍必须满足原有 episode 边界要求，原始 raw 和 episode 元数据不改写。

CRC、SHA256、文件大小、记录数及 Profile 绑定错误属于完整性错误，不能按坏片段跳过。
缺流、维度错误、非有限值、图像损坏和对齐缺口属于显式片段缺陷。
全零手部反馈和超出标称范围的手部值保留原样并写入警告，不自动裁剪或伪造反馈。
Provider 不运行 token-to-joint 模型推理，也不把 token 当关节角度比较。
现有通用 Doctor/评分与 Viewer 不因新增 Provider 自动获得 token 专属判定或曲线配对能力。

## 配置与独立转换

使用 `configs/server.g1-sonic.json` 和 `configs/g1-sonic11-brainco-v1.json` 作为示例。
示例中的 `semantics_approved` 和 `clock_verified` 保持 false，现场核实后再由可信部署修改。
运行 `scripts/install_letools.sh` 会在独立工具环境安装解析依赖；无需安装机器人 SDK。

直接离线转换原生单段或包含多个 episode 的原生数据集：

```bash
.runtime/letools-venv/bin/python pipeline/server/providers/g1_sonic_source.py \
  /absolute/path/to/native-dataset /absolute/path/to/new-lerobot-dataset \
  --raw --profile configs/g1-sonic11-brainco-v1.json \
  --task 'Pick up the box' --to v3.0
```

该离线入口沿用显式 schema 校验，但不绕过或修改 Server 的现场审批状态。
它将指定 task 应用于本次输出；不同任务应分批转换。
输出目录必须位于输入数据目录和各原始 episode 目录之外，且不能已存在。
