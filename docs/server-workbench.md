# Server 工作台

Server 页面用于管理原始数据、提交批量转换、删除或合并数据集、运行质检和打开只读预览。页面采用侧边栏切换四个工作区：

1. **原始数据**
2. **数据转化**
3. **预览与质检**
4. **数据集管理**（导航列表最末尾，包含删除与合并）

所有结果都保存在 Server 文件系统，不使用数据库。页面中显示的路径都是运行 Server 的主机路径，不是操作者本机路径。

原始采集转换表单会按 Runtime 能力显示可选项。目前 ARX 支持“夹持阶段闭合度增加”修正，
默认关闭，启用后填写电机名义行程的百分点增量；规则和部署兼容说明见
[夹爪闭合度修正](robots/gripper-closure.md)。

## 1. 原始数据

### 1.1 机器人类型优先

顶部机器人类型选择器同时筛选：

- 数据集；
- 已上传 episodes；
- 转换任务；
- 预览与质检结果；
- 合并来源、合并任务和首尾裁剪来源；
- 页面统计数量。

操作顺序应为：

```text
选择机器人类型 → 选择数据集 → 选择已完成上传的 episodes
```

切换机器人类型会清空上一类型的数据选择，避免把不同 Profile 或原始格式误提交到同一任务。后台轮询刷新会保留当前选择。

管理台每轮只刷新当前标签页需要的列表：合并候选、版本转换候选、首尾裁剪候选和标注不会在无关页面重复请求；切换标签页时立即拉取该页所需数据，隐藏页面（浏览器标签页切到后台）暂停轮询。

无法从 Manifest 或 Profile 恢复机器人类型的历史记录会归入“未标注类型”，不会冒用当前服务器默认型号。

### 1.2 数据集与 episode

每次 Client 采集都会追加到对应 collection。Server 展示：

- 数据集名称；
- 机器人类型；
- episode 数量；
- 已完成上传 / 总 episode 数；
- 总大小；
- 存储目标和子目录。

只有上传状态为 `COMMITTED` 的 episode 可以勾选转换。未完成上传的记录仍显示在数据集中，便于排查断点续传或失败。

### 1.3 导入历史数据

点击“原始数据 → 导入历史数据”，填写 Server 上的绝对路径。

支持结构：

| 路径内容 | 行为 |
| --- | --- |
| `manifest.json` + `episode.hdf5` | 登记为当前 HDF5 原始采集 |
| `manifest.json` + `data/episode0` | 登记为旧原始采集 |
| `manifest.json` + `raw/episode0` | 登记为更早原始采集，兼容旧 Client |
| 目录内 `*.hdf5`（无 Manifest） | 登记为上游 ALOHA 风格 HDF5 episode 集合，每个文件一个 episode |
| `meta/info.json` 且版本为 v2.1 / v3.0 | 登记为已有 LeRobot 数据集 |

导入要求：

- 路径在 `import_roots` 或 `data_root` 白名单内；
- 路径是普通目录，不是符号链接；
- 机器人类型可从 Manifest / LeRobot 元数据读取，或在页面显式指定；
- HDF5 episode 目录不声明机器人类型，导入时必须显式指定（或由 API 传 `runtime_id`）；
- 显式类型与文件声明冲突时拒绝；
- 同一路径重复导入是幂等的；
- 源数据不被复制、移动或改写。

原始采集导入后可以进入批量转换；LeRobot 导入后可以直接预览和运行 Doctor。

#### 上游 HDF5 episode 集合

这类目录由上游日志器直接写出，没有 `manifest.json`，每个 episode 一个 HDF5 文件：

```text
<dataset>/
├── episode_0.hdf5      # observations/qpos、action、observations/images/<camera>
├── episode_1.hdf5
└── ...
```

导入按 Runtime 绑定的 Profile 读取字段；字段名、关节维度和相机集合都来自 Profile，
不会从文件内容猜测。Server 为每个文件生成一条上传记录，Manifest 只保存在 Server 状态里，
**不会在数据目录写入任何文件**。导入后与普通采集一样进入批量转换。

