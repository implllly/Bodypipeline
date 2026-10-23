# Tianji / Marvin Pro：Apex 采集接入

适配基线为 `server` 分支 `9a0f340`，ROS 2 Jazzy、Apex Pro / new_m6_696、tianji-provider **0.7.0**。本接入负责采集、上传、转换和数据预览，不是 DM05 或 ForceVLA 的模型推理入口。

## 边界

Client 页面、API、上传协议和任务流程复用现有实现。可信配置选择 `capture_adapter=apex`、`capture_packager=tianji_mcap`；未知或不匹配组合直接报错。Server 静态注册 `tianji` Provider，由认证设备的 Runtime 绑定选中，上传者不能指定 Python 模块或可执行程序。

本次不执行真机录制、回放、模式切换、使能、Home、关节或夹爪控制。新 Client 的 `allow_recording=false`，`replay.enabled=false`。ApexAdapter 仅允许本机录制 API：GET status/projects、POST start/stop，没有机器人控制入口。预览进程只有相机订阅，没有 publisher 或 service client。

## 信号契约

| 特征 | 来源与语义 |
| --- | --- |
| observation.state `[16]` | `/tj/joint_states` 依 name 重排，结合左右 gripper_feedback；左7关节+左夹爪+右7关节+右夹爪 |
| action `[16]` | `/tj/control/joint_cmd_A/B` 与 `/control/gripperValueL/R` 录到的最终目标；不以反馈代替 |
| 关节单位 | rad；A=left、B=right |
| 夹爪单位 | 按录制 `/info/web_gripper` 的标定归一化；不是米，不裁剪 |
| observation.eef `[14]` | 左右 base_link 下 xyz(m)+xyzw 四元数 |
| observation.wrench_windows `[4,12]` | 200 Hz 因果原始力/力矩样本，左/右 flange，每行末点对齐50 Hz网格；保留完整二维形状 |
| 四相机 | head_left/head_right/left_wrist/right_wrist；四宫格左上/右上/左下/右下，单路尺寸取实际四宫格的一半；旧1920×1080录制对应960×540，无额外裁剪/缩放 |

复用原生 Provider 的严格对齐：软件时间域逐录制校验、50 Hz、反馈 lowpass、首尾静止裁剪，关节命令最大年龄100ms、夹爪目标150ms、观测/视频100ms；不放宽阈值。软件对齐不代表四相机曝光硬同步。具体时钟来源、夹爪标定、命令覆盖率和裁剪结果保留在逐录制报告中。

## 数据格式

`tianji_apex_mcap_v1` 为独立版本化目录格式：

```text
raw/episode0/
  data/*.mcap + metadata.yaml
  video/*.mcap + metadata.yaml
  bodypipeline-source.json
manifest.json（在 raw 的父目录）
```

这里选择保留 MCAP，而不是推荐的原始单文件HDF5：MCAP本来就是少量压缩文件，内嵌ROS schema、命令、标定及辅助事件，原样上传能完整保留这些信息，不展开逐帧小文件。Manifest为每个原始文件记录大小与SHA256。拒绝符号链接、缺失data/video、空文件和缺元数据。

Apex保留自己的原件；Client只有在 `phase=saved` 且 data/video writer 均 `closed` 后，才复制到受管目录。Client丢弃或清理不删除Apex原件，因此现场需要分别管理两处留存空间。复制中断的 `.copying` 不自动覆盖，需要人工检查后恢复，避免把半包当成完成包。

Server调用独立native worker，生成 `bodypipeline.tianji.aligned_hdf5.v1`。`observation.action`显式映射为`action`，`observation.image.*`映射为`observation.images.*`；值不变。图像四宫格只解码一次，以无损PNG存入HDF5。LeTools使用已有HDF5 mapping转成v2.1/v3.0，再进行deep validation和11项非运动学Doctor。H.264最终视频编码有损，不应称最终数据集像素无损。

对齐HDF5保留逐行绝对时间、视频包索引、wrench采样时间及原生特征schema/报告。LeRobot额外特征元数据也保留wrench的200Hz、采样偏移、坐标名与单位。辅助事件完整保留在原始MCAP；当前没有额外生成离散事件训练列。

## 采集生命周期

开始前检查独立Apex project及storage_root一致。将request_id和本地STARTING状态先写盘，再请求start。重复start不重发；响应丢失后只用GET状态核对同一request_id/session_id。重启后可以用stop核对并结束自己持有的录制；不允许停止他人的session。

stop只操作自己的session，等待Apex写入排空再封存。关闭Client不会调用stop或任何控制接口；失败或结果未知的会话保持RECOVERY_REQUIRED。需要人工恢复的状态不会自动产生可上传训练样本。

## 配置与部署

模板：`configs/client.tianji-marvin.json`、`configs/server.tianji-isolated.json`、`configs/tianji-marvin-pro-v1.json`。模板中的主机、目录与端口需要按部署环境填写。密钥使用环境变量，不写入Profile、Manifest或报告。

Python主环境使用本项目`uv.lock`。原生worker使用部署方配置的`PIPELINE_TIANJI_PYTHON`，要求该环境已有tianji-provider0.7.0及其LeTools/native依赖。该路径不接受网页/Manifest输入。不要向现有Apex/ForceVLA环境安装包。中心Server也需要这套原生依赖；仅拷贝Profile或网页密钥不能完成接入。

启动命令均在候选release根执行，并预先加载独立认证环境文件：

```bash
.venv/bin/python -m pipeline.cli client --config /path/to/client.json --host 127.0.0.1 --port 8100
.venv/bin/python -m pipeline.cli server --config /path/to/server.json --host 127.0.0.1 --port 8101
```

预览需在ROS Jazzy系统Python ABI兼容环境下加载ROS环境，并运行 `scripts/tianji_preview.py --root <preview_root>`。它持续解码所有H.264包，仅覆盖已解码画面；缩略图处理在线程中完成，保留真实源时间，过期图不显示。

实际部署必须使用不可变release、独立端口/状态根/数据根；共享Server的增量发布须经过全机队回归和维护窗口确认。不要直接覆盖正在运行的统一Server源码、配置或虚拟环境。

## 离线验收

```bash
uv run --locked pytest -q tests/test_tianji.py
PIPELINE_TIANJI_PYTHON=/path/to/native/bin/python \
  uv run --locked python -m scripts.tianji_verify /absolute/existing/bag /absolute/new/output \
  --letools /path/to/native/bin/letools
```

验证脚本不导入ROS、不调用Apex；输出必须是原始目录之外的新目录。原始SHA256前后比对、对齐、QC、双版本转换、deep validation和Doctor报告均独立保存。Doctor WARN照实保留，不通过隐藏检查生成PASS。

最低覆盖包括：丢失start响应、重复start/stop、owner改变、writer未关闭、禁用录制、关闭只读、缺视频、符号链接、16维与四相机QC、辅助字段mapping、分块上传ACK与跨Runtime拒绝。真机采集与回放必须等现场授权后单独验收，离线测试不证明这些已通过。
