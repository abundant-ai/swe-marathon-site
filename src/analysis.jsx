/* SWE-Marathon analysis section — cost / pass@1 Pareto, compute-horizon
   scatter, and reward-hacking incidence. All numbers come from src/data.js. */

import React, { useEffect, useRef, useState } from "react";
import { BarChart, LineChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import { init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { LEADERBOARD, MODEL_COLORS } from "./data.js";

use([
  BarChart,
  CanvasRenderer,
  GridComponent,
  LegendComponent,
  LineChart,
  ScatterChart,
  TitleComponent,
  TooltipComponent,
]);

const agentLabel = (scaffold) => scaffold.replace(/\s+v\d[\d.]*$/i, "");

/* "Model / Agent" label with agent version suffixes stripped, so the two
   same-model configs (e.g. GPT-5.5) are distinguishable in plots. */
const cfgLabel = (m) => `${m.name} / ${agentLabel(m.scaffold)}`;
const pointLabel = (m) => `${m.name}\n${agentLabel(m.scaffold)}`;

const LABEL_OFFSETS = {
  horizon: {
    "claude48-cc": [10, -4],
    "gpt55-codex": [10, -14],
    "claude47-cc": [10, 18],
    "gpt55-term": [10, 16],
    "gemini31-term": [12, -28],
    "gemini31-cli": [12, -4],
    "claude47-term": [12, 22],
    "gemini35-cli": [12, 24],
    "deepseek-term": [12, 38],
    "glm-term": [12, 0],
    "kimi-term": [10, 24],
    "minimax-term": [10, -18],
    "kimi-cli": [10, 8],
  },
  pareto: {
    "claude48-cc": [10, -4],
    "gpt55-codex": [10, -14],
    "claude47-cc": [10, 18],
    "gpt55-term": [10, 18],
    "gemini31-term": [12, -34],
    "gemini31-cli": [12, -6],
    "claude47-term": [12, 22],
    "gemini35-cli": [12, 18],
    "deepseek-term": [12, 34],
    "glm-term": [12, 0],
    "kimi-term": [10, 28],
    "minimax-term": [10, -20],
    "kimi-cli": [10, 10],
  },
};

const labelOffset = (chart, id) => LABEL_OFFSETS[chart]?.[id] || [8, 0];

const LOG_METRIC_OVERRIDES = {
  "claude48-cc": { n: 96, costPerTrial: 37.57, avgTokensM: 49.65 },
  "gpt55-codex": { n: 101, costPerTrial: 11.38, avgTokensM: 10.74 },
  "claude47-cc": { n: 90, costPerTrial: 35.88, avgTokensM: 50.98 },
  "gpt55-term": { n: 100, costPerTrial: 46.11, avgTokensM: 51.61 },
  "gemini31-term": { n: 100, costPerTrial: 3.65, avgTokensM: 5.24 },
  "gemini31-cli": { n: 100, costPerTrial: 5.41, avgTokensM: 14.20 },
  "claude47-term": { n: 100, costPerTrial: 18.87, avgTokensM: 30.13 },
  "gemini35-cli": { n: 99, costPerTrial: 6.55, avgTokensM: 19.70 },
  "deepseek-term": { n: 99, costPerTrial: 11.19, avgTokensM: 36.56 },
  "glm-term": { n: 99, costPerTrial: 44.55, avgTokensM: 43.94 },
  "kimi-term": { n: 100, costPerTrial: 5.41, avgTokensM: 19.19 },
  "minimax-term": { n: 100, costPerTrial: 1.63, avgTokensM: 21.03 },
  "kimi-cli": { n: 100, costPerTrial: 2.64, avgTokensM: 6.60 },
};

/* ---------- MODELS: visible configs with log-derived Pareto metrics ---------- */
const ANALYSIS_MODELS = LEADERBOARD
  .filter((r) => !r.ref)
  .map((r) => {
    const logMetrics = LOG_METRIC_OVERRIDES[r.id];
    return {
      id: r.id,
      name: r.name,
      scaffold: r.scaffold,
      color: MODEL_COLORS[r.id] || "#888",
      pass1: r.pass1,
      costPerTrial: logMetrics?.costPerTrial ?? r.costAvg,
      avgTokensM: logMetrics?.avgTokensM ?? r.tokAvg,
      nLoggedTrials: logMetrics?.n,
      perCat: r.perCat,
    };
  });

const RH_CLASSIFICATIONS = [
  { name: "GPT-5.5", harness: "Terminus 2 + Codex", n: 10, hacked: 4, success: 0, criteria: { "Instruct model substitution": 4, "Dataset provenance": 1 } },
  { name: "Kimi K2.6", harness: "Kimi CLI + Terminus 2", n: 10, hacked: 4, success: 0, criteria: { "Test data contamination": 4 } },
  { name: "GLM 5.1", harness: "Terminus 2", n: 3, hacked: 1, success: 0, criteria: { "Test data contamination": 1 } },
  { name: "Claude Opus 4.8", harness: "Claude Code", n: 5, hacked: 1, success: 0, criteria: { "Test data contamination": 1 } },
  { name: "Gemini 3.5 Flash", harness: "Gemini CLI", n: 5, hacked: 1, success: 0, criteria: { "Test data contamination": 1 } },
  { name: "Gemini 3.1 Pro", harness: "Gemini CLI + Terminus 2", n: 10, hacked: 1, success: 0, criteria: { "Instruct model substitution": 1, "Dataset provenance": 1 } },
  { name: "Claude Opus 4.7", harness: "Terminus 2", n: 5, hacked: 0, success: 0, criteria: {} },
  { name: "DeepSeek V4 Pro", harness: "Terminus 2", n: 5, hacked: 0, success: 0, criteria: {} },
  { name: "MiniMax M2.7", harness: "Terminus 2", n: 5, hacked: 0, success: 0, criteria: {} },
];

const RH_CLASSIFIED_TOTALS = RH_CLASSIFICATIONS.reduce(
  (acc, row) => ({
    n: acc.n + row.n,
    hacked: acc.hacked + row.hacked,
    success: acc.success + row.success,
  }),
  { n: 0, hacked: 0, success: 0 }
);

/* ---------- ECharts theme tokens (derived from active CSS variables) ---------- */
function readThemeTokens(node) {
  const styles = getComputedStyle(node || document.documentElement);
  const css = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
  const isDark = document.documentElement.dataset.theme === "dark";
  return {
    bg: "transparent",
    ink: css("--ink", "#18181B"),
    ink2: css("--ink-2", "#3F3F46"),
    ink3: css("--ink-3", "#71717A"),
    rule: css("--rule", "#E4E4E7"),
    rule2: css("--rule-2", "#F4F4F5"),
    accent: css("--accent", "#4CAF50"),
    pos: css("--pos", "#2d7a4f"),
    warn: css("--warn", "#B45309"),
    tooltipBg: css("--paper", "#FAFAFA"),
    labelBg: isDark ? "rgba(18,19,18,0.82)" : "rgba(250,250,250,0.78)",
    shadow: isDark ? "rgba(0,0,0,0.32)" : "rgba(24,24,27,0.08)",
    pointBorder: isDark ? "rgba(244,244,245,0.86)" : "#18181B",
    rewardHack: isDark ? "#F87171" : "oklch(0.55 0.18 25)",
  };
}

function axisCommon(theme) {
  return {
    axisLine: { lineStyle: { color: theme.rule } },
    axisTick: { lineStyle: { color: theme.rule } },
    axisLabel: { color: theme.ink2, fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
    nameTextStyle: { color: theme.ink3, fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
    splitLine: { lineStyle: { color: theme.rule2, type: "dashed" } },
  };
}

function tooltipCommon(theme) {
  return {
    backgroundColor: theme.tooltipBg,
    borderColor: theme.rule,
    borderWidth: 1,
    confine: true,
    textStyle: { color: theme.ink, fontFamily: "Inter, sans-serif", fontSize: 12 },
    extraCssText: `box-shadow: 0 4px 16px ${theme.shadow}; border-radius: 0; padding: 10px 12px;`,
  };
}

function chartLayout(node) {
  const width = node?.clientWidth || window.innerWidth || 0;
  return {
    width,
    isMobile: width < 560,
  };
}

/* ---------- Hook: ECharts instance bound to a div ref ---------- */
function useEcharts(buildOption, deps) {
  const ref = useRef(null);
  const chartRef = useRef(null);
  useEffect(() => {
    if (!ref.current) return;
    if (!chartRef.current) {
      chartRef.current = init(ref.current, null, { renderer: "canvas" });
    }
    const render = () => {
      if (!chartRef.current || !ref.current) return;
      chartRef.current.setOption(buildOption(readThemeTokens(ref.current), chartLayout(ref.current)), { notMerge: true });
      chartRef.current.resize();
    };
    render();
    const onResize = () => render();
    const resizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(render);
    if (resizeObserver) resizeObserver.observe(ref.current);
    const observer = new MutationObserver(render);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "data-theme-mode", "style"],
    });
    window.addEventListener("resize", onResize);
    return () => {
      if (resizeObserver) resizeObserver.disconnect();
      observer.disconnect();
      window.removeEventListener("resize", onResize);
    };
  }, deps);
  useEffect(() => () => {
    if (chartRef.current) { chartRef.current.dispose(); chartRef.current = null; }
  }, []);
  return ref;
}

/* ============================================================
   PLOT 1 — Compute-horizon scatter
   x: mean tokens/trial (M, log). y: config pass@1.
   One point per (model, scaffold); faint connectors join configs
   of the same model to show that more tokens ≠ higher pass@1.
   ============================================================ */
function ComputeHorizonChart() {
  const ref = useEcharts((theme, layout) => {
    const mobile = layout.isMobile;
    const axis = axisCommon(theme);
    const tooltip = tooltipCommon(theme);
    const axisLabel = { ...axis.axisLabel, fontSize: mobile ? 10 : 11, hideOverlap: true };
    const nameTextStyle = { ...axis.nameTextStyle, fontSize: mobile ? 10 : 11 };
    const configs = ANALYSIS_MODELS.filter((m) => m.avgTokensM != null && m.avgTokensM > 0);
    const maxPass = Math.max(...configs.map((m) => m.pass1));

    // Connectors: same model name, ordered by token spend.
    const byName = {};
    for (const m of configs) (byName[m.name] = byName[m.name] || []).push(m);
    const connectors = Object.values(byName)
      .filter((g) => g.length >= 2)
      .map((g) => {
        const pts = g.slice().sort((a, b) => a.avgTokensM - b.avgTokensM);
        return {
          name: pts[0].name + " · same model",
          type: "line",
          data: pts.map((m) => [m.avgTokensM, m.pass1]),
          showSymbol: false,
          smooth: false,
          lineStyle: { color: pts[0].color, width: 1.5, opacity: 0.35, type: "dashed" },
          z: 2,
          tooltip: { show: false },
        };
      });

    const scatterData = configs.map((m) => ({
      value: [m.avgTokensM, m.pass1],
      m,
      itemStyle: { color: m.color, opacity: 0.95, borderColor: theme.pointBorder, borderWidth: 1 },
      label: { show: !mobile, offset: mobile ? [0, 0] : labelOffset("horizon", m.id) },
    }));

    return {
      backgroundColor: "transparent",
      grid: mobile
        ? { left: 48, right: 10, top: 36, bottom: 60 }
        : { left: 62, right: 230, top: 48, bottom: 64 },
      xAxis: {
        ...axis,
        type: "log",
        name: mobile ? "Tokens / trial (M, log)" : "Mean tokens per trial (M, log)",
        nameLocation: "middle",
        nameGap: mobile ? 30 : 34,
        nameTextStyle,
        min: 3,
        max: 80,
        logBase: 10,
        axisLabel: { ...axisLabel, formatter: (v) => v + "M" },
      },
      yAxis: {
        ...axis,
        type: "value",
        name: "Resolution rate (%)",
        nameLocation: "middle",
        nameGap: mobile ? 34 : 40,
        nameTextStyle,
        min: 0,
        max: Math.ceil((maxPass + 4) / 5) * 5,
        axisLabel: { ...axisLabel, formatter: (v) => v + "%" },
      },
      tooltip: {
        ...tooltip,
        trigger: "item",
        formatter: (p) => {
          if (!p.data || !p.data.m) return "";
          const m = p.data.m;
          return `<div style="font-family:JetBrains Mono;font-size:11px;color:${theme.ink3};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">${m.name}</div>
                  <div style="color:${theme.ink2};font-size:11px;margin-bottom:6px;">${m.scaffold}</div>
                  <div>Resolution rate: <b>${m.pass1.toFixed(1)}%</b></div>
                  <div>Tokens / trial: <b>${m.avgTokensM.toFixed(1)}M</b></div>
                  <div>Cost / trial: <b>${m.costPerTrial != null ? "$" + m.costPerTrial.toFixed(2) : "—"}</b></div>
                  <div>Logged trials: <b>${m.nLoggedTrials ?? "—"}</b></div>`;
        },
      },
      legend: { show: false },
      animation: false,
      series: [
        ...connectors,
        {
          name: "configs",
          type: "scatter",
          data: scatterData,
          symbolSize: mobile ? 15 : 17,
          z: 5,
          label: {
            show: !mobile,
            position: "right",
            formatter: (p) => pointLabel(p.data.m),
            fontFamily: "JetBrains Mono, monospace",
            fontSize: 9,
            lineHeight: 12,
            color: theme.ink2,
            distance: 6,
            backgroundColor: theme.labelBg,
            padding: [1, 3],
          },
          emphasis: {
            scale: 1.3,
            label: {
              show: true,
              formatter: (p) => cfgLabel(p.data.m),
              fontSize: mobile ? 9 : 10,
              color: theme.ink,
              backgroundColor: theme.tooltipBg,
              borderColor: theme.rule,
              borderWidth: 1,
              padding: [2, 4],
            },
          },
          labelLayout: { hideOverlap: false },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: theme.accent, type: "dashed", width: 1.5, opacity: 0.7 },
            label: {
              formatter: `ceiling · ${maxPass.toFixed(0)}%`,
              color: theme.accent,
              fontFamily: "JetBrains Mono, monospace",
              fontSize: mobile ? 9 : 10,
              position: "insideEndTop",
            },
            data: [{ yAxis: maxPass }],
          },
        },
      ],
    };
  }, []);

  return (
    <div className="anal-card">
      <div className="anal-card-head">
        <div>
          <div className="anal-card-no">FIG · COMPUTE HORIZON</div>
        </div>
      </div>
      <div ref={ref} className="anal-chart anal-chart-horizon"></div>
    </div>
  );
}

