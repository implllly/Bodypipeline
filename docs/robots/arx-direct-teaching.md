# ARX direct-teaching 接入

`ARXDirectTeachingProvider`（`arx_direct_teaching`）接受 grus 采集器的
`arx_direct_teaching_hdf5_v1`。它与历史 `aloha_hdf5` Runtime 分开绑定，
不按目录名猜测格式，不把未知 schema 当成兼容文件。

Server Runtime 使用 `configs/arx-x5-direct-teaching-v1.json`，机器人类型统一为
现有 `ARX_X5`。设备注册项通过 `runtime_by_raw_format` 将直接示教绑定到
`arx_direct_teaching`。
Profile 从 grus 当前文件整理而来，机器人类型归一为 `ARX_X5`，当前摘要为
`48e1d2bbe63f061e17724f7f14fd68236bfd2ce73b6f171d740a1217320e8b94`。

## 正式上传

Client 应使用既有 `/device/uploads` HTTP 分块上传接口：创建、查询偏移、上传、
完成。必须移除 `capture.upload_ssh` 才会进入此分支；仅修改 `server_url` 无效。
`episode.hdf5` 是唯一包文件，Manifest 携带采集 Profile 和 SHA256。

Server 在 `COMMITTED` 前校验文件摘要、schema、自包含 HDF5、全部 JPEG、
逐维字段、实测轨迹 action、时间戳、会话/设备/项目/Profile/任务元数据。
相同会话可断点续传、重复提交；登记成功后出现在工作台。
SSH/rsync 到共享目录的历史文件不会因此自动登记。

## 转换与验收

raw_first 文件按参考相机精确源时间戳建立均匀时间网格，使用有界最近完整快照；
其他同 schema 文件使用记录的单调时钟。保留源索引、帧复用和逐帧跨传感器偏差；
超长采集间隙或超出帧对齐容差会阻止转换，跨传感器源偏差超过提示阈值时生成
`WARN`，不再整段拒绝。原文件不修改。
对齐 HDF5 复用通用 LeTools preset，输出 LeRobot v2.1 / v3.0。

`action` 的含义是同帧实测关节位置作为示教目标，不是独立控制命令。
直接示教 Profile 按现场确认的 `action[t]=observations/qpos[t]` 和头部相机参考
时钟标记为 `semantics_approved=true`、`clock_verified=true`。

直接示教 Profile 覆盖了 `static_intervals` 的静止判定参数：

```json
"static_intervals": {
  "default_threshold": 0.1,
  "unit_thresholds": { "rad": 0.1 }
}
```

阈值的单位是 rad/s。注意 `observation.state` 声明为 `rad` 时会优先使用内置的
单位阈值 `0.02`，只改 `default_threshold` 不会生效；必须通过
`unit_thresholds.rad` 覆盖。默认 `0.02 rad/s` 低于 ARX 关节噪声/漂移底噪，
会让带轻微漂移的保持段被判成运动，这里放宽到 `0.1`。任何被判为静止的帧都会直接
删除，不再要求持续达到某个时长，也不在动作边界保留余量。`min_motion_seconds=0.2`
和 `min_output_seconds=1.0` 保持默认。该配置只影响 Server 端转换，不参与
`capture_contract_hash`；旧版 Profile 摘要仍通过 Runtime 的
`accepted_capture_profile_hashes` 接受，已有采集无需重新上传。

两个 ARX Profile 都复用了历史 ARX X5 的名义电机行程参数
`arx_x5_nominal_motor_travel_v1`，即夹爪 `-3.14` 为张开、`0` 为闭合，默认在
夹持阶段把闭合度增加 10 个百分点。该百分比只表示名义电机行程，不表示物理
开口宽度或夹持力。转换配置属于服务端处理策略，不参与
`capture_contract_hash`，因此给已有数据增加夹爪能力不会导致原始文件摘要失配。

## Server 部署与机器人配置

服务端入口示例为 `http://SERVER_ADDRESS:8081`。设备密钥通过独立环境变量
`PIPELINE_ARX_DEVICE_TOKEN` 绑定；Client 使用对应值作为 `PIPELINE_DEVICE_TOKEN`。
密钥保存在部署账号私有 handover 目录，不写入仓库或日志。

grus 由配置/服务所属账号修改并重启 Client：设置 `server_url`、设备密钥，
移除 `capture.upload_ssh`，保持当前设备身份和采集 Profile。历史 ACKED 的 SSH
任务不会自动重传，需单独明确选择重传或导入，避免重复和错误身份登记。

## DAgger kinesthetic 数据

ARX 纠偏数据使用独立的 `arx_kinesthetic_hdf5_v1`。它的 `action` 是下一帧实测
位置，并携带 `action_valid_mask`；不能送入 directly-teaching Provider。设备注册
项应按原始格式显式路由，缺少映射时服务端拒绝，不静默回退：

```json
{
  "robot_id": "arx-x5-001",
  "project_id": "teleop",
  "runtime_by_raw_format": {
    "arx_direct_teaching_hdf5_v1": "arx_direct_teaching",
    "arx_kinesthetic_hdf5_v1": "arx_kinesthetic"
  }
}
```

对应的 Runtime 配置为：

```json
{
  "arx_kinesthetic": {
    "provider": "arx_kinesthetic",
    "profile_path": "configs/arx-x5-kinesthetic-v1.json",
    "raw_formats": ["arx_kinesthetic_hdf5_v1"]
  }
}
```

DAgger Runtime 使用 `configs/arx-x5-kinesthetic-v1.json` 和
`ARXKinestheticProvider`（`provider=arx_kinesthetic`）。当前 Profile 摘要为
`0e0ed64b4a7c9b26b0d93844ccece8e2b96ca1bf31d85869a9addb2ab5c828f7`。
转换时保留 `action.valid` 数值掩码；超过 250 ms 的采集间隙会重复最近的观测，
但对应 action 标记为无效，不会作为新标签使用。该 Profile 仍保持
`semantics_approved=false`、`clock_verified=false`，上传登记可完成，正式转换
任务在审批前保持阻塞。
