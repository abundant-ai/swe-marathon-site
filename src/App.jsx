
import React, { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import {
  CASE_STUDIES,
  CATEGORIES,
  CAT_LABEL,
  HEADLINE,
  LEADERBOARD,
  PIPELINE,
  RH_BY_MODEL,
  SLACK_TRIAL_BY_ID,
  TASK_DETAILS,
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
          <h2 className="section-title">Pass@1 across 20 long-horizon tasks.</h2>
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
                      <span className="agent-name">{row.name}</span>
                      <span className="scaffold" style={{ fontSize: 11, color: "var(--ink-3)", marginLeft: 8 }}>{row.scaffold}</span>
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
          <h2 className="section-title">Four families. Twenty marathons.</h2>
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
          <a className={"task task-link " + (TASK_DETAILS[t.id] ? "has-detail" : "")} href={`#task/${t.id}`} key={t.id}>
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
                <div><span className="k">open</span><span className="v">{TASK_DETAILS[t.id] ? "task page" : "overview"}</span></div>
              </div>
            </a>
          )}
        </div>
      </div>
    </section>);

}

/* ---------------- Trajectory viewer (DeepSWE-inspired) ---------------- */

// Per-tool-kind presentation metadata. Colors are drawn from the site palette
// so the trajectory reads as part of the same visual language.
const TOOL_META = {
  Bash:       { label: "Bash",     glyph: "$",  color: "#1a1a17" },
  Write:      { label: "Write",    glyph: "✎",  color: "oklch(0.50 0.10 145)" },
  Edit:       { label: "Edit",     glyph: "±",  color: "oklch(0.55 0.13 70)" },
  Read:       { label: "Read",     glyph: "▤",  color: "#6b8da3" },
  Grep:       { label: "Grep",     glyph: "⌕",  color: "#5a6cb8" },
  TaskCreate: { label: "Task +",   glyph: "◆",  color: "oklch(0.55 0.15 35)" },
  TaskUpdate: { label: "Task ·",   glyph: "◇",  color: "#a86237" },
  ToolSearch: { label: "Search",   glyph: "≋",  color: "#7a83b3" },
  Submit:     { label: "Submit",   glyph: "✓",  color: "oklch(0.52 0.12 150)" },
};
const DEFAULT_TOOL_META = { label: "Tool", glyph: "•", color: "#84827a" };
const toolMeta = (kind) => TOOL_META[kind] || DEFAULT_TOOL_META;

// The logged `detail` looks like:
//   "Agent message:\n<text>\n\nTool arguments:\n{ ...json... }"
// Split it into a human header and the parsed tool arguments so we can render
// commands, file writes, and edits in a structured way instead of raw JSON.
function parseDetail(detail) {
  const marker = "Tool arguments:";
  const idx = detail ? detail.indexOf(marker) : -1;
  if (idx === -1) return { header: detail || "", args: null, rawArgs: "" };
  const header = detail.slice(0, idx).replace(/^Agent message:\s*/i, "").trim();
  const rawArgs = detail.slice(idx + marker.length).trim();
  let args = null;
  try { args = JSON.parse(rawArgs); } catch { args = null; }
  return { header, args, rawArgs };
}