已适配的布局见 [`configs/arx-x5-hdf5-v1.json`](../configs/arx-x5-hdf5-v1.json)：ARX X5，
14 维 `observations/qpos` / `action`，头部与左右腕三路 JPEG 相机。上游文件不携带 fps，
`output_fps` 取上游录制约定值；改变它等于改变动作的时间尺度。

## 2. 数据转化

### 2.1 创建转换任务

在原始数据中勾选同一数据集的 episodes 后，进入“数据转化”：

1. 确认数据集和 episode 列表。
2. 填写整个批次统一的 instruction。
3. 选择目标格式：
   - LeRobot `v3.0`（默认）；
   - LeRobot `v2.1`。
4. 选择不合格采集处理策略：默认“全部通过才转换”；也可选择“跳过不合格采集，转换合格部分”。
5. 选择是否在转换完成后自动运行 Doctor。
6. 按需勾选“转换时自动删除首尾静止帧”或“转换时自动删除全程静止帧”（所有机器人通用，二者互斥，演示模式不可用）。
7. 提交任务。

要求：

- 至少选择一个 episode；
- 所有 episodes 必须属于同一 collection、机器人类型、Profile、采集模式和存储目标；
- instruction 长度为 1–2000 字符；
- 批量上限为 1000 个 episodes。

相同来源、Manifest 摘要、instruction、Profile、目标格式和工具版本会复用未失败任务。同一输入不会因为重复点击生成多个处理任务。

不合格采集策略由通用 Server 批处理流程执行，所有 RobotProvider 共用。API 参数
`invalid_episode_policy` 接受 `fail`（默认）或 `skip`，单条和批量转换都支持；策略不同会产生不同任务。
首尾裁剪通过独立的布尔参数 `trim_idle_frames`（默认 `false`）开关，不进入机器人特有的
`conversion_options`；只有勾选时任务去重键才包含该选项。静止帧裁剪使用
`trim_static_intervals`（默认 `false`），二者互斥，同样只在勾选时进入去重键。
只有 Provider 明确抛出的 `EpisodeValidationError` 可以跳过，包括已识别的数据字段缺失、
时序缺口、数值或图像内容不合格。文件摘要变化、配置不匹配、工具缺失、超时、I/O 或程序异常
仍使整个任务失败。转换后的深度验证和 Doctor 流程继续执行，不在此策略下逐条跳过。

跳过策略至少需要一条合格采集；全部不合格时任务失败，不生成空数据集。
通过项按原输入顺序重新连续编号，`lineage.json` 中的 `sources` 同时保存输出 `episode_index`、
原输入 `source_episode_index`、`upload_id` 和 Manifest 摘要。原始输入快照保持完整，原始数据不删除。
工作目录的 `episode-selection.json`、任务详情和独立报告展示输入、通过、跳过与失败数量及原因。
因此“任务成功”可以表示合格子集转换成功，输出 episode 数可能少于输入数。

### 2.2 处理阶段

```text
排队
→ Profile / 所有权 / 格式校验
→ 逐 episode 从 HDF5 时间对齐
→ letools convert --source-format hdf5
→ 转换结构检查
→ deep validate
→ 可选 Doctor
→ 可预览结果
```

批量任务会继续核验后续 episodes，并把每个 episode 的结果写入 `episode_diagnostics`。一个 episode 失败不会掩盖其他 episodes 的具体错误。“全部通过”策略会终止该批次；“跳过不合格”策略会转换通过项，全部不合格或存在系统错误时仍失败。

#### 转换前图像预处理的性能与检查范围

“核验与对齐”发生在 `letools convert` 之前，包含原始文件 SHA256 核验、Provider 数据检查、
时间对齐和中间文件写入，不是后面的 Doctor。Episode 仍按输入顺序逐个处理。

HDF5 图像预处理由底层 Provider 复用有界批处理工具：ALOHA HDF5、ARX 直接示教和
ARX Kinesthetic 的图像复制每批最多处理 32 个输出帧，避免逐帧反复查找 HDF5 数据集、
执行单帧读写。重采样中的重复源帧在批内只读取一次，输出帧顺序、重复次数和编码字节保持不变。

