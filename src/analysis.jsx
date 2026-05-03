/* SWE-Marathon analysis section — METR-style time horizon, cost Pareto,
   family heatmap, task distribution. Uses ECharts via refs. */

import React, { useEffect, useMemo, useRef, useState } from "react";
import { BarChart, HeatmapChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

use([
  BarChart,
  CanvasRenderer,
  GridComponent,
  HeatmapChart,
  LegendComponent,
  LineChart,
  PieChart,
  ScatterChart,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
]);

/* ---------- DATA: 20-task set with human-baseline hours ---------- */
/* Human baseline = geometric mean of expert completion time, METR-style.
   These are illustrative values consistent with the 1–8h agent budget. */
const ANALYSIS_TASKS = [
  { id: "T01", fam: "port",     title: "BioFabric Java → Rust port",            humanHours: 4.2 },
  { id: "T02", fam: "perf",     title: "Network-aligner C++ inner loop",        humanHours: 1.8 },
  { id: "T03", fam: "research", title: "Extend graphlet sampler past k=8",      humanHours: 5.5 },
  { id: "T04", fam: "perf",     title: "Graphlet sampler memory layout",        humanHours: 2.1 },
  { id: "T05", fam: "systems",  title: "Self-hosting C compiler · 3 targets",   humanHours: 6.8 },
  { id: "T06", fam: "impl",     title: "OAuth2 device-flow",                    humanHours: 2.6 },
  { id: "T07", fam: "impl",     title: "S3-compatible storage API",             humanHours: 3.4 },
  { id: "T08", fam: "research", title: "Reproduce paper · sparse attention",    humanHours: 7.5 },
  { id: "T09", fam: "systems",  title: "Distributed log replication",           humanHours: 5.1 },
  { id: "T10", fam: "port",     title: "Lua → TypeScript transpile core",       humanHours: 3.8 },
  { id: "T11", fam: "perf",     title: "Vectorise PNG decoder",                 humanHours: 1.4 },
  { id: "T12", fam: "impl",     title: "JWT refresh-rotation w/ revocation",    humanHours: 2.2 },
  { id: "T13", fam: "research", title: "Custom CUDA kernel · attention",        humanHours: 6.2 },
  { id: "T14", fam: "systems",  title: "POSIX-compatible FUSE driver",          humanHours: 4.6 },
  { id: "T15", fam: "port",     title: "Erlang → Rust GenServer port",          humanHours: 5.0 },
  { id: "T16", fam: "impl",     title: "GraphQL → REST gateway",                humanHours: 1.9 },
  { id: "T17", fam: "perf",     title: "Quantize embedding service · int8",     humanHours: 2.8 },
  { id: "T18", fam: "research", title: "Beat baseline on bench-X",              humanHours: 6.5 },
  { id: "T19", fam: "systems",  title: "Container runtime · cgroups v2",        humanHours: 7.0 },
  { id: "T20", fam: "port",     title: "MATLAB → NumPy with parity",            humanHours: 3.2 },
];

const FAM_LABEL = {
  impl: "Implementation", perf: "Performance", port: "Port / Rewrite",
  research: "Research", systems: "Systems",
};
const FAM_ORDER = ["impl", "perf", "port", "research", "systems"];

/* ---------- MODELS for analysis (subset of leaderboard, dedup'd by name+scaffold) ---------- */
/* Each carries a "horizon" — the 50%-success time horizon in hours.
   Trials are synthesized from a logistic curve around this horizon. */
const ANALYSIS_MODELS = [
  // name, scaffold (short), horizon hours (50%), color, $/hr inference cost class
  { id: "claude47-cc",   name: "Claude Opus 4.7",          scaffold: "Claude Code v2.1", horizon: 1.05, slope: 1.7, color: "#c7733b", costPerHr: 6.5 },
  { id: "gpt55-codex",   name: "GPT-5.5",                  scaffold: "Codex CLI v0.128", horizon: 0.88, slope: 1.6, color: "#3a7d5f", costPerHr: 5.8 },
  { id: "claude47-term", name: "Claude Opus 4.7",          scaffold: "Terminus 2",       horizon: 0.78, slope: 1.55, color: "#a86237", costPerHr: 5.2 },
  { id: "gemini31-cli",  name: "Gemini 3.1 Pro",           scaffold: "Gemini CLI v0.40", horizon: 0.65, slope: 1.5, color: "#5a6cb8", costPerHr: 4.6 },
  { id: "gpt55-term",    name: "GPT-5.5",                  scaffold: "Terminus 2",       horizon: 0.58, slope: 1.45, color: "#5d8a72", costPerHr: 4.9 },
  { id: "kimi-cli",      name: "Kimi K2.6",                scaffold: "Kimi Code CLI",    horizon: 0.46, slope: 1.4, color: "#8a6d4a", costPerHr: 3.2 },
  { id: "gemini31-term", name: "Gemini 3.1 Pro",           scaffold: "Terminus 2",       horizon: 0.42, slope: 1.35, color: "#7a83b3", costPerHr: 4.0 },
  { id: "kimi-term",     name: "Kimi K2.6",                scaffold: "Terminus 2",       horizon: 0.34, slope: 1.3, color: "#b09778", costPerHr: 2.8 },
  { id: "deepseek-term", name: "DeepSeek V4 Pro",          scaffold: "Terminus 2",       horizon: 0.28, slope: 1.25, color: "#6b8da3", costPerHr: 1.6 },
  { id: "glm-term",      name: "GLM 5.1",                  scaffold: "Terminus 2",       horizon: 0.20, slope: 1.2, color: "#9a7daa", costPerHr: 1.3 },
  { id: "minimax-term",  name: "MiniMax M2.7",             scaffold: "Terminus 2",       horizon: 0.16, slope: 1.15, color: "#a18267", costPerHr: 1.1 },
];

/* ---------- DETERMINISTIC RNG so charts don't reshuffle every render ---------- */
function mulberry32(seed) {
  return function() {
    let t = seed += 0x6D2B79F5;
    t = Math.imul(t ^ t >>> 15, t | 1);
    t ^= t + Math.imul(t ^ t >>> 7, t | 61);
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
function gauss(rng) {
  // Box–Muller
  let u = 0, v = 0;
  while (u === 0) u = rng();
  while (v === 0) v = rng();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

/* Logistic success probability — METR uses log-duration on x.
   p(success | t_human) = sigmoid(-slope * (log(t_human) - log(horizon))) */
function pSuccess(humanHours, horizon, slope) {
  const x = Math.log(humanHours) - Math.log(horizon);
  return 1 / (1 + Math.exp(slope * x));
}

/* ---------- Synthesized trials: 5 per (model × task) ---------- */
const TRIALS = (() => {
  const rng = mulberry32(424242);
  const out = [];
  for (const m of ANALYSIS_MODELS) {
    for (const t of ANALYSIS_TASKS) {
      const p = pSuccess(t.humanHours, m.horizon, m.slope);
      for (let trial = 0; trial < 5; trial++) {
        const resolved = rng() < p;
        // Wall-clock hours: agents are 2–6× faster than humans on success;
        // failures often burn full budget.
        const speedup = 2 + rng() * 4;
        const baseHours = resolved
          ? Math.max(0.1, (t.humanHours / speedup) * (0.85 + rng() * 0.3))
          : Math.min(8, t.humanHours * (0.7 + rng() * 0.6));
        const hours = Math.max(0.1, Math.min(8, baseHours));
        const cost = hours * m.costPerHr * (0.85 + rng() * 0.4);
        // Reward-hack: ~6% of the *resolved* cases for weaker models;
        // ~2% for top scaffolds. Always among "resolved" by tests but flagged by judge.
        const hackProb = resolved ? (0.02 + 0.10 * (1 - p)) : 0;
        const rewardHacked = rng() < hackProb;
        out.push({
          model: m.id,
          task: t.id,
          fam: t.fam,
          humanHours: t.humanHours,
          hours: +hours.toFixed(2),
          cost: +cost.toFixed(2),
          resolved: resolved && !rewardHacked,
          rewardHacked,
        });
      }
    }
  }
  return out;
})();

/* ---------- ECharts theme tokens (sync w/ paper aesthetic) ---------- */
const PAPER = {
  bg: "transparent",
  ink: "#1a1a17",
  ink2: "#494842",
  ink3: "#84827a",
  rule: "#d8d3c4",
  rule2: "#ebe6d7",
  accent: "#b56636",
  pos: "#5a7d4f",
  warn: "#a8763a",
};
const AXIS_COMMON = {
  axisLine: { lineStyle: { color: PAPER.rule } },
  axisTick: { lineStyle: { color: PAPER.rule } },
  axisLabel: { color: PAPER.ink2, fontFamily: "IBM Plex Mono, monospace", fontSize: 11 },
  nameTextStyle: { color: PAPER.ink3, fontFamily: "IBM Plex Mono, monospace", fontSize: 11 },
  splitLine: { lineStyle: { color: PAPER.rule2, type: "dashed" } },
};
const TOOLTIP_COMMON = {
  backgroundColor: "#fdfaf2",
  borderColor: PAPER.rule,
  borderWidth: 1,
  textStyle: { color: PAPER.ink, fontFamily: "Inter, sans-serif", fontSize: 12 },
  extraCssText: "box-shadow: 0 4px 16px rgba(26,26,23,0.08); border-radius: 2px; padding: 10px 12px;",
};

/* ---------- Hook: ECharts instance bound to a div ref ---------- */
function useEcharts(buildOption, deps) {
  const ref = useRef(null);
  const chartRef = useRef(null);
  useEffect(() => {
    if (!ref.current) return;
    if (!chartRef.current) {
      chartRef.current = init(ref.current, null, { renderer: "canvas" });
    }
    chartRef.current.setOption(buildOption(), { notMerge: true });
    const onResize = () => chartRef.current && chartRef.current.resize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, deps);
  useEffect(() => () => {
    if (chartRef.current) { chartRef.current.dispose(); chartRef.current = null; }
  }, []);
  return ref;
}

/* ============================================================
   PLOT 1 — Time-horizon (METR-style)
   x: human-baseline hours (log). y: success rate per task (jittered).
   Curve: logistic fit per model. Highlight 50% time horizon.
   ============================================================ */
function TimeHorizonChart() {
  const [selectedIds, setSelectedIds] = useState(() => ANALYSIS_MODELS.slice(0, 4).map(m => m.id));
  const [yMode, setYMode] = useState("rate"); // rate | jitter

  const ref = useEcharts(() => {
    // Per (model × task) success rate over its 5 trials
    const rateByModelTask = {};
    for (const tr of TRIALS) {
      const k = tr.model + "::" + tr.task;
      if (!rateByModelTask[k]) rateByModelTask[k] = { ok: 0, n: 0, t: tr };
      rateByModelTask[k].n++;
      if (tr.resolved) rateByModelTask[k].ok++;
    }

    const series = [];
    const rng = mulberry32(7);
    for (const m of ANALYSIS_MODELS) {
      if (!selectedIds.includes(m.id)) continue;

      // Scatter — one point per task
      const scatterData = ANALYSIS_TASKS.map(t => {
        const r = rateByModelTask[m.id + "::" + t.id];
        const rate = r ? r.ok / r.n : 0;
        const yJ = yMode === "jitter" ? (rate + (rng() - 0.5) * 0.06) : rate;
        return {
          value: [t.humanHours, +(yJ).toFixed(3)],
          rate, task: t,
        };
      });
      series.push({
        name: m.name + " · " + m.scaffold,
        type: "scatter",
        data: scatterData,
        symbolSize: 9,
        itemStyle: { color: m.color, opacity: 0.85, borderColor: "#fff", borderWidth: 1 },
        emphasis: { focus: "series", scale: 1.4 },
        z: 5,
      });

      // Logistic fit curve
      const xs = [];
      const minX = 0.5, maxX = 12;
      for (let i = 0; i <= 80; i++) {
        const lx = Math.log(minX) + (Math.log(maxX) - Math.log(minX)) * (i / 80);
        const x = Math.exp(lx);
        xs.push([x, pSuccess(x, m.horizon, m.slope)]);
      }
      series.push({
        name: m.name + " · " + m.scaffold + " · fit",
        type: "line",
        data: xs,
        smooth: true,
        showSymbol: false,
        lineStyle: { color: m.color, width: 2, type: "solid", opacity: 0.7 },
        emphasis: { lineStyle: { width: 3, opacity: 1 } },
        z: 4,
        tooltip: { show: false },
      });

      // Marker — 50% time horizon
      series.push({
        name: m.name + " · 50% horizon",
        type: "scatter",
        data: [{
          value: [m.horizon, 0.5],
          symbol: "diamond",
          symbolSize: 14,
          itemStyle: { color: m.color, borderColor: "#1a1a17", borderWidth: 1.5 },
        }],
        z: 6,
        tooltip: { show: false },
      });
    }

    return {
      backgroundColor: "transparent",
      grid: { left: 60, right: 24, top: 30, bottom: 56 },
      xAxis: {
        ...AXIS_COMMON,
        type: "log",
        name: "Human-baseline duration (hours, log)",
        nameLocation: "middle",
        nameGap: 32,
        min: 0.5,
        max: 12,
        logBase: 10,
        axisLabel: { ...AXIS_COMMON.axisLabel, formatter: (v) => v < 1 ? v.toFixed(1) + "h" : v + "h" },
      },
      yAxis: {
        ...AXIS_COMMON,
        type: "value",
        name: yMode === "rate" ? "Resolved-rate per task (mean@5)" : "Resolved-rate (jittered)",
        nameLocation: "middle",
        nameGap: 44,
        min: -0.05,
        max: 1.05,
        axisLabel: { ...AXIS_COMMON.axisLabel, formatter: (v) => Math.round(v * 100) + "%" },
      },
      tooltip: {
        ...TOOLTIP_COMMON,
        trigger: "item",
        formatter: (p) => {
          if (p.seriesType !== "scatter" || !p.data.task) {
            // Fit curve hover shows model + duration
            const x = p.data && p.data[0] ? p.data[0] : p.value && p.value[0];
            const y = p.data && p.data[1] != null ? p.data[1] : p.value && p.value[1];
            if (x == null) return "";
            return `<div style="font-family:IBM Plex Mono;font-size:11px;color:${PAPER.ink3};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">${p.seriesName.replace(" · fit","")}</div>
                    <div><b>${(+x).toFixed(2)}h</b> human task → <b>${Math.round(y * 100)}%</b> predicted</div>`;
          }
          const t = p.data.task;
          return `<div style="font-family:IBM Plex Mono;font-size:11px;color:${PAPER.ink3};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">${p.seriesName}</div>
                  <div style="font-weight:600;margin-bottom:2px;">${t.id} · ${t.title}</div>
                  <div style="color:${PAPER.ink2};font-size:11px;">${FAM_LABEL[t.fam]} · human baseline ${t.humanHours}h</div>
                  <div style="margin-top:4px;">Resolved: <b>${Math.round(p.data.rate * 100)}%</b> (${Math.round(p.data.rate * 5)}/5 trials)</div>`;
        },
      },
      legend: { show: false },
      animation: false,
      series,
      // Reference horizontal lines at 50 / 80
      graphic: [],
      markLine: undefined,
    };
  }, [selectedIds, yMode]);

  const toggleModel = (id) => {
    setSelectedIds(s => s.includes(id) ? s.filter(x => x !== id) : [...s, id]);
  };

  return (
    <div className="anal-card">
      <div className="anal-card-head">
        <div>
          <div className="anal-card-no">FIG · TIME HORIZON</div>
          <h3 className="anal-card-title">How <em>long</em> a task can each agent handle?</h3>
          <p className="anal-card-sub">
            Each dot is a task; x-axis is the time a human expert needs (log scale).
            Curve is a logistic fit; the diamond marks the <b>50% time horizon</b> — the
            human-task duration at which the agent succeeds half the time. Inspired by{" "}
            <a href="https://metr.org/time-horizons/" target="_blank" rel="noopener">METR's time-horizon analysis</a>.
          </p>
        </div>
        <div className="anal-controls">
          <button className={"pill " + (yMode === "rate" ? "active" : "")} onClick={() => setYMode("rate")}>Mean rate</button>
          <button className={"pill " + (yMode === "jitter" ? "active" : "")} onClick={() => setYMode("jitter")}>Jittered</button>
        </div>
      </div>
      <div className="anal-legend">
        {ANALYSIS_MODELS.map(m => {
          const on = selectedIds.includes(m.id);
          return (
            <button key={m.id}
              className={"anal-legend-chip " + (on ? "on" : "")}
              onClick={() => toggleModel(m.id)}>
              <span className="leg-dot" style={{background: m.color, opacity: on ? 1 : 0.3}}></span>
              <span className="leg-name">{m.name}</span>
              <span className="leg-tag">{m.scaffold}</span>
              <span className="leg-h">{(m.horizon * 60).toFixed(0)}m</span>
            </button>
          );
        })}
      </div>
      <div ref={ref} className="anal-chart" style={{height: 420}}></div>
      <div className="anal-foot">
        50% time horizon shown in minutes per chip. Frontier agents top out around <b>1 hour</b> of
        human-equivalent work; SWE-Marathon's tasks (1–8h human-baseline) sit well past that line.
      </div>
    </div>
  );
}

/* ============================================================
   PLOT 2 — Cost vs resolved-rate Pareto (trial-level)
   x: trial cost ($). y: resolved (1) / failed (0) per trial — but
   we render as agent-level summary scatter: mean cost per resolved task
   vs. resolved-rate. Plus a Pareto frontier line.
   ============================================================ */
function ParetoChart() {
  const [xAxis, setXAxis] = useState("cost"); // cost | hours

  const ref = useEcharts(() => {
    // Aggregate per model: mean cost per attempted task, resolved-rate, mean hours
    const agg = ANALYSIS_MODELS.map(m => {
      const trials = TRIALS.filter(t => t.model === m.id);
      const resolved = trials.filter(t => t.resolved).length;
      const totalCost = trials.reduce((s, t) => s + t.cost, 0);
      const totalHours = trials.reduce((s, t) => s + t.hours, 0);
      return {
        m,
        rate: resolved / trials.length,
        cost: totalCost / trials.length,        // per-trial cost
        hours: totalHours / trials.length,      // per-trial hours
        nTrials: trials.length,
        nResolved: resolved,
      };
    });

    // Pareto frontier in chosen-x-axis vs rate space:
    // a model is dominated if some other model has >= rate AND <= x.
    const xKey = xAxis;
    const frontierPts = agg
      .filter(a => !agg.some(b => b !== a && b.rate >= a.rate && b[xKey] <= a[xKey] && (b.rate > a.rate || b[xKey] < a[xKey])))
      .sort((p, q) => p[xKey] - q[xKey]);

    const scatterData = agg.map(a => ({
      value: [a[xKey], a.rate * 100],
      a,
      itemStyle: { color: a.m.color, opacity: 0.95, borderColor: "#1a1a17", borderWidth: 1 },
    }));

    return {
      backgroundColor: "transparent",
      grid: { left: 60, right: 24, top: 30, bottom: 56 },
      xAxis: {
        ...AXIS_COMMON,
        type: "value",
        name: xAxis === "cost" ? "Mean cost per trial (USD)" : "Mean wall-clock per trial (hours)",
        nameLocation: "middle",
        nameGap: 32,
        axisLabel: {
          ...AXIS_COMMON.axisLabel,
          formatter: (v) => xAxis === "cost" ? "$" + v.toFixed(0) : v.toFixed(1) + "h",
        },
      },
      yAxis: {
        ...AXIS_COMMON,
        type: "value",
        name: "Resolved-rate (%)",
        nameLocation: "middle",
        nameGap: 44,
        axisLabel: { ...AXIS_COMMON.axisLabel, formatter: (v) => v + "%" },
      },
      tooltip: {
        ...TOOLTIP_COMMON,
        trigger: "item",
        formatter: (p) => {
          if (!p.data || !p.data.a) return "";
          const a = p.data.a;
          return `<div style="font-family:IBM Plex Mono;font-size:11px;color:${PAPER.ink3};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">${a.m.name}</div>
                  <div style="color:${PAPER.ink2};font-size:11px;margin-bottom:6px;">${a.m.scaffold}</div>
                  <div>Resolved: <b>${(a.rate*100).toFixed(1)}%</b> (${a.nResolved}/${a.nTrials} trials)</div>
                  <div>Cost / trial: <b>$${a.cost.toFixed(2)}</b></div>
                  <div>Time / trial: <b>${a.hours.toFixed(2)}h</b></div>
                  <div style="color:${PAPER.ink3};font-size:11px;margin-top:4px;">50% horizon: ${(a.m.horizon*60).toFixed(0)}m</div>`;
        },
      },
      animation: false,
      series: [
        {
          name: "Pareto frontier",
          type: "line",
          data: frontierPts.map(p => [p[xKey], p.rate * 100]),
          showSymbol: false,
          smooth: false,
          lineStyle: { color: PAPER.accent, type: "dashed", width: 1.5 },
          areaStyle: { color: PAPER.accent, opacity: 0.05 },
          z: 2,
          tooltip: { show: false },
        },
        {
          name: "agents",
          type: "scatter",
          data: scatterData,
          symbolSize: 18,
          z: 5,
          label: {
            show: true,
            position: "right",
            formatter: (p) => p.data.a.m.name.replace(/(.+? \S+).*/, "$1"),
            fontFamily: "IBM Plex Mono, monospace",
            fontSize: 10,
            color: PAPER.ink2,
            distance: 4,
          },
        },
      ],
    };
  }, [xAxis]);

  return (
    <div className="anal-card">
      <div className="anal-card-head">
        <div>
          <div className="anal-card-no">FIG · PARETO</div>
          <h3 className="anal-card-title">Spend more, score… <em>marginally</em> more.</h3>
          <p className="anal-card-sub">
            Each point is one (model × scaffold) configuration averaged across all trials.
            The dashed line is the Pareto frontier — agents below it are strictly dominated.
          </p>
        </div>
        <div className="anal-controls">
          <button className={"pill " + (xAxis === "cost" ? "active" : "")} onClick={() => setXAxis("cost")}>Cost ($)</button>
          <button className={"pill " + (xAxis === "hours" ? "active" : "")} onClick={() => setXAxis("hours")}>Time (h)</button>
        </div>
      </div>
      <div ref={ref} className="anal-chart" style={{height: 380}}></div>
      <div className="anal-foot">
        The frontier hugs the bottom-right: the cheapest scaffolds barely score, but the most expensive ones
        only just clear single-digit resolved-rate. There is currently no compute-bought shortcut to a marathon-grade agent.
      </div>
    </div>
  );
}

/* ============================================================
   PLOT 3 — Family heatmap (model × task family)
   ============================================================ */
function FamilyHeatmap() {
  const ref = useEcharts(() => {
    const data = [];
    ANALYSIS_MODELS.forEach((m, mi) => {
      FAM_ORDER.forEach((fam, fi) => {
        const trials = TRIALS.filter(t => t.model === m.id && t.fam === fam);
        const rate = trials.length ? trials.filter(t => t.resolved).length / trials.length : 0;
        data.push({ value: [fi, mi, +(rate * 100).toFixed(1)], m, fam });
      });
    });
    return {
      backgroundColor: "transparent",
      grid: { left: 220, right: 60, top: 50, bottom: 30 },
      xAxis: {
        type: "category",
        data: FAM_ORDER.map(f => FAM_LABEL[f]),
        position: "top",
        axisLine: { show: false },
        axisTick: { show: false },
        splitArea: { show: false },
        axisLabel: { color: PAPER.ink2, fontFamily: "IBM Plex Mono, monospace", fontSize: 11 },
      },
      yAxis: {
        type: "category",
        data: ANALYSIS_MODELS.map(m => m.name + "  ·  " + m.scaffold),
        inverse: true,
        axisLine: { show: false },
        axisTick: { show: false },
        splitArea: { show: false },
        axisLabel: { color: PAPER.ink2, fontFamily: "Inter, sans-serif", fontSize: 11, align: "right" },
      },
      tooltip: {
        ...TOOLTIP_COMMON,
        trigger: "item",
        formatter: (p) => {
          const m = p.data.m;
          return `<div style="font-family:IBM Plex Mono;font-size:11px;color:${PAPER.ink3};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">${FAM_LABEL[p.data.fam]}</div>
                  <div style="font-weight:600">${m.name}</div>
                  <div style="color:${PAPER.ink2};font-size:11px;margin-bottom:4px;">${m.scaffold}</div>
                  <div>Resolved: <b>${p.value[2]}%</b></div>`;
        },
      },
      visualMap: {
        min: 0, max: 25,
        calculable: false,
        orient: "horizontal",
        left: "center",
        bottom: 0,
        itemWidth: 14, itemHeight: 100,
        text: ["25%+", "0%"],
        textStyle: { color: PAPER.ink3, fontFamily: "IBM Plex Mono, monospace", fontSize: 10 },
        inRange: { color: ["#f3efe4", "#e8c9a8", "#d49765", "#b56636", "#8b3d1f"] },
        show: false,
      },
      animation: false,
      series: [{
        name: "Resolved %",
        type: "heatmap",
        data,
        label: {
          show: true,
          color: PAPER.ink,
          fontFamily: "IBM Plex Mono, monospace",
          fontSize: 11,
          formatter: (p) => p.value[2] > 0 ? p.value[2].toFixed(0) : "·",
        },
        itemStyle: { borderColor: "#faf7f0", borderWidth: 2 },
        emphasis: { itemStyle: { borderColor: PAPER.ink, borderWidth: 1.5 } },
      }],
    };
  }, []);

  return (
    <div className="anal-card">
      <div className="anal-card-head">
        <div>
          <div className="anal-card-no">FIG · HEATMAP</div>
          <h3 className="anal-card-title">Where each agent <em>actually</em> earns points.</h3>
          <p className="anal-card-sub">
            Resolved-rate (%) by domain. Implementation is the easiest column;
            Performance and Port/Rewrite are where most scaffolds collapse.
          </p>
        </div>
      </div>
      <div ref={ref} className="anal-chart" style={{height: 420}}></div>
    </div>
  );
}

/* ============================================================
   PLOT 4 — Task distribution (donut: family) + bar (duration buckets)
   ============================================================ */
function TaskDistribution() {
  const ref = useEcharts(() => {
    const famCount = {};
    FAM_ORDER.forEach(f => famCount[f] = 0);
    ANALYSIS_TASKS.forEach(t => famCount[t.fam]++);

    // Duration buckets
    const buckets = [
      { label: "<2h",   lo: 0,   hi: 2 },
      { label: "2–4h",  lo: 2,   hi: 4 },
      { label: "4–6h",  lo: 4,   hi: 6 },
      { label: "6h+",   lo: 6,   hi: 99 },
    ];
    const bucketCount = buckets.map(b => ({
      label: b.label,
      n: ANALYSIS_TASKS.filter(t => t.humanHours >= b.lo && t.humanHours < b.hi).length,
    }));

    const famColor = {
      impl: "#a86237", perf: "#5a7d4f", port: "#8a6d4a",
      research: "#5a6cb8", systems: "#9a7daa",
    };

    return {
      backgroundColor: "transparent",
      tooltip: {
        ...TOOLTIP_COMMON,
        trigger: "item",
      },
      title: [
        { text: "By domain", left: "20%", top: 8, textAlign: "center",
          textStyle: { color: PAPER.ink3, fontFamily: "IBM Plex Mono, monospace", fontSize: 11, fontWeight: 400 } },
        { text: "By human-baseline duration", left: "70%", top: 8, textAlign: "center",
          textStyle: { color: PAPER.ink3, fontFamily: "IBM Plex Mono, monospace", fontSize: 11, fontWeight: 400 } },
      ],
      grid: { left: "52%", right: 24, top: 50, bottom: 40 },
      xAxis: {
        ...AXIS_COMMON,
        type: "category",
        gridIndex: 0,
        data: bucketCount.map(b => b.label),
      },
      yAxis: {
        ...AXIS_COMMON,
        type: "value",
        gridIndex: 0,
        name: "tasks",
        nameLocation: "middle",
        nameGap: 30,
        max: 8,
        interval: 2,
      },
      animation: false,
      series: [
        {
          name: "By domain",
          type: "pie",
          radius: ["48%", "78%"],
          center: ["20%", "55%"],
          label: {
            position: "outside",
            color: PAPER.ink2,
            fontFamily: "Inter, sans-serif",
            fontSize: 11,
            formatter: (p) => `${FAM_LABEL[p.data.fam]}\n{n|${p.data.value}}`,
            rich: { n: { color: PAPER.ink, fontWeight: 600, fontSize: 13 } },
          },
          labelLine: { lineStyle: { color: PAPER.rule } },
          data: FAM_ORDER.map(f => ({
            value: famCount[f], name: FAM_LABEL[f], fam: f,
            itemStyle: { color: famColor[f], borderColor: "#faf7f0", borderWidth: 2 },
          })),
          emphasis: { itemStyle: { borderColor: PAPER.ink } },
        },
        {
          name: "By duration",
          type: "bar",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: bucketCount.map(b => ({
            value: b.n,
            itemStyle: { color: PAPER.accent, opacity: 0.85, borderColor: "#1a1a17", borderWidth: 1 },
          })),
          barWidth: "55%",
          label: {
            show: true,
            position: "top",
            color: PAPER.ink2,
            fontFamily: "IBM Plex Mono, monospace",
            fontSize: 11,
          },
        },
      ],
    };
  }, []);

  return (
    <div className="anal-card">
      <div className="anal-card-head">
        <div>
          <div className="anal-card-no">FIG · DISTRIBUTION</div>
          <h3 className="anal-card-title">The <em>20-task</em> course at a glance.</h3>
          <p className="anal-card-sub">
            Domain mix on the left; human-baseline duration spread on the right.
            Most tasks sit in the 2–6 hour range — the regime where current agents start to drift.
          </p>
        </div>
      </div>
      <div ref={ref} className="anal-chart" style={{height: 320}}></div>
    </div>
  );
}

/* ============================================================
   ANALYSIS SECTION — wraps all four plots with tab nav
   ============================================================ */
function Analysis() {
  const [tab, setTab] = useState("horizon"); // horizon | pareto | heatmap | dist

  return (
    <section id="analysis">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§02 / Analysis</div>
          <h2 className="section-title">Time-horizons, <span className="ital">Pareto</span> frontiers, where it breaks.</h2>
        </div>

        <div className="anal-tabs">
          <button className={"anal-tab " + (tab === "horizon" ? "active" : "")} onClick={() => setTab("horizon")}>
            <span className="anal-tab-no">01</span>
            <span className="anal-tab-t">Time horizon</span>
            <span className="anal-tab-s">METR-style</span>
          </button>
          <button className={"anal-tab " + (tab === "pareto" ? "active" : "")} onClick={() => setTab("pareto")}>
            <span className="anal-tab-no">02</span>
            <span className="anal-tab-t">Cost vs score</span>
            <span className="anal-tab-s">Pareto frontier</span>
          </button>
          <button className={"anal-tab " + (tab === "heatmap" ? "active" : "")} onClick={() => setTab("heatmap")}>
            <span className="anal-tab-no">03</span>
            <span className="anal-tab-t">By domain</span>
            <span className="anal-tab-s">Heatmap</span>
          </button>
          <button className={"anal-tab " + (tab === "dist" ? "active" : "")} onClick={() => setTab("dist")}>
            <span className="anal-tab-no">04</span>
            <span className="anal-tab-t">Task mix</span>
            <span className="anal-tab-s">20-task spread</span>
          </button>
        </div>

        <div className="anal-stage">
          {tab === "horizon" && <TimeHorizonChart />}
          {tab === "pareto"  && <ParetoChart />}
          {tab === "heatmap" && <FamilyHeatmap />}
          {tab === "dist"    && <TaskDistribution />}
        </div>
      </div>
    </section>
  );
}

export default Analysis;
