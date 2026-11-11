# 转换时平移夹持阶段的夹爪闭合度

Server 的原始数据转换表单支持可选的夹爪动作修正。当前 ARX 的
`arx-x5-hdf5-v1`、`arx-x5-hdf5-mem-v1`、`arx-x5-direct-teaching-v1` 与
`arx-x5-kinesthetic-v1` 声明此能力。选择这些 Runtime 时显示开关，
默认关闭；启用后输入 0–100 的闭合度增加量（百分点），初始值为 10。
其他 Runtime 隐藏控件，切换 Runtime 会清空启用状态；后端也拒绝不支持的选项。

百分比定义为 **Profile 配置的电机名义行程比例**，不表示实际开口宽度、
力矩或夹持力。ARX 当前参考端点为 open=-3.14、closed=0：
增加 10 个百分点相当于在原始 action 上平移 0.314，增加 5 个百分点相当于
0.157。沿用原数据的数值单位，不在此功能中自动纠正旧数据将夹爪标为 `m`
的元数据。

## 修改范围

`AlohaHDF5Provider.prepare()` 在独立的转换工作副本上处理：

1. 每个 episode、每个配置的夹爪单独处理，**只用实测 `observation.state` 作为判据**，
   不把 action 当作夹持证据；判定“确实夹住”后才改 action。
2. 中值滤波只辅助识别；需要先出现闭合，再找到部分闭合的稳定平台。
   静止的初始姿态、空夹全闭、短暂停留不作为夹持平台。
3. 只在确认向下释放时结束阶段，不因位置到达名义空夹电平而提前结束；
   只选择阶段内稳定窗口覆盖、持续足够久且整体位置变化足够小的平台。
   识别阈值及时间窗口由 Profile 声明。
4. 阈值按本条 episode 自适应：幅度阈值乘以实测行程占名义行程的比例，
   量程小的 episode 自动降低最小闭合幅度；所有幅度同时以稳健噪声（MAD）
   的倍数兜底，噪声大的 episode 自动加宽释放确认窗口。
5. 释放判定改为单调性 + CUSUM 变化点：要求连续下降、回撤不超过噪声带，
   且前向确认窗口内的累计跌幅超过阈值；固定窗口的幅度投票容易在噪声下反复触发。
6. 对每个识别出的夹持区间计算一次固定偏移，把区间内的原始 action 整体向
   闭合方向平移；不再把 action 拉齐到某个固定目标值。
   开合方向由各通道的 open/closed 端点决定，通道下标从 joint_names 解析。
7. 后续正常转换到 LeRobot v2.1 或 v3.0，由 LeTools 基于修改后的 action 生成统计。

原始 HDF5、实测 state、非夹爪 action 和图像保持不变。现有转换链路只输出
`action`，本功能不新增 `action_eef`。输出继续使用既有的共享读写权限策略。
未找到需要提高闭合度的帧时正常生成数据，报告警告与零修改数。

检测器版本为 `closing_hold_plateaus_v4`，修正方式为 `additive_shift_v1`。
检测逻辑参考 `relabel_arx_gripper_closure.py` 和既有
`closing_hold_plateaus_only_v2` 数据处理清单：检测到向上闭合并确认存在稳定
平台后，从闭合起点开始把 action 整体平移；只有确认向下释放后，才恢复该区间
后续的原始 action。它不是历史 v2 处理的逐帧复刻，也不会把所有未进入夹持阶段
的帧强制改为固定值。位置推断无法证明真实接触，需在新任务和真机上验证。

v4 修正了 v3 在“浅夹持”上的提前结束问题。v3 在结束判定中把
`closed - empty_closed_margin` 当作硬结束电平，而浅夹持平台常停在该电平附近：
开合端点为 0 与 -3.14、余量 0.2 时阈值是 -0.2，实测平台却在 -0.19～-0.22 之间，
于是闭合刚结束就被判为“空夹已全闭”，平台后半段没有继续修正。
平台期实测斜率只有约 -0.003 rad/s（-0.1% 行程/s），真实释放为
-0.33～-2.25 rad/s（-10%～-72% 行程/s），相差两个数量级以上。因此 v4 只在
入口阶段用该电平否决“空夹全闭”；进入 HOLDING 后必须出现真实下降
（`release_drop`、`release_confirm_drop`、单调性、CUSUM 同时满足）才结束，
不再因电平触及名义空夹位而恢复原始 action。

