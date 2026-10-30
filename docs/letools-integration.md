# letools 集成

BodyPipeline 不实现 LeRobot 转换和 Doctor 算法，而是在 Server 上通过子进程调用固定版本的 [XieWeikai/letools](https://github.com/XieWeikai/letools)。

## 1. 固定版本

| 项目 | 值 |
| --- | --- |
| 上游仓库 | `https://github.com/XieWeikai/letools` |
| 固定提交 | `1c417cc9c205f85cd01a5e5860646f1594738354` |
| Python | 3.12 |
| 工具环境 | `.runtime/letools-venv/` |
| letools 源码 | `.runtime/vendor/letools/` |
| Tianji provider | `tianji-provider==0.6.3`，data-providers commit `487418f5270a77722820d104015cda907fa6facd` |
| Python 锁文件 | `environments/letools/uv.lock` |

安装：

```bash
bash scripts/install_letools.sh
```

安装脚本会检查两个源码 checkout 是否干净、切换到固定提交、初始化 letools 子模块，并使用独立 uv 项目同步依赖，最后把官方 Tianji provider 安装到同一个 Python 环境。Server 配置中的 `letools_bin` 和 `tianji_provider_bin` 应分别指向：

```text
.runtime/letools-venv/bin/letools
.runtime/letools-venv/bin/tianji-provider
```

不要在 ROS Bridge 环境中安装 letools，也不要让 letools 依赖覆盖 Client / Server 主环境。

## 2. 上游能力边界

letools 是数据转换、验证、质检和可视化工具，不是机器人控制、上传、身份权限或任务编排平台。

BodyPipeline 负责：

- 机器人采集与上传；
- Manifest 和原始文件不可变封存；
- collection 分组；
- Server 身份与设备边界；
- 时间对齐 staging；
- 任务状态、日志、重试和报告展示；
- Viewer 会话生命周期。

letools 负责：

- 读取支持的输入格式；
- 转换为 LeRobot v2.1 / v3.0；
- deep validate；
- Doctor 检查；
- Visualizer 前端与数据桥。

当前 AgileX Provider 的主路径是单文件 HDF5：

```text
--source-format hdf5 --preset <generated-preset>
```

旧版 `agilex_timestamped_directory_v1` 仍使用 `--source-format agilex` 兼容读取，但不是新采集的默认
路径。其他 Provider 可以声明不同的 source-format 和输入布局；这不代表 letools 能直接读取所有
ROS2 bag、MCAP 或任意机器人目录。新增输入格式必须在 Server 侧增加 Provider / staging adapter，
或先确认上游已有可用 source provider。

当前 Tianji Server 适配使用官方 `tianji-provider`：

```text
episode0/
├── data/metadata.yaml + *.mcap
├── video/metadata.yaml + *.mcap
└── bodypipeline-source.json  # 可选来源标记
```

对应配置示例是 `configs/server.tianji.json`，Profile 是
`configs/tianji-thor-v1.json`。Server 先复制并核验每个 `episode0`，再调用同一独立环境中的
`tianji-provider inspect` 计算保留帧数，最后执行：

```bash
<letools_bin> convert <work>/aligned <work>/dataset \
  --source-format tianji --task '<instruction>' --fps 50 --wrench-fps 200 --to v2.1
```

官方输出包含 `observation.state`、`observation.action`、`observation.wrench_windows`、
`observation.eef` 与四路 `observation.image.*`。输入原始 MCAP 保持只读，Server 不在主环境安装
ROS2 或 MCAP 解码依赖。

## 3. Server 调用链

### 3.1 环境诊断

任务开始前和安装后执行：

```bash
<letools_bin> doctor environment
```

非零退出会阻断转换任务。

### 3.2 转换

`AgileXProvider` 从上传的 `episode.hdf5` 读出时间戳流，生成
`work/<job_id>/aligned/episodeN.hdf5`，再根据受信任 Profile 生成
`letools-hdf5-preset.json`。转换命令由 Provider 生成，不是通用 `JobRunner` 的硬编码：

```bash
<letools_bin> convert \
  <work>/aligned \
  <work>/dataset \
  --source-format hdf5 \
  --preset <work>/letools-hdf5-preset.json \
  --to v3.0 \
  --workers 1 \
  --video-workers 1 \
  --video-codec libx264 \
  --video-pixel-format yuv420p
```

HDF5 preset 明确声明：

- `action` 与 `observation.state` 的 source/target key、float32 dtype 和关节名；
- 三路 `observation.images.<camera>` 的 source key、宽高与 JPEG 编码；
- 输出 fps、robot type 和本次任务的 instruction。

因此 Server 不需要解包原始 HDF5，也不需要恢复时间戳 JPEG/JSON 目录。旧版
`agilex_timestamped_directory_v1` 仍可通过 `--source-format agilex` 兼容转换。

目标格式支持：

- `v3.0`
- `v2.1`

转换后 Server 会回填 Profile 中的 `joint_names` 到 `meta/info.json`，并检查 episode / frame 数量与输入快照一致。

已有 LeRobot 数据集不经过机器人 Provider 或 HDF5 时间对齐。Server 从已登记的数据集记录中选择来源，
锁定来源目录并记录文件快照，然后直接调用 Letools 的 LeRobot 读取器：

```bash
<letools_bin> validate <source>
<letools_bin> convert \
  <source> \
  <work>/dataset \
  --source-format lerobot \
  --to <v2.1|v3.0> \
  --workers <N> \
  --video-workers <N>
<letools_bin> validate <work>/dataset --deep
```

来源与目标必须不同，仅支持 v2.1 与 v3.0 双向转换。已有 MP4 不传视频编码参数，由 Letools
执行 remux 和版本切片。结果保留 Task、特征、统计信息及模拟标识，并写入
`meta/bodypipeline-version-conversion.json` 记录来源任务、版本和快照。来源文件保持只读，
转换输出使用独立的 `work/<job_id>/dataset` 目录和共享权限。

### 3.3 深度结构验证

```bash
<letools_bin> validate <dataset> --deep
```

这一步验证目录、元数据、schema、Parquet 和媒体可读取性。失败会阻断预览和 Doctor。

### 3.4 Doctor

```bash
<letools_bin> doctor check <dataset> \
  --checks metadata,temporal,actions,videos,statistics,episodes,consistency,training,anomalies,portability,per_episode \
  --ci \
  --fail-on warn
```

BodyPipeline 固定排除 `kinematics`，只运行上述 11 项非运动学检查。原因：

- 关节限位和 URDF 运动学属于机器人控制器与安全层；
- 当前 LeRobot 元信息不足以可靠推断所有机器人的运动学约束；
- 数据管线不应裁剪、拒绝或改写超出 Profile 标称范围的原始值。

Doctor 的 JSON 报告必须与数据集 `meta/info.json` 中的 episode 数和帧数一致，并包含预期的 11 项检查。退出码和报告 severity 一起用于状态判定：

| 结果 | 任务状态 |
| --- | --- |
| PASS | `COMPLETED` 或 `SYNTHETIC_READY` |
| WARN / FAIL | `QUARANTINED` |
| 工具异常、报告无效、检查缺失 | `FAILED` |

WARN / FAIL 不会删除结果目录，仍可在 Server 中预览和查看报告。

#### Episode 质量评分

Visualizer 的 Episode 侧栏显示逐片段总分（0–100），选中片段时展示动作平滑度、动作覆盖度、
跨片段一致性和长度四个分项，以及原算法建议复查的提示。评分不参与 Doctor 质量门禁，也不执行
数据修复、裁剪或删除；分数是启发式指标，不代表任务成功率，也不适合作为不同机器人间的统一质量标尺。

首次打开预览后，浏览器通过 Viewer Gateway 的 `POST /api/episode-scores/<dataset_id>` 提交评分，
再用同路径 GET 读取状态与结果。接口沿用 Viewer 鉴权、同源校验和预览有效期；只接受已登记的数据集 ID。
评分运行于现有 LeTools Python 环境中的独立 CPU 子进程，不占用转换队列；Viewer 最多同时计算一个数据集，
播放可以继续。结果缓存于私有 `state/episode-score/`，原数据集保持只读。元信息或 Parquet 分片变化后，
旧结果失效并在再次打开/轮询时重新计算；计算期间发生变化则不发布该次结果。

固定 Doctor 的 `score` 数据加载器会展平向量动作、遗漏后续分片。`episode_score_worker.py` 在独立
子进程中替换该加载步骤，合并同一 Episode 的各分片并保持动作矩阵的 `(帧数, 维度)`，随后调用
原版 `score_episodes` 算法和权重；不修改上游源码。每个声明的 Episode 都保留一行，缺数据、
少于三帧、索引异常或含 NaN/Inf 时显示“无法评分”及原因。有效 Episode 不足四个时，原算法
一致性项未评估且按零计入总分；界面显示该项为 `—` 并说明这一限制。更新 Doctor 固定版本时需重新验证适配。

Viewer 原有“筛选”页在“Episode 时长筛选”下方增加“Episode 评分筛选”，复用原有交互：
拖动双滑块设置保留的评分区间，再批量标记范围外的片段，通过原有已标记列表复查。
支持总分、平滑度、动作覆盖、一致性和长度，分值范围 0–100，包含边界；滑块支持键盘
微调，上下限不会交叉。无此项评分的片段只显示数量，不作为零分或范围外片段自动标记。
回收站片段不参与判断，永久清理期间暂停筛选。批量标记保留已有标记；原有时长、
低运动量及速度筛选逻辑保持不变，不增加标签页或结果表。
`viewer_score_filter.patch` 和评分筛选组件参与构建指纹，更新后重启 Viewer 会自动重新构建。

### 3.5 Visualizer

BodyPipeline 不再为每个数据集执行 `letools visualizer serve`（它把应用绑定到单个数据集，
导致同一时间只能预览一个）。Server 改为：

```bash
<letools_bin> visualizer setup --cache-dir <server_root>/viewer-cache   # 准备固定版本源码并应用补丁
bun run build   # DATASET_URL=<viewer_origin>/_data，仅在输入变化时构建
bun run start --hostname 127.0.0.1 --port <viewer_ui_port>
```

Visualizer 由 `pipeline/server/viewer_bootstrap.py` 启动并常驻；网关用
`<viewer_origin>/_data/bp/<job_id>/resolve/main/...` 这一与 Hugging Face 相同的 URL 形态
按数据集提供文件，并在 `/_data/doctor/?dataset=bp/<job_id>` 提供 Doctor 报告。

要求：

- Bun 1.3.9 位于 `viewer_bun` 或服务 `PATH`；
- UI 和数据桥只监听 loopback；
- Viewer Gateway 代理 UI、数据 API 和视频 Range；
- 只读、关闭 annotation 后端；
- 可同时登记多个数据集，各自独立；
- 预览链接默认 30 分钟过期；
- Viewer 入口与 Server 共用 `PIPELINE_TOKEN`，未登录返回登录页；仍应只监听内网或 Tailscale 地址。

关节曲线固定为每个通道一个图框，同一通道的 `action` 与 `observation.state` 放在一起对照。
不按数值范围组合不同通道，也不提供“合并显示”模式。启动时自动应用
`pipeline/server/viewer_channels.patch`；补丁内容参与构建指纹，更新后会在下次启动
Visualizer 时重新构建，无需重新转换数据集。

`viewer_presentation.patch` 和 `pipeline/server/viewer_ui/` 在上述基础上提供信号浏览器：
关节位置、关节速度、力矩、末端位姿、底盘位姿及底盘速度分别查看。每张图仅显示
同一通道、同一类别的信号，指令使用蓝色实线，实测使用琥珀色虚线；未知字段独立成类。
原始采样和单位不变，支持图例开关、时间联动、通道筛选和单列放大。三路视频等宽铺满，
保持画面比例，顺序为左腕、Head、右腕；支持隐藏、恢复、放大和 Esc 退出放大。

工作台、预览、登录和质检报告默认使用深夜模式，可切换浅色。`pipeline/web/theme.js`
通过同主机 Cookie 在 Server / Viewer 端口间共享偏好，并用 localStorage 保留设置。
页面头部先初始化主题，避免进入页面时闪白。所有展示补丁、UI 源文件与主题脚本都参与
构建指纹；部署后重启服务即可更新，结果文件无需移动或重新转换。

### 3.6 中文基础补丁与主题覆盖

上游 Visualizer 是英文深色界面。BodyPipeline 通过 letools 的集成补丁机制，在准备 Visualizer
源码时叠加一个中文化与主题补丁，不修改上游子模块：

```text
patches/letools-visualizer/0002-zh-cn-light-theme.patch
```

安装（在已执行过 `scripts/install_letools.sh` 的机器上）：

```bash
bash scripts/install_visualizer_zh_patch.sh
```

脚本会用 letools 自身的解析逻辑找到补丁目录，并把补丁复制进去，因此同时支持
`git` checkout 与打包安装两种 letools 布局。补丁集合会进入 Visualizer 源码指纹，
下一次打开预览时会自动重建缓存并重新 `bun install`。下表描述基础中文补丁；
启动时还会叠加上一节的信号浏览器与深夜主题，最终主题以用户偏好为准。

补丁内容：

| 范围 | 变化 |
| --- | --- |
| 文案 | 标签页、侧边栏、统计、动作分析、筛选、帧预览、标注、Doctor、错误与提示全部中文 |
| 主题 | 深色改为 BodyPipeline 浅色：`#f7f8fa` 背景、白色面板、`#2455eb` 主色、相同圆角与字体 |
| 调色板 | 通过 `@theme` 重映射 slate / cyan / 红绿黄，避免逐个组件改写 className |
| 图表 | Recharts 网格、坐标轴、Tooltip 改为浅色，原有数据配色保留 |
| 标注 | `annotations-skin.css` 表面与前景色改为浅色 |
| 首页 | 去掉全屏视频背景，改为与工作台一致的浅色封面 |
| Doctor | Viewer Gateway 为 Doctor HTML 注入共享主题，随深夜 / 浅色偏好切换 |

边界：

- 这是展示层补丁，不改变 Doctor 检查项、数据读取、转换或任何质量判定；
- 上游业务逻辑与组件结构不变，升级 letools 时需要重新核对补丁是否仍能干净应用；
- 报告中 Doctor 的逐条明细仍保留英文原文，便于与上游工具对照；
- `--no-annotations` 仍然生效。对 Server 登记的 `bp/<preview-id>` 数据集，任务模板标注与语言 / VQA 共用服务端草稿和版本号，保存不改来源数据集；普通 Hub / 本地独立预览仍保留浏览器编辑模式；
- Doctor 标签页里的报告由 letools 以硬编码深色样式渲染，因此宿主在
  `pipeline/server/viewer.py` 的 Viewer Gateway 中给 `/_data/doctor` 注入共享主题，
  不 fork 固定版本工具链。该路由只在 Viewer 进程内生效，改动后需重启 `bodypipeline-viewer`。

任务标注时，视频与播放条固定在标注区上方，向下编辑字段时仍可查看画面。时间轴的普通点击和拖动只定位视频；按住 **Shift** 在空白轨道拖动新建片段，在已有片段上拖动移动片段，在左右边缘拖动调整边界。片段边界吸附源帧，视频预览按鼠标所在的实际时间定位；点击片段不会强制跳回其起点。

右键时间轴片段或左侧记录可删除这条标注，右下角提供撤销。`NULL · 无任务片段` 表示已经确认没有任务内容，和仍未标注的空白区分保存。服务端模式每帧只允许一个 Subtask，Subtask / NULL 片段之间不能重叠。

### 3.7 AI 初标、人工修订与服务端保存

管理台现有 AI 标注流程生成 Subtask 数据集后，在 Viewer 打开其预览。新时间轴直接加载该数据集的 AI / 来源片段；同名的多次尝试各自保留 ID、边界和评分。新工作区负责 Subtask / NULL、Episode 评价和单帧字段；下方的「更多标注：语言、计划与 VQA」继续编辑非 Subtask 内容，共用一次保存和一次导出。

- **保存到服务器**：`POST /api/subtask-review/<dataset-id>/episodes/<episode>` 同时保存 `segments`、完整 `task_document` 和手动 / VQA `atoms`。草稿位于 Server `state/subtask_draft/`，每次保存先写 `state/subtask_draft_history/` 历史，再发布新版本。另一窗口已保存或来源变化时返回 409，不覆盖旧版本。
- **共用任务模板**：`GET/POST /api/task-templates` 通过同源 Viewer Gateway 访问服务端模板库，保存需要模板库 `revision`。已保存 Episode 的模板快照和填写结果随草稿保留；重新打开时在页面应用共用模板的最新字段，核对后再保存。改变已填写字段的类型或作用范围时，旧值以「历史字段」保留。新 Episode 沿用数据集最近绑定的模板，同类数据集也可选择同一模板。
- **浏览器恢复**：未保存的完整工作区以 sessionStorage 做本窗口备份，返回 Episode 时可恢复；它不是已保存服务器的替代。旧版 localStorage 记录通过「导入浏览器旧标注」显式载入，核对后再保存，不自动覆盖 AI / 服务端草稿，原记录保留。
- **导出修订数据集**：先保存当前修改，再使用原有修订导出队列生成独立 v3.0 副本。每帧 `subtask_index` 和 `meta/subtasks.parquet` 表达最终 Subtask；`meta/bodypipeline-task-annotations.json` 无损保存模板、各作用范围的自定义值、尝试 ID、NULL 和单帧备注。NULL 与未标注空白在逐帧标签中均为 -1，通过该 sidecar 区分；相邻同名尝试也通过 sidecar 保持独立。语言 / VQA 仍通过 `meta/bodypipeline-manual-annotations.json` 保存。
- **重新打开**：修订副本的预览同时读取逐帧标签与任务 sidecar，核对语义覆盖一致后恢复新界面。后续人工修改不会影响已排队导出的快照。重新运行 AI 仍生成独立候选数据集；如果复制的旧任务 sidecar 已不适用于新标签，会作为 `meta/bodypipeline-task-annotations-source-*.json` 归档，不作为候选结果的当前标注。

这些字段通过 sidecar 随修订数据集交付，尚未将星级、备注等自定义字段展开成训练 Parquet 列。永久清理导致来源签名或 Episode 编号变化时，旧服务端草稿拒绝自动套用，需要重新核对。

### 3.8 本机 Mock 标注预览

已有 Python 基础环境、固定版本 letools、Node.js、Bun 和 FFmpeg 时，可准备一份独立的 Viewer 缓存并启动本机演示：

```bash
.runtime/letools-venv/bin/letools visualizer setup --cache-dir .runtime/annotation-preview/viewer
uv run --with pyarrow python scripts/preview_annotations.py
```

脚本会应用本仓库的 Viewer 补丁，生成两条含三路合成视频的 LeRobot v2.1 数据，并启动 Mock Client（18080）、Server（18081）和 Viewer（18082，内部 UI 13100）。这些服务只监听 `127.0.0.1`，运行数据及配置位于 `.runtime/annotation-preview/`。打开 `http://127.0.0.1:18082/annotation-demo.html`，演示登录密码为 `annotation-demo`，按 Ctrl+C 停止本次服务。

脚本首次启动会通过真实保存接口在演示 Server 中建立示例模板、重试片段和评价；已有服务端草稿不会被覆盖。演示入口保留旧浏览器示例，便于手动验证旧记录导入。可用 `--viewer-source <独立缓存>/source` 选择其他固定版本的演示源码缓存；该缓存会被应用界面补丁并采用本机字体，不要指向正在使用的生产 Viewer 缓存。

预览需要可用的 LeTools Python 环境读取源数据并执行修订导出，使用合成数据，不会调用付费 AI 或连接机器人。浏览器新窗口打开后可以读取已保存的 Server 草稿。

## 4. 与上游协作

新增机器人时，先确认以下事实，再决定是否扩展 letools：

1. 输入是否已有上游 source provider；
2. episode 顺序、索引和 schema 是否稳定；
3. 图像、视频、Parquet 的媒体描述是否完整；
4. state / action 特征名称、维度和单位是否可表达；
5. `letools validate --deep` 是否能读取输出；
6. Visualizer 是否能显示每路相机和关键 feature。

如果 letools 不支持新输入：

- 优先在 BodyPipeline Server 中增加独立 Provider / staging adapter；
- 必要时向上游提交通用 provider 或 bug fix；
- 不要让 `pipeline/web` 或采集页面出现机器人专用分支；
- 不要把“能读文件”误当成“state / action 语义已通过”。

## 5. 升级策略

升级 letools 时必须：

- 固定新 commit，而不是使用浮动 `main`；
- 更新 `jobs.py` 中的幂等工具版本；
- 更新 `environments/letools/uv.lock`；
- 重新运行 v2.1 / v3.0 转换、deep validate、Doctor 和 Visualizer；
- 比较 Doctor schema 和必需检查项；
- 用至少一个 AgileX 原始 episode 做完整回归；
- 记录未兼容的旧数据集和报告；
- 重新应用 `patches/letools-visualizer/0002-zh-cn-light-theme.patch`，确认它仍能干净应用。

不要在运行中的任务上替换工具环境；先排空任务、停止 Server，再同步依赖。

## 6. Episode 回收站

Viewer 侧边栏垃圾箱按钮位于标记按钮左侧，数据集回收站地址为
`/bp/<preview-id>/trash`。软删除状态按数据集绝对路径保存至
`state/recycle/`，因此多个预览链接共享同一个回收站，重新打开预览也不会丢失。
支持批量恢复与批量永久清理；所有写操作沿用登录认证与同源 CSRF 检查，
并要求提交列表 revision，避免误操作已经重新编号的 Episode。

永久清理由 letools Python 环境中的 `pipeline.server.recycle_worker` 执行：
保留其他 Episode，重写共享 Parquet 和受影响视频，更新计数、统计、拆分和
连续索引，完成 deep validate 后用 `renameat2(RENAME_EXCHANGE)` 原子发布，
最后删除旧数据。未受影响视频沿用原文件；跨 GOP 的剪切需要重新编码 H.264。
清理对象仅为当前转换数据集，不追溯删除原始采集目录或其他副本。
全部清空也保留有效的空数据集元数据。

同一数据集的回收站修改和清理使用跨进程文件锁。持久化日志及数据集内的
`.bodypipeline-recycle.json` 标记区分发布前后中断：发布前保留原数据，
发布后继续移除旧数据并完成状态更新。清理过程中检测到数据被其他进程修改
则取消发布。预览缓存随数据版本失效，剩余回收站项目与标记同步重编号。

验证：`tests/test_recycle.py` 测试 API、权限、路径约束及持久化；
`tests/fixtures/recycle_scenario.py` 在独立 letools 环境中验证共享数据/视频的
实际删除、非关键帧边界、全部清空，以及发布前后中断恢复。
