/* ASMR WebUI 前端逻辑 — 原生 JS fetch */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ============ 工具 ============
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    const err = await r.text();
    throw new Error(`${r.status}: ${err}`);
  }
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

// 统一图标（内联 SVG sprite，禁止 emoji）
function icon(name) {
  return `<svg class="ico" aria-hidden="true"><use href="/icons.svg#i-${name}"></use></svg>`;
}

// 骨架屏占位卡片
function skeletonCards(n = 8) {
  let html = "";
  for (let i = 0; i < n; i++) {
    html += `<div class="work-card sk-card">
      <div class="sk-cover"></div>
      <div class="sk-body">
        <div class="sk-line w40"></div>
        <div class="sk-line w70"></div>
        <div class="sk-line w90"></div>
      </div>
    </div>`;
  }
  return html;
}

// ============ 发布日期快捷选择 ============
function isoDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function clearDatePreset(box) {
  box.querySelectorAll("button[data-days]").forEach(b => {
    b.classList.remove("active");
    b.dataset.active = "false";
  });
}

function applyDatePreset(box, fromSel, toSel, days) {
  const fromEl = $(fromSel);
  const toEl = $(toSel);
  if (days === "all") {
    fromEl.value = "";
    toEl.value = "";
  } else {
    const today = new Date();
    const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    if (days === "year") {
      start.setMonth(0, 1);
    } else if (days === "2y") {
      start.setFullYear(start.getFullYear() - 2);
    } else {
      start.setDate(start.getDate() - (Number(days) - 1));
    }
    fromEl.value = isoDate(start);
    toEl.value = isoDate(today);
  }
  clearDatePreset(box);
  const hit = box.querySelector(`button[data-days="${days}"]`);
  if (hit) {
    hit.classList.add("active");
    hit.dataset.active = "true";
  }
}

function setupDateRange(presetsSel, fromSel, toSel, onChange) {
  const box = $(presetsSel);
  box.querySelectorAll("button[data-days]").forEach(btn => {
    btn.dataset.active = btn.classList.contains("active") ? "true" : "false";
    btn.addEventListener("click", () => {
      applyDatePreset(box, fromSel, toSel, btn.dataset.days);
      onChange();
    });
  });
  if (!box.querySelector("button[data-days].active")) {
    applyDatePreset(box, fromSel, toSel, "all");
  }
  [fromSel, toSel].forEach(sel => {
    $(sel).addEventListener("change", () => {
      clearDatePreset(box);
      onChange();
    });
  });
}

/* 随机排序：每次选择生成新种子，翻页沿用同一种子保持顺序稳定 */
let libRandomSeed = 1;

function newSeed() {
  return ((Date.now() % 2147483647) ^ Math.floor(Math.random() * 1e9)) >>> 0;
}

function seededShuffle(list, seed) {
  const out = list.slice();
  let s = (seed >>> 0) || 1;
  const rnd = () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rnd() * (i + 1));
    const tmp = out[i];
    out[i] = out[j];
    out[j] = tmp;
  }
  return out;
}

/* 重新选择同一个「随机」选项时原生 change 不会触发，这里补一次重洗 */
function bindRandomReshuffle(selectEl, onReshuffle) {
  if (!selectEl) return;
  let wasRandom = false;
  selectEl.addEventListener("pointerdown", () => {
    wasRandom = selectEl.value === "random";
  });
  selectEl.addEventListener("click", () => {
    if (wasRandom && selectEl.value === "random") onReshuffle();
    wasRandom = false;
  });
}

// ============ Tab 切换 ============
function switchTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach(p => p.classList.toggle("active", p.id === `panel-${name}`));
  if (window.__gooeyNav) {
    const idx = Array.from($$(".tab")).findIndex(t => t.dataset.tab === name);
    if (idx >= 0) window.__gooeyNav.setActive(idx);
  }
  if (name === "system") loadSystem();
  if (name === "download") loadDownloads();
  if (name === "library") loadLibrary();
  else {
    const bar = $("#lib-bulk");
    if (bar) { bar.classList.add("hidden"); bar.classList.remove("in", "leaving"); }
  }
  if (name !== "subm") {
    const sbar = $("#subm-bulk");
    if (sbar) { sbar.classList.add("hidden"); sbar.classList.remove("in", "leaving"); }
  }
  if (name === "subm") loadSubm();
}

$$(".tab").forEach(btn => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// 支持 /?tab=subm（播放页「返回生成字幕」）
const bootTab = new URLSearchParams(location.search).get("tab");
if (bootTab && document.getElementById(`panel-${bootTab}`)) {
  setTimeout(() => switchTab(bootTab), 0);
}

// 移动端筛选折叠
$$(".filters-toggle").forEach(btn => {
  btn.addEventListener("click", () => {
    const panel = document.getElementById(btn.dataset.filters);
    if (!panel) return;
    const open = panel.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
});

// ============ 搜索发现 ============
let searchPage = 1;
let searchTotal = 0;
let selectedWorks = new Set();
// 失败任务重试来源：作品 ID -> 原任务 ID，加入队列时不立即下载
const retryTaskByWork = new Map();
// 搜索结果缓存：队列弹窗需要展示标题
const worksById = new Map();
// 搜索请求序号：快速连续筛选时只采用最后一次请求的结果
let searchSeq = 0;

$("#search-go").addEventListener("click", () => { searchPage = 1; doSearch(); });
$("#search-input").addEventListener("keydown", e => { if (e.key === "Enter") { searchPage = 1; doSearch(); } });

// 搜索框右侧小圆 X：清空关键词后立即重新搜索
const searchInputEl = $("#search-input");
const searchClearBtn = $("#search-clear");
function syncSearchClear() {
  searchClearBtn.classList.toggle("show", !!searchInputEl.value);
}
searchInputEl.addEventListener("input", syncSearchClear);
searchClearBtn.addEventListener("click", () => {
  searchInputEl.value = "";
  searchInputEl.focus();
  syncSearchClear();
  searchPage = 1;
  doSearch();
});
$("#search-reset").addEventListener("click", () => {
  ["#search-va","#search-tag","#search-input"].forEach(s => $(s).value = "");
  ["#search-sub","#search-order","#search-sort","#search-page-size"].forEach(s => $(s).selectedIndex = 0);
  ["tag", "va", "circle", "notTag", "notVa", "notCircle"].forEach(k => { searchFilters[k] = []; });
  clearAgeFilter("search");
  applyExcludeDownloaded(false);
  renderActiveFilters();
  doSearch();
});
// 有无字幕 / 排序改动后立即刷新，无需再点搜索
["search-sub", "search-order", "search-sort"].forEach(id => {
  $("#" + id).addEventListener("change", () => {
    searchPage = 1;
    doSearch();
  });
});
$("#batch-download").addEventListener("click", batchDownload);

// 排除已下载：复用安全模式的弹簧开关动效
let excludeDownloaded = false;
const excludeDlSwitch = $("#search-exclude-dl");
function applyExcludeDownloaded(on) {
  excludeDownloaded = !!on;
  excludeDlSwitch.classList.toggle("on", excludeDownloaded);
  excludeDlSwitch.setAttribute("aria-checked", excludeDownloaded ? "true" : "false");
}
excludeDlSwitch.addEventListener("click", () => {
  applyExcludeDownloaded(!excludeDownloaded);
  searchPage = 1;
  doSearch();
});

// 输入框里填的 VA / 标签：点击搜索后并入筛选标签（与点标签添加一致），并清空输入框
function promoteFilterInputs() {
  let changed = false;
  [["tag", "#search-tag"], ["va", "#search-va"]].forEach(([kind, sel]) => {
    const el = $(sel);
    if (!el) return;
    const val = el.value.trim();
    if (!val) return;
    el.value = "";
    if (!searchFilters[kind].includes(val)) searchFilters[kind].push(val);
    searchFilters[NEG_KEY[kind]] = searchFilters[NEG_KEY[kind]].filter(v => v !== val);
    changed = true;
  });
  if (changed) renderActiveFilters();
}

async function doSearch() {
  promoteFilterInputs();
  syncSearchClear();
  let q = $("#search-input").value.trim();
  const params = new URLSearchParams({
    page: searchPage,
    pageSize: $("#search-page-size").value,
    order: $("#search-order").value,
    sort: $("#search-sort").value,
  });
  const tagVals = searchFilters.tag.slice();
  const vaVals = searchFilters.va.slice();
  const circleVals = searchFilters.circle.slice();

  // 同类型只允许一个 filter key，多出来的条件退化为关键词（空格=同时包含）
  const extra = tagVals.slice(1).concat(vaVals.slice(1), circleVals.slice(1));
  if (extra.length) q = [q, ...extra].filter(Boolean).join(" ");
  if (q) params.set("q", q);
  if (tagVals[0]) params.set("tag", tagVals[0]);
  if (vaVals[0]) params.set("va", vaVals[0]);
  if (circleVals[0]) params.set("circle", circleVals[0]);
  if (searchFilters.notTag.length) params.set("exclude_tag", searchFilters.notTag.join(","));
  if (searchFilters.notVa.length) params.set("exclude_va", searchFilters.notVa.join(","));
  if (searchFilters.notCircle.length) params.set("exclude_circle", searchFilters.notCircle.join(","));

  const sub = $("#search-sub").value;
  if (sub !== "all") params.set("has_subtitle", sub);
  if (excludeDownloaded) params.set("exclude_downloaded", "1");
  const ages = Array.from(ageState.search);
  if (ages.length) params.set("exclude_age", ages.join(","));

  const grid = $("#works-grid");
  if (grid) grid.innerHTML = skeletonCards(8);
  const seq = ++searchSeq;
  try {
    const data = await api("/api/search?" + params);
    if (seq !== searchSeq) return; // 已有更新的筛选请求，丢弃旧结果
    renderWorks(data.works);
    searchTotal = data.total || 0;
    renderPagination(data.page, data.pageSize, searchTotal, doSearch);
  } catch (e) {
    if (seq !== searchSeq) return;
    grid.innerHTML = `<div class='empty'>搜索失败: ${e.message}</div>`;
  }
}

function renderWorks(works, gridSel = "#works-grid", onCardClick = null, checkSet = null, onChange = null) {
  const grid = $(gridSel);
  const sel = checkSet || selectedWorks;
  const notify = onChange || updateSelectedCount;
  if (!works.length) {
    if (grid._staggerIO) { grid._staggerIO.disconnect(); grid._staggerIO = null; }
    grid.innerHTML = `<div class="empty">${icon("search")}<span>没有找到作品</span></div>`;
    if (!checkSet) { selectedWorks.clear(); updateSelectedCount(); }
    else notify();
    return;
  }
  // 搜索/本地库默认整卡点击即打开；传入 checkSet 时才显示批量勾选框
  const withCheck = checkSet ? true : !onCardClick;
  works.forEach(w => { if (w && w.source_id) worksById.set(w.source_id, w); });
  grid.innerHTML = works.map(w => workCardHTML(w, withCheck, sel)).join("");
  grid.querySelectorAll(".work-card").forEach(card => {
    const sid = card.dataset.id;
    card.addEventListener("click", (e) => {
      if (e.target.closest(".check")) return;
      if (e.target.closest(".chip")) return;
      if (onCardClick) onCardClick(sid);
      else showWorkDetail(sid);
    });
    const check = card.querySelector(".check");
    if (check) check.addEventListener("click", (e) => {
      e.stopPropagation();
      if (sel.has(sid)) {
        sel.delete(sid);
        if (sel === selectedWorks) retryTaskByWork.delete(sid);
      } else sel.add(sid);
      const selected = sel.has(sid);
      card.classList.toggle("selected", selected);
      card.classList.remove("dip");
      if (selected) {
        void card.offsetWidth;
        card.classList.add("dip");
      }
      check.innerHTML = selected ? icon("check") : "";
      notify();
    });
    bindChipEvents(card, onCardClick ? "lib" : "search");
  });
  setupStaggerGrid(grid);
  fillCardDurations(grid);
  notify();
}

// 分批向服务端取总时长，回填到作品卡片（每批 8 个，避免一次探测过多音轨）
function fillCardDurations(grid) {
  if (!grid) return;
  const ids = Array.from(grid.querySelectorAll(".rj-dur[data-dur]"))
    .map(el => el.dataset.dur).filter(Boolean);
  if (!ids.length) return;

  const batches = [];
  for (let i = 0; i < ids.length; i += 8) batches.push(ids.slice(i, i + 8));
  batches.reduce((chain, batch) => chain.then(async () => {
    try {
      const data = await api("/api/library/durations?ids=" + encodeURIComponent(batch.join(",")));
      const map = (data && data.durations) || {};
      batch.forEach(id => {
        const sec = Number(map[id] || 0);
        if (!(sec > 0)) return;
        grid.querySelectorAll(`.rj-dur[data-dur="${CSS.escape(id)}"] b`)
          .forEach(b => { b.textContent = fmtDurCard(sec); });
      });
    } catch (e) {
      // 时长失败静默，卡片保留占位
    }
  }), Promise.resolve());
}

// ---------------- 结果网格入场：保持原有等大网格，仅做进入视口交错动效 ----------------
function setupStaggerGrid(grid) {
  if (!grid) return;
  if (grid._staggerIO) { grid._staggerIO.disconnect(); grid._staggerIO = null; }
  const cards = Array.from(grid.querySelectorAll(".work-card"));
  cards.forEach(card => card.classList.add("stagger-item"));
  if (!("IntersectionObserver" in window)) {
    cards.forEach(card => card.classList.add("in"));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    // 按空间顺序（先上后下、先左后右）错峰，一次进入视口的卡片各自延迟递增
    const hits = entries.filter(en => en.isIntersecting).sort((a, b) => {
      const ra = a.boundingClientRect, rb = b.boundingClientRect;
      return ra.top - rb.top || ra.left - rb.left;
    });
    hits.forEach((en, i) => {
      en.target.style.setProperty("--stagger-i", String(Math.min(i, 9)));
      en.target.classList.add("in");
      io.unobserve(en.target);
    });
  }, { threshold: 0.08, rootMargin: "0px 0px -32px 0px" });
  cards.forEach(card => io.observe(card));
  grid._staggerIO = io;
}

function escHTML(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 封面标签筛选状态（search 页）：包含项与排除项分开存
const searchFilters = { tag: [], va: [], circle: [], notTag: [], notVa: [], notCircle: [] };
// 本地资源页独立的筛选状态，结构与搜索发现一致
const libFilters = { tag: [], va: [], circle: [], notTag: [], notVa: [], notCircle: [] };
const FILTER_LABEL = { tag: "标签", va: "作者", circle: "社团" };
const NEG_KEY = { tag: "notTag", va: "notVa", circle: "notCircle" };

const FILTER_KEYS = ["tag", "va", "circle", "notTag", "notVa", "notCircle"];

function applyChip(scope, kind, value, mode) {
  const st = scope === "lib" ? libFilters : searchFilters;
  const inputs = scope === "lib"
    ? { tag: "#lib-tag", va: "#lib-va", circle: "#lib-circle" }
    : { tag: "#search-tag", va: "#search-va" };
  const nk = NEG_KEY[kind];
  if (mode === "only") {
    FILTER_KEYS.forEach(k => { st[k] = []; });
    Object.values(inputs).forEach(sel => { const el = $(sel); if (el) el.value = ""; });
    $(scope === "lib" ? "#lib-q" : "#search-input").value = "";
    st[kind] = [value];
  } else if (mode === "add") {
    if (!st[kind].includes(value)) st[kind].push(value);
    st[nk] = st[nk].filter(v => v !== value);
  } else {
    st[kind] = st[kind].filter(v => v !== value);
    if (!st[nk].includes(value)) st[nk].push(value);
  }
  if (scope === "lib") {
    renderLibActiveFilters();
    loadLibrary();
  } else {
    searchPage = 1;
    renderActiveFilters();
    doSearch();
  }
}

function renderActiveFilters() {
  const wrap = $("#search-active");
  if (!wrap) return;
  const items = [];
  ["tag", "va", "circle"].forEach(kind => {
    searchFilters[kind].forEach(v => items.push(activeFilterBtn(kind, v, false)));
  });
  ["tag", "va", "circle"].forEach(kind => {
    searchFilters[NEG_KEY[kind]].forEach(v => items.push(activeFilterBtn(kind, v, true)));
  });
  wrap.innerHTML = items.length
    ? `<span class="af-label">筛选</span>${items.join("")}`
    : "";
  wrap.querySelectorAll(".af-chip").forEach(btn => {
    btn.addEventListener("click", () => {
      const kind = btn.dataset.kind;
      const value = btn.dataset.val;
      if (btn.dataset.ex === "1") {
        searchFilters[NEG_KEY[kind]] = searchFilters[NEG_KEY[kind]].filter(v => v !== value);
      } else {
        searchFilters[kind] = searchFilters[kind].filter(v => v !== value);
      }
      searchPage = 1;
      renderActiveFilters();
      doSearch();
    });
  });
}

function activeFilterBtn(kind, value, exclude) {
  return `<button type="button" class="af-chip${exclude ? " exclude" : ""}"
    data-kind="${kind}" data-val="${escHTML(value)}" data-ex="${exclude ? "1" : "0"}"
    title="点击移除">${exclude ? "-" : ""}${FILTER_LABEL[kind]}：${escHTML(value)}${icon("x")}</button>`;
}

// 本地资源：已生效的标签筛选条件（与搜索发现一致，点击即移除）
function renderLibActiveFilters() {
  const wrap = $("#lib-active");
  if (!wrap) return;
  const items = [];
  ["tag", "va", "circle"].forEach(kind => {
    libFilters[kind].forEach(v => items.push(activeFilterBtn(kind, v, false)));
  });
  ["tag", "va", "circle"].forEach(kind => {
    libFilters[NEG_KEY[kind]].forEach(v => items.push(activeFilterBtn(kind, v, true)));
  });
  wrap.innerHTML = items.length
    ? `<span class="af-label">筛选</span>${items.join("")}`
    : "";
  wrap.querySelectorAll(".af-chip").forEach(btn => {
    btn.addEventListener("click", () => {
      const kind = btn.dataset.kind;
      const value = btn.dataset.val;
      if (btn.dataset.ex === "1") {
        libFilters[NEG_KEY[kind]] = libFilters[NEG_KEY[kind]].filter(v => v !== value);
      } else {
        libFilters[kind] = libFilters[kind].filter(v => v !== value);
      }
      renderLibActiveFilters();
      loadLibrary();
    });
  });
}

// ---------------- 标签右键 / 长按菜单 ----------------
let chipMenu = null;

function closeChipMenu() {
  if (chipMenu) { chipMenu.remove(); chipMenu = null; }
}

function showChipMenu(x, y, scope, kind, value) {
  closeChipMenu();
  const label = FILTER_LABEL[kind] || "标签";
  chipMenu = document.createElement("div");
  chipMenu.className = "ctx-menu";
  chipMenu.innerHTML =
    `<button type="button" data-mode="add">添加${label}筛选：${escHTML(value)}</button>
     <button type="button" data-mode="exclude">排除此${label}</button>`;
  document.body.appendChild(chipMenu);
  const rect = chipMenu.getBoundingClientRect();
  chipMenu.style.left = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8)) + "px";
  chipMenu.style.top = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8)) + "px";
  chipMenu.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      applyChip(scope, kind, value, btn.dataset.mode);
      closeChipMenu();
    });
  });
  setTimeout(() => document.addEventListener("click", closeChipMenu, { once: true }), 0);
}

function bindChipEvents(container, scope) {
  container.querySelectorAll(".chip").forEach(chip => {
    const kind = chip.dataset.kind;
    const value = chip.dataset.val;
    let pressTimer = null;
    let longPressed = false;

    chip.addEventListener("click", (e) => {
      e.stopPropagation();
      if (longPressed) { longPressed = false; return; }
      applyChip(scope, kind, value, "only");
    });

    chip.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      showChipMenu(e.clientX, e.clientY, scope, kind, value);
    });

    chip.addEventListener("touchstart", (e) => {
      const t = e.touches[0];
      longPressed = false;
      pressTimer = setTimeout(() => {
        longPressed = true;
        showChipMenu(t.clientX, t.clientY, scope, kind, value);
      }, 500);
    }, { passive: true });

    ["touchend", "touchmove", "touchcancel"].forEach(ev => {
      chip.addEventListener(ev, () => { clearTimeout(pressTimer); }, { passive: true });
    });
  });
}

function chipHTML(kind, value) {
  const label = { tag: "标签", circle: "社团", va: "作者" }[kind] || "标签";
  return `<button type="button" class="chip chip-${kind}" data-kind="${kind}"
    data-val="${escHTML(value)}" title="${label}：${escHTML(value)}（左键筛选，右键更多）"
    >${escHTML(value)}</button>`;
}

// ---------------- 分级筛选（弹性勾选 + 划线，无对勾） ----------------
const AGE_OPTIONS = [
  { key: "all", label: "全年龄", color: "var(--age-all)" },
  { key: "r15", label: "R15", color: "var(--age-r15)" },
  { key: "adult", label: "ADULT", color: "var(--age-adult)" },
];
const ageState = { search: new Set(), lib: new Set() };

function buildAgeFilters() {
  [["#search-age", "search"], ["#lib-age", "lib"]].forEach(([sel, scope]) => {
    const host = $(sel);
    if (!host) return;
    host.innerHTML = AGE_OPTIONS.map(o => `
      <button type="button" class="spring-check" role="checkbox" aria-checked="false"
              data-scope="${scope}" data-age="${o.key}" title="勾选后排除该分级"
              style="--sc-ink:${o.color};--sc-fill:${o.color};--sc-check:#0b0b0f">
        <span class="spring-check__press"><span class="spring-check__box">
          <span class="spring-check__ring"></span><span class="spring-check__fill"></span>
          <svg class="spring-check__x" viewBox="0 0 24 24" aria-hidden="true">
            <path pathLength="1" d="M7 7 L17 17"></path><path pathLength="1" d="M17 7 L7 17"></path>
          </svg>
        </span></span>
        <span class="spring-check__label"><span class="spring-check__word">${o.label}</span><span class="spring-check__rule"></span></span>
      </button>`).join("");

    // 一行放不下三个选项时整体改为一行一个，不允许出现 2+1 折行
    const fit = () => {
      host.classList.remove("stack");
      if (host.scrollWidth > host.clientWidth + 1) host.classList.add("stack");
    };
    fit();
    if ("ResizeObserver" in window) new ResizeObserver(fit).observe(host);
  });

  $$(".spring-check[data-scope]").forEach(btn => {
    btn.addEventListener("pointerdown", () => { btn.dataset.pressed = ""; });
    ["pointerup", "pointercancel", "pointerleave"].forEach(ev =>
      btn.addEventListener(ev, () => { delete btn.dataset.pressed; }));
    btn.addEventListener("click", () => {
      const scope = btn.dataset.scope, key = btn.dataset.age;
      const set = ageState[scope];
      const on = !set.has(key);
      if (on) set.add(key); else set.delete(key);
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-checked", on ? "true" : "false");
      if (scope === "lib") loadLibrary();
      else { searchPage = 1; doSearch(); }
    });
  });
}

