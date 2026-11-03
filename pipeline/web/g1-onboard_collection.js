// This component owns collection controls only. The host supplies telemetry
// and owns all camera streams; failures here do not stop the host's polling.
export function mountCollection(root) {
  const field = name => root.querySelector(`[data-field="${name}"]`);
  const button = name => root.querySelector(`[data-action="${name}"]`);
  const input = name => root.querySelector(`[name="${name}"]`);
  const text = (name, value) => { field(name).textContent = String(value); };
  let telemetry = null, telemetryAt = 0, session = null, busy = false;
  let rawInitialized = false, rawWasOwned = false;
  const preferenceKey = 'sonic.collection.preferences.v1';
  const textNames = ['subdirectory', 'dataset_name', 'task_prompt'];
  const preferences = {version: 1};

  function storageUnavailable() {
    text('preferences-state', '浏览器无法保存配置；本页仍可正常使用，刷新后可能丢失填写内容。');
  }
  function restorePreferences() {
    let stored;
    try { stored = window.localStorage.getItem(preferenceKey); }
    catch { storageUnavailable(); return; }
    let saved;
    try { saved = JSON.parse(stored); }
    catch { return; }
    if (!saved || saved.version !== 1 || typeof saved !== 'object' || Array.isArray(saved)) return;
    for (const name of textNames) {
      if (typeof saved[name] === 'string' && saved[name].length <= input(name).maxLength) {
        preferences[name] = saved[name];
        input(name).value = saved[name];
      }
    }
    for (const name of ['raw_only', 'camera_depth']) {
      if (typeof saved[name] === 'boolean') preferences[name] = saved[name];
    }
  }
  function savePreferences() {
    try {
      window.localStorage.setItem(preferenceKey, JSON.stringify(preferences));
      text('preferences-state', '配置已保存在当前浏览器、当前地址；刷新不会自动启动服务。');
    } catch { storageUnavailable(); }
  }
  restorePreferences();
  for (const name of textNames) {
    input(name).oninput = () => {
      preferences[name] = input(name).value;
      savePreferences();
    };
  }

  async function api(path, data) {
    const options = {cache: 'no-store', signal: AbortSignal.timeout(10000)};
    if (data !== undefined) Object.assign(options, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Collection-Action': '1'},
      body: JSON.stringify(data),
    });
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
    return result;
  }

  function render() {
    const exporter = session?.exporter || {};
    const rawKnown = typeof session?.raw_only === 'boolean';
    if (rawKnown) {
      if (session.session) input('raw_only').checked = session.raw_only;
      else if (!rawInitialized || rawWasOwned) {
        input('raw_only').checked = preferences.raw_only ?? session.raw_only;
      }
      rawInitialized = true;
      rawWasOwned = !!session.session;
    }
    input('raw_only').indeterminate = !rawKnown;
    input('raw_only').disabled = busy || session?.phase !== 'idle' || !rawKnown;
    text('output-format', !rawKnown ? '输出格式：未知' :
      input('raw_only').checked ? '输出格式：Raw 原始消息' : '输出格式：LeRobot');
    const camera = session?.camera;
    const cameraReady = camera?.ready && typeof camera.depth === 'boolean';
    input('camera_depth').indeterminate = !cameraReady;
    if (cameraReady) input('camera_depth').checked = camera.depth;
    input('camera_depth').disabled = busy || session?.phase !== 'idle' || !cameraReady;
    const previousDepth = typeof preferences.camera_depth === 'boolean' &&
      (!cameraReady || preferences.camera_depth !== camera.depth) ?
      ` · 上次选择：${preferences.camera_depth ? 'on' : 'off'}` : '';
    text('camera-depth-state', (camera?.error || (cameraReady ?
      `实际：${camera.depth ? 'on' : 'off'}` :
      typeof camera?.requested_depth === 'boolean' ? '切换 / 等待 RGB 恢复' : '状态未知')) + previousDepth);
    const online = telemetry?.sources || {};
    const fresh = telemetry && performance.now() - telemetryAt < 2500;
    const ready = session?.phase === 'running' && fresh &&
      ['camera', 'robot', 'pico_pose', 'manager'].every(key => online[key]?.online) &&
      ['left_wrist', 'ego_view', 'right_wrist'].every(name => {
        const age = telemetry.camera_age_ms?.[name];
        return age !== undefined && age >= -1000 && age < 500;
      });
    text('session-state', session?.phase || '控制连接断开');
    text('record-state', exporter.state === 'paused' ? '采集暂停（同一 episode）' : exporter.state || '未录制');
    text('frames', exporter.frame_count ?? 0);
    text('count-unit', exporter.count_unit === 'packets' ? '消息（raw）' : '帧');
    text('index', exporter.episode_index ?? '--');
    text('output', session?.output || '');
    text('episode-result', exporter.last_result ?
      `#${exporter.last_result.episode_index} ${exporter.last_result.outcome}` : '');
    text('message', session?.error || exporter.error || exporter.camera_health?.message ||
      (exporter.hold_active ? 'HOLD：B+Y 或 A+X 恢复动作及采集' :
        session?.pose_walk ? 'POSE 摇杆行走已启用：左杆前后移动，右杆左右转向' : ''));
    button('session-start').disabled = busy || session?.phase !== 'idle' || !cameraReady || !rawKnown;
    button('session-stop').disabled = busy || !session?.session;
    button('start').disabled = busy || !ready || exporter.state !== 'idle' || !!exporter.error || !!exporter.hold_active;
    button('stop').disabled = button('discard').disabled = busy || !['recording', 'paused'].includes(exporter.state);
    for (const name of textNames) {
      input(name).disabled = busy || session?.phase !== 'idle';
    }
  }

  async function refresh() {
    try {
      session = await api('/capture-visualizer/api/collection/status');
      text('connection-error', '');
    } catch (error) {
      session = null;
      text('connection-error', `采集控制连接失败：${error.message}`);
    }
    render();
  }
  // Schedule after completion: a slow exporter must not accumulate requests.
  async function poll() {
    await refresh();
    if (root.isConnected) setTimeout(poll, 1000);
  }
  async function perform(callback) {
    if (busy) return;
    busy = true;
    text('error', '');
    render();
    try { await callback(); }
    catch (error) { text('error', error.message); }
    finally { busy = false; render(); }
  }

  button('session-start').onclick = () => perform(async () => {
    const data = Object.fromEntries(textNames
      .map(name => [name, input(name).value.trim()]));
    for (const name of textNames) preferences[name] = input(name).value;
    preferences.raw_only = input('raw_only').checked;
    savePreferences();
    if (!data.dataset_name || !data.task_prompt) throw Error('请填写 dataset 名称与任务');
    data.camera_depth = input('camera_depth').checked;
    data.raw_only = input('raw_only').checked;
    const dataset = await api('/capture-visualizer/api/collection/dataset', data);
    const resuming = dataset.exists === true;
    const continuation = resuming
      ? `\n已有 ${dataset.episode_count} 段，下一段编号：${dataset.next_episode_index}。\n保留旧数据并追加新的 episode，不继续写入上一次未完成的段。${dataset.unfinished_episodes ? `\n${dataset.unfinished_episodes} 段未完成或中断，将原样保留。` : ''}`
      : '';
    const {nonce} = await api('/capture-visualizer/api/collection/challenge', {operation: 'start'});
    if (!confirm(`${resuming ? '续用已有录制输出，' : ''}启动真实 GR00T 控制服务并自动确认 deploy？\n输出：outputs/${data.subdirectory ? data.subdirectory + '/' : ''}${data.dataset_name}\n格式：${data.raw_only ? 'Raw 原始消息（离线转换）' : 'LeRobot'}${continuation}\n请确认机械支撑、现场监护和独立急停已就位。`)) return;
    data.resume_dataset = resuming;
    session = await api('/capture-visualizer/api/collection/start', {...data, nonce});
  });
  button('session-stop').onclick = () => perform(async () => {
    const {nonce} = await api('/capture-visualizer/api/collection/challenge', {operation: 'stop'});
    if (!confirm('强制停止自有 GR00T 控制服务？活动 episode 将被废弃，机器人可能失去主动支撑。网页 Stop 不是急停。')) return;
    session = await api('/capture-visualizer/api/collection/stop', {nonce});
  });
  input('camera_depth').onchange = () => {
    const desired = input('camera_depth').checked;
    return perform(async () => {
      if (!confirm('切换共享相机深度流？三路 RGB 会短暂中断，所有相机订阅者都会受影响。')) return;
      const camera = await api('/capture-visualizer/api/collection/camera-depth', {depth: desired});
      preferences.camera_depth = desired;
      savePreferences();
      if (session) session.camera = camera;
    });
  };
  input('raw_only').onchange = () => {
    preferences.raw_only = input('raw_only').checked;
    savePreferences();
    render();
  };
  for (const action of ['start', 'stop', 'discard']) {
    button(action).onclick = () => perform(async () => {
      // getRandomValues also works over the supported plain HTTP LAN entry.
      const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)),
        value => value.toString(16).padStart(2, '0')).join('');
      const result = await api(`/capture-visualizer/api/collection/episodes/${action}`, {request_id: requestId});
      if (result.error) throw Error(result.error);
      if (session?.exporter && result.state) session.exporter.state = result.state;
    });
  }
  poll();
  return {
    updateTelemetry(value) {
      telemetry = value;
      telemetryAt = performance.now();
      render();
    },
  };
}