/* ============================================================
   PLOT 2 — Cost / tokens vs pass@1 Pareto (per-config)
   ============================================================ */
function ParetoChart() {
  const [xAxis, setXAxis] = useState("cost"); // cost | tokens

  const ref = useEcharts((theme, layout) => {
    const mobile = layout.isMobile;
    const axis = axisCommon(theme);
    const tooltip = tooltipCommon(theme);
    const axisLabel = { ...axis.axisLabel, fontSize: mobile ? 10 : 11, hideOverlap: true };
    const nameTextStyle = { ...axis.nameTextStyle, fontSize: mobile ? 10 : 11 };
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
      itemStyle: { color: a.m.color, opacity: 0.95, borderColor: theme.pointBorder, borderWidth: 1 },
      label: { show: !mobile, offset: mobile ? [0, 0] : labelOffset("pareto", a.m.id) },
    }));

    return {
      backgroundColor: "transparent",
      grid: mobile
        ? { left: 48, right: 10, top: 34, bottom: 60 }
        : { left: 62, right: 230, top: 48, bottom: 64 },
      xAxis: {
        ...axis,
        type: "value",
        name: xAxis === "cost"
          ? (mobile ? "Cost / trial (USD)" : "Mean cost per trial (USD)")
          : (mobile ? "Tokens / trial (M)" : "Mean tokens per trial (M)"),
        nameLocation: "middle",
        nameGap: mobile ? 30 : 32,
        nameTextStyle,
        axisLabel: {
          ...axisLabel,
          formatter: (v) => xAxis === "cost" ? "$" + v.toFixed(0) : v.toFixed(0) + "M",
        },
      },
      yAxis: {
        ...axis,
        type: "value",
        name: "Resolution rate (%)",
        nameLocation: "middle",
        nameGap: mobile ? 34 : 44,
        nameTextStyle,
        axisLabel: { ...axisLabel, formatter: (v) => v + "%" },
      },
      tooltip: {
        ...tooltip,
        trigger: "item",
        formatter: (p) => {
          if (!p.data || !p.data.a) return "";
          const a = p.data.a;
          return `<div style="font-family:JetBrains Mono;font-size:11px;color:${theme.ink3};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">${a.m.name}</div>
                  <div style="color:${theme.ink2};font-size:11px;margin-bottom:6px;">${a.m.scaffold}</div>
                  <div>Resolution rate: <b>${a.m.pass1.toFixed(1)}%</b></div>
                  <div>Cost / trial: <b>${a.m.costPerTrial != null ? "$" + a.m.costPerTrial.toFixed(2) : "—"}</b></div>
                  <div>Tokens / trial: <b>${a.m.avgTokensM.toFixed(1)}M</b></div>
                  <div>Logged trials: <b>${a.m.nLoggedTrials ?? "—"}</b></div>`;
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
          lineStyle: { color: theme.accent, type: "dashed", width: 1.5 },
          areaStyle: { color: theme.accent, opacity: 0.05 },
          z: 2,
          tooltip: { show: false },
        },
        {
          name: "agents",
          type: "scatter",
          data: scatterData,
          symbolSize: mobile ? 15 : 18,
          z: 5,
          label: {
            show: !mobile,
            position: "right",
            formatter: (p) => pointLabel(p.data.a.m),
            fontFamily: "JetBrains Mono, monospace",
            fontSize: 9,
            lineHeight: 12,
            color: theme.ink2,
            distance: 6,
            backgroundColor: theme.labelBg,
            padding: [1, 3],
          },
          emphasis: {
            scale: 1.3,
            label: {
              show: true,
              formatter: (p) => cfgLabel(p.data.a.m),
              fontSize: mobile ? 9 : 10,
              color: theme.ink,
              backgroundColor: theme.tooltipBg,
              borderColor: theme.rule,
              borderWidth: 1,
              padding: [2, 4],
            },
          },
          labelLayout: { hideOverlap: false },
        },
      ],
    };
  }, [xAxis]);

  return (
    <div className="anal-card">
      <div className="anal-card-head">
        <div>
          <div className="anal-card-no">FIG · PARETO</div>
        </div>
        <div className="anal-controls">
          <button className={"pill " + (xAxis === "cost" ? "active" : "")} onClick={() => setXAxis("cost")}>Cost ($)</button>
          <button className={"pill " + (xAxis === "tokens" ? "active" : "")} onClick={() => setXAxis("tokens")}>Tokens (M)</button>
        </div>
      </div>
      <div ref={ref} className="anal-chart anal-chart-pareto"></div>
    </div>
  );
}

