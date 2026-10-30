# Unitree G1 Client：手柄采集与采集优先上传

## 范围与当前状态

此实现绑定 `g1_brainco`、SONIC 1.1、BrainCo 与 `g1_sonic_raw_v1`。
它复用 GR00T-WholeBodyControl 中现有的 **独立 raw subscriber**，不实现机器人控制、不接管 deploy、PICO、相机服务，不支持 G1 动作回放。
停止采集或退出 Client **不是急停**。

本地适配与自动化测试不等于真机验收。示例 Profile 的现场审批标志仍为 `false`；Server 正式任务会被 `BLOCKED`，不能为了跑通而自动改为已批准。

2026-10-29 本地验证：提供的 127 MB raw 经 Client 封存、真实 Server HTTP 路由（进程内传输）完成持久化 ACK，再由 Provider 生成 v2.1/v3.0；两种输出均为 943 帧，deep validate 通过。此验证只替代硬件启动/状态，不验证真实网络吞吐与机器人性能；原始文件未改写。此前同一样本也通过三路视频完整解码。当前仍未进行 G1 现场部署。

## 部署前提

- Linux，`/proc` 可读，Python 提供 `os.pidfd_open`、`signal.pidfd_send_signal`，内核支持 pidfd。
- 机器人侧已经安装带 raw-only、控制 Unix Socket、原子 `episode.json` 与 SHA-256 的采集器版本。
- 机器人控制、PICO、相机服务由现场独立准备；默认订阅 `127.0.0.1:5555/5556/5557`。
- **不要同时运行原有 all-in-one 数采启动器与 Client 管理的 raw recorder**。检测到已有 `run_data_exporter.py` 时拒绝启动第二个；Client 不会替你结束旧数采。
- Client 进程使用独立权限、磁盘与凭据，数据目录不得包含符号链接；只运行一个 Client 实例。

复制并修改 [`configs/client.g1-sonic.json`](../../configs/client.g1-sonic.json)：

| 配置 | 含义 |
| --- | --- |
| `mode: external` / `capture.adapter: g1_sonic` | 显式绑定外部触发采集，上传类别为通用 `capture_mode: real`，不冒充 ROS2 |
| `capture.repository` | 可信的 GR00T-WholeBodyControl 绝对路径 |
| `capture.python` | 现有数采环境的 Python，不是 Client 的 Python |
| `root` | Client 状态目录；务必保持短路径，内部 Unix Socket 总路径不超过 100 字节 |
| `local_roots.default` | 受管采集根，需要足够空间；ACK 后 G1 raw 仍保留 |
| `server_url` | 实际 Server 地址，替换 `SERVER_ADDRESS` |
| `profiles` / `default_profile` | Client、Server 使用一致且显式注册的 G1 Profile |
| `capture.raw_buffer_mb` | 原生 writer 缓冲上限，默认 64 MiB |

在独立 Client 环境安装项目依赖；设置独立的管理员与设备凭据（不要写进仓库），例如：

```bash
export PIPELINE_TOKEN='替换为至少24字符的管理员凭据'
export PIPELINE_DEVICE_TOKEN='替换为不同的至少24字符设备凭据'
uv run --locked embodied-pipeline client --config configs/client.g1-sonic.json --host 127.0.0.1 --port 8080
```

以上是部署说明，本分支的本地测试不会执行远端部署。Server 还需配置对应 Runtime、设备 `robot_id/project_id` 和同一设备密钥。远程访问建议通过 SSH 隧道或已有受保护入口。

## 操作流程

1. 现场确认机器人控制系统安全、相机与遥操数据发布正常。
2. Client 填写任务/数据集、本地根与远端目标，选择“进入手柄采集作业”。这只启动 raw subscriber。
3. 使用已有手柄逻辑开始、保存、丢弃和 HOLD 暂停/恢复。Client 不重放按钮点击，以原生 `episode.json` 为持久账本，Socket 仅补充实时状态和包计数。
4. 正常保存自动进入待封存队列；丢弃不上传；中断保留本地供排查，不能作为正常数据上传。
5. **整个批次期间**都不做大文件 SHA-256、封存或上传，包括 episode 间隙和 HOLD，不能用瞬时 CPU 低负载推断录制结束。
6. 先用手柄保存当前段，等待原生采集器 idle，再点击“结束本批次采集”。只有受管 recorder 退出、最终 episode 核对成功后才放行后台处理。
7. 资源稳定空闲后逐段校验并上传。重新进入批次会先撤销后台许可并等待现有后台任务让出，不会边采集边传历史 raw。

