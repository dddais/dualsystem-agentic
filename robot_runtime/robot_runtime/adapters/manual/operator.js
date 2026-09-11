"use strict";
const $ = id => document.getElementById(id);
const cameras = ["cam_high", "cam_left_wrist", "cam_right_wrist"];
const actions = {
  execute: ["开始任务", "参考帧已就绪。请在原 UI 选择本轮指令并启动 VLA，再点击“已开始”。", "已开始"],
  stop: ["停止任务", "请在原 UI 停止 VLA 和机械臂当前动作，再点击“已停止”。", "已停止"],
  reset: ["恢复初始状态", "请在原系统让机械臂归位，完成后点击“已归位”。", "已归位"]
};
const bridgeActions = {
  execute: ["等待启动", "参考帧已就绪。在 VLA 控制中选择自主运行，设置本轮指令并启动评分。"],
  stop: ["等待停止", "在 VLA 控制中选择空闲，停止本轮执行与评分。"],
  reset: ["等待归位", "在 VLA 控制中选择 Homing，完成后进入下一轮。"],
  recover: ["等待恢复", "选择 Homing 归位，或遥操作调整。调整完成后切空闲，进入下一轮。"]
};
function loopState() {
  const p = status?.pending;
  if (p?.phase === "adjusting") return ["调整中", "通过遥操作调整机械臂和场景，完成后选择空闲；本阶段不运行 GRM。"];
  if (p?.phase === "finishing") return ["结束调整", "正在切回空闲并清理动作队列，请等待完成。"];
  if (p && p.phase !== "waiting") return [p.action === "stop" ? "停止中" : p.choice === "homing" ? "归位中" : p.choice === "teleop" ? "进入调整" : "启动中", "正在执行命令，请等待完成。"];
  if (p) return bridgeActions[p.action];
  if (status?.estop_latched) return ["软件停止已锁存", "完成 Homing 归位后才能开始下一轮。"];
  if (status?.active_execution_id) return status.execution?.driver_result?.executed
    ? ["执行中", "正在运行本轮指令并评分。选择空闲可以提前结束本轮。"]
    : ["准备参考帧", "已请求开始；Monitor 就绪后将启动本轮 VLA。"];
  if (status?.recovery_required) return ["已停止", "等待 loop 进入恢复阶段，然后选择归位或遥操作调整。"];
  if (status?.input?.task) return ["准备启动", "已请求开始，等待 loop 准备本轮 Monitor。"];
  return ["等待任务", "保存指令后按自主运行（A）开始；后续循环可直接复用。"];
}

let bridgeStatus = null, bridgeConnected = false, bridgeSending = false, sendingEstop = false, operationId = null;
let status = null, connected = false, view = "live", sendingTarget = false, sendingAck = false;
let inputId = null, imageEpoch = 0, imageKey = "", lastLive = 0, displayedMonitor = null;
let historyId = null, history = [];
let inputMode = "template";
let draftDirty = false, draftRevision = null, draftTemplates = null, draftTemplatesRevision = null;
const persistentInstruction = () => !!status?.instruction_editor?.enabled;
function loadInstructionDraft() {
  const editor = status.instruction_editor, task = editor.task;
  if (draftDirty || sendingTarget || (editor.revision === draftRevision && editor.templates_revision === draftTemplatesRevision)) return;
  draftRevision = editor.revision;
  draftTemplates = editor.instruction_templates;
  draftTemplatesRevision = editor.templates_revision;
  inputMode = task?.mode || "template";
  $("target").value = task?.target || "";
  $("full-instruction").value = task?.mode === "instruction" ? task.instruction : "";
  $("target-queries").value = (task?.target_queries || []).join("\n");
  selectOptions("instruction-template", Object.entries(draftTemplates).map(([name]) => [name, name === "default" ? "默认模板" : name]), task?.template_id || "default");
}
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
  return connected && status?.input && status.input.target === null && !status.input.task && !status.active_execution_id
    && !status.resetting && !status.estop_latched && !status.recovery_required && !status.pending;
}