两种 ARX 时间戳格式仍检查每一张原始图像，不改为抽样；解码最多使用 3 个线程，受进程 CPU
亲和性限制。HDF5 读写只在调用线程执行，解码线程只处理已读取的图像数据，不共享 HDF5 句柄。
普通 ALOHA HDF5 的首帧检查策略保持不变。SHA256、对齐阈值、动作语义、动作修正、失败策略和
转换后的 deep validate / Doctor 均不因该优化而放宽。此优化不会自动更改已部署服务或运行中任务。

常见状态：

| 状态 | 含义 |
| --- | --- |
| `QUEUED` | 等待执行 |
| `RUNNING` | 当前处理中 |
| `BLOCKED` | Profile、语义、时钟或机器人类型不匹配 |
| `FAILED` | 对齐、转换或工具执行失败 |
| `DEMO_READY` | 演示模式完成，不包含真实 LeRobot 数据 |
| `CONVERTED` | 转换完成且结构验证通过，未运行 Doctor |
| `SYNTHETIC_READY` | 模拟数据通过真实工具链 |
| `COMPLETED` | Doctor 总体 PASS |
| `QUARANTINED` | Doctor 有 WARN / FAIL，数据保留供排查 |
| `INTERRUPTED` | Server 重启中断，需要显式重试 |

### 2.3 已有 LeRobot 版本转换

同一“数据转化”页面还提供“已有 LeRobot 版本转换”：

1. 选择机器人类型。
2. 选择一个已转换、已导入或已合并的 LeRobot `v2.1` / `v3.0` 数据集。
3. 填写新数据集名称，目标版本自动设为另一版本。
4. 选择是否在转换后运行 Doctor。

版本转换不经过机器人 Provider、HDF5 对齐或 instruction 输入。Server 从已登记任务中选择来源，
记录来源文件快照并锁定目录；后台执行来源结构验证、`letools convert --source-format lerobot`、
结果 deep validate 和可选 Doctor。来源存在回收站 Episode、正在永久清理、元数据损坏或来源发生变化时，
任务会拒绝执行。

来源目录保持只读，新结果写入独立任务目录并自动应用共享权限。仅支持 v2.1 与 v3.0 互相转换；
同版本、跨到其他 LeRobot schema 和跨版本合并都不允许。

### 2.4 时间对齐

当前 AgileX 适配器要求：

- 三路相机：`left`、`front`、`right`；
- state：`puppetLeft`、`puppetRight`；
- action：`masterLeft`、`masterRight`；
- 每路关节 7 维；
- 30 Hz 输出；
- 相机最大偏差 20 ms；
- 关节因果保持最大 50 ms；
- 最短 30 帧。

出现如下错误时：

```text
时间对齐越界：arm/jointState/puppetLeft frame=5 error=70.115ms limit=50.000ms
```

说明指定 episode 的第 5 个输出帧之前没有足够新的关节样本。应检查 recorder 启动阶段、topic 连续性、时间戳单位和写入延迟，不应直接放宽阈值。

### 2.5 转换结果路径

转换结果固定保存在：

```text
<data_root>/<robot_type>/work/<job_id>/dataset
```

Server 当前 AgileX 数据示例：

```text
/srv/bodypipeline-data/agilex_aloha/work/<job_id>/dataset
```

任务工作目录同时包含：

```text
work/<job_id>/
├── aligned/episodeN.hdf5     # 对齐后的 Letools 输入
├── letools-hdf5-preset.json  # 字段映射
├── version-conversion-input.json # 已有数据集版本转换来源快照
├── dataset/                  # 可预览 LeRobot 数据集
├── checks/<check_id>/        # 独立质检报告与日志
├── alignment-report.json
├── robot-report.json
├── lineage.json
└── convert / validate / doctor 日志
```

页面在“预览与质检 → 转换结果”显示可复制的完整数据集路径。只有达到可预览状态且 `dataset/meta/info.json` 存在时，结果才会标记为可用。

没有额外的数据集晋级目录。Doctor 通过、WARN 或 FAIL 都不会移动结果目录。

### 2.6 删除首尾静止帧（所有机器人通用）