HTTP 接口（沿用 Client 登录和权限）：

- `POST /api/capture-batches`：与普通 capture 请求相同的任务/路径/幂等键字段。
- `GET /api/capture-batches`：批次历史。
- `POST /api/capture-batches/{id}/end`：幂等结束，不能代替手柄保存。
- `GET /api/health`：当前批次、采集器状态、后台等待原因和资源快照。

状态 `RECOVERY_REQUIRED` 时禁止再起批次、禁止后台处理，不自动重发 start。Client 重启会恢复 PID、Linux boot ID、进程启动时间与原始路径；不可信身份绝不发送信号。只对身份核实后的单个 raw subscriber 发送 SIGTERM，不信号进程组。Unix Socket 不可用但进程仍在时，先现场检查 `recorder.log` 与原生采集器状态，不盲目解除门控。

Client 关闭不会隐式结束录制。应优先按流程结束批次后再关闭；意外退出后需恢复确认。启动结果不明且尚未持久化进程身份时保持保守阻塞，需要人工核对，不提供一键强制清除状态。

## Raw 封存与上传

每段生成独立 Manifest，逻辑文件为 `episode0/packets.raw`、`episode0/episode.json` 和存在时的 `robot_config.json`。大 raw 通过受管源路径映射直接读取，不复制、不压缩、不转 HDF5。小元数据以快照封存；原始字节数、SHA-256 与文件身份变化都会检查。

默认单后台工作租约、单文件分块读取，chunk 上限 1 MiB，校验与传输分别限速 5 MB/s（十进制）。上传暂停不计失败次数，恢复时查询 Server 字节 offset，重启也可续传；未完成文件 SHA-256 的中间状态只保留在当前进程中，重启需重算。后台不把整个 raw 读入内存。网络请求有超时，但现有在途请求不可瞬间撤销，进入下一批次会等待其结束并让出。

默认资源策略（Linux `/proc`）：

| 指标 | 暂停条件 | 恢复条件 |
| --- | --- | --- |
| 可用内存 / 总内存 | < 15% | ≥ 20% |
| swap out | > 0 页/秒 | = 0 |
| 总 CPU | > 85% | ≤ 70% |
| 最忙 CPU 核 | > 98% | ≤ 98% |
| I/O PSI some avg10 | > 20% | ≤ 5% |
| 内存 PSI some avg10 | > 5% | ≤ 1% |
| 网络收发速率 | > 20 MB/s | ≤ 10 MB/s |
| 磁盘读写速率 | > 100 MB/s | ≤ 50 MB/s |

每秒采样，恢复还需连续 20 秒稳定窗口。网络取最忙的非 loopback 接口，磁盘取最忙设备/分区，避免叠加重复计数；这是保守调度指标，不是精确的进程归因，也不是链路容量自动探测。应根据现场共享网卡、控制流量、磁盘基准调整阈值和带宽上限。PSI 在旧内核缺失时显示 `null`，仍有吞吐与内存/换页门控；必需资源采样失败则暂停。磁盘低水位会请求中断 raw recorder，原始段保留为异常，不代替机器人安全处理。

## 可选预览

默认关闭原生 ZMQ 预览，避免额外订阅开销。可配置可信 `capture.preview_url` 复用现有 visualizer 的 MJPEG；或者在 Client 环境额外安装 `pyzmq` 并配置 `capture.preview_zmq: true`。
后者按需订阅相机，限长、限队列，只缓存各相机最新 JPEG，不解码重编码；没有浏览请求 5 秒后退出。预览不决定录制状态，也不保证降低相机发布端负载。部署前需测量实际影响。

## 现场验收清单（尚待完成）

- 连续多次保存/丢弃与快速按钮操作，Client 与原生 episode 数量、结果一致。
- HOLD 跨暂停边界的数据转换：确认恢复后第一帧存在合法的新样本；Server 不允许跨暂停复用旧样本，不能只凭状态机测试宣称完整通过。
- 同批次间隙、HOLD、保存期间均无后台校验/上传。
- 压力场景下记录内存、换页、CPU 单核、磁盘、网络与采集队列，确认不影响控制与采集。
- 网络中断、重启、服务超时、磁盘低水位、recorder 异常退出、PID 复用和旧数采冲突。
- 批次结束后续传、Server 持久化回执、raw 保留，以及 v2.1/v3.0 deep validate、视频完整解码与 Doctor 报告。
- 现场确认动作语义、关节顺序、单位、时间戳、相机身份后，按现有审批流程更新 Profile；不能用测试结果替代人工签核。