/* ============================================================
   PLOT 3 — Reward-hacking incidence by model family
   ============================================================ */
function RewardHackingChart() {
  const ref = useEcharts((theme, layout) => {
    const mobile = layout.isMobile;
    const axis = axisCommon(theme);
    const tooltip = tooltipCommon(theme);
    const axisLabel = { ...axis.axisLabel, fontSize: mobile ? 10 : 11, hideOverlap: true };
    const nameTextStyle = { ...axis.nameTextStyle, fontSize: mobile ? 10 : 11 };
    const rows = RH_CLASSIFICATIONS
      .map((row) => ({
        ...row,
        hackedPct: row.hacked / row.n * 100,
        successPct: row.success / row.n * 100,
      }))
      .sort((a, b) => b.hackedPct - a.hackedPct);
    return {
      backgroundColor: "transparent",
      grid: mobile
        ? { left: 100, right: 40, top: 32, bottom: 44 }
        : { left: 190, right: 52, top: 42, bottom: 48 },
      xAxis: {
        ...axis,
        type: "value",
        name: mobile ? "Trials (%)" : "Share of trials (%)",
        nameLocation: "middle",
        nameGap: mobile ? 28 : 34,
        nameTextStyle,
        max: Math.ceil(Math.max(...rows.map((r) => r.hackedPct)) / 10) * 10,
        axisLabel: { ...axisLabel, formatter: (v) => v + "%" },
      },
      yAxis: {
        type: "category",
        inverse: true,
        data: rows.map((r) => mobile ? r.name : `${r.name} / ${r.harness}`),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: theme.ink2,
          fontFamily: "Inter, sans-serif",
          fontSize: mobile ? 10 : 11,
          lineHeight: 12,
        },
      },
      legend: {
        show: !mobile,
        top: 0,
        right: 0,
        itemWidth: 10,
        itemHeight: 10,
        textStyle: { color: theme.ink2, fontFamily: "JetBrains Mono, monospace", fontSize: 10 },
      },
      tooltip: {
        ...tooltip,
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (params) => {
          const row = rows[params[0].dataIndex];
          const criteria = Object.entries(row.criteria)
            .map(([name, count]) => `${name}: <b>${count}</b>`)
            .join("<br />") || "No failed reward-hacking criteria";
          return `<div style="font-family:JetBrains Mono;font-size:11px;color:${theme.ink3};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">${row.name}</div>
                  <div style="color:${theme.ink2};font-size:11px;margin-bottom:6px;">${row.harness}</div>
                  <div>Trials: <b>${row.n}</b></div>
                  <div>Classifier-positive: <b>${row.hacked}</b> (${row.hackedPct.toFixed(1)}%)</div>
                  <div>Earned reward after hacking: <b>${row.success}</b> (${row.successPct.toFixed(1)}%)</div>
                  <div style="margin-top:6px;color:${theme.ink2};">${criteria}</div>`;
        },
      },
      animation: false,
      series: [
        {
          name: "Reward-hacking classification",
          type: "bar",
          data: rows.map((r) => +r.hackedPct.toFixed(1)),
          barWidth: mobile ? 14 : 18,
          itemStyle: { color: theme.rewardHack, borderColor: theme.pointBorder, borderWidth: 0.8 },
          label: {
            show: true,
            position: "right",
            formatter: (p) => p.value > 0 ? `${p.value.toFixed(1)}%` : "0%",
            color: theme.ink2,
            fontFamily: "JetBrains Mono, monospace",
            fontSize: mobile ? 9 : 10,
          },
        },
      ],
    };
  }, []);

  return (
    <div className="anal-card">
      <div className="anal-card-head">
        <div>
          <div className="anal-card-no">FIG · REWARD HACKING</div>
        </div>
      </div>
      <div ref={ref} className="anal-chart anal-chart-reward"></div>
      <div className="anal-foot">
        Verifier-side reward-hacking classifications over {RH_CLASSIFIED_TOTALS.n} real agent runs:
        {" "}{RH_CLASSIFIED_TOTALS.hacked} were classifier-positive, and {RH_CLASSIFIED_TOTALS.success} earned reward after being classified as hacked.
      </div>
    </div>
  );
}