“数据转化”页在所有机器人类型下都提供“删除首尾静止帧”，用于删除每个 Episode 开头和结尾机器人尚未运动或已经停止的画面。中间的停顿不会被删除，避免把演示中的关键等待动作裁掉。来源数据集保持不变，输出是新的独立 LeRobot 数据集。

原始数据转换表单还提供“转换时自动删除首尾静止帧”复选项。勾选后无需单独预览：转换任务在 `letools convert` 完成、Provider 写入元数据之后，直接在同一个工作目录内先检测再裁剪，然后才对最终数据集执行 deep validate 和 Doctor。裁剪结果先写入 `work/trimmed`，只有 Episode / 帧数与检测计划一致时才替换 `work/dataset`，因此失败不会发布写了一半的数据集。若检测确认所有 Episode 都没有可删除帧，则跳过重写和视频重编码，只记录溯源文件并继续 deep validate。独立裁剪与转换内裁剪的视频按相机和来源视频分片并行编码，并行度受当前 CPU 预算约束。转换内的裁剪始终保留整段静止的 Episode，不会静默删除整个 Episode；演示模式不生成 LeRobot 数据，该选项不可用。裁剪策略、摘要和逐 Episode 范围记录在任务字段 `trim_policy` / `trim_summary` / `trim_report`，以及 `dataset/meta/bodypipeline-stationary-trim.json` 和工作目录的 `trim-lineage.json`。

流程固定为“先预览、后执行”：

1. 选择机器人类型和一个已转换、已导入或已合并的 LeRobot `v2.1` / `v3.0` 数据集。
2. 调整最少连续静止时长、最短持续运动时长、开头 / 结尾保留余量和是否删除整段静止的 Episode。
3. 点击“预览首尾静止帧检测”，查看每个 Episode 的保留范围、预计删除帧和检测状态。
4. 确认后创建裁剪任务。预览失效、来源在预览后发生变化或比例对不上时不会提交。

检测按帧计算所选数值特征的变化速度：任一维度超过自身阈值即视为运动，阈值单位（`rad`、`m`、`normalized` 等）和是否按帧解释来自机器人 Provider / Profile，页面和接口不按型号分支。只有持续运动被确认后才会裁剪边界，且仅当边界空闲时长达到最少连续静止时长时才执行；整段静止的 Episode 默认保留并标记，可显式选择删除。

裁剪会同步处理全部相机视频与数值帧，重新编号 Episode、`frame_index`、全局 `index`、时间戳、tasks、split 和统计信息，并执行深度结构验证。输出保存到：

```text
<data_root>/<robot_type>/work/<new_job_id>/dataset
```

任务复用现有后台串行队列、超时与 Slurm 配置；可在创建时选择裁剪后运行 Doctor，Doctor 报告和裁剪摘要一并记录。来源快照、检测策略、每个 Episode 的保留范围和结果映射保存在 `dataset/meta/bodypipeline-stationary-trim.json` 与工作目录的 `lineage.json`。

接口：`GET /api/trim-candidates` 获取候选数据集，`GET /api/jobs/{id}/trim-policy` 获取 Provider 解析后的检测特征、FPS 与阈值；`POST /api/jobs/{id}/trim-previews` 提交预览，`GET /api/trim-previews/{id}` 读取结果，`POST /api/jobs/{id}/trims` 接收 `preview_id`、`name` 和 `run_doctor` 并返回 202。相同来源、预览计划、名称和 Doctor 选项的排队或运行中请求会复用同一任务。

### 2.7 删除静止帧（所有机器人通用）

原始数据转换表单另提供“转换时自动删除静止帧”复选项，用于删除录制中任何位置的静止帧，包括开头、中间和结尾。它解决的是“动作 A → 静止 → 动作 B”这类数据：这些静止帧不属于首尾边界，首尾裁剪按设计不会删除。识别结果是“静止帧”，不代表一定能确认它由推理等待造成；任务摘要和溯源也使用同样的措辞。