function clearAgeFilter(scope) {
  ageState[scope].clear();
  $$(`.spring-check[data-scope="${scope}"]`).forEach(btn => {
    btn.classList.remove("on");
    btn.setAttribute("aria-checked", "false");
  });
}

buildAgeFilters();

function workCardHTML(w, withCheck = true, checkSet = selectedWorks) {
  const checked = withCheck && checkSet.has(w.source_id);
  const tagChips = (w.tags || []).map(t => chipHTML("tag", t)).join("");
  const circle = (w.tags_circle || []).join(" / ");
  const va = (w.tags_va || []).join(" / ");
  const circleChips = (w.tags_circle || []).map(t => chipHTML("circle", t)).join("");
  const vaChips = (w.tags_va || []).map(t => chipHTML("va", t)).join("");
  const cover = w.cover_url || `/api/cover/${w.source_id}?v=2`;
  const local = w.local_path ? " local" : "";
  const ageLevel = ["all", "r15", "adult"].includes(w.age_level) ? w.age_level : "adult";
  // 黄色【中字】=官方，蓝色【中字】=本地生成
  const subStatus = w.subtitle_status
    || (w.has_subtitle ? "official_only" : (w.has_generated_zh ? "generated_only" : "none"));
  const officialBadge = (subStatus === "official_only" || subStatus === "both")
    ? `<span class="subtitle-badge">中字</span>` : "";
  const generatedBadge = (subStatus === "generated_only" || subStatus === "both")
    ? `<span class="subtitle-badge gen">中字</span>` : "";
  const subbadge = officialBadge + generatedBadge;
  const checkHTML = withCheck ? `<div class="check">${checked ? icon("check") : ""}</div>` : "";

  // 社团与作者分列显示，作者完整展示不再省略
  const metaRows = [
    circle ? `<div class="meta-row"><span class="role">社团</span><span class="who circle">${escHTML(circle)}</span></div>` : "",
    va ? `<div class="meta-row"><span class="role">作者</span><span class="who author">${escHTML(va)}</span></div>` : "",
  ].join("");
  const integrityLabels = { complete: "完整", incomplete: "不完整", error: "校验失败" };
  const integrityStatus = w.integrity_status || "unchecked";
  // 搜索发现页不展示“未校验”，只提示已有的校验结果
  const showIntegrity = w.local_path && (!withCheck || integrityStatus !== "unchecked");
  const integrityBadge = showIntegrity
    ? `<span class="integrity-badge integrity-${integrityStatus}">${integrityLabels[integrityStatus] || "未校验"}</span>`
    : "";
  const sizeHTML = w.local_path
    ? `<div class="local-size">${icon("drive")}${humanSize(w.total_size || 0)} · ${w.file_count || 0} 个文件${integrityBadge}</div>`
    : "";
  // 本地作品在 RJ 号右侧显示总时长（先占位，渲染后批量回填）
  const dur = Number(w.duration || 0);
  const durHTML = w.local_path
    ? `<span class="rj-dur" data-dur="${w.source_id}">${icon("clock")}<b>${dur > 0 ? fmtDurCard(dur) : "--:--"}</b></span>`
    : "";
  const releaseDate = (w.release_date || "").substring(0, 10);
  const dateBadge = releaseDate ? `<span class="cover-date">${releaseDate}</span>` : "";
  const price = Number(w.price || 0).toLocaleString("en-US");

  return `
    <div class="work-card${local}${checked ? " selected" : ""}" data-id="${w.source_id}">
      ${checkHTML}
      <div class="cover">${dateBadge}<img src="${cover}" alt="" loading="lazy"
        onerror="if(!this.dataset.retried){this.dataset.retried='1';this.src='${cover}${cover.includes('?') ? '&' : '?'}retry=' + Date.now();}else{this.replaceWith(Object.assign(document.createElement('div'),{className:'cover-placeholder',textContent:'no cover'}));}"></div>
      <div class="body">
        <div class="rj rj-${ageLevel}">${subbadge}${w.source_id}${durHTML}</div>
        ${metaRows}
        <div class="title">${w.title}</div>
        <div class="meta">
          <span class="price">${price} JPY</span>
          <span class="sales" title="销量">${icon("download")}${w.dl_count || 0}</span>
        </div>
        ${sizeHTML}
        <div class="chips">${tagChips}${circleChips}${vaChips}</div>
      </div>
    </div>`;
}

function scrollPageTop() {
  window.scrollTo(0, 0);
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
}

function renderPagination(page, pageSize, total, onChange, navSel = "#pagination", onJump = null) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const nav = $(navSel);
  const rawSetPage = onJump || ((p) => { searchPage = p; onChange(); });
  const setPage = (p) => {
    if (p === page) return;
    scrollPageTop();
    rawSetPage(p);
  };
  let html = "";
  const start = Math.max(1, page - 3);
  const end = Math.min(totalPages, page + 3);
  if (start > 1) html += `<button data-p="1">1</button>`;
  if (start > 2) html += `<span>...</span>`;
  for (let i = start; i <= end; i++) html += `<button data-p="${i}" class="${i === page ? "active" : ""}">${i}</button>`;
  if (end < totalPages - 1) html += `<span>...</span>`;
  if (end < totalPages) html += `<button data-p="${totalPages}">${totalPages}</button>`;
  html += `<label class="pg-jump">跳至 <input class="pg-jump__input" type="number" min="1" max="${totalPages}"
    inputmode="numeric" value="${page}" aria-label="跳转页码"> / ${totalPages} 页</label>`;
  nav.innerHTML = html;
  nav.querySelectorAll("button").forEach(b => b.addEventListener("click", () => setPage(Number(b.dataset.p))));

  const jump = nav.querySelector(".pg-jump__input");
  if (!jump) return;
  const apply = () => {
    let v = parseInt(jump.value, 10);
    if (!Number.isFinite(v)) { jump.value = page; return; }
    v = Math.min(totalPages, Math.max(1, v));
    jump.value = v;
    if (v !== page) setPage(v);
  };
  jump.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); apply(); }
  });
  jump.addEventListener("change", apply);
}

function pageWorkIds() {
  return Array.from($$("#works-grid .work-card")).map(c => c.dataset.id);
}

// “全部选择 / 全部取消”：当前页全部已选则显示全部取消
function syncSelectAllBtn() {
  const btn = $("#search-select-all");
  if (!btn) return;
  const ids = pageWorkIds();
  const all = ids.length > 0 && ids.every(id => selectedWorks.has(id));
  btn.textContent = all ? "全部取消" : "全部选择";
  btn.disabled = ids.length === 0;
}

function paintCardSelection() {
  $$("#works-grid .work-card").forEach(card => {
    const on = selectedWorks.has(card.dataset.id);
    card.classList.toggle("selected", on);
    const check = card.querySelector(".check");
    if (check) check.innerHTML = on ? icon("check") : "";
  });
}

$("#search-select-all").addEventListener("click", () => {
  const ids = pageWorkIds();
  if (!ids.length) return;
  const all = ids.every(id => selectedWorks.has(id));
  ids.forEach(id => {
    if (all) {
      selectedWorks.delete(id);
      retryTaskByWork.delete(id);
    } else selectedWorks.add(id);
  });
  paintCardSelection();
  updateSelectedCount();
});

function updateSelectedCount() {
  $("#selected-count").textContent = selectedWorks.size;
  $("#batch-download").disabled = selectedWorks.size === 0;
  syncSelectAllBtn();
}

function clearSelection() {
  selectedWorks.clear();
  retryTaskByWork.clear();
  paintCardSelection();
  updateSelectedCount();
}

// ============ 已下载冲突：提示覆盖 / 跳过 ============
// null = 每次询问；"skip" / "overwrite" = 本次会话内对后续冲突沿用该选择
let dupPolicy = null;
let dupResolve = null;

function finishDup(choice) {
  closeDialog("dup-modal");
  const r = dupResolve;
  dupResolve = null;
  if (r) r(choice);
}

function askDuplicate(localIds, ctx) {
  if (dupPolicy) return Promise.resolve(dupPolicy);
  const total = ctx ? ctx.total : localIds.length;
  $("#dup-text").textContent = ctx
    ? `选中的作品中有 ${total} 个已经下载到本地，还有 ${localIds.length} 个待处理：可以跳过当前作品，或长按覆盖本地文件。`
    : `选中的作品中有 ${total} 个已经下载到本地，可以跳过它们，或覆盖本地文件重新下载。`;
  // 逐个询问时只列还没处理的作品，第一个即当前询问对象
  const shown = localIds.slice(0, 12).map((id, i) =>
    `<span class="dup-id${ctx && i === 0 ? " cur" : ""}">${id}</span>`).join("");
  $("#dup-ids").innerHTML = shown +
    (localIds.length > 12 ? `<span class="dup-id dup-id-more">+${localIds.length - 12}</span>` : "");
  dupCb.classList.remove("on");
  dupCb.setAttribute("aria-checked", "false");
  dupHold.reset();
  openDialog("dup-modal");
  return new Promise(res => { dupResolve = res; });
}

// “之后都这样处理”勾选框：沿用现有的弹性勾选动效
const dupCb = $("#dup-apply-all");
dupCb.addEventListener("pointerdown", () => { dupCb.dataset.pressed = ""; });
["pointerup", "pointercancel", "pointerleave"].forEach(ev =>
  dupCb.addEventListener(ev, () => { delete dupCb.dataset.pressed; }));
function toggleDupCb() {
  const on = !dupCb.classList.contains("on");
  dupCb.classList.toggle("on", on);
  dupCb.setAttribute("aria-checked", on ? "true" : "false");
}
dupCb.addEventListener("click", toggleDupCb);
$("#dup-modal .dup-apply-text").addEventListener("click", toggleDupCb);

// 原生版 HoldButton：长按注水确认（波浪边缘 + 按压回弹 + 完成脉冲）
function initHoldButton(btn, opts = {}) {
  const holdTime = Number(opts.holdTime) || 1200;
  const releaseTime = Number(opts.releaseTime) || 200;
  const onHold = opts.onHold;
  const reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const LINEAR = t => t;
  const EASE_OUT = t => 1 - Math.pow(1 - t, 3);

  let phase = "idle";
  let input = null;
  let raf = 0;
  let p = 0;
  let fromP = 0;
  let toP = 0;
  let startT = 0;
  let completeTimer = 0;
  const gesture = { pointerId: null, start: 0, rect: null };

  const measure = () => {
    btn.style.setProperty("--hb-w", `${btn.offsetWidth}px`);
    btn.style.setProperty("--hb-h", `${btn.offsetHeight}px`);
  };
  measure();
  if ("ResizeObserver" in window) new ResizeObserver(measure).observe(btn);

  function setPhase(next, kind) {
    phase = next;
    input = kind;
    btn.dataset.phase = next;
    if (kind) btn.dataset.input = kind;
    else btn.removeAttribute("data-input");
  }

  function drive(to, duration, ease) {
    cancelAnimationFrame(raf);
    fromP = p;
    toP = to;
    startT = performance.now();
    const step = now => {
      const t = duration > 0 ? Math.min(1, (now - startT) / duration) : 1;
      p = fromP + (toP - fromP) * ease(t);
      btn.style.setProperty("--hb-p", p.toFixed(4));
      if (t < 1) { raf = requestAnimationFrame(step); return; }
      raf = 0;
      if (toP === 1) complete();
    };
    raf = requestAnimationFrame(step);
  }

  function complete() {
    if (phase !== "holding") return;
    if (performance.now() - gesture.start < holdTime - 50) return;
    clearTimeout(completeTimer);
    setPhase("done", input);
    if (onHold) onHold();
  }

  function begin(kind) {
    if (btn.disabled || phase !== "idle") return false;
    gesture.start = performance.now();
    gesture.rect = btn.getBoundingClientRect();
    setPhase("holding", kind);
    drive(1, holdTime, LINEAR);
    completeTimer = setTimeout(complete, holdTime + 100);
    return true;
  }

  function release(opts2 = {}) {
    if (phase !== "holding") return;
    clearTimeout(completeTimer);
    setPhase("idle");
    drive(0, releaseTime, EASE_OUT);
    void opts2;
  }

  function reset() {
    clearTimeout(completeTimer);
    cancelAnimationFrame(raf);
    raf = 0;
    p = 0;
    fromP = 0;
    toP = 0;
    btn.style.setProperty("--hb-p", "0");
    setPhase("idle");
  }

  btn.addEventListener("pointerdown", e => {
    if (e.button !== 0 || !e.isPrimary || gesture.pointerId !== null) return;
    if (!begin("pointer")) return;
    gesture.pointerId = e.pointerId;
    try { btn.setPointerCapture(e.pointerId); } catch {}
  });

  function endPointer(e, drifted) {
    if (e.pointerId !== gesture.pointerId) return;
    gesture.pointerId = null;
    try {
      if (btn.hasPointerCapture(e.pointerId)) btn.releasePointerCapture(e.pointerId);
    } catch {}
    release({ drifted });
  }

  btn.addEventListener("pointermove", e => {
    if (e.pointerId !== gesture.pointerId) return;
    const r = gesture.rect;
    if (!r) return;
    const pad = 10;
    if (e.clientX < r.left - pad || e.clientX > r.right + pad ||
        e.clientY < r.top - pad || e.clientY > r.bottom + pad) {
      endPointer(e, true);
    }
  });
  btn.addEventListener("pointerup", e => endPointer(e, false));
  btn.addEventListener("pointercancel", e => endPointer(e, true));
  btn.addEventListener("lostpointercapture", e => endPointer(e, true));
  btn.addEventListener("pointerleave", e => {
    if (e.pointerType !== "touch") endPointer(e, true);
  });
  btn.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      if (input === "key") release({ drifted: true });
      return;
    }
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      if (!e.repeat) begin("key");
    }
  });
  btn.addEventListener("keyup", e => {
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      if (input === "key") release();
    }
  });
  btn.addEventListener("contextmenu", e => e.preventDefault());
  if (reduce) btn.dataset.reduce = "true";

  return { reset };
}

const dupHold = initHoldButton($("#dup-overwrite"), {
  holdTime: 1200,
  onHold: () => {
    if (dupCb.classList.contains("on")) dupPolicy = "overwrite";
    finishDup("overwrite");
  },
});

$("#dup-skip").addEventListener("click", () => {
  if (dupCb.classList.contains("on")) dupPolicy = "skip";
  finishDup("skip");
});
$("#dup-modal .modal-close").addEventListener("click", () => finishDup("cancel"));
$("#dup-modal").addEventListener("click", e => {
  if (e.target && e.target.id === "dup-modal") finishDup("cancel");
});

// ============ 批量下载：下载中跳过 / 已下载询问 / 已下载且下载中跳过 ============
// 从进行中的任务里推算“正在下载”的作品（current_work 及其之后的都还没跑完）
function activeWorkIds(tasks) {
  const out = new Set();
  (tasks || []).forEach(t => {
    if (t.status !== "running" && t.status !== "pending") return;
    const ids = t.work_ids || [];
    const cur = t.current_work;
    const start = cur ? Math.max(0, ids.indexOf(cur)) : 0;
    ids.slice(start).forEach(id => out.add(id));
  });
  return out;
}

async function loadDownloadStatus(ids) {
  const [chk, tasks] = await Promise.all([
    api("/api/library/local-check", {
      method: "POST", body: JSON.stringify({ ids }),
    }).catch(() => ({ local: [] })),
    api("/api/download/tasks").catch(() => ({ tasks: [] })),
  ]);
  const all = tasks.tasks || [];
  return {
    localIds: new Set((chk.local || []).map(x => x.source_id)),
    running: activeWorkIds(all),
    activeTasks: all.filter(t => t.status === "running" || t.status === "pending"),
  };
}

// ============ 下载前语言版本检查 ============
const ZH_ORDER = { CHI_HANS: 0, CHI: 1, CHI_HANT: 2 };
let langMode = "switch";
let langResolve = null;

function isZhWork(w) {
  return !!w && ZH_ORDER[w.lang] !== undefined;
}

// 本地库旧记录没有语言字段时，实时从上游补齐
async function ensureLangData(ids) {
  const missing = [...new Set(ids)].filter(id => {
    const w = worksById.get(id);
    return !w || w.lang_known !== true;
  });
  if (!missing.length) return;
  try {
    const data = await api("/api/download/lang-info", {
      method: "POST", body: JSON.stringify({ ids: missing }),
    });
    (data.items || []).forEach(w => {
      if (!w || w.error || !w.source_id) return;
      const old = worksById.get(w.source_id) || {};
      worksById.set(w.source_id, { ...old, ...w, lang_known: true });
    });
  } catch (e) { /* 拿不到语言信息时按原逻辑下载 */ }
}

// 切换前复核：候选必须被当前作品列出，且原始作品 ID / 版本组对得上
async function verifySwitchTargets(pairs) {
  const ids = [...new Set(pairs.map(p => p.to.source_id).filter(Boolean))];
  if (!ids.length) return [];
  let items = [];
  try {
    const data = await api("/api/download/lang-info", {
      method: "POST", body: JSON.stringify({ ids }),
    });
    items = data.items || [];
  } catch (e) {
    return [];
  }
  const byId = new Map();
  items.forEach(w => {
    if (!w || w.error || !w.source_id) return;
    byId.set(w.source_id, w);
    const old = worksById.get(w.source_id) || {};
    worksById.set(w.source_id, { ...old, ...w });
  });
  return pairs.filter(p => {
    const c = byId.get(p.to.source_id);
    if (!c) return false;
    const listed = (p.from.editions || []).some(e => e.source_id === c.source_id);
    const sameOriginal = c.original_workno && c.original_workno === p.from.source_id;
    const sameGroup = c.edition_group && p.from.edition_group &&
                      String(c.edition_group) === String(p.from.edition_group);
    return listed && (sameOriginal || sameGroup);
  }).map(p => ({ from: p.from, to: byId.get(p.to.source_id) }));
}

function langMember(id, fallback) {
  const w = worksById.get(id) || {};
  const f = fallback || {};
  return {
    source_id: id,
    title: w.title || f.title || id,
    lang: w.lang || f.lang || "",
    lang_label: w.lang_label || f.lang_label || "",
    cover_url: w.cover_url || f.cover_url || `/api/cover/${id}?v=2`,
    release_date: (w.release_date || "").substring(0, 10),
    edition_group: w.edition_group || f.edition_group || "",
    original_workno: w.original_workno || f.original_workno || "",
  };
}

function buildLanguagePlan(ids, localIds, running, verifiedPairs) {
  const verified = new Set((verifiedPairs || []).map(p => `${p.from.source_id}|${p.to.source_id}`));
  const plan = {
    targetById: new Map(),
    switchPairs: [],
    conflicts: [],
    skips: new Map(),
    removeIds: new Set(),
    notes: [],
  };

  // 同一 edition_group 视为同一作品的不同语言版本
  const groups = new Map();
  ids.forEach(id => {
    const w = worksById.get(id) || {};
    const key = String(w.edition_group || id);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(id);
  });

  groups.forEach(groupIds => {
    const present = groupIds.map(id => langMember(id));
    const memberMap = new Map(present.map(m => [m.source_id, m]));
    present.forEach(m => {
      const w = worksById.get(m.source_id) || {};
      (w.editions || []).forEach(e => {
        if (e && e.source_id && !memberMap.has(e.source_id)) {
          memberMap.set(e.source_id, langMember(e.source_id, e));
        }
      });
    });
    const members = [...memberMap.values()];

    // 目标版本：优先本次选中的中文版；否则使用通过复核的中文候选
    let target = null;
    const zhSelected = present.filter(isZhWork)
      .sort((a, b) => ZH_ORDER[a.lang] - ZH_ORDER[b.lang]);
    if (zhSelected.length) {
      target = zhSelected[0];
    } else {
      const rep = present[0];
      const w = rep && worksById.get(rep.source_id);
      const zh = w && w.chinese_edition;
      if (zh && zh.source_id && verified.has(`${rep.source_id}|${zh.source_id}`)) {
        target = memberMap.get(zh.source_id) || langMember(zh.source_id, zh);
        plan.switchPairs.push({ from: w, to: target });
      } else {
        target = rep;
      }
    }
    if (!target) return;

    // 本地已有中文版：静默跳过，不再下载其他版本
    const localMembers = members.filter(m => localIds.has(m.source_id));
    const zhLocal = localMembers.filter(isZhWork);
    if (zhLocal.length) {
      groupIds.forEach(id => plan.skips.set(id, "local_zh"));
      plan.notes.push(`${zhLocal[0].source_id} 已有中文版本，已跳过`);
      return;
    }

    // 其他语言版本正在下载：不打断任务，跳过新版本
    const runningMembers = members.filter(m => running.has(m.source_id));
    if (runningMembers.length) {
      const reason = runningMembers.some(isZhWork) ? "running_zh" : "running_sibling";
      groupIds.forEach(id => plan.skips.set(id, reason));
      plan.notes.push(`${runningMembers[0].source_id} 正在下载，已跳过 ${target.source_id}`);
      return;
    }

    // 本地非中文版与目标不同：交给用户选择保留或覆盖
    const targetLocal = localMembers.some(m => m.source_id === target.source_id);
    if (localMembers.length && !targetLocal) {
      plan.conflicts.push({
        local: localMembers[0],
        target,
        localIds: localMembers.map(m => m.source_id),
        origIds: groupIds.slice(),
        targetIds: [target.source_id],
      });
    }

    groupIds.forEach(id => {
      plan.targetById.set(id, target.source_id);
      if (id !== target.source_id) plan.skips.set(id, "dup_batch");
    });
    if (groupIds.some(id => id !== target.source_id)) {
      plan.notes.push(`${target.source_id}：同一作品的其他语言版本已跳过`);
    }
  });

  return plan;
}

function langPairHTML(from, to) {
  const thumb = m => `<img class="lang-thumb" src="${m.cover_url || `/api/cover/${m.source_id}?v=2`}" alt="" loading="lazy">`;
  const body = m => `<div class="lang-pair__body">
      <b>${escHTML(m.title || m.source_id)}</b>
      <small>${escHTML(m.source_id)} · ${escHTML(m.lang_label || "未知语言")}</small>
    </div>`;
  return `<div class="lang-pair">${thumb(from)}${body(from)}
    <span class="lang-vs">${icon("next")}</span>
    ${thumb(to)}${body(to)}</div>`;
}

