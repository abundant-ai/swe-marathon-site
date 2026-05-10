
import React, { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import {
  CASE_STUDIES,
  CATEGORIES,
  CAT_LABEL,
  HEADLINE,
  LEADERBOARD,
  PIPELINE,
  RH_BY_MODEL,
  TASKS,
} from "./data.js";

const Analysis = lazy(() => import("./analysis.jsx"));

const TASK_FAMILIES = CATEGORIES;

// Per-config tokens-per-trial vs pass@1 — both real numbers from the
// canonical sweep. Section §04's token-usage story.
function TokenUsageBars() {
  const rows = LEADERBOARD
    .filter((r) => !r.ref && r.tokAvg != null)
    .slice()
    .sort((a, b) => b.tokAvg - a.tokAvg);
  const maxTok = Math.max(...rows.map((r) => r.tokAvg));
  return (
    <div style={{
      border: "1px solid var(--rule)",
      background: "var(--bg)",
      padding: "16px 18px 14px",
      fontFamily: "var(--mono)",
      fontSize: 11,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 12 }}>
        <span>Mean tokens / trial · per (model, scaffold)</span>
        <span>pass@1 marker on right</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(200px, 240px) 1fr 60px", gap: 12, alignItems: "center", rowGap: 6 }}>
        {rows.map((r) => (
          <React.Fragment key={r.id}>
            <div style={{ fontFamily: "var(--sans)", fontSize: 12, color: "var(--ink-2)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {r.name} · <span style={{ color: "var(--ink-3)" }}>{r.scaffold}</span>
            </div>
            <div style={{ position: "relative", height: 14, background: "var(--rule2, #ebe6d7)" }}>
              <div style={{
                position: "absolute", left: 0, top: 0, bottom: 0,
                width: `${(r.tokAvg / maxTok) * 100}%`,
                background: r.highlight ? "var(--accent)" : "var(--ink)",
                opacity: r.highlight ? 0.9 : 0.55,
              }} />
              <span style={{ position: "absolute", right: 6, top: 0, bottom: 0, display: "flex", alignItems: "center", color: "var(--ink-2)", fontSize: 10 }}>
                {r.tokAvg.toFixed(1)}M
              </span>
            </div>
            <div style={{ textAlign: "right", color: r.highlight ? "var(--accent)" : "var(--ink)", fontWeight: 600 }}>
              {r.pass1.toFixed(1)}%
            </div>
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}



/* ---------------- COMPONENTS ---------------- */

// Pixel-art fox runner — Chrome-dino-style minigame.
// Two-tone sprite (silhouette + highlight) painted on a canvas, parallax scrolling
// landscape behind. Spacebar / click jumps. Cacti increase in speed.
function FoxRunner() {
  const canvasRef = useRef(null);
  const stateRef = useRef({
    started: false,
    over: false,
    fox: { y: 0, vy: 0, frame: 0 },
    obstacles: [], // {x, w, h, kind}
    scroll: 0,
    speed: 4.2,
    score: 0,
    best: 0,
    tick: 0
  });
  const [ui, setUi] = useState({ score: 0, best: 0, started: false, over: false });

  // Coyote sprite — three SVG frames (run-A, run-B, jump), rasterized to images.
  // Same silhouette as the brand-mark coyote but with a thicker torso + belly fill
  // so the body reads at game scale instead of looking like a slab.
  // viewBox is 32×22; body baseline (back) ≈ y8, belly ≈ y15, feet ≈ y21.
  // Body path: ears, back, snout, then BACK ALONG THE BELLY so the torso is filled.
  const COYOTE_BODY =
    // Top edge: tail → back → head/ears → snout
    "M2 11 Q5 10 7 12 L8 13 L10 11.5 L12.5 10.5 L15 10.8 L18 10.8 L20 10.5 L21 10 " +
    "L21.5 7 L22.5 9.5 L23.5 9.5 L24.5 7 L25 9.8 L26 10.5 L28.5 11.2 L30 12.4 " +
    "L28 13.2 L26.6 13.4 " +
    // Bottom edge: jaw under snout, neck, full belly back to tail
    "L26 14.6 L24 15.2 L22 15.4 " +
    "L20.5 15.4 L18 15.6 L15 15.6 L12 15.6 L9 15.4 L7 15.2 L5 14.5 L3.5 13.5 Z";

  // Four legs (front pair + back pair). Two frames swap stride.
  // Feet end at y=21 so they sit on the canvas baseline.
  const LEGS_A =
    "M22 15 L22.6 21 L20.6 21 L20 15.5 Z " +   // back-left
    "M19 15.5 L19.4 21 L17.5 21 L17 16 Z " +   // back-right
    "M13 15.5 L13.4 21 L11.5 21 L11 16 Z " +   // front-left
    "M9.5 15.2 L10 21 L8 21 L7.5 15.5 Z";       // front-right
  const LEGS_B =
    "M22 15 L23.6 20.4 L21.6 21 L20 15.5 Z " +
    "M19 15.5 L17.6 20.6 L15.8 21 L17 16 Z " +
    "M13 15.5 L14.6 21 L12.7 21 L11 16 Z " +
    "M9.5 15.2 L8 20.6 L6.2 21 L7.5 15.5 Z";
  // Jump: legs tucked, all four bent
  const LEGS_J =
    "M22 15 L24 17.5 L22.5 18.5 L20 16 Z " +
    "M19 15.5 L20.5 16.8 L19 17.8 L17 16.5 Z " +
    "M13 15.5 L11 16.8 L12 17.8 L14 16.5 Z " +
    "M9.5 15.2 L7 17 L7.8 18 L9.5 17 Z";

  const COL_BODY = "oklch(0.55 0.15 35)";
  const COL_BODY_DK = "oklch(0.42 0.13 35)";

  function buildSpriteSVG(legs) {
    // 32×22 viewBox: ears at y7, feet at y21, ~1 unit of breathing room.
    return (
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 22" width="208" height="143">' +
        '<g fill="' + COL_BODY + '">' +
          '<path d="' + COYOTE_BODY + '"/>' +
        '</g>' +
        '<g fill="' + COL_BODY_DK + '">' +
          '<path d="' + legs + '"/>' +
        '</g>' +
        '<circle cx="26.5" cy="11.6" r="0.55" fill="#1a1a17"/>' +
      '</svg>'
    );
  }
  const SPRITE_IMGS = useRef({ A: null, B: null, J: null, ready: false });
  useEffect(() => {
    let loaded = 0;
    const targets = { A: LEGS_A, B: LEGS_B, J: LEGS_J };
    Object.entries(targets).forEach(([k, legs]) => {
      const svg = buildSpriteSVG(legs);
      const img = new Image();
      img.onload = () => {
        SPRITE_IMGS.current[k] = img;
        loaded++;
        if (loaded === 3) SPRITE_IMGS.current.ready = true;
      };
      img.src = "data:image/svg+xml;utf8," + encodeURIComponent(svg);
    });
  }, []);

  // Draw size for the coyote on canvas (32:22 aspect)
  const COYOTE_W = 104, COYOTE_H = 71;

  // Canvas dimensions
  const CW = 880,CH = 200;
  const PIXEL = 4; // each sprite cell is 4x4 canvas pixels
  const GROUND_Y = CH - 36; // baseline for fox feet

  // Colors pulled from CSS variables (hardcoded fallback values)
  const COL_SILHOUETTE = "oklch(0.55 0.15 35)"; // accent
  const COL_HIGHLIGHT = "oklch(0.78 0.10 60)"; // lighter terracotta/cream
  const COL_GROUND = "#5a564a";
  const COL_TREE_FAR = "oklch(0.65 0.06 145)";
  const COL_TREE_NEAR = "oklch(0.45 0.10 145)";
  const COL_MTN = "oklch(0.62 0.04 50)";
  const COL_SKY = "oklch(0.94 0.025 50)";
  const COL_SKY_TOP = "oklch(0.96 0.02 60)";
  // Obstacle palettes — each kind has its own (base, dark) pair.
  //   warning  — amber, only used for the ⚠ triangle
  //   error    — red,   used for the X mark
  //   bug      — near-black, used for the insect
  const COL_WARN    = "oklch(0.74 0.17 75)";
  const COL_WARN_DK = "oklch(0.55 0.15 70)";
  const COL_ERROR   = "oklch(0.58 0.21 25)";
  const COL_ERROR_DK= "oklch(0.42 0.18 25)";
  const COL_BUG     = "oklch(0.28 0.04 30)";
  const COL_BUG_DK  = "oklch(0.18 0.03 30)";
  const COL_TEXT = "#494842";

  function drawCoyote(ctx, frameKey, x, y) {
    const img = SPRITE_IMGS.current[frameKey];
    if (!img) return;
    ctx.drawImage(img, x, y, COYOTE_W, COYOTE_H);
  }

  // Pixel-art obstacles — unmistakable error glyphs.
  // Two-tone: "S" = base color, "D" = darker shadow, "W" = white inner mark.
  function drawObstacleSprite(ctx, sp, x, y, base, dark) {
    for (let r = 0; r < sp.length; r++) {
      for (let c = 0; c < sp[r].length; c++) {
        const ch = sp[r][c];
        if (ch === ".") continue;
        let col;
        if (ch === "D") col = dark;
        else if (ch === "W") col = "oklch(0.96 0.015 60)";
        else col = base;
        ctx.fillStyle = col;
        ctx.fillRect(x + c * PIXEL, y + r * PIXEL, PIXEL, PIXEL);
      }
    }
  }
  function drawCactus(ctx, x, y, kind) {
    if (kind === 0) {
      // Warning triangle with !  — 9w x 9h cells (36w x 36h px)
      const sp = [
      "....SS....",
      "...SSSS...",
      "...SWWS...",
      "..SSWWSS..",
      "..SSWWSS..",
      ".SSSWWSSS.",
      ".SSSSSSSS.",
      "SSSSWWSSSS",
      "SSSSSSSSSS",
      "DDDDDDDDDD"];
      drawObstacleSprite(ctx, sp, x, y, COL_WARN, COL_WARN_DK);
    } else if (kind === 1) {
      // Bold X error mark — wide red X (10w x 8h, 40w x 32h px)
      const sp = [
      "SS......SS",
      "SSS....SSS",
      ".SSS..SSS.",
      "..SSSSSS..",
      "...SSSS...",
      "..SSSSSS..",
      ".SSS..SSS.",
      "SSS....SSS",
      "SS......SS",
      "DDDDDDDDDD"];
      drawObstacleSprite(ctx, sp, x, y, COL_ERROR, COL_ERROR_DK);
    } else {
      // Tall "BUG" — squat dark insect with legs (10w x 12h, 40w x 48h px)
      const sp = [
      ".S......S.",
      "..SS..SS..",
      "...SSSS...",
      "..SSWWSS..",
      ".SSSWWSSS.",
      "SSSSSSSSSS",
      "S.SSSSSS.S",
      "S.DSSSSD.S",
      "S..SSSS..S",
      "...SSSS...",
      "..S....S..",
      ".SS....SS."];
      drawObstacleSprite(ctx, sp, x, y, COL_BUG, COL_BUG_DK);
    }
  }

  // Background mountains — drawn as triangles
  function drawMountains(ctx, off) {
    ctx.fillStyle = COL_MTN;
    const peaks = [];
    for (let i = 0; i < 8; i++) peaks.push({ x: i * 140 + 60, h: 50 + i % 3 * 18 });
    for (const p of peaks) {
      const x = ((p.x - off * 0.18) % (CW + 200) + CW + 200) % (CW + 200) - 100;
      ctx.beginPath();
      ctx.moveTo(x, GROUND_Y);
      ctx.lineTo(x + 70, GROUND_Y - p.h);
      ctx.lineTo(x + 140, GROUND_Y);
      ctx.closePath();
      ctx.fill();
    }
  }

  // Background pines — silhouette triangles
  function drawPines(ctx, off, layer) {
    const speed = layer === "far" ? 0.45 : 1.0;
    const spacing = layer === "far" ? 80 : 130;
    const w = layer === "far" ? 16 : 26;
    const h = layer === "far" ? 24 : 42;
    ctx.fillStyle = layer === "far" ? COL_TREE_FAR : COL_TREE_NEAR;
    const yBase = layer === "far" ? GROUND_Y - 4 : GROUND_Y - 2;
    for (let i = -1; i < CW / spacing + 2; i++) {
      const x = ((i * spacing - off * speed) % (CW + spacing) + CW + spacing) % (CW + spacing) - spacing;
      ctx.beginPath();
      ctx.moveTo(x, yBase);
      ctx.lineTo(x + w / 2, yBase - h);
      ctx.lineTo(x + w, yBase);
      ctx.closePath();
      ctx.fill();
    }
  }

  function drawGround(ctx, off) {
    // Ground line
    ctx.strokeStyle = COL_GROUND;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, GROUND_Y + 4);
    ctx.lineTo(CW, GROUND_Y + 4);
    ctx.stroke();
    // Dashes ticking past
    ctx.fillStyle = COL_GROUND;
    for (let i = 0; i < CW / 30 + 2; i++) {
      const x = ((i * 30 - off) % (CW + 30) + CW + 30) % (CW + 30) - 30;
      ctx.fillRect(x, GROUND_Y + 9, 12, 1);
    }
  }

  // Game loop
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    let raf;
    function reset() {
      const s = stateRef.current;
      s.fox = { y: 0, vy: 0, frame: 0 };
      s.obstacles = [];
      s.scroll = 0;
      s.speed = 4.2;
      s.score = 0;
      s.over = false;
      s.tick = 0;
    }

    function jump() {
      const s = stateRef.current;
      if (s.over) {
        reset();
        s.started = true;
        setUi((u) => ({ ...u, over: false, started: true, score: 0 }));
        return;
      }
      if (!s.started) {
        s.started = true;
        setUi((u) => ({ ...u, started: true }));
      }
      // Only jump when on the ground
      if (s.fox.y >= -0.5) {
        s.fox.vy = -11.5;
      }
    }

    function step() {
      const s = stateRef.current;
      // Background scrolls always (idle-friendly)
      const bgSpeed = s.started && !s.over ? s.speed : 1.5;
      s.scroll += bgSpeed;

      // Physics
      if (s.started && !s.over) {
        s.tick++;
        // Gravity (tuned so peak airtime ≈ 47 frames → reach ≥200px at speed 5)
        s.fox.vy += 0.55;
        s.fox.y += s.fox.vy;
        if (s.fox.y > 0) {s.fox.y = 0;s.fox.vy = 0;}

        // Spawn obstacles — randomized cadence, not on a strict timer
        const last = s.obstacles[s.obstacles.length - 1];
        if (!last && s.tick > 60) {
          // first obstacle: give the player a beat to settle
          s.obstacles.push({ x: CW + 10, kind: Math.floor(Math.random() * 3) });
        } else if (last) {
          const minGap = 260 + s.speed * 14;          // grows with speed (need more space)
          const variance = 80 + Math.random() * 380;  // wide jitter so cadence isn't metronomic
          if (last.x < CW - minGap - variance) {
            // 8% chance of a tight double; otherwise normal
            const tight = Math.random() < 0.08;
            const x0 = CW + (tight ? 6 : 10);
            const kind = Math.floor(Math.random() * 3);
            s.obstacles.push({ x: x0, kind });
          }
        }

        // Move + collision
        const foxX = 80;
        const foxRect = {
          x: foxX + 14, y: GROUND_Y - COYOTE_H + 3 + s.fox.y + 14,
          w: COYOTE_W - 28, h: COYOTE_H - 24
        };
        for (const o of s.obstacles) {
          o.x -= s.speed;
          // Forgiving hitbox — only the upper-middle of the obstacle kills.
          // Cuts ~30% off each axis so visuals look threatening but the actual
          // contact zone is smaller than the sprite.
          const ow = 40, oh = o.kind === 2 ? 48 : 40;
          const hbW = ow - 18;     // shave 9px each side
          const hbH = oh - 18;     // shave 18px off the top (legs/lower-half are forgiving)
          const ox = o.x + 9, oy = GROUND_Y - oh + 14;
          if (foxRect.x < ox + hbW && foxRect.x + foxRect.w > ox &&
              foxRect.y < oy + hbH && foxRect.y + foxRect.h > oy) {
            s.over = true;
            if (s.score > s.best) s.best = s.score;
            setUi((u) => ({ ...u, over: true, best: s.best, score: s.score }));
          }
        }
        // Cull
        s.obstacles = s.obstacles.filter((o) => o.x > -50);

        // Score: output tokens (Mtok) — 1 frame ≈ a chunk of generation
        s.score += s.speed * 0.010;
        // Speed ramps — gentler curve, capped lower so jumps stay reachable
        s.speed = Math.min(6.8, 4.2 + s.score / 22);
        if (s.tick % 12 === 0) setUi((u) => ({ ...u, score: s.score }));

        // Animate fox running gait
        s.fox.frame = Math.floor(s.tick / 6) % 2;
      }

      // ---- Render ----
      // Sky gradient
      const grad = ctx.createLinearGradient(0, 0, 0, CH);
      grad.addColorStop(0, COL_SKY_TOP);
      grad.addColorStop(1, COL_SKY);
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, CW, CH);

      drawMountains(ctx, s.scroll);
      drawPines(ctx, s.scroll, "far");
      drawPines(ctx, s.scroll, "near");
      drawGround(ctx, s.scroll);

      // Coyote — sprite y=21/22 puts feet ~67.7px from sprite top; nudge so they kiss the baseline.
      const foxX = 80;
      const foxY = GROUND_Y - COYOTE_H + 3 + s.fox.y;
      let frameKey;
      if (s.fox.y < -2) frameKey = "J";
      else if (!s.started) frameKey = "A";
      else frameKey = s.fox.frame === 0 ? "A" : "B";
      drawCoyote(ctx, frameKey, foxX, foxY);

      // Obstacles
      for (const o of s.obstacles) {
        const oh = o.kind === 2 ? 48 : 40;
        drawCactus(ctx, o.x, GROUND_Y - oh, o.kind);
      }

      // Score badge — output tokens generated (top-right)
      ctx.fillStyle = COL_TEXT;
      ctx.font = '500 12px ui-monospace, "JetBrains Mono", monospace';
      ctx.textAlign = "right";
      const tok = s.score.toFixed(2);
      const best = s.best.toFixed(2);
      ctx.fillText("CONTEXT  " + tok + " Mtok   BEST  " + best + " Mtok", CW - 16, 22);

      // Idle / over overlays
      if (!s.started) {
        ctx.fillStyle = "rgba(250, 247, 240, 0.85)";
        ctx.fillRect(CW / 2 - 180, CH / 2 - 30, 360, 60);
        ctx.strokeStyle = "rgba(26,26,23,0.25)";
        ctx.strokeRect(CW / 2 - 180, CH / 2 - 30, 360, 60);
        ctx.fillStyle = COL_TEXT;
        ctx.textAlign = "center";
        ctx.font = '600 13px ui-monospace, "JetBrains Mono", monospace';
        ctx.fillText("PRESS SPACE OR CLICK TO RUN", CW / 2, CH / 2 + 5);
      } else if (s.over) {
        ctx.fillStyle = "rgba(250, 247, 240, 0.92)";
        ctx.fillRect(CW / 2 - 220, CH / 2 - 40, 440, 80);
        ctx.strokeStyle = COL_SILHOUETTE;
        ctx.lineWidth = 1;
        ctx.strokeRect(CW / 2 - 220, CH / 2 - 40, 440, 80);
        ctx.fillStyle = COL_SILHOUETTE;
        ctx.textAlign = "center";
        ctx.font = '700 16px Georgia, serif';
        // Cycle thru a few flavor messages so it doesn't always say the same thing
        const dnfMsgs = [
          "Lost coherence at " + s.score.toFixed(2) + " Mtok",
          "Reward-hacked into a bug",
          "Tripped on an assertion"
        ];
        const msg = dnfMsgs[Math.floor(s.best * 7 + s.score) % dnfMsgs.length];
        ctx.fillText(msg, CW / 2, CH / 2 - 8);
        ctx.fillStyle = COL_TEXT;
        ctx.font = '500 12px ui-monospace, monospace';
        ctx.fillText("press space / click to retry", CW / 2, CH / 2 + 16);
      }

      raf = requestAnimationFrame(step);
    }
    raf = requestAnimationFrame(step);

    function onKey(e) {
      if (e.code === "Space" || e.code === "ArrowUp") {
        // Only intercept if fox is in viewport (avoid hijacking page scroll)
        const rect = canvas.getBoundingClientRect();
        if (rect.bottom > 0 && rect.top < window.innerHeight) {
          e.preventDefault();
          jump();
        }
      }
    }
    function onClick() {jump();}
    window.addEventListener("keydown", onKey);
    canvas.addEventListener("click", onClick);
    canvas.addEventListener("touchstart", (e) => {e.preventDefault();jump();}, { passive: false });

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  return (
    <div className="fox-runner">
      <canvas
        ref={canvasRef}
        width={880}
        height={200}
        className="fox-canvas"
        tabIndex={0}
        aria-label="Pixel-art coyote runner mini-game. Press space to jump." />
      
      <div className="fox-runner-caption">
        <span className="fr-key">SPACE</span> to jump · dodge errors · keep the coyote coherent across Mtok
      </div>
    </div>);

}

function StatStrip() {
  const stats = [
  { num: String(HEADLINE.nTasks), unit: "", label: "Long-horizon tasks" },
  { num: `${HEADLINE.agentBudgetMinH}–${HEADLINE.agentBudgetMaxH}`, unit: "h", label: "Agent budget" },
  { num: `<${Math.ceil(HEADLINE.bestPass1Pct)}`, unit: "%", label: "Best pass@1" },
  { num: HEADLINE.nTrials.toLocaleString(), unit: "", label: "Logged trials" }];

  return (
    <div className="stats-strip">
      {stats.map((s, i) =>
      <div className="stat" key={i}>
          <div className="stat-num">{s.num}<span className="unit">{s.unit}</span></div>
          <div className="stat-label">{s.label}</div>
        </div>
      )}
    </div>);

}

function Hero() {
  return (
    <header className="hero">
      <div className="container">
        <div className="eyebrow">SWE-MARATHON · 20 LONG-HORIZON TASKS · 1,100 LOGGED TRIALS</div>
        <h1 className="title">
          Can agents autonomously complete<br />
          <span className="ital">ultra-long-horizon</span> software work?
        </h1>
        <p className="lede">
          <strong>SWE-Marathon</strong> is a benchmark of <strong>20 multi-hour</strong> software-engineering
          tasks: library reproductions, full-stack product clones, ML engineering, and algorithmic
          optimisation. Each task ships an executable environment, a held-out reference solution,
          and a multi-channel verifier — agent budgets of {HEADLINE.agentBudgetMinH}–{HEADLINE.agentBudgetMaxH} hours against
          expert estimates of {HEADLINE.humanEstMinH}–{HEADLINE.humanEstMaxH} hours.
        </p>
        <p className="hero-sub">
          Across {HEADLINE.nTrials.toLocaleString()} real-agent rollouts averaging {HEADLINE.avgTokensPerTrialM}M tokens each,
          no evaluated configuration exceeds {Math.ceil(HEADLINE.bestPass1Pct)}% pass@1.
          {" "}{HEADLINE.rhAttemptPct}% of trajectories contain at least one exploit-shaped action;
          {" "}{HEADLINE.rhSuccessPct}% earn reward despite shipping the exploit.
        </p>
        <FoxRunner />
        <div className="cta-row">
          <a className="btn" href="#leaderboard">View leaderboard <span className="arr">↓</span></a>
          <a className="btn ghost" href="#about">Method <span className="arr">↓</span></a>
          <a className="btn ghost" href="https://github.com/abundant-ai/long-horizon">GitHub ↗</a>
        </div>
        <StatStrip />
      </div>
    </header>);

}

function Leaderboard() {
  const [fam, setFam] = useState("all");
  const [view, setView] = useState("summary"); // summary | full

  const scoreFor = (row, key) => {
    if (key === "all") return row.pass1 ?? 0;
    return row.perCat?.[key] ?? 0;
  };

  const sorted = useMemo(() => {
    return [...LEADERBOARD].sort((a, b) => {
      if (a.ref && !b.ref) return 1;
      if (b.ref && !a.ref) return -1;
      return scoreFor(b, fam) - scoreFor(a, fam);
    });
  }, [fam]);

  const allCatCols = ["library", "clone", "ml", "algo"];
  const cols = fam === "all" ? allCatCols : [fam];

  return (
    <section id="leaderboard">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§01 / Leaderboard</div>
          <h2 className="section-title">Pass@1 <span className="ital">across 20 long-horizon tasks.</span></h2>
        </div>

        <div className="lb-controls">
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
            <span className="lb-label">Filter</span>
            {TASK_FAMILIES.map((f) =>
            <button
              key={f.id}
              className={"pill " + (fam === f.id ? "active" : "")}
              onClick={() => setFam(f.id)}>
              {f.label}</button>
            )}
          </div>
          <span style={{ flex: 1 }}></span>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
            <span className="lb-label">View</span>
            <button className={"pill " + (view === "summary" ? "active" : "")} onClick={() => setView("summary")}>Summary</button>
            <button className={"pill " + (view === "full" ? "active" : "")} onClick={() => setView("full")}>All families</button>
          </div>
        </div>

        <div style={{ overflowX: "auto" }}>
        <table className="lb">
          <thead>
            <tr>
              <th style={{ width: 36 }}>#</th>
              <th>Agent · Scaffold</th>
              <th className="num">Pass@1</th>
              {(view === "full" ? allCatCols : cols).map((c) =>
                <th key={c} className="num">{CAT_LABEL[c]}</th>
                )}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
                const showCols = view === "full" ? allCatCols : cols;
                return (
                  <React.Fragment key={`${row.rank}-${row.name}-${row.scaffold}`}>
                  <tr className={row.highlight ? "highlight" : ""}>
                    <td>
                      <span className={"rank-badge " + (row.rank === 1 ? "rank-1 " : "") + (row.ref ? "rank-ref" : "")}>
                        {row.rank}
                      </span>
                    </td>
                    <td>
                      <div className="agent-name">{row.name}</div>
                      <div className="scaffold" style={{ fontSize: 11, color: "var(--ink-3)", marginTop: 2 }}>{row.scaffold}</div>
                    </td>
                    <td className="num score-bar-cell">
                      <span className={"score-bar " + (row.ref ? "ref" : "")}
                        style={{ width: `${Math.min(100, row.pass1 / 25 * 100)}%` }}></span>
                      <span className="num-on-bar">{row.pass1.toFixed(1)}</span>
                    </td>
                    {showCols.map((c) =>
                      <td key={c} className="num">{row.perCat?.[c] != null ? row.perCat[c].toFixed(1) : "—"}</td>
                      )}
                  </tr>
                </React.Fragment>);

              })}
          </tbody>
        </table>
        </div>

        <div className="footnotes">
          <div><sup>1</sup>Pass@1 = mean over 5 independent rollouts per (agent, model, task); a trial counts resolved when reward = 1.0 from the multi-channel verifier. n = 100 trials per non-reference row (5 trials × 20 tasks); n = 99 / 96 where infrastructure-failed trials were excluded from the canonical grid.</div>
          <div><sup>2</sup>Reference rows are not directly comparable: <i>Oracle</i> is the held-out maintainer solution; <i>NOP</i> is a no-action baseline.</div>
          <div><sup>3</sup>Sandbox: Modal via Harbor, with FrontierSWE-style egress controls on the four offline tasks. Agent budgets range from 2 to 10 hours per task.</div>
        </div>
      </div>
    </section>);

}

function Tasks() {
  return (
    <section id="tasks">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§03 / Tasks</div>
          <h2 className="section-title">Four families. Twenty <span className="ital">marathons.</span></h2>
        </div>

        <div className="section-body" style={{ marginBottom: 28 }}>
          <div className="sb-side">
            <div className="label-row">Sources<span>Hand-curated real OSS / research code; 11 unique contributors authored the 20 accepted tasks.</span></div>
            <div className="label-row">Budget<span>{HEADLINE.agentBudgetMinH}–{HEADLINE.agentBudgetMaxH}h agent · {HEADLINE.humanEstMinH}–{HEADLINE.humanEstMaxH}h expert estimate.</span></div>
            <div className="label-row">Submission<span>Container state at submit time, graded by the multi-channel verifier.</span></div>
          </div>
          <div>
            <p style={{ fontSize: 16, color: "var(--ink-2)", margin: 0, maxWidth: 600 }}>
              Each task ships a Dockerized starter, an instruction file specifying
              outcomes (not algorithms), a held-out human-written reference solution,
              and a multi-layer verifier. Tasks are accepted only if NOP fails,
              Oracle passes, and the adversarial cheat sweep finds no shortcut —
              {" "}{HEADLINE.nVerifierFamilies} verifier families across the suite,
              spanning {HEADLINE.languages.join(", ")}.
            </p>
          </div>
        </div>

        <div className="tasks-grid">
          {TASKS.map((t, i) =>
          <div className="task" key={t.id}>
              <div className="task-head">
                <div className="task-id">T{String(i + 1).padStart(2, "0")} · {CAT_LABEL[t.cat]}</div>
                <div className="task-budget">{t.humanH}h human · {t.agentH}h agent</div>
              </div>
              <h3 className="task-title">{t.title}</h3>
              <p className="task-desc">{t.desc}</p>
              <div className="task-meta">
                <div><span className="k">repo</span><span className="v">{t.id}</span></div>
                <div><span className="k">verifier</span><span className="v">{t.verifier}</span></div>
                <div><span className="k">pass@1</span><span className="v">{t.pass1.toFixed(1)}% · n=55</span></div>
                {t.exploit > 0 && (
                  <div><span className="k">exploit</span><span className="v">{t.exploit.toFixed(1)}% attempts · {t.succ} succ.</span></div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </section>);

}

function CourseProfileSection() {
  return (
    <section id="course">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§04 / The course</div>
          <h2 className="section-title">Multi-hour rollouts, <span className="ital">millions of tokens.</span></h2>
        </div>
        <div className="section-body" style={{ marginBottom: 28 }}>
          <div className="sb-side">
            <div className="label-row">Budget<span>2–10h agent wall-clock, set per task to reflect difficulty.</span></div>
            <div className="label-row">Tokens<span>Median 7.6M (input + output) per trial; right tail past 870M.</span></div>
            <div className="label-row">Compaction<span>Tracks failure rather than rescue: 0 / 71 reward-bearing terminus-2 summariser trials pass.</span></div>
          </div>
          <div>
            <p style={{ fontSize: 15, color: "var(--ink-2)", margin: "0 0 18px", maxWidth: 600 }}>
              SWE-Marathon trials run for hours. Cumulative input across API
              calls reaches millions to hundreds of millions of tokens — far
              past what any single context window holds. Holding the model
              fixed and varying the scaffold moves median tokens-per-trial
              by up to 12×.
            </p>
            <TokenUsageBars />
            <div className="split-chips" style={{ marginTop: 18 }}>
              <div className="split-chip">Median tokens / trial: <strong>{HEADLINE.medianTokensPerTrialM}M</strong></div>
              <div className="split-chip">Mean tokens / trial: <strong>{HEADLINE.avgTokensPerTrialM}M</strong></div>
              <div className="split-chip">Right-tail max: <strong>{HEADLINE.maxTokensPerTrialM}M</strong></div>
              <div className="split-chip">Best pass@1: <strong>{HEADLINE.bestPass1Pct}%</strong> ({HEADLINE.bestPass1Label})</div>
            </div>
          </div>
        </div>
      </div>
    </section>);

}

function About() {
  return (
    <section id="about">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§05 / Method</div>
          <h2 className="section-title">A <span className="ital">marathon</span>, not a sprint.</h2>
        </div>

        <div className="section-body">
          <div className="sb-side">
            <div className="label-row">Position<span>Project-scale tasks whose difficulty comes from sustained engineering work, not patch localisation — well past SWE-Bench's per-issue scope and Terminal-Bench's per-session scope.</span></div>
            <div className="label-row">Sandbox<span>Modal sandboxes through Harbor. 1–8 vCPU, 8–32 GB RAM, GPU on the four ML tasks.</span></div>
            <div className="label-row">Verifiers<span>{HEADLINE.nVerifierFamilies} families: dense unit tests, behavioural parity, performance gates, deterministic replay, integrity / audit, and computer-use UI/UX judges.</span></div>
          </div>
          <div>
            <p style={{ fontSize: 18, color: "var(--ink)", margin: "0 0 18px", maxWidth: 620, lineHeight: 1.5, fontFamily: "var(--serif)" }}>
              Most coding benchmarks ask: can the model write a function?
              SWE-Marathon asks whether agents can sustain coherent engineering
              work over multi-hour rollouts and millions of tokens.
            </p>
            <p style={{ maxWidth: 620, color: "var(--ink-2)" }}>
              Tasks ship a Dockerized starter, an instruction file specifying
              outcomes (not algorithms), a held-out human-written reference solution,
              and a multi-channel verifier. Tasks are accepted only if NOP fails,
              Oracle passes, frontier scaffolds struggle, and the adversarial cheat
              sweep finds no shortcut. Final scoring uses container state at
              submit-time; for tasks with both shell and UX stages, trial reward is
              the minimum across stages so a UI regression floors the score even
              when every deterministic gate passes.
            </p>
          </div>
        </div>

        <h4 style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.12em", marginTop: 40, marginBottom: 18 }}>The pipeline · five stages</h4>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {PIPELINE.map((p) => (
            <div key={p.num} style={{
              display: "grid",
              gridTemplateColumns: "112px 1fr",
              border: "1px solid var(--rule)",
              background: "var(--bg)",
            }}>
              <div style={{
                background: "var(--ink)",
                color: "var(--bg)",
                padding: "14px 16px",
                borderRight: "2px solid var(--accent)",
                display: "flex",
                flexDirection: "column",
                justifyContent: "space-between",
              }}>
                <div style={{ fontFamily: "var(--mono)", fontWeight: 700, fontSize: 22, letterSpacing: "-0.02em" }}>{p.num}</div>
                <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "rgba(250,247,240,0.6)", textTransform: "uppercase", letterSpacing: "0.12em" }}>STAGE</div>
              </div>
              <div style={{ padding: "14px 18px 16px" }}>
                <div style={{ fontFamily: "var(--serif)", fontSize: 19, lineHeight: 1.15, marginBottom: 6 }}>{p.t}</div>
                <div style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.55 }}>{p.d}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>);

}

function Findings() {
  return (
    <section id="findings">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§06 / Observations</div>
          <h2 className="section-title">Where agents <span className="ital">cheat</span>, drift, and stall.</h2>
        </div>

        <div className="findings">
          <div className="finding">
            <div className="find-num">— 01 / Reward hacking is endemic at long horizons</div>
            <h3 className="find-h">{HEADLINE.rhAttemptPct}% of trajectories show exploit-shaped action; {HEADLINE.rhSuccessPct}% earn reward despite shipping the cheat.</h3>
            <p className="find-body">
              The full audit covers all {HEADLINE.nTrials.toLocaleString()} canonical-grid rollouts.
              {" "}{HEADLINE.rhExploitPct}% ship a clear verifier bypass; {HEADLINE.rhSuccessN} earn live reward
              despite the exploit. Per-model exploit rates span <strong>{HEADLINE.rhSpreadX}×</strong> between
              {" "}{HEADLINE.rhMinModel.name} ({HEADLINE.rhMinModel.pct}%) and {HEADLINE.rhMaxModel.name}
              {" "}({HEADLINE.rhMaxModel.pct}%). {HEADLINE.rhTopTwoModelsShareOfSuccess} of {HEADLINE.rhSuccessN}
              {" "}successful exploits come from just two models.
            </p>
          </div>

          <div className="finding">
            <div className="find-num">— 02 / Failures concentrate in two buckets</div>
            <h3 className="find-h">73% of agent-attributable failures are Implementation Failure or Timeout.</h3>
            <p className="find-body">
              Across {HEADLINE.failureTotalAttributable} agent-attributable failed trials, Implementation Failure (41.6%)
              and Timeout (31.4%) dominate. Reward Hacking is the third bucket at 15.4% — concentrated in a
              few task / configuration combinations: Codex on GPT-5.5 hits 24% reward-hacking among its failures;
              Terminus on GPT-5.5 reaches <strong>57%</strong> (24 of 42 failures) — the dominant locus of in-trial gaming.
            </p>
          </div>

          <div className="finding">
            <div className="find-num">— 03 / Validation weakness is universal</div>
            <h3 className="find-h">{HEADLINE.validationSignalPct}% of failed trials carry a validation-failure signal.</h3>
            <p className="find-body">
              Almost every agent-attributable failure exposes some local-validation gap that better testing would
              have surfaced. Compaction tracks failure rather than rescue: {HEADLINE.compactionPassWithSummariser} reward-bearing
              terminus-2 summariser trials pass, against {HEADLINE.compactionPassWithoutPct}% without. The implication: local-testing tooling
              improvements would lift the headline numbers across <em>all five</em> failure buckets, not just the
              Poor Self-Verification slice.
            </p>
          </div>

          <div className="finding">
            <div className="find-num">— 04 / Scaffold &gt; model on token use</div>
            <h3 className="find-h">Holding the model fixed and varying the scaffold moves median tokens-per-trial by up to 12×.</h3>
            <p className="find-body">
              GPT-5.5 runs at 0.40M median tokens under terminus-2 versus 4.8M under codex; Claude Opus 4.7 runs
              at 4.4M under terminus-2 versus 21.9M under claude-code. Silent duplication compounds: terminus-2's
              tool calls repeat an earlier (function, args) pair {HEADLINE.duplicationTerminusPct}% of the time, claude-code
              {" "}{HEADLINE.duplicationClaudeCodePct}%. Tool error rate sits at {HEADLINE.toolErrorRateRange} across scaffolds — the cost of long horizons
              is paid in repetition tax, not just headline difficulty.
            </p>
          </div>
        </div>

        <div style={{ marginTop: 48 }}>
          <h4 style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 14 }}>Selected case studies — one per failure bucket</h4>

          {CASE_STUDIES.map((c) => (
            <div className="trace" key={c.trial}>
              <div className="tr-head">{c.bucket} · {c.trial} · {c.config}</div>
              <div className="tr-quote">{c.pattern}</div>
            </div>
          ))}
        </div>
      </div>
    </section>);

}