该选项与“删除首尾静止帧”互斥：前端勾选一个会取消另一个，后端同时收到两个 `true` 时返回 400。演示模式不生成 LeRobot 数据集，因此不可用。转换任务在 Provider 写入元数据后执行 `detect-static`，确认计划后把保留区间写入 `work/trimmed`，校验输出 Episode / 帧数与计划一致再原子替换 `work/dataset`。

检测规则：

- 使用 Provider / Profile 指定的运动特征和单位阈值；Profile 未声明 `static_intervals` 时复用 `trim` 块的运动信号。任何被判为静止的帧立即删除，不要求持续达到最短时长。Profile 可以按机器人覆盖阈值，例如 ARX direct-teaching Profile 声明 `unit_thresholds.rad=0.1`（rad/s），用于识别带轻微漂移的保持帧；注意 `rad` 单位有内置阈值 `0.02`，只改 `default_threshold` 不生效。
- 任一维度超过阈值即视为运动；短于最小时长且累计位移落在抖动范围内的运动突发会被忽略，避免零星抖动把长静止段切断，但短而明显的真实动作仍会切开静止段。
- 删除时不在动作边界保留任何余量。
- 删除后若保留片段短于最短输出时长，则把该片段并入相邻删除区间：录制首尾的短运动片段并入边界删除，两个静止区间之间的短动作并入并合并两段删除，从而保证开头、中间和结尾的静止画面都会被删除；若合并后只剩一个片段，则保留该片段，避免整段消失。
- 整段静止的 Episode 保留并标记，绝不因该选项消失。

删除静止帧后，每个来源 Episode 仍输出一个 Episode：保留片段按原顺序前后直接相连，静止帧两侧的帧成为相邻帧。数值帧、全部相机视频、`frame_index`、全局 `index`、时间戳、tasks、split 和统计都会重建；动作列按区间切片后拼接，因此切点处会产生一次来源动作到来源状态的直接衔接，这是“删除并相连”语义的预期结果。来源数据集保持不变。任务字段为 `trim_static_intervals`、`interval_summary`、`interval_policy` 和 `interval_report`，溯源写入 `dataset/meta/bodypipeline-static-intervals.json` 与工作目录的 `trim-intervals-lineage.json`。

### 2.8 在 Visualizer 中手动删除时间段（所有机器人通用）

打开一个 `bp/` 预览后，Episode 页的时间轴下方会出现“手动裁剪时间段”面板。用户直接在当前 Episode 的时间轴上拖选要删除的片段，面板会把秒数吸附到真实帧号，并列出每段删除区间；可逐段调整起点 / 终点、删除单段或清空后重新选择。删除后只保留一个 Episode：未删除的片段按原顺序前后直接相连，与 2.7 的拼接语义一致。

该功能只作用于当前 Episode，数据集中的其他 Episode 原样保留。服务端不接受浏览器传来的秒数或数据集路径，只接收 `episode_index` 和半开帧区间 `[start, stop)`；来源固定为当前预览已注册的 `bp/<job_id>`，Worker 再按真实 Episode 长度校验区间，越界、空区间、重叠或会删空整个 Episode 的请求都会被拒绝。

提交流程：

1. 浏览器向同源网关 `POST /api/manual-trims/{dataset_id}` 提交帧区间，网关校验预览注册后转发到 Server 的 `POST /api/jobs/{id}/manual-trims`。
2. Server 创建独立的 `manual_trim` 任务，来源快照、Episode、删除区间、帧数和 `run_doctor` 进入去重键；相同请求复用排队或运行中的任务。
3. Worker 以 `trim-manual` 模式计算删除区间的补集，复用现有裁剪写入器重建数值帧、全部相机视频、`frame_index`、全局 `index`、时间戳、tasks、split 和统计，然后执行深度结构验证。
4. 只有输出 Episode 数与来源一致、总帧数等于来源帧数减去删除帧数时才会发布；失败不会留下半写入的数据集。输出是新的独立数据集，来源数据集保持不变。

