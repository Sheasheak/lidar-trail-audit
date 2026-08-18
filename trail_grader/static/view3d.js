/* LiDAR Trail Audit — results page.
   Draws the 3D terrain + draped trail ribbon, and the 2D long section,
   plan view and grade breakdown that sit under it. */
(function () {
  "use strict";

  const DATA = JSON.parse(document.getElementById("audit-data").textContent);
  const ST = DATA.stations;
  const S = DATA.summary;
  const GRADES = DATA.grades;
  const OPTS = DATA.options;
  const SPACING = OPTS.spacing;

  const CORE = ST.filter(s => !s.edge && s.g !== null);
  const el = id => document.getElementById(id);
  const fmt = (v, d) => (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d === undefined ? 1 : d);
  const gradeName = n => (GRADES.find(g => g.n === n) || {}).name || "—";
  const deg = pct => Math.atan(Math.abs(pct) / 100) * 180 / Math.PI;

  /* ---------- palette ---------- */
  // Read the ramp out of CSS so the charts follow the page's light/dark tokens.
  function ramp() {
    const cs = getComputedStyle(document.documentElement);
    return [1, 2, 3, 4, 5, 6].map(n => cs.getPropertyValue("--g" + n).trim());
  }
  function ink(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  let RAMP = ramp();
  const gradeVar = n => "var(--g" + n + ")";

  /* ---------- metric definitions ----------
     Every metric bins onto the same six-step ramp, so one legend reads for all
     of them and nothing needs a rainbow scale. */
  const METRICS = {
    grade: {
      label: "Assessed grade",
      caption: "worst of gradient and turn radius at each station",
      value: s => s.grade,
      bin: s => s.grade - 1,
      bins: GRADES.map(g => g.n + " · " + g.name),
      lo: "Easiest", hi: "Extreme",
      fmt: s => "Grade " + s.grade + " · " + gradeName(s.grade)
    },
    gradient: {
      label: "Gradient",
      caption: "per cent, smoothed over " + OPTS.window + " m",
      value: s => s.g === null ? null : Math.abs(s.g),
      edges: [5, 10, 15, 20, 30],
      bins: ["0–5%", "5–10%", "10–15%", "15–20%", "20–30%", "30%+"],
      lo: "Flat", hi: "Steep",
      fmt: s => s.g === null ? "no reading" : fmt(s.g) + "%"
    },
    radius: {
      label: "Turn radius",
      caption: "metres — tighter turns read darker",
      value: s => s.r,
      // reversed: a small radius is the hard end
      edges: [2.5, 4, 6, 10, 20], reverse: true,
      bins: ["20 m +", "10–20 m", "6–10 m", "4–6 m", "2.5–4 m", "under 2.5 m"],
      lo: "Open", hi: "Tight",
      fmt: s => s.r === null ? "straight" : fmt(s.r) + " m"
    },
    sideslope: {
      label: "Cross-slope",
      caption: "degrees across the tread, from the DEM",
      value: s => s.ss,
      edges: [10, 20, 30, 40, 50],
      bins: ["0–10°", "10–20°", "20–30°", "30–40°", "40–50°", "50°+"],
      lo: "Flat", hi: "Steep",
      fmt: s => s.ss === null ? "no reading" : fmt(s.ss) + "°"
    }
  };

  function binOf(metric, s) {
    const m = METRICS[metric];
    if (m.bin) return m.bin(s);
    const v = m.value(s);
    if (v === null || v === undefined) return null;
    if (m.reverse) {
      // edges ascending, but low values are the hard (last) bin
      for (let i = 0; i < m.edges.length; i++) if (v < m.edges[i]) return 5 - i;
      return 0;
    }
    for (let i = 0; i < m.edges.length; i++) if (v < m.edges[i]) return i;
    return 5;
  }

  function colourOf(metric, s) {
    const b = binOf(metric, s);
    return b === null ? ink("--ink-3") : RAMP[b];
  }

  /* =======================================================================
     Header, verdict, tiles
     ======================================================================= */

  const assessed = GRADES.find(g => g.n === S.assessed_grade);

  el("f-len").textContent = S.length_m.toLocaleString() + " m";
  el("f-desc").textContent = fmt(S.descent_m) + " m";
  el("f-stations").textContent = S.core_stations + " assessed";
  el("lede").textContent =
    "Sampled every " + SPACING + " m off the LINZ 1 m bare-earth DEM, gradient smoothed over " +
    OPTS.window + " m, and graded against the NZ MTB Trail Design & Construction Guidelines.";

  el("verdict-badge").style.setProperty("--vg", gradeVar(S.assessed_grade));
  el("v-num").textContent = S.assessed_grade;
  el("v-name").textContent = S.assessed_name;

  el("v-lede").innerHTML =
    "This trail audits as <strong>Grade " + S.assessed_grade + " — " + S.assessed_name +
    "</strong>. Its average gradient of " + fmt(Math.abs(S.avg_gradient_pct)) + "% (" +
    fmt(S.avg_gradient_deg) + "°) sits " +
    (S.assessed_avg_max === null ? "beyond every published ceiling" :
      "inside the " + fmt(S.assessed_avg_max) + "° ceiling for that grade") +
    (S.assessed_peak_max === null ? "" :
      ", and " + fmt(S.over_peak_pct) + "% of its length exceeds the " + fmt(S.assessed_peak_max) +
      "° peak — against the " + S.assessed_exc + "% the guidelines allow") + ".";

  el("v-detail").innerHTML =
    "Judged station by station with no allowance at all, the worst stretch reaches <strong>Grade " +
    S.strict_grade + " — " + gradeName(S.strict_grade) + "</strong>. That is the ceiling, not the grade: " +
    "the exception allowances exist because a short pitch over the line does not re-grade a trail. " +
    "Both are reported so a builder can go and look at the sections listed below.";

  const checks = [];
  if (S.assessed_avg_max !== null) {
    checks.push([fmt(S.avg_gradient_deg) + "° average ≤ " + fmt(S.assessed_avg_max) + "°",
                 S.avg_gradient_deg <= S.assessed_avg_max]);
  }
  if (S.assessed_peak_max !== null) {
    checks.push(["over-peak length " + fmt(S.over_peak_pct) + "% ≤ " + S.assessed_exc + "%",
                 S.over_peak_pct <= S.assessed_exc]);
    checks.push(["under-radius length " + fmt(S.under_radius_pct) + "% ≤ " + S.assessed_exc + "%",
                 S.under_radius_pct <= S.assessed_exc]);
  }
  if (S.min_radius_m !== null) {
    checks.push(["tightest turn " + fmt(S.min_radius_m) + " m ≥ " + fmt(S.assessed_radius) + " m",
                 S.min_radius_m >= S.assessed_radius]);
  }
  el("v-checks").innerHTML = checks
    .map(c => '<span class="' + (c[1] ? "" : "fail") + '">' + c[0] + "</span>").join("");

  const tiles = [
    ["Length", S.length_m.toLocaleString(), "m", S.core_stations + " stations assessed"],
    ["Descent", fmt(S.descent_m), "m", fmt(S.start_z) + " → " + fmt(S.end_z) + " m"],
    ["Average gradient", fmt(Math.abs(S.avg_gradient_pct)), "%", fmt(S.avg_gradient_deg) + "° overall"],
    ["Steepest " + OPTS.window + " m", fmt(Math.abs(S.max_descent_pct)), "%", "at " + S.max_descent_at_m + " m"],
    ["Tightest turn", fmt(S.min_radius_m), "m", S.min_radius_at_m === null ? "no turns fitted" : "at " + S.min_radius_at_m + " m"],
    ["Mean cross-slope", fmt(S.mean_sideslope_deg), "°", "peaks at " + fmt(S.max_sideslope_deg) + "°"],
    ["Climbing", fmt(S.uphill_pct, 0), "%", "of length gains height"]
  ];
  el("stats").innerHTML = tiles.map(t =>
    '<div class="stat"><dt>' + t[0] + '</dt><dd>' + t[1] +
    '<span class="u">' + t[2] + '</span></dd><div class="sub">' + t[3] + "</div></div>").join("");

  /* =======================================================================
     3D scene
     ======================================================================= */

  const plotDiv = el("plot3d");
  const T = DATA.terrain;
  // Shift to a local origin — NZTM easting/northing are ~1e6/5e6 and WebGL
  // runs out of float precision if you hand those straight to the GPU.
  const ox = ST[0].x, oy = ST[0].y;

  const zVals = ST.map(s => s.z).filter(z => z !== null);
  let zMin = Math.min.apply(null, zVals), zMax = Math.max.apply(null, zVals);
  let xLo = Infinity, xHi = -Infinity, yLo = Infinity, yHi = -Infinity;

  if (T) {
    T.x.forEach(v => { xLo = Math.min(xLo, v - ox); xHi = Math.max(xHi, v - ox); });
    T.y.forEach(v => { yLo = Math.min(yLo, v - oy); yHi = Math.max(yHi, v - oy); });
    T.z.forEach(row => row.forEach(v => {
      if (v !== null) { zMin = Math.min(zMin, v); zMax = Math.max(zMax, v); }
    }));
  } else {
    ST.forEach(s => {
      xLo = Math.min(xLo, s.x - ox); xHi = Math.max(xHi, s.x - ox);
      yLo = Math.min(yLo, s.y - oy); yHi = Math.max(yHi, s.y - oy);
    });
  }

  const HALF_W = 2.2;   // ribbon half-width, metres
  const LIFT = 1.2;     // hold the ribbon just clear of the surface

  function ribbonGeometry() {
    const vx = [], vy = [], vz = [], i0 = [], i1 = [], i2 = [], owner = [];
    const usable = [];
    for (let i = 0; i < ST.length; i++) if (ST[i].z !== null) usable.push(ST[i]);

    for (let k = 0; k < usable.length; k++) {
      const s = usable[k];
      const p = usable[Math.max(0, k - 1)], n = usable[Math.min(usable.length - 1, k + 1)];
      let dx = n.x - p.x, dy = n.y - p.y;
      const len = Math.hypot(dx, dy) || 1;
      const px = -dy / len * HALF_W, py = dx / len * HALF_W;
      vx.push(s.x - ox + px, s.x - ox - px);
      vy.push(s.y - oy + py, s.y - oy - py);
      vz.push(s.z + LIFT, s.z + LIFT);
      owner.push(s, s);
    }
    for (let k = 0; k < usable.length - 1; k++) {
      const a = 2 * k, b = a + 1, c = a + 2, d = a + 3;
      i0.push(a, b); i1.push(b, d); i2.push(c, c);
    }
    return { vx, vy, vz, i0, i1, i2, owner, usable };
  }

  const RIB = ribbonGeometry();

  function vertexColours(metric) {
    return RIB.owner.map(s => colourOf(metric, s));
  }

  function aspect(exag) {
    const dx = Math.max(xHi - xLo, 1), dy = Math.max(yHi - yLo, 1);
    const dz = Math.max(zMax - zMin, 1);
    const m = Math.max(dx, dy);
    return { x: dx / m, y: dy / m, z: (dz * exag) / m };
  }

  let metric = "grade";
  let showTerrain = true;
  let exag = 2;

  function surfaceTrace() {
    const greyLo = ink("--surface-2") || "#eee";
    return {
      type: "surface",
      x: T.x.map(v => v - ox),
      y: T.y.map(v => v - oy),
      z: T.z,
      showscale: false,
      // one neutral ramp so the trail's own colours are the only hues on screen
      colorscale: [[0, ink("--grid")], [1, ink("--surface")]],
      opacity: 1,
      hoverinfo: "skip",
      contours: { x: { highlight: false }, y: { highlight: false }, z: { highlight: false } },
      lighting: { ambient: 0.62, diffuse: 0.82, specular: 0.06, roughness: 0.95, fresnel: 0.1 },
      lightposition: { x: -8000, y: 12000, z: 9000 },
      name: "LiDAR ground",
      visible: showTerrain
    };
  }

  function ribbonTrace() {
    return {
      type: "mesh3d",
      x: RIB.vx, y: RIB.vy, z: RIB.vz,
      i: RIB.i0, j: RIB.i1, k: RIB.i2,
      vertexcolor: vertexColours(metric),
      flatshading: true,
      hoverinfo: "skip",
      lighting: { ambient: 0.92, diffuse: 0.35, specular: 0.02, roughness: 1 },
      name: "Trail"
    };
  }

  function hoverTrace() {
    const pts = RIB.usable;
    return {
      type: "scatter3d",
      mode: "markers",
      x: pts.map(s => s.x - ox),
      y: pts.map(s => s.y - oy),
      z: pts.map(s => s.z + LIFT + 0.4),
      marker: { size: 3, opacity: 0.01, color: ink("--ink") },
      text: pts.map(s =>
        "Chainage " + Math.round(s.d) + " m<br>" +
        "Elevation " + fmt(s.z) + " m<br>" +
        "Gradient " + (s.g === null ? "—" : fmt(s.g) + "%") + "<br>" +
        "Turn radius " + (s.r === null ? "straight" : fmt(s.r) + " m") + "<br>" +
        "Cross-slope " + (s.ss === null ? "—" : fmt(s.ss) + "°") + "<br>" +
        "Grade " + s.grade + " · " + gradeName(s.grade)),
      hovertemplate: "%{text}<extra></extra>",
      name: "",
      showlegend: false
    };
  }

  function endpointTrace() {
    const a = RIB.usable[0], b = RIB.usable[RIB.usable.length - 1];
    return {
      type: "scatter3d", mode: "markers+text",
      x: [a.x - ox, b.x - ox], y: [a.y - oy, b.y - oy], z: [a.z + 4, b.z + 4],
      marker: { size: 5, color: [ink("--ink-3"), ink("--ink")] },
      text: ["Top", "Bottom"],
      textposition: "top center",
      textfont: { color: ink("--ink-2"), size: 11 },
      hoverinfo: "skip", showlegend: false
    };
  }

  const CAMERA_HOME = { eye: { x: 1.5, y: -1.5, z: 0.95 }, up: { x: 0, y: 0, z: 1 } };

  function layout3d() {
    return {
      margin: { l: 0, r: 0, t: 0, b: 0 },
      paper_bgcolor: "rgba(0,0,0,0)",
      showlegend: false,
      hoverlabel: {
        bgcolor: ink("--surface"), bordercolor: ink("--rule"),
        font: { color: ink("--ink"), size: 12,
                family: getComputedStyle(document.body).fontFamily }
      },
      scene: {
        aspectmode: "manual",
        aspectratio: aspect(exag),
        camera: CAMERA_HOME,
        xaxis: axis3d("Easting, m"),
        yaxis: axis3d("Northing, m"),
        zaxis: axis3d("Elevation, m")
      }
    };
  }

  function axis3d(title) {
    return {
      title: { text: title, font: { size: 10, color: ink("--ink-3") } },
      showgrid: true, gridcolor: ink("--grid"),
      zeroline: false,
      showbackground: true, backgroundcolor: "rgba(0,0,0,0)",
      color: ink("--ink-3"),
      tickfont: { size: 9, color: ink("--ink-3") }
    };
  }

  function traces() {
    const t = [];
    if (T) t.push(surfaceTrace());
    t.push(ribbonTrace(), hoverTrace(), endpointTrace());
    return t;
  }

  function drawScene() {
    Plotly.react(plotDiv, traces(), layout3d(),
      { responsive: true, displaylogo: false,
        modeBarButtonsToRemove: ["toImage", "resetCameraLastSave3d"] });
  }
  drawScene();

  el("terrain-note").textContent = T
    ? "Terrain surface: " + T.shape[1] + " × " + T.shape[0] + " cells at " +
      T.stride + " m, cropped " + OPTS.terrain_buffer + " m around the line, from " +
      (S.tiles_used || "?") + " LiDAR tiles."
    : "Terrain surface was switched off for this run — the trail ribbon is drawn on its own.";

  /* ---------- 3D controls ---------- */

  el("metric-set").addEventListener("click", ev => {
    const btn = ev.target.closest("button[data-metric]");
    if (!btn) return;
    metric = btn.dataset.metric;
    el("metric-set").querySelectorAll("button").forEach(b =>
      b.setAttribute("aria-pressed", String(b === btn)));
    Plotly.restyle(plotDiv, { vertexcolor: [vertexColours(metric)] }, [T ? 1 : 0]);
    drawScale();
  });

  const terrainBtn = el("terrain-toggle");
  terrainBtn.addEventListener("click", () => {
    if (!T) return;
    showTerrain = !showTerrain;
    terrainBtn.setAttribute("aria-pressed", String(showTerrain));
    terrainBtn.textContent = showTerrain ? "Show DEM" : "DEM hidden";
    Plotly.restyle(plotDiv, { visible: showTerrain }, [0]);
  });
  if (!T) { terrainBtn.disabled = true; terrainBtn.setAttribute("aria-pressed", "false"); }

  const exagInput = el("exag");
  exagInput.addEventListener("input", () => {
    exag = parseFloat(exagInput.value);
    el("exag-out").textContent = exag + "×";
    Plotly.relayout(plotDiv, { "scene.aspectratio": aspect(exag) });
  });

  el("reset-view").addEventListener("click", () => {
    Plotly.relayout(plotDiv, { "scene.camera": CAMERA_HOME });
  });

  function drawScale() {
    const m = METRICS[metric];
    el("scale-lo").textContent = m.lo;
    el("scale-hi").textContent = m.hi;
    el("scale-cap").textContent = m.label + " — " + m.caption;
    el("scale-ramp").innerHTML = m.bins
      .map((b, i) => '<i style="background:' + gradeVar(i + 1) + '" title="' + b + '"></i>').join("");
    el("scale-ramp").setAttribute("aria-label", m.label + ": " + m.bins.join(", "));
  }
  drawScale();

  // Re-theme everything if the OS flips light/dark under us.
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onTheme = () => { RAMP = ramp(); drawScene(); drawScale(); paint2d(); };
  if (mq.addEventListener) mq.addEventListener("change", onTheme);

  /* =======================================================================
     2D charts
     ======================================================================= */

  const SVGNS = "http://www.w3.org/2000/svg";
  function mk(tag, attrs, parent) {
    const n = document.createElementNS(SVGNS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function txt(x, y, s, cls, parent, extra) {
    const t = mk("text", Object.assign({ x: x, y: y, class: cls }, extra || {}), parent);
    t.textContent = s;
    return t;
  }

  const listeners = [];
  let active = -1;
  function setActive(i) {
    if (i === active) return;
    active = i;
    listeners.forEach(fn => fn(i));
  }

  function tipHtml(s) {
    return '<div class="th">Chainage ' + Math.round(s.d) + " m</div>" +
      '<div class="row"><span>Elevation</span><b>' + fmt(s.z) + " m</b></div>" +
      '<div class="row"><span>Gradient</span><b>' + (s.g === null ? "—" : fmt(s.g) + " %") + "</b></div>" +
      '<div class="row"><span>Turn radius</span><b>' + (s.r === null ? "straight" : fmt(s.r) + " m") + "</b></div>" +
      '<div class="row"><span>Cross-slope</span><b>' + (s.ss === null ? "—" : fmt(s.ss) + "°") + "</b></div>" +
      '<div class="gpill" style="background:' + gradeVar(s.grade) + '">Grade ' + s.grade +
      " · " + gradeName(s.grade) + "</div>" +
      (s.edge ? '<div class="th" style="margin:6px 0 0">Part-filled window — excluded</div>' : "");
  }

  const profSvg = el("profile");

  function paint2d() {
    listeners.length = 0;
    active = -1;
    profSvg.querySelectorAll(":not(title)").forEach(n => n.remove());
    drawProfile();
    drawLegendAndDist();
  }

  /* ---------- long section ---------- */

  function drawProfile() {
    const W = 1000, H = 420, L = 58, R = 22;
    const elevTop = 20, elevH = 168;
    const gradTop = 236, gradH = 132;
    const xMax = ST[ST.length - 1].d;
    const X = d => L + (d / xMax) * (W - L - R);

    const zs = ST.map(s => s.z).filter(z => z !== null);
    const zLo = Math.floor(Math.min.apply(null, zs) / 20) * 20;
    const zHi = Math.ceil(Math.max.apply(null, zs) / 20) * 20;
    const Z = z => elevTop + elevH - ((z - zLo) / Math.max(zHi - zLo, 1)) * elevH;

    const gAbs = Math.max(30, Math.ceil(Math.max.apply(null,
      CORE.map(s => Math.abs(s.g))) / 5) * 5);
    const G = g => gradTop + gradH / 2 - (g / gAbs) * (gradH / 2);

    const stepX = xMax > 3000 ? 500 : 250;
    for (let d = 0; d <= xMax; d += stepX) {
      mk("line", { x1: X(d), y1: elevTop, x2: X(d), y2: elevTop + elevH, class: "gridline" }, profSvg);
      mk("line", { x1: X(d), y1: gradTop, x2: X(d), y2: gradTop + gradH, class: "gridline" }, profSvg);
      txt(X(d), gradTop + gradH + 20, d.toLocaleString(), "tick", profSvg, { "text-anchor": "middle" });
    }
    txt(W - R, gradTop + gradH + 38, "Chainage, metres from top", "axlabel", profSvg, { "text-anchor": "end" });

    const zStep = (zHi - zLo) > 200 ? 50 : 40;
    for (let z = zLo; z <= zHi; z += zStep) {
      mk("line", { x1: L, y1: Z(z), x2: W - R, y2: Z(z), class: "gridline" }, profSvg);
      txt(L - 10, Z(z) + 3.5, z, "tick", profSvg, { "text-anchor": "end" });
    }
    txt(L, elevTop - 8, "Ground elevation, metres", "axlabel", profSvg);

    const pts = ST.filter(s => s.z !== null);
    const area = pts.map(s => X(s.d).toFixed(1) + " " + Z(s.z).toFixed(1)).join(" L ");
    mk("path", {
      d: "M " + X(pts[0].d).toFixed(1) + " " + (elevTop + elevH) + " L " + area +
         " L " + X(pts[pts.length - 1].d).toFixed(1) + " " + (elevTop + elevH) + " Z",
      fill: "var(--surface-2)"
    }, profSvg);
    mk("path", { d: "M " + area, fill: "none", stroke: "var(--ink-3)", "stroke-width": 1.5, "stroke-linejoin": "round" }, profSvg);

    mk("line", { x1: L, y1: G(0), x2: W - R, y2: G(0), class: "baseline" }, profSvg);
    [-20, -10, 10].forEach(v => {
      if (Math.abs(v) > gAbs) return;
      mk("line", { x1: L, y1: G(v), x2: W - R, y2: G(v), class: "gridline" }, profSvg);
      txt(L - 10, G(v) + 3.5, v > 0 ? "+" + v : v, "tick", profSvg, { "text-anchor": "end" });
    });
    txt(L - 10, G(0) + 3.5, "0", "tick", profSvg, { "text-anchor": "end" });
    txt(L, gradTop - 8, "Gradient, per cent — " + OPTS.window + " m smoothed", "axlabel", profSvg);

    const bw = Math.max(1.1, (W - L - R) / ST.length - 0.6);
    ST.forEach(s => {
      if (s.g === null) return;
      const y = G(s.g), y0 = G(0);
      mk("rect", {
        x: (X(s.d) - bw / 2).toFixed(2), y: Math.min(y, y0).toFixed(2),
        width: bw.toFixed(2), height: Math.max(Math.abs(y - y0), 0.6).toFixed(2),
        fill: gradeVar(s.grade), opacity: s.edge ? 0.3 : 1
      }, profSvg);
    });

    const steep = CORE.reduce((a, s) => s.g < a.g ? s : a, CORE[0]);
    mk("circle", { cx: X(steep.d), cy: G(steep.g), r: 3.5, fill: "none", stroke: "var(--ink)", "stroke-width": 1.5 }, profSvg);
    txt(X(steep.d) + 9, G(steep.g) + 4, "steepest " + fmt(steep.g) + "%", "marklabel", profSvg);

    const withR = CORE.filter(s => s.r !== null);
    if (withR.length) {
      const tight = withR.reduce((a, s) => s.r < a.r ? s : a, withR[0]);
      mk("line", { x1: X(tight.d), y1: elevTop, x2: X(tight.d), y2: elevTop + elevH, stroke: "var(--ink-3)", "stroke-width": 1 }, profSvg);
      txt(X(tight.d) + 7, elevTop + 14, "tightest turn " + fmt(tight.r) + " m", "marklabel", profSvg);
    }

    const cross = mk("g", { "pointer-events": "none" }, profSvg);
    const vline = mk("line", { x1: -9, y1: elevTop, x2: -9, y2: gradTop + gradH, stroke: "var(--ink)", "stroke-width": 1 }, cross);
    const dotZ = mk("circle", { cx: -9, cy: -9, r: 4, fill: "var(--surface)", stroke: "var(--ink)", "stroke-width": 2 }, cross);
    const dotG = mk("circle", { cx: -9, cy: -9, r: 4, fill: "var(--surface)", stroke: "var(--ink)", "stroke-width": 2 }, cross);
    cross.style.opacity = 0;

    const hit = mk("rect", {
      x: L, y: elevTop, width: W - L - R, height: gradTop + gradH - elevTop, fill: "transparent"
    }, profSvg);
    hit.addEventListener("pointermove", ev => {
      const pt = profSvg.createSVGPoint();
      pt.x = ev.clientX; pt.y = 0;
      const loc = pt.matrixTransform(profSvg.getScreenCTM().inverse());
      const d = (loc.x - L) / (W - L - R) * xMax;
      setActive(nearestByDistance(d));
    });
    hit.addEventListener("pointerleave", () => setActive(-1));

    profSvg.addEventListener("keydown", ev => {
      const k = ev.key;
      if (k === "ArrowRight" || k === "ArrowLeft") {
        setActive(Math.max(0, Math.min(ST.length - 1, (active < 0 ? 0 : active) + (k === "ArrowRight" ? 1 : -1))));
        ev.preventDefault();
      } else if (k === "Home") { setActive(0); ev.preventDefault(); }
      else if (k === "End") { setActive(ST.length - 1); ev.preventDefault(); }
      else if (k === "Escape") { setActive(-1); }
    });

    const tip = el("profile-tip");
    listeners.push(i => {
      if (i < 0) { cross.style.opacity = 0; tip.classList.remove("on"); return; }
      const s = ST[i];
      cross.style.opacity = 1;
      vline.setAttribute("x1", X(s.d)); vline.setAttribute("x2", X(s.d));
      dotZ.setAttribute("cx", X(s.d)); dotZ.setAttribute("cy", s.z === null ? -9 : Z(s.z));
      dotG.setAttribute("cx", X(s.d)); dotG.setAttribute("cy", s.g === null ? -9 : G(s.g));
      tip.classList.add("on");
      tip.innerHTML = tipHtml(s);
      const box = profSvg.getBoundingClientRect();
      tip.style.left = Math.min(Math.max(X(s.d) * (box.width / W) + 14, 4), Math.max(box.width - 190, 4)) + "px";
      tip.style.top = "8px";
    });
  }

  // Stations are evenly spaced in theory, but densify() keeps the original KML
  // vertices too, so chainage is not exactly index × spacing. Binary search it.
  function nearestByDistance(d) {
    let lo = 0, hi = ST.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (ST[mid].d < d) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && Math.abs(ST[lo - 1].d - d) < Math.abs(ST[lo].d - d)) lo--;
    return lo;
  }

  /* ---------- legend, distribution, flags ---------- */

  function drawLegendAndDist() {
    const dist = S.grade_distribution;
    const maxLen = Math.max.apply(null, GRADES.map(g => dist[g.n] || 0));

    el("grade-legend").innerHTML =
      '<span class="key n" style="margin-right:2px">Assessed grade</span>' +
      GRADES.map(g =>
        '<span class="key"><i style="background:' + gradeVar(g.n) + '"></i>' + g.n + " · " + g.name +
        ' <span class="n">' + Math.round(dist[g.n] || 0).toLocaleString() + " m</span></span>").join("");

    el("dist").innerHTML = GRADES.map(g => {
      const len = dist[g.n] || 0;
      const pct = S.core_stations ? len / (S.core_stations * SPACING) * 100 : 0;
      const w = maxLen ? (len / maxLen) * 100 : 0;
      return '<div class="dist-row">' +
        '<div class="nm"><span class="swatch" style="background:' + gradeVar(g.n) + '"></span>' + g.n + " · " + g.name + "</div>" +
        '<div class="dist-track"><div class="dist-fill" style="width:' + w + "%;background:" + gradeVar(g.n) + '"></div></div>' +
        '<div class="val">' + Math.round(len).toLocaleString() + " m · " + fmt(pct) + "%</div></div>";
    }).join("");
  }

  (function drawFlags() {
    const runs = [];
    let cur = null;
    CORE.forEach(s => {
      if (s.grade > S.assessed_grade) {
        if (cur && s.d - cur.end <= SPACING * 1.5) {
          cur.end = s.d; cur.max = Math.max(cur.max, s.grade); cur.rows.push(s);
        } else {
          cur = { start: s.d, end: s.d, max: s.grade, rows: [s] };
          runs.push(cur);
        }
      } else { cur = null; }
    });
    runs.sort((a, b) => b.max - a.max || (b.end - b.start) - (a.end - a.start));

    const body = el("flags").querySelector("tbody");
    if (!runs.length) {
      body.innerHTML = '<tr><td colspan="4" class="cause">Nothing measures above Grade ' +
        S.assessed_grade + " — the whole line sits inside its assessed grade.</td></tr>";
    } else {
      body.innerHTML = runs.map(r => {
        const byGrad = r.rows.some(s => s.gg === r.max);
        const byRad = r.rows.some(s => s.rg === r.max);
        const worstG = r.rows.reduce((a, s) => Math.abs(s.g) > Math.abs(a.g) ? s : a, r.rows[0]);
        const withR = r.rows.filter(s => s.r !== null);
        const worstR = withR.length ? withR.reduce((a, s) => s.r < a.r ? s : a, withR[0]) : null;
        const cause = [
          byGrad ? "gradient to " + fmt(worstG.g) + "%" : null,
          byRad && worstR ? "radius to " + fmt(worstR.r) + " m" : null
        ].filter(Boolean).join(", ");
        return "<tr><td>" + Math.round(r.start) + "–" + Math.round(r.end + SPACING) + " m</td>" +
          "<td>" + Math.round(r.end - r.start + SPACING) + " m</td>" +
          '<td><span class="pill" style="background:' + gradeVar(r.max) + '">' + r.max + "</span></td>" +
          '<td class="cause">' + cause + "</td></tr>";
      }).join("");
    }

    const flagLen = runs.reduce((a, r) => a + (r.end - r.start + SPACING), 0);
    el("flags-note").textContent = runs.length
      ? runs.length + " runs, " + Math.round(flagLen) + " m total — " +
        fmt(flagLen / S.length_m * 100) + "% of the trail. The guidelines allow " +
        (S.assessed_exc === null ? "no stated" : S.assessed_exc + "%") +
        " at Grade " + S.assessed_grade + "."
      : "";
  })();

  /* ---------- station table ---------- */

  el("station-table").querySelector("tbody").innerHTML = ST.map(s =>
    "<tr><td>" + Math.round(s.d) + "</td><td>" + fmt(s.z) + "</td>" +
    "<td>" + (s.g === null ? "—" : fmt(s.g)) + "</td>" +
    "<td>" + (s.r === null ? "straight" : fmt(s.r)) + "</td>" +
    "<td>" + (s.ss === null ? "—" : fmt(s.ss)) + "</td>" +
    "<td>" + s.grade + "</td><td>" + (s.edge ? "part-filled" : "full") + "</td></tr>").join("");

  /* ---------- settings + thresholds ---------- */

  el("settings-table").innerHTML = [
    ["Trail line", DATA.meta.source_label],
    ["Elevation", "LINZ 1 m bare-earth DEM, " + (S.tiles_used || "?") + " local tiles"],
    ["Working CRS", "NZTM2000 · EPSG:2193"],
    ["Station spacing", OPTS.spacing + " m"],
    ["Gradient window", OPTS.window + " m moving average"],
    ["Turn-radius chord", OPTS.chord + " m each side"],
    ["Cross-slope offset", OPTS.sideslope_offset + " m each side"],
    ["Straight cutoff", OPTS.radius_cutoff + " m"],
    ["Terrain margin", OPTS.terrain_buffer + " m"],
    ["Excluded from stats", "first and last " + (OPTS.window / 2) + " m — part-filled window"]
  ].map(r => "<tr><th scope=\"row\">" + r[0] + "</th><td>" + r[1] + "</td></tr>").join("");

  el("thresh-table").querySelector("tbody").innerHTML = GRADES.map(g =>
    "<tr" + (g.n === S.assessed_grade ? ' class="hit"' : "") + ">" +
    '<td><span class="swatch" style="background:' + gradeVar(g.n) + '"></span>' + g.n + " · " + g.name + "</td>" +
    "<td>" + (g.avg_deg === null ? "—" : fmt(g.avg_deg) + "°") + "</td>" +
    "<td>" + (g.max_deg === null ? "—" : fmt(g.max_deg) + "°") + "</td>" +
    "<td>" + (g.exc === null ? "—" : g.exc + "%") + "</td>" +
    "<td>" + fmt(g.radius) + " m</td></tr>").join("");

  paint2d();
})();