function Team() {
  return (
    <section id="team">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§07 / Team</div>
          <h2 className="section-title">Built by a small <span className="ital">cross-lab</span> group.</h2>
        </div>
        <div style={{ maxWidth: 620, color: "var(--ink-2)", fontSize: 16, lineHeight: 1.55 }}>
          The 20 accepted tasks were authored by 11 unique contributors —
          software engineers familiar with the systems each task targets.
          Author and affiliation list is held back during the double-blind
          review window and will be added when the paper de-anonymises.
        </div>
      </div>
    </section>);

}

function Citation() {
  const bib = `@misc{swemarathon_2026,
  title        = {SWE-Marathon: Long-Horizon Software Engineering for Agents},
  author       = {{SWE-Marathon Authors}},
  year         = {2026},
  howpublished = {\\url{https://github.com/abundant-ai/long-horizon}},
  note         = {Benchmark and evaluation code; preprint forthcoming.}
}`;
  const [copied, setCopied] = useState(false);
  return (
    <section id="cite">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§08 / Paper</div>
          <h2 className="section-title">If SWE-Marathon is useful,<br />please <span className="ital">cite us.</span></h2>
        </div>
        <p style={{ maxWidth: 620, color: "var(--ink-2)", margin: "0 0 18px", fontSize: 15, lineHeight: 1.55 }}>
          Paper currently in submission; an arXiv preprint will follow shortly.
          Until then, please cite the benchmark via the entry below — we'll
          update the title, authors, and a canonical <code>@article</code>
          entry once the preprint is posted.
        </p>
        <div className="citation-block">
          <button className="copy-btn" onClick={() => {
            navigator.clipboard?.writeText(bib);
            setCopied(true); setTimeout(() => setCopied(false), 1500);
          }}>{copied ? "Copied" : "Copy"}</button>
          {bib}
        </div>
      </div>
    </section>);

}


