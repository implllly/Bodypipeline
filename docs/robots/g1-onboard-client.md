# G1 完整数采服务接入 Client

## 适配范围

`g1_sonic_onboard` 复用机器人仓库里的 `run_web_data_visualizer.py`、`SessionSupervisor`、`build_launch_plan` 和录制控制接口。G1 Client 的根页面来自机器人 `127.0.0.1:8081` 实际组装后的最终 HTML，而不是尚未替换占位符的 `web_data_visualizer.html` 模板。捕获快照保存在 `pipeline/web/g1-sonic-8081.html`，原版采集组件保存在 `g1-onboard_collection.js/css`。

原页面负责采集控制、数据源状态、三路相机、实时曲线、信号搜索和图表；BodyPipeline Client 的独立功能区追加在同一页面下方，提供基础服务启动、当前 Client 批次、数据集、上传队列和重试能力。G1 页面不使用 Client iframe，也不经过通用 Client 工作台外壳，并且不再要求网页访问密钥。

与只管理 raw subscriber 的 `g1_sonic` 相比，该适配器允许用户在 Client 前端显式启动本批次的机器人控制、PICO 管理器和 raw 采集器。停止录制、停止整套服务和机器人急停是不同操作。

当前按 Web UI 命令中的 `--raw-only` 模式接入，Raw 输出固定开启：保存数据沿用 `g1_sonic_raw_v1` 封存、分块续传和 Server 的 `G1SONICProvider` 转换。未接入机器人端在线 LeRobot 输出的上传；不能将该模式的输出交给 Raw Packager。

## 配置

参考 [`configs/client.g1-onboard.json`](../../configs/client.g1-onboard.json)。切换已有 Client 时保留实际 root、Server 地址、robot_id、project_id、Profile 路径和凭据；先正常结束旧 Adapter 的所有批次，再切换配置。

机器人依赖与可执行路径放在可信的 `capture` 配置中，不允许网页传入命令、模型路径或 Python 环境：

| 配置 | 用途 |
|---|---|
| `adapter: g1_sonic_onboard` | 显式选择完整服务模式 |
| `repository` | `/workspace/unitree/GR00T-WholeBodyControl` |
| `python` | `.venv_camera/bin/python` 的绝对路径 |
| `service_url` | 原生 SONIC 服务的 loopback 地址，默认 `http://127.0.0.1:8081` |
| `robotics_service_script` | `/opt/apps/roboticsservice/runService.sh` |
| `deploy_checkpoint` | `policy/sonic_v1_1/model` |
| `deploy_obs_config` | `policy/sonic_v1_1/observation_config.yaml` |
| `pose_walk` / `pose_walk_max_speed` | 默认启用 POSE 行走，最大速度 `0.3` |

“启动基础服务”检查 xrobotservice；未运行时执行已配置的脚本。原生 Web 服务未启动时，使用配置的 Python 启动以下入口，模型与行走参数也来自配置：

```bash
.venv_camera/bin/python gear_sonic/scripts/run_web_data_visualizer.py \
  --web-host 127.0.0.1 --web-port 8081 --enable-web-control \
  --deploy-checkpoint policy/sonic_v1_1/model \
  --deploy-obs-config policy/sonic_v1_1/observation_config.yaml \
  --raw-only --camera-depth off --pose-walk --pose-walk-max-speed 0.3
```

已有原生服务可复用，但须开启 Web 控制和 Raw 模式，且处于 idle。若已有外部会话在运行或处于异常状态，Client 显示实际状态并拒绝另起采集器，不隐式停止或接管该会话。首次切换时，应先在原管理入口正常保存、结束旧会话。

## 前端流程

1. 填写 Dataset 名称与任务描述。名称为 1–64 个 ASCII 字母、数字、下划线或连字符，描述最多 400 字符。
2. 可先启动基础服务、设置深度流并应用配置。改变深度流会让共享 RGB 短暂重启，等待相机 ready 后再启动。
3. 点击下方“启动数采基础服务”。该操作准备 xrobotservice 和 8081 Web 服务，但不会自动启动机器人或录制。
4. 使用上方原版采集控制填写输出子目录、Dataset、任务 prompt 和深度设置，再点击“启动服务”。Client 的兼容层将原版 start 操作转换为 Client 批次，确保 episode、封存和上传均可追踪。
5. 根页面仍是 8081 最终 SONIC 文档；其 `/api/status`、`/api/signals`、`/api/history`、相机流和 `/api/collection/*` 被重写到同源 `/capture-visualizer/api/`。原版页面和新增 Client 区在同一 DOM 中直接显示。Client Web 和本地 API 不做密钥验证，部署时应由可信机器人内网限制访问范围。
6. 服务就绪后，可点击“开始录制”“停止并保存”“废弃当前集”，也可继续使用原有手柄流程。正常保存自动登记，丢弃不进入上传队列。
7. 使用原版“强制停止服务”结束后，必须同时确认原生会话退出、最终 episode 核对完成，才解除批次门控。资源稳定空闲后自动封存和上传。

