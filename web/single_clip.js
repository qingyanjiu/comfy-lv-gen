import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_TYPE = "H3SingleClipDirector";
const POLLERS = new Map();

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function ensureStyles() {
  if (document.getElementById("h3-single-clip-style")) return;
  const style = document.createElement("style");
  style.id = "h3-single-clip-style";
  style.textContent = `
    .h3-single-panel { width: 100%; height: 330px; box-sizing: border-box; padding: 10px; color: #ddd; background: #15191d; border-top: 1px solid #343b43; font-family: sans-serif; overflow-y: auto; }
    .h3-single-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .h3-single-state { flex: 1; font-size: 14px; font-weight: 650; }
    .h3-single-refresh { border: 1px solid #555; background: #282d33; color: #eee; border-radius: 4px; padding: 3px 9px; cursor: pointer; }
    .h3-single-video { display: block; width: 100%; height: 245px; box-sizing: border-box; object-fit: contain; background: #000; border: 1px solid #333; border-radius: 5px; }
    .h3-single-meta { color: #909aa5; font-size: 11px; margin-top: 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .h3-single-error { color: #ffb4b4; white-space: pre-wrap; font-size: 12px; margin-top: 6px; }
    .h3-single-empty { height: 245px; display: grid; place-items: center; color: #77818b; border: 1px dashed #39414a; border-radius: 5px; }
  `;
  document.head.appendChild(style);
}

function stopPolling(node) {
  const timer = POLLERS.get(node.id);
  if (timer) clearInterval(timer);
  POLLERS.delete(node.id);
}

async function refresh(node, ui, jobId) {
  if (!jobId) return;
  try {
    const response = await api.fetchApi(
      `/h3-long-video/status/${encodeURIComponent(jobId)}`,
      { cache: "no-store" },
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);

    const labels = {
      scheduled: "已排队",
      running: "正在生成单段视频",
      completed: "生成完成",
      failed: "生成失败",
      unknown: "等待状态",
    };
    ui.state.textContent = labels[data.status] || data.status;
    ui.error.textContent = data.error || "";
    const video = data.segments?.[0];
    if (video?.url) {
      if (ui.video.src !== new URL(video.url, window.location.href).href) {
        ui.video.src = video.url;
        ui.video.style.display = "block";
        ui.empty.style.display = "none";
      }
    } else {
      ui.video.style.display = "none";
      ui.empty.style.display = "grid";
    }

    const details = [];
    if (data.seed !== undefined && data.seed !== null) {
      details.push(`Seed ${data.seed}`);
    }
    if (video?.seconds) details.push(`${video.seconds} 秒`);
    if (video?.two_pass !== undefined) {
      details.push(video.two_pass ? "二采" : "一采");
    }
    if (data.log) details.push(data.log);
    ui.meta.textContent = details.join("  |  ");
    ui.meta.title = data.log || "";

    if (data.status === "completed" || data.status === "failed") {
      stopPolling(node);
    }
  } catch (error) {
    ui.error.textContent = `读取任务状态失败：${error.message}`;
  }
}

function startPolling(node, ui, jobId) {
  stopPolling(node);
  node.properties ||= {};
  node.properties.h3_job_id = jobId;
  ui.jobId = jobId;
  refresh(node, ui, jobId);
  POLLERS.set(node.id, setInterval(() => refresh(node, ui, jobId), 3000));
}

function install(node) {
  ensureStyles();
  const panel = element("div", "h3-single-panel");
  const head = element("div", "h3-single-head");
  const state = element("div", "h3-single-state", "等待运行");
  const button = element("button", "h3-single-refresh", "刷新");
  head.append(state, button);

  const empty = element("div", "h3-single-empty", "生成完成后在这里预览");
  const video = document.createElement("video");
  video.className = "h3-single-video";
  video.controls = true;
  video.preload = "metadata";
  video.style.display = "none";
  const meta = element("div", "h3-single-meta");
  const error = element("div", "h3-single-error");
  panel.append(head, empty, video, meta, error);

  const ui = { state, empty, video, meta, error, jobId: "" };
  node.h3SingleUI = ui;
  node.addDOMWidget("h3_single_clip_preview", "H3SingleClipPreview", panel, {
    serialize: false,
  });
  node.setSize([
    Math.max(node.size[0], 460),
    Math.max(node.size[1], 820),
  ]);

  button.addEventListener("click", () => {
    const jobId = ui.jobId || node.properties?.h3_job_id;
    if (jobId) refresh(node, ui, jobId);
  });

  if (node.properties?.h3_job_id) {
    startPolling(node, ui, node.properties.h3_job_id);
  }
}

app.registerExtension({
  name: "H3SingleClip.Preview",
  nodeCreated(node) {
    if (node.comfyClass !== NODE_TYPE && node.type !== NODE_TYPE) return;
    install(node);
    const originalExecuted = node.onExecuted;
    node.onExecuted = function (message) {
      const result = originalExecuted?.apply(this, arguments);
      const payload = message?.h3_single_clip?.[0];
      if (payload?.job_id && node.h3SingleUI) {
        startPolling(node, node.h3SingleUI, payload.job_id);
      }
      return result;
    };
    const originalRemoved = node.onRemoved;
    node.onRemoved = function () {
      stopPolling(node);
      return originalRemoved?.apply(this, arguments);
    };
  },
});