任务完成后可在面板中直接打开裁剪结果预览，也可以在管理台继续运行 Doctor 或质检。任务字段为 `manual_episode`、`manual_removed_ranges`、`manual_removed_frames`、`manual_summary` 和 `trim_report`，溯源写入 `dataset/meta/bodypipeline-manual-trim.json` 与工作目录的 `lineage.json`。接口：`POST /api/jobs/{id}/manual-trims` 接收 `episode_index`、`remove_ranges`、`name` 和 `run_doctor`；网关另提供 `GET /api/manual-trims/{dataset_id}/jobs/{job_id}` 轮询状态和 `POST /api/manual-trims/{dataset_id}/jobs/{job_id}/preview` 打开结果。

## 3. 预览与质检

### 3.1 运行 Doctor

对已有转换结果可以单独点击“运行质检”。Server 会依次执行：

1. 转换结构检查；
2. `letools validate`；
3. `letools doctor check --json`；
4. 保存报告和日志。

Doctor 固定执行 11 项非运动学检查：

```text
metadata
temporal
actions
videos
statistics
episodes
consistency
training
anomalies
portability
per_episode
```

明确不执行：

- 关节 / 夹爪软限位检查；
- URDF 正逆运动学检查；
- 自动修复、裁剪或删除。

Doctor 报告中如果出现 WARN / FAIL，数据集仍保留，仍可预览，方便定位问题。

### 3.2 独立质检报告页

“预览与质检 → 转换结果”中的“查看报告”会跳转到独立报告页：

```text
/report                      # 任务选择页，列出全部转换任务与质检结论
/report/<job_id>             # 指定任务的完整报告
```

报告页与工作台共用 `PIPELINE_TOKEN` 登录（HttpOnly Cookie）。页面本身可匿名打开，
但数据接口仍需登录，未登录时会显示登录表单。报告页只读取 Server 上已保存的
`doctor_report` 与 `quality_gate`，不会触发新的转换，也不会修改数据集。

页面内容包括：

- 数据集、机器人类型、目标格式、来源模式、episode 数、总帧数、FPS、任务状态与路径；
- 总体结论与 PASS / WARN / FAIL 检查项计数；
- 质检记录切换：转换时的自动报告，或任一独立质检运行，并可读取对应质检 / 验证日志；
- 检查项明细：中文检查名 + 原始英文检查名、检查目的、逐条明细消息，
  通过项折叠显示，可按“仅警告与失败 / 仅失败”筛选并搜索；
- 原始 Doctor JSON、Gate 覆盖范围、时间对齐与转换检查、逐 episode 诊断；
- “导出 JSON”把当前任务、所选报告和全部质检记录下载为单个文件。

报告明细消息保留 Doctor 原始英文，便于与上游工具对照；中文只覆盖检查名、状态和说明。
`/report` 仅在 Server 角色挂载，Client 不暴露该页面。

### 3.3 内容分析（Argus）

Doctor 回答"数据结构和统计是否可用"，内容分析回答"录像里实际发生了什么"。
两者针对同一份数据集，因此**运行 Doctor 时同步启动一次本地内容分析**。

分析产物写入 `<data_root>/<robot_type>/analyses/<analysis_id>/`，与任务工作目录分离，
不修改已转换的数据集。分析结论不参与任务状态：Doctor 的 `COMPLETED` / `QUARANTINED`
仍是唯一的质量门禁。

| 模式 | 内容 | 费用 |
| --- | --- | --- |
| `local` | 数据准备 + 确定性检查（像素、冻结、跨模态、夹爪） | 免费，默认 |
| `full` | 额外调用视觉语言模型做语义标注 | 按量计费，受预算约束 |

预算：

- 任务额度 = `min(任务上限, 每 episode 额度 × 本批 episode 数)`，默认每 episode 1 美元。
- 每日额度按本地零点结算，未结算的预留继续占用额度。
- 预算不足时 `full` 被拒绝，`local` 仍可运行；已产生的部分结果保留。

结论有三种，且**"未评估"不等于通过**：

| 结论 | 含义 |
| --- | --- |
| `CLEAN` | 所有检查都执行了，且没有命中 |
| `REVIEW` | 至少一项检查命中 |
| `INCOMPLETE` | 有检查无法评估（缺单位、缺安装关系、数据静止等） |

