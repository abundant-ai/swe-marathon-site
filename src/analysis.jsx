/* SWE-Marathon analysis section — time-horizon scatter, cost / pass@1 Pareto,
   family heatmap, task distribution. All numbers come from src/data.js
   which is derived from the canonical 1,100-trial sweep. */

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
import {
  CAT_LABEL,
  HEADLINE,
  LEADERBOARD,
  MODEL_COLORS,
  PER_TASK_PASS1,
  TASKS,
} from "./data.js";

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

/* ---------- TASKS: keyed by id, with paper-stated expert hours ---------- */
const ANALYSIS_TASKS = TASKS.map((t) => ({
  id: t.id,
  cat: t.cat,
  title: t.title,
  humanHours: t.humanH,
  agentHours: t.agentH,
  pass1: t.pass1,
}));

const FAM_LABEL = CAT_LABEL;
const FAM_ORDER = ["library", "clone", "ml", "algo"];

/* ---------- MODELS: 11 real configs from the leaderboard ---------- */
const ANALYSIS_MODELS = LEADERBOARD
  .filter((r) => !r.ref)
  .map((r) => ({
    id: r.id,
    name: r.name,
    scaffold: r.scaffold,
    color: MODEL_COLORS[r.id] || "#888",
    pass1: r.pass1,
    costPerTrial: r.costAvg,
    avgTokensM: r.tokAvg,
    perCat: r.perCat,
  }));

/* Logistic fit to per-task pass-rate vs. human-hours: we fit a single
   slope per model on points {(humanHours_t, pass1_t)} and report the
   inferred 50%-horizon. Pure descriptive — used only for the hover
   curve and the diamond marker. */
function fitLogistic(points) {
  // points = [{x: humanHours, y: rate∈[0,1]}, ...]
  // Search over (logHorizon, slope) by coarse grid + refine.
  const validPts = points.filter((p) => p.x > 0 && Number.isFinite(p.y));
  if (validPts.length === 0) return { horizon: 100, slope: 1.2 };
  const sigm = (x) => 1 / (1 + Math.exp(x));
  const loss = (logH, slope) =>
    validPts.reduce((s, p) => {
      const yhat = sigm(slope * (Math.log(p.x) - logH));
      return s + (yhat - p.y) ** 2;
    }, 0);
  let best = { logH: Math.log(150), slope: 1.2, l: Infinity };
  for (const slope of [0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5]) {
    for (let logH = Math.log(20); logH <= Math.log(800); logH += 0.15) {
      const l = loss(logH, slope);
      if (l < best.l) best = { logH, slope, l };
    }
  }
  return { horizon: Math.exp(best.logH), slope: best.slope };
}

/* For each model, build its (task, pass1) point cloud and fit. */
const FITS = Object.fromEntries(
  ANALYSIS_MODELS.map((m) => {
    const points = ANALYSIS_TASKS.map((t) => ({
      x: t.humanHours,
      // PER_TASK_PASS1 keys use "post-train-ifeval"; data.js TASKS too — match.
      y: (PER_TASK_PASS1[m.id]?.[t.id] ?? 0) / 100,
    }));
    return [m.id, fitLogistic(points)];
  })
);

/* TRIALS-equivalent: pre-aggregated per (model, task) cell. We don't have
   trial-level cost in the manifest aggregation, so cost is per-config
   averaged from the leaderboard and shared across that config's trials. */
const TRIALS = ANALYSIS_MODELS.flatMap((m) =>
  ANALYSIS_TASKS.map((t) => {
    const rate = (PER_TASK_PASS1[m.id]?.[t.id] ?? 0) / 100;
    return {
      model: m.id,
      task: t.id,
      cat: t.cat,
      humanHours: t.humanHours,
      agentHours: t.agentHours,
      rate,                         // mean@5 for this (model, task)
      cost: m.costPerTrial,         // per-trial avg cost for this config
      tokensM: m.avgTokensM,
    };
  })
);

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
   PLOT 1 — Time-horizon scatter (METR-style)
   x: human-expert duration (hours, log). y: per-task pass@1.
   Logistic fit per model + 50%-horizon diamond.
   ============================================================ */