function langCardHTML(m, badge) {
  const rel = m.edition_group ? `版本组 ${escHTML(m.edition_group)}` : "同版本组";
  const orig = m.original_workno ? ` · 原作 ${escHTML(m.original_workno)}` : "";
  return `<div class="lang-card">
    <span class="lang-card__badge">${badge}</span>
    <img class="lang-card__cover" src="${m.cover_url || `/api/cover/${m.source_id}?v=2`}" alt="" loading="lazy">
    <div class="lang-card__body">
      <b>${escHTML(m.title || m.source_id)}</b>
      <small>${escHTML(m.source_id)} · ${escHTML(m.lang_label || "未知语言")}</small>
      ${m.release_date ? `<small>${escHTML(m.release_date)}</small>` : ""}
      <small class="lang-card__rel">${rel}${orig}</small>
    </div>
  </div>`;
}

function finishLang(value) {
  closeDialog("lang-modal");
  const r = langResolve;
  langResolve = null;
  if (r) r(value);
}

function askLangSwitch(pairs) {
  langMode = "switch";
  $("#lang-title").textContent = `检测到 ${pairs.length} 个作品有中文版本`;
  $("#lang-text").textContent = "存在可直接下载的中文版本，是否改为下载中文版？同一作品只会保留一个语言版本。";
  const box = $("#lang-compare");
  box.className = "lang-compare lang-compare--list";
  box.innerHTML = pairs.map(p => langPairHTML(p.from, p.to)).join("");
  $("#lang-switch-actions").classList.remove("hidden");
  $("#lang-dup-actions").classList.add("hidden");
  langKeepHold.reset();
  langOverwriteHold.reset();
  openDialog("lang-modal");
  return new Promise(res => { langResolve = res; });
}

function askLangConflict(conflict) {
  langMode = "dup";
  $("#lang-title").textContent = "该作品已有其他语言版本";
  $("#lang-text").textContent = "本地已存在同一作品的另一个语言版本。可长按保留本地，或长按覆盖为待下载版本。";
  const box = $("#lang-compare");
  box.className = "lang-compare";
  box.innerHTML = langCardHTML(conflict.local, "本地") +
    `<span class="lang-vs">${icon("next")}</span>` +
    langCardHTML(conflict.target, "将下载");
  $("#lang-switch-actions").classList.add("hidden");
  $("#lang-dup-actions").classList.remove("hidden");
  langKeepHold.reset();
  langOverwriteHold.reset();
  openDialog("lang-modal");
  return new Promise(res => { langResolve = res; });
}

const langKeepHold = initHoldButton($("#lang-keep-local"), {
  holdTime: 1200,
  onHold: () => finishLang("keep"),
});
const langOverwriteHold = initHoldButton($("#lang-overwrite-local"), {
  holdTime: 1200,
  onHold: () => finishLang("overwrite"),
});

// ============ 中文版检查 ============
const langAuditStartHold = initHoldButton($("#lang-audit-start"), {
  holdTime: 1200,
  onHold: () => submitLangUpdate(),
});

$("#lang-keep").addEventListener("click", () => finishLang(false));
$("#lang-switch").addEventListener("click", () => finishLang(true));
$("#lang-close").addEventListener("click", () => finishLang(langMode === "dup" ? "cancel" : false));
$("#lang-modal").addEventListener("click", e => {
  if (e.target && e.target.id === "lang-modal") finishLang(langMode === "dup" ? "cancel" : false);
});

// 统一入口：补齐语言数据 → 复核候选 → 切换确认 → 本地冲突处理
async function resolveLanguagePlan(ids) {
  await ensureLangData(ids);
  const siblings = new Set();
  ids.forEach(id => {
    const w = worksById.get(id) || {};
    (w.editions || []).forEach(e => { if (e && e.source_id) siblings.add(e.source_id); });
  });
  const { localIds, running } = await loadDownloadStatus([...new Set([...ids, ...siblings])]);

  const candidates = [];
  ids.forEach(id => {
    const w = worksById.get(id) || {};
    if (isZhWork(w) || !w.chinese_edition) return;
    candidates.push({ from: w, to: w.chinese_edition });
  });
  const verifiedPairs = await verifySwitchTargets(candidates);
  let plan = buildLanguagePlan(ids, localIds, running, verifiedPairs);

  if (plan.switchPairs.length) {
    const ok = await askLangSwitch(plan.switchPairs);
    if (!ok) {
      plan = buildLanguagePlan(ids, localIds, running, []);
    } else {
      plan.notes.push(`已改为下载 ${plan.switchPairs.length} 个作品的中文版本`);
    }
  }

  for (const c of plan.conflicts) {
    const choice = await askLangConflict(c);
    if (choice === "cancel") return null;
    if (choice === "keep") {
      c.origIds.forEach(id => plan.skips.set(id, "local_other"));
      c.targetIds.forEach(id => plan.skips.set(id, "local_other"));
      plan.notes.push(`${c.target.source_id}：保留本地版本 ${c.local.source_id}，已跳过`);
    } else {
      c.localIds.forEach(id => plan.removeIds.add(id));
      plan.notes.push(`${c.target.source_id}：覆盖本地版本 ${c.local.source_id}`);
    }
  }

  const targets = [];
  ids.forEach(id => {
    if (plan.skips.has(id)) return;
    const t = plan.targetById.get(id);
    if (!t || plan.skips.has(t)) return;
    if (!targets.includes(t)) targets.push(t);
  });
  return { plan, targets, localIds, running };
}

async function startBatchDownload(ids) {
  const order = ids.filter(Boolean);
  if (!order.length) return;

  // 失败任务重试与普通新下载分组；重试不参与“已下载”覆盖询问
  const retryGroups = new Map();
  const normalOrder = [];
  order.forEach(id => {
    const taskId = retryTaskByWork.get(id);
    if (taskId) {
      if (!retryGroups.has(taskId)) retryGroups.set(taskId, []);
      retryGroups.get(taskId).push(id);
    } else normalOrder.push(id);
  });

  const handled = new Set();
  const startedTasks = new Set();
  const errors = [];
  const notes = [];
  let toDownload = [];
  const overwrite = [];
  let removeIds = [];

  if (normalOrder.length) {
    // 先做语言版本检查：切换中文版、同作品只保留一个版本
    const resolved = await resolveLanguagePlan(normalOrder);
    if (!resolved) return;   // 用户在语言弹窗取消，中止启动
    const { plan, localIds } = resolved;
    toDownload = resolved.targets;
    removeIds = [...plan.removeIds];

    const { running } = resolved;
    normalOrder.filter(id => running.has(id) && !plan.skips.has(id)).forEach(id => handled.add(id));
    new Set(plan.notes).forEach(n => notes.push(n));

    const askIds = toDownload.filter(id => localIds.has(id));
    const skipped = new Set();
    if (askIds.length) {
      if (dupPolicy) {
        askIds.forEach(id => { if (dupPolicy === "skip") skipped.add(id); else overwrite.push(id); });
      } else {
        for (let i = 0; i < askIds.length; i++) {
          const id = askIds[i];
          const choice = await askDuplicate(askIds.slice(i), { index: i + 1, total: askIds.length });
          // 用户取消普通下载冲突询问时，重试也暂不启动
          if (choice === "cancel") return;
          if (choice === "skip") skipped.add(id); else overwrite.push(id);
          if (dupPolicy) {
            askIds.slice(i + 1).forEach(rest => {
              if (dupPolicy === "skip") skipped.add(rest); else overwrite.push(rest);
            });
            break;
          }
        }
      }
      toDownload = toDownload.filter(id => !skipped.has(id));
      skipped.forEach(id => handled.add(id));
    }

    // 明确跳过的作品（含语言版本重复）移出选择
    plan.skips.forEach((_, id) => handled.add(id));
    // 原始作品 ID 与下载目标对应，成功后一并清除选择
    plan.targetById.forEach((targetId, origId) => {
      if (toDownload.includes(targetId)) handled.add(origId);
    });
  }

  // 复用原失败任务 ID；后端会跳过下载中/已下载完成的作品，其余完整文件跳过、半成品断点续传
  for (const [taskId, workIds] of retryGroups) {
    try {
      const data = await api(`/api/download/tasks/${taskId}/retry`, {
        method: "POST", body: JSON.stringify({ ids: workIds }),
      });
      const started = data.work_ids || [];
      if (started.length) startedTasks.add(taskId);
      workIds.forEach(id => handled.add(id));
      const sk = data.skipped || {};
      const nDl = (sk.downloading || []).length;
      const nDone = (sk.downloaded || []).length;
      if (nDl) notes.push(`任务 #${taskId}：${nDl} 个作品正在下载，已跳过`);
      if (nDone) notes.push(`任务 #${taskId}：${nDone} 个作品已下载完成，已跳过`);
    } catch (e) {
      errors.push(`任务 #${taskId}：${e.message}`);
    }
  }

  if (toDownload.length) {
    try {
      const data = await api("/api/download", {
        method: "POST", body: JSON.stringify({ ids: toDownload, overwrite, remove_ids: removeIds }),
      });
      startedTasks.add(data.task_id);
      toDownload.forEach(id => handled.add(id));
    } catch (e) {
      errors.push(`新下载：${e.message}`);
    }
  }

  // 已成功启动、已在下载或明确跳过的作品移出选择；失败组保留以便再次操作
  handled.forEach(id => {
    selectedWorks.delete(id);
    retryTaskByWork.delete(id);
  });
  queueItems = queueItems.filter(it => selectedWorks.has(it.id));
  paintCardSelection();
  updateSelectedCount();

  if (startedTasks.size || !errors.length) closeDialog("queue-modal");
  if (startedTasks.size) {
    switchTab("download");
    startedTasks.forEach(taskId => startDownloadPolling(taskId));
  }
  if (errors.length) {
    showAlert("部分下载未能启动", errors.concat(notes).join("\n"));
  } else if (notes.length) {
    showAlert(startedTasks.size ? "部分作品已跳过" : "已全部跳过", notes.join("\n"));
  } else if (!startedTasks.size) {
    showAlert("已全部跳过", "选中的作品均已在本地或正在下载，本次没有需要下载的内容。");
  }
}

async function batchDownload() {
  if (!selectedWorks.size) return;
  try {
    await startBatchDownload(Array.from(selectedWorks));
  } catch (e) {
    showAlert("下载创建失败", e.message);
  }
}

// ============ 选择队列弹窗（点击「已选 N 项」） ============
let queueItems = [];
let queueActiveTasks = [];
let undoState = null;

function queueRowHTML(it, i) {
  const tag =
    (it.local ? `<span class="q-tag q-tag-done">已下载</span>` : "") +
    (it.downloading ? `<span class="q-tag q-tag-dl">下载中</span>` : "");
  return `<li class="q-row" data-id="${escHTML(it.id)}">
    <div class="q-drawer"><button class="q-del" type="button" tabindex="-1">删除</button></div>
    <div class="q-card">
      <span class="q-num">${i + 1}</span>
      <button class="q-handle" type="button" aria-label="拖动调整顺序">
        <svg class="ico" viewBox="0 0 24 24"><path d="M9 5h.01M9 12h.01M9 19h.01M15 5h.01M15 12h.01M15 19h.01"/></svg>
      </button>
      <span class="q-main"><b>${escHTML(it.title || it.id)}</b><small>${escHTML(it.id)}</small></span>
      ${tag}
    </div>
  </li>`;
}

function renderQueue() {
  const list = $("#queue-list");
  list.innerHTML = queueItems.length
    ? queueItems.map(queueRowHTML).join("")
    : `<li class="q-empty">队列为空</li>`;
  $("#queue-summary").textContent = `共 ${queueItems.length} 项`;
  $("#queue-start").disabled = !queueItems.length;

  const active = queueActiveTasks.filter(t => t.current_work);
  const box = $("#queue-active");
  if (active.length) {
    box.classList.remove("hidden");
    $("#queue-active-list").innerHTML = active.map(t =>
      `<li class="q-arow"><span class="q-tag q-tag-dl">下载中</span>` +
      `<span class="q-atitle">${escHTML(t.current_work)}</span>` +
      `<small>任务 #${t.id}</small></li>`
    ).join("");
  } else {
    box.classList.add("hidden");
    $("#queue-active-list").innerHTML = "";
  }
  bindQueueRows();
}

async function openQueueModal() {
  if (!selectedWorks.size) {
    showAlert("还没有选择", "先在搜索结果中勾选要下载的作品。");
    return;
  }
  if (undoState) hideUndo();
  const ids = Array.from(selectedWorks);
  openDialog("queue-modal");
  $("#queue-list").innerHTML = `<li class="q-empty">读取下载状态…</li>`;
  $("#queue-start").disabled = true;

  const { localIds, running, activeTasks } = await loadDownloadStatus(ids);
  queueActiveTasks = activeTasks;
  queueItems = ids.map(id => ({
    id,
    title: (worksById.get(id) || {}).title || "",
    local: localIds.has(id),
    downloading: running.has(id),
    retryTaskId: retryTaskByWork.get(id) || null,
  }));
  renderQueue();
}

// ---- 左滑删除 + 拖动排序 ----
function bindQueueRows() {
  const list = $("#queue-list");
  list.querySelectorAll(".q-row").forEach(row => {
    bindSwipe(row, row.querySelector(".q-card"), row.querySelector(".q-del"));
    bindReorder(row, row.querySelector(".q-card"), row.querySelector(".q-handle"));
  });
}

function bindSwipe(row, card, delBtn) {
  if (!card) return;
  let sx = 0, sy = 0, dx = 0, tracking = false, decided = false, horiz = false;

  card.addEventListener("pointerdown", e => {
    if (e.target.closest(".q-handle") || e.target.closest(".q-del")) return;
    if (row.classList.contains("shredding")) return;
    tracking = true; decided = false; horiz = false;
    sx = e.clientX; sy = e.clientY; dx = 0;
    card.style.transition = "none";
    card.setPointerCapture?.(e.pointerId);
  });
  card.addEventListener("pointermove", e => {
    if (!tracking) return;
    const mx = e.clientX - sx, my = e.clientY - sy;
    if (!decided) {
      if (Math.abs(mx) < 6 && Math.abs(my) < 6) return;
      decided = true;
      horiz = Math.abs(mx) > Math.abs(my);
      if (!horiz) { tracking = false; card.style.transition = ""; return; }
    }
    if (!horiz) return;
    // 超过抽屉宽度后加阻力
    const over = Math.max(0, -mx - 76);
    dx = Math.max(-108, Math.min(0, mx) + over * 0.28);
    card.style.transform = `translateX(${dx}px)`;
    // 红底只在左滑时显示，静止时隐藏避免卡片圆角透出红边
    row.classList.toggle("swiping", dx < -1);
  });
  const end = () => {
    if (!tracking) return;
    tracking = false;
    card.style.transition = "";
    if (horiz && dx < -96) { shredRow(row); return; }
    card.style.transform = "";
    dx = 0;
    setTimeout(() => row.classList.remove("swiping"), 280);
  };
  card.addEventListener("pointerup", end);
  card.addEventListener("pointercancel", end);
  if (delBtn) delBtn.addEventListener("click", () => shredRow(row));
}

function bindReorder(row, card, handle) {
  if (!handle || !card) return;
  const list = $("#queue-list");
  let dragging = false, grabOffset = 0, dy = 0;

  handle.addEventListener("pointerdown", e => {
    if (row.classList.contains("shredding")) return;
    e.preventDefault();
    dragging = true;
    dy = 0;
    grabOffset = e.clientY - row.getBoundingClientRect().top;
    row.classList.add("dragging");
    row.style.transition = "none";
    // 拖动中会移动行元素，指针捕获会失效，改用 window 监听
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  });

  // 按当前真实布局重算位移，交换后不会跳变
  function follow(clientY) {
    const layoutTop = row.getBoundingClientRect().top - dy;
    let next = clientY - grabOffset - layoutTop;
    // 限制在列表可视范围内，避免 transform 撑大滚动区域出现滚动条
    const lr = list.getBoundingClientRect();
    next = Math.max(lr.top - layoutTop,
      Math.min(lr.bottom - row.offsetHeight - layoutTop, next));
    dy = next;
    // 只用垂直位移并取整：scale/rotate/亚像素会让合成层文字按位图重采样而模糊
    row.style.transform = `translateY(${Math.round(dy)}px)`;
  }

  // 兄弟行被换位时用 FLIP 补一段过渡，避免生硬跳变
  function swap(mutate) {
    const others = Array.from(list.querySelectorAll(".q-row")).filter(r => r !== row);
    const before = new Map(others.map(r => [r, r.getBoundingClientRect().top]));
    mutate();
    others.forEach(r => {
      const d = before.get(r) - r.getBoundingClientRect().top;
      if (!d) return;
      r.style.transition = "none";
      r.style.transform = `translateY(${Math.round(d)}px)`;
      void r.offsetHeight;
      r.style.transition = "transform 240ms cubic-bezier(0.22, 1, 0.36, 1)";
      r.style.transform = "";
      setTimeout(() => { r.style.transition = ""; }, 260);
    });
  }

  function onMove(e) {
    if (!dragging) return;
    follow(e.clientY);
    row.style.zIndex = "6";

    // 跨过相邻行中线时交换位置
    const rows = Array.from(list.querySelectorAll(".q-row"));
    const i = rows.indexOf(row);
    const next = rows[i + 1], prev = rows[i - 1];
    if (next) {
      const r = next.getBoundingClientRect();
      if (e.clientY > r.top + r.height / 2) {
        swap(() => list.insertBefore(next, row));
        follow(e.clientY);
        return;
      }
    }
    if (prev) {
      const r = prev.getBoundingClientRect();
      if (e.clientY < r.top + r.height / 2) {
        swap(() => list.insertBefore(row, prev));
        follow(e.clientY);
      }
    }
  }

  const end = () => {
    if (!dragging) return;
    dragging = false;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", end);
    window.removeEventListener("pointercancel", end);
    row.classList.remove("dragging");
    // 交回 CSS 过渡，松手后平滑归位
    row.style.transition = "";
    row.style.transform = "";
    row.style.zIndex = "";
    const order = Array.from(list.querySelectorAll(".q-row")).map(r => r.dataset.id);
    if (order.length === queueItems.length) {
      queueItems = order.map(id => queueItems.find(it => it.id === id)).filter(Boolean);
      selectedWorks = new Set(order);
      list.querySelectorAll(".q-num").forEach((el, idx) => { el.textContent = idx + 1; });
    }
  };
}

// ---- 粉碎删除 + 撤销 ----
function shredRow(row) {
  if (!row || row.classList.contains("shredding")) return;
  const id = row.dataset.id;
  const item = queueItems.find(x => x.id === id);
  if (!item) return;

  row.classList.add("shredding");
  const card = row.querySelector(".q-card");
  card.style.visibility = "hidden";

  const strips = 14;
  for (let i = 0; i < strips; i++) {
    const strip = document.createElement("div");
    strip.className = "q-strip";
    strip.style.clipPath =
      `inset(0 ${100 - (i + 1) * (100 / strips)}% 0 ${i * (100 / strips)}%)`;
    strip.style.setProperty("--d", `${i * 18}ms`);
    strip.style.setProperty("--dx", `${(Math.random() - 0.5) * 46}px`);
    strip.style.setProperty("--rot", `${(Math.random() - 0.5) * 26}deg`);
    strip.innerHTML = card.outerHTML;
    const inner = strip.querySelector(".q-card");
    if (inner) inner.style.visibility = "visible";
    strip.querySelectorAll("button").forEach(b => b.setAttribute("tabindex", "-1"));
    row.appendChild(strip);
  }

  const total = 720 + strips * 18;
  setTimeout(() => {
    row.classList.add("gone");
    setTimeout(() => row.remove(), 200);
    queueItems = queueItems.filter(x => x.id !== id);
    selectedWorks.delete(id);
    retryTaskByWork.delete(id);
    paintCardSelection();
    updateSelectedCount();
    if (!$("#queue-modal").classList.contains("hidden")) renderQueue();
    showUndo(item);
  }, total);
}

function showUndo(item) {
  clearTimeout(undoState?.timer);
  const el = $("#queue-undo");
  el.classList.remove("hidden", "paused");
  const bar = el.querySelector(".undo-bar");
  bar.style.animation = "none";
  void bar.offsetWidth;
  bar.style.animation = "";
  $("#undo-text").textContent = `已移出队列：${item.title || item.id}`;
  $("#undo-btn").onclick = () => { restoreQueueItem(item); hideUndo(); };
  undoState = { item, el, timer: setTimeout(hideUndo, 8000) };

  // 鼠标停在提示条上时暂停计时，移开后重新给满 8 秒
  el.onpointerenter = () => {
    if (!undoState) return;
    clearTimeout(undoState.timer);
    undoState.timer = null;
    el.classList.add("paused");
  };
  el.onpointerleave = () => {
    if (!undoState || undoState.timer) return;
    el.classList.remove("paused");
    bar.style.animation = "none";
    void bar.offsetWidth;
    bar.style.animation = "";
    undoState.timer = setTimeout(hideUndo, 8000);
  };
}

function hideUndo() {
  if (undoState?.timer) clearTimeout(undoState.timer);
  undoState = null;
  const el = $("#queue-undo");
  el.classList.remove("paused");
  el.classList.add("hidden");
}

function restoreQueueItem(item) {
  if (!queueItems.some(x => x.id === item.id)) queueItems.push(item);
  selectedWorks.add(item.id);
  if (item.retryTaskId) retryTaskByWork.set(item.id, item.retryTaskId);
  paintCardSelection();
  updateSelectedCount();
  if (!$("#queue-modal").classList.contains("hidden")) renderQueue();
}

$("#sel-info-btn").addEventListener("click", openQueueModal);
$("#queue-start").addEventListener("click", async () => {
  try {
    await startBatchDownload(queueItems.map(it => it.id));
  } catch (e) {
    showAlert("下载创建失败", e.message);
  }
});

