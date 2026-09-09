"use strict";
const $ = id => document.getElementById(id);
const cameras = ["cam_high", "cam_left_wrist", "cam_right_wrist"];
const actions = {
  execute: ["开始任务", "参考帧已就绪。请在原 UI 选择本轮指令并启动 VLA，再点击“已开始”。", "已开始"],
  stop: ["停止任务", "请在原 UI 停止 VLA 和机械臂当前动作，再点击“已停止”。", "已停止"],
  reset: ["恢复初始状态", "请在原系统让机械臂归位，完成后点击“已归位”。", "已归位"]
};
let status = null, connected = false, view = "live", sendingTarget = false, sendingAck = false;
let inputId = null, imageEpoch = 0, imageKey = "", lastLive = 0, displayedMonitor = null;
let historyId = null, history = [];
const percent = value => typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";

async function request(path, payload, method) {
  const response = await fetch(path, {method: method || (payload ? "POST" : "GET"),
    headers: payload ? {"Content-Type": "application/json"} : {},
    body: payload ? JSON.stringify(payload) : undefined, cache: "no-store", signal: AbortSignal.timeout(7000)});
  const result = await response.json();
  if (!response.ok || result.success !== true) throw Error(result.message || `HTTP ${response.status}`);
  return result.data;
}

function isReady() {
  return connected && status?.input && status.input.target === null && !status.active_execution_id
    && !status.resetting && !status.estop_latched && !status.pending;
}

function taskPreview() {
  const prompt = status?.input;
  if (!prompt) return;
  const target = $("target").value.trim() || prompt.last_target;
  $("task-preview").textContent = target ? prompt.instruction_template.replaceAll("{target}", target)
    : "输入目标后会显示本轮完整指令。";
}

function renderControls() {
  const pending = connected ? status?.pending : null;
  const label = pending && actions[pending.action];
  const ready = isReady();
  $("target").disabled = !ready || sendingTarget;
  $("submit-target").disabled = !ready || sendingTarget;
  $("submit-target").textContent = status?.input && status.input.target !== null ? "已提交，等待 loop" : "提交目标";
  if (status?.input && inputId !== status.input.request_id) {
    inputId = status.input.request_id;
    $("target").value = "";
    $("target-error").textContent = "";
    $("target").placeholder = status.input.last_target ? `留空复用 ${status.input.last_target}` : "例如 carrot / white cube";
  }
  $("phase").textContent = !connected ? "连接中断" : ready ? "ready" : pending ? label?.[0] || "人工操作"
    : status?.input?.target ? "准备任务" : status?.active_execution_id ? "执行中" : "等待 loop";
  $("target-hint").textContent = ready ? "提交后先准备参考帧，再提示人工开始。"
    : status?.active_execution_id || pending ? "本轮停止、归位完成后，可输入下一个目标。"
    : status?.input?.target ? "目标已提交，请等待参考帧准备。"
    : "等待使用 web 输入的 loop 进入 ready。终端模式可加 --input-source web 重启。";
  taskPreview();
  $("action-title").textContent = label ? label[0] : status?.active_execution_id ? "等待评分或准备参考帧" : "等待任务";
  $("instruction").textContent = pending?.instruction || status?.execution?.subtask || "—";
  $("action-help").textContent = label ? label[1] : "需要人工操作时，这里会显示提示。";
  $("ack").hidden = !label;
  $("ack").disabled = !connected || sendingAck;
  if (label) $("ack").textContent = label[2];
  for (const action of Object.keys(actions)) $("step-" + action).classList.toggle("current", pending?.action === action);
  $("execution-id").textContent = status?.execution ? `本轮 ${status.execution.execution_id}` : "";
}

$("target").addEventListener("input", taskPreview);
$("target-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!isReady() || sendingTarget) return;
  const requestId = status.input.request_id;
  sendingTarget = true; renderControls();
  try {
    await request("/manual/target", {request_id: requestId, target: $("target").value});
    if (status?.input?.request_id === requestId) status.input.target = $("target").value.trim() || status.input.last_target;
    $("target-error").textContent = "";
  } catch (error) { $("target-error").textContent = error.message; }
  finally { sendingTarget = false; renderControls(); }
});