“强制停止服务”需要前端二次确认，调用原生 supervisor 的停止逻辑，未保存的录制段可能被丢弃。Client 不会以“已发出停止请求”代替退出确认。

原始数据存储在 `repository/outputs/<batch-id-prefix>/<dataset-name>`；每个批次独立，避免同名任务覆盖。Client 管理目录保留批次、episode、Manifest 和上传状态，小元数据快照和 raw 源路径映射沿用现有封存实现。

## 统一接口

| 接口 | 用途 |
|---|---|
| `GET /api/capture-service` | 服务、相机配置、数据源状态与当前归属 |
| `POST /api/capture-service/prepare` | 显式准备基础服务 |
| `PATCH /api/capture-service/options` | 应用适配器声明的配置项 |
| `POST /api/capture-batches` | 创建批次并启动完整服务，支持 `capture_options` |
| `POST /api/capture-batches/{id}/commands` | 发送带幂等 request_id 的录制、保存、丢弃指令 |
| `POST /api/capture-batches/{id}/end` | 正常停止；`{"force": true}` 请求强制停止 |
| `GET /api/capture-telemetry/signals` | 读取可用数值信号目录 |
| `GET /api/capture-telemetry/history?signal=...` | 读取最多 16 个只读信号的历史窗口 |
| `GET /` | G1 返回原版 SONIC 主文档并在页面末尾追加 BodyPipeline Client 功能区 |
| `GET /capture-visualizer/` | 保留原生页面单独访问入口，用于诊断和兼容 |
| `GET /capture-visualizer/api/*` | 为原页面代理状态、曲线和相机流 |
| `GET /capture-visualizer/api/collection/status` | 将原版状态读取映射到 Client 所属批次 |
| `POST /capture-visualizer/api/collection/{challenge,start,stop,camera-depth}` | 将原版操作映射到 Client 服务和批次生命周期 |
| `POST /capture-visualizer/api/collection/episodes/{start,stop,discard}` | 将原版录制操作映射到 Client 幂等命令 |

这些 Client 本地接口不要求网页访问密钥；`PIPELINE_DEVICE_TOKEN` 仍只用于 Client 向 Server 发起设备上传，Server 端设备鉴权没有放宽。UI 按 capability 声明渲染，不包含机器人型号、固定维度、厂商路径或 SDK 分支。机器人专有协议、启动参数和错误解析均在 Adapter 中。

## 手柄输入与故障可见性

- A+X、B+Y、A+B+X+Y 每次组合只产生一次边沿；组合触发后必须完全释放 A/B/X/Y，部分松开不会误触发其他组合。
- XRT 或 Isaac 控制器读取失败时不更新按键状态，也不会被当成“全部松开”而重复触发。
- A+B+X+Y 在运行模式下仍是明确停止策略：发送停止命令后退出 PICO manager。Client 不自动重启策略，页面会把会话标为异常并保留现场处置入口。
- supervisor 为每个会话在 `outputs/.sonic-web/logs/` 保存 `deploy`、`pico_manager`、`data_exporter` 日志。
- 运行中任一 pane 退出，`GET /api/capture-service` 返回 `phase: error`、退出 pane 的末尾输出和日志路径，不再显示为正常就绪。

## 恢复与验收

- 开始前持久化预期输出路径；原生启动请求超时后保持待恢复状态，不自动重发启动。
- Client 重启恢复已有批次；控制请求须匹配已登记的原生 session 和输出路径。检测到其他会话时不控制它，也不放行后台上传。
- STOPPING 状态及停止请求会持久化；只有原生服务确认 idle 且没有会话，才继续最终核对。
- Client 退出只关闭自身预览，不隐式停止控制程序。设备重启后原生 supervisor 的遗留 owner 记录可能需要在原管理入口人工核对；当前适配器不会自动删除不可信归属记录。
- 健康检查不会启动任何服务。未归属到 Client 的原生采集仍会阻塞后台传输，避免边采集边传历史数据。
- `tests/test_g1_onboard.py` 覆盖原生 HTTP 协议、原版 HTML 单文档组合、异步停止、录制保存后的 Raw 封存队列、启动响应丢失后的恢复、幂等指令、错误会话与外部会话隔离、认证和选项校验。
- 本地浏览器模拟验证了桌面与手机布局、三路预览和完整按钮流程；这些检查不代表已经部署到 G1 或完成真机采集、实时性及动作语义验收。