// Render the parsed arguments for a single step in a tool-aware way.
function StepBody({ kind, detail }) {
  const { args, rawArgs } = parseDetail(detail);

  if (args && typeof args.command === "string") {
    return (
      <div className="step-body">
        {args.description && <div className="step-desc">{args.description}</div>}
        <div className="code-block term">
          <div className="code-block-tag">shell</div>
          <pre>{args.command}</pre>
        </div>
      </div>
    );
  }
  if (args && typeof args.content === "string") {
    return (
      <div className="step-body">
        <div className="code-block">
          <div className="code-block-tag">write · {args.file_path || "file"}</div>
          <pre>{args.content || "(empty file)"}</pre>
        </div>
      </div>
    );
  }
  if (args && (typeof args.old_string === "string" || typeof args.new_string === "string")) {
    return (
      <div className="step-body">
        {args.file_path && <div className="step-desc mono">{args.file_path}</div>}
        <div className="code-block diff">
          <div className="code-block-tag">− removed</div>
          <pre className="diff-old">{args.old_string || ""}</pre>
        </div>
        <div className="code-block diff">
          <div className="code-block-tag">+ added</div>
          <pre className="diff-new">{args.new_string || ""}</pre>
        </div>
      </div>
    );
  }
  if (args && (args.subject || args.description)) {
    return (
      <div className="step-body">
        {args.subject && <div className="step-desc"><b>{args.subject}</b></div>}
        {args.description && <div className="step-desc">{args.description}</div>}
      </div>
    );
  }
  if (args && (args.pattern || args.query)) {
    return (
      <div className="step-body">
        <div className="code-block term">
          <div className="code-block-tag">{args.pattern ? "grep" : "search"}</div>
          <pre>{args.pattern || args.query}</pre>
        </div>
      </div>
    );
  }
  if (args && args.file_path) {
    return (
      <div className="step-body">
        <div className="step-desc mono">{args.file_path}</div>
      </div>
    );
  }
  return (
    <div className="step-body">
      <pre className="code-block-raw">{rawArgs || detail}</pre>
    </div>
  );
}