$("ack").addEventListener("click", async () => {
  if (!connected || !status?.pending || sendingAck) return;
  const requestId = status.pending.request_id;
  sendingAck = true; renderControls();
  try {
    await request("/manual/ack", {request_id: requestId});
    $("action-error").textContent = "";
  } catch (error) { $("action-error").textContent = error.message; }
  finally { sendingAck = false; renderControls(); }
});

function clearCameras(message) {
  imageKey = "";
  for (const camera of cameras) {
    const canvas = $(camera);
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    canvas.parentElement.classList.remove("loaded");
    canvas.parentElement.querySelector(".empty-camera").textContent = message;
    $("note-" + camera).textContent = "";
  }
}

function changeView(next) {
  view = next; imageEpoch++; lastLive = 0;
  clearCameras("等待画面");
  $("view-live").setAttribute("aria-pressed", String(view === "live"));
  $("view-grm").setAttribute("aria-pressed", String(view === "grm"));
  $("show-bbox").disabled = view === "live";
  $("bbox-mode").disabled = view === "live";
  $("view-description").textContent = view === "live"
    ? "实时画面约每秒刷新；得分来自最近一次 GRM 推理。"
    : "画面、bbox 与下方得分来自同一轮推理；新的评分完成后一起更新。";
  $("camera-error").textContent = "";
  $("frame-info").textContent = "等待画面…";
  if (view === "grm") renderScore(null);
}
$("view-live").onclick = () => changeView("live");
$("view-grm").onclick = () => changeView("grm");
$("show-bbox").onchange = () => { imageEpoch++; imageKey = ""; };
$("bbox-mode").onchange = () => { imageEpoch++; imageKey = ""; };

function renderScore(monitor) {
  displayedMonitor = monitor;
  const result = monitor?.result || {};
  const hasScore = Number.isInteger(result.inference_step) && result.inference_step > 0;
  $("progress").textContent = hasScore ? percent(result.progress ?? monitor.progress) : "—";
  $("progress-bar").style.width = hasScore ? `${Math.max(0, Math.min(1, result.progress ?? monitor.progress)) * 100}%` : "0%";
  $("score-status").textContent = hasScore ? ({running: "最近结果：进行中", success: "任务成功", failed: "任务失败"}[result.status] || result.status || "已有评分") : "等待评分";
  $("monitor-error").textContent = monitor?.error || result.error || "";
  const body = $("mode-scores"); body.replaceChildren();
  for (const [mode, data] of Object.entries(result.modes || {})) {
    const row = document.createElement("tr");
    for (const value of [mode, percent(data.score), percent(data.progress)]) {
      const cell = document.createElement("td"); cell.textContent = value; row.appendChild(cell);
    }
    body.appendChild(row);
  }
  if (!body.childElementCount) {
    const row = document.createElement("tr"), cell = document.createElement("td");
    cell.colSpan = 3; cell.textContent = "暂无评分"; cell.className = "muted"; row.appendChild(cell); body.appendChild(row);
  }
  $("branches").hidden = !result.branches;
  $("progress-label").textContent = result.branches ? "Steering 融合进度" : "融合进度";
  $("baseline-progress").textContent = percent(result.branches?.baseline?.progress);
  $("branch-difference").textContent = result.comparison ? `${percent(result.comparison.difference)} / 阈值 ${percent(result.comparison.threshold)}` : "—";
  updateAge();
}

function updateAge() {
  const result = displayedMonitor?.result;
  if (!result?.inference_step) { $("score-info").textContent = "等待首次推理或对应评分画面。"; return; }
  const age = Math.max(0, Date.now() / 1000 - (result.inference_updated_at || displayedMonitor.updated_at));
  $("score-info").textContent = `第 ${result.inference_step} 次评分 · ${age.toFixed(0)} 秒前更新`
    + (typeof result.latency_s === "number" ? ` · 耗时 ${result.latency_s.toFixed(1)} 秒` : "");
}