跨模态检查（相机与机械臂是否对应）需要 Profile 在 `cameras[].mounting` 声明
相机安装关系（如 `wrist_left` / `scene`）。未声明时该检查会被标记为未评估，
并在分析结果中说明原因；不会为了"通过"而假设安装位置。

### 3.4 打开 Visualizer

在“预览与质检”中选择可用任务，点击“预览数据”。

行为：

- LeRobot Visualizer **随 Server 常驻运行**，不会因为点击而启动或停止；
- 点击只把该数据集登记到网关，并在新标签页打开 `<viewer_origin>/bp/<job_id>/episode_0`；
- 可以**同时打开多个数据集**，每个数据集一个标签页，互不影响；
- Viewer 与工作台共用 `PIPELINE_TOKEN` 登录；
- 预览链接默认 30 分钟过期，再次点击会续期；
- 页面下方“LeRobot Visualizer”面板列出当前所有已打开的预览，可逐个“打开”或“关闭”。

“关闭”只是撤销该数据集的预览链接（立即对该数据集返回 404），不影响其他预览，
也不会停止 Visualizer 进程。若浏览器拦截了新标签页，页面顶部会显示“打开数据预览 ↗”链接。

Visualizer 刚启动或因故退出时，网关会显示“正在启动”等待页（每 2 秒自动刷新）；
进程退出后 Server 会在约 30 秒内自动重新拉起。服务不可用的原因显示在面板里。

Viewer 入口与 Server 使用同一个 `PIPELINE_TOKEN`：未登录时显示登录页，`/_data` 与视频请求返回 401。仍建议只绑定内网或 Tailscale 地址，不要直接暴露公网。

### 3.5 预览不可用

可能原因：

| 现象 | 检查项 |
| --- | --- |
| 没有“打开数据预览”按钮 | 任务尚未达到可预览状态 |
| Viewer 返回 503 | 未启动会话或会话已过期 |
| 页面空白 | Bun、Visualizer 进程、Viewer Gateway 或浏览器控制台 |
| 视频不能拖动 | 数据桥 / Range 转发异常 |
| 某些相机缺失 | LeRobot 输出视频特征或转换日志 |

## 4. 数据集管理

### 4.1 删除数据集

选择机器人类型后，“已有数据集”展示该类型下的原始 collection 与已转换、已导入、已合并的
LeRobot 数据集，可按类型和名称 / ID 筛选。删除前显示实际文件范围，必须输入完整数据集名称确认。
这是永久删除真实文件，不能通过 Viewer 的 Episode 回收站恢复。

- 原始 collection：删除该 collection 中全部已登记 episodes。服务接收的数据会删除对应 episode
  目录；原地导入的数据只删除 Manifest 登记的文件及 Manifest，HDF5 导入只删除已登记 HDF5 文件，
  不递归删除包含它们的共享目录。未登记文件和已经生成的转换结果保留。
- LeRobot 结果：删除该结果的 `dataset` 目录（原地导入的 LeRobot 数据集也会删除源目录）。
  原始数据、已经完成的独立合并 / 版本转换 / 首尾裁剪结果保留；任务工作目录内的中间文件、日志和历史报告不一并清理。
- 未完成上传，或仍有排队 / 运行中的转换、合并、版本转换、首尾裁剪、Doctor、Argus 或 Episode 永久清理任务时，
  禁止删除。路径与其他登记数据集重叠、存在目录链接或目标为存储根目录时同样拒绝，并显示原因。
- 删除请求进入后台队列，立即关闭对应预览并禁止再用于转换、预览、合并、版本转换、首尾裁剪及质检。
  后台在同一文件系统内改名后清理文件，记录路径和 inode，进程重启后继续执行；失败保留记录，
  可通过“删除记录 → 重试删除”继续。权限不足时需由管理员修复实际文件权限后重试。
- 删除保留状态墓碑与审计记录，已删除的数据从原始数据、预览、合并和版本转换候选中移除。
  原始数据仍在时，可以重新提交转换生成新结果；已删除的原始采集不能沿用原采集 ID 自动重新上传。