function TrajectoryExplorer({ trial, label, rows, status }) {
  const [open, setOpen] = useState(() => new Set());
  const [mutedKinds, setMutedKinds] = useState(() => new Set());
  const stepRefs = useRef({});
  const listRef = useRef(null);

  // Reset interaction state when switching trials.
  useEffect(() => {
    setOpen(new Set());
    setMutedKinds(new Set());
  }, [trial]);

  const kinds = useMemo(() => {
    const counts = {};
    rows.forEach((r) => { counts[r.kind] = (counts[r.kind] || 0) + 1; });
    return Object.entries(counts).sort((a, b) => b[1] - a[1]);
  }, [rows]);

  const visible = useMemo(
    () => rows.filter((r) => !mutedKinds.has(r.kind)),
    [rows, mutedKinds]
  );

  const maxWeight = useMemo(
    () => Math.max(1, ...rows.map((r) => (r.detail || "").length)),
    [rows]
  );

  const toggleStep = (i) =>
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });

  const focusStep = (i) => {
    setOpen((prev) => new Set(prev).add(i));
    const el = stepRefs.current[i];
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  };

  const toggleKind = (k) =>
    setMutedKinds((prev) => {
      const next = new Set(prev);
      next.has(k) ? next.delete(k) : next.add(k);
      return next;
    });

  const allOpen = open.size >= visible.length && visible.length > 0;
  const setAll = () =>
    setOpen(allOpen ? new Set() : new Set(rows.map((_, i) => i)));

  if (status === "loading") {
    return <div className="trajectory-loading">Loading trajectory…</div>;
  }
  if (status === "error") {
    return <div className="trajectory-loading">Could not load trajectory JSON.</div>;
  }

  return (
    <div className="traj">
      <div className="traj-head">
        <div className="traj-head-main">
          <div className="traj-kicker">Tool-by-tool agent trajectory{label ? ` · ${label}` : ""}</div>
          <div className="traj-count">
            <b>{rows.length}</b> tool calls · {kinds.length} tool types
          </div>
        </div>
        <button className="pill" onClick={setAll}>
          {allOpen ? "Collapse all" : "Expand all"}
        </button>
      </div>

      {/* Timeline: one tick per step, height ∝ payload size, color by tool. */}
      <div className="traj-timeline" aria-hidden="true">
        {rows.map((r, i) => {
          const w = (r.detail || "").length;
          const h = 22 + Math.round((Math.log(w + 1) / Math.log(maxWeight + 1)) * 26);
          const muted = mutedKinds.has(r.kind);
          return (
            <button
              key={`${r.step}-${r.call}-${i}`}
              className={"traj-tick " + (muted ? "muted" : "")}
              style={{ height: h, background: muted ? "var(--rule)" : toolMeta(r.kind).color }}
              title={`#${i + 1} · ${r.kind} · ${r.title}`}
              onClick={() => focusStep(i)}
            />
          );
        })}
      </div>

      {/* Legend doubles as a per-tool filter. */}
      <div className="traj-legend">
        {kinds.map(([k, n]) => {
          const m = toolMeta(k);
          const muted = mutedKinds.has(k);
          return (
            <button
              key={k}
              className={"traj-legend-chip " + (muted ? "off" : "")}
              onClick={() => toggleKind(k)}
            >
              <span className="leg-swatch" style={{ background: m.color }} />
              {m.label}
              <b>{n}</b>
            </button>
          );
        })}
      </div>

      <div className="traj-steps" ref={listRef}>
        {visible.map((r) => {
          const i = rows.indexOf(r);
          const m = toolMeta(r.kind);
          const isOpen = open.has(i);
          return (
            <div
              key={`${r.step}-${r.call}-${i}`}
              ref={(el) => { stepRefs.current[i] = el; }}
              className={"traj-step " + (isOpen ? "open" : "")}
            >
              <button className="traj-step-head" onClick={() => toggleStep(i)}>
                <span className="traj-step-no">{String(i + 1).padStart(3, "0")}</span>
                <span className="traj-step-kind" style={{ "--tk": m.color }}>
                  <i>{m.glyph}</i>{m.label}
                </span>
                <span className="traj-step-title">{r.title}</span>
                <span className="traj-step-meta">step {r.step}</span>
                <span className="traj-step-chev">{isOpen ? "−" : "+"}</span>
              </button>
              {isOpen && <StepBody kind={r.kind} detail={r.detail} />}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SlackArtifact({ artifacts }) {
  const [activeId, setActiveId] = useState(artifacts.trials[0].id);
  const [loadedFor, setLoadedFor] = useState(null);
  const [trajectoryRows, setTrajectoryRows] = useState([]);
  const [trajectoryStatus, setTrajectoryStatus] = useState("loading");
  const active = artifacts.trials.find((t) => t.id === activeId) || artifacts.trials[0];

  useEffect(() => {
    let cancelled = false;
    setTrajectoryStatus("loading");
    setTrajectoryRows([]);
    fetch(active.trajectoryUrl)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled) {
          setTrajectoryRows(data.rows || []);
          setTrajectoryStatus("loaded");
        }
      })
      .catch(() => {
        if (!cancelled) setTrajectoryStatus("error");
      });
    return () => { cancelled = true; };
  }, [active.trajectoryUrl]);

  return (
    <div className="artifact-card">
      <div className="artifact-head">
        <div className={"artifact-status " + (loadedFor === active.id ? "live" : "")} style={{ marginLeft: "auto" }}>
          {loadedFor === active.id ? "IFRAME LOADED" : "LOCAL SERVICE"}
        </div>
      </div>

      <div className="artifact-pick-label">Pick a trial</div>
      <div className="artifact-selector">
        {artifacts.trials.map((trial, i) => (
          <button
            key={trial.id}
            className={"artifact-option " + (trial.id === active.id ? "active" : "")}
            onClick={() => {
              setActiveId(trial.id);
              setLoadedFor(null);
            }}
          >
            <b>{trial.model}</b>
            <span>{trial.agent}</span>
            <em>{trial.tokens} tokens · {trial.cost}</em>
          </button>
        ))}
      </div>

      <div className="artifact-scoreline">
        <div className="asl-main">
          <span>{active.agent} · {active.trial}</span>
          <b>{active.model}</b>
        </div>
        <div className="asl-stats">
          <span><i>Partial</i>{active.result.replace(/\s*partial$/i, "")}</span>
          {active.stages.split(" · ").map((part) => (
            <span key={part}><i>{/ux/i.test(part) ? "CUA UX" : "Unit tests"}</i>{part.replace(/\s*(CUA UX|correctness gates)$/i, "")}</span>
          ))}
        </div>
      </div>

      <div className="live-artifact-frame">
        <div className="iframe-toolbar">
          <div>
            <span>Live app · {active.agent} · {active.model}</span>
            <b>{active.liveUrl}</b>
          </div>
          <a className="btn ghost" href={active.liveUrl} target="_blank" rel="noreferrer">Open full app ↗</a>
        </div>
        <iframe
          key={active.id}
          title={`Interactive Slack clone artifact ${active.trial}`}
          src={active.liveUrl}
          onLoad={() => setLoadedFor(active.id)}
          sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
        />
      </div>

      <TrajectoryExplorer
        trial={active.id}
        label={`${active.agent} · ${active.model} · ${active.trial}`}
        rows={trajectoryRows}
        status={trajectoryStatus}
      />

    </div>
  );
}

function TaskEvidence({ evidence }) {
  if (!evidence) return null;

  return (
    <div className="evidence-card">
      <div className="artifact-head">
        <div>
          <div className="artifact-kicker">{evidence.kicker}</div>
          <h3>{evidence.title}</h3>
        </div>
        <div className="artifact-status live">{evidence.status}</div>
      </div>
      <p className="artifact-intro">{evidence.intro}</p>

      <div className="selected-artifact-meta">
        {evidence.stats.map((s) => (
          <div key={s.label}><span>{s.label}</span><b>{s.value}</b></div>
        ))}
      </div>

      {evidence.metrics && (
        <div className="metric-grid">
          {evidence.metrics.map((m) => (
            <div className="metric-card" key={m.label}>
              <div className="metric-label">{m.label}</div>
              <div className="metric-value">{m.value}</div>
              <p>{m.note}</p>
            </div>
          ))}
        </div>
      )}

      {evidence.notes?.map((note) => (
        <div className="trace" key={note.head}>
          <div className="tr-head">{note.head}</div>
          <div className="tr-quote">{note.body}</div>
        </div>
      ))}
    </div>
  );
}

function TaskLeaderboard({ leaderboard }) {
  const [openRank, setOpenRank] = useState(null);
  if (!leaderboard) return null;

  return (
    <div className="task-lb-card">
      <p className="task-lb-note">{leaderboard.note}</p>
      <div className="task-lb-list">
        {leaderboard.rows.map((row) => {
          const hasTrials = Array.isArray(row.trials) && row.trials.length > 0;
          const isOpen = openRank === row.rank;
          return (
          <div className="task-lb-group" key={`${row.rank}-${row.agent}-${row.model}`}>
            <button
              type="button"
              className={"task-lb-row " + (row.rank === 1 ? "top " : "") + (isOpen ? "open " : "") + (hasTrials ? "clickable" : "")}
              onClick={() => hasTrials && setOpenRank(isOpen ? null : row.rank)}
              aria-expanded={isOpen}
            >
              <span className="rank-badge">{row.rank}</span>
              <div className="task-lb-id">
                <span className="task-lb-name">{row.model}</span>
                <span className="task-lb-agent">{row.agent}</span>
              </div>
              <div className="task-lb-metrics">
                <span><b>Reward</b> {row.binary}</span>
                <span><b>Unit tests</b> {row.correctness.toFixed(3)}</span>
                <span><b>UX</b> {row.ux.toFixed(3)}</span>
              </div>
              <div className="task-lb-bar-track" title={`partial ${row.partial.toFixed(3)} of 1.0`}>
                <div className="task-lb-bar" style={{ width: `${Math.min(100, row.partial * 100)}%` }} />
              </div>
              <div className="task-lb-score">{row.partial.toFixed(3)}</div>
              {hasTrials && <span className="task-lb-chev">{isOpen ? "−" : "+"}</span>}
            </button>

            {isOpen && hasTrials && (
              <div className="task-lb-trials">
                {row.trials.map((t) => (
                  <a className="trial-chip" key={t.id} href={`#trajectory/${encodeURIComponent(t.id)}`}>
                    <span className="trial-chip-id">{t.trial}</span>
                    <span className="trial-chip-metrics">
                      <span><i>partial</i>{t.partial.toFixed(3)}</span>
                      <span><i>tokens</i>{t.tokens}</span>
                      <span><i>duration</i>{t.duration || "—"}</span>
                    </span>
                    <span className="trial-chip-open">View trajectory →</span>
                  </a>
                ))}
              </div>
            )}
          </div>
        );})}
      </div>
    </div>
  );
}

function SampleTask({ sample }) {
  const [activeId, setActiveId] = useState(sample?.tabs?.[0]?.id);
  const [selectedFilePath, setSelectedFilePath] = useState(null);
  if (!sample) return null;
  const active = sample.tabs.find((tab) => tab.id === activeId) || sample.tabs[0];
  const selectedFile = active.files?.find((file) => file.path === selectedFilePath) || active.files?.[0];
  // Inline markdown: `code`, **bold**.
  const renderInline = (text) => {
    const out = [];
    const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
    let last = 0;
    let m;
    let k = 0;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) out.push(text.slice(last, m.index));
      const tok = m[0];
      if (tok[0] === "`") out.push(<code key={k++}>{tok.slice(1, -1)}</code>);
      else out.push(<strong key={k++}>{tok.slice(2, -2)}</strong>);
      last = m.index + tok.length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  };
  const renderMarkdownish = (text) => String(text).split("\n").map((line, i) => {
    if (line.startsWith("### ")) return <h5 key={i}>{renderInline(line.slice(4))}</h5>;
    if (line.startsWith("## ")) return <h4 key={i}>{renderInline(line.slice(3))}</h4>;
    if (line.startsWith("- ")) return <li key={i}>{renderInline(line.slice(2))}</li>;
    if (line.trim() === "") return null;
    return <p key={i}>{renderInline(line)}</p>;
  });

  return (
    <div className="sample-task">
      <div className="sample-tabs">
        {sample.tabs.map((tab) => (
          <button
            key={tab.id}
            className={"sample-tab " + (tab.id === active.id ? "active" : "")}
            onClick={() => setActiveId(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {sample.note && <p className="sample-note">{sample.note}</p>}

      <div className="sample-panel">
        {active.meta && (
          <div className="env-meta">
            {active.meta.map((m) => (
              <div className="env-row" key={m.label}>
                <span className="env-k">{m.label}</span>
                <span className="env-v">{m.value}</span>
              </div>
            ))}
          </div>
        )}

        {active.blocks?.map((block) => (
          <div className="sample-block" key={block.title}>
            <div className="sample-block-title">{block.title}</div>
            <div className={"sample-markdown" + (block.scroll ? " scroll" : "")}>
              {renderMarkdownish(block.body)}
            </div>
          </div>
        ))}

        {active.files && (
          <div className="task-files-view">
            <div className="file-tree">
              {active.files.map((file) => (
                <button
                  type="button"
                  className={selectedFile?.path === file.path ? "active" : ""}
                  onClick={() => setSelectedFilePath(file.path)}
                  key={file.path}
                >
                  <span>{file.kind}</span>
                  <b>{file.path}</b>
                </button>
              ))}
            </div>
            <div className="file-snippets">
              {selectedFile && (
                <div className="file-card">
                  <div className="file-card-head">
                    <span>{selectedFile.kind}</span>
                    <b>{selectedFile.path}</b>
                  </div>
                  <p>{selectedFile.description}</p>
                  {selectedFile.snippet && <pre>{selectedFile.snippet}</pre>}
                </div>
              )}
            </div>
          </div>
        )}

        {active.groups && (
          <div className="verifier-groups">
            {active.groups.map((group) => (
              <div className="verifier-group" key={group.title}>
                <h4>{group.title}</h4>
                {group.intro && <p>{group.intro}</p>}
                <div className="verifier-group-rows">
                  {group.rows.map((row) => (
                    <div className="verifier-group-row" key={row}>{row}</div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}

        {active.steps?.map((step, i) => (
          <div className="trajectory-step" key={`${step.label}-${i}`}>
            <div className="trajectory-label">{step.label}</div>
            <div className={step.mono ? "trajectory-body mono" : "trajectory-body"}>{step.body}</div>
          </div>
        ))}

        {active.rubric && (
          <div className="rubric-table">
            <div className="rubric-head"><span>Criterion</span><span>Score</span></div>
            {active.rubric.map((row) => (
              <div className={"rubric-row " + (row.score === "No" || row.score === "Fail" ? "bad" : "good")} key={row.criterion}>
                <span>{row.criterion}</span>
                <b>{row.score}</b>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// Full-page trajectory viewer (DeepSWE-style) reached via #trajectory/<id>.
function TrajectoryPage({ trialId }) {
  const trial = SLACK_TRIAL_BY_ID[trialId];
  const [rows, setRows] = useState([]);
  const [status, setStatus] = useState("loading");

  useEffect(() => {
    if (!trial) return;
    let cancelled = false;
    setStatus("loading");
    setRows([]);
    fetch(trial.trajectoryUrl)
      .then((res) => { if (!res.ok) throw new Error(`HTTP ${res.status}`); return res.json(); })
      .then((data) => { if (!cancelled) { setRows(data.rows || []); setStatus("loaded"); } })
      .catch(() => { if (!cancelled) setStatus("error"); });
    return () => { cancelled = true; };
  }, [trial]);

  if (!trial) {
    return (
      <section className="task-page">
        <div className="container">
          <a className="back-link" href="#task/slack-clone">← Back to leaderboard</a>
          <h1 className="task-page-title">Trajectory not found.</h1>
        </div>
      </section>
    );
  }

  const stats = [
    { label: "Rank", value: `#${trial.rank}` },
    { label: "Reward", value: trial.reward.toFixed(1) },
    { label: "Partial", value: trial.partial.toFixed(3) },
    { label: "Unit tests", value: trial.gates },
    { label: "CUA UX", value: trial.ux.toFixed(3) },
    { label: "Tokens", value: trial.tokens },
    { label: "Cost", value: trial.cost },
    { label: "Duration", value: trial.duration || "—" },
    { label: "Tool calls", value: String(trial.steps) },
  ];

  return (
    <>
      <section className="task-page hero task-hero">
        <div className="container">
          <a className="back-link" href="#task/slack-clone">← Back to leaderboard</a>
          <div className="eyebrow">Trajectory · {trial.trial}</div>
          <h1 className="title">{trial.configAgent} · {trial.configModel}</h1>
          <p className="lede">
            Full agent trajectory for <b>{trial.trial}</b> — every tool call the agent made,
            replayable step by step.
          </p>
          <div className="cta-row" style={{ marginTop: 18 }}>
            {trial.liveUrl && (
              <a className="btn" href={trial.liveUrl} target="_blank" rel="noreferrer">Open live app ↗</a>
            )}
            <a className="btn ghost" href="#task/slack-clone">Back to task</a>
          </div>
        </div>
      </section>

      <section className="task-page">
        <div className="container">
          <div className="selected-artifact-meta" style={{ marginBottom: 22 }}>
            {stats.map((s) => (
              <div key={s.label}><span>{s.label}</span><b>{s.value}</b></div>
            ))}
          </div>
          <TrajectoryExplorer
            trial={trial.id}
            label={`${trial.configAgent} · ${trial.configModel}`}
            rows={rows}
            status={status}
          />
        </div>
      </section>
    </>
  );
}

function TaskDetailPage({ taskId }) {
  const detail = TASK_DETAILS[taskId];
  const task = TASKS.find((t) => t.id === taskId);

  if (!task) {
    return (
      <section className="task-page">
        <div className="container">
          <a className="back-link" href="#tasks">← Back to tasks</a>
          <h1 className="task-page-title">Task not found.</h1>
        </div>
      </section>
    );
  }

  if (!detail) {
    return (
      <section className="task-page">
        <div className="container">
          <a className="back-link" href="#tasks">← Back to tasks</a>
          <div className="eyebrow">{CAT_LABEL[task.cat]} · {task.id}</div>
          <h1 className="task-page-title">{task.title}</h1>
          <p className="task-page-lede">{task.desc}</p>
          <div className="detail-placeholder">
            Detailed task page coming next. Slack clone is implemented first with a CUA artifact replay.
          </div>
        </div>
      </section>
    );
  }

  return (
    <>
      <section className="task-page hero task-hero">
        <div className="container">
          <a className="back-link" href="#tasks">← Back to all tasks</a>
          <div className="eyebrow">{detail.taskNo} · {detail.kicker}</div>
          <h1 className="title">{detail.title}</h1>
          <p className="lede">{detail.summary}</p>
        </div>
      </section>

      {detail.leaderboard && (
        <section className="task-page">
          <div className="container">
            <div className="section-head">
              <div className="section-no"><span className="dot">●</span>Leaderboard</div>
            </div>
            <TaskLeaderboard leaderboard={detail.leaderboard} />
          </div>
        </section>
      )}

      {detail.sections?.length > 0 && (
        <section className="task-page">
          <div className="container">
            {detail.sections.map((s) => (
              <div className="task-detail-section" key={s.title}>
                <div className="section-no"><span className="dot">●</span>{s.title}</div>
                <p>{s.body}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {detail.sample && (
        <section className="task-page">
          <div className="container">
            <div className="section-head">
              <div className="section-no"><span className="dot">●</span>Task specification</div>
            </div>
            <SampleTask sample={detail.sample} />
          </div>
        </section>
      )}

      {detail.verifier && (
        <section className="task-page">
          <div className="container">
            <div className="section-head">
              <div className="section-no"><span className="dot">●</span>Task verifier</div>
              <h2 className="section-title">{detail.verifierTitle}</h2>
            </div>
            <div className="verifier-grid">
              {(detail.verifier.groups || [
                { title: "Deterministic gates", items: detail.verifier.deterministic },
                { title: "CUA browser rubric", items: detail.verifier.ux },
              ]).map((group) => (
                <div key={group.title}>
                  <h3>{group.title}</h3>
                  {group.items.map((v) => <div className="check-row" key={v}>{v}</div>)}
                </div>
              ))}
            </div>
          </div>
        </section>
      )}

      {(detail.artifacts || detail.evidence) && (
        <section className="task-page">
          <div className="container">
            <div className="section-head">
              <div className="section-no"><span className="dot">●</span>{detail.artifacts ? "Agent trials" : "Result"}</div>
              <h2 className="section-title">{detail.resultTitle}</h2>
            </div>
            {detail.artifacts && <SlackArtifact artifacts={detail.artifacts} />}
            {detail.evidence && <TaskEvidence evidence={detail.evidence} />}
          </div>
        </section>
      )}
    </>
  );
}

function CourseProfileSection() {
  return (
    <section id="course">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§04 / The course</div>
          <h2 className="section-title">Multi-hour rollouts, millions of tokens.</h2>
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
          <h2 className="section-title">A marathon, not a sprint.</h2>
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
          <h2 className="section-title">Where agents cheat, drift, and stall.</h2>
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
          <h2 className="section-title">Built by a small cross-lab group.</h2>
        </div>
        <div style={{ maxWidth: 620, color: "var(--ink-2)", fontSize: 16, lineHeight: 1.55 }}>
          The 20 accepted tasks were authored by 11 unique contributors —
          software engineers familiar with the systems each task targets.
          Author and affiliation details will be added with the public release.
        </div>
      </div>
    </section>);

}

function Citation() {
  const bib = `@misc{swemarathon_2026,
  title        = {{SWE-Marathon: Can Agents Autonomously Complete Ultra-Long-Horizon Software Work?}},
  author       = {{SWE-Marathon Authors}},
  year         = {2026},
  howpublished = {\\url{https://github.com/abundant-ai/long-horizon}},
  note         = {Benchmark and evaluation code.}
}`;
  const [copied, setCopied] = useState(false);
  return (
    <section id="cite">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§08 / Paper</div>
          <h2 className="section-title">If SWE-Marathon is useful,<br />please cite us.</h2>
        </div>
        <p style={{ maxWidth: 620, color: "var(--ink-2)", margin: "0 0 18px", fontSize: 15, lineHeight: 1.55 }}>
          Please cite the benchmark via the entry below for now. We'll update
          this section with the canonical paper citation when it is available.
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
              Apache 2.0. We welcome new tasks, new agents, and new judges.
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
          <div>Apache 2.0 License</div>
        </div>
      </div>
    </footer>);

}

function App() {
  const [hash, setHash] = useState(() => window.location.hash);

  useEffect(() => {
    const onHash = () => {
      setHash(window.location.hash);
      if (/^#(task|trajectory)\//.test(window.location.hash)) window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const trajMatch = hash.match(/^#trajectory\/(.+)$/);
  if (trajMatch) {
    return (
      <>
        <TrajectoryPage trialId={decodeURIComponent(trajMatch[1])} />
        <Footer />
      </>
    );
  }

  const taskMatch = hash.match(/^#task\/(.+)$/);
  if (taskMatch) {
    return (
      <>
        <TaskDetailPage taskId={decodeURIComponent(taskMatch[1])} />
        <Footer />
      </>
    );
  }

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