### 保持结束判据

结束 HOLDING 用的是**平均变化率 + 累计幅度**，不是电平比较：

1. `release_window_seconds`（默认 0.2 s）内的均值下降必须超过
   `release_drop`（默认 0.045，按本条 episode 实测行程缩放并受噪声下限约束），
   等价于约 3%～7% 名义行程/秒的下降速率；
2. `release_confirm_seconds` 内累计下降超过 `release_confirm_drop`；
3. 逐帧下降的单调比例不低于 `release_monotonic_fraction`，且单边 CUSUM
   越过噪声带。

平台期实测只有 -0.1% 行程/秒，比阈值低一到两个数量级，所以稳定平台不会
被误判为释放；真实的释放段（-10%～-72% 行程/秒）在一个确认窗口内即可确认。
本判据只看向下（张开）方向：保持期间继续闭合不需要结束保持，因为区间内的
平移量只取决于原始 action，不会因为实测继续闭合而改变。

### 相位区间与 action 写入

检测器的逐帧结果不会直接写入 action，v3 增加了三层约束：

1. **区间组装**：检测片段先合并为连续区间。相邻区间之间不超过
   `minimum_gap_seconds`（默认 0.2 s）的检测空洞会被填平；短于
   `minimum_phase_seconds` 的区间直接丢弃。一个物理夹持只应产生一个区间，
   避免 `True → False → True` 的 mask 让 action 反复跳变。
2. **进出 ramp**：action 不再在区间边界硬切。`correction_ramp_seconds`
   （默认 0.15 s）内的帧按 smoothstep 权重把固定偏移从 0 过渡到全量，再平滑
   退出；区间内部权重为 1，因此“新 action − 原 action”是常数。
3. **通道共享相位**：Profile 的通道可声明相同的可选 `group`。同组通道先归一化
   闭合比例并取中位数，只检测一次相位，再把同一区间应用到组内所有通道；
   组内共用一个归一化增量，保持通道间的相对位置。不设置 `group` 时各通道独立
   检测（双机械臂的两个夹爪就是独立机构）。
4. **action/state 时间偏移**：Provider 声明 `correct_actions(...,
   action_state_offset=...)`。当数据契约是 `action[t] == state[t + 1]` 时
   （如 `arx_kinesthetic`）传 `1`，把观测区间整体前移一帧后再写 action。

### 固定偏移与接近全闭时的整段限幅

区间内闭合比例定义为：

```text
p(t) = (action(t) - open) / (closed - open)     # 0 全开，1 名义全闭
新 action(t) = action(t) + weight(t) × Δ × (closed - open)
```

- `Δ` 是本次请求的 `increase_percent / 100`。
- 每个夹持区间只计算一次 `Δ`，区间内所有帧共用，因此原曲线的起伏和趋势被保留，
  不会被压成平顶。`weight = 1` 的区间内部严格满足“新 action − 原 action 为常数”。
- 接近名义全闭时不允许逐帧截断。区间允许的最大增量为

  ```text
  允许增量 = max(0, 1 - 区间内原始闭合比例峰值)
  实际增量 = min(请求增量, 允许增量)
  ```

  整段按区间峰值统一缩小；若原曲线已经触及或超过名义全闭端点，该区间不再增加，
  报告 warning 说明“部分夹持区间已接近名义全闭，增量按区间峰值缩小”。
- 同一 `group` 的多个通道取组内所有通道的峰值共同限幅，保证组内使用同一个
  归一化增量。

因此“90% 目标值”不再是本功能的语义：如果原曲线本身在 60% 附近波动，
增加 10 个百分点后是 70% 附近同样形状的曲线，而不是每一帧都达到 90%。

### 诊断字段

每条 episode 的自适应结果（实测行程、噪声带、缩放比例、生效阈值）写入
`action_corrections.gripper_closure.channels[*].adaptive`，便于复核为什么某个
episode 被识别或跳过。`adaptive` 同时记录 `intervals`、
`raw_interval_count`、`filled_gap_count`、`dropped_interval_count`；
通道级记录 `increase_percent`、`applied_intervals`、`selected_frames`、
`ramped_frames`、`full_frames`、`action_state_offset`、`group` 以及选中/实际
修改的半开帧区间。