/* ============================================================
   ANALYSIS SECTION — wraps plots with tab nav
   ============================================================ */
function Analysis() {
  const [tab, setTab] = useState("pareto"); // pareto | horizon | rewardHack

  return (
    <section id="analysis">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span> 02 / analysis</div>
          <h2 className="section-title">Analysis</h2>
        </div>

        <div className="anal-tabs">
          <button className={"anal-tab " + (tab === "pareto" ? "active" : "")} onClick={() => setTab("pareto")}>
            <span className="anal-tab-no">01</span>
            <span className="anal-tab-t">Cost vs score</span>
            <span className="anal-tab-s">Pareto frontier</span>
          </button>
          <button className={"anal-tab " + (tab === "horizon" ? "active" : "")} onClick={() => setTab("horizon")}>
            <span className="anal-tab-no">02</span>
            <span className="anal-tab-t">Compute horizon</span>
            <span className="anal-tab-s">tokens vs resolution</span>
          </button>
          <button className={"anal-tab " + (tab === "rewardHack" ? "active" : "")} onClick={() => setTab("rewardHack")}>
            <span className="anal-tab-no">03</span>
            <span className="anal-tab-t">Reward hacking</span>
            <span className="anal-tab-s">bypass incidence</span>
          </button>
        </div>

        <div className="anal-stage">
          {tab === "pareto"  && <ParetoChart />}
          {tab === "horizon" && <ComputeHorizonChart />}
          {tab === "rewardHack" && <RewardHackingChart />}
        </div>
      </div>
    </section>
  );
}

export default Analysis;