接口：`GET /api/datasets` 返回数据集、可删除状态与删除记录；`POST /api/dataset-deletions`
接受 `kind`（`raw` / `result`）、登记 `id` 和 `confirmation`（完整名称），返回 202。
失败任务通过 `POST /api/dataset-deletions/{id}/retry` 重试。接口使用工作台身份认证，不接受任意磁盘路径。

### 4.2 合并 LeRobot 数据集

入口位于侧边栏“预览与质检”的下方。选择机器人类型后，勾选 2–100 个已有的 LeRobot 数据集，填写新数据集名称并创建任务。已转换、已导入和已合并的可用数据集均可作为来源。

来源必须具有相同的 LeRobot 版本（v2.1 或 v3.0）、机器人类型、FPS、特征结构（包括名称、顺序、维度、单位等）、split 名称和模拟标识。每个来源须只有一个覆盖全部 Episode 的 split；有回收站 Episode 的来源需先还原或永久清理。不兼容来源在页面显示原因，后台也会重新校验。

合并按勾选顺序拼接全部 Episodes，保留各自的 instruction，重新编号 Episode、帧和任务标签并汇总统计信息。相同片段不会自动去重，跨版本合并暂不支持。来源数据保持原样，新输出保存到：

```text
<data_root>/<robot_type>/work/<new_job_id>/dataset
```

输出目录及其管理的父目录对所有人可读写、可进入（`a+rwX`），普通数据文件对所有人可读写，沿用数据转换的共享权限规则。即使启动服务的用户设置了 `umask 077`，发布前也会补齐权限；权限设置失败时任务失败，不发布结果。源数据与服务凭据、任务状态文件的权限不会被批量修改。

任务复用现有后台串行队列、超时和 Slurm 配置。“合并任务与结果”展示阶段、失败原因与结果入口。合并完成后可在“预览与质检”预览、运行 Doctor 或使用可视化评分。合并自身执行深度结构验证，不自动运行 Doctor。

提交时记录来源文件快照，执行前后再次核验；来源变化、空间不足、验证失败或权限设置失败均不会发布正式数据集。来源记录和 Episode 范围保存在 `dataset/meta/bodypipeline-merge.json` 与工作目录的 `lineage.json`。中断任务保留诊断文件，页面“重新选择 / 重试”会创建新任务和新输出目录。

接口：`GET /api/merge-candidates` 获取候选及兼容性，`POST /api/merges` 接收 `name` 和有序的 `source_job_ids` 并返回 202；进度、日志和结果沿用现有任务接口。相同名称、来源顺序和快照的排队或运行中请求会复用同一任务。

## 5. 限位与质量边界

数据转换和 Doctor 不执行机器人软限位或 URDF 运动学门禁。Profile 中的 `state_limits` / `action_limits` 只作为硬件说明元数据保留，不是数据转换准入条件。

即使原始数值超出标称范围，转换链也不会：

- 裁剪值；
- 删除 episode；
- 拒绝转换；
- 自动修改夹爪单位。

机器人限位、关节标定、控制器安全包络和运动急停由机器人控制器与安全层负责。若数据数值异常，应作为数据语义或硬件标定问题单独排查，而不是在转换器中添加软件限位。

## 6. 历史任务兼容

升级前记录的任务可能包含旧的 `dataset_path` 或不同目录布局。Server 会在配置允许的根目录内只读解析这些路径：

- 不移动；
- 不复制；
- 不重写；
- 不重新生成。

路径不在 `root`、`data_root` 或 `import_roots` 内时不会开放预览。旧报告中的检查项数量和“运动学检查被跳过”记录保持原样，不与当前 11 项报告混为一谈。

## 7. 数据维护

- 原始数据位于 `data_root/<robot_type>/raw/`。
- 任务工作区位于 `data_root/<robot_type>/work/`。
- 导入数据保留在源目录，由 Server 记录引用。
- 转换失败时保留工作目录和对齐报告用于诊断。
- 数据集可在“数据集管理”逐个永久删除；工作目录中间文件、日志清理，以及归档和备份仍由运维流程执行。
- 备份时必须同时保存状态 JSON、Manifest、原始数据、转换结果和报告。