function taskPreview() {
  const persistent = persistentInstruction();
  const prompt = persistent ? {instruction_templates: draftTemplates, input_modes: ["template", "instruction"], last_target: ""} : status?.input;
  if (!prompt) return;
  if (!persistent && prompt.task) { $("task-preview").textContent = prompt.task.instruction; return; }
  if (inputMode === "instruction" && prompt.input_modes?.includes("instruction")) {
    $("task-preview").textContent = $("full-instruction").value.trim() || "输入完整指令后，将原样用于本轮任务。";
    return;
  }
  const target = $("target").value.trim() || prompt.last_target;
  const template = prompt.instruction_templates?.[$("instruction-template").value] || prompt.instruction_template;
  $("task-preview").textContent = target ? template.replace(/\{\{|\}\}|\{target\}/g, token => token === "{target}" ? target : token[0])
    : "输入目标后会显示本轮完整指令。";
}

function renderControls() {
  const integrated = status?.control_mode === "bridge";
  const persistent = persistentInstruction();
  if (persistent) loadInstructionDraft();
  const editable = persistent ? connected : isReady();
  const pending = connected ? status?.pending : null;
  const label = pending && (integrated ? bridgeActions : actions)[pending.action];
  const busy = integrated && pending && pending.phase !== "waiting";
  const ready = isReady();
  const advanced = persistent || status?.input?.input_modes?.includes("instruction");
  const submitted = status?.input && (status.input.task || status.input.target !== null);
  $("target").disabled = !editable || sendingTarget;
  $("submit-target").disabled = !editable || sendingTarget;
  $("submit-target").textContent = persistent ? "保存指令" : submitted ? "已提交，等待 loop" : advanced ? "提交任务" : "提交目标";
  if (!persistent && status?.input && inputId !== status.input.request_id) {
    inputId = status.input.request_id;
    $("target").value = "";
    $("full-instruction").value = "";
    $("target-queries").value = "";
    $("target-error").textContent = "";
    $("target").placeholder = status.input.last_target ? `留空复用 ${status.input.last_target}` : "例如 carrot / white cube";
  }
  if (!persistent && status?.input?.task) inputMode = status.input.task.mode;
  $("input-modes").hidden = !advanced;
  $("template-choice").hidden = !advanced;
  $("template-fields").hidden = advanced && inputMode === "instruction";
  $("instruction-fields").hidden = !advanced || inputMode !== "instruction";
  $("instruction-destination").hidden = !advanced || !integrated;
  $("mode-template").setAttribute("aria-pressed", String(inputMode === "template"));
  $("mode-instruction").setAttribute("aria-pressed", String(inputMode === "instruction"));
  for (const id of ["mode-template", "mode-instruction", "instruction-template", "full-instruction", "target-queries"]) $(id).disabled = !editable || sendingTarget;
  if (advanced && !persistent) {
    const templates = status.input.instruction_templates;
    const previous = status.input.task?.template_id || $("instruction-template").value;
    const selected = Object.hasOwn(templates, previous) ? previous : "default";
    selectOptions("instruction-template", Object.entries(templates).map(([name]) => [name, name === "default" ? "默认模板" : name]), selected);
  }
  $("phase").textContent = !connected ? "连接中断" : ready ? "ready" : pending ? label?.[0] || "人工操作"
    : submitted ? "准备任务" : status?.active_execution_id ? "执行中" : "等待 loop";
  $("target-hint").textContent = ready ? "提交后先准备参考帧，再提示人工开始。"
    : status?.active_execution_id || pending ? "本轮停止并完成归位或调整后，可输入下一个目标。"
    : submitted ? "任务已提交，请等待参考帧准备。"
    : "等待使用 web 输入的 loop 进入 ready。终端模式可加 --input-source web 重启。";
  $("task-heading").textContent = persistent ? "任务指令" : "目标任务";
  $("saved-instruction-panel").hidden = !persistent;
  $("discard-instruction").hidden = !persistent;
  $("discard-instruction").disabled = !draftDirty || sendingTarget;
  $("instruction-draft-status").hidden = !persistent;
  if (persistent) {
    $("saved-instruction").textContent = status.instruction_editor.task?.instruction || "尚未保存指令";
    $("instruction-draft-status").textContent = draftDirty ? "有未保存的编辑；保存后才会用于下一次开始。" : status.instruction_editor.task ? "已保存，后续循环持续复用。" : "填写并保存一条指令。";
    $("target-hint").textContent = status.active_execution_id || status.pending || status.input?.task
      ? "本轮指令已锁定。现在保存的修改用于下次开始，本轮 VLA 和 VLM 保持一致。"
      : isReady() ? "保存不会启动机器人。按自主运行（A）开始；每轮结束后无需重复提交。"
      : "指令保存在 Runtime 中；等待 loop 进入 ready 后即可开始。";
  }
  taskPreview();
  $("action-title").textContent = label ? label[0] : status?.active_execution_id ? "等待评分或准备参考帧" : "等待任务";
  $("instruction").textContent = pending?.instruction || status?.input?.task?.instruction || status?.execution?.subtask || "—";
  $("action-help").textContent = busy ? "操作已提交，正在执行命令并等待完成…" : label ? label[1] : "需要人工操作时，这里会显示提示。";
  $("ack").hidden = integrated || !label;
  $("ack").disabled = !connected || sendingAck || busy || (integrated && pending?.action === "execute"
    && (!bridgeConnected || !!bridgeStatus?.selection_error || status.estop_latched));
  if (label && !integrated) $("ack").textContent = label[2];
  $("runtime-mode").textContent = integrated ? "manual_bridge" : "manual";
  $("operator-heading").textContent = integrated ? "本轮状态" : "人工操作";
  $("operator-mode").textContent = integrated ? "由 VLA 控制驱动状态切换" : "原 VLA 控制界面操作";
  $("operator-hint").textContent = integrated ? "本区域只显示 loop 状态；使用下方 VLA 控制操作。" : "在原有控制界面完成动作后，再点击确认。";
  $("bridge-panel").hidden = !integrated;
  $("bridge-shortcuts").hidden = !integrated;
  $("ack").dataset.shortcut = integrated && pending?.action === "reset" ? "H / Space" : "Space";
  $("ack").setAttribute("aria-keyshortcuts", integrated && pending?.action === "reset" ? "h Space" : "Space");
  $("bridge-estop").hidden = !integrated;
  $("bridge-estop").disabled = !connected || sendingEstop;
  if (integrated && status.last_operation && operationId !== status.last_operation.request_id) {
    operationId = status.last_operation.request_id;
    $("action-error").textContent = status.last_operation.error || "";
  }
  $("manual-space-help").hidden = integrated;
  if (integrated) {
    const [title, help] = loopState();
    $("action-title").textContent = title;
    $("action-help").textContent = help;
    if (!ready) $("phase").textContent = title;
  }
  for (const action of Object.keys(actions)) $("step-" + action).classList.toggle("current", pending?.action === action || (action === "reset" && pending?.action === "recover"));
  $("execution-id").textContent = status?.execution ? `本轮 ${status.execution.execution_id}` : "";
  renderBridge();
}

