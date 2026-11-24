/* 播放页前端逻辑 */
const params = new URLSearchParams(location.search);
const sourceId = params.get("id");
const from = params.get("from") || "";
const $ = (sel) => document.querySelector(sel);

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

function humanSize(n) {
  n = Number(n) || 0;
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (n < 1024) return n.toFixed(n < 10 && n > 0 ? 1 : 0) + u;
    n /= 1024;
  }
  return n.toFixed(1) + "PB";
}

if (!sourceId) {
  document.body.innerHTML = "<p style='text-align:center;padding:60px'>缺少作品 ID</p>";
} else {
  init();
}

let currentAudio = null;
let currentSubtitleUrl = null;
let subtitleInterval = null;

async function init() {
  if (from === "subm") {
    const back = $("#player-back");
    if (back) {
      back.href = "/?tab=subm";
      back.innerHTML = '<svg class="ico" aria-hidden="true"><use href="/icons.svg#i-arrow-left"></use></svg>返回生成字幕';
    }
  }
  try {
    const data = await api(`/api/player/work/${sourceId}`);
    const w = data.work;
    const files = data.files;
    const subs = data.subtitles || [];

    if (!data.has_local) {
      $("#no-local").classList.remove("hidden");
      $("#player-wrap").classList.add("hidden");
      return;
    }

    $("#no-local").classList.add("hidden");
    $("#player-wrap").classList.remove("hidden");
    $("#work-title").textContent = `${w.source_id} — ${w.title}`;
    $("#work-meta").textContent = `${w.release_date || ""} | 销量: ${w.dl_count || 0} | 本地 ${w.file_count || 0} 个文件 (${humanSize(w.total_size || 0)})`;

    // 过滤音频文件
    const audioExts = [".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".webm"];
    const audioFiles = files.filter(f =>
      f.status === "downloaded" &&
      audioExts.includes(f.filename.toLowerCase().substring(f.filename.lastIndexOf(".")))
    );

    if (!audioFiles.length) {
      $("#file-list").innerHTML = "<div class='empty'>没有已下载的音频文件</div>";
      return;
    }

    // 播放页只负责点击播放并预览字幕，音轨翻译开关在「生成字幕」卡片弹窗里
    const viewFiles = audioFiles;

    const flist = $("#file-list");
    flist.innerHTML = viewFiles.map((f, i) => `
      <div class="file-item ${i === 0 ? "active" : ""}" data-idx="${i}">
        <svg class="ico" aria-hidden="true"><use href="/icons.svg#i-music"></use></svg>
        <span class="file-name">${f.filename}</span>
        <span class="ext">${humanSize(f.size)}</span>
      </div>`).join("");

    flist.querySelectorAll(".file-item").forEach(el => {
      el.addEventListener("click", () => {
        flist.querySelectorAll(".file-item").forEach(x => x.classList.remove("active"));
        el.classList.add("active");
        playFile({ filename: viewFiles[Number(el.dataset.idx)].filename });
      });
    });

    // 渲染字幕选择（本地优先，其次在线 API，按置信度排序）
    const sel = $("#subtitle-select");
    subs.forEach((s) => {
      const conf = Math.round((s.confidence || 0) * 100);
      let url = "";
      if (s.local_path) {
        url = `/api/player/subtitle_content/${encodeURIComponent(sourceId + "/" + (s.rel_path || s.filename))}`;
      } else if (s.filename) {
        url = `/api/player/api_subtitle/${encodeURIComponent(sourceId)}?name=${encodeURIComponent(s.filename)}`;
      }
      if (!url) return;
      const opt = document.createElement("option");
      opt.value = url;
      opt.dataset.name = s.filename;
      opt.textContent = `${s.filename} [${s.source === "api" ? "在线" : "本地"}, ${conf}%]`;
      sel.appendChild(opt);
    });

    sel.addEventListener("change", () => loadSubtitle(sel.value));

    // 转录按钮（本地和在线都没有字幕时才提示）
    if (!subs.length) $("#btn-transcribe").disabled = false;
    $("#btn-transcribe").addEventListener("click", startTranscribe);

    // 开始播放（playFile 内部会按音轨名智能选字幕）
    playFile({ filename: viewFiles[0].filename });

  } catch (e) {
    $("#work-title").textContent = "加载失败: " + e.message;
  }
}

function playFile(fileRecord) {
  const player = $("#player");
  const url = `/api/library/file/${sourceId}?file=${encodeURIComponent(fileRecord.filename)}`;
  player.src = url;
  player.load();
  player.play().catch(() => {});
  currentAudio = fileRecord;
  // 重置字幕
  $("#subtitle-display").textContent = "";
  if (subtitleInterval) { clearInterval(subtitleInterval); subtitleInterval = null; }

  // 智能选字幕：优先与当前音轨同名的字幕（如 音轨1.mp3 → 音轨1.mp3.vtt）
  const sel = $("#subtitle-select");
  if (sel.options.length <= 1) return;
  const stem = fileRecord.filename.replace(/\.[^.]+$/, "").toLowerCase();
  let matched = null;
  Array.from(sel.options).forEach(o => {
    if (!o.dataset.name || matched) return;
    const n = o.dataset.name.replace(/\.[^.]+$/, "").toLowerCase();
    // 生成字幕文件名形如「双语_RJ01426915_01_本編」，音轨名包含在其中
    if (n === stem || n.startsWith(stem) || stem.startsWith(n) ||
        (stem.length >= 3 && n.includes(stem))) matched = o;
  });
  const target = matched || (sel.selectedIndex > 0 ? sel.options[sel.selectedIndex] : null);
  if (target) {
    sel.value = target.value;
    loadSubtitle(target.value);
  }
}

function loadSubtitle(url) {
  if (subtitleInterval) { clearInterval(subtitleInterval); subtitleInterval = null; }
  const player = $("#player");
  // 移除旧 track
  Array.from(player.querySelectorAll("track")).forEach(t => t.remove());
  const disp = $("#subtitle-display");
  disp.textContent = "";

  if (!url || url === "none") return;

  const track = document.createElement("track");
  track.kind = "subtitles";
  track.label = "字幕";
  track.srclang = "zh";
  track.src = url;
  track.default = true;
  player.appendChild(track);
  try { player.textTracks[0].mode = "showing"; } catch (e) {}

  // 同时用自定义字幕显示（更美观）
  disp.textContent = "字幕加载中...";
  fetch(url).then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.text();
  }).then(vtt => {
    const cues = parseVTT(vtt);
    if (!cues.length) { disp.textContent = "字幕为空"; return; }
    subtitleInterval = setInterval(() => {
      const now = player.currentTime;
      const active = cues.find(c => now >= c.start && now <= c.end);
      disp.textContent = active ? active.text : "";
    }, 200);
  }).catch(e => { disp.textContent = "字幕加载失败: " + e.message; });
}

