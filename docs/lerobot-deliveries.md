# 完整 LeRobot v3 数据集增量登记

适用于外部生产者已经完成转换、对齐与视频编码的成品数据。扫描器是通用的，
不调用 CaptureAdapter，不给未知机器人绑定其他型号的转换 Provider。

## 配置

将以下字段合入现有 Server 配置（示例不是生产配置）：

```json
{
  "import_roots": ["/srv/bodypipeline-imports"],
  "delivery_sources": [
    {"root": "/srv/bodypipeline-imports/sim", "robot_type": "sim"}
  ],
  "delivery_scan_seconds": 60,
  "delivery_retry_seconds": 300,
  "delivery_scan_batch_limit": 10
}
```

扫描根必须为绝对路径，末级目录名等于绑定的 `robot_type`，并处于导入白名单中。
各源不可重叠；缺少目录时仅告警，不在共享目录自动新建。默认为不启用扫描。
需要已有 LeTools Python 工具链中的 PyArrow、PyAV，不增加 API 虚拟环境依赖。
`command_timeout` 限制每批结构及全视频解码验证的时间。

## 生产者目录与完成协议

```text
<root>/<dataset-name>/<batch-name>/
  delivery.json
  READY
  lerobot/
    meta/info.json
    meta/platform.json
    meta/stats.json
    meta/tasks.parquet
    meta/episodes/chunk-000/file-000.parquet
    data/chunk-000/file-000.parquet
    videos/observation.images.<camera>/chunk-000/file-000.mp4
```

发布后目录不可变，不支持向原目录继续追加。新数据发新批次；忽略隐藏临时目录，
不递归扫描 Parquet/视频内容目录。没有 READY 的目录不处理。

`delivery.json` 使用 `bodypipeline-lerobot-delivery/1`：

- 全局唯一 `delivery_id`，逻辑分组 `dataset_id`，版本标识 `feature_schema_id`。
- `robot_type` 与源绑定一致；`format: "lerobot"`、`codebase_version: "v3.0"`、
  `dataset_path: "lerobot"`。
- 正数 `total_episodes` / `total_frames` / `fps`，UTC `created_at`。
- 按 episode 顺序的全局唯一 `episode_ids`。
- `supersedes_delivery_id: null`；替代批次会报告需人工审核，不自动替代或删除旧批次。
- `files` 列出 lerobot 内全部文件的 `path`（含 lerobot/ 前缀）、`size_bytes`、
  小写 64 位 `sha256`；不列出 READY/delivery.json。

READY 是 JSON：`{"delivery_id": "...", "manifest_sha256": "..."}`，
哈希对应 delivery.json 原始字节。关闭全部 writer 并完成清单后，最后原子发布 READY。

`platform.json` 必须至少包含一致的 `robot_type` 和 `feature_schema_id`；
还应由交付双方审核状态/动作语义、单位、坐标系、时钟、相机和传感器定义。
自然语言语义与实际物理测量是否真实不能由结构校验自动证明。

## 校验和登记

- 校验完成标记、清单哈希、全量文件 SHA-256、文件大小和无符号链接约束。
- 独立工具链流式检查基础字段 dtype/维度、连续索引、任务映射、帧率/时间戳、
  episode 数据区间；至少一路 RGB，H.264/yuv420p、无音频。
- 完整解码视频检查帧数、PTS、分辨率、帧率和共享视频区间。
- 检查基础 stats 存在，不重新计算统计量；检查平台语义版本，不证明人工语义声明。
- 新批次的 episode_ids 不得与已认领批次重叠；同一机器人和 feature_schema_id
  的 features 定义不得变化。升级语义应使用新版本标识。
- 验证前后比对文件 inode、大小、mtime、ctime 及完成标记，拒绝边写边登记。

成功生成普通 `lerobot_import` 结果，可在结果页按 robot_type 筛选、预览和质检。
`logical_dataset_id` 保留归组关系，但不自动合并批次；job 记录中保留交付 ID、
schema ID、清单哈希和 `immutable_delivery: true`。

增量台账存放在 Server 私有状态 `state/delivery/`。首次认领先于 job 登记，
job ID 由 delivery_id 确定，可恢复写入之间的中断。重启及重复扫描不重复登记。
已登记批次只检查元数据/文件属性，不反复哈希或解码全部视频；该机制不是对抗恶意
修改的存储完整性保证，源挂载仍应由提供方保证不可变。

## 管理入口

工作台“已接收采集/导入”区域新增“外部数据增量登记”，展示源目录、批次状态和错误。

- `GET /api/deliveries`：受现有管理员鉴权保护，返回源状态、计数和最近 500 条记录。
- `POST /api/deliveries/scan`：仅请求后台立即扫描并重试失败项，返回 202，不阻塞请求。
- 自动失败重试有独立间隔；一个坏批次不影响其他批次。

扫描是独立后台循环，不占用转换主循环；仅最终登记时短暂取得现有 admission locks。

## 只读及边界

配置源内的目录禁止手动导入绕过 READY 校验，禁止永久删除和原地裁剪。
已登记只读 job 即使移除扫描源也保留只读标志；Viewer 回收站的隐藏/恢复仅写私有
状态，永久清理会被拒绝。可以生成新副本后在副本上操作。
推荐同时使用只读挂载，代码保护不能替代文件系统权限。

批次/根目录消失会显示异常，不删除登记；同 delivery_id 换内容或换路径会报告冲突。
修正失败数据应发布新 delivery_id，不覆盖原批；替代关系需人工处理。

扫描器不新增机器人型号页面或型号分支，不触发付费标注，不自动部署或改写生产配置。
