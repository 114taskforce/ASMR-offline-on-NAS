/* 文件浏览器 + 底部迷你播放器 + 播放抽屉 + 图片/视频查看 */
(function () {
  "use strict";

  var AUDIO_EXT = /\.(mp3|wav|flac|m4a|aac|ogg|opus)$/i;
  var IMAGE_EXT = /\.(jpg|jpeg|png|webp|gif|bmp)$/i;
  var VIDEO_EXT = /\.(mp4|webm|mkv|mov|m4v)$/i;
  var SUB_EXT = /\.(vtt|srt|ass|ssa|lrc)$/i;

  var S = {
    work: null,        // 当前浏览的作品 {source_id,title,cover_url,tree}
    dir: "",           // 当前浏览目录 rel
    play: null,        // 正在播放的上下文 {work,dir,queue,index}，与浏览页解耦
    loop: "all",       // off | one | all（默认列表循环）
    subOn: true,       // 始终开启字幕
    cues: [],
    subRel: "",        // 当前字幕文件 rel
    subSeq: null,      // 最近使用的字幕 {work,dir,rel}，用于同目录顺延
    subOffset: 0,      // 字幕整体偏移（秒）
    backToLib: false, // 关闭目录页时标记：历史跳回后切到「本地资源」面板
    subOpen: false,
    subCue: -1,
    subUserScroll: 0,
    extras: "",        // "" | "playlist"
    photos: [],
    photoIndex: 0,
    dur: {},           // rel -> 秒（已探测到的媒体时长）
  };

  var $ = function (s) { return document.querySelector(s); };
  // 统一图标（内联 SVG sprite，禁止 emoji）
  function ico(name) {
    return '<svg class="ico" aria-hidden="true"><use href="/icons.svg#i-' + name + '"></use></svg>';
  }
  var audio = new Audio();
  audio.preload = "metadata";
  audio.volume = parseFloat(localStorage.getItem("mp.vol") || "1");
  var seekCtl = null;   // 弹性进度滑杆控制器

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  }
  function fmt(t) {
    if (!isFinite(t) || t < 0) t = 0;
    var m = Math.floor(t / 60), s = Math.floor(t % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }
  function humanSize(b) {
    if (b > 1073741824) return (b / 1073741824).toFixed(1) + "G";
    if (b > 1048576) return (b / 1048576).toFixed(1) + "M";
    if (b > 1024) return Math.round(b / 1024) + "K";
    return b + "B";
  }
  function fmtDur(sec) {
    if (!isFinite(sec) || sec <= 0) return "--:--";
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = Math.floor(sec % 60);
    var head = h ? h + ":" + ("0" + m).slice(-2) : String(m);
    return head + ":" + ("0" + s).slice(-2);
  }
  function rawURL(rel, workId) {
    var id = workId || (S.work && S.work.source_id) || "";
    return "/api/library/raw/" + encodeURIComponent(id) + "?rel=" + encodeURIComponent(rel);
  }

  /* ---------------- DOM ---------------- */

  function ensureDom() {
    if ($("#mp-browser")) return;

    var wrap = document.createElement("div");
    wrap.innerHTML = [
      // 文件浏览（二级页面，URL 为 /library/<source_id>）
      '<div class="mp-page" id="mp-browser">',
      '  <div class="mp-page-top">',
      '    <div class="mp-page-top__inner">',
      '      <button class="mp-back" data-act="close-browser" aria-label="返回">' + ico("arrow-left") + '返回</button>',
      '    </div>',
      '  </div>',
      '  <div class="mp-panel">',
      '    <div class="mp-head">',
      '      <img class="mp-head-cover" id="mp-b-cover" alt="">',
      '      <div class="mp-head-info">',
      '        <div class="mp-head-title-row">',
      '          <div class="mp-head-title" id="mp-b-title"></div>',
      '          <button class="mp-verify-work" id="mp-b-verify" aria-label="完整性校验" title="完整性校验（远程清单 + 文件大小）">' + ico("check") + '</button>',
      '          <button class="mp-del-work" id="mp-b-del" aria-label="删除本地作品" title="删除本地作品">' + ico("trash") + '</button>',
      '        </div>',
      '        <div class="mp-head-sub" id="mp-b-sub">',
      '          <span class="mp-head-id" id="mp-b-id"></span>',
      '          <span class="mp-head-total" id="mp-b-total" title="作品总时长">' + ico("clock") + '<b id="mp-b-total-val">--:--</b></span>',
      '        </div>',
      '      </div>',
      '    </div>',
      '    <div class="mp-integrity" id="mp-integrity" aria-live="polite"></div>',
      '    <div class="mp-crumb" id="mp-crumb"></div>',
      '    <div class="mp-list" id="mp-list"></div>',
      '  </div>',
      '</div>',

      // 迷你播放条
      '<div class="mp-bar" id="mp-bar">',
      '  <img class="mp-bar-cover" id="mp-bar-cover" alt="">',
      '  <div class="mp-bar-text">',
      '    <div class="mp-bar-title" id="mp-bar-title"></div>',
      '    <div class="mp-bar-file" id="mp-bar-file"></div>',
      '  </div>',
      '  <div class="mp-bar-ctrl">',
      '    <button data-act="prev" aria-label="上一个">' + ico("prev") + '</button>',
      '    <button data-act="toggle" class="mp-bar-play" id="mp-bar-toggle" aria-label="播放或暂停">' + ico("play") + '</button>',
      '    <button data-act="next" aria-label="下一个">' + ico("next") + '</button>',
      '  </div>',
      '  <div class="mp-bar-progress"><i id="mp-bar-fill"></i></div>',
      '</div>',
      '<div class="mp-caption" id="mp-caption"></div>',

      // 播放抽屉
      '<div class="mp-drawer" id="mp-drawer">',
      '  <button class="mp-x mp-drawer-x" data-act="close-drawer" aria-label="收起">' + ico("chevron-down") + '</button>',
      '  <div class="mp-d-cover-wrap"><img id="mp-d-cover" alt=""></div>',
      '  <div class="mp-d-meta">',
      '    <div class="mp-d-title" id="mp-d-title"></div>',
      '    <div class="mp-d-file" id="mp-d-file"></div>',
      '  </div>',
      '  <div class="mp-d-sub" id="mp-d-sub"></div>',
      '  <div class="mp-d-seek">',
      '    <span class="eslider__icon" id="eslider-icon-start"><span class="eslider__icon-inner" id="mp-cur">0:00</span></span>',
      '    <div class="eslider" id="mp-eslider" role="slider" tabindex="0" aria-label="播放进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">',
      '      <div class="eslider__track" id="eslider-track"><div class="eslider__range" id="eslider-range"></div></div>',
      '    </div>',
      '    <span class="eslider__icon" id="eslider-icon-end"><span class="eslider__icon-inner" id="mp-dur">0:00</span></span>',
      '  </div>',
      '  <div class="mp-d-btns">',
      '    <button data-act="prev" aria-label="上一首">' + ico("prev") + '</button>',
      '    <button data-act="back10" aria-label="后退 10 秒"><span class="mp-seek-num">-10</span></button>',
      '    <button data-act="back5" aria-label="后退 5 秒"><span class="mp-seek-num">-5</span></button>',
      '    <button data-act="toggle" class="mp-play" id="mp-d-toggle" aria-label="播放或暂停">' + ico("play") + '</button>',
      '    <button data-act="fwd5" aria-label="前进 5 秒"><span class="mp-seek-num">+5</span></button>',
      '    <button data-act="fwd10" aria-label="前进 10 秒"><span class="mp-seek-num">+10</span></button>',
      '    <button data-act="next" aria-label="下一首">' + ico("next") + '</button>',
      '  </div>',
      '  <div class="mp-d-row">',
      '    <div class="mp-vol" id="mp-vol">',
      '      <button type="button" class="mp-vol-btn" id="mp-vol-btn" aria-label="音量" aria-expanded="false" title="音量">' + ico("volume") + '</button>',
      '      <div class="mp-vol-pop" id="mp-vol-pop" aria-hidden="true">',
      '        <div class="wake-slider" id="mp-vol-slider" role="slider" tabindex="0" aria-label="音量" aria-valuemin="0" aria-valuemax="100" aria-valuenow="100">',
      '          <div class="wake-slider__track" id="mp-vol-track"></div>',
      '        </div>',
      '        <span class="wake-slider__value" id="mp-vol-val">100%</span>',
      '      </div>',
      '    </div>',
      '    <button data-act="loop" id="mp-loop-btn" class="mp-icon-btn" title="列表循环" aria-label="循环模式">' + ico("repeat") + '</button>',
      '    <button data-act="sub" id="mp-sub-btn" class="mp-icon-btn" title="字幕" aria-label="字幕">' + ico("captions") + '</button>',
      '    <button data-act="playlist" class="mp-icon-btn" title="播放列表" aria-label="播放列表">' + ico("list") + '</button>',
      '    <button data-act="folder" id="mp-folder-btn" class="mp-icon-btn" title="访问作品目录" aria-label="访问作品目录">' + ico("folder") + '</button>',
      '  </div>',
      '  <div class="mp-extra" id="mp-extra"></div>',
      '</div>',

      // 字幕详情弹窗
      '<div class="mp-overlay mp-subm" id="mp-subm">',
      '  <div class="mp-subm-card" role="dialog" aria-modal="true" aria-label="字幕详情">',
      '    <div class="mp-subm-top">',
      '      <select id="mp-subm-match" aria-label="重新匹配字幕"></select>',
      '      <button class="mp-x mp-subm-x" data-act="close-subm" aria-label="关闭">' + ico("x") + '</button>',
      '    </div>',
      '    <div class="mp-subm-list" id="mp-subm-list"></div>',
      '    <div class="mp-subm-foot">',
      '      <span class="mp-subm-off" id="mp-subm-off">偏移 0.0s</span>',
      '      <button data-act="subm-minus" aria-label="字幕提前 0.3 秒">-0.3S</button>',
      '      <button data-act="subm-zero" aria-label="偏移归零">0</button>',
      '      <button data-act="subm-plus" aria-label="字幕延后 0.3 秒">+0.3S</button>',
      '    </div>',
      '  </div>',
      '</div>',

      // 图片查看
      '<div class="mp-overlay mp-photo" id="mp-photo">',
      '  <button class="mp-x" data-act="close-photo" aria-label="关闭">' + ico("x") + '</button>',
      '  <button class="mp-nav mp-prev" data-act="photo-prev" aria-label="上一张">' + ico("chevron-left") + '</button>',
      '  <img id="mp-photo-img" alt="">',
      '  <button class="mp-nav mp-next" data-act="photo-next" aria-label="下一张">' + ico("chevron-right") + '</button>',
      '  <div class="mp-photo-name" id="mp-photo-name"></div>',
      '</div>',

      // 视频查看
      '<div class="mp-overlay mp-video" id="mp-video">',
      '  <button class="mp-x" data-act="close-video" aria-label="关闭">' + ico("x") + '</button>',
      '  <video id="mp-video-el" controls playsinline></video>',
      '  <div class="mp-photo-name" id="mp-video-name"></div>',
      '</div>',

      // 删除本地作品（密码确认）
      '<div class="mp-overlay mp-del" id="mp-del">',
      '  <div class="mp-del-card" role="dialog" aria-modal="true" aria-label="删除本地作品">',
      '    <div class="mp-del-head"><h3>' + ico("trash") + '删除本地作品</h3>',
      '      <button class="mp-x" data-act="del-cancel" aria-label="关闭">' + ico("x") + '</button></div>',
      '    <p class="mp-del-desc" id="mp-del-desc"></p>',
      '    <input type="password" id="mp-del-pw" placeholder="输入删除密码" autocomplete="current-password">',
      '    <p class="mp-del-err" id="mp-del-err"></p>',
      '    <div class="mp-del-actions">',
      '      <button type="button" data-act="del-cancel">取消</button>',
      '      <button type="button" data-act="del-confirm" class="danger">确认删除</button>',
      '    </div>',
      '  </div>',
      '</div>'
    ].join("");
    while (wrap.firstChild) document.body.appendChild(wrap.firstChild);

    setupCaptionDrag();
    setupSeekSlider();
    setupDrawerSwipe();
    bind();
    setupVolume();

    $("#mp-b-verify").addEventListener("click", verifyCurrentWork);
    $("#mp-b-del").addEventListener("click", function () {
      if (!S.work) return;
      requestDeleteWork(S.work.source_id, S.work.title, function () {
        closePage();
        if (window.switchTab) window.switchTab("library");
        if (window.loadLibrary) window.loadLibrary();
      });
    });
    $("#mp-del").addEventListener("click", function (e) {
      if (e.target === this) closeDelete();
    });
    // 点图片/视频弹窗外空白处关闭
    $("#mp-photo").addEventListener("click", function (e) {
      if (e.target === this) this.classList.remove("show");
    });
    $("#mp-video").addEventListener("click", function (e) {
      if (e.target === this) closeVideo();
    });
    $("#mp-del-pw").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); confirmDelete(); }
      if (e.key === "Escape") closeDelete();
    });
  }

  /* 字幕浮窗：可随意拖动，位置记忆在 localStorage */
  function setupCaptionDrag() {
    var cap = $("#mp-caption");
    if (!cap || cap.dataset.dragReady) return;
    cap.dataset.dragReady = "1";

    try {
      var saved = JSON.parse(localStorage.getItem("mp.cap.pos") || "null");
      if (saved && typeof saved.x === "number" && typeof saved.y === "number") {
        var x = Math.min(Math.max(4, saved.x), Math.max(4, window.innerWidth - 160));
        var y = Math.min(Math.max(4, saved.y), Math.max(4, window.innerHeight - 60));
        cap.style.left = x + "px";
        cap.style.top = y + "px";
        cap.style.bottom = "auto";
        cap.style.transform = "none";
      }
    } catch (e) {}

    var drag = null;

    cap.addEventListener("pointerdown", function (e) {
      var r = cap.getBoundingClientRect();
      drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
      // 每次开始拖动时把居中布局换算成绝对坐标，避免拖动瞬间跳动
      cap.style.transform = "none";
      cap.style.bottom = "auto";
      cap.style.left = r.left + "px";
      cap.style.top = r.top + "px";
      cap.classList.add("dragging");
      if (cap.setPointerCapture) {
        try { cap.setPointerCapture(e.pointerId); } catch (err) {}
      }
      e.preventDefault();
    });

    cap.addEventListener("pointermove", function (e) {
      if (!drag) return;
      var w = cap.offsetWidth, h = cap.offsetHeight;
      var x = Math.min(Math.max(4, e.clientX - drag.dx), Math.max(4, window.innerWidth - w - 4));
      var y = Math.min(Math.max(4, e.clientY - drag.dy), Math.max(4, window.innerHeight - h - 4));
      cap.style.left = x + "px";
      cap.style.top = y + "px";
    });

    function endDrag(e) {
      if (!drag) return;
      drag = null;
      cap.classList.remove("dragging");
      if (cap.releasePointerCapture && e && e.pointerId != null) {
        try { cap.releasePointerCapture(e.pointerId); } catch (err) {}
      }
      try {
        var r = cap.getBoundingClientRect();
        localStorage.setItem("mp.cap.pos", JSON.stringify({ x: Math.round(r.left), y: Math.round(r.top) }));
      } catch (err) {}
    }

    cap.addEventListener("pointerup", endDrag);
    cap.addEventListener("pointercancel", endDrag);
  }

  /* ---------------- 播放抽屉开关 ---------------- */

  function openDrawer() {
    var d = $("#mp-drawer");
    if (!d) return;
    d.style.transition = "";
    d.style.transform = "";
    d.style.opacity = "";
    d.classList.remove("mp-drawer-dragging");
    d.classList.add("show");
    d.scrollTop = 0;
    var cap = $("#mp-caption");
    if (cap) { cap.textContent = ""; cap.classList.remove("show"); }
    if (seekCtl) seekCtl.refresh();
  }

  function closeDrawer() {
    var d = $("#mp-drawer");
    if (!d) return;
    d.style.transition = "";
    d.style.transform = "";
    d.style.opacity = "";
    d.classList.remove("mp-drawer-dragging");
    d.classList.remove("show");
    paintCue(audio.currentTime);
  }

  /* ---------------- 弹性进度滑杆 ---------------- */

  function esClamp(v, min, max) { return Math.min(max, Math.max(min, v)); }

  // 与参考实现一致的 sigmoid 橡皮筋：越界像素 → 0..limit 的位移
  function esRubber(distance, limit) {
    return limit > 0 ? 2 * (1 / (1 + Math.exp(-distance / limit)) - 0.5) * limit : 0;
  }

  function setupSeekSlider() {
    var root = $("#mp-eslider");
    if (!root || root.dataset.ready) return;
    root.dataset.ready = "1";

    var track = $("#eslider-track");
    var range = $("#eslider-range");
    var labelStart = $("#eslider-icon-start");
    var labelEnd = $("#eslider-icon-end");
    var iconStart = $("#mp-cur");
    var iconEnd = $("#mp-dur");

    var mqReduce = window.matchMedia
      ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    var TAU = Math.PI * 2;
    var MAX_STRETCH = 50;
    var BOUNCE = 0.5;
    var THICK = 6;
    var ACTIVE_THICK = 12;
    var length = 1;
    var m = {
      p: 0, vp: 0, target: 0,
      s: 0, vs: 0, side: 1, held: false,
      a: 0, va: 0, goal: 0,
      dragging: false, hovered: false, focused: false,
      raf: 0, last: 0
    };

    function reduced() { return !!(mqReduce && mqReduce.matches); }
    function measure() { length = Math.max(1, root.clientWidth); }

    function paint() {
      var thick = THICK + (ACTIVE_THICK - THICK) * m.a;
      track.style.height = thick.toFixed(2) + "px";

      var len = Math.max(1, length);
      var along = 1 + m.s / len;
      var across = 1 - 0.22 * esClamp(m.s / MAX_STRETCH, -1, 1);
      var atEnd = m.side > 0;
      track.style.transformOrigin = atEnd ? "0% 50%" : "100% 50%";
      track.style.transform =
        "scale(" + along.toFixed(4) + ", " + across.toFixed(4) + ")";

      var p = esClamp(m.p, 0, 1);
      range.style.clipPath =
        "inset(0 " + ((1 - p) * 100).toFixed(3) + "% 0 0 round 999px)";

      var startShift = atEnd ? 0 : m.s;
      var endShift = atEnd ? m.s : 0;
      var iconScale = (1 + 0.08 * m.a).toFixed(3);
      labelStart.style.transform =
        "translate3d(" + (-startShift).toFixed(2) + "px,0,0) scale(" + iconScale + ")";
      labelEnd.style.transform =
        "translate3d(" + endShift.toFixed(2) + "px,0,0) scale(" + iconScale + ")";
    }

    function frame(now) {
      m.raf = 0;
      var elapsed = esClamp((now - m.last) / 1000, 0, 1 / 20);
      m.last = now;
      if (reduced()) {
        m.p = m.target; m.vp = 0;
        if (!m.held) { m.s = 0; m.vs = 0; }
        m.a = m.goal; m.va = 0;
      } else {
        var steps = Math.max(1, Math.ceil(elapsed / (1 / 240)));
        var dt = elapsed / steps;
        var fillOmega = TAU / (m.dragging ? 0.09 : 0.3);
        var fillZeta = m.dragging ? 1 : 0.86;
        var stretchOmega = TAU / 0.42;
        var stretchZeta = 1 - 0.9 * BOUNCE;
        var activeOmega = TAU / 0.32;
        for (var i = 0; i < steps; i++) {
          m.vp += (-fillOmega * fillOmega * (m.p - m.target) -
            2 * fillZeta * fillOmega * m.vp) * dt;
          m.p += m.vp * dt;
          if (!m.held) {
            m.vs += (-stretchOmega * stretchOmega * m.s -
              2 * stretchZeta * stretchOmega * m.vs) * dt;
            m.s += m.vs * dt;
          }
          m.va += (-activeOmega * activeOmega * (m.a - m.goal) -
            2 * 0.9 * activeOmega * m.va) * dt;
          m.a += m.va * dt;
        }
      }
      var settled =
        Math.abs(m.p - m.target) < 0.0004 &&
        Math.abs(m.vp) < 0.002 &&
        (m.held || (Math.abs(m.s) < 0.05 && Math.abs(m.vs) < 0.5)) &&
        Math.abs(m.a - m.goal) < 0.002 &&
        Math.abs(m.va) < 0.005;
      if (settled) {
        m.p = m.target; m.vp = 0;
        m.a = m.goal; m.va = 0;
        if (!m.held) { m.s = 0; m.vs = 0; }
      }
      paint();
      if (!settled || m.dragging) m.raf = requestAnimationFrame(frame);
    }

    function kick() {
      if (m.raf) return;
      m.last = performance.now();
      m.raf = requestAnimationFrame(frame);
    }

    function refreshGoal() {
      m.goal = (m.hovered || m.dragging || m.focused) ? 1 : 0;
      kick();
    }

    function pulse(side) {
      if (reduced()) return;
      var el = side > 0 ? iconEnd : iconStart;
      if (!el) return;
      el.classList.remove("pulse");
      void el.offsetWidth;
      el.classList.add("pulse");
    }
    iconStart.addEventListener("animationend", function () {
      iconStart.classList.remove("pulse");
    });
    iconEnd.addEventListener("animationend", function () {
      iconEnd.classList.remove("pulse");
    });

    function commit(frac) {
      var p = esClamp(frac, 0, 1);
      m.target = p;
      kick();
      var d = audio.duration || 0;
      if (d > 0) {
        var t = p * d;
        if (Math.abs(audio.currentTime - t) > 0.05) audio.currentTime = t;
        root.setAttribute("aria-valuenow", String(Math.round(p * 1000)));
        root.setAttribute("aria-valuetext", fmt(t) + " / " + fmt(d));
      }
    }

    function syncTargets() {
      measure();
      var d = audio.duration || 0;
      if (!m.dragging) m.target = d > 0 ? esClamp(audio.currentTime / d, 0, 1) : 0;
      if (d > 0) {
        root.setAttribute("aria-valuenow",
          String(Math.round(esClamp(audio.currentTime / d, 0, 1) * 1000)));
        root.setAttribute("aria-valuetext", fmt(audio.currentTime) + " / " + fmt(d));
      }
      kick();
    }

    function follow(e) {
      var rect = root.getBoundingClientRect();
      var len = Math.max(1, length);
      var position = e.clientX - rect.left;
      var fraction = position / len;
      var over = fraction < 0 ? -fraction * len : fraction > 1 ? (fraction - 1) * len : 0;
      if (over > 0 && MAX_STRETCH > 0) {
        var side = fraction < 0 ? -1 : 1;
        if (!m.held || m.side !== side) pulse(side);
        m.held = true;
        m.side = side;
        m.s = esRubber(over, MAX_STRETCH);
        m.vs = 0;
      } else if (m.held) {
        m.held = false;
      }
      commit(fraction);
    }

    function onDown(e) {
      if (e.button != null && e.button !== 0) return;
      m.dragging = true;
      root.classList.add("is-dragging");
      try { root.focus({ preventScroll: true }); } catch (err) {}
      try { root.setPointerCapture(e.pointerId); } catch (err) {}
      refreshGoal();
      follow(e);
      e.preventDefault();
    }
    function onMove(e) {
      if (!m.dragging) return;
      follow(e);
      e.preventDefault();
    }
    function release(e) {
      if (!m.dragging) return;
      m.dragging = false;
      m.held = false;
      root.classList.remove("is-dragging");
      if (e && e.pointerId != null && root.releasePointerCapture) {
        try { root.releasePointerCapture(e.pointerId); } catch (err) {}
      }
      refreshGoal();
    }

    root.addEventListener("pointerdown", onDown);
    root.addEventListener("pointermove", onMove);
    root.addEventListener("pointerup", release);
    root.addEventListener("pointercancel", release);
    root.addEventListener("pointerenter", function () { m.hovered = true; refreshGoal(); });
    root.addEventListener("pointerleave", function () { m.hovered = false; refreshGoal(); });
    root.addEventListener("focus", function () {
      m.focused = root.matches(":focus-visible");
      refreshGoal();
    });
    root.addEventListener("blur", function () { m.focused = false; refreshGoal(); });

    root.addEventListener("keydown", function (e) {
      var d = audio.duration || 0;
      if (!d) return;
      var now = audio.currentTime;
      var wanted = null;
      if (e.key === "ArrowRight" || e.key === "ArrowUp") wanted = now + 5;
      else if (e.key === "ArrowLeft" || e.key === "ArrowDown") wanted = now - 5;
      else if (e.key === "PageUp") wanted = now + 30;
      else if (e.key === "PageDown") wanted = now - 30;
      else if (e.key === "Home") wanted = 0;
      else if (e.key === "End") wanted = d;
      if (wanted === null) return;
      e.preventDefault();
      if (wanted < 0 || wanted > d) { pulse(wanted < 0 ? -1 : 1); return; }
      audio.currentTime = wanted;
      commit(wanted / d);
    });

    if (window.ResizeObserver) {
      try {
        new ResizeObserver(function () { measure(); paint(); }).observe(root);
      } catch (err) {}
    } else {
      window.addEventListener("resize", function () { measure(); paint(); });
    }

    seekCtl = {
      sync: syncTargets,
      refresh: function () { measure(); syncTargets(); },
      reset: function () {
        measure();
        m.p = 0; m.vp = 0; m.target = 0;
        m.s = 0; m.vs = 0; m.held = false;
        paint();
      }
    };
    measure();
    paint();
  }

  /* ---------------- 播放器下滑折叠 ---------------- */
  function setupDrawerSwipe() {
    var d = $("#mp-drawer");
    if (!d || d.dataset.swipeReady) return;
    d.dataset.swipeReady = "1";

    var start = null, active = false, dy = 0, height = 1;

    d.addEventListener("touchstart", function (e) {
      if (e.touches.length !== 1 || d.scrollTop > 0) { start = null; return; }
      if (e.target.closest("button,input,select,textarea,.eslider,.mp-vol-pop")) {
        start = null;
        return;
      }
      height = d.clientHeight || window.innerHeight || 1;
      start = { y: e.touches[0].clientY, x: e.touches[0].clientX };
      active = false;
      dy = 0;
    }, { passive: true });

    d.addEventListener("touchmove", function (e) {
      if (!start || e.touches.length !== 1) return;
      var t = e.touches[0];
      dy = t.clientY - start.y;
      var dx = t.clientX - start.x;
      if (!active) {
        if (dy > 8 && Math.abs(dy) > Math.abs(dx) && d.scrollTop <= 0) {
          active = true;
          d.classList.add("mp-drawer-dragging");
        } else if (Math.abs(dy) > 10 || Math.abs(dx) > 10) {
          start = null;
          return;
        } else {
          return;
        }
      }
      if (dy < 0) dy = 0;
      e.preventDefault();
      var p = Math.min(1, dy / height);
      d.style.transform = "translate3d(0," + dy + "px,0)";
      d.style.opacity = String(1 - p * 0.65);
    }, { passive: false });

    function finish() {
      var wasActive = active;
      start = null;
      active = false;
      if (!wasActive) return;
      d.classList.remove("mp-drawer-dragging");
      var p = Math.min(1, dy / height);
      d.style.transition = "transform 220ms cubic-bezier(0.2,0.8,0.2,1), opacity 200ms ease";
      if (p > 0.4) {
        d.style.transform = "translate3d(0,100%,0)";
        d.style.opacity = "0";
        setTimeout(function () {
          d.classList.remove("show");
          d.style.transition = "";
          d.style.transform = "";
          d.style.opacity = "";
          paintCue(audio.currentTime);
        }, 210);
      } else {
        d.style.transform = "translate3d(0,0,0)";
        d.style.opacity = "1";
        setTimeout(function () {
          d.style.transition = "";
          d.style.transform = "";
          d.style.opacity = "";
        }, 230);
      }
      dy = 0;
    }

    d.addEventListener("touchend", finish);
    d.addEventListener("touchcancel", finish);
  }

  /* ---------------- 事件 ---------------- */

  // 点击确认：播放键收缩回弹，上一首/下一首图标先位移再归位
  function popEl(el) {
    el.classList.remove("mp-pop");
    void el.offsetWidth;
    el.classList.add("mp-pop");
  }

  function nudgeIcon(btn, dir) {
    var svg = btn.querySelector("svg");
    if (!svg) return;
    svg.classList.remove("mp-nudge-r", "mp-nudge-l");
    void svg.getBoundingClientRect();
    svg.classList.add(dir > 0 ? "mp-nudge-r" : "mp-nudge-l");
  }

  // 前进/后退按钮：数字上跳，连续点击时每次重新起跳
  function jumpSeekNum(btn) {
    var num = btn.querySelector(".mp-seek-num");
    if (!num) return;
    num.classList.remove("mp-num-jump");
    void num.offsetWidth;
    num.classList.add("mp-num-jump");
  }

  function bind() {
    document.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      var act = btn.dataset.act;
      switch (act) {
        case "close-browser": closePage(); break;
        case "close-drawer": closeDrawer(); break;
        case "close-photo": $("#mp-photo").classList.remove("show"); break;
        case "close-video": closeVideo(); break;
        case "toggle": popEl(btn); togglePlay(); break;
        case "prev": nudgeIcon(btn, -1); step(-1); break;
        case "next": nudgeIcon(btn, 1); step(1); break;
        case "back10": jumpSeekNum(btn); seekBy(-10); break;
        case "back5": jumpSeekNum(btn); seekBy(-5); break;
        case "fwd5": jumpSeekNum(btn); seekBy(5); break;
        case "fwd10": jumpSeekNum(btn); seekBy(10); break;
        case "loop": cycleLoop(); break;
        case "sub": openSubModal(); break;
        case "close-subm": closeSubModal(); break;
        case "subm-minus": shiftSubOffset(-0.3); break;
        case "subm-zero": setSubOffset(0); break;
        case "subm-plus": shiftSubOffset(0.3); break;
        case "playlist": showExtras("playlist"); break;
        case "folder": {
          var sid = (S.play && S.play.work && S.play.work.source_id) ||
                    (S.work && S.work.source_id) || "";
          if (!sid) break;
          // 收起播放详情，同页打开作品目录；audio 不受影响，继续播放
          closeDrawer();
          openPage(sid, true);
          break;
        }
        case "photo-prev": showPhoto(-1); break;
        case "photo-next": showPhoto(1); break;
        case "del-cancel": closeDelete(); break;
        case "del-confirm": confirmDelete(); break;
      }
    });

    $("#mp-bar").addEventListener("click", function (e) {
      if (e.target.closest("[data-act]")) return;
      openDrawer();
    });

    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("durationchange", function () {
      $("#mp-dur").textContent = fmt(audio.duration);
    });
    audio.addEventListener("play", function () { paintPlayState(true); });
    audio.addEventListener("pause", function () { paintPlayState(false); });
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("error", function () {
      if (audio.src) $("#mp-bar-file").textContent = "加载失败：" + currentName();
    });

    // 字幕弹窗：重新匹配 / 点击跳转 / 手动滚动时暂停自动定位
    $("#mp-subm-match").addEventListener("change", function () {
      var rel = this.value;
      if (!rel) return;
      S.subOffset = 0;
      paintOffset();
      rememberSub(rel);
      fetchSubtitle(rel);
    });
    $("#mp-subm-list").addEventListener("click", function (e) {
      var row = e.target.closest("[data-ci]");
      if (!row) return;
      var cue = S.cues[+row.dataset.ci];
      if (!cue) return;
      audio.currentTime = Math.max(0, cue.start + S.subOffset);
      paintCue(audio.currentTime);
    });
    $("#mp-subm-list").addEventListener("scroll", function () {
      S.subUserScroll = Date.now();
    });
    $("#mp-subm").addEventListener("click", function (e) {
      if (e.target.id === "mp-subm") closeSubModal();
    });
  }

  /* ---------------- 音量：点开收起 + 声波滑槽 ---------------- */
  function setupVolume() {
    var wrap = $("#mp-vol");
    if (!wrap || wrap.dataset.ready) return;
    wrap.dataset.ready = "1";

    var btn = $("#mp-vol-btn");
    var pop = $("#mp-vol-pop");
    var track = $("#mp-vol-track");
    var slider = $("#mp-vol-slider");
    var val = $("#mp-vol-val");
    var BARS = 22;
    var REST = 0.14;
    var bars = [];
    for (var i = 0; i < BARS; i++) {
      var b = document.createElement("span");
      b.className = "wake-slider__bar";
      track.appendChild(b);
      bars.push(b);
    }

    var v = Math.max(0, Math.min(100, audio.volume * 100));
    var shown = v;
    var amp = 0;
    var dir = 1;
    var speed = 0;
    var raf = 0;
    var dragging = false;
    var lastX = -1;
    var lastT = 0;

    // 音量图标随音量变化：静音 / 低音量 / 高音量
    function paintVolIcon(value) {
      var btn = $("#mp-vol-btn");
      if (!btn) return;
      var name = value <= 0 ? "volume-x" : value < 50 ? "volume-1" : "volume-2";
      var use = btn.querySelector("use");
      if (use) use.setAttribute("href", "/icons.svg#i-" + name);
      var label = value <= 0 ? "静音" : "音量 " + Math.round(value) + "%";
      btn.title = label;
      btn.setAttribute("aria-label", label);
    }
    paintVolIcon(v);

    function paint() {
      var h = (shown / 100) * (BARS - 1);
      var reach = 4.6;
      var behind = reach * 1.7;
      var ahead = reach * 0.7;
      for (var i = 0; i < BARS; i++) {
        var on = i <= Math.round(h) ? "true" : "false";
        if (bars[i].dataset.on !== on) bars[i].dataset.on = on;
        var d = i - h;
        var R = d * dir < 0 ? behind : ahead;
        var lift = Math.abs(d) < R ? amp * Math.pow(Math.cos((Math.PI * d) / (2 * R)), 2) : 0;
        bars[i].style.transform = "scaleY(" + (REST + lift * (1 - REST)).toFixed(4) + ")";
      }
    }

    function tick() {
      raf = 0;
      var delta = v - shown;
      shown += delta * 0.32;
      if (Math.abs(delta) < 0.15) shown = v;
      if (dragging) {
        amp *= 0.985;
      } else {
        amp *= 0.93;
        speed *= 0.86;
        if (amp < 0.003) amp = 0;
      }
      paint();
      if (Math.abs(v - shown) > 0.1 || amp > 0.003) raf = requestAnimationFrame(tick);
    }
    function kick() {
      if (!raf) raf = requestAnimationFrame(tick);
    }

    function commit(next) {
      v = Math.max(0, Math.min(100, next));
      audio.volume = v / 100;
      localStorage.setItem("mp.vol", String(audio.volume));
      val.textContent = Math.round(v) + "%";
      slider.setAttribute("aria-valuenow", String(Math.round(v)));
      slider.setAttribute("aria-valuetext", Math.round(v) + "%");
      paintVolIcon(v);
      if (!dragging) amp = Math.max(amp, 0.55);
      kick();
    }

    function fromX(x) {
      var rect = track.getBoundingClientRect();
      if (!rect.width) return;
      var now = performance.now();
      var dt = now - lastT;
      if (dragging && dt > 0 && lastX >= 0) {
        var inst = Math.abs(x - lastX) / dt;
        speed += (inst - speed) * 0.5;
        var target = Math.min(1, 0.42 + speed / 1.1);
        if (target > amp) amp = target;
        if (x !== lastX) dir = x > lastX ? 1 : -1;
      }
      lastX = x;
      lastT = now;
      commit(((x - rect.left) / rect.width) * 100);
    }

    function open() {
      wrap.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
      pop.setAttribute("aria-hidden", "false");
      amp = 0.8;
      speed = 0;
      kick();
      try { slider.focus({ preventScroll: true }); } catch (e) {}
    }
    function close() {
      wrap.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
      pop.setAttribute("aria-hidden", "true");
      amp = 0;
      paint();
    }

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (wrap.classList.contains("open")) close(); else open();
    });

    track.addEventListener("pointerdown", function (e) {
      dragging = true;
      lastX = -1;
      lastT = performance.now();
      try { track.setPointerCapture(e.pointerId); } catch (err) {}
      fromX(e.clientX);
      amp = Math.max(amp, 0.55);
      kick();
      e.preventDefault();
    });
    track.addEventListener("pointermove", function (e) {
      if (dragging) fromX(e.clientX);
    });
    function stopDrag(e) {
      if (!dragging) return;
      dragging = false;
      speed = 0;
      amp = Math.max(amp, 0.5);
      try { track.releasePointerCapture(e.pointerId); } catch (err) {}
      kick();
    }
    track.addEventListener("pointerup", stopDrag);
    track.addEventListener("pointercancel", stopDrag);
    track.addEventListener("lostpointercapture", stopDrag);

    slider.addEventListener("keydown", function (e) {
      var jumps = { ArrowRight: 5, ArrowUp: 5, ArrowLeft: -5, ArrowDown: -5, PageUp: 10, PageDown: -10 };
      if (e.key === "Home") { commit(0); e.preventDefault(); return; }
      if (e.key === "End") { commit(100); e.preventDefault(); return; }
      if (jumps[e.key] != null) { commit(v + jumps[e.key]); e.preventDefault(); }
    });

    document.addEventListener("pointerdown", function (e) {
      if (wrap.classList.contains("open") && !wrap.contains(e.target)) close();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && wrap.classList.contains("open")) close();
    });

    commit(v);
    shown = v;
    paint();
  }

  /* ---------------- 文件浏览页（二级页面） ---------------- */

  var integritySeq = 0;

  function reportCount(report, key) {
    var value = Number(report[key + "_count"]);
    return isFinite(value) ? value : (report[key] || []).length;
  }

  function integrityTitle(work) {
    var report = work.integrity_report || {};
    var lines = ["校验依据：远程清单 + 本地文件字节大小"];
    function add(label, items, withSize) {
      (items || []).forEach(function (item) {
        var line = label + "：" + (item.path || "");
        if (withSize && item.expected_size != null) {
          line += "（应有 " + humanSize(item.expected_size) +
                  "，实际 " + humanSize(item.actual_size || 0) + "）";
        }
        lines.push(line);
      });
    }
    add("缺失", report.missing, false);
    add("大小不符", report.size_mismatch, true);
    (report.covers || []).forEach(function (item) {
      lines.push("封面：" + (item.path || "") +
                 "（" + humanSize(item.actual_size || 0) + "）");
    });
    add("多余", report.extra_files, false);
    add("远程大小未知", report.unknown_size, false);
    if (work.integrity_checked_at) lines.unshift("最近校验：" + work.integrity_checked_at);
    return lines.join("\n");
  }

  // 多余文件较少时直接显示文件名（通常是自动刮削的 cover.jpg）
  function extraNames(report) {
    var items = report.extra_files || [];
    if (!items.length || items.length > 2) return "";
    return "（" + items.map(function (i) { return i.path; }).join("、") + "）";
  }

  function renderIntegrity(work) {
    var el = $("#mp-integrity");
    if (!el) return;
    $("#mp-b-verify").disabled = false;
    var status = work.integrity_status || "unchecked";
    var report = work.integrity_report || {};
    var missing = reportCount(report, "missing");
    var mismatch = reportCount(report, "size_mismatch");
    var extra = reportCount(report, "extra_files");
    var unknown = reportCount(report, "unknown_size");
    var covers = reportCount(report, "covers");
    var text = "未校验";
    var iconName = "check";
    if (status === "complete") {
      text = "完整 · " + (report.expected_files || 0) + " 个文件";
      if (covers) text += " · 封面 " + covers;
      if (extra) text += " · 多余 " + extra + extraNames(report);
      if (unknown) text += " · 大小未知 " + unknown;
    } else if (status === "incomplete") {
      text = "不完整 · 缺失 " + missing + " · 大小不符 " + mismatch;
      if (covers) text += " · 封面 " + covers;
      if (extra) text += " · 多余 " + extra + extraNames(report);
      if (unknown) text += " · 大小未知 " + unknown;
      iconName = "alert";
    } else if (status === "error") {
      text = "校验失败：" + (report.error || "未知错误");
      iconName = "alert";
    }
    el.className = "mp-integrity " + status;
    el.innerHTML = ico(iconName) + "<span>" + esc(text) + "</span>";
    el.title = integrityTitle(work);
  }

  function verifyCurrentWork() {
    if (!S.work || $("#mp-b-verify").disabled) return;
    var sourceId = S.work.source_id;
    var seq = ++integritySeq;
    var btn = $("#mp-b-verify");
    var el = $("#mp-integrity");
    btn.disabled = true;
    el.className = "mp-integrity checking";
    el.innerHTML = ico("refresh") + "<span>校验中…</span>";
    el.title = "正在获取远程清单并核对本地文件大小";

    fetch("/api/library/works/" + encodeURIComponent(sourceId) + "/verify", { method: "POST" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.d.message || res.d.error || "请求失败");
        if (seq !== integritySeq || !S.work || S.work.source_id !== sourceId) return;
        S.work.integrity_status = res.d.status;
        S.work.integrity_checked_at = res.d.checked_at;
        S.work.integrity_report = res.d;
        renderIntegrity(S.work);
        if (window.loadLibrary) window.loadLibrary();
      })
      .catch(function (e) {
        if (seq !== integritySeq || !S.work || S.work.source_id !== sourceId) return;
        btn.disabled = false;
        el.className = "mp-integrity error";
        el.innerHTML = ico("alert") + "<span>校验失败：" + esc(e.message) + "</span>";
        el.title = e.message;
      });
  }

  function pageURL(sourceId) {
    return "/library/" + encodeURIComponent(sourceId);
  }

  // push=true 时把当前地址推进历史记录；从地址栏直接进入时传 false
  function openPage(sourceId, push, dir) {
    ensureDom();
    integritySeq++;
    return fetch("/api/library/tree/" + encodeURIComponent(sourceId))
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { alert("读取作品目录失败：" + (res.d.error || "未知错误")); return; }
        S.work = res.d;
        S.dir = dir || "";
        $("#mp-b-cover").src = res.d.cover_url;
        $("#mp-b-title").textContent = res.d.title;
        $("#mp-b-id").textContent = res.d.source_id;
        renderIntegrity(res.d);
        loadTotalDuration(res.d);
        renderBrowser();
        $("#mp-browser").classList.add("show");
        document.body.classList.add("mp-page-open");
        // depth：当前 /library 历史条目距进入前页面的步数，返回键用它一次跳完
        if (push && location.pathname !== pageURL(sourceId)) {
          history.pushState({ libPage: sourceId, dir: S.dir, depth: 1 }, "", pageURL(sourceId));
        } else if (!history.state || !history.state.libPage) {
          history.replaceState({ libPage: sourceId, dir: S.dir, depth: 0 }, "", pageURL(sourceId));
        }
      })
      .catch(function (e) { alert("读取作品目录失败：" + e.message); });
  }

  // 目录切换：push 进入下一级，replace 用于面包屑跳级；系统返回键逐级回退
  function navToDir(dir, mode) {
    if (!S.work || dir === S.dir) return;
    var prevDepth = S.dir ? S.dir.split("/").length : 0;
    var nextDepth = dir ? dir.split("/").length : 0;
    S.dir = dir;
    renderBrowser();
    var list = $("#mp-list");
    if (list) {
      list.classList.remove("mp-slide-forward", "mp-slide-back");
      if (nextDepth !== prevDepth) {
        void list.offsetWidth;
        list.classList.add(nextDepth > prevDepth ? "mp-slide-forward" : "mp-slide-back");
      }
    }
    var state = { libPage: S.work.source_id, dir: dir };
    if (mode === "push") {
      state.depth = ((history.state && history.state.depth) || 0) + 1;
      history.pushState(state, "", location.href);
    } else if (mode === "replace") {
      state.depth = (history.state && history.state.depth) || 0;
      history.replaceState(state, "", location.href);
    }
  }

  // 关闭页面只改视觉与地址，不触碰 audio，播放不会中断
  // 返回键直接回「本地资源」：按 depth 一次跳完所有 /library 历史
  // （含同级文件夹来回跳转产生的条目），不再逐级原路返回
  function closePage() {
    var box = $("#mp-browser");
    if (box) box.classList.remove("show");
    document.body.classList.remove("mp-page-open");
    if (location.pathname.indexOf("/library/") !== 0) return;
    S.dir = "";
    var depth = history.state && typeof history.state.depth === "number" ? history.state.depth : 0;
    if (depth > 0) {
      S.backToLib = true; // popstate 落回非 /library 后切到「本地资源」面板
      history.go(-depth);
      return;
    }
    // 地址栏直达 / 外链进入：无可退历史，直接回「本地资源」
    history.replaceState(null, "", "/");
    if (window.switchTab) window.switchTab("library");
  }

  window.addEventListener("popstate", function (ev) {
    var m = location.pathname.match(/^\/library\/([^/]+)\/?$/);
    if (m) {
      var st = ev.state || {};
      var sid = decodeURIComponent(m[1]);
      var prevDepth = S.dir ? S.dir.split("/").length : 0;
      openPage(sid, false, st.libPage === sid ? st.dir || "" : "").then(function () {
        var nextDepth = S.dir ? S.dir.split("/").length : 0;
        var list = $("#mp-list");
        if (list && nextDepth !== prevDepth) {
          list.classList.remove("mp-slide-forward", "mp-slide-back");
          void list.offsetWidth;
          list.classList.add(nextDepth < prevDepth ? "mp-slide-back" : "mp-slide-forward");
        }
      });
    } else {
      var box = $("#mp-browser");
      if (box) box.classList.remove("show");
      document.body.classList.remove("mp-page-open");
      // 返回键跳出目录页后：切到「本地资源」面板（switchTab 内部会刷新列表）
      if (S.backToLib) {
        S.backToLib = false;
        if (window.switchTab) window.switchTab("library");
      }
    }
  });

  window.MediaPlayer = {
    browse: function (sourceId) { return openPage(sourceId, true); },
    open: openPage,
    close: closePage,
    requestDelete: requestDeleteWork
  };

  /* ---------------- 删除本地作品 ---------------- */

  var delTarget = null;

  function requestDeleteWork(sourceId, title, done) {
    ensureDom();
    delTarget = { id: sourceId, title: title || sourceId, done: done || null };
    $("#mp-del-desc").textContent =
      "将删除《" + (title || sourceId) + "》的全部本地文件，操作无法恢复。";
    $("#mp-del-pw").value = "";
    $("#mp-del-err").textContent = "";
    $("#mp-del").classList.add("show");
    setTimeout(function () { $("#mp-del-pw").focus(); }, 30);
  }

  function closeDelete() {
    var box = $("#mp-del");
    if (box) box.classList.remove("show");
    delTarget = null;
  }

  function confirmDelete() {
    if (!delTarget) return;
    var pw = $("#mp-del-pw").value;
    if (!pw) {
      $("#mp-del-err").textContent = "请输入删除密码";
      return;
    }
    var btn = document.querySelector('[data-act="del-confirm"]');
    var target = delTarget;
    btn.disabled = true;
    $("#mp-del-err").textContent = "";
    fetch("/api/library/works/" + encodeURIComponent(target.id), {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw })
    })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (res) {
        btn.disabled = false;
        if (!res.ok) {
          $("#mp-del-err").textContent = res.d.message || "删除失败";
          return;
        }
        closeDelete();
        stopIfPlaying(target.id);
        if (target.done) target.done();
      })
      .catch(function (e) {
        btn.disabled = false;
        $("#mp-del-err").textContent = "删除失败：" + e.message;
      });
  }

  function stopIfPlaying(sourceId) {
    if (!S.play || !S.play.work || S.play.work.source_id !== sourceId) return;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    S.play = null;
    $("#mp-bar").classList.remove("show");
    $("#mp-drawer").classList.remove("show");
    document.title = "ASMR WebUI";
  }

  function nodesOf(rel) {
    var nodes = S.work.tree, parts = rel ? rel.split("/") : [];
    for (var i = 0; i < parts.length; i++) {
      var found = null;
      for (var j = 0; j < nodes.length; j++) {
        if (nodes[j].type === "dir" && nodes[j].name === parts[i]) { found = nodes[j]; break; }
      }
      if (!found) return [];
      nodes = found.children || [];
    }
    return nodes;
  }

  function renderBrowser() {
    var nodes = nodesOf(S.dir);
    var c = S.work.counts || {};
    probeQueue.length = 0; // 切换目录后丢弃旧的探测队列

    // 面包屑
    var crumb = '<a data-dir="">' + ico("folder") + '根目录</a>';
    if (S.dir) {
      var acc = [];
      S.dir.split("/").forEach(function (p) {
        acc.push(p);
        crumb += '<span>/</span><a data-dir="' + esc(acc.join("/")) + '">' + esc(p) + "</a>";
      });
    }
    crumb += '<span class="mp-crumb-count">音频 ' + (c.audio || 0) +
             ' · 图片 ' + (c.image || 0) + ' · 视频 ' + (c.video || 0) + "</span>";
    $("#mp-crumb").innerHTML = crumb;
    $("#mp-crumb").querySelectorAll("a[data-dir]").forEach(function (a) {
      a.onclick = function () { navToDir(a.dataset.dir, "replace"); };
    });

    if (!nodes.length) {
      $("#mp-list").innerHTML = '<div class="mp-empty">这个文件夹是空的</div>';
      return;
    }

    $("#mp-list").innerHTML = nodes.map(function (n, i) {
      if (n.type === "dir") {
        var subs = (n.children || []).length;
        return '<div class="mp-row dir" data-i="' + i + '"><span class="mp-ico">' + ico("folder") + '</span>' +
               '<span class="mp-name">' + esc(n.name) + '</span>' +
               '<span class="mp-meta">' + subs + ' 项</span></div>';
      }
      var kindIcon = n.kind === "audio" ? "music" : n.kind === "image" ? "image" :
                     n.kind === "video" ? "film" : n.kind === "subtitle" ? "file" : "file";
      var mediadur = (n.kind === "audio" || n.kind === "video") ? S.dur[n.rel] : null;
      var durHTML = (n.kind === "audio" || n.kind === "video")
        ? '<span class="mp-meta mp-dur" data-rel="' + esc(n.rel) + '">' + fmtDur(mediadur) + "</span>"
        : "";
      return '<div class="mp-row file ' + n.kind + '" data-i="' + i + '"><span class="mp-ico">' + ico(kindIcon) + '</span>' +
             '<span class="mp-name">' + esc(n.name) + '</span>' +
             durHTML +
             '<span class="mp-meta mp-size">' + humanSize(n.size) + "</span></div>";
    }).join("");

    probeDurations(nodes);

    $("#mp-list").querySelectorAll(".mp-row").forEach(function (row) {
      row.onclick = function () {
        var n = nodes[+row.dataset.i];
        if (n.type === "dir") { navToDir(n.rel, "push"); return; }
        if (n.kind === "audio") playRel(n.rel);
        else if (n.kind === "image") openPhoto(n.rel);
        else if (n.kind === "video") openVideo(n.rel);
        else if (n.kind === "subtitle") window.open(rawURL(n.rel), "_blank");
      };
    });
  }

  /* ---------------- 媒体时长探测（复用同一个元素，串行执行） ---------------- */

  var probeEl = null;
  var probeQueue = [];
  var probing = false;

  function setDurCell(rel, text) {
    var cells = document.querySelectorAll("#mp-list .mp-dur");
    for (var i = 0; i < cells.length; i++) {
      if (cells[i].dataset.rel === rel) cells[i].textContent = text;
    }
  }

  function probeDurations(nodes) {
    nodes.forEach(function (n) {
      if (n.type !== "file") return;
      if (n.kind !== "audio" && n.kind !== "video") return;
      if (S.dur[n.rel]) { setDurCell(n.rel, fmtDur(S.dur[n.rel])); return; }
      if (probeQueue.indexOf(n.rel) < 0) probeQueue.push(n.rel);
    });
    pumpProbe();
  }

  function pumpProbe() {
    if (probing || !probeQueue.length) return;
    var rel = probeQueue.shift();
    if (S.dur[rel]) { setDurCell(rel, fmtDur(S.dur[rel])); setTimeout(pumpProbe, 10); return; }

    probing = true;
    if (!probeEl) {
      probeEl = document.createElement("video");
      probeEl.preload = "metadata";
      probeEl.muted = true;
    }
    var el = probeEl;

    function done(sec) {
      el.removeEventListener("loadedmetadata", onMeta);
      el.removeEventListener("error", onErr);
      clearTimeout(timer);
      el.removeAttribute("src");
      el.load();
      if (sec > 0) S.dur[rel] = sec;
      setDurCell(rel, sec > 0 ? fmtDur(sec) : "--:--");
      probing = false;
      setTimeout(pumpProbe, 30);
    }
    function onMeta() { done(el.duration); }
    function onErr() { done(0); }
    var timer = setTimeout(function () { done(0); }, 20000);

    el.addEventListener("loadedmetadata", onMeta);
    el.addEventListener("error", onErr);
    el.src = rawURL(rel);
  }

  /* ---------------- 作品总时长（按音轨名去重，逐个探测元数据） ---------------- */

  function fmtTotal(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    if (h > 0) return h + ":" + ("0" + m).slice(-2) + ":" + ("0" + s).slice(-2);
    return m + ":" + ("0" + s).slice(-2);
  }

  function setTotalText(sec) {
    var el = $("#mp-b-total-val");
    if (el) el.textContent = sec == null ? "--:--" : fmtTotal(sec);
  }

  // 同一作品常有 wav/mp3、SE 有/无 等多份重复音轨，按去掉扩展名的文件名去重
  function collectTracks(nodes, map) {
    (nodes || []).forEach(function (n) {
      if (n.type === "dir") { collectTracks(n.children, map); return; }
      if (!AUDIO_EXT.test(n.name)) return;
      var base = n.name.replace(/\.[^.]+$/, "").toLowerCase();
      if (!map[base]) map[base] = n.rel;
    });
    return map;
  }

  function probeMeta(rel, workId) {
    return new Promise(function (resolve) {
      var el = document.createElement("audio");
      var settled = false;
      el.preload = "metadata";
      function finish(sec) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        el.removeEventListener("loadedmetadata", onMeta);
        el.removeEventListener("error", onErr);
        el.removeAttribute("src");
        resolve(isFinite(sec) && sec > 0 ? sec : 0);
      }
      function onMeta() { finish(el.duration); }
      function onErr() { finish(0); }
      var timer = setTimeout(function () { finish(0); }, 15000);
      el.addEventListener("loadedmetadata", onMeta);
      el.addEventListener("error", onErr);
      el.src = rawURL(rel, workId);
      el.load();
    });
  }

  var totalToken = 0;

  function loadTotalDuration(work) {
    var token = ++totalToken;
    setTotalText(null);
    // 优先由服务端 ffprobe 统计（快且不受浏览器解码限制），失败再回退前端探测
    fetch("/api/library/duration/" + encodeURIComponent(work.source_id))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (token !== totalToken) return;
        if (j && j.total > 0) { setTotalText(j.total); return; }
        probeTotal(work, token);
      })
      .catch(function () {
        if (token === totalToken) probeTotal(work, token);
      });
  }

  function probeTotal(work, token) {
    var map = collectTracks(work.tree, {});
    var rels = Object.keys(map).map(function (k) { return map[k]; });
    if (!rels.length) { setTotalText(0); return; }

    var total = 0, finished = 0, cursor = 0, active = 0;
    var pending = [];
    rels.forEach(function (rel) {
      if (S.dur[rel] > 0) { total += S.dur[rel]; finished++; }
      else pending.push(rel);
    });
    if (!pending.length) { setTotalText(total); return; }

    function next() {
      if (token !== totalToken) return;
      while (active < 3 && cursor < pending.length) {
        (function (rel) {
          active++;
          probeMeta(rel, work.source_id).then(function (sec) {
            if (token !== totalToken) return;
            active--;
            finished++;
            if (sec > 0) { S.dur[rel] = sec; total += sec; }
            if (finished === rels.length) setTotalText(total);
            else next();
          });
        })(pending[cursor++]);
      }
    }
    next();
  }

  /* ---------------- 播放 ---------------- */

  function audioInDir() {
    return nodesOf(S.dir).filter(function (n) { return n.type === "file" && AUDIO_EXT.test(n.name); });
  }

  function playRel(rel) {
    var queue = audioInDir();
    var index = queue.findIndex(function (n) { return n.rel === rel; });
    if (index < 0) return;
    // 播放上下文快照：之后浏览其他作品不会影响正在播放的队列与封面
    S.play = { work: S.work, dir: S.dir, queue: queue, index: index };
    loadCurrent(true);
  }

  function currentName() {
    var p = S.play;
    return p && p.index >= 0 && p.queue[p.index] ? p.queue[p.index].name : "";
  }

  function loadCurrent(autoplay) {
    var p = S.play;
    if (!p || !p.work) return;
    var n = p.queue[p.index];
    if (!n) return;
    audio.src = rawURL(n.rel, p.work.source_id);
    audio.playbackRate = 1;
    S.subOffset = 0;
    paintOffset();
    if (autoplay) audio.play().catch(function () {});

    $("#mp-bar").classList.add("show");
    $("#mp-bar-cover").src = p.work.cover_url;
    $("#mp-bar-title").textContent = p.work.title;
    $("#mp-bar-file").textContent = n.name;
    $("#mp-d-cover").src = p.work.cover_url;
    $("#mp-d-title").textContent = p.work.title;
    $("#mp-d-file").textContent = n.name;
    $("#mp-d-sub").textContent = "";
    if (seekCtl) seekCtl.reset();

    loadSubtitleFor(n.rel);
    if (S.extras === "playlist") showExtras("playlist");
    paintPlayState(!audio.paused);
  }

  function togglePlay() {
    if (!audio.src) return;
    if (audio.paused) audio.play().catch(function () {}); else audio.pause();
  }

  function paintPlayState(playing) {
    var glyph = ico(playing ? "pause" : "play");
    var a = $("#mp-bar-toggle"), b = $("#mp-d-toggle");
    if (a) a.innerHTML = glyph;
    if (b) b.innerHTML = glyph;
    document.title = playing ? currentName() : "ASMR WebUI";
  }

  function step(delta) {
    var p = S.play;
    if (!p || !p.queue.length) return;
    p.index = (p.index + delta + p.queue.length) % p.queue.length;
    loadCurrent(true);
  }

  function seekBy(sec) {
    if (!audio.duration) return;
    audio.currentTime = Math.max(0, Math.min(audio.duration, audio.currentTime + sec));
  }

  function onTime() {
    var d = audio.duration || 0, cur = audio.currentTime || 0;
    if (seekCtl) seekCtl.sync();
    if ($("#mp-cur")) $("#mp-cur").textContent = fmt(cur);
    if ($("#mp-bar-fill")) $("#mp-bar-fill").style.width = (d ? cur / d * 100 : 0) + "%";
    paintCue(cur);
  }

  function onEnded() {
    var p = S.play;
    if (!p) return;
    if (S.loop === "one") { audio.currentTime = 0; audio.play().catch(function () {}); return; }
    // 始终自动下一首：同目录播完继续，最后再按循环模式处理
    if (p.index < p.queue.length - 1) { step(1); return; }
    if (S.loop === "all" && p.queue.length) { p.index = 0; loadCurrent(true); return; }
    paintPlayState(false);
  }

  function cycleLoop() {
    S.loop = S.loop === "all" ? "one" : S.loop === "one" ? "off" : "all";
    var btn = $("#mp-loop-btn");
    if (!btn) return;
    var label = { all: "列表循环", one: "单曲循环", off: "顺序播放" }[S.loop];
    btn.innerHTML = ico(S.loop === "all" ? "repeat" : S.loop === "one" ? "repeat-1" : "order");
    btn.title = label;
    btn.setAttribute("aria-label", label);
  }

  /* ---------------- 字幕 ---------------- */

  function allFiles(nodes, acc) {
    acc = acc || [];
    (nodes || []).forEach(function (n) {
      if (n.type === "dir") allFiles(n.children || [], acc);
      else acc.push(n);
    });
    return acc;
  }

  function normStem(name) {
    return name
      .replace(/\.(zh|ja|en|ko)\.(vtt|srt|ass|ssa|lrc)$/i, "")
      .replace(SUB_EXT, "")
      .replace(AUDIO_EXT, "")
      .replace(/\s+/g, "")
      .toLowerCase();
  }

  function dirOf(rel) {
    return rel.indexOf("/") >= 0 ? rel.slice(0, rel.lastIndexOf("/")) : "";
  }

  // 字幕匹配度：同名 10 分、同前缀 6 分、同目录 +3 分（满分 13）；生成的 .zh.vtt 优先
  function subScore(node, stem, dir) {
    var s = normStem(node.name);
    var score = genScore(s, stem);
    if (!score) {
      score = s === stem ? 10 : (s && stem && (s.indexOf(stem) === 0 || stem.indexOf(s) === 0)) ? 6 : 1;
    }
    // 生成字幕/<rel>.zh.vtt 视作与音频同目录
    var nd = dirOf(node.rel).replace(/(^|\/)生成字幕$/, "");
    if (nd === dir) score += 3;
    if (/\.zh\.vtt$/i.test(node.name)) score += 0.6;
    return score;
  }

  // 生成字幕文件名是「<类别>_<RJ号>_<展平后的音频相对路径>.vtt」，
  // 与音频原始文件名不同，必须单独比对，否则永远匹配不上。
  function genScore(s, stem) {
    var rest = s.replace(/^(日文|中文|双语)_/, "");
    if (rest === s) return 0;
    var rj = rest.match(/^rj\d+_/);
    if (!rj) return 0;
    var flat = stem.replace(/[/\\]/g, "_");
    var tail = rest.slice(rj[0].length);
    var head = tail.replace(/….*$/, "");
    var score = 0;
    if (tail === flat) score = 10;
    else if (flat && tail.indexOf(flat) === 0) score = 8;
    else if (head && flat.indexOf(head) === 0) score = 8;
    else if (flat.length > 12 && tail.indexOf(flat.slice(-16)) >= 0) score = 6;
    if (!score) return 0;
    if (s.indexOf("中文_") === 0) score += 0.6;
    else if (s.indexOf("双语_") === 0) score += 0.4;
    return score;
  }

  // 该作品目录下所有字幕文件，按与当前音频的匹配度降序
  function subtitleFiles(rel, work) {
    var w = work || (S.play && S.play.work) || S.work;
    if (!w) return [];
    var stem = normStem((rel || "").split("/").pop() || "");
    var dir = dirOf(rel || "");
    return allFiles(w.tree)
      .filter(function (n) { return SUB_EXT.test(n.name); })
      .map(function (n) { return { node: n, score: subScore(n, stem, dir) }; })
      .sort(function (a, b) { return b.score - a.score || a.node.name.localeCompare(b.node.name); });
  }

  function findSubtitle(rel, work) {
    var list = subtitleFiles(rel, work);
    return list.length && list[0].score >= 4 ? list[0].node : null;
  }

  function rememberSub(rel) {
    var p = S.play;
    S.subSeq = { work: p && p.work ? p.work.source_id : "", dir: dirOf(rel), rel: rel };
  }

  // 上一首手动选的（或自动匹配到的）字幕所在目录里的下一个字幕
  function nextSubInDir(last) {
    var p = S.play;
    var w = p && p.work;
    if (!w || !last || !last.dir) return null;
    if (last.work !== w.source_id) return null;
    var list = allFiles(w.tree)
      .filter(function (n) { return SUB_EXT.test(n.name) && dirOf(n.rel) === last.dir; })
      .sort(function (a, b) {
        return a.name.localeCompare(b.name, undefined, { numeric: true });
      });
    var i = list.findIndex(function (n) { return n.rel === last.rel; });
    if (i < 0) return list[0] || null;
    return list[i + 1] || null;
  }

  function loadSubtitleFor(audioRel) {
    S.cues = [];
    S.subRel = "";
    S.subOffset = 0;
    var best = findSubtitle(audioRel, S.play && S.play.work);
    if (!best) best = nextSubInDir(S.subSeq);
    if (!best) {
      setSubBtn(false);
      $("#mp-d-sub").textContent = "";
      renderCueList();
      return;
    }
    rememberSub(best.rel);
    setSubBtn(true);
    fetchSubtitle(best.rel);
  }

  function fetchSubtitle(rel) {
    S.subRel = rel;
    return fetch(rawURL(rel, S.play && S.play.work && S.play.work.source_id))
      .then(function (r) { return r.text(); })
      .then(function (txt) {
        S.cues = parseSubs(txt);
        S.subCue = -1;
        setSubBtn(S.cues.length > 0);
        renderCueList();
        syncCueHighlight(audio.currentTime, true);
        paintCue(audio.currentTime);
      })
      .catch(function () {});
  }

  function setSubBtn(on) {
    var b = $("#mp-sub-btn");
    if (!b) return;
    b.classList.toggle("dim", !on);
    b.title = on ? "字幕" : "无字幕";
    b.setAttribute("aria-label", b.title);
  }

  function parseSubs(text) {
    var t = String(text || "").replace(/\r/g, "");
    if (/^\s*WEBVTT/i.test(t)) return parseTimed(t.replace(/^\s*WEBVTT.*$/im, ""));
    if (/\[Script Info\]|\[Events\]/i.test(t)) return parseASS(t);
    if (!t.match(/-->/) && /^\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]/m.test(t)) return parseLRC(t);
    return parseTimed(t);
  }

  // LRC：一行可带多个 [mm:ss.xx] 时间标签，结束时间取下一句开始（兜底 4 秒）
  function parseLRC(t) {
    var raw = [];
    t.split("\n").forEach(function (line) {
      var rest = line, stamp, times = [];
      while ((stamp = rest.match(/^\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/))) {
        times.push((+stamp[1]) * 60 + (+stamp[2]) + (stamp[3] ? Number("0." + stamp[3]) : 0));
        rest = rest.slice(stamp[0].length);
      }
      // 去掉逐字时间标签 <mm:ss.xx>，歌词正文保留
      var text = rest.replace(/<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>/g, "").trim();
      if (!times.length || !text) return;
      times.forEach(function (s) { raw.push({ start: s, text: text }); });
    });
    raw.sort(function (a, b) { return a.start - b.start; });
    var out = [];
    raw.forEach(function (c, i) {
      var next = raw[i + 1];
      var end = next ? next.start : c.start + 4;
      if (end - c.start < 0.4) end = c.start + 0.4;
      out.push({ start: c.start, end: end, text: c.text });
    });
    return out;
  }

  function toSec(s) {
    var m = s.trim().match(/^(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})$/);
    if (!m) return null;
    return (+(m[1] || 0)) * 3600 + (+m[2]) * 60 + (+m[3]) + (+(m[4] + "00").slice(0, 3)) / 1000;
  }

  function parseTimed(t) {
    var out = [], blocks = t.split(/\n{2,}/);
    blocks.forEach(function (b) {
      var lines = b.split("\n").filter(function (l) { return l.trim() !== ""; });
      if (!lines.length) return;
      var idx = lines.findIndex(function (l) { return l.indexOf("-->") >= 0; });
      if (idx < 0) return;
      var parts = lines[idx].split("-->");
      // 结束时间后面可能跟 VTT 的 cue 设置（position/align 等），先 trim 再取第一个字段
      var a = toSec(parts[0]), z = toSec((parts[1] || "").trim().split(/\s+/)[0]);
      if (a == null || z == null) return;
      var text = lines.slice(idx + 1).join("\n").replace(/<[^>]*>/g, "");
      out.push({ start: a, end: z, text: text });
    });
    return out;
  }

  function parseASS(t) {
    var out = [], inEv = false;
    t.split("\n").forEach(function (line) {
      var s = line.trim();
      if (s.charAt(0) === "[") { inEv = /^\[events\]/i.test(s); return; }
      if (!inEv || !/^dialogue:/i.test(s)) return;
      var f = s.split(",");
      if (f.length < 10) return;
      var a = toSec(f[1].trim()), z = toSec(f[2].trim());
      if (a == null || z == null) return;
      var text = f.slice(9).join(",").replace(/\{[^}]*\}/g, "").replace(/\\N/g, "\n").trim();
      if (text) out.push({ start: a, end: z, text: text });
    });
    return out;
  }

  function paintCue(now) {
    var t = now - S.subOffset;
    var txt = "";
    for (var i = 0; i < S.cues.length; i++) {
      if (t >= S.cues[i].start && t <= S.cues[i].end) { txt = S.cues[i].text; break; }
    }
    var el = $("#mp-d-sub");
    if (el && el.textContent !== txt) el.textContent = txt;

    // 字幕浮窗：抽屉关闭时悬浮显示，可拖动
    var cap = $("#mp-caption");
    if (cap) {
      var drawerOpen = $("#mp-drawer").classList.contains("show");
      var barShown = $("#mp-bar").classList.contains("show");
      if (S.subOn && !drawerOpen && barShown && S.cues.length) {
        cap.classList.add("show");
        if (txt) {
          if (cap.textContent !== txt) cap.textContent = txt;
          cap.classList.remove("idle");
        } else {
          // 两句字幕之间保留上一条，降低不透明度，避免浮窗反复闪现
          cap.classList.add("idle");
        }
      } else {
        cap.classList.remove("show");
      }
    }
    syncCueHighlight(now);
  }

  /* ---------------- 字幕详情弹窗 ---------------- */

  function fmtStamp(t) {
    if (!isFinite(t) || t < 0) t = 0;
    var m = Math.floor(t / 60), s = t % 60;
    return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
  }

  function openSubModal() {
    ensureDom();
    S.subOpen = true;
    S.subCue = -1;
    buildSubMatches();
    renderCueList();
    paintOffset();
    $("#mp-subm").classList.add("show");
    syncCueHighlight(audio.currentTime, true);
  }

  function closeSubModal() {
    S.subOpen = false;
    var box = $("#mp-subm");
    if (box) box.classList.remove("show");
  }

  function buildSubMatches() {
    var sel = $("#mp-subm-match");
    if (!sel) return;
    var p = S.play;
    var rel = p && p.queue[p.index] ? p.queue[p.index].rel : "";
    var list = subtitleFiles(rel, p && p.work);
    if (!list.length) {
      sel.innerHTML = '<option value="">（该文件夹内没有字幕文件）</option>';
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML = list.map(function (item) {
      var pct = Math.round(item.score / 13 * 100);
      return '<option value="' + esc(item.node.rel) + '">' + esc(item.node.name) +
        " · 匹配 " + pct + "%</option>";
    }).join("");
    if (S.subRel) sel.value = S.subRel;
    // 打开弹窗时还没有加载到字幕（例如生成字幕命名未自动匹配），
    // 直接选中最接近的一条并加载，避免只有一个字幕时无从选择
    if (!S.subRel && !S.cues.length) {
      var top = list[0].node;
      sel.value = top.rel;
      fetchSubtitle(top.rel);
    }
  }

  function renderCueList() {
    var box = $("#mp-subm-list");
    if (!box) return;
    if (!S.cues.length) {
      var sel = $("#mp-subm-match");
      box.innerHTML = sel && sel.disabled
        ? '<div class="mp-subm-empty">该作品没有字幕文件</div>'
        : '<div class="mp-subm-empty">当前文件未匹配到字幕，可在上方重新选择</div>';
      return;
    }
    box.innerHTML = S.cues.map(function (c, i) {
      return '<button type="button" class="mp-cue" data-ci="' + i + '">' +
        '<span class="mp-cue-t">' + fmtStamp(c.start + S.subOffset) + "</span>" +
        '<span class="mp-cue-x">' + esc(c.text) + "</span></button>";
    }).join("");
    box.scrollTop = 0;
    S.subCue = -1;
  }

  // 滚动歌词式高亮：取已经开始的最后一条
  function cueIndexAt(now) {
    var t = now - S.subOffset;
    var idx = -1;
    for (var i = 0; i < S.cues.length; i++) {
      if (S.cues[i].start <= t) idx = i;
      else break;
    }
    return idx;
  }

  function syncCueHighlight(now, force) {
    if (!S.subOpen) return;
    var idx = cueIndexAt(now);
    if (idx === S.subCue && !force) return;
    S.subCue = idx;
    var box = $("#mp-subm-list");
    if (!box) return;
    var rows = box.querySelectorAll(".mp-cue");
    for (var i = 0; i < rows.length; i++) rows[i].classList.toggle("on", i === idx);
    var row = rows[idx];
    // 用户手动滚动后 1.5 秒内不抢滚动条
    if (row && Date.now() - S.subUserScroll > 1500) {
      var top = row.offsetTop - box.clientHeight / 2 + row.offsetHeight / 2;
      box.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
    }
  }

  function paintOffset() {
    var el = $("#mp-subm-off");
    if (el) el.textContent = "偏移 " + (S.subOffset > 0 ? "+" : "") + S.subOffset.toFixed(1) + "s";
  }

  function shiftSubOffset(delta) {
    setSubOffset(S.subOffset + delta);
  }

  function setSubOffset(value) {
    S.subOffset = Math.round(value * 10) / 10;
    paintOffset();
    renderCueList();
    syncCueHighlight(audio.currentTime, true);
    paintCue(audio.currentTime);
  }

  /* ---------------- 播放列表 ---------------- */

  function showExtras(kind) {
    S.extras = S.extras === kind ? "" : kind;
    var box = $("#mp-extra");
    if (!S.extras) { box.innerHTML = ""; return; }

    if (S.extras === "playlist") {
      var p = S.play;
      var queue = p ? p.queue : [];
      var at = p ? p.index : -1;
      box.innerHTML = '<div class="mp-ex-title">播放列表 (' + queue.length + ")</div>" +
        queue.map(function (n, i) {
          return '<div class="mp-ex-item' + (i === at ? " on" : "") + '" data-pi="' + i + '">' +
                 '<span>' + (i === at ? ico("play") : "") + esc(n.name) + "</span>" +
                 '<span class="mp-meta">' + humanSize(n.size) + "</span></div>";
        }).join("");
      box.querySelectorAll("[data-pi]").forEach(function (el) {
        el.onclick = function () {
          if (!S.play) return;
          S.play.index = +el.dataset.pi;
          loadCurrent(true);
        };
      });
    }
  }

  /* ---------------- 图片 / 视频 ---------------- */

  function openPhoto(rel) {
    S.photos = nodesOf(S.dir).filter(function (n) { return n.type === "file" && IMAGE_EXT.test(n.name); });
    S.photoIndex = S.photos.findIndex(function (n) { return n.rel === rel; });
    if (S.photoIndex < 0) S.photoIndex = 0;
    paintPhoto();
    $("#mp-photo").classList.add("show");
  }

  function paintPhoto() {
    var n = S.photos[S.photoIndex];
    if (!n) return;
    $("#mp-photo-img").src = rawURL(n.rel);
    $("#mp-photo-name").textContent = (S.photoIndex + 1) + " / " + S.photos.length + "  " + n.name;
  }

  function showPhoto(delta) {
    if (!S.photos.length) return;
    S.photoIndex = (S.photoIndex + delta + S.photos.length) % S.photos.length;
    paintPhoto();
  }

  function openVideo(rel) {
    var v = $("#mp-video-el");
    v.innerHTML = "";
    v.src = rawURL(rel);
    $("#mp-video-name").textContent = rel.split("/").pop();

    // 同名字幕自动挂载（全作品范围匹配）
    var sub = findSubtitle(rel);
    if (sub && S.subOn) {
      fetch(rawURL(sub.rel)).then(function (r) { return r.text(); }).then(function (txt) {
        var cues = parseSubs(txt);
        var vtt = "WEBVTT\n\n" + cues.map(function (c) {
          return vttTime(c.start) + " --> " + vttTime(c.end) + "\n" + c.text;
        }).join("\n\n");
        var track = document.createElement("track");
        track.kind = "subtitles";
        track.label = "字幕";
        track.srclang = "zh";
        track.default = true;
        track.src = URL.createObjectURL(new Blob([vtt], { type: "text/vtt" }));
        v.appendChild(track);
      });
    }

    $("#mp-video").classList.add("show");
    v.play().catch(function () {});
  }

  function vttTime(t) {
    var h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
    return ("0" + h).slice(-2) + ":" + ("0" + m).slice(-2) + ":" +
           (s < 10 ? "0" : "") + s.toFixed(3);
  }

  function closeVideo() {
    var v = $("#mp-video-el");
    v.pause();
    v.removeAttribute("src");
    v.load();
    $("#mp-video").classList.remove("show");
  }

  // 暴露给其他脚本
  window.MP = { audio: audio, state: S };
})();