function editInstruction() { if (persistentInstruction()) draftDirty = true; renderControls(); }
$("target").addEventListener("input", editInstruction);
$("full-instruction").addEventListener("input", editInstruction);
$("target-queries").addEventListener("input", editInstruction);
$("instruction-template").addEventListener("change", editInstruction);
$("mode-template").onclick = () => { inputMode = "template"; editInstruction(); };
$("mode-instruction").onclick = () => { inputMode = "instruction"; editInstruction(); };
$("discard-instruction").onclick = () => { draftDirty = false; draftRevision = null; $("target-error").textContent = ""; renderControls(); };
$("target-form").addEventListener("submit", async event => {
  event.preventDefault();
  const persistent = persistentInstruction();
  if (!(persistent ? connected : isReady()) || sendingTarget) return;
  const requestId = status.input?.request_id;
  const advanced = persistent || status.input.input_modes?.includes("instruction");
  const payload = persistent ? {revision: draftRevision, templates_revision: draftTemplatesRevision} : {request_id: requestId};
  if (advanced) {
    payload.mode = inputMode;
    if (inputMode === "template") {
      payload.template_id = $("instruction-template").value;
      payload.target = $("target").value;
    } else {
      payload.instruction = $("full-instruction").value;
      const queries = $("target-queries").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
      if (queries.length) payload.target_queries = queries;
    }
  } else payload.target = $("target").value;
  sendingTarget = true; renderControls();
  try {
    const result = await request(persistent ? "/manual/instruction" : advanced ? "/manual/task" : "/manual/target", payload);
    if (persistent) {
      status.instruction_editor = {enabled: true, ...result};
      draftDirty = false; draftRevision = null;
    } else if (status?.input?.request_id === requestId) {
      if (advanced) status.input.task = result.task;
      else status.input.target = result.target;
    }
    $("target-error").textContent = "";
  } catch (error) { $("target-error").textContent = error.message; }
  finally { sendingTarget = false; renderControls(); }
});