async function showWorkDetail(sourceId, from = "") {
  const modal = $("#modal");
  const body = $("#modal-body");
  modal.classList.remove("hidden");
  body.innerHTML = "加载中...";
  $("#modal-close").onclick = () => modal.classList.add("hidden");
  // 「生成字幕」入口的音轨列表与作品信息并行拉取，弹窗打开更快
  const tracksReq = from === "subm"
    ? api(`/api/subtitles/tracks/${encodeURIComponent(sourceId)}`).catch(() => null)
    : Promise.resolve(null);
  try {
    const data = await api(`/api/player/work/${sourceId}`);
    const w = data.work;
    const files = data.files;
    const subs = data.subtitles || [];
    const canPlay = data.has_local;
    const playHref = `/player.html?id=${sourceId}${from ? `&from=${encodeURIComponent(from)}` : ""}`;

    // 「生成字幕」入口：音轨翻译勾选放在这里，弹窗底部只保留打开播放页
    if (from === "subm") {
      const tr = await tracksReq;
      let tracks = (tr && tr.tracks) || [];

      const rows = tracks.map(t => {
        const on = t.translate !== false;
        const hasJa = !!t.has_ja;
        const hasZh = !!t.has_zh;
        return `<div class="file-item${on ? "" : " muted"}${hasJa ? " has-ja" : ""}${hasZh ? " has-zh" : ""}">
          <button type="button" class="track-check" data-rel="${(t.rel_path || "").replace(/"/g, "&quot;")}"
                  role="checkbox" aria-checked="${on ? "true" : "false"}" title="${hasZh ? "已有中文字幕" : hasJa ? "已有日文字幕" : "勾选表示该音频参与翻译"}">
            <span class="track-check__box">
              <svg class="ico track-check__tick" aria-hidden="true"><use href="/icons.svg#i-check"></use></svg>
            </span>
            <span class="track-check__name">${t.filename}</span>
          </button>
          ${hasZh ? `<span class="track-zh-tag">中文</span>` : ""}
          ${hasJa ? `<span class="track-ja-tag">日文</span>` : ""}
          <span class="ext">${t.count > 1 ? `×${t.count}` : ""}</span>
        </div>`;
      }).join("") || `<div class="empty">该作品没有可参与翻译的音轨</div>`;

      body.innerHTML = `
        <h2>${w.source_id} — ${w.title}</h2>
        <p>发布: ${(w.release_date || "").substring(0, 10)} | 售价: ${Number(w.price || 0).toLocaleString("en-US")} JPY | 销量: ${w.dl_count || 0} | 有字幕: ${w.has_subtitle ? "是" : "否"}</p>
        <div class="subm-track-list">${rows}</div>
        <div style="margin-top:16px; display:flex; gap:10px;">
          <button type="button" class="btn-accent" id="subm-add-queue">${icon("plus")}添加到队列</button>
          ${canPlay ? `<a href="${playHref}" class="btn-primary" style="text-decoration:none">${icon("play")}打开播放页</a>` : ""}
        </div>`;

      body.querySelectorAll(".track-check").forEach(btn => {
        btn.addEventListener("click", async () => {
          const next = btn.getAttribute("aria-checked") !== "true";
          const item = btn.closest(".file-item");
          const apply = (on) => {
            btn.setAttribute("aria-checked", on ? "true" : "false");
            if (item) item.classList.toggle("muted", !on);
          };
          apply(next);
          try {
            await api(`/api/subtitles/tracks/${encodeURIComponent(sourceId)}`, {
              method: "POST",
              body: JSON.stringify({ rel_path: btn.dataset.rel, translate: next }),
            });
          } catch (err) {
            apply(!next);
          }
        });
      });

      const addBtn = body.querySelector("#subm-add-queue");
      if (addBtn) {
        addBtn.addEventListener("click", async () => {
          addBtn.disabled = true;
          try {
            const r = await api("/api/subtitles/queue", {
              method: "POST",
              body: JSON.stringify({ ids: [sourceId] }),
            });
            toast(r && r.queued ? "已添加到生成队列" : "该作品已在生成队列中");
            modal.classList.add("hidden");
            loadSubm();
          } catch (err) {
            addBtn.disabled = false;
            toast("加入队列失败：" + err.message);
          }
        });
      }
      return;
    }

    const fileList = files.filter(f => f.status === "downloaded").map(f => {
      return `<li><code>${f.filename}</code> <span style="color:#8892a6">${humanSize(f.size)}</span></li>`;
    }).join("") || "<li>无下载文件</li>";

    const subsList = subs.map(s => {
      const conf = Math.round((s.confidence || 0) * 100);
      const lang = s.language || "?";
      const from = s.source === "api" ? "在线" : "本地";
      return `<li>${s.filename} <span style="color:#8892a6">[${from}, ${lang}, ${conf}%]</span></li>`;
    }).join("") || "<li>无可用字幕</li>";

    body.innerHTML = `
      <h2>${w.source_id} — ${w.title}</h2>
      <p>发布: ${(w.release_date || "").substring(0, 10)} | 售价: ${Number(w.price || 0).toLocaleString("en-US")} JPY | 销量: ${w.dl_count || 0} | 有字幕: ${w.has_subtitle ? "是" : "否"}</p>
      <h4>本地文件 (${files.filter(f => f.status === "downloaded").length}/${files.length})</h4>
      <ul>${fileList}</ul>
      <h4>字幕匹配 (${subs.length})</h4>
      <ul>${subsList}</ul>
      <div style="margin-top:16px; display:flex; gap:10px;">
        ${canPlay ? `<a href="${playHref}" class="btn-primary" style="text-decoration:none">${icon("play")}打开播放页</a>` : ""}
        ${canPlay ? "" : `<button class="btn-accent" onclick="downloadOne('${sourceId}')">${icon("download")}下载此作品</button>`}
        <button class="btn-danger" onclick="deleteLocal('${sourceId}')" ${canPlay ? "" : "disabled"}>${icon("trash")}删除本地</button>
      </div>`;
  } catch (e) {
    body.innerHTML = `<p>加载失败: ${e.message}</p>`;
  }
}

async function downloadOne(sourceId) {
  try {
    const resolved = await resolveLanguagePlan([sourceId]);
    if (!resolved) return;   // 用户在语言弹窗取消
    const { plan, targets } = resolved;
    if (!targets.length) {
      $("#modal").classList.add("hidden");
      showAlert("已跳过", new Set(plan.notes).size
        ? [...new Set(plan.notes)].join("\n")
        : "该作品已有中文版本或正在下载，本次没有需要下载的内容。");
      return;
    }
    const data = await api("/api/download", {
      method: "POST",
      body: JSON.stringify({ ids: targets, remove_ids: [...plan.removeIds] }),
    });
    $("#modal").classList.add("hidden");
    switchTab("download");
    startDownloadPolling(data.task_id);
  } catch (e) {
    alert("下载创建失败: " + e.message);
  }
}

// ============ 下载任务轮询 ============
let downloadPollers = new Map();
const downloadTasksById = new Map();

function maybeStorageAlert(t) {
  if (!t || t.status !== "failed") return;
  const err = t.error || "";
  if (!err.includes("[STORAGE_FULL]") || storageWarned.has(t.id)) return;
  storageWarned.add(t.id);
  showAlert("存储空间不足", err.replace("[STORAGE_FULL]", "").trim());
}

async function loadDownloads() {
  try {
    const data = await api("/api/download/tasks");
    data.tasks.forEach(maybeStorageAlert);
    renderTasks(data.tasks);
    // 对 running/pending 任务启动轮询
    data.tasks.filter(t => ["pending","running","pausing"].includes(t.status)).forEach(t => {
      if (!downloadPollers.has(t.id)) startDownloadPolling(t.id);
    });
  } catch (e) {
    $("#download-queue").innerHTML = `<div class='empty'>加载失败: ${e.message}</div>`;
  }
}

// 三个抽屉：下载中 / 暂停中 / 已完成（默认收起）；失败任务不归入抽屉，直接置顶
const DL_GROUPS = [
  { key: "active", title: "下载中", statuses: ["pending", "running", "pausing"], open: true },
  { key: "paused", title: "暂停中", statuses: ["paused"], open: true },
  { key: "done", title: "已完成", statuses: ["done", "cancelled"], open: false },
];

function renderTasks(tasks) {
  const container = $("#download-queue");
  const empty = $("#download-empty");
  const meta = $("#dl-head-meta");
  if (!container) return;
  tasks.forEach(t => downloadTasksById.set(t.id, t));

  // 保留用户手动展开/收起的状态
  const prevOpen = {};
  container.querySelectorAll(".dl-group").forEach(g => {
    prevOpen[g.dataset.group] = g.dataset.open === "true";
  });

  if (!tasks.length) {
    container.innerHTML = "";
    if (empty) empty.classList.remove("hidden");
    if (meta) meta.textContent = "";
    return;
  }
  if (empty) empty.classList.add("hidden");

  const failedItems = tasks.filter(t => t.status === "failed");
  const groups = DL_GROUPS.map(g => ({
    ...g,
    items: tasks.filter(t => g.statuses.includes(t.status)),
  }));
  if (meta) {
    meta.textContent = [
      failedItems.length ? `失败 ${failedItems.length}` : "",
      ...groups.map(g => `${g.title} ${g.items.length}`),
    ].filter(Boolean).join("  ·  ");
  }
  const pinned = failedItems.length
    ? `<div class="dl-pinned">${failedItems.map(taskHTML).join("")}</div>`
    : "";
  container.innerHTML = pinned + groups.map(g => {
    const open = g.key in prevOpen ? prevOpen[g.key] : g.open;
    return `
      <section class="dl-group" data-group="${g.key}" data-open="${open ? "true" : "false"}">
        <button class="dl-group__head" type="button" aria-expanded="${open}">
          <span>${g.title}</span>
          <span class="dl-group__count">${g.items.length} 项</span>
          <span class="dl-group__chev">${icon("chevron-down")}</span>
        </button>
        <div class="dl-group__body">${g.items.map(taskHTML).join("")}</div>
      </section>`;
  }).join("");

  container.querySelectorAll(".dl-group__head").forEach(btn => {
    btn.addEventListener("click", () => {
      const group = btn.closest(".dl-group");
      const open = group.dataset.open !== "true";
      group.dataset.open = open ? "true" : "false";
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });

  // 事件委托：轮询会用 outerHTML 重绘卡片，逐个绑定会在刷新后失效
  container.onclick = async (e) => {
    const btn = e.target.closest("button[data-id]");
    if (!btn || btn.disabled) return;
    const id = btn.dataset.id;
    let action;
    if (btn.classList.contains("pause-btn")) action = "pause";
    else if (btn.classList.contains("resume-btn")) action = "resume";
    else if (btn.classList.contains("cancel-btn")) action = "cancel";
    else if (btn.classList.contains("retry-btn")) action = "retry";
    else return;
    if (action === "retry") {
      const task = downloadTasksById.get(Number(id));
      if (!task || task.status !== "failed") return;
      btn.disabled = true;
      try {
        const data = await api(`/api/download/tasks/${id}/retry`, {
          method: "POST", body: JSON.stringify({ ids: task.work_ids || [] }),
        });
        const started = (data.work_ids || []).length;
        const skipped = data.skipped || {};
        const notes = [];
        if ((skipped.downloading || []).length) {
          notes.push(`${skipped.downloading.length} 个作品正在其他任务中下载，已跳过`);
        }
        if ((skipped.downloaded || []).length) {
          notes.push(`${skipped.downloaded.length} 个作品已下载完成，已跳过`);
        }
        await loadDownloads();
        if (started) {
          toast(`已重新开始 ${started} 个作品`);
          startDownloadPolling(Number(id));
          if (notes.length) showAlert("部分作品已跳过", notes.join("\n"));
        } else {
          showAlert("没有可重新下载的作品", notes.join("\n") || "对应文件均已下载完成");
        }
      } catch (err) {
        let msg = err.message || "未知错误";
        try {
          const body = JSON.parse(msg.replace(/^\d+:\s*/, ""));
          if (body.message) msg = body.message;
          else if (body.error === "invalid_state") msg = "任务状态已变化，请刷新后重试";
          else if (body.error === "task_active") msg = "任务仍在停止中，请稍后再试";
        } catch (_) { /* 保留原始错误信息 */ }
        btn.disabled = false;
        showAlert("重新下载未能启动", msg);
      }
      return;
    }
    btn.disabled = true;
    try {
      if (action === "cancel") {
        await api(`/api/download/tasks/${id}`, { method: "DELETE" });
      } else {
        await api(`/api/download/tasks/${id}/${action}`, { method: "POST" });
      }
    } catch (err) {
      btn.disabled = false;
      alert("操作失败: " + err.message);
      return;
    }
    loadDownloads();
  };
}

const STATUS_LABEL = {
  pending: "排队中", running: "进行中", done: "已完成",
  failed: "失败", cancelled: "已取消",
  pausing: "暂停中", paused: "已暂停",
};

function taskHTML(t) {
  const s = t.status;
  const progress = t.progress_json || {};
  const done = progress.done_entries || 0;
  const total = progress.total_entries || t.work_ids.length;
  const pct = total ? Math.round(done / total * 100) : 0;
  const summary = progress.summary || {};
  return `
    <div class="task-card status-${s}" data-id="${t.id}">
      <div class="task-header">
        <span class="task-id">#${t.id} ${t.current_work ? `(${t.current_work})` : ""}</span>
        <span class="task-status status-${s}">${STATUS_LABEL[s] || s}</span>
      </div>
      <div class="progress-bar"><div class="fill" style="width:${pct}%"></div></div>
      <div class="task-summary">
        <span>作品: ${t.work_ids.join(", ")}</span>
        <span>${done}/${total}</span>
        ${summary.ok ? `<span>${icon("check")} ${summary.ok}</span>` : ""}
        ${summary.skip ? `<span>${icon("next")} ${summary.skip}</span>` : ""}
        ${summary.fail ? `<span>${icon("x")} ${summary.fail}</span>` : ""}
      </div>
      <div class="task-actions">
        ${["pending","running"].includes(s) ? `<button class="btn-ghost pause-btn" data-id="${t.id}">暂停</button>` : ""}
        ${s === "pausing" ? `<button class="btn-ghost" disabled>暂停中…</button>` : ""}
        ${s === "paused" ? `<button class="btn-primary resume-btn" data-id="${t.id}">继续</button>
          <button class="btn-danger cancel-btn" data-id="${t.id}">取消</button>` : ""}
        ${s === "failed" ? `<button class="btn-ghost retry-btn" data-id="${t.id}" title="重新下载" aria-label="重新下载">${icon("refresh")}重新下载</button>` : ""}
        ${s === "failed" || s === "cancelled" ? `<span class="task-error">${esc((t.error || "").replace("[STORAGE_FULL]", "").trim())}</span>` : ""}
      </div>
    </div>`;
}

function startDownloadPolling(taskId) {
  if (downloadPollers.has(taskId)) return;
  const poll = setInterval(async () => {
    try {
      const t = await api(`/api/download/tasks/${taskId}`);
      downloadTasksById.set(t.id, t);
      maybeStorageAlert(t);
      // 更新对应卡片
      const card = document.querySelector(`.task-card[data-id="${taskId}"]`);
      if (card) card.outerHTML = taskHTML(t);
      if (["done","failed","cancelled","paused"].includes(t.status)) {
        clearInterval(poll);
        downloadPollers.delete(taskId);
        // 状态归入其它抽屉：整页重排一次
        loadDownloads();
      }
    } catch {}
  }, 2000);
  downloadPollers.set(taskId, poll);
}

// ============ 本地资源库 ============
const libQEl = $("#lib-q");
const libClearBtn = $("#lib-clear");
function syncLibClear() {
  libClearBtn.classList.toggle("show", !!libQEl.value);
}
libQEl.addEventListener("input", syncLibClear);
libClearBtn.addEventListener("click", () => {
  libQEl.value = "";
  libQEl.focus();
  syncLibClear();
  loadLibrary();
});

$("#lib-go").addEventListener("click", () => loadLibrary());
libQEl.addEventListener("keydown", e => { if (e.key === "Enter") loadLibrary(); });
// 有无字幕 / 排序改动后立即刷新
["lib-sub", "lib-order", "lib-sort"].forEach(id => {
  $("#" + id).addEventListener("change", (e) => {
    if (id === "lib-order") {
      if (e.target.value === "random") libRandomSeed = newSeed();
      $("#lib-sort").disabled = e.target.value === "random";
    }
    loadLibrary();
  });
});
bindRandomReshuffle($("#lib-order"), () => {
  libRandomSeed = newSeed();
  loadLibrary();
});
$("#lib-page-size").addEventListener("change", () => {
  libPageSize = $("#lib-page-size").value;
  scrollPageTop();
  loadLibrary();
});
setupDateRange("#lib-date-presets", "#lib-from", "#lib-to", () => loadLibrary());
// 标签 / VA / 社团输入：回车即并入筛选条件（清空输入框，生成可删除的标签）
["lib-tag", "lib-va", "lib-circle"].forEach(id => {
  $("#" + id).addEventListener("keydown", e => { if (e.key === "Enter") loadLibrary(); });
});

// 本地资源输入框里的标签：查询时并入筛选条件并清空输入，与搜索发现一致
function promoteLibFilterInputs() {
  let changed = false;
  [["tag", "#lib-tag"], ["va", "#lib-va"], ["circle", "#lib-circle"]].forEach(([kind, sel]) => {
    const el = $(sel);
    if (!el) return;
    const val = el.value.trim();
    if (!val) return;
    el.value = "";
    if (!libFilters[kind].includes(val)) libFilters[kind].push(val);
    libFilters[NEG_KEY[kind]] = libFilters[NEG_KEY[kind]].filter(v => v !== val);
    changed = true;
  });
  if (changed) renderLibActiveFilters();
}
$("#lib-rescan").addEventListener("click", async () => {
  $("#lib-rescan").textContent = "扫描中...";
  try {
    const r = await api("/api/library/scan", { method: "POST" });
    alert(`扫描完成: 新发现 ${r.newly_found}, 清理 ${r.removed_stale}`);
    loadLibrary();
  } catch (e) { alert("扫描失败: " + e.message); }
  $("#lib-rescan").innerHTML = icon("refresh") + "扫描存储";
});

// ============ 中文版检查 ============
let langAuditTimer = 0;
let langAuditRunning = false;

// 有确定总量时圆弧直接表示真实进度；否则保留旋转弧。离开运行态必须清除内联样式。
function setSmkProgress(mark, ratio) {
  const ring = mark && mark.querySelector(".smk__ring");
  if (!ring) return;
  ring.style.removeProperty("stroke-dasharray");
  ring.style.removeProperty("animation");
  if (!Number.isFinite(ratio)) return;
  const p = Math.max(0, Math.min(1, ratio));
  ring.style.strokeDasharray = `${p} ${1 - p}`;
  ring.style.animation = "none";
}

function setLangAuditState(status, label, progress) {
  const mark = $("#lang-audit-mark");
  const text = $("#lang-audit-label");
  const pct = $("#lang-audit-pct");
  const btn = $("#lib-lang-audit");
  if (mark) {
    mark.dataset.status = status;
    setSmkProgress(mark, status === "running" ? progress : null);
  }
  if (text) text.textContent = label;
  if (pct) {
    pct.textContent = status === "running" && Number.isFinite(progress)
      ? `${Math.round(progress * 100)}%` : "";
  }
  if (btn) btn.dataset.state = status;
}

function renderLangAuditInfo(s) {
  if (!s) { setLangAuditState("idle", "检查中文版"); return; }
  if (s.running) {
    langAuditRunning = true;
    const total = Number(s.total) || 0;
    const checked = Number(s.checked) || 0;
    setLangAuditState("running", "检查中", total > 0 ? Math.min(1, checked / total) : null);
    return;
  }
  langAuditRunning = false;
  if (s.cancelled) {
    setLangAuditState("idle", "检查中文版");
  } else if (Number(s.error_count) > 0 && !Number(s.found)) {
    setLangAuditState("error", "检查失败");
  } else if (Number(s.found) > 0) {
    setLangAuditState("repair", `可更新 ${s.found}`);
  } else if (s.finished_at) {
    setLangAuditState("done", "无可用中文版");
  } else {
    setLangAuditState("idle", "检查中文版");
  }
}

async function startLangAudit() {
  // 检查进行中再次点击 = 取消
  if (langAuditRunning) {
    try {
      await api("/api/library/lang-audit", {
        method: "POST", body: JSON.stringify({ action: "cancel" }),
      });
    } catch (e) { /* 忽略 */ }
    return;
  }
  setLangAuditState("running", "检查中");
  try {
    await api("/api/library/lang-audit", { method: "POST", body: JSON.stringify({}) });
  } catch (e) {
    if (!String(e.message).startsWith("409")) {
      setLangAuditState("error", "检查失败");
      alert("检查启动失败: " + e.message);
      return;
    }
  }
  clearInterval(langAuditTimer);
  langAuditTimer = setInterval(pollLangAudit, 1000);
  pollLangAudit();
}

async function pollLangAudit() {
  try {
    const s = await api("/api/library/lang-audit");
    renderLangAuditInfo(s);
    if (!s.running) {
      clearInterval(langAuditTimer);
      langAuditTimer = 0;
      if (s.candidates && s.candidates.length && !s.cancelled) openLangAuditModal(s.candidates);
    }
  } catch (e) {
    clearInterval(langAuditTimer);
    langAuditTimer = 0;
    langAuditRunning = false;
    setLangAuditState("error", "检查失败");
  }
}

// 刷新页面后恢复上次检查状态；若仍在运行则继续轮询
async function loadLangAuditStatus() {
  try {
    const s = await api("/api/library/lang-audit");
    renderLangAuditInfo(s);
    if (s && s.running && !langAuditTimer) {
      langAuditTimer = setInterval(pollLangAudit, 1000);
    }
  } catch (e) {
    // 从未启动过检查时忽略
  }
}

function auditItemHTML(c) {
  return `<label class="lang-audit-item">
    <input type="checkbox" class="lang-audit-cb" data-new="${escHTML(c.new_id)}" data-old="${escHTML(c.old_id)}" checked>
    <img class="lang-thumb" src="${escHTML(c.old_cover)}" alt="" loading="lazy">
    <div class="lang-audit-meta">
      <b>${escHTML(c.old_title || c.old_id)}</b>
      <small>${escHTML(c.old_id)} · ${escHTML(c.old_lang_label || "原语言")}</small>
      <small>→ ${escHTML(c.new_title || c.new_id)} · ${escHTML(c.new_id)}</small>
      <small>需下载 ${c.download_files}/${c.total_files} 个 · 约 ${humanSize(c.download_bytes)}（复用 ${c.reuse_files} 个）</small>
      ${c.has_generated ? `<small class="lang-audit-gen">${c.generated_dropped ? "已按设置丢弃自生成中字" : "含自生成中字，将保留"}</small>` : ""}
    </div>
    <img class="lang-thumb" src="${escHTML(c.new_cover)}" alt="" loading="lazy">
  </label>`;
}

function openLangAuditModal(cands) {
  $("#lang-audit-summary").textContent =
    `共 ${cands.length} 个作品可更新为中文版。更新会复用旧版音频/图片，仅下载新增文件；中文版完整性校验通过后删除旧版本。`;
  $("#lang-audit-list").innerHTML = cands.map(auditItemHTML).join("");
  $("#lang-audit-all").checked = true;
  langAuditStartHold.reset();
  openDialog("lang-audit-modal");
}

async function submitLangUpdate() {
  const picked = [...$$(".lang-audit-cb")].filter(cb => cb.checked);
  if (!picked.length) {
    closeDialog("lang-audit-modal");
    return;
  }
  const ids = picked.map(cb => cb.dataset.new);
  const reuse_from = {};
  const purge_after = [];
  picked.forEach(cb => {
    reuse_from[cb.dataset.new] = cb.dataset.old;
    if (!purge_after.includes(cb.dataset.old)) purge_after.push(cb.dataset.old);
  });
  try {
    const data = await api("/api/download", {
      method: "POST",
      body: JSON.stringify({ ids, reuse_from, purge_after, filter: { source: "lang_update" } }),
    });
    closeDialog("lang-audit-modal");
    switchTab("download");
    startDownloadPolling(data.task_id);
  } catch (e) {
    alert("更新创建失败: " + e.message);
    langAuditStartHold.reset();
  }
}

$("#lib-lang-audit").addEventListener("click", startLangAudit);
$("#lang-audit-all").addEventListener("change", () => {
  const on = $("#lang-audit-all").checked;
  $$(".lang-audit-cb").forEach(cb => { cb.checked = on; });
});
$("#lang-audit-list").addEventListener("change", e => {
  if (!e.target.classList.contains("lang-audit-cb")) return;
  const all = [...$$(".lang-audit-cb")];
  $("#lang-audit-all").checked = all.length > 0 && all.every(cb => cb.checked);
});
$("#lang-audit-close").addEventListener("click", () => closeDialog("lang-audit-modal"));
$("#lang-audit-cancel").addEventListener("click", () => closeDialog("lang-audit-modal"));
$("#lang-audit-modal").addEventListener("click", e => {
  if (e.target && e.target.id === "lang-audit-modal") closeDialog("lang-audit-modal");
});

// 本地库请求序号：快速连续筛选时只采用最后一次请求的结果
let libSeq = 0;
let libPage = 1;
let libPageSize = "20";
const libSelected = new Set();

function updateLibSelectionUI() {
  const count = $("#lib-bulk-count");
  if (count) count.textContent = String(libSelected.size);
  if (libSelected.size > 0) showBulkbar(); else hideBulkbar();
}

function showBulkbar() {
  const bar = $("#lib-bulk");
  if (!bar) return;
  bar.classList.remove("hidden", "leaving", "in");
  void bar.offsetWidth;
  requestAnimationFrame(() => bar.classList.add("in"));
}

function hideBulkbar() {
  const bar = $("#lib-bulk");
  if (!bar || bar.classList.contains("hidden")) return;
  bar.classList.remove("in");
  bar.classList.add("leaving");
  setTimeout(() => {
    if (libSelected.size > 0) return;
    bar.classList.add("hidden");
    bar.classList.remove("leaving");
  }, 300);
}

function clearLibSelection() {
  libSelected.clear();
  document.querySelectorAll("#lib-grid .work-card.selected").forEach(card => {
    card.classList.remove("selected");
    const check = card.querySelector(".check");
    if (check) check.innerHTML = "";
  });
  updateLibSelectionUI();
}

async function libBulkQueue() {
  const ids = [...libSelected];
  if (!ids.length) return;
  const btn = $("#lib-bulk-queue");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/api/subtitles/queue", {
      method: "POST", body: JSON.stringify({ ids }),
    });
    const skipN = (res.skipped || []).length;
    toast(`已加入队列 ${res.queued ?? 0} 个作品`
      + (skipN ? `，${skipN} 个已有中字已跳过` : ""));
    clearLibSelection();
  } catch (e) {
    toast("加入队列失败：" + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 删除弹窗的通用上下文：ids 为待删除作品，done 为成功后的收尾
let deleteCtx = null;

function openDeleteDialog(ids, done) {
  if (!ids.length) return;
  deleteCtx = { ids: [...ids], done };
  const cnt = $("#lib-del-count");
  if (cnt) cnt.textContent = String(ids.length);
  const pw = $("#lib-del-pw");
  if (pw) pw.value = "";
  const hint = $("#lib-del-hint");
  if (hint) hint.textContent = "";
  $("#lib-del-modal").classList.remove("hidden");
  if (pw) setTimeout(() => pw.focus(), 40);
}

function openLibDeleteDialog() {
  openDeleteDialog([...libSelected], () => {
    clearLibSelection();
    loadLibrary();
  });
}

function openSubmDeleteDialog() {
  openDeleteDialog([...submSelected], async () => {
    submSelected.clear();
    document.querySelectorAll("#subm-queue .work-card.selected").forEach(card => {
      card.classList.remove("selected");
      const check = card.querySelector(".check");
      if (check) check.innerHTML = "";
    });
    updateSubmSelectionUI();
    await loadSubm();
  });
}

async function libDeleteConfirm() {
  if (!deleteCtx) return;
  const { ids, done } = deleteCtx;
  if (!ids.length) return;
  const pw = $("#lib-del-pw");
  const hint = $("#lib-del-hint");
  const btn = $("#lib-del-confirm");
  if (btn) btn.disabled = true;
  const password = pw ? pw.value : "";
  let ok = 0;
  let firstErr = "";
  for (const sid of ids) {
    try {
      await api(`/api/library/works/${encodeURIComponent(sid)}`, {
        method: "DELETE", body: JSON.stringify({ password }),
      });
      ok += 1;
    } catch (e) {
      if (!firstErr) firstErr = e.message;
    }
  }
  if (btn) btn.disabled = false;
  if (ok === ids.length) {
    closeDialog("lib-del-modal");
    deleteCtx = null;
    toast(`已删除 ${ok} 个作品`);
    if (done) await done();
    return;
  }
  if (/^401/.test(firstErr)) {
    if (hint) hint.textContent = "密码错误";
    return;
  }
  toast(`删除失败：${firstErr || "未知错误"}`);
}

const HOLD_DELETE_MS = 650;

/* 长按删除：红色液体从左向右填充，波浪前沿滚动，松手回弹 */
function bindHoldDelete(btn, isReady, onComplete) {
  if (!btn) return;
  let raf = 0;
  let active = false;
  let done = false;
  let startAt = 0;
  let fromP = 0;
  let pid = null;

  const setP = p => btn.style.setProperty("--hold-p", String(p));

  const animateBack = () => {
    const begin = performance.now();
    const from = fromP;
    const tick = now => {
      const t = Math.min(1, (now - begin) / 220);
      setP(from * (1 - t) ** 3);
      if (t < 1) raf = requestAnimationFrame(tick);
      else { raf = 0; fromP = 0; }
    };
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(tick);
  };

  const cancel = () => {
    if (!active) return;
    active = false;
    fromP = parseFloat(btn.style.getPropertyValue("--hold-p")) || 0;
    btn.classList.remove("pressing");
    if (pid !== null) {
      try { btn.releasePointerCapture(pid); } catch (_) {}
      pid = null;
    }
    animateBack();
  };

  btn.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    if (!isReady()) return;
    const rect = btn.getBoundingClientRect();
    btn.style.setProperty("--hold-w", `${rect.width}px`);
    btn.style.setProperty("--hold-h", `${rect.height}px`);
    active = true;
    done = false;
    startAt = performance.now();
    pid = e.pointerId;
    btn.classList.add("pressing");
    try { btn.setPointerCapture(pid); } catch (_) {}
    cancelAnimationFrame(raf);
    const tick = now => {
      if (!active) return;
      const p = Math.min(1, (now - startAt) / HOLD_DELETE_MS);
      fromP = p;
      setP(p);
      if (p >= 1) {
        active = false;
        done = true;
        raf = 0;
        btn.classList.remove("pressing");
        if (pid !== null) {
          try { btn.releasePointerCapture(pid); } catch (_) {}
          pid = null;
        }
        setTimeout(animateBack, 90);
        onComplete();
      } else {
        raf = requestAnimationFrame(tick);
      }
    };
    raf = requestAnimationFrame(tick);
  });

  ["pointerup", "pointercancel", "lostpointercapture"].forEach(ev =>
    btn.addEventListener(ev, cancel));

  btn.addEventListener("click", () => {
    if (!done) toast("长按删除按钮确认删除");
    done = false;
  });
}