`applied_intervals` 按区间记录：

```text
from_frame / to_frame_exclusive
requested_increase_percent
applied_increase_percent
max_original_closure_percent
limited_by_travel
```

报告顶层记录 `algorithm: closing_hold_plateaus_v4`、
`correction: additive_shift_v1` 和 `increase_percent`。新增的自适应参数位于
Profile 的 `conversion.gripper_closure.phase_parameters`，缺省值见
`pipeline/server/providers/gripper_closure.py` 的 `DEFAULT_PARAMETERS`。

## 公共接口和追溯

`POST /api/uploads/{id}/process` 和 `POST /api/collections/{id}/process`：

```json
{
  "instruction": "Unscrew the screw.",
  "target_format": "v3.0",
  "run_doctor": true,
  "conversion_options": {
    "gripper_closure": {"increase_percent": 10}
  }
}
```

省略 conversion_options、传 `{}` 或传 `{"gripper_closure": null}` 均表示关闭。
百分比必须是有限数值，拒绝字符串、布尔值、未知字段及不支持此能力的 Runtime。
百分比进入任务去重标识；任务持久化当时的选项、Profile 和算法版本。
Worker 使用任务快照，修改部署配置不会改变已排队任务的增量。

`GET /api/config` 的 runtimes[].conversion_capabilities 由 Provider 生成，
当前返回 `correction`、`minimum_increase_percent`、`maximum_increase_percent`
与 `default_increase_percent`。前端只根据能力渲染，不判断机器人型号、不接触
关节下标和位置端点。新 Provider 可实现 conversion_capabilities(profile) 并扩展
prepare(..., options=...)；未声明能力的旧 Provider 继续只接收无选项转换。

任务详情展示增加百分点及修改帧数。alignment-report.json、lineage.json 保存
每个 episode 的算法、修正方式、名义端点版本、阈值、请求/实际增量、选中/实际
修改帧数及半开帧区间。

## 升级说明：从 `minimum_percent` 到 `increase_percent`

旧版本（`closing_hold_plateaus_v1`/v3）的参数名是 `minimum_percent`（默认 90），
语义是“把 action 抬到至少该闭合比例”。新版本改为 `increase_percent`（默认 10），
语义是“整段向闭合方向平移多少个百分点”。**两者的数值不能直接迁移**：

- 旧任务快照中的 `minimum_percent` 不会被重新解释；已排队任务按原选项执行。
- 新任务必须传 `increase_percent`。传旧字段会被视为未知字段并拒绝。
- 若希望接近原先 90% 目标的效果，需要按实际数据判断；新语义不保证达到固定水平。

## 部署已有 ARX Runtime

此次为 ARX Profile 增加 conversion 配置并更新算法版本，会改变 Profile 摘要。
若要继续接收旧版采集配置，审核后在 **ARX Runtime 自己的**
accepted_capture_profile_hashes 中保留旧摘要。当前仓库四个 ARX Profile 的摘要为：

```text
arx-x5-direct-teaching-v1.json  816ee56bb7068abbb1ab9797613ccd10952155f7047f7d76a3c2bbd896bb39e6
arx-x5-hdf5-v1.json             a50a06ba6a4a62b680a052ae2eb81d584e2c5681791b6b119d4cb152612c9ac8
arx-x5-hdf5-mem-v1.json         1dd20ade7371e93fb88c9a2816808d9483ef9bdb0419d649fc109c9d695ff245
arx-x5-kinesthetic-v1.json      8831ad8f8c9588885fd2de36ab0eba41a21a303cee640ed4f98eb6b38d118557
```

部署后应以实际发布包中 Profile 文件的 digest 为准再更新兼容列表；不要把摘要
放到其他机器人的兼容列表，也不要自动接受所有旧摘要。已有部署若使用独立的
Profile 副本，应同步完整 conversion 配置后再重启；仅替换前端不会启用能力。
名义端点应与对应硬件/采集版本核对，不能由样本极值推断。
