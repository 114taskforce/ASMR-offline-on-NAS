/* GooeyNav — 顶部导航胶质药丸动画
   移植自 React Bits 的 <GooeyNav />（React 版 → 原生 JS）
   使用 WebGL2 片元着色器做 metaball 融合；不支持时退化为普通药丸 */
(function () {
  "use strict";

  var MAX_DROPS = 32;
  var MAX_PILLS = 4;
  var MARGIN = 72;

  var VERTEX = `#version 300 es
in vec2 aPosition;
void main() {
  gl_Position = vec4(aPosition, 0.0, 1.0);
}
`;

  var FRAGMENT = `#version 300 es
precision highp float;

uniform vec2 uSize;
uniform float uRatio;
uniform vec4 uPills[${MAX_PILLS}];
uniform vec4 uHover;
uniform vec4 uDrops[${MAX_DROPS}];
uniform vec2 uTails[${MAX_DROPS}];
uniform vec3 uTints[${MAX_DROPS}];
uniform int uCount;
uniform float uSmooth;
uniform float uRadius;
uniform vec3 uPill;
uniform vec4 uHoverColor;
uniform float uShadow;

out vec4 outColor;

float sdPill(vec2 p, vec4 box) {
  vec2 extent = max(box.zw, vec2(0.0));
  float r = min(min(extent.x, extent.y), max(uRadius, 0.0));
  vec2 q = abs(p - box.xy) - extent + r;
  return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

float sdCapsule(vec2 p, vec2 a, vec2 b, float r) {
  vec2 pa = p - a;
  vec2 ba = b - a;
  float h = clamp(dot(pa, ba) / max(dot(ba, ba), 0.0001), 0.0, 1.0);
  return length(pa - ba * h) - r;
}

vec4 blend(vec4 a, vec4 b, float k) {
  float h = clamp(0.5 + 0.5 * (b.w - a.w) / k, 0.0, 1.0);
  return vec4(mix(b.rgb, a.rgb, h), mix(b.w, a.w, h) - k * h * (1.0 - h));
}

vec4 field(vec2 p) {
  vec4 shape = vec4(uPill, 1e5);
  for (int i = 0; i < ${MAX_PILLS}; i++) {
    vec4 pill = uPills[i];
    if (pill.z <= 0.0) continue;
    shape = blend(shape, vec4(uPill, sdPill(p, pill)), uSmooth);
  }
  for (int i = 0; i < ${MAX_DROPS}; i++) {
    if (i >= uCount) break;
    vec4 drop = uDrops[i];
    if (drop.z <= 0.0) continue;
    float d = sdCapsule(p, drop.xy, drop.xy - uTails[i], drop.z);
    if (d > shape.w + uSmooth) continue;
    shape = blend(shape, vec4(uTints[i], d), uSmooth);
  }
  return shape;
}

void main() {
  vec2 p = vec2(gl_FragCoord.x, uSize.y * uRatio - gl_FragCoord.y) / uRatio;
  vec4 shape = field(p);
  float d = shape.w;
  float aa = max(fwidth(d), 0.35);
  float body = clamp(0.5 - d / aa, 0.0, 1.0);

  vec2 grad = vec2(dFdx(d), -dFdy(d));
  vec2 n = length(grad) > 0.0001 ? normalize(grad) : vec2(0.0, 1.0);
  float rim = (1.0 - smoothstep(0.0, 1.6, -d)) * max(0.0, -n.y) * 0.3;
  vec3 fill = mix(shape.rgb, vec3(1.0), rim) * (1.0 - 0.06 * max(0.0, n.y) * (1.0 - smoothstep(0.0, 6.0, -d)));

  float lifted = field(p - vec2(0.0, 3.0)).w;
  float shadow = (1.0 - smoothstep(-3.0, 9.0, lifted)) * uShadow * (1.0 - body);

  float hover = clamp(0.5 - sdPill(p, uHover) / aa, 0.0, 1.0) * uHoverColor.a;

  vec4 color = vec4(0.0, 0.0, 0.0, shadow);
  color = vec4(uHoverColor.rgb * hover, hover) + color * (1.0 - hover);
  color = vec4(fill * body, body) + color * (1.0 - body);
  outColor = color;
}
`;

  var UNIFORMS = [
    "uSize", "uRatio", "uPills", "uHover", "uDrops", "uTails", "uTints",
    "uCount", "uSmooth", "uRadius", "uPill", "uHoverColor", "uShadow"
  ];

  function parseColor(value) {
    var v = String(value || "").trim();
    var match = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
    if (match) {
      var hex = match[1].length === 3 ? match[1].replace(/./g, function (d) { return d + d; }) : match[1];
      return [0, 2, 4].map(function (i) { return parseInt(hex.slice(i, i + 2), 16) / 255; }).concat(1);
    }
    var rgba = /rgba?\(([^)]+)\)/i.exec(v);
    if (rgba) {
      var parts = rgba[1].split(",").map(function (p) { return parseFloat(p); });
      return [parts[0] / 255, parts[1] / 255, parts[2] / 255, parts[3] == null ? 1 : parts[3]];
    }
    return [1, 1, 1, 1];
  }

  function clamp01(v) { return Math.min(1, Math.max(0, v)); }

  function travelEase(t) {
    var x = clamp01(t);
    return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
  }

  function bezier(a, b, c, d, t) {
    var u = 1 - t;
    return u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * d;
  }

  function compile(gl, type, source) {
    var shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    return shader;
  }

  function build(gl) {
    var program = gl.createProgram();
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX));
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      gl.deleteProgram(program);
      return null;
    }
    var uniforms = {};
    UNIFORMS.forEach(function (name) { uniforms[name] = gl.getUniformLocation(program, name); });
    var buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    return { program: program, uniforms: uniforms, buffer: buffer, position: gl.getAttribLocation(program, "aPosition") };
  }

  function createGooeyNav(nav, options) {
    options = options || {};
    var stage = nav.querySelector(".gooey-nav__stage");
    var canvas = nav.querySelector(".gooey-nav__canvas");
    var list = nav.querySelector(".gooey-nav__list");
    var fallback = nav.querySelector(".gooey-nav__fallback");
    if (!stage || !canvas || !list || !fallback) return null;

    var cfg = {
      animationTime: options.animationTime || 600,
      particleCount: options.particleCount || 15,
      timeVariance: options.timeVariance == null ? 300 : options.timeVariance,
      spread: options.spread || 56,
      radius: options.radius == null ? 8 : options.radius,
      gooeyness: options.gooeyness == null ? 0.5 : options.gooeyness,
      wobble: options.wobble == null ? 0.5 : options.wobble,
      hoverEffect: options.hoverEffect !== false,
      pillRgb: parseColor(options.color || "#5e6ad2"),
      tints: [],
      hoverRgb: parseColor("rgba(255,255,255,0.08)"),
      shadow: 0.4
    };

    var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var gl = canvas.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false });
    var pipeline = gl ? build(gl) : null;
    fallback.style.display = pipeline ? "none" : "block";
    canvas.style.display = pipeline ? "block" : "none";

    var view = { width: 0, height: 0 };
    var rects = [];
    var target = -1;
    var pills = new Map();
    var drops = [];
    var hover = { x: 0, y: 0, w: 0, h: 0, alpha: 0, index: -1 };
    var pillData = new Float32Array(MAX_PILLS * 4);
    var dropData = new Float32Array(MAX_DROPS * 4);
    var tailData = new Float32Array(MAX_DROPS * 2);
    var tintData = new Float32Array(MAX_DROPS * 3);
    var raf = 0;
    var settleTimer = 0;
    var last = 0;
    var disposed = false;

    function measure() {
      var box = stage.getBoundingClientRect();
      var ratio = Math.min(window.devicePixelRatio || 1, 2);
      view.width = box.width;
      view.height = box.height;
      var outerWidth = box.width + MARGIN * 2;
      var outerHeight = box.height + MARGIN * 2;
      canvas.style.left = -MARGIN + "px";
      canvas.style.top = -MARGIN + "px";
      canvas.style.width = outerWidth + "px";
      canvas.style.height = outerHeight + "px";
      var width = Math.max(1, Math.round(outerWidth * ratio));
      var height = Math.max(1, Math.round(outerHeight * ratio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      rects = Array.prototype.map.call(list.children, function (item) {
        var rect = item.getBoundingClientRect();
        return { x: rect.left - box.left, y: rect.top - box.top, w: rect.width, h: rect.height };
      });
    }

    function pillState(index) {
      if (!pills.has(index)) pills.set(index, { mass: 0, scale: 0, velocity: 0, outgoing: 0 });
      return pills.get(index);
    }

    function shape(index, amount) {
      var rect = rects[index];
      if (!rect || amount <= 0.002) return null;
      var h = rect.h * Math.min(1.08, 0.42 + 0.58 * Math.sqrt(Math.max(0, amount)));
      var w = Math.max(h, rect.w * Math.pow(Math.max(0, amount), 0.75));
      return { x: rect.x + rect.w / 2 - w / 2, y: rect.y + rect.h / 2 - h / 2, w: w, h: h };
    }

    function plan(drop, index, now, fresh) {
      var rect = rects[index];
      if (!rect) return;
      var reach = Math.max(0, cfg.spread);
      var ex = rect.x + rect.w / 2 + (Math.random() - 0.5) * rect.w * 0.45;
      var ey = rect.y + rect.h / 2 + (Math.random() - 0.5) * rect.h * 0.2;
      var base = Math.max(0.15, cfg.animationTime / 1000);
      var variance = (Math.random() - 0.5) * (Math.max(0, cfg.timeVariance) / 1000);
      var duration = Math.max(0.15, base * (0.62 + Math.random() * 0.32) + variance * 0.6);
      var out = Math.random() * Math.PI * 2;
      var into = Math.random() * Math.PI * 2;
      var burst = reach * (0.7 + Math.random() * 0.6);
      var swing = reach * (0.55 + Math.random() * 0.55);
      var carry = Math.min(duration / 3, 0.12);
      drop.index = index;
      drop.start = now;
      drop.duration = duration;
      drop.path = [
        drop.x,
        drop.y,
        fresh ? drop.x + Math.cos(out) * burst * 1.6 : drop.x + drop.vx * carry,
        fresh ? drop.y + Math.sin(out) * burst * 1.6 : drop.y + drop.vy * carry,
        ex + Math.cos(into) * swing * 1.5,
        ey + Math.sin(into) * swing * 1.5,
        ex,
        ey
      ];
    }

    function emit(from, to, now) {
      var source = pills.get(from);
      var rect = rects[from];
      if (!source || !rect || !rects[to]) return;
      var available = source.mass - source.outgoing;
      if (available <= 0.002) return;
      var room = MAX_DROPS - drops.length;
      var count = Math.min(room, Math.max(1, Math.round(Math.max(1, cfg.particleCount) * available)));
      if (count <= 0) return;
      var windowMs = Math.max(0.15, cfg.animationTime / 1000) * 0.16;
      for (var i = 0; i < count; i++) {
        var mass = available / count;
        source.outgoing += mass;
        drops.push({
          x: rect.x + rect.w / 2 + (Math.random() - 0.5) * rect.w * 0.6,
          y: rect.y + rect.h / 2 + (Math.random() - 0.5) * rect.h * 0.3,
          vx: 0,
          vy: 0,
          mass: mass,
          size: 0.8 + Math.random() * 0.4,
          given: 0,
          source: from,
          index: to,
          leave: now + Math.random() * windowMs * 1000,
          emerged: 0,
          absorbed: 0,
          state: "waiting",
          tint: cfg.tints.length ? cfg.tints[Math.floor(Math.random() * cfg.tints.length)] : null
        });
      }
    }

    function render() {
      var height = (rects[target] && rects[target].h) || (rects[0] && rects[0].h) || 36;
      var smooth = height * (0.18 + 0.5 * clamp01(cfg.gooeyness));
      var boxes = [];
      var blobs = [];

      pills.forEach(function (state, index) {
        var box = shape(index, state.scale);
        if (box) boxes.push({ cx: box.x + box.w / 2, cy: box.y + box.h / 2, hw: box.w / 2, hh: box.h / 2 });
      });
      drops.forEach(function (drop) {
        if (drop.state === "waiting") return;
        var radius = 0.26 * height * drop.size * Math.min(drop.emerged, 1 - drop.absorbed);
        if (radius <= 0.05) return;
        var speed = Math.hypot(drop.vx, drop.vy);
        var length = Math.min(radius * 1.1, speed * 0.018);
        blobs.push({
          x: drop.x,
          y: drop.y,
          r: radius,
          tx: speed > 0.01 ? (drop.vx / speed) * length : 0,
          ty: speed > 0.01 ? (drop.vy / speed) * length : 0,
          tint: drop.tint
        });
      });

      if (!pipeline || !gl) {
        var box = shape(target, 1);
        if (box) {
          fallback.style.transform = "translate(" + box.x.toFixed(2) + "px," + box.y.toFixed(2) + "px)";
          fallback.style.width = box.w + "px";
          fallback.style.height = box.h + "px";
        }
        fallback.style.opacity = box ? "1" : "0";
        return;
      }

      var slot = 0;
      for (var bi = 0; bi < boxes.length; bi++) {
        if (slot >= MAX_PILLS) break;
        var b = boxes[bi];
        pillData[slot * 4] = b.cx + MARGIN;
        pillData[slot * 4 + 1] = b.cy + MARGIN;
        pillData[slot * 4 + 2] = b.hw;
        pillData[slot * 4 + 3] = b.hh;
        slot += 1;
      }
      pillData.fill(0, slot * 4);

      var count = 0;
      for (var di = 0; di < blobs.length; di++) {
        if (count >= MAX_DROPS) break;
        var blob = blobs[di];
        dropData[count * 4] = blob.x + MARGIN;
        dropData[count * 4 + 1] = blob.y + MARGIN;
        dropData[count * 4 + 2] = blob.r;
        dropData[count * 4 + 3] = 0;
        tailData[count * 2] = blob.tx;
        tailData[count * 2 + 1] = blob.ty;
        var tint = blob.tint || cfg.pillRgb;
        tintData[count * 3] = tint[0];
        tintData[count * 3 + 1] = tint[1];
        tintData[count * 3 + 2] = tint[2];
        count += 1;
      }

      var program = pipeline.program;
      var uniforms = pipeline.uniforms;
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(program);
      gl.bindBuffer(gl.ARRAY_BUFFER, pipeline.buffer);
      gl.enableVertexAttribArray(pipeline.position);
      gl.vertexAttribPointer(pipeline.position, 2, gl.FLOAT, false, 0, 0);
      gl.uniform2f(uniforms.uSize, view.width + MARGIN * 2, view.height + MARGIN * 2);
      gl.uniform1f(uniforms.uRatio, canvas.width / Math.max(1, view.width + MARGIN * 2));
      gl.uniform4fv(uniforms.uPills, pillData);
      gl.uniform4f(uniforms.uHover, hover.x + MARGIN, hover.y + MARGIN, hover.w / 2, hover.h / 2);
      gl.uniform4fv(uniforms.uDrops, dropData);
      gl.uniform2fv(uniforms.uTails, tailData);
      gl.uniform3fv(uniforms.uTints, tintData);
      gl.uniform1i(uniforms.uCount, count);
      gl.uniform1f(uniforms.uSmooth, smooth);
      gl.uniform1f(uniforms.uRadius, cfg.radius);
      gl.uniform3f(uniforms.uPill, cfg.pillRgb[0], cfg.pillRgb[1], cfg.pillRgb[2]);
      gl.uniform4f(uniforms.uHoverColor, cfg.hoverRgb[0], cfg.hoverRgb[1], cfg.hoverRgb[2], cfg.hoverRgb[3] * hover.alpha);
      gl.uniform1f(uniforms.uShadow, cfg.shadow);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    function frame(now) {
      raf = 0;
      var dt = Math.min(0.05, (now - last) / 1000 || 0);
      last = now;
      var emergeTime = 0.09;
      var absorbTime = 0.12;

      for (var i = drops.length - 1; i >= 0; i--) {
        var drop = drops[i];
        if (drop.state === "waiting") {
          if (now < drop.leave) continue;
          drop.state = "flying";
          plan(drop, drop.index, now, true);
        }
        if (drop.state === "flying") {
          drop.emerged = Math.min(1, drop.emerged + dt / emergeTime);
          var source = pills.get(drop.source);
          if (source && drop.given < drop.mass) {
            var step = Math.min(drop.mass * (dt / emergeTime), drop.mass - drop.given);
            source.mass = Math.max(0, source.mass - step);
            source.outgoing = Math.max(0, source.outgoing - step);
            drop.given += step;
          }
          var t = clamp01((now - drop.start) / 1000 / drop.duration);
          var eased = travelEase(t);
          var path = drop.path;
          var x = bezier(path[0], path[2], path[4], path[6], eased);
          var y = bezier(path[1], path[3], path[5], path[7], eased);
          if (dt > 0) {
            drop.vx = (x - drop.x) / dt;
            drop.vy = (y - drop.y) / dt;
          }
          drop.x = x;
          drop.y = y;
          if (t >= 1) drop.state = "absorbing";
        }
        if (drop.state === "absorbing") {
          var absorbStep = Math.min(1 - drop.absorbed, dt / absorbTime);
          drop.absorbed += absorbStep;
          drop.vx *= 0.8;
          drop.vy *= 0.8;
          var host = pillState(drop.index);
          host.mass = Math.min(1, host.mass + drop.mass * absorbStep);
          if (drop.absorbed >= 1) drops.splice(i, 1);
        }
      }

      var zeta = reduced ? 1 : 1 - 0.72 * clamp01(cfg.wobble);
      var omega = 26;
      var steps = Math.max(1, Math.ceil(dt / (1 / 240)));
      var h = dt / steps;
      pills.forEach(function (state, index) {
        for (var n = 0; n < steps; n++) {
          state.velocity += (omega * omega * (state.mass - state.scale) - 2 * zeta * omega * state.velocity) * h;
          state.scale += state.velocity * h;
        }
        if (index !== target && state.mass <= 0.002 && state.outgoing <= 0.002 && Math.abs(state.scale) < 0.004) {
          pills.delete(index);
        }
      });

      if (hover.index >= 0 && rects[hover.index]) {
        var rect = rects[hover.index];
        var k = 1 - Math.exp(-dt / 0.06);
        hover.x += (rect.x + rect.w / 2 - hover.x) * k;
        hover.y += (rect.y + rect.h / 2 - hover.y) * k;
        hover.w += (rect.w - hover.w) * k;
        hover.h += (rect.h - hover.h) * k;
      }
      var hoverGoal = cfg.hoverEffect && hover.index >= 0 && hover.index !== target ? 1 : 0;
      hover.alpha += (hoverGoal - hover.alpha) * (1 - Math.exp(-dt / 0.08));

      render();

      var host = pills.get(target);
      var resting = drops.length === 0 && pills.size === 1 && host &&
        Math.abs(host.mass - host.scale) < 0.0008 && Math.abs(host.velocity) < 0.004;
      var hoverRect = rects[hover.index];
      var hovering = Math.abs(hoverGoal - hover.alpha) > 0.003 ||
        (hoverRect && Math.abs(hoverRect.x + hoverRect.w / 2 - hover.x) + Math.abs(hoverRect.w - hover.w) > 0.1);
      if (!resting || hovering) {
        raf = requestAnimationFrame(frame);
      } else {
        host.mass = 1;
        host.scale = 1;
        host.velocity = 0;
        hover.alpha = hoverGoal;
        render();
      }
    }

    function wake() {
      if (raf || disposed) return;
      last = performance.now();
      raf = requestAnimationFrame(frame);
    }

    function settle(index) {
      clearTimeout(settleTimer);
      settleTimer = 0;
      pills.clear();
      drops.length = 0;
      target = index;
      var state = pillState(index);
      state.mass = 1;
      state.scale = 1;
      state.velocity = 0;
      render();
    }

    function go(index, animate) {
      if (index === target) return;
      if (!rects[index] || !animate || reduced || target < 0) {
        settle(index);
        return;
      }
      var now = performance.now();
      target = index;
      pillState(index);
      drops.forEach(function (drop) {
        drop.index = index;
        if (drop.state === "flying") plan(drop, index, now, false);
        if (drop.state === "absorbing") {
          drop.state = "flying";
          drop.mass *= 1 - drop.absorbed;
          drop.absorbed = 0;
          plan(drop, index, now, false);
        }
      });
      Array.from(pills.keys()).forEach(function (from) {
        if (from !== index) emit(from, index, now);
      });
      wake();
      // 页面休眠或 GPU 跳帧后也强制收束，避免旧位置留下蓝色粒子。
      clearTimeout(settleTimer);
      settleTimer = setTimeout(function () {
        if (target >= 0) settle(target);
      }, Math.max(1400, cfg.animationTime + cfg.timeVariance + 700));
    }

    function onOver(event) {
      var item = event.target.closest("[data-gooey-index]");
      if (!item || !list.contains(item)) return;
      var index = Number(item.dataset.gooeyIndex);
      var rect = rects[index];
      if (!rect) return;
      if (hover.alpha < 0.01) {
        hover.x = rect.x + rect.w / 2;
        hover.y = rect.y + rect.h / 2;
        hover.w = rect.w;
        hover.h = rect.h;
      }
      hover.index = index;
      wake();
    }

    function onLeave() {
      hover.index = -1;
      wake();
    }

    var resizeObserver = new ResizeObserver(function () {
      measure();
      if (!drops.length) settle(target < 0 ? initial : target);
      else render();
    });
    resizeObserver.observe(stage);
    resizeObserver.observe(list);

    list.addEventListener("pointerover", onOver);
    list.addEventListener("pointerleave", onLeave);
    canvas.addEventListener("webglcontextlost", function (event) {
      event.preventDefault();
      pipeline = null;
      canvas.style.display = "none";
      fallback.style.display = "block";
      settle(Math.max(0, target));
    });

    Array.prototype.forEach.call(list.children, function (item, index) {
      var button = item.querySelector("button, a");
      if (!button) return;
      button.addEventListener("click", function () { go(index, true); });
    });

    var initial = Math.max(0, Math.min(options.activeIndex || 0, list.children.length - 1));
    measure();
    settle(initial);
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(function () {
        if (disposed) return;
        measure();
        if (!drops.length) settle(target);
      });
    }

    return {
      setActive: function (index) { go(index, true); },
      refresh: function () { render(); wake(); }
    };
  }

  window.createGooeyNav = createGooeyNav;

  var nav = document.getElementById("main-tabs");
  if (nav) {
    window.__gooeyNav = createGooeyNav(nav, {
      activeIndex: 0,
      color: "#5e6ad2",
      animationTime: 600,
      particleCount: 15,
      spread: 56,
      timeVariance: 300,
      gooeyness: 0.5,
      wobble: 0.5,
      hoverEffect: true
    });
  }
})();