function bindLibBulk() {
  const del = $("#lib-bulk-del");
  const queue = $("#lib-bulk-queue");
  const clear = $("#lib-bulk-clear");
  const confirm = $("#lib-del-confirm");

  if (queue) queue.addEventListener("click", libBulkQueue);
  if (clear) clear.addEventListener("click", clearLibSelection);
  if (confirm) confirm.addEventListener("click", libDeleteConfirm);

  if (del) bindHoldDelete(del, () => libSelected.size > 0, openLibDeleteDialog);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bindLibBulk);
} else {
  bindLibBulk();
}

async function loadLibrary(keepPage) {
  const seq = ++libSeq;
  if (!keepPage) libPage = 1;
  promoteLibFilterInputs();
  syncLibClear();
  const params = new URLSearchParams({
    page: String(libPage),
    pageSize: libPageSize,
    order: $("#lib-order").value,
    sort: $("#lib-sort").value,
  });
  if ($("#lib-order").value === "random") params.set("seed", String(libRandomSeed));
  const q = libQEl.value.trim();
  if (q) params.set("q", q);
  // 标签走 tag 参数；VA / 社团走 author 参数；排除项用 - 前缀
  const tagTokens = libFilters.tag.concat(libFilters.notTag.map(v => "-" + v));
  const authorTokens = libFilters.va.concat(
    libFilters.circle,
    libFilters.notVa.map(v => "-" + v),
    libFilters.notCircle.map(v => "-" + v),
  );
  if (tagTokens.length) params.set("tag", tagTokens.join(" "));
  if (authorTokens.length) params.set("author", authorTokens.join(" "));
  const from = $("#lib-from").value;
  const to = $("#lib-to").value;
  const sub = $("#lib-sub").value;
  if (from) params.set("from_date", from);
  if (to) params.set("to_date", to);
  if (sub === "gen") params.set("has_generated", "1");
  else if (sub !== "all") params.set("has_subtitle", sub);
  const ages = Array.from(ageState.lib);
  if (ages.length) params.set("exclude_age", ages.join(","));

  $("#lib-hint").innerHTML = "";
  $("#lib-grid").innerHTML = skeletonCards(6);
  try {
    const data = await api("/api/library/works?" + params);
    if (seq !== libSeq) return; // 已有更新的筛选请求，丢弃旧结果
    $("#lib-total").textContent = `共 ${data.total} 个`;
    libSelected.clear();
    renderWorks(data.works, "#lib-grid", (sid) => { window.MediaPlayer.browse(sid); },
      libSelected, updateLibSelectionUI);
    const libNav = $("#lib-pagination");
    if (libPageSize === "all") {
      libNav.innerHTML = ""; // 全部不分页
    } else {
      renderPagination(data.page || libPage, Number(libPageSize), data.total, loadLibrary,
        "#lib-pagination", (p) => {
          libPage = p;
          loadLibrary(true);
        });
    }

    // 本地没搜到且有关键词 → 只保留“去在线搜索”这一条提示，避免与网格空态重复
    if (!data.works.length && q) {
      $("#lib-grid").innerHTML = "";
      $("#lib-hint").innerHTML =
        `<div class="empty">${icon("search")}<span>本地没有「${q}」</span><button id="lib-goto-online" class="btn-accent">${icon("globe")}去在线搜索</button></div>`;
      $("#lib-goto-online").onclick = () => {
        $("#search-input").value = q;
        switchTab("search");
        searchPage = 1;
        doSearch();
      };
    }
  } catch (e) {
    $("#lib-grid").innerHTML = `<div class='empty'>加载失败: ${e.message}</div>`;
  }
}

function deleteLocal(sourceId) {
  if (!window.MediaPlayer || !window.MediaPlayer.requestDelete) return;
  window.MediaPlayer.requestDelete(sourceId, sourceId, function () {
    $("#modal").classList.add("hidden");
    loadLibrary();
  });
}