$("ack").addEventListener("click", async () => {
  if (!connected || !status?.pending || sendingAck) return;
  const requestId = status.pending.request_id;
  sendingAck = true; renderControls();
  try {
    const integrated = status?.control_mode === "bridge";
    await request(integrated ? "/manual/action" : "/manual/ack", {request_id: requestId});
    if (integrated && status?.pending?.request_id === requestId) status.pending.phase = "queued";
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
  const colors = getComputedStyle(document.documentElement);
  ctx.strokeStyle = colors.getPropertyValue("--line").trim(); ctx.lineWidth = 1;
  for (const y of [12, h/2, h-12]) { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(w,y); ctx.stroke(); }
  for (const [key,color] of [["value",colors.getPropertyValue("--accent").trim()],
                           ["baseline",colors.getPropertyValue("--baseline").trim()]]) {
    if (!history.some(row => typeof row[key] === "number")) continue;
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 3; ctx.beginPath();
    history.forEach((row,i) => {
      const x = 8 + i / Math.max(1, history.length-1) * (w-16), y = h-12-Math.max(0, Math.min(1,row[key]))*(h-24);
      if (i === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    }); ctx.stroke();
    if (history.length === 1) { ctx.beginPath(); ctx.arc(8,h-12-history[0][key]*(h-24),4,0,Math.PI*2); ctx.fill(); }
  }
  $("history-info").textContent = history.length ? `融合进度趋势 · 第 ${history[0].step}–${history.at(-1).step} 次评分`
    + (history.at(-1).baseline !== undefined ? " · 蓝色 Steering / 橙色 Baseline" : "") : "融合进度趋势 · 最近 60 次评分";
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

function selectOptions(id, entries, selected) {
  const element = $(id), key = JSON.stringify(entries);
  if (element.dataset.options !== key) {
    element.replaceChildren(...entries.map(([value, label]) => new Option(label, String(value))));
    element.dataset.options = key; delete element.dataset.dirty;
  }
  if (!element.dataset.dirty && document.activeElement !== element) element.value = String(selected ?? "");
}

function renderBridge() {
  if (status?.control_mode !== "bridge") return;
  const s = bridgeStatus?.state || {}, supported = s.actions || [];
  const available = connected && bridgeConnected && !bridgeSending && !status.pending && !status.resetting && !status.estop_latched;
  const allowed = available ? bridgeStatus.allowed_actions || [] : [];
  $("bridge-connection").textContent = bridgeConnected ? "Scheduler 已连接" : "Scheduler 未连接";
  $("bridge-summary").textContent = `${s.scheduler || "Scheduler"} · 迭代 ${s.iteration ?? "—"} · ${s.mode || (s.single_step ? "单步 / 暂停" : "连续运行")}${s.homing_pending ? " · 归位中" : ""}`;
  $("bridge-selection").textContent = bridgeStatus?.selection_error || (bridgeStatus?.selection
    ? `本轮启动将使用：${bridgeStatus.selection.index == null ? "" : bridgeStatus.selection.index + ". "}${bridgeStatus.selection.prompt}`
    : "本轮提交的完整指令将在开始时设置到 VLA。");
  for (const row of document.querySelectorAll("[data-support]")) row.hidden = !supported.includes(row.dataset.support);
  for (const group of document.querySelectorAll("[data-support-any]")) {
    group.hidden = !group.dataset.supportAny.split(" ").some(action => supported.includes(action));
  }
  const digitTarget = bridgeDigitMode() === "phase" ? "Phase" : "Prompt";
  $("bridge-digit-status").textContent = `数字键 0–9 → ${digitTarget}`
    + (allowed.includes(digitTarget === "Phase" ? "set_phase" : "set_prompt") ? "" : "（当前不可设置）");
  selectOptions("bridge-prompt", (s.prompts || []).map((prompt, i) => [i, `${i}. ${prompt}`]), (s.prompts || []).indexOf(s.prompt));
  selectOptions("bridge-phase", Array.from({length: 10}, (_, i) => [i, `${i} ${s.phase_labels?.[i] || ""}`]), s.phase);
  const lifecycle = connected && bridgeConnected && !bridgeSending ? [...(status.vla_controls || [])] : [];
  if (connected && bridgeConnected && !bridgeSending && isReady() && status.instruction_editor?.task
      && status.input?.input_modes?.includes("instruction")) lifecycle.push("autonomous");
  for (const button of document.querySelectorAll("#bridge-modes [data-control]")) {
    const control = button.dataset.control;
    const supportedControl = control !== "teleop" || (supported.includes("set_mode") && (s.modes || []).includes("teleop"));
    button.disabled = !supportedControl || !lifecycle.includes(control)
      || (control === "autonomous" && (draftDirty || sendingTarget || (!isReady() && !!bridgeStatus?.selection_error)));
    button.setAttribute("aria-pressed", String(control === s.mode));
  }
  $("bridge-auto-stop").textContent = status.auto_stop
    ? "自动停止已启用：loop 请求停止时自动切空闲；恢复方式仍由你选择。"
    : "手动停止：loop 请求停止后，选择空闲继续。";
  $("bridge-record").textContent = s.recording ? "停止录制（录制中）" : "开始录制";
  $("bridge-record").setAttribute("aria-pressed", String(!!s.recording));
  const recording = status.recording || {};
  const recordingStates = {idle: "等待录制", recording: "正在记录", stopping: "正在停止视频录制", finalizing: "正在补齐进度数据",
    complete: "记录已保存", incomplete: "记录不完整", interrupted: "记录已中断"};
  $("progress-recording-status").textContent = recording.enabled
    ? `视频 + GRM 进度 · ${recordingStates[recording.state] || recording.state} · ${recording.record_count || 0} 条评分`
    : "GRM 进度录制未启用；需启用 Runtime recording 配置。";
  $("progress-recording-path").textContent = recording.directory ? `Runtime 保存位置：${recording.directory}` : "";
  $("progress-recording-error").textContent = [s.recording_info?.error, recording.error, ...(recording.warnings || [])].filter(Boolean).join(" · ");
  $("progress-recording-download").hidden = !recording.download_ready;
  if (recording.download_ready) $("progress-recording-download").href = `/manual/recordings/${encodeURIComponent(recording.recording_id)}/download`;
  $("bridge-lock").textContent = s.phase_locked ? "解锁 phase" : "锁定 phase";
  $("bridge-lock").setAttribute("aria-pressed", String(!!s.phase_locked));
  $("bridge-latency").textContent = s.latency_step ?? "—";
  $("bridge-move").textContent = s.move_steps ?? "—";
  $("bridge-single-step").textContent = s.single_step ? "切到连续运行" : "切到单步 / 暂停";
  for (const [id, value] of [["bridge-person", s.person], ["bridge-scale", s.gripper_map?.scale], ["bridge-offset", s.gripper_map?.offset]]) {
    if (!$(id).dataset.dirty && document.activeElement !== $(id)) $(id).value = value ?? "";
  }
  for (const button of document.querySelectorAll("#bridge-controls [data-action]")) {
    button.disabled = !allowed.includes(button.dataset.action) || !supported.includes(button.dataset.action)
      || (button.dataset.action === "toggle_recording" && !!s.recording_info?.busy)
      || (button.dataset.action === "step" && !s.single_step);
  }
  for (const input of document.querySelectorAll("#bridge-controls input, #bridge-controls select")) {
    input.disabled = !allowed.includes(input.closest("[data-support]").dataset.support);
  }
}

$("bridge-modes").addEventListener("click", async event => {
  const button = event.target.closest("button[data-control]");
  if (!button || button.disabled || bridgeSending) return;
  const control = button.dataset.control;
  const args = {execution_id: status.active_execution_id || status.execution?.execution_id};
  if (control === "autonomous" && isReady() && status.instruction_editor?.task) {
    args.input_request_id = status.input.request_id;
    args.instruction_revision = status.instruction_editor.revision;
  } else if (status.pending) args.request_id = status.pending.request_id;
  if (control !== "homing") args.mode = control;
  bridgeSending = true; renderControls();
  try {
    await request("/manual/bridge/action", {name: control === "homing" ? "homing" : "set_mode", args});
    $("bridge-error").textContent = "";
    if (status.pending) {
      status.pending.phase = control === "idle" && status.pending.phase === "adjusting" ? "finishing" : "queued";
      status.pending.choice = status.pending.choice || control;
    }
    status.vla_controls = [];
    if (args.input_request_id && status.input) status.input.task = {...status.instruction_editor.task};
  } catch (error) { $("bridge-error").textContent = error.message; }
  finally { bridgeSending = false; renderControls(); }
});

for (const input of document.querySelectorAll("#bridge-controls input, #bridge-controls select")) {
  input.addEventListener("input", () => { input.dataset.dirty = "true"; });
}
$("bridge-controls").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled || bridgeSending) return;
  const name = button.dataset.action;
  let args = {}, edited = [];
  if (button.dataset.delta) args = {delta: Number(button.dataset.delta)};
  if (name === "set_mode") args = {mode: button.dataset.mode};
  if (name === "set_prompt") { args = {index: Number($("bridge-prompt").value)}; edited = ["bridge-prompt"]; }
  if (name === "set_phase") { args = {phase: Number($("bridge-phase").value)}; edited = ["bridge-phase"]; }
  if (name === "set_person") { args = {person: $("bridge-person").value}; edited = ["bridge-person"]; }
  if (name === "set_gripper_map") {
    edited = ["bridge-scale", "bridge-offset"];
    if (edited.some(id => !$(id).value.trim() || !Number.isFinite(Number($(id).value)))) {
      $("bridge-error").textContent = "夹爪映射必须填写有效数值。"; return;
    }
    args = {scale: Number($("bridge-scale").value), offset: Number($("bridge-offset").value)};
  }
  bridgeSending = true; renderBridge();
  try {
    await request("/manual/bridge/action", {name, args});
    edited.forEach(id => { delete $(id).dataset.dirty; });
    $("bridge-error").textContent = "";
    await refreshBridgeStatus();
  } catch (error) { $("bridge-error").textContent = error.message; }
  finally { bridgeSending = false; renderBridge(); }
});

$("bridge-estop").addEventListener("click", async () => {
  if (!connected || sendingEstop) return;
  sendingEstop = true; renderControls();
  try {
    const response = await fetch("/control/emergency_stop", {method: "POST"});
    const result = await response.json();
    if (!response.ok || !result.success || !result.data?.emergency_stop) throw Error(result.message || "软件停止失败");
    $("estop-error").textContent = "";
  } catch (error) { $("estop-error").textContent = error.message; }
  finally { sendingEstop = false; renderControls(); }
});

async function refreshBridgeLog() {
  if (!bridgeConnected || !$("bridge-debug").open) return;
  try {
    const result = await request(`/manual/bridge/log?target=${encodeURIComponent($("bridge-log-target").value)}&lines=200`);
    $("bridge-log").textContent = (result.lines || []).join("\n") || "暂无日志";
  } catch (error) { $("bridge-log").textContent = error.message; }
}
$("bridge-debug").addEventListener("toggle", refreshBridgeLog);
$("bridge-log-target").addEventListener("change", refreshBridgeLog);
$("bridge-refresh-log").addEventListener("click", refreshBridgeLog);

async function refreshBridgeStatus() {
  try {
    bridgeStatus = await request("/manual/bridge/status");
    if (!bridgeConnected) $("bridge-error").textContent = "";
    bridgeConnected = true;
  } catch (error) {
    bridgeConnected = false; $("bridge-error").textContent = error.message;
  }
  renderControls();
}
async function pollBridge() {
  if (connected && status?.control_mode === "bridge") await refreshBridgeStatus();
  setTimeout(pollBridge, 1000);
}

function bridgeDigitMode() {
  const s = bridgeStatus?.state || {};
  return s.digit_mode || ((s.actions || []).includes("set_phase") ? "phase" : "prompt");
}

function shortcutAvailable(button) {
  return button && !button.disabled && !button.closest("[hidden]") && button.getClientRects().length > 0;
}

// Use the same buttons and lifecycle gates as mouse clicks. In particular,
// digits send explicit set_phase/set_prompt, never a raw press_digit that
// could change the running task's prompt after a scheduler mode switch.
document.addEventListener("keydown", event => {
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229
      || event.ctrlKey || event.metaKey || event.altKey) return;
  const target = event.target;
  if (target instanceof Element && (target.isContentEditable
      || target.closest("input, textarea, select, [role=textbox]"))) return;
  const key = event.key.toLowerCase();
  if (event.repeat) {
    // Native Enter on a focused button also repeats unless its default action
    // is cancelled. Keep normal held-key typing inside editable fields above.
    if (key === "enter" || key === " ") event.preventDefault();
    return;
  }
  // Native activation must not also run the global Enter/Space shortcut.
  if ((key === "enter" || key === " ") && target instanceof Element
      && target.closest("button, a, summary, [role=button]")) return;
  let button = null;
  if (key === " " && status?.control_mode !== "bridge") button = $("ack");
  else if (status?.control_mode === "bridge") {
    if (key === "h") button = $("bridge-home");
    else if (/^[0-9]$/.test(key)) {
      const phase = bridgeDigitMode() === "phase";
      const select = $(phase ? "bridge-phase" : "bridge-prompt");
      button = $(phase ? "bridge-set-phase" : "bridge-set-prompt");
      if (!shortcutAvailable(button) || select.disabled || !Array.from(select.options).some(option => option.value === key)) return;
      select.value = key; select.dataset.dirty = "true";
    } else {
      const selector = {
        r: "#bridge-record", l: "#bridge-lock", p: "#bridge-digit-mode",
        s: "#bridge-single-step", enter: "#bridge-step",
        i: '#bridge-modes [data-mode="idle"]', t: '#bridge-modes [data-mode="teleop"]', a: '#bridge-modes [data-mode="autonomous"]',
        "[": '#bridge-controls [data-action="adjust_latency"][data-delta="-1"]',
        "]": '#bridge-controls [data-action="adjust_latency"][data-delta="1"]'
      }[key];
      if (selector) button = document.querySelector(selector);
    }
  }
  if (!shortcutAvailable(button)) return;
  event.preventDefault();
  button.click();
});
setInterval(updateAge,1000);
pollStatus(); pollCameras(); pollBridge();