function Footer() {
  return (
    <footer>
      <div className="container">
        <div className="foot-grid">
          <div>
            <div className="brand" style={{ marginBottom: 14 }}>
              <div className="brand-mark" aria-label="SWE-Marathon coyote mascot">
                <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                  <path d="M2 11 Q5 10 7 12 L8 13.5 L10 12 L12.5 11 L15 11.5 L18 11.5 L20 11 L21 10.5 L21.5 8 L22.5 10 L23.5 10 L24.5 8 L25 10.5 L26 11 L28.5 11.8 L30 12.8 L28 13.5 L26.5 13.6 L26.5 14.4 L25 15 L23 14.8 L22 14.5 L22 21 L20.5 21 L20.5 16 L18 16 L18 21 L16.5 21 L16.5 16 L13 16 L13 21 L11.5 21 L11.5 16 L9 16 L8 21 L6.5 21 L7 15 L5 14 L3.5 13 Z" fill="currentColor" />
                  <circle cx="26.5" cy="11.8" r="0.55" fill="#1a1a17" />
                </svg>
              </div>
              <span>SWE-Marathon</span>
            </div>
            <p style={{ maxWidth: 380, color: "var(--ink-2)", fontSize: 13, margin: 0 }}>
              A long-horizon software engineering benchmark. Open-source under
              MIT. We welcome new tasks, new agents, and new judges.
            </p>
          </div>
          <div>
            <div className="foot-h">Project</div>
            <div className="foot-list">
              <a href="#leaderboard">Leaderboard</a>
              <a href="#tasks">Tasks</a>
              <a href="#about">Method</a>
              <a href="#findings">Observations</a>
            </div>
          </div>
          <div>
            <div className="foot-h">Resources</div>
            <div className="foot-list">
              <a href="https://github.com/abundant-ai/long-horizon">GitHub ↗</a>
              <a href="#cite">Paper</a>
              <a href="#leaderboard">Submit an agent</a>
              <a href="#tasks">Donate a task</a>
            </div>
          </div>
        </div>
        <div className="foot-meta">
          <div>SWE-Marathon · v1.0</div>
          <div>MIT License</div>
        </div>
      </div>
    </footer>);

}

function App() {
  return (
    <>
      <Hero />
      <Leaderboard />
      <Suspense fallback={<div className="analysis-loading">Loading analysis...</div>}>
        <Analysis />
      </Suspense>
      <Tasks />
      <CourseProfileSection />
      <About />
      <Findings />
      <Team />
      <Citation />
      <Footer />
    </>);

}

export default App;