function TimeHorizonChart() {
  const [selectedIds, setSelectedIds] = useState(() =>
    ANALYSIS_MODELS.slice(0, 4).map((m) => m.id)
  );

  const ref = useEcharts(() => {
    const series = [];
    for (const m of ANALYSIS_MODELS) {
      if (!selectedIds.includes(m.id)) continue;

      const scatterData = ANALYSIS_TASKS.map((t) => {
        const rate = (PER_TASK_PASS1[m.id]?.[t.id] ?? 0) / 100;
        return { value: [t.humanHours, +rate.toFixed(3)], rate, task: t };
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

      // Fit curve
      const fit = FITS[m.id];
      const sigm = (z) => 1 / (1 + Math.exp(z));
      const xs = [];
      for (let i = 0; i <= 80; i++) {
        const lx = Math.log(20) + (Math.log(800) - Math.log(20)) * (i / 80);
        const x = Math.exp(lx);
        xs.push([x, sigm(fit.slope * (Math.log(x) - Math.log(fit.horizon)))]);
      }
      series.push({
        name: m.name + " · " + m.scaffold + " · fit",
        type: "line",
        data: xs,
        smooth: true,
        showSymbol: false,
        lineStyle: { color: m.color, width: 2, opacity: 0.55 },
        z: 4,
        tooltip: { show: false },
      });

      // 50%-horizon diamond, drawn only if it sits inside the visible band
      if (fit.horizon >= 20 && fit.horizon <= 800) {
        series.push({
          name: m.name + " · 50% horizon",
          type: "scatter",
          data: [{
            value: [fit.horizon, 0.5],
            symbol: "diamond",
            symbolSize: 14,
            itemStyle: { color: m.color, borderColor: "#1a1a17", borderWidth: 1.5 },
          }],
          z: 6,
          tooltip: { show: false },
        });
      }
    }

    return {
      backgroundColor: "transparent",
      grid: { left: 60, right: 24, top: 30, bottom: 56 },
      xAxis: {
        ...AXIS_COMMON,
        type: "log",
        name: "Human-expert duration (hours, log)",
        nameLocation: "middle",
        nameGap: 32,
        min: 20,
        max: 800,
        logBase: 10,
        axisLabel: { ...AXIS_COMMON.axisLabel, formatter: (v) => v + "h" },
      },
      yAxis: {
        ...AXIS_COMMON,
        type: "value",
        name: "Pass@1 per task (mean@5)",
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
            const x = p.data && p.data[0] != null ? p.data[0] : p.value && p.value[0];
            const y = p.data && p.data[1] != null ? p.data[1] : p.value && p.value[1];
            if (x == null) return "";
            return `<div style="font-family:IBM Plex Mono;font-size:11px;color:${PAPER.ink3};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">${p.seriesName.replace(" · fit","")}</div>
                    <div><b>${(+x).toFixed(0)}h</b> human task → <b>${Math.round(y * 100)}%</b> predicted</div>`;
          }
          const t = p.data.task;
          return `<div style="font-family:IBM Plex Mono;font-size:11px;color:${PAPER.ink3};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">${p.seriesName}</div>
                  <div style="font-weight:600;margin-bottom:2px;">${t.title}</div>
                  <div style="color:${PAPER.ink2};font-size:11px;">${FAM_LABEL[t.cat]} · ${t.humanHours}h human · ${t.agentHours}h agent budget</div>
                  <div style="margin-top:4px;">Pass@1: <b>${Math.round(p.data.rate * 100)}%</b> (${Math.round(p.data.rate * 5)}/5 trials)</div>`;
        },
      },
      legend: { show: false },
      animation: false,
      series,
    };
  }, [selectedIds]);

  const toggleModel = (id) => {
    setSelectedIds((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  };

  return (
    <div className="anal-card">
      <div className="anal-card-head">
        <div>
          <div className="anal-card-no">FIG · TIME HORIZON</div>
          <h3 className="anal-card-title">How long a task can each agent handle?</h3>
          <p className="anal-card-sub">
            Each dot is one of the 20 SWE-Marathon tasks; x-axis is the expert
            estimate (40–400h, log scale). Curve is a logistic fit over the
            (humanHours, pass@1) cloud per config; the diamond, when in range,
            marks the 50%-horizon. Frontier agents sit nowhere near the
            multi-day region this benchmark targets.
          </p>
        </div>
      </div>
      <div className="anal-legend">
        {ANALYSIS_MODELS.map((m) => {
          const on = selectedIds.includes(m.id);
          const fit = FITS[m.id];
          return (
            <button key={m.id}
              className={"anal-legend-chip " + (on ? "on" : "")}
              onClick={() => toggleModel(m.id)}>
              <span className="leg-dot" style={{ background: m.color, opacity: on ? 1 : 0.3 }}></span>
              <span className="leg-name">{m.name}</span>
              <span className="leg-tag">{m.scaffold}</span>
              <span className="leg-h">{m.pass1.toFixed(1)}%</span>
            </button>
          );
        })}
      </div>
      <div ref={ref} className="anal-chart" style={{ height: 420 }}></div>
      <div className="anal-foot">
        Pass@1 in the leg chips is the canonical-sweep number (n = 100 trials per config across the 20 tasks).
      </div>
    </div>
  );
}

/* ============================================================
   PLOT 2 — Cost / tokens vs pass@1 Pareto (per-config)
   ============================================================ */
function ParetoChart() {
  const [xAxis, setXAxis] = useState("cost"); // cost | tokens

  const ref = useEcharts(() => {
    const xKey = xAxis === "cost" ? "costPerTrial" : "avgTokensM";
    const agg = ANALYSIS_MODELS
      .filter((m) => m[xKey] != null && m[xKey] >= 0)
      .map((m) => ({ m, x: m[xKey], rate: m.pass1 / 100 }));

    const frontierPts = agg
      .filter((a) => !agg.some((b) => b !== a && b.rate >= a.rate && b.x <= a.x && (b.rate > a.rate || b.x < a.x)))
      .sort((p, q) => p.x - q.x);

    const scatterData = agg.map((a) => ({
      value: [a.x, a.rate * 100],
      a,
      itemStyle: { color: a.m.color, opacity: 0.95, borderColor: "#1a1a17", borderWidth: 1 },
    }));

    return {
      backgroundColor: "transparent",
      grid: { left: 60, right: 24, top: 30, bottom: 56 },
      xAxis: {
        ...AXIS_COMMON,
        type: "value",
        name: xAxis === "cost" ? "Mean cost per trial (USD)" : "Mean tokens per trial (M)",
        nameLocation: "middle",
        nameGap: 32,
        axisLabel: {
          ...AXIS_COMMON.axisLabel,
          formatter: (v) => xAxis === "cost" ? "$" + v.toFixed(0) : v.toFixed(0) + "M",
        },
      },
      yAxis: {
        ...AXIS_COMMON,
        type: "value",
        name: "Pass@1 (%)",
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
                  <div>Pass@1: <b>${a.m.pass1.toFixed(1)}%</b></div>
                  <div>Cost / trial: <b>${a.m.costPerTrial != null ? "$" + a.m.costPerTrial.toFixed(2) : "—"}</b></div>
                  <div>Tokens / trial: <b>${a.m.avgTokensM.toFixed(1)}M</b></div>`;
        },
      },
      animation: false,
      series: [
        {
          name: "Pareto frontier",
          type: "line",
          data: frontierPts.map((p) => [p.x, p.rate * 100]),
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
          <h3 className="anal-card-title">Cost-effective configs are not the highest-scoring configs.</h3>
          <p className="anal-card-sub">
            Each point is one of the 11 (model × scaffold) configs averaged
            across its 100-trial canonical sweep. The dashed line is the Pareto
            frontier — configs below it are strictly dominated. Several
            cost-effective systems sit near the empirical frontier; the most
            expensive ones don't always score best.
          </p>
        </div>
        <div className="anal-controls">
          <button className={"pill " + (xAxis === "cost" ? "active" : "")} onClick={() => setXAxis("cost")}>Cost ($)</button>
          <button className={"pill " + (xAxis === "tokens" ? "active" : "")} onClick={() => setXAxis("tokens")}>Tokens (M)</button>
        </div>
      </div>
      <div ref={ref} className="anal-chart" style={{ height: 380 }}></div>
      <div className="anal-foot">
        Token spend is mean (input + output) per trial; cost is mean USD per trial from manifest.cost_usd.
      </div>
    </div>
  );
}

/* ============================================================
   PLOT 3 — Family heatmap (config × task family)
   ============================================================ */
function FamilyHeatmap() {
  const ref = useEcharts(() => {
    const data = [];
    ANALYSIS_MODELS.forEach((m, mi) => {
      FAM_ORDER.forEach((fam, fi) => {
        const rate = m.perCat?.[fam] ?? 0;
        data.push({ value: [fi, mi, +rate.toFixed(1)], m, fam });
      });
    });
    return {
      backgroundColor: "transparent",
      grid: { left: 220, right: 60, top: 50, bottom: 30 },
      xAxis: {
        type: "category",
        data: FAM_ORDER.map((f) => FAM_LABEL[f]),
        position: "top",
        axisLine: { show: false },
        axisTick: { show: false },
        splitArea: { show: false },
        axisLabel: {
          color: PAPER.ink2,
          fontFamily: "IBM Plex Mono, monospace",
          fontSize: 11,
          interval: 0,             // force every header to render
          hideOverlap: false,
          overflow: "break",       // wrap long labels onto two lines
          width: 110,
          lineHeight: 13,
        },
      },
      yAxis: {
        type: "category",
        data: ANALYSIS_MODELS.map((m) => m.name + "  ·  " + m.scaffold),
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
                  <div>Pass@1: <b>${p.value[2]}%</b></div>`;
        },
      },
      visualMap: {
        min: 0, max: 80,
        calculable: false,
        show: false,
        inRange: { color: ["#f3efe4", "#e8c9a8", "#d49765", "#b56636", "#8b3d1f"] },
      },
      animation: false,
      series: [{
        name: "Pass@1",
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
          <h3 className="anal-card-title">Where each config actually scores.</h3>
          <p className="anal-card-sub">
            Pass@1 (%) by task family. ML engineering is the only column with
            non-trivial scores across configs; library reproductions, product
            clones, and algorithmic optimisation are essentially zero for
            most configs in the canonical sweep.
          </p>
        </div>
      </div>
      <div ref={ref} className="anal-chart" style={{ height: 420 }}></div>
    </div>
  );
}

/* ============================================================
   PLOT 4 — Task distribution (donut: family) + bar (duration buckets)
   ============================================================ */
function TaskDistribution() {
  const ref = useEcharts(() => {
    const famCount = {};
    FAM_ORDER.forEach((f) => (famCount[f] = 0));
    ANALYSIS_TASKS.forEach((t) => famCount[t.cat]++);

    // Human-hour buckets matching the paper's 40–400h range
    const buckets = [
      { label: "40–60h",  lo: 0,   hi: 60 },
      { label: "60–100h", lo: 60,  hi: 100 },
      { label: "100–200h", lo: 100, hi: 200 },
      { label: "200h+",   lo: 200, hi: 9999 },
    ];
    const bucketCount = buckets.map((b) => ({
      label: b.label,
      n: ANALYSIS_TASKS.filter((t) => t.humanHours >= b.lo && t.humanHours < b.hi).length,
    }));

    const famColor = {
      library: "#a86237", clone: "#5a7d4f", ml: "#5a6cb8", algo: "#9a7daa",
    };

    return {
      backgroundColor: "transparent",
      tooltip: { ...TOOLTIP_COMMON, trigger: "item" },
      title: [
        { text: "By family", left: "20%", top: 8, textAlign: "center",
          textStyle: { color: PAPER.ink3, fontFamily: "IBM Plex Mono, monospace", fontSize: 11, fontWeight: 400 } },
        { text: "By human-expert estimate", left: "70%", top: 8, textAlign: "center",
          textStyle: { color: PAPER.ink3, fontFamily: "IBM Plex Mono, monospace", fontSize: 11, fontWeight: 400 } },
      ],
      grid: { left: "52%", right: 24, top: 50, bottom: 40 },
      xAxis: {
        ...AXIS_COMMON,
        type: "category",
        gridIndex: 0,
        data: bucketCount.map((b) => b.label),
      },
      yAxis: {
        ...AXIS_COMMON,
        type: "value",
        gridIndex: 0,
        name: "tasks",
        nameLocation: "middle",
        nameGap: 30,
        max: 10,
        interval: 2,
      },
      animation: false,
      series: [
        {
          name: "By family",
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
          data: FAM_ORDER.map((f) => ({
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
          data: bucketCount.map((b) => ({
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
          <h3 className="anal-card-title">The 20-task course at a glance.</h3>
          <p className="anal-card-sub">
            Family mix on the left; expert-estimate spread on the right.
            Tasks span library reproductions (8), product clones (5), ML
            engineering (5), and algorithmic optimisation (2) — agent budgets
            of 2–10h against expert estimates of 40–400h.
          </p>
        </div>
      </div>
      <div ref={ref} className="anal-chart" style={{ height: 320 }}></div>
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
          <h2 className="section-title">Time-horizons, Pareto frontiers, where it breaks.</h2>
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