function parseVTT(text) {
  const raw = text.replace(/\r\n/g, "\n");
  if (!raw.includes("-->") && /^\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]/m.test(raw)) return parseLRC(raw);
  const cues = [];
  const blocks = raw.split(/\n\n+/);
  for (const block of blocks) {
    const lines = block.trim().split("\n");
    if (!lines.length || lines[0].startsWith("WEBVTT")) continue;
    // 找时间行
    let timeLine = lines.find(l => l.includes("-->"));
    if (!timeLine) continue;
    const [s, e] = timeLine.split("-->").map(x => x.trim());
    const start = parseTime(s);
    const end = parseTime(e);
    const text = lines.slice(lines.indexOf(timeLine) + 1).join("\n");
    cues.push({ start, end, text });
  }
  return cues;
}

function parseLRC(text) {
  const raw = [];
  text.replace(/\r/g, "").split("\n").forEach(line => {
    let rest = line, stamp;
    const times = [];
    while ((stamp = rest.match(/^\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/))) {
      times.push(+stamp[1] * 60 + +stamp[2] + (stamp[3] ? Number("0." + stamp[3]) : 0));
      rest = rest.slice(stamp[0].length);
    }
    const body = rest.replace(/<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>/g, "").trim();
    if (!times.length || !body) return;
    times.forEach(s => raw.push({ start: s, text: body }));
  });
  raw.sort((a, b) => a.start - b.start);
  return raw.map((c, i) => {
    const next = raw[i + 1];
    let end = next ? next.start : c.start + 4;
    if (end - c.start < 0.4) end = c.start + 0.4;
    return { start: c.start, end, text: c.text };
  });
}

function parseTime(s) {
  const m = s.match(/(\d+):(\d+):(\d+)[.,](\d+)/) || s.match(/(\d+):(\d+)[.,](\d+)/);
  if (!m) return 0;
  if (m.length === 5) return +m[1]*3600 + +m[2]*60 + +m[3] + +m[4]/1000;
  return +m[1]*60 + +m[2] + +m[3]/1000;
}

async function startTranscribe() {
  const disp = $("#transcribe-status");
  disp.textContent = "创建任务...";
  try {
    const data = await api(`/api/subtitles/work/${sourceId}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    const job = data.job || {};
    if (!job.id) throw new Error(data.error || "创建失败");
    disp.textContent = "字幕生成中（本机识别/翻译，较慢）...";
    const poll = setInterval(async () => {
      try {
        const st = await api(`/api/subtitles/jobs/${job.id}`);
        disp.textContent = `状态: ${st.status}` +
          (st.message ? ` · ${st.message}` : "") +
          (st.error ? ` · ${st.error}` : "");
        if (["done", "failed", "cancelled"].includes(st.status)) {
          clearInterval(poll);
          if (st.status === "done") disp.textContent = "生成完成 — 刷新页面加载字幕";
        }
      } catch (e) {
        clearInterval(poll);
        disp.textContent = "轮询失败: " + e.message;
      }
    }, 3000);
  } catch (e) {
    disp.textContent = "失败: " + e.message;
  }
}