// ============ 系统设置 ============
let sysStorages = [];
let sysStorageSuggestions = [];
const storageWarned = new Set();

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtDurCard(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = n => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分`;
  return `${sec} 秒`;
}

function sysKv(rows) {
  return rows.map(([k, v, cls]) =>
    `<div class="kv-row"><span class="k">${k}</span><span class="v ${cls || ""}">${v}</span></div>`
  ).join("");
}

// ============ 生成字幕 板块 ============
let submTimer = null;
let submSettings = null;

function toast(msg) {
  let el = document.getElementById("subm-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "subm-toast";
    el.className = "toast-msg hidden";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(el._tid);
  el._tid = setTimeout(() => el.classList.add("hidden"), 2600);
}

const SUBM_GROUPS = [
  { key: "running", title: "生成中", open: true },
  { key: "translating", title: "翻译中", open: true },
  { key: "queue", title: "待生成（队列）", open: true },
  { key: "pending", title: "待翻译", open: true },
  { key: "missing", title: "未生成", open: false },
  { key: "generated", title: "已生成", open: false },
  { key: "official", title: "已有官方中字", open: false },
];

const submSelected = new Set();
let submGroupsCache = [];
let submForceOpen = "";
let submDurFilled = false;
let submDragEndedAt = 0;

const SUBM_STATUS_TEXT = {
  queued: "排队中", running: "生成中", paused: "已暂停",
  pending_translate: "待翻译",
  done: "已完成", failed: "失败", cancelled: "已取消",
};

function renderSubmOverview(ov) {
  const memOk = ov.mem_ok !== false;
  const warn = $("#subm-mem-warn");
  if (warn) warn.classList.toggle("hidden", memOk);
  const panel = $("#panel-subm");
  if (panel) panel.classList.toggle("is-disabled", !memOk);
  ["subm-pause", "subm-cancel", "subm-settings"].forEach(id => {
    const el = $("#" + id);
    if (el) el.disabled = !memOk;
  });
  const pill = $("#subm-engine-pill");
  if (pill) {
    const ok = !!ov.asr_available;
    pill.textContent = ok ? "引擎就绪" : "模型缺失";
    pill.classList.toggle("off", !ok);
  }
  const meta = $("#subm-head-meta");
  if (meta) {
    const parts = [
      `作品 ${ov.local_works ?? 0}`,
      `音轨 ${ov.audio_covered ?? 0}/${ov.audio_total ?? 0} 已覆盖`,
    ];
    if (ov.running) parts.unshift(`运行中：${ov.running.work_title || ov.running.work_id || ""}`);
    meta.textContent = parts.join("  ·  ");
  }
}

function submCardHTML(w, group) {
  const job = w.job || null;
  // 「在队列」（可拖动排序）只属于生成队列；待翻译等分组的卡片
  // 不带队列语义，避免误以为能拖动调整翻译顺序
  const sortable = group === "queue" && !!job &&
    ["queued", "paused", "pending_translate"].includes(job.status);
  const official = !!w.has_subtitle;
  return `<div class="subm-card-wrap${official ? " is-official" : ""}${sortable ? " is-queued" : ""}"
    data-id="${w.source_id}" data-job="${job ? job.id : ""}" title="查看作品详情">
    ${workCardHTML(w, !official, submSelected)}
  </div>`;
}

function submRunningHTML(w) {
  const job = w.job || {};
  const prog = Math.round((job.progress || 0) * 100);
  const cover = w.cover_url || `/api/cover/${w.source_id}?v=2`;
  const tr = job.mode === "translate";
  const st = job.status === "paused" ? "已暂停"
    : (tr ? "翻译中" : (SUBM_STATUS_TEXT[job.status] || "生成中"));
  const stage = job.stage ? esc(job.stage) : (tr ? "准备翻译" : "准备中");
  return `<div class="subm-run${job.status === "paused" ? " is-paused" : ""}" data-id="${w.source_id}" title="查看作品详情">
    <img class="subm-run__cover" src="${cover}" alt="" loading="lazy">
    <div class="subm-run__info">
      <b>${w.source_id}${w.title ? " · " + esc(w.title) : ""}</b>
      <span>${esc(st)} · ${stage}</span>
    </div>
    <div class="subm-run__side">
      <span class="subm-run__pct">${prog}%</span>
      <span class="subm-run__bar"><i style="width:${prog}%"></i></span>
      <span class="subm-run__stage">${job.message ? esc(job.message) : "等待开始"}</span>
    </div>
  </div>`;
}

function submTranslateNote(data) {
  const tr = (data && data.translate) || {};
  let text;
  if (!tr.api_ready) {
    text = "未配置翻译 API，填写密钥后自动开始翻译";
  } else if (tr.blocked) {
    text = "API 禁用时段，暂停调用翻译接口"
      + (tr.waiting ? `；待翻译 ${tr.waiting} 个作品` : "")
      + (tr.open_at ? `，${tr.open_at} 后自动继续` : "，时段结束后自动继续");
  } else if (tr.waiting) {
    text = `待翻译 ${tr.waiting} 个作品，翻译线程即将开始`;
  } else {
    text = "暂无翻译任务";
  }
  return `<div class="subm-note">${esc(text)}</div>`;
}

function syncPauseBtn(groups) {
  const btn = $("#subm-pause");
  const btnCancel = $("#subm-cancel");
  const busy = groups.filter(g => g.key === "running" || g.key === "translating");
  const hasRunning = busy.some(g => g.items.length);
  // 暂停 / 取消只作用于生成中的作品，排队中的任务不参与
  // 暂停后的任务可能落在队列或待翻译分组，两处都要能恢复
  const allItems = groups.flatMap(g => g.items || []);
  const hasPaused = allItems.some(w => (w.job || {}).status === "paused");
  if (btn) {
    btn.dataset.mode = hasPaused ? "resume" : "pause";
    btn.textContent = hasPaused ? "恢复" : "暂停";
    btn.disabled = !(hasPaused || hasRunning);
  }
  if (btnCancel) btnCancel.disabled = !(hasRunning || hasPaused);
}

function submBusyJobs() {
  // 识别与翻译双线程并行：两栏里的任务都要参与暂停 / 取消
  const busy = submGroupsCache.filter(g => g.key === "running" || g.key === "translating");
  const seen = new Set();
  const jobs = [];
  busy.forEach(g => (g.items || []).forEach(w => {
    const job = (w || {}).job;
    if (!job || !job.id || seen.has(job.id)) return;
    seen.add(job.id);
    jobs.push(job);
  }));
  return jobs;
}

function showSubmBulk() {
  const bar = $("#subm-bulk");
  if (!bar || bar.classList.contains("in")) return;
  bar.classList.remove("hidden", "leaving");
  void bar.offsetWidth;
  bar.classList.add("in");
}

function hideSubmBulk() {
  const bar = $("#subm-bulk");
  if (!bar || bar.classList.contains("hidden")) return;
  bar.classList.remove("in");
  bar.classList.add("leaving");
  setTimeout(() => {
    if (submSelected.size > 0) return;
    bar.classList.add("hidden");
    bar.classList.remove("leaving");
  }, 300);
}

function clearSubmSelection() {
  submSelected.clear();
  document.querySelectorAll("#subm-queue .work-card.selected").forEach(card => {
    card.classList.remove("selected");
    const check = card.querySelector(".check");
    if (check) check.innerHTML = "";
  });
  updateSubmSelectionUI();
}

function updateSubmSelectionUI() {
  const bcnt = $("#subm-bulk-count");
  if (bcnt) bcnt.textContent = String(submSelected.size);
  const active = $("#panel-subm") && $("#panel-subm").classList.contains("active");
  if (submSelected.size > 0 && active) showSubmBulk();
  else hideSubmBulk();
}

function renderSubmWorks(data) {
  const box = $("#subm-queue");
  const empty = $("#subm-empty");
  if (!box) return;

  // 清掉已不存在的选择，避免计数虚高
  const alive = new Set();
  Object.values(data.groups || {}).forEach(list => (list || []).forEach(w => alive.add(w.source_id)));
  [...submSelected].forEach(id => { if (!alive.has(id)) submSelected.delete(id); });

  const prevOpen = {};
  box.querySelectorAll(".dl-group").forEach(g => {
    prevOpen[g.dataset.group] = g.dataset.open === "true";
  });

  const groups = SUBM_GROUPS.map(g => ({
    ...g,
    items: (data.groups && data.groups[g.key]) || [],
  }));
  submGroupsCache = groups;
  const total = groups.reduce((n, g) => n + g.items.length, 0);
  if (empty) empty.classList.toggle("hidden", total > 0);

  box.innerHTML = groups.map(g => {
    if (g.key === "running" || g.key === "translating") {
      const body = g.items.map(submRunningHTML).join("");
      if (g.key === "translating") {
        // 「翻译中」常驻：没有任务时给出禁用时段 / 待翻译说明
        return `<div class="subm-sec-title">${g.title}<em>${g.items.length} 项</em></div>
          <div class="subm-running">${body || submTranslateNote(data)}</div>`;
      }
      if (!body) return "";
      return `<div class="subm-sec-title">${g.title}<em>${g.items.length} 项</em></div>
        <div class="subm-running">${body}</div>`;
    }
    let open = g.key in prevOpen ? prevOpen[g.key] : g.open;
    if (submForceOpen === g.key) open = true;
    return `
      <section class="dl-group" data-group="${g.key}" data-open="${open ? "true" : "false"}">
        <button class="dl-group__head" type="button" aria-expanded="${open}">
          <span>${g.title}</span>
          <span class="dl-group__count">${g.items.length} 项</span>
          <span class="dl-group__chev">${icon("chevron-down")}</span>
        </button>
        <div class="dl-group__body">
          <div class="works-grid subm-grid">${g.items.map(w => submCardHTML(w, g.key)).join("")}</div>
        </div>
      </section>`;
  }).join("");
  submForceOpen = "";
  syncPauseBtn(groups);

  box.querySelectorAll(".dl-group__head").forEach(btn => {
    btn.addEventListener("click", () => {
      const group = btn.closest(".dl-group");
      const open = group.dataset.open !== "true";
      group.dataset.open = open ? "true" : "false";
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });

  box.querySelectorAll(".subm-card-wrap").forEach(wrap => {
    const sid = wrap.dataset.id;
    const check = wrap.querySelector(".work-card .check");
    const toggle = (e) => {
      e.stopPropagation();
      if (submSelected.has(sid)) submSelected.delete(sid); else submSelected.add(sid);
      const on = submSelected.has(sid);
      // 同一作品可能同时出现在队列与待翻译，勾选状态保持同步
      box.querySelectorAll(`.subm-card-wrap[data-id="${sid}"]`).forEach(dup => {
        const card = dup.querySelector(".work-card");
        if (!card) return;
        card.classList.toggle("selected", on);
        const box2 = card.querySelector(".check");
        if (box2) box2.innerHTML = on ? icon("check") : "";
        card.classList.remove("dip");
        if (on) { void card.offsetWidth; card.classList.add("dip"); }
      });
      updateSubmSelectionUI();
    };
    if (check) check.addEventListener("click", toggle);
    wrap.addEventListener("click", (e) => {
      if (e.target.closest(".check")) return;
      if (Date.now() - submDragEndedAt < 320) return;
      showWorkDetail(sid, "subm");
    });
  });

  box.querySelectorAll(".subm-run").forEach(el => {
    el.addEventListener("click", () => showWorkDetail(el.dataset.id, "subm"));
  });

  bindQueueDrag(box);
  if (!submDurFilled) {
    submDurFilled = true;
    fillCardDurations(box);
  }
  updateSubmSelectionUI();
}

// 队列卡片拖动排序：手机主屏式抬升跟随、空槽让位、落下回弹
const SUBM_DRAG_HOLD = 190;   // 触屏长按起拖
const SUBM_DRAG_SLOP = 6;     // 鼠标起拖位移阈值

function submFlip(before, skip) {
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  before.forEach((rect, node) => {
    if (node === skip || !node.isConnected) return;
    const now = node.getBoundingClientRect();
    const dx = rect.left - now.left;
    const dy = rect.top - now.top;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
    node.style.transition = "none";
    node.style.transform = `translate3d(${dx}px, ${dy}px, 0)`;
    void node.offsetWidth;
    node.style.transition = "transform 240ms cubic-bezier(0.22, 1, 0.36, 1)";
    node.style.transform = "";
    const clear = () => {
      node.style.transition = "";
      node.style.transform = "";
      node.removeEventListener("transitionend", clear);
    };
    node.addEventListener("transitionend", clear);
    setTimeout(clear, 420);
  });
}

function bindQueueDrag(box) {
  const grid = box.querySelector('.dl-group[data-group="queue"] .subm-grid');
  if (!grid) return;
  const cards = () => [...grid.querySelectorAll(".subm-card-wrap[data-job]")]
    .filter(el => el.dataset.job);
  let st = null;
  let holdTimer = 0;

  const blockTouch = (e) => { if (st && st.active) e.preventDefault(); };

  const unbind = () => {
    clearTimeout(holdTimer);
    holdTimer = 0;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
    window.removeEventListener("touchmove", blockTouch);
  };

  const activate = () => {
    if (!st || st.active) return;
    const el = st.el;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    st.active = true;
    st.rect = rect;
    st.offX = st.startX - rect.left;
    st.offY = st.startY - rect.top;
    const ghost = el.cloneNode(true);
    ghost.classList.remove("is-queued", "settle");
    ghost.classList.add("subm-drag-ghost");
    ghost.removeAttribute("title");
    ghost.style.width = rect.width + "px";
    ghost.style.height = rect.height + "px";
    ghost.style.left = rect.left + "px";
    ghost.style.top = rect.top + "px";
    document.body.appendChild(ghost);
    st.ghost = ghost;
    el.classList.add("subm-card-hold");
    document.body.classList.add("subm-dragging");
    requestAnimationFrame(() => { if (st && st.ghost) st.ghost.classList.add("lifted"); });
  };

  const reorderTo = (target, cx, cy) => {
    const el = st.el;
    const r = target.getBoundingClientRect();
    const sameRow = Math.abs(cy - (r.top + r.height / 2)) < r.height * 0.45;
    const after = sameRow ? cx > r.left + r.width / 2 : cy > r.top + r.height / 2;
    if (after ? target.nextElementSibling === el : target.previousElementSibling === el) return;
    const before = new Map();
    cards().forEach(node => before.set(node, node.getBoundingClientRect()));
    grid.insertBefore(el, after ? target.nextSibling : target);
    submFlip(before, el);
  };

  const moveGhost = (px, py) => {
    const g = st.ghost;
    if (!g) return;
    g.style.left = px + "px";
    g.style.top = py + "px";
    // 贴边自动滚动
    if (py < 60) window.scrollBy(0, -14);
    else if (py + st.rect.height > window.innerHeight - 24) window.scrollBy(0, 14);
    const cx = px + st.rect.width / 2;
    const cy = py + st.rect.height / 2;
    const under = document.elementFromPoint(cx, cy);
    const target = under && under.closest(".subm-card-wrap");
    if (!target || target === st.el || !grid.contains(target) || !target.dataset.job) return;
    reorderTo(target, cx, cy);
  };

  const onMove = (e) => {
    if (!st) return;
    if (!st.active) {
      const dx = e.clientX - st.startX;
      const dy = e.clientY - st.startY;
      if (Math.hypot(dx, dy) <= SUBM_DRAG_SLOP) return;
      // 触屏滑动仍是滚动页面，长按后才会进入拖动
      if (st.pointerType !== "mouse") { unbind(); st = null; return; }
      activate();
    }
    if (!st || !st.active) return;
    e.preventDefault();
    moveGhost(e.clientX - st.offX, e.clientY - st.offY);
  };

  const onUp = () => {
    if (!st) return;
    const wasActive = st.active;
    const el = st.el;
    const ghost = st.ghost;
    unbind();
    st = null;
    if (!wasActive) return;
    document.body.classList.remove("subm-dragging");
    const final = el.getBoundingClientRect();
    let landed = false;
    const land = () => {
      if (landed) return;
      landed = true;
      if (ghost) ghost.remove();
      el.classList.remove("subm-card-hold");
      el.classList.add("settle");
      setTimeout(() => el.classList.remove("settle"), 340);
    };
    if (ghost) {
      ghost.classList.remove("lifted");
      ghost.classList.add("dropping");
      ghost.style.left = final.left + "px";
      ghost.style.top = final.top + "px";
      ghost.addEventListener("transitionend", land, { once: true });
      setTimeout(land, 320);
    } else {
      land();
    }
    submDragEndedAt = Date.now();
    const order = cards().map(x => Number(x.dataset.job)).filter(Boolean);
    api("/api/subtitles/queue/order", {
      method: "POST", body: JSON.stringify({ order }),
    }).catch(err => toast("排序保存失败：" + err.message));
  };

  const onDown = (e) => {
    if (e.button !== undefined && e.button > 0) return;
    if (e.target.closest(".check")) return;
    const el = e.target.closest(".subm-card-wrap.is-queued");
    if (!el || !grid.contains(el) || !el.dataset.job) return;
    st = {
      el,
      pointerType: e.pointerType || "mouse",
      startX: e.clientX,
      startY: e.clientY,
      active: false,
    };
    window.addEventListener("pointermove", onMove, { passive: false });
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    window.addEventListener("touchmove", blockTouch, { passive: false });
    if (st.pointerType !== "mouse") {
      clearTimeout(holdTimer);
      holdTimer = setTimeout(() => { if (st && !st.active) activate(); }, SUBM_DRAG_HOLD);
    }
  };

  grid.addEventListener("pointerdown", onDown);
}

async function loadSubm() {
  const box = $("#subm-queue");
  try {
    const [ov, data, cfg] = await Promise.all([
      api("/api/subtitles/engine/overview"),
      api("/api/subtitles/works"),
      api("/api/subtitles/settings"),
    ]);
    submSettings = cfg;
    renderSubmOverview(ov);
    renderSubmWorks(data);
    if (submTimer) clearInterval(submTimer);
    submTimer = setInterval(async () => {
      if (!$("#panel-subm").classList.contains("active")) return;
      try {
        renderSubmOverview(await api("/api/subtitles/engine/overview"));
        // 5 秒刷新一次列表，排队任务开始生成时能及时切到「生成中」
        renderSubmWorks(await api("/api/subtitles/works"));
      } catch (e) { /* 轮询失败忽略 */ }
    }, 5000);
  } catch (e) {
    if (box) box.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

async function submEnqueue() {
  const ids = [...submSelected];
  if (!ids.length) return;
  if (!(submSettings && submSettings.api_key_set)) {
    toast("请先在 设置 → 密钥令牌 填写翻译 API Key");
    return;
  }
  ids.forEach(sid => {
    const el = document.querySelector(`.subm-card-wrap[data-id="${CSS.escape(sid)}"]`);
    if (el) {
      el.classList.remove("queued-flash");
      void el.offsetWidth;
      el.classList.add("queued-flash");
    }
  });
  try {
    const res = await api("/api/subtitles/queue", {
      method: "POST", body: JSON.stringify({ ids }),
    });
    const skipN = (res.skipped || []).length;
    toast(`已加入队列 ${res.queued ?? 0} 个作品`
      + (skipN ? `，${skipN} 个已有中字已跳过` : ""));
    setTimeout(async () => {
      clearSubmSelection();
      submForceOpen = "queue";
      try { renderSubmWorks(await api("/api/subtitles/works")); } catch (e) { /* 忽略 */ }
    }, 420);
  } catch (e) {
    toast("加入队列失败：" + e.message);
  }
}

async function submTogglePause() {
  const allItems = submGroupsCache.flatMap(g => g.items || []);
  const paused = allItems.some(w => (w.job || {}).status === "paused");
  if (!paused && !submBusyJobs().length) { toast("当前没有正在生成的作品"); return; }
  try {
    await api(`/api/subtitles/engine/${paused ? "resume" : "pause"}`,
              { method: "POST" });
    toast(paused ? "已恢复生成" : "已暂停生成");
    await loadSubm();
  } catch (e) {
    toast("操作失败：" + e.message);
  }
}

async function submCancelRunning() {
  const jobs = submBusyJobs();
  if (!jobs.length) { toast("当前没有正在生成的作品"); return; }
  try {
    for (const job of jobs) {
      await api(`/api/subtitles/jobs/${job.id}/cancel`, { method: "POST" });
    }
    toast("已取消当前生成");
    await loadSubm();
  } catch (e) {
    toast("取消失败：" + e.message);
  }
}

async function submClear() {
  if (!confirm("确定清空所有自生成字幕文件？作品上的蓝色【中字】标记会同时被重置。")) return;
  try {
    const res = await api("/api/subtitles/engine/clear", { method: "POST" });
    toast(`已删除 ${res.removed ?? 0} 个字幕文件`);
    await loadSubm();
  } catch (e) {
    toast("清空失败：" + e.message);
  }
}

/* ---------------- 生成字幕设置弹窗（时段 / 官方中文 / 清空） ---------------- */
const SUBM_DAYS = [["一", 1], ["二", 2], ["三", 3], ["四", 4],
                   ["五", 5], ["六", 6], ["日", 7]];
let submOffpeakDraft = { enabled: false, windows: [] };
let submSettingsBaseline = "";

/* JellyRadio：0=关闭 1=低 2=中 3=高，原生弹簧动画替代 motion */
const SUBM_EFFORT = [["off", "关闭"], ["low", "低"], ["medium", "中"], ["high", "高"]];
const submMotionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
const submEffortStates = [];
let submEffortIndex = 0;

function submEffortRender(i) {
  const track = $("#subm-effort");
  const state = submEffortStates[i];
  const button = track && track.children[i];
  if (!state || !button) return;
  button.style.transform = `translateX(${state.value.x}px) scale(${state.value.sx}, ${state.value.sy})`;
}

function submEffortSpring(i, key, target, stiffness, mass, bounce, delay = 0) {
  const state = submEffortStates[i];
  if (!state) return;
  cancelAnimationFrame(state.raf[key]);
  const start = performance.now() + delay;
  let previous = 0;
  const tick = now => {
    if (now < start) {
      state.raf[key] = requestAnimationFrame(tick);
      return;
    }
    const dt = Math.min(0.032, Math.max(0.001, (now - (previous || now)) / 1000));
    previous = now;
    const current = state.value[key];
    const damping = 2 * Math.sqrt(stiffness * mass) * (1 - bounce);
    const acceleration = (stiffness * (target - current) - damping * state.velocity[key]) / mass;
    state.velocity[key] += acceleration * dt;
    state.value[key] += state.velocity[key] * dt;
    const epsilon = key === "x" ? 0.02 : 0.0005;
    if (Math.abs(target - state.value[key]) < epsilon && Math.abs(state.velocity[key]) < epsilon * 20) {
      state.value[key] = target;
      state.velocity[key] = 0;
      state.raf[key] = 0;
    } else {
      state.raf[key] = requestAnimationFrame(tick);
    }
    submEffortRender(i);
  };
  state.raf[key] = requestAnimationFrame(tick);
}

function submEffortTarget(i, selected, width) {
  const swell = 1.12;
  const shrink = 0.97;
  const push = width * (swell - 1) / 2 + 4;
  return {
    x: Math.sign(i - selected) * push,
    sx: i === selected ? swell : shrink,
    sy: i === selected ? swell : shrink,
  };
}

function submEffortApply(idx, animate = true) {
  const track = $("#subm-effort");
  if (!track) return;
  const buttons = Array.from(track.children);
  submEffortIndex = Math.max(0, Math.min(buttons.length - 1, idx));
  const widths = buttons.map(button => button.offsetWidth);
  const chipHeight = buttons[0] ? buttons[0].offsetHeight : 36;
  track.style.setProperty("--jr-pad-x", "4px");
  track.style.setProperty("--jr-pad-y", `${Math.ceil(chipHeight * 0.2 / 2) + 2}px`);
  buttons.forEach((button, i) => {
    const on = i === submEffortIndex;
    button.dataset.on = on ? "true" : "false";
    button.setAttribute("aria-checked", on ? "true" : "false");
    button.tabIndex = on ? 0 : -1;
    if (!submEffortStates[i]) {
      submEffortStates[i] = {
        value: { x: 0, sx: 1, sy: 1 }, velocity: { x: 0, sx: 0, sy: 0 },
        raf: { x: 0, sx: 0, sy: 0 },
      };
    }
    const state = submEffortStates[i];
    const target = submEffortTarget(i, submEffortIndex, widths[submEffortIndex] || 0);
    const inFlight = Object.values(state.raf).some(Boolean);
    if (!animate || submMotionPreference.matches || !widths[submEffortIndex]) {
      ["x", "sx", "sy"].forEach(key => {
        cancelAnimationFrame(state.raf[key]);
        state.raf[key] = 0;
        state.velocity[key] = 0;
        state.value[key] = target[key];
      });
      submEffortRender(i);
      return;
    }
    const far = Math.abs(i - submEffortIndex);
    const stiffness = 580 * (1 - 0.12 * Math.min(far, 3));
    const delay = inFlight ? 0 : far * 22;
    submEffortSpring(i, "x", target.x, stiffness, 0.9, 0.25, delay);
    submEffortSpring(i, "sx", target.sx, stiffness * 1.24, 0.8, 0.55, delay);
    submEffortSpring(i, "sy", target.sy, stiffness * 0.86, 0.95, 0.25, delay + 50);
  });
  submSyncSaveIcon();
}

function bindSubmEffort() {
  const track = $("#subm-effort");
  if (!track) return;
  const buttons = Array.from(track.children);
  buttons.forEach((button, i) => {
    button.addEventListener("click", e => submEffortApply(i, e.detail !== 0));
    button.addEventListener("keydown", e => {
      let next = null;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % buttons.length;
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + buttons.length) % buttons.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = buttons.length - 1;
      if (next === null) return;
      e.preventDefault();
      submEffortApply(next, false);
      buttons[next].focus();
    });
  });
  const settle = () => submEffortApply(submEffortIndex, false);
  if (window.ResizeObserver) new ResizeObserver(settle).observe(track);
  else window.addEventListener("resize", settle);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(settle);
  settle();
}

/* ScrubField：拖动调值、点击输入、方向键步进、Shift/Alt 倍率 */
const submScrubControls = new WeakMap();

function bindSubmScrubField(root) {
  const input = root.querySelector(".scrub-field__input");
  const fill = root.querySelector(".scrub-field__fill");
  const ghost = root.querySelector(".scrub-field__ghost");
  const suffix = root.dataset.suffix || "";
  const min = Number(root.dataset.min);
  const max = Number(root.dataset.max);
  const step = Number(root.dataset.step);
  const baseDecimals = (String(step).split(".")[1] || "").length;
  const fineDecimals = Math.min(6, baseDecimals + 1);
  const reach = 0;
  const clamp = value => Math.min(max, Math.max(min, value));
  const bend = raw => reach ? Math.sign(raw) * reach * Math.log1p(Math.abs(raw) / reach) : 0;
  const unbend = over => reach ? Math.sign(over) * reach * Math.expm1(Math.abs(over) / reach) : 0;
  const toShown = raw => { const bounded = clamp(raw); return bounded + bend(raw - bounded); };
  const toRaw = shown => { const bounded = clamp(shown); return bounded + unbend(shown - bounded); };
  const multiplier = e => e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
  const format = value => {
    const scaled = value * 10 ** baseDecimals;
    return value.toFixed(Math.abs(scaled - Math.round(scaled)) < 1e-6 ? baseDecimals : fineDecimals);
  };
  let value = clamp(Number(input.value) || min);
  let shown = value;
  let draft = null;
  let drag = null;
  let returnRaf = 0;
  let suppressClick = false;

  const setDisplay = (next, force = false) => {
    shown = next;
    if (draft === null || force) input.value = format(next);
    const fraction = (clamp(next) - min) / Math.max(max - min, Number.EPSILON);
    fill.style.transform = `scaleX(${fraction})`;
    const outside = next < min || next > max;
    root.dataset.over = outside ? "true" : "false";
    const lean = submMotionPreference.matches || !reach ? 0 :
      Math.max(-1, Math.min(1, (next - clamp(next)) / reach)) * 4;
    root.style.transform = `translateX(${lean}px)`;
    input.setAttribute("aria-valuenow", String(value));
    input.setAttribute("aria-valuetext", `${format(value)}${suffix ? ` ${suffix}` : ""}`);
    submSyncSaveIcon();
  };
  const commit = next => {
    value = clamp(Number(next.toFixed(fineDecimals)));
  };
  const adopt = next => {
    cancelAnimationFrame(returnRaf);
    draft = null;
    root.dataset.typing = "false";
    commit(clamp(Number(next) || min));
    setDisplay(value, true);
  };
  const stopReturn = () => cancelAnimationFrame(returnRaf);
  const returnToBound = () => {
    const target = clamp(shown);
    if (shown === target || submMotionPreference.matches) { setDisplay(target); return; }
    const from = shown;
    const start = performance.now();
    const tick = now => {
      const t = Math.min(1, (now - start) / 300);
      const eased = 1 - Math.exp(-6 * t) * (1 + 6 * t);
      setDisplay(from + (target - from) * eased);
      if (t < 1) returnRaf = requestAnimationFrame(tick);
      else setDisplay(target);
    };
    returnRaf = requestAnimationFrame(tick);
  };
  const onEscape = e => { if (e.key === "Escape") endDrag(true); };
  const endDrag = (cancel = false) => {
    if (!drag) return;
    const current = drag;
    drag = null;
    root.dataset.dragging = "false";
    document.documentElement.style.cursor = "";
    window.removeEventListener("keydown", onEscape);
    if (cancel) {
      commit(current.before);
      setDisplay(value, true);
      return;
    }
    if (!current.moved) {
      input.focus();
      input.select();
      return;
    }
    suppressClick = true;
    setTimeout(() => { suppressClick = false; }, 0);
    returnToBound();
  };

  root.addEventListener("pointerdown", e => {
    if ((e.button !== undefined && e.button !== 0) || drag || draft !== null) return;
    stopReturn();
    drag = {
      id: e.pointerId, x: e.clientX, raw: toRaw(shown), before: value,
      moved: false, mult: multiplier(e), slack: e.pointerType === "touch" ? 8 : 3,
      left: root.getBoundingClientRect().left,
    };
    try { root.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    if (e.pointerType !== "touch") e.preventDefault();
    window.addEventListener("keydown", onEscape);
  });
  root.addEventListener("pointermove", e => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved) {
      if (Math.abs(e.clientX - drag.x) < drag.slack) return;
      drag.moved = true;
      drag.x = e.clientX;
      drag.raw = toRaw(shown);
      root.dataset.dragging = "true";
      document.documentElement.style.cursor = "ew-resize";
    }
    const nextMultiplier = multiplier(e);
    if (nextMultiplier !== drag.mult) {
      drag.mult = nextMultiplier;
      drag.raw = toRaw(shown);
      drag.x = e.clientX;
    }
    const totalSteps = Math.max(1, Math.round((max - min) / step));
    const pxPerStep = Math.max(2, root.clientWidth / totalSteps);
    const raw = drag.raw + Math.round((e.clientX - drag.x) / pxPerStep) * step * drag.mult;
    commit(raw);
    setDisplay(toShown(raw));
    ghost.textContent = `${shown - drag.before < 0 ? "−" : "+"}${format(Math.abs(shown - drag.before))}`;
    const ghostHalf = (ghost.offsetWidth || 42) / 2;
    const localX = Math.min(root.clientWidth - ghostHalf - 4, Math.max(ghostHalf + 4, e.clientX - drag.left));
    ghost.style.left = `${localX}px`;
  });
  root.addEventListener("pointerup", () => endDrag());
  root.addEventListener("pointercancel", () => endDrag(true));
  root.addEventListener("lostpointercapture", () => endDrag());
  root.addEventListener("click", e => {
    if (!suppressClick) return;
    suppressClick = false;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  input.addEventListener("focus", () => {
    stopReturn();
    draft = input.value;
    root.dataset.typing = "true";
    input.select();
  });
  input.addEventListener("input", () => { draft = input.value; submSyncSaveIcon(); });
  input.addEventListener("keydown", e => {
    const typed = draft !== null && draft.trim() !== "" ? Number(draft) : NaN;
    const from = Number.isNaN(typed) ? value : typed;
    const deltas = {
      ArrowUp: step * multiplier(e), ArrowDown: -step * multiplier(e),
      PageUp: step * 10, PageDown: -step * 10,
    };
    if (deltas[e.key] !== undefined || e.key === "Home" || e.key === "End") {
      e.preventDefault();
      draft = null;
      root.dataset.typing = "false";
      commit(deltas[e.key] !== undefined ? from + deltas[e.key] : e.key === "Home" ? min : max);
      setDisplay(value, true);
    } else if (e.key === "Enter") {
      e.preventDefault();
      input.blur();
    } else if (e.key === "Escape") {
      e.preventDefault();
      draft = null;
      root.dataset.typing = "false";
      setDisplay(value, true);
      input.blur();
    }
  });
  input.addEventListener("blur", () => {
    const typed = draft === null ? NaN : parseFloat(draft.replace(/[^\d.-]/g, ""));
    if (!Number.isNaN(typed)) commit(typed);
    draft = null;
    root.dataset.typing = "false";
    setDisplay(value, true);
  });

  submScrubControls.set(input, { set: adopt });
  adopt(value);
}

function bindSubmScrubFields() {
  $$(".scrub-field").forEach(bindSubmScrubField);
}

function submSetToggle(on) {
  const btn = $("#subm-offpeak-toggle");
  if (!btn) return;
  btn.classList.toggle("on", !!on);
  btn.setAttribute("aria-checked", on ? "true" : "false");
}

// 「空闲自动卸载」开关：on = 引擎空闲后立即释放 VAD/ASR 模型内存
function submIdleSet(on) {
  const btn = $("#subm-idle-toggle");
  if (!btn) return;
  btn.classList.toggle("on", !!on);
  btn.setAttribute("aria-checked", on ? "true" : "false");
}

function submRenderWindows() {
  const box = $("#subm-offpeak-windows");
  if (!box) return;
  box.innerHTML = submOffpeakDraft.windows.map((w, i) => `
    <div class="subm-window" data-idx="${i}">
      <div class="subm-window__days">${SUBM_DAYS.map(([lbl, d]) => `
        <button type="button" class="subm-day${(w.days || []).includes(d) ? " on" : ""}"
                data-day="${d}" aria-pressed="${(w.days || []).includes(d) ? "true" : "false"}">${lbl}</button>`).join("")}
      </div>
      <div class="subm-window__time">
        <input type="time" class="w-start" value="${esc(w.start || "09:00")}" aria-label="开始时间">
        <span>至</span>
        <input type="time" class="w-end" value="${esc(w.end || "18:00")}" aria-label="结束时间">
        <button type="button" class="subm-window__del" aria-label="删除时间段">${icon("trash")}</button>
      </div>
    </div>`).join("");

  box.querySelectorAll(".subm-window").forEach(row => {
    const idx = Number(row.dataset.idx);
    const win = submOffpeakDraft.windows[idx];
    if (!win) return;
    win.days = win.days || [];
    row.querySelectorAll(".subm-day").forEach(btn => {
      btn.addEventListener("click", () => {
        const d = Number(btn.dataset.day);
        if (win.days.includes(d)) win.days = win.days.filter(x => x !== d);
        else win.days.push(d);
        submRenderWindows();
      });
    });
    const s = row.querySelector(".w-start");
    const e = row.querySelector(".w-end");
    if (s) s.addEventListener("change", () => { win.start = s.value || "09:00"; submSyncSaveIcon(); });
    if (e) e.addEventListener("change", () => { win.end = e.value || "18:00"; submSyncSaveIcon(); });
    const del = row.querySelector(".subm-window__del");
    if (del) del.addEventListener("click", () => {
      submOffpeakDraft.windows.splice(idx, 1);
      submRenderWindows();
    });
  });
  submSyncSaveIcon();
}

function openSubmSettings() {
  const s = submSettings || {};
  const sch = s.api_offpeak || { enabled: false, windows: [] };
  submOffpeakDraft = {
    enabled: !!sch.enabled,
    windows: (sch.windows || []).map(w => ({
      days: [...(w.days || [])],
      start: w.start || "09:00",
      end: w.end || "18:00",
    })),
  };
  const drop = $("#subm-set-drop");
  if (drop) drop.value = s.drop_generated_chinese || "keep";
  const setNum = (id, val) => {
    const el = $("#" + id);
    if (!el) return;
    const control = submScrubControls.get(el);
    if (control) control.set(val);
    else el.value = val;
  };
  setNum("subm-set-vad", s.vad_threshold ?? 0.4);
  setNum("subm-set-min-speech", s.vad_min_speech ?? 0.25);
  setNum("subm-set-min-silence", s.vad_min_silence ?? 0.2);
  const idleEl = $("#subm-idle-toggle");
  if (idleEl) submIdleSet(Number(s.model_idle_minutes ?? 10) > 0);
  const wk = $("#subm-set-workers");
  if (wk) {
    const maxW = Math.max(1, Number(s.asr_workers_max) || 1);
    wk.max = String(maxW);
    wk.value = String(Math.min(maxW, Math.max(1, Number(s.asr_workers) || 1)));
    const hint = $("#subm-workers-hint");
    if (hint) {
      const cores = Number(s.cpu_cores) || 1;
      const mem = Number(s.mem_total_gb) || 0;
      hint.textContent = `上限 ${maxW} 线程（${cores} 核心${mem ? ` · ${mem}GB 内存` : ""}）`;
    }
  }
  const lvl = SUBM_EFFORT.findIndex(([k]) => k === (s.api_thinking || "off"));
  submEffortApply(lvl < 0 ? 0 : lvl, false);
  submSetToggle(submOffpeakDraft.enabled);
  submRenderWindows();
  openDialog("subm-settings-modal");
  submSettingsBaseline = submSettingsSnapshot();
  submSyncSaveIcon();
  requestAnimationFrame(() => {
    submEffortApply(submEffortIndex, false);
    submSyncSaveIcon();
  });
}

/* 保存按钮图标：与已保存配置一致时打勾，有改动时显示软盘 */
function submSettingsSnapshot() {
  const val = id => ($("#" + id)?.value ?? "").trim();
  const windows = (submOffpeakDraft.windows || []).map(w => ({
    days: [...(w.days || [])].sort().join(","),
    start: w.start || "",
    end: w.end || "",
  }));
  return JSON.stringify({
    drop: val("subm-set-drop"),
    vad: val("subm-set-vad"),
    speech: val("subm-set-min-speech"),
    silence: val("subm-set-min-silence"),
    workers: val("subm-set-workers"),
    idle: $("#subm-idle-toggle")?.classList.contains("on") ? "1" : "0",
    effort: submEffortIndex,
    offpeak: !!submOffpeakDraft.enabled,
    windows,
  });
}

function submSyncSaveIcon() {
  const modal = $("#subm-settings-modal");
  const btn = $("#subm-settings-save");
  if (!modal || !btn || modal.classList.contains("hidden")) return;
  const dirty = submSettingsSnapshot() !== submSettingsBaseline;
  btn.dataset.dirty = dirty ? "true" : "false";
  const use = btn.querySelector("use");
  if (use) use.setAttribute("href", dirty ? "/icons.svg#i-save" : "/icons.svg#i-check");
}

function submNumValue(id, fallback, min, max) {
  const el = $("#" + id);
  const v = parseFloat(el && el.value);
  if (!isFinite(v)) return fallback;
  return Math.min(max, Math.max(min, v));
}

async function saveSubmSettings() {
  const drop = $("#subm-set-drop");
  const body = {
    drop_generated_chinese: drop ? drop.value : "keep",
    vad_threshold: submNumValue("subm-set-vad", 0.4, 0, 1),
    vad_min_speech: submNumValue("subm-set-min-speech", 0.25, 0.05, 2),
    vad_min_silence: submNumValue("subm-set-min-silence", 0.2, 0.05, 2),
    asr_workers: submNumValue("subm-set-workers", 1, 1,
      Math.max(1, Number($("#subm-set-workers")?.max) || 1)),
    model_idle_minutes: $("#subm-idle-toggle")?.classList.contains("on") ? 1 : 0,
    api_thinking: (SUBM_EFFORT[submEffortIndex] || SUBM_EFFORT[0])[0],
    api_offpeak: {
      enabled: !!submOffpeakDraft.enabled,
      windows: submOffpeakDraft.windows,
    },
  };
  try {
    submSettings = await api("/api/subtitles/settings", {
      method: "POST", body: JSON.stringify(body),
    });
    closeDialog("subm-settings-modal");
    toast("设置已保存");
    await loadSubm();
  } catch (e) {
    toast("保存失败：" + e.message);
  }
}

function bindSubmBulk() {
  const queue = $("#subm-bulk-queue");
  const clear = $("#subm-bulk-clear");
  const del = $("#subm-bulk-del");

  if (queue) queue.addEventListener("click", submEnqueue);
  if (clear) clear.addEventListener("click", clearSubmSelection);

  if (del) bindHoldDelete(del, () => submSelected.size > 0, openSubmDeleteDialog);
}

document.addEventListener("DOMContentLoaded", () => {
  const bind = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
  bind("#subm-pause", submTogglePause);
  bind("#subm-cancel", submCancelRunning);
  bind("#subm-settings", openSubmSettings);
  bindSubmEffort();
  bindSubmScrubFields();
  bindSubmBulk();

  const setToggle = $("#subm-offpeak-toggle");
  if (setToggle) setToggle.addEventListener("click", () => {
    submOffpeakDraft.enabled = !submOffpeakDraft.enabled;
    submSetToggle(submOffpeakDraft.enabled);
    submSyncSaveIcon();
  });
  const dropSet = $("#subm-set-drop");
  if (dropSet) dropSet.addEventListener("change", submSyncSaveIcon);
  const workersSet = $("#subm-set-workers");
  if (workersSet) {
    const clamp = () => {
      const maxW = Math.max(1, Number(workersSet.max) || 1);
      const v = Math.round(Number(workersSet.value) || 1);
      workersSet.value = String(Math.min(maxW, Math.max(1, v)));
      submSyncSaveIcon();
    };
    workersSet.addEventListener("input", submSyncSaveIcon);
    workersSet.addEventListener("change", clamp);
    workersSet.addEventListener("blur", clamp);
  }
  const idleToggle = $("#subm-idle-toggle");
  if (idleToggle) idleToggle.addEventListener("click", () => {
    submIdleSet(!idleToggle.classList.contains("on"));
    submSyncSaveIcon();
  });
  bind("#subm-window-add", () => {
    const last = submOffpeakDraft.windows[submOffpeakDraft.windows.length - 1];
    submOffpeakDraft.windows.push({
      days: last ? [...last.days] : [1, 2, 3, 4, 5],
      start: "09:00", end: "18:00",
    });
    submRenderWindows();
  });
  bind("#subm-set-clear", submClear);
  bind("#subm-settings-save", saveSubmSettings);
});



function openDialog(id) { $("#" + id).classList.remove("hidden"); }
function closeDialog(id) { $("#" + id).classList.add("hidden"); }

function showAlert(title, text) {
  $("#alert-title").textContent = title;
  $("#alert-text").textContent = text;
  openDialog("alert-modal");
}

$$("[data-close]").forEach(btn => {
  btn.addEventListener("click", () => closeDialog(btn.dataset.close));
});
["storage-modal", "alert-modal", "modal", "queue-modal", "subm-settings-modal"].forEach(id => {
  const el = $("#" + id);
  el.addEventListener("click", e => { if (e.target === el) closeDialog(id); });
});

async function loadSystem() {
  try {
    const data = await api("/api/system/status");
    const app = data.app || {};
    const cfg = data.config || {};
    $("#sys-app").innerHTML = sysKv([
      ["版本", `v${esc(app.version)}`],
      ["运行时长", fmtDuration(app.uptime_seconds)],
      ["内存占用", `${app.memory_mb ?? "-"} MB`],
      ["启动时间", esc(app.started_at || "-"), "dim"],
      ["进程 PID", esc(app.pid ?? "-")],
      ["下载并发", `${cfg.workers ?? "-"} 线程 · ${cfg.qps ?? "-"} QPS`],
      ["数据库", esc(cfg.db_path || "-"), "mono-val"],
    ]);

    const host = cfg.web_host === "::" ? "[::]" : (cfg.web_host || "-");
    $("#sys-net").innerHTML = sysKv([
      ["监听地址", esc(`${host}:${cfg.web_port ?? "-"}`), "mono-val"],
      ["当前访问", esc(location.origin), "mono-val"],
      ["网络协议", "IPv6 严格模式"],
      ["解析目标", "api.asmr.one · asmr.one", "dim"],
    ]);

    const s = data.stats || {};
    $("#sys-stats").innerHTML = [
      [fmtDuration(Number(s.total_duration) || 0), "作品总时长"],
      [s.local_works, "本地作品"],
      [s.total_files, "已下载文件"],
      [humanSize(s.total_size), "本地占用"],
    ].map(([n, l]) => `<div class="stat-tile"><span class="n">${n ?? 0}</span><span class="l">${l}</span></div>`).join("");

    sysStorages = data.storages || [];
    sysStorageSuggestions = data.storage_suggestions || [];
    renderStorages();

    const pwSet = !!data.delete_password_set;
    const pill = $("#pw-state");
    if (pill) {
      pill.textContent = pwSet ? "已设置" : "未设置";
      pill.classList.toggle("off", !pwSet);
    }
    applySafeMode(!!data.safe_mode);
    loadVerifyAllStatus();
    loadLangAuditStatus();
    loadOrphans();
    loadSubtitlePanel();
  } catch (e) {
    $("#sys-app").innerHTML = `<div class="kv-row"><span class="v">加载失败：${esc(e.message)}</span></div>`;
  }
}

$("#sys-refresh").addEventListener("click", loadSystem);

// ---------- 未入库文件扫描 / 清理 ----------
let orphanState = { items: [], files: 0, bytes: 0, running: false };

function renderOrphans(st) {
  orphanState = st;
  const items = st.items || [];
  const summary = $("#orphan-summary");
  if (st.running) summary.textContent = "扫描中…";
  else if (st.error) summary.textContent = `扫描失败：${st.error}`;
  else if (!st.finished_at) summary.textContent = "扫描下载中断或重复下载留下的目录";
  else if (!items.length) summary.textContent = "未发现未入库目录";
  else summary.textContent = `${items.length} 个目录 · ${st.files} 个文件 · ${humanSize(st.bytes)}`;

  $("#orphan-scan").disabled = !!st.running;
  $("#orphan-clean").disabled = !!st.running || !items.length;
  $("#orphan-list").innerHTML = items.map(i => `
    <div class="orphan-row">
      <span class="orphan-name" title="${esc(i.path)}">${esc(i.name)}</span>
      <span class="num">${i.files} 文件</span>
      <span class="num">${humanSize(i.size)}</span>
    </div>`).join("");
}

async function loadOrphans() {
  try {
    renderOrphans(await api("/api/system/orphans"));
  } catch (e) { /* 旧版本后端无此接口时静默 */ }
}

$("#orphan-scan").addEventListener("click", async () => {
  renderOrphans({ ...orphanState, running: true, items: [], files: 0, bytes: 0 });
  try {
    await api("/api/system/orphans/scan", { method: "POST" });
  } catch (e) {
    renderOrphans({ ...orphanState, running: false, error: e.message });
    return;
  }
  const tick = async () => {
    try {
      const st = await api("/api/system/orphans");
      renderOrphans(st);
      if (st.running) setTimeout(tick, 800);
    } catch {
      setTimeout(tick, 1500);
    }
  };
  setTimeout(tick, 400);
});

$("#orphan-clean").addEventListener("click", async () => {
  const items = orphanState.items || [];
  if (!items.length) return;
  const total = items.reduce((a, i) => a + (i.size || 0), 0);
  if (!confirm(`删除 ${items.length} 个未入库目录？共 ${humanSize(total)}，删除后不可恢复。`)) return;
  try {
    const r = await api("/api/system/orphans/clean", {
      method: "POST", body: JSON.stringify({ paths: items.map(i => i.path) }),
    });
    await loadOrphans();
    if (r.failed && r.failed.length) {
      showAlert("部分目录未清理", r.failed.map(f => `${f.path}：${f.error}`).join("\n"));
    }
  } catch (e) {
    showAlert("清理失败", e.message);
  }
});

// ============ 文件完整性比对（FuseButton 风格） ============
let verifyAllTimer = null;
let verifyInstantOnce = false;
let verifyIntroUntil = 0;
let verifyLastRatio = 0;
let rimValue = 0;
let rimAnim = null;
let rimGeom = null;

// 圆角矩形周长参数化，供进度游标逐帧取点（顺时针，起点在左上圆角之后）
function pointAt(p) {
  if (!rimGeom) return [-20, -20];
  const { x, y, w, h, r } = rimGeom;
  const SW = Math.max(0, w - 2 * r);
  const SH = Math.max(0, h - 2 * r);
  const ARC = Math.PI * r / 2;
  const PERIM = 2 * SW + 2 * SH + 4 * ARC;
  let s = Math.max(0, Math.min(1, p)) * PERIM;
  if (s <= SW) return [x + r + s, y];
  s -= SW;
  if (s <= ARC) {
    const a = -Math.PI / 2 + s / r;
    return [x + w - r + Math.cos(a) * r, y + r + Math.sin(a) * r];
  }
  s -= ARC;
  if (s <= SH) return [x + w, y + r + s];
  s -= SH;
  if (s <= ARC) {
    const a = s / r;
    return [x + w - r + Math.cos(a) * r, y + h - r + Math.sin(a) * r];
  }
  s -= ARC;
  if (s <= SW) return [x + w - r - s, y + h];
  s -= SW;
  if (s <= ARC) {
    const a = Math.PI / 2 + s / r;
    return [x + r + Math.cos(a) * r, y + h - r + Math.sin(a) * r];
  }
  s -= ARC;
  if (s <= SH) return [x, y + h - r - s];
  s -= SH;
  const a = Math.PI + s / r;
  return [x + r + Math.cos(a) * r, y + r + Math.sin(a) * r];
}

function measureRim() {
  const svg = $("#integrity-rim");
  if (!svg) return;
  const box = svg.getBoundingClientRect();
  const w = box.width;
  const h = box.height;
  if (w < 8 || h < 8) { rimGeom = null; return; }
  const stroke = 1.5;
  const x = stroke / 2;
  const y = stroke / 2;
  const rw = Math.max(1, w - stroke);
  const rh = Math.max(1, h - stroke);
  const face = $("#integrity-run");
  const radius = face
    ? parseFloat(getComputedStyle(face).borderTopLeftRadius) || 6 : 6;
  const rx = Math.max(0, radius - stroke / 2);
  const bar = $("#integrity-ring-bar");
  if (bar) {
    bar.setAttribute("x", x);
    bar.setAttribute("y", y);
    bar.setAttribute("width", rw);
    bar.setAttribute("height", rh);
    bar.setAttribute("rx", rx);
    bar.setAttribute("ry", rx);
  }
  rimGeom = { x, y, w: rw, h: rh, r: rx };
}

function paintRim(p) {
  const v = Math.max(0, Math.min(1, p));
  const bar = $("#integrity-ring-bar");
  const tip = $("#integrity-ring-tip");
  const pct = $("#integrity-pct");
  if (bar) bar.style.strokeDashoffset = String(1 - v);
  if (tip) {
    if (!rimGeom) measureRim();
    const pt = pointAt(v);
    tip.setAttribute("cx", pt[0]);
    tip.setAttribute("cy", pt[1]);
  }
  if (pct) pct.textContent = `${Math.round(v * 100)}%`;
}

// 进度必须线性推进：与真实检查进度一一对应，不加缓动
function animateRimTo(target, instant, dur) {
  target = Math.max(0, Math.min(1, target));
  cancelAnimationFrame(rimAnim);
  if (instant) {
    rimValue = target;
    paintRim(target);
    return;
  }
  const from = rimValue;
  const span = dur || 360;
  const start = performance.now();
  const step = now => {
    const t = span > 0 ? Math.min(1, (now - start) / span) : 1;
    rimValue = from + (target - from) * t;
    paintRim(rimValue);
    if (t < 1) rimAnim = requestAnimationFrame(step);
  };
  rimAnim = requestAnimationFrame(step);
}

function setPhase(phase, instant) {
  const fb = $("#integrity-run-wrap");
  if (!fb || fb.dataset.phase === phase) return;
  if (instant) {
    fb.setAttribute("data-instant", "");
    requestAnimationFrame(() => fb.removeAttribute("data-instant"));
  }
  fb.dataset.phase = phase;
  const idle = $("#integrity-run");
  const undo = $("#integrity-cancel");
  const done = $("#integrity-done");
  if (idle) idle.inert = phase !== "idle";
  if (undo) undo.inert = phase !== "armed";
  if (done) done.inert = phase !== "settled";
  const sr = $("#integrity-sr");
  if (sr) {
    sr.textContent = phase === "armed" ? "正在比对，可随时取消"
      : phase === "settled" ? (done ? done.textContent.trim() : "比对完成") : "";
  }
  if (phase === "armed" && undo) undo.focus({ preventScroll: true });
  if (phase === "idle" && idle) idle.focus({ preventScroll: true });
  requestAnimationFrame(() => {
    measureRim();
    paintRim(rimValue);
  });
}

function renderVerifyAll(s) {
  const fb = $("#integrity-run-wrap");
  if (!fb) return;

  const total = Number(s.total) || 0;
  const checked = Number(s.checked) || 0;
  const ratio = total > 0 ? Math.min(1, checked / total) : (s.running ? 0 : 1);
  verifyLastRatio = ratio;
  const sr = $("#integrity-sr");
  const runningMark = $("#integrity-cancel .smk");
  if (s.running) {
    // 开场先转一圈，结束后再交给真实百分比
    if (performance.now() >= verifyIntroUntil) {
      setSmkProgress(runningMark, total > 0 ? ratio : null);
    }
  } else {
    setSmkProgress(runningMark);
    setSmkProgress($("#integrity-mark"));
  }

  if (s.running) {
    setPhase("armed", verifyInstantOnce);
    verifyInstantOnce = false;
    animateRimTo(ratio);
    const pct = $("#integrity-pct");
    if (pct) pct.textContent = total > 0 ? `${Math.round(ratio * 100)}%` : "";
    if (sr) sr.textContent = `正在校验 ${checked}/${total || "?"}`;
  } else if (s.cancelled) {
    // 取消：光圈匀速收回后再回到空闲态，避免满圈瞬间闪空
    animateRimTo(0, false, 260);
    setTimeout(() => setPhase("idle"), 260);
    if (sr) sr.textContent = s.message || "已取消校验";
  } else if (s.finished_at) {
    // 完成后结果保持显示，刷新页面后也按上次结果继续展示
    animateRimTo(1);
    const pass = !Number(s.incomplete) && !Number(s.error_count);
    const failed = Number(s.error_count) > 0;
    const result = pass ? "done" : failed ? "error" : "repair";
    fb.dataset.result = pass ? "pass" : failed ? "error" : "repair";
    const label = pass ? "校验通过"
      : failed ? "校验失败"
        : (s.repair_task_id ? "补齐任务" : "发现缺失");
    const doneLabel = $("#integrity-done .fb__label");
    if (doneLabel) doneLabel.textContent = label;
    const donePct = $("#integrity-done-pct");
    const complete = Number(s.complete) || 0;
    if (donePct) {
      donePct.textContent = total > 0 ? `${Math.round(Math.min(1, complete / total) * 100)}%` : "";
    }
    const mark = $("#integrity-mark");
    if (mark) mark.dataset.status = result;
    setPhase("settled", verifyInstantOnce);
    verifyInstantOnce = false;
    if (sr) sr.textContent = label;
  } else {
    if (fb.dataset.phase !== "idle") {
      setPhase("idle");
      animateRimTo(0, true);
    }
    if (sr) sr.textContent = "";
  }

  clearTimeout(verifyAllTimer);
  if (s.running) verifyAllTimer = setTimeout(pollVerifyAll, 1200);
}

async function loadVerifyAllStatus() {
  try {
    renderVerifyAll(await api("/api/library/verify-all"));
    measureRim();
    paintRim(rimValue);
  } catch (e) {
    // 首次进入系统页时忽略状态读取失败
  }
}

async function pollVerifyAll() {
  try {
    const s = await api("/api/library/verify-all");
    renderVerifyAll(s);
    if (!s.running && s.repair_task_id) loadDownloads();
  } catch (e) {
    clearTimeout(verifyAllTimer);
  }
}

// 开始校验：圆环先转一整圈，与中文版检查开场一致
function startVerifyIntro() {
  const mark = $("#integrity-cancel .smk");
  const ring = mark && mark.querySelector(".smk__ring");
  if (!ring) return;
  verifyIntroUntil = performance.now() + 900;
  ring.style.removeProperty("stroke-dasharray");
  ring.style.animation = "smkSpinOnce 900ms cubic-bezier(0.4, 0, 0.2, 1)";
  setTimeout(() => {
    const fb = $("#integrity-run-wrap");
    if (!fb || fb.dataset.phase !== "armed") return;
    verifyIntroUntil = 0;
    setSmkProgress(mark, verifyLastRatio);
  }, 900);
}

async function startVerifyAll(ev) {
  const fb = $("#integrity-run-wrap");
  if (!fb || fb.dataset.phase !== "idle") return;
  verifyInstantOnce = !ev || ev.detail === 0;
  try {
    await api("/api/library/verify-all", {
      method: "POST",
      body: JSON.stringify({ action: "start" }),
    });
    startVerifyIntro();
    await pollVerifyAll();
  } catch (e) {
    if (String(e.message).startsWith("409")) {
      await pollVerifyAll();
      return;
    }
    showAlert("检查失败", e.message);
  }
}

async function cancelVerifyAll() {
  const fb = $("#integrity-run-wrap");
  if (!fb || fb.dataset.phase !== "armed") return;
  try {
    renderVerifyAll(await api("/api/library/verify-all", {
      method: "POST",
      body: JSON.stringify({ action: "cancel" }),
    }));
  } catch (e) {
    showAlert("取消失败", e.message);
  }
}

const fbRoot = $("#integrity-run-wrap");
if (fbRoot) {
  fbRoot.addEventListener("pointerdown", e => {
    if (e.button === 0) fbRoot.dataset.pressed = "";
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach(name => {
    fbRoot.addEventListener(name, () => { delete fbRoot.dataset.pressed; });
  });
  fbRoot.addEventListener("click", () => {
    fbRoot.classList.remove("is-pop");
    void fbRoot.offsetWidth;
    fbRoot.classList.add("is-pop");
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && fbRoot.dataset.phase === "armed") {
      verifyInstantOnce = true;
      cancelVerifyAll();
    }
  });
  window.addEventListener("resize", () => {
    measureRim();
    paintRim(rimValue);
  });
}

$("#integrity-run").addEventListener("click", startVerifyAll);
$("#integrity-cancel").addEventListener("click", cancelVerifyAll);
// 结果常驻：点击结果面即重新验证
$("#integrity-done").addEventListener("click", () => {
  const fb = $("#integrity-run-wrap");
  if (!fb || fb.dataset.phase !== "settled") return;
  fb.dataset.result = "";
  setPhase("idle");
  animateRimTo(0, true);
  startVerifyAll();
});

// ============ 安全模式：所有封面渲染为纯黑 ============
function applySafeMode(on) {
  const enabled = !!on;
  document.documentElement.classList.toggle("safe-mode", enabled);
  const sw = $("#sys-safe-mode");
  if (sw) {
    sw.classList.toggle("on", enabled);
    sw.setAttribute("aria-checked", enabled ? "true" : "false");
  }
  try { localStorage.setItem("safeMode", enabled ? "1" : "0"); } catch {}
}

// 首屏先按本地缓存应用，避免封面先闪一下彩色
let safeModeBoot = false;
try { safeModeBoot = localStorage.getItem("safeMode") === "1"; } catch {}
applySafeMode(safeModeBoot);

$("#sys-safe-mode").addEventListener("click", async () => {
  const next = !document.documentElement.classList.contains("safe-mode");
  applySafeMode(next);
  try {
    const r = await api("/api/system/safe-mode", {
      method: "POST", body: JSON.stringify({ enabled: next }),
    });
    applySafeMode(r.safe_mode);
  } catch (e) {
    applySafeMode(!next);
    showAlert("保存失败", e.message);
  }
});

async function saveDeletePassword() {
  const input = $("#delete-password");
  const pw = input.value;
  try {
    const r = await api("/api/system/delete-password", {
      method: "POST", body: JSON.stringify({ password: pw }),
    });
    input.value = "";
    const pill = $("#pw-state");
    pill.textContent = r.set ? "删除保护 已设置" : "删除保护 未设置";
    pill.classList.toggle("off", !r.set);
    showAlert("删除保护", r.set ? "删除密码已保存。" : "删除密码已清除。");
  } catch (e) {
    showAlert("保存失败", e.message);
  }
}
$("#delete-password-save").addEventListener("click", saveDeletePassword);
$("#delete-password").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); saveDeletePassword(); }
});

function storageRowHTML(s, i) {
  const total = Number(s.total) || 0;
  const pct = n => total > 0 ? Math.max(0, Math.min(100, n / total * 100)) : 0;
  const otherPct = pct(s.other_used);
  const asmrPct = pct(s.asmr_used);
  const freePct = pct(s.free);
  const tags = [];
  if (i === 0) tags.push('<span class="storage-tag">首选下载</span>');
  if (!s.exists) tags.push('<span class="storage-tag warn">不可用</span>');
  else if (!s.writable) tags.push('<span class="storage-tag warn">不可写</span>');
  return `
    <div class="storage-row${s.exists ? "" : " missing"}" data-path="${esc(s.path)}">
      <button class="storage-grip" aria-label="拖动排序" title="拖动调整优先级">
        ${icon("grip")}
      </button>
      <div class="storage-rank">${i + 1}</div>
      <div class="storage-body">
        <div class="storage-top">
          <span class="storage-path" title="${esc(s.path)}">${esc(s.path)}</span>
          ${tags.join("")}
          <button class="storage-edit" data-path="${esc(s.path)}" aria-label="编辑保留空间" title="编辑保留空间">
            ${icon("edit")}
          </button>
          <button class="storage-del" data-path="${esc(s.path)}" aria-label="移除存储空间" title="移除（不删除文件）">
            ${icon("trash")}
          </button>
        </div>
        <div class="storage-bar">
          <span class="seg other" style="width:${otherPct}%"></span>
          <span class="seg asmr" style="width:${asmrPct}%"></span>
          <span class="seg free" style="width:${freePct}%"></span>
        </div>
        <div class="storage-meta">
          <span title="该存储卷上 NAS 系统、Docker 及其他应用占用，不是 ASMR 残留">系统/其他 <b>${humanSize(s.other_used)}</b></span>
          <span title="本应用已入库的作品文件，按清单大小统计（不含封面等未入库文件）">ASMR <b>${humanSize(s.asmr_used)}</b></span>
          <span>未使用 <b>${humanSize(s.free)}</b> / ${humanSize(total)}</span>
          ${s.min_free_gb ? `<span>保留 <b>${s.min_free_gb} GB</b></span>` : ""}
        </div>
      </div>
    </div>`;
}

function renderStorages() {
  const box = $("#storage-list");
  if (!sysStorages.length) {
    box.innerHTML = '<div class="storage-empty">还没有存储空间，下载将写入数据目录。点击右上角「添加新的存储空间」。</div>';
    return;
  }
  box.innerHTML = sysStorages.map(storageRowHTML).join("");
  box.querySelectorAll(".storage-edit").forEach(btn => {
    btn.addEventListener("click", () => openStorageEditor(btn.dataset.path));
  });
  box.querySelectorAll(".storage-del").forEach(btn => {
    btn.addEventListener("click", async () => {
      const path = btn.dataset.path;
      const removed = sysStorages.find(x => x.path === path);
      const only = sysStorages.length === 1;
      const tip = only
        ? "\n移除后将没有可用存储空间，下载会写入数据目录。"
        : "";
      if (!confirm(`移除存储空间 ${path}？\n硬盘上的文件不会被删除。${tip}`)) return;
      try {
        const r = await api("/api/system/storages", {
          method: "DELETE", body: JSON.stringify({ path }),
        });
        try {
          localStorage.setItem("asmr:lastStorage", JSON.stringify({
            path, reserve: removed ? removed.min_free_gb : 0,
          }));
        } catch {}
        sysStorages = r.storages;
        renderStorages();
      } catch (e) {
        showAlert("移除失败", e.message);
      }
    });
  });
  initStorageDrag();
}

// ---- Shredder 风格拖拽排序：弹簧跟随 + 侧移倾角 + 其余行腾挪 ----
const SD = { FOLLOW: 30, STACK_K: 320, STACK_C: 22, STAGGER: 0.035 };

function initStorageDrag() {
  const list = $("#storage-list");
  const rows = Array.from(list.querySelectorAll(".storage-row"));
  if (rows.length < 2) return;

  rows.forEach(row => {
    row.addEventListener("pointerdown", e => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      const hit = e.target.closest("button, a, input");
      if (hit && !hit.classList.contains("storage-grip")) return;
      // 触屏只在把手上拖动，避免影响页面滚动
      if (e.pointerType !== "mouse" && !(hit && hit.classList.contains("storage-grip"))) return;
      startStorageDrag(e, row, rows);
    });
  });
}

function startStorageDrag(e, row, rows) {
  e.preventDefault();
  const idx = rows.indexOf(row);
  const rects = rows.map(r => r.getBoundingClientRect());
  const step = rows.length > 1
    ? (rects[1].top - rects[0].top)
    : rects[0].height + 8;
  const list = $("#storage-list");
  const startX = e.clientX;
  const startY = e.clientY;
  const OVER = 24;
  const minY = rects[0].top - rects[idx].top - OVER;
  const maxY = rects[rows.length - 1].bottom - rects[idx].bottom + OVER;

  let dragY = 0, targetY = 0, dx = 0, dy = 0;
  let target = idx;
  let moved = false;
  let raf = 0;
  let last = performance.now();
  const offsets = rows.map(() => ({ p: 0, v: 0, target: 0, delay: 0 }));

  if (list.setPointerCapture) {
    try { list.setPointerCapture(e.pointerId); } catch {}
  }
  row.classList.add("dragging");
  row.classList.remove("animating");

  const setTargets = () => {
    const nowS = performance.now() / 1000;
    offsets.forEach((o, i) => {
      let want = 0;
      if (i !== idx) {
        if (target > idx && i > idx && i <= target) want = -step;
        if (target < idx && i >= target && i < idx) want = step;
      }
      if (want !== o.target) {
        o.target = want;
        o.delay = nowS + Math.min(Math.abs(i - idx), 4) * SD.STAGGER;
      }
    });
  };

  const tick = now => {
    const dt = Math.min(0.032, Math.max(0.001, (now - last) / 1000));
    last = now;

    dragY += (targetY - dragY) * (1 - Math.exp(-dt * SD.FOLLOW));
    // 位移取整且不做 scale/rotate，避免合成层文字被位图重采样发虚
    row.style.transform = `translate3d(0, ${Math.round(dragY)}px, 0)`;

    offsets.forEach((o, i) => {
      if (i === idx) return;
      if (now / 1000 < o.delay) return;
      const a = -SD.STACK_K * (o.p - o.target) - SD.STACK_C * o.v;
      o.v += a * dt;
      o.p += o.v * dt;
      if (Math.abs(o.p - o.target) < 0.01 && Math.abs(o.v) < 0.01) {
        o.p = o.target;
        o.v = 0;
      }
      rows[i].style.transform = `translate3d(0, ${Math.round(o.p)}px, 0)`;
    });
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);

  const onMove = ev => {
    dx = ev.clientX - startX;
    dy = ev.clientY - startY;
    if (Math.abs(dy) > 3 || Math.abs(dx) > 3) moved = true;
    targetY = Math.max(minY, Math.min(maxY, dy));

    const curTop = rects[idx].top + targetY;
    const curMid = curTop + rects[idx].height / 2;
    target = idx;
    if (targetY > 0) {
      for (let i = idx + 1; i < rows.length; i++) {
        if (curMid > rects[i].top + rects[i].height / 2) target = i;
      }
    } else if (targetY < 0) {
      for (let i = idx - 1; i >= 0; i--) {
        if (curMid < rects[i].top + rects[i].height / 2) target = i;
      }
    }
    setTargets();
  };

  const finish = () => {
    cancelAnimationFrame(raf);
    list.removeEventListener("pointermove", onMove);
    list.removeEventListener("pointerup", finish);
    list.removeEventListener("pointercancel", finish);
    try { list.releasePointerCapture(e.pointerId); } catch {}

    row.classList.remove("dragging");
    if (!moved) {
      row.style.transform = "";
    }

    if (!moved || target === idx) {
      rows.forEach(r => {
        r.classList.add("animating");
        r.style.transform = "translate3d(0, 0, 0)";
      });
      setTimeout(renderStorages, 330);
      return;
    }

    const delta = rects[target].top - rects[idx].top;
    row.classList.add("animating");
    row.style.transform = `translate3d(0, ${delta}px, 0)`;
    rows.forEach((r, i) => {
      if (i === idx) return;
      r.classList.add("animating");
      r.style.transform = "translate3d(0, 0, 0)";
    });

    const [item] = sysStorages.splice(idx, 1);
    sysStorages.splice(target, 0, item);
    saveStorageOrder();
    setTimeout(renderStorages, 340);
  };

  list.addEventListener("pointermove", onMove);
  list.addEventListener("pointerup", finish);
  list.addEventListener("pointercancel", finish);
}

async function saveStorageOrder() {
  try {
    const r = await api("/api/system/storages/order", {
      method: "PUT",
      body: JSON.stringify({ paths: sysStorages.map(s => s.path) }),
    });
    sysStorages = r.storages;
  } catch (e) {
    showAlert("保存顺序失败", e.message);
    loadSystem();
  }
}

let storageEditing = null;

function openStorageEditor(path) {
  const s = sysStorages.find(x => x.path === path);
  if (!s) return;
  storageEditing = path;
  $("#storage-modal-title").textContent = "编辑保留空间";
  $("#storage-modal-desc").textContent = "设置该存储空间至少保留的剩余空间，0 表示不保留。路径不可修改。";
  $("#storage-path").value = path;
  $("#storage-path").readOnly = true;
  $("#storage-reserve").value = s.min_free_gb || 0;
  $("#storage-save-label").textContent = "保存";
  openDialog("storage-modal");
  setTimeout(() => $("#storage-reserve").select(), 40);
}

$("#storage-add").addEventListener("click", () => {
  storageEditing = null;
  $("#storage-modal-title").textContent = "添加新的存储空间";
  $("#storage-modal-desc").textContent =
    "填写飞牛 OS 中已挂载存储空间的目录路径，例如 /vol2/asmr-downloads。目录不存在时会自动创建。";
  let last = null;
  try { last = JSON.parse(localStorage.getItem("asmr:lastStorage") || "null"); } catch {}
  const opts = $("#storage-path-options");
  opts.innerHTML = sysStorageSuggestions
    .map(p => `<option value="${esc(p)}"></option>`).join("");
  $("#storage-path").value = (last && last.path) || sysStorageSuggestions[0] || "";
  $("#storage-path").readOnly = false;
  $("#storage-reserve").value = last && last.reserve ? last.reserve : "";
  $("#storage-save-label").textContent = "添加";
  openDialog("storage-modal");
  setTimeout(() => $("#storage-path").focus(), 40);
});

$("#storage-save").addEventListener("click", async () => {
  const editing = storageEditing;
  const path = editing || $("#storage-path").value.trim();
  if (!path) { showAlert("无法添加", "请填写存储路径"); return; }
  const reserve = Number($("#storage-reserve").value) || 0;
  $("#storage-save").disabled = true;
  try {
    const r = await api("/api/system/storages", {
      method: editing ? "PUT" : "POST",
      body: JSON.stringify({ path, min_free_gb: reserve }),
    });
    sysStorages = r.storages;
    renderStorages();
    if (!editing) {
      try { localStorage.removeItem("asmr:lastStorage"); } catch {}
    }
    closeDialog("storage-modal");
  } catch (e) {
    showAlert(editing ? "保存失败" : "添加失败", e.message);
  } finally {
    $("#storage-save").disabled = false;
  }
});

$("#storage-path").addEventListener("keydown", e => {
  if (e.key === "Enter") $("#storage-save").click();
});

$("#btn-ipv6-test").addEventListener("click", async () => {
  const box = $("#sys-ipv6");
  box.innerHTML = '<div class="ipv6-line"><span class="txt">测试中...</span></div>';
  try {
    const data = await api("/api/system/ipv6");
    const lines = [];
    if (data.egress_error) {
      lines.push(["bad", `出口链路：${data.egress_error}`]);
    } else {
      const e = data.egress ? JSON.stringify(data.egress) : "";
      lines.push(["ok", `IPv6 出口正常${e ? " · " + e.slice(0, 120) : ""}`]);
    }
    if (data.api_login === "ok") {
      lines.push(["ok", "API 登录成功"]);
    } else {
      lines.push(["bad", `API 登录失败：${data.api_login_error || "未知错误"}`]);
    }
    Object.entries(data.aaaa || {}).forEach(([host, addrs]) => {
      if (Array.isArray(addrs) && addrs.length) {
        lines.push(["ok", `${host} → ${addrs.join(", ")}`]);
      } else {
        lines.push(["bad", `${host} → ${(addrs && addrs.error) || "解析失败"}`]);
      }
    });
    box.innerHTML = lines.map(([cls, txt]) =>
      `<div class="ipv6-line ${cls}">${icon(cls === "ok" ? "check" : "x")}<span class="txt">${esc(txt)}</span></div>`
    ).join("");
  } catch (e) {
    box.innerHTML = `<div class="ipv6-line bad">${icon("x")}<span class="txt">测试失败：${esc(e.message)}</span></div>`;
  }
});

// ============ 启动 ============
// 二级页面 /library/<RJ号> 直达时，直接渲染文件浏览页
const libMatch = location.pathname.match(/^\/library\/([^/]+)\/?$/);
if (libMatch) {
  switchTab("library");
  window.MediaPlayer.open(decodeURIComponent(libMatch[1]), false);
} else {
  doSearch();
}

// ============ 点击确认回弹（与验证完整性一致） ============
const POP_SELECTOR = [
  "#search-go", "#search-reset", "#search-select-all", "#batch-download",
  "#lib-rescan", "#lib-lang-audit",
  ".btn-primary", ".btn-accent", ".btn-ghost", ".btn-danger",
].join(",");
document.addEventListener("click", (e) => {
  const btn = e.target.closest(POP_SELECTOR);
  if (!btn || btn.disabled || btn.closest(".fb")) return;
  btn.classList.remove("pop-anim");
  void btn.offsetWidth;
  btn.classList.add("pop-anim");
});
document.addEventListener("animationend", (e) => {
  if (e.animationName === "uiPop") e.target.classList.remove("pop-anim");
});

// ============ 密钥令牌（翻译 API） ============
let subSettings = null;

function paintApiState(cfg) {
  const pill = $("#api-state");
  if (!pill) return;
  const ok = !!(cfg && cfg.api_key_set);
  pill.textContent = ok ? "已配置" : "未配置";
  pill.classList.toggle("off", !ok);
}

async function loadSubtitlePanel() {
  const hint = $("#kst-hint");
  try {
    const cfg = await api("/api/subtitles/settings");
    subSettings = cfg;
    $("#sub-api-base").value = cfg.api_base || "";
    $("#sub-api-model").value = cfg.api_model || "";
    $("#sub-api-key").value = "";
    $("#sub-api-key").placeholder = cfg.api_key_set ? "已保存，留空保持不变" : "未设置";
    paintApiState(cfg);
    if (hint) hint.textContent = "";
  } catch (e) {
    if (hint) hint.textContent = "API 设置加载失败：" + e.message;
  }
}

async function saveApiSettings() {
  const btn = $("#kst-save");
  const hint = $("#kst-hint");
  if (btn) btn.disabled = true;
  const body = {
    api_base: $("#sub-api-base").value.trim(),
    api_model: $("#sub-api-model").value.trim(),
  };
  const key = $("#sub-api-key").value.trim();
  if (key) body.api_key = key;
  try {
    subSettings = await api("/api/subtitles/settings", {
      method: "POST", body: JSON.stringify(body),
    });
    $("#sub-api-key").value = "";
    $("#sub-api-key").placeholder = subSettings.api_key_set ? "已保存，留空保持不变" : "未设置";
    paintApiState(subSettings);
    if (hint) hint.textContent = `已保存 · ${new Date().toTimeString().slice(0, 5)}`;
  } catch (e) {
    if (hint) hint.textContent = "保存失败：" + e.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 测试配置：与「检查中文版」一致的状态标记（旋转 → 勾 / 叉）
function setApiTestState(status, label) {
  const mark = $("#kst-test-mark");
  const text = $("#kst-test-label");
  const btn = $("#kst-test");
  if (mark) {
    mark.dataset.status = status;
    setSmkProgress(mark, null);
  }
  if (text) text.textContent = label;
  if (btn) btn.dataset.state = status;
}

async function testApiSettings() {
  const btn = $("#kst-test");
  const hint = $("#kst-hint");
  if (btn && btn.dataset.state === "running") return;
  const body = {
    api_base: $("#sub-api-base").value.trim(),
    api_model: $("#sub-api-model").value.trim(),
  };
  const key = $("#sub-api-key").value.trim();
  if (key) body.api_key = key;
  setApiTestState("running", "测试中");
  if (hint) hint.textContent = "";
  try {
    const r = await api("/api/subtitles/test", {
      method: "POST", body: JSON.stringify(body),
    });
    const ms = Number(r.ms) || 0;
    setApiTestState("done", ms ? `连接正常 ${ms}ms` : "连接正常");
  } catch (e) {
    setApiTestState("error", "连接失败");
    if (hint) hint.textContent = "测试失败：" + String(e.message).slice(0, 160);
  }
}

$("#kst-save").addEventListener("click", saveApiSettings);
$("#kst-test").addEventListener("click", testApiSettings);