function updateHistory(monitor) {
  if (historyId !== monitor?.execution_id) { historyId = monitor?.execution_id; history = []; }
  const result = monitor?.result;
  if (result?.inference_step && history.at(-1)?.step !== result.inference_step) {
    history.push({step: result.inference_step, value: result.progress ?? monitor.progress,
      baseline: result.branches?.baseline?.progress});
    history = history.slice(-60);
  }
  const canvas = $("history"), ctx = canvas.getContext("2d"), w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "#e5eded"; ctx.lineWidth = 1;
  for (const y of [12, h/2, h-12]) { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(w,y); ctx.stroke(); }
  for (const [key,color] of [["value","#087f80"],["baseline","#b48649"]]) {
    if (!history.some(row => typeof row[key] === "number")) continue;
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 3; ctx.beginPath();
    history.forEach((row,i) => {
      const x = 8 + i / Math.max(1, history.length-1) * (w-16), y = h-12-Math.max(0, Math.min(1,row[key]))*(h-24);
      if (i === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    }); ctx.stroke();
    if (history.length === 1) { ctx.beginPath(); ctx.arc(8,h-12-history[0][key]*(h-24),4,0,Math.PI*2); ctx.fill(); }
  }
  $("history-info").textContent = history.length ? `融合进度趋势 · 第 ${history[0].step}–${history.at(-1).step} 次评分`
    + (history.at(-1).baseline !== undefined ? " · 绿色 Steering / 棕色 Baseline" : "") : "融合进度趋势 · 最近 60 次评分";
}

async function bitmap(path) {
  const response = await fetch(path, {signal: AbortSignal.timeout(7000)});
  if (!response.ok) throw Error(`画面读取失败（HTTP ${response.status}）`);
  return createImageBitmap(await response.blob());
}

function drawCamera(camera, image, monitor, mode) {
  const canvas = $(camera), ctx = canvas.getContext("2d");
  canvas.width = image.width; canvas.height = image.height;
  ctx.drawImage(image,0,0); canvas.parentElement.classList.add("loaded");
  if (view === "live") { $("note-" + camera).textContent = "实时原图 · 不叠加其他时刻的 bbox"; return; }
  const steering = monitor.result.modes?.[mode]?.steering || {};
  const detection = steering.grounding?.["after_" + camera];
  let note = !detection ? (steering.degraded ? `本轮检测降级：${steering.reason || "不可用"}` : "该模式未对这一视角做 SAM3 检测")
    : detection.selection_status === "ambiguous" ? "检测到多个相近候选，未选定 bbox"
    : !detection.selected ? "未检测到可用目标 bbox" : "";
  const selected = detection?.selected, box = selected?.bbox;
  if (box) {
    const size = detection.image_size;
    const valid = Array.isArray(box) && box.length === 4 && box.every(Number.isFinite)
      && box[2] > box[0] && box[3] > box[1]
      && (!size || (size[0] === image.width && size[1] === image.height));
    if (!valid) note = "bbox 与当前图像尺寸不匹配，未绘制";
    else {
      const source = detection.source === "sam3_tracker" ? "SAM3 跟踪存在分数" : "SAM3 检测分数";
      note = `${selected.query || "目标"}${typeof selected.score === "number" ? " · " + source + " " + percent(selected.score) : ""}`
        + (steering.applied ? " · steering 已应用" : " · 本轮未应用 steering");
      if ($("show-bbox").checked) {
        const line = Math.max(2, image.width / 220), font = Math.max(13,image.width / 35);
        ctx.lineWidth = line; ctx.strokeStyle = "#34edb5"; ctx.strokeRect(box[0],box[1],box[2]-box[0],box[3]-box[1]);
        const label = selected.query || "SAM3"; ctx.font = `${font}px sans-serif`;
        const x = Math.max(0,Math.min(box[0],image.width-ctx.measureText(label).width-12));
        const y = Math.max(font+8,box[1]); ctx.fillStyle = "#123f38";
        ctx.fillRect(x,y-font-8,ctx.measureText(label).width+12,font+8); ctx.fillStyle = "#bfffe8"; ctx.fillText(label,x+6,y-5);
      }
    }
  }
  $("note-" + camera).textContent = note;
}

async function refreshCameras() {
  const epoch = imageEpoch, monitor = status?.monitor;
  let images = [];
  try {
    let paths, key, info, mode = $("bbox-mode").value;
    if (view === "live") {
      if (Date.now() - lastLive < 1000) return;
      lastLive = Date.now();
      const metadata = await request("/observations/latest/metadata");
      paths = cameras.map(camera => metadata.binary_endpoints?.[camera]);
      if (paths.some(path => typeof path !== "string" || !path.startsWith("/observations/"))) throw Error("相机缺少三视角图像地址");
      key = `live:${metadata.frame_id}`;
      info = `相机响应 ${new Date().toLocaleTimeString()} · ${metadata.timestamp == null ? "上游无采集时间戳" : "源时间戳 " + metadata.timestamp}`;
    } else {
      if (!connected) return;
      const result = monitor?.result, frameId = result?.preview?.frame_set_id;
      if (!frameId) {
        clearCameras(result?.inference_step ? "Monitor 尚未提供评分图像" : "等待首次评分");
        $("frame-info").textContent = result?.inference_step ? "请更新并重启服务器 Monitor，以提供与得分对应的原图。" : "首次评分完成后展示该轮原图与 SAM3 bbox。";
        renderScore(monitor); return;
      }
      if (!/^[a-f0-9]{32}$/.test(frameId)) throw Error("评分图像标识无效");
      const modes = Object.keys(result.modes || {});
      for (const option of $("bbox-mode").options) option.disabled = !modes.includes(option.value);
      if (!modes.includes(mode)) { mode = modes[0] || "forward"; $("bbox-mode").value = mode; }
      key = `grm:${frameId}:${mode}:${$("show-bbox").checked}`;
      paths = cameras.map(camera => `/manual/monitor/frames/${frameId}/${camera}.png`);
      info = `第 ${result.inference_step} 次评分 · ${mode} 的 SAM3 结果 · 当前三视角原图`;
    }
    if (epoch !== imageEpoch) return;
    if (key !== imageKey) {
      const loaded = await Promise.allSettled(paths.map(bitmap));
      images = loaded.filter(item => item.status === "fulfilled").map(item => item.value);
      const failed = loaded.find(item => item.status === "rejected");
      if (failed) throw failed.reason;
      if (epoch !== imageEpoch) return;
      cameras.forEach((camera,i) => drawCamera(camera,images[i],monitor,mode));
      imageKey = key;
    }
    if (epoch !== imageEpoch) return;
    if (view === "grm") { renderScore(monitor); updateHistory(monitor); }
    $("frame-info").textContent = info; $("camera-error").textContent = "";
  } catch (error) {
    if (epoch === imageEpoch) {
      clearCameras("画面不可用"); $("camera-error").textContent = error.message;
      if (view === "grm") renderScore(null);
    }
  } finally { images.forEach(image => image.close()); }
}

async function pollStatus() {
  try {
    const next = await request("/manual/status");
    if (next.execution?.execution_id !== status?.execution?.execution_id) {
      imageEpoch++; imageKey = "";
      if (view === "grm") { clearCameras("等待本轮评分"); renderScore(null); updateHistory(next.monitor); }
    }
    status = next; connected = true;
    $("connection").textContent = "Runtime 已连接"; $("connection-dot").className = "dot online";
    if (view === "live") { renderScore(status.monitor); updateHistory(status.monitor); }
    if (status.monitor?.error) $("monitor-error").textContent = status.monitor.error;
  } catch (error) {
    connected = false; $("connection").textContent = "连接中断，正在重试"; $("connection-dot").className = "dot offline";
    $("monitor-error").textContent = error.message;
  }
  renderControls(); setTimeout(pollStatus,500);
}
async function pollCameras() { await refreshCameras(); setTimeout(pollCameras,350); }
setInterval(updateAge,1000);
pollStatus(); pollCameras();
