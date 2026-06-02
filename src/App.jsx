
import React, { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import {
  CASE_STUDIES,
  CATEGORIES,
  CAT_LABEL,
  HEADLINE,
  MARATHON_ANATOMY,
  LEADERBOARD,
  RH_BY_MODEL,
  TRIAL_BY_ID,
  TASK_DETAILS,
  TASKS,
} from "./data.js";

const Analysis = lazy(() => import("./analysis.jsx"));

const TASK_FAMILIES = CATEGORIES;

// §04 "Anatomy of a marathon" — what agents actually do across a multi-hour
// run. Aggregated over 1,180 logged trajectories; numbers in MARATHON_ANATOMY.
function ShellMixBars() {
  const rows = MARATHON_ANATOMY.shellMix;
  const maxPct = Math.max(...rows.map((r) => r.pct));
  return (
    <div style={{
      border: "1px solid var(--rule)",
      background: "var(--bg)",
      padding: "16px 18px 16px",
      fontFamily: "var(--mono)",
      fontSize: 11,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 14 }}>
        <span>Shell commands by intent</span>
        <span>% of all terminal calls</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(150px, 200px) 1fr 46px", gap: "10px 12px", alignItems: "center" }}>
        {rows.map((r, i) => (
          <React.Fragment key={r.label}>
            <div style={{ fontFamily: "var(--sans)", fontSize: 13, color: "var(--ink)", lineHeight: 1.2 }}>
              {r.label}
              <span style={{ display: "block", fontSize: 10.5, color: "var(--ink-3)" }}>{r.hint}</span>
            </div>
            <div style={{ position: "relative", height: 16, background: "var(--rule2, #ebe6d7)" }}>
              <div style={{
                position: "absolute", left: 0, top: 0, bottom: 0,
                width: `${(r.pct / maxPct) * 100}%`,
                background: i === 0
                  ? "linear-gradient(90deg, var(--accent), color-mix(in oklch, var(--accent) 55%, var(--bg)))"
                  : "var(--ink)",
                opacity: i === 0 ? 0.95 : 0.5 - i * 0.04,
              }} />
            </div>
            <div style={{ textAlign: "right", color: "var(--ink)", fontWeight: 600 }}>
              {r.pct.toFixed(1)}%
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
  { num: `<${Math.ceil(HEADLINE.bestPass1Pct)}`, unit: "%", label: "Best pass@1" },
  { num: String(Math.round(HEADLINE.avgTokensPerTrialM)), unit: "M", label: "Mean tokens / trial" },
  { num: String(HEADLINE.rhAttemptPct), unit: "%", label: "Trials reward-hacking" },
  { num: "1,300", unit: "", label: "Logged trials" }];

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
        <div className="eyebrow">SWE-MARATHON · 20 LONG-HORIZON TASKS · 1,300 LOGGED TRIALS</div>
        <h1 className="title">
          Can agents autonomously complete<br />
          <span className="ital">ultra-long-horizon</span> software work?
        </h1>
        <p className="lede">
          <strong>SWE-Marathon</strong> is a benchmark of <strong>20 multi-hour</strong> software-engineering
          tasks: library reproductions, full-stack product clones, and ML engineering.
        </p>
        <FoxRunner />
        <div className="cta-row">
          <a className="btn" href="#leaderboard">View leaderboard <span className="arr">↓</span></a>
          <a className="btn ghost" href="#tasks">Tasks <span className="arr">↓</span></a>
          <a className="btn ghost" href="https://github.com/abundant-ai/long-horizon">GitHub ↗</a>
        </div>
        <StatStrip />
      </div>
    </header>);

}

// Brand marks + gradients for each model family. Used to give every
// leaderboard row a recognizable logo and a brand-tinted bar.
const BRANDS = {
  openai: {
    grad: ["#2563eb", "#7aa7f7"],
    logo: (
      <svg viewBox="0 0 24 24" fill="currentColor" style={{ color: "#1a1a17" }}>
        <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" />
      </svg>
    ),
  },
  anthropic: {
    grad: ["#d97757", "#e9b15a"],
    logo: (
      <svg viewBox="0 0 24 24" fill="currentColor" style={{ color: "#1a1a17" }}>
        <path d="M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5528h3.7442L10.5363 3.541Zm-.3712 10.2188 2.2914-5.9456 2.2914 5.9456Z" />
      </svg>
    ),
  },
  gemini: {
    grad: ["#7c5cd6", "#c158dc"],
    logo: (
      <svg viewBox="0 0 24 24" fill="currentColor" style={{ color: "#9b6dd6" }}>
        <path d="M12 24A14.304 14.304 0 0 0 0 12 14.304 14.304 0 0 0 12 0a14.305 14.305 0 0 0 12 12 14.305 14.305 0 0 0-12 12" />
      </svg>
    ),
  },
  deepseek: {
    grad: ["#4d6bfe", "#9db4ff"],
    logo: (
      <svg viewBox="0 0 24 24" fill="#4D6BFE">
        <path d="M23.748 4.482c-.254-.124-.364.113-.512.234-.051.039-.094.09-.137.136-.372.397-.806.657-1.373.626-.829-.046-1.537.214-2.163.848-.133-.782-.575-1.248-1.247-1.548-.352-.156-.708-.311-.955-.65-.172-.241-.219-.51-.305-.774-.055-.16-.11-.323-.293-.35-.2-.031-.278.136-.356.276-.313.572-.434 1.202-.422 1.84.027 1.436.633 2.58 1.838 3.393.137.093.172.187.129.323-.082.28-.18.552-.266.833-.055.179-.137.217-.329.14a5.526 5.526 0 01-1.736-1.18c-.857-.828-1.631-1.742-2.597-2.458a11.365 11.365 0 00-.689-.471c-.985-.957.13-1.743.388-1.836.27-.098.093-.432-.779-.428-.872.004-1.67.295-2.687.684a3.055 3.055 0 01-.465.137 9.597 9.597 0 00-2.883-.102c-1.885.21-3.39 1.102-4.497 2.623C.082 8.606-.231 10.684.152 12.85c.403 2.284 1.569 4.175 3.36 5.653 1.858 1.533 3.997 2.284 6.438 2.14 1.482-.085 3.133-.284 4.994-1.86.47.234.962.327 1.78.397.63.059 1.236-.03 1.705-.128.735-.156.684-.837.419-.961-2.155-1.004-1.682-.595-2.113-.926 1.096-1.296 2.746-2.642 3.392-7.003.05-.347.007-.565 0-.845-.004-.17.035-.237.23-.256a4.173 4.173 0 001.545-.475c1.396-.763 1.96-2.015 2.093-3.517.02-.23-.004-.467-.247-.588zM11.581 18c-2.089-1.642-3.102-2.183-3.52-2.16-.392.024-.321.471-.235.763.09.288.207.486.371.739.114.167.192.416-.113.603-.673.416-1.842-.14-1.897-.167-1.361-.802-2.5-1.86-3.301-3.307-.774-1.393-1.224-2.887-1.298-4.482-.02-.386.093-.522.477-.592a4.696 4.696 0 011.529-.039c2.132.312 3.946 1.265 5.468 2.774.868.86 1.525 1.887 2.202 2.891.72 1.066 1.494 2.082 2.48 2.914.348.292.625.514.891.677-.802.09-2.14.11-3.054-.614zm1-6.44a.306.306 0 01.415-.287.302.302 0 01.2.288.306.306 0 01-.31.307.303.303 0 01-.304-.308zm3.11 1.596c-.2.081-.399.151-.59.16a1.245 1.245 0 01-.798-.254c-.274-.23-.47-.358-.552-.758a1.73 1.73 0 01.016-.588c.07-.327-.008-.537-.239-.727-.187-.156-.426-.199-.688-.199a.559.559 0 01-.254-.078c-.11-.054-.2-.19-.114-.358.028-.054.16-.186.192-.21.356-.202.767-.136 1.146.016.352.144.618.408 1.001.782.391.451.462.576.685.914.176.265.336.537.445.848.067.195-.019.354-.25.452z" />
      </svg>
    ),
  },
  zhipu: {
    grad: ["#0d9488", "#5eead4"],
    logo: (
      <svg viewBox="0 0 24 24" fill="#3859FF" fillRule="nonzero">
        <path d="M11.991 23.503a.24.24 0 00-.244.248.24.24 0 00.244.249.24.24 0 00.245-.249.24.24 0 00-.22-.247l-.025-.001zM9.671 5.365a1.697 1.697 0 011.099 2.132l-.071.172-.016.04-.018.054c-.07.16-.104.32-.104.498-.035.71.47 1.279 1.186 1.314h.366c1.309.053 2.338 1.173 2.286 2.523-.052 1.332-1.152 2.38-2.478 2.327h-.174c-.715.018-1.274.64-1.239 1.368 0 .124.018.23.053.337.209.373.54.658.96.8.75.23 1.517-.125 1.9-.782l.018-.035c.402-.64 1.17-.96 1.92-.711.854.284 1.378 1.226 1.099 2.167a1.661 1.661 0 01-2.077 1.102 1.711 1.711 0 01-.907-.711l-.017-.035c-.2-.323-.463-.58-.851-.711l-.056-.018a1.646 1.646 0 00-1.954.746 1.66 1.66 0 01-1.065.764 1.677 1.677 0 01-1.989-1.279c-.209-.906.332-1.83 1.257-2.043a1.51 1.51 0 01.296-.035h.018c.68-.071 1.151-.622 1.116-1.333a1.307 1.307 0 00-.227-.693 2.515 2.515 0 01-.366-1.403 2.39 2.39 0 01.366-1.208c.14-.195.21-.444.227-.693.018-.71-.506-1.261-1.186-1.332l-.07-.018a1.43 1.43 0 01-.299-.07l-.05-.019a1.7 1.7 0 01-1.047-2.114 1.68 1.68 0 012.094-1.101zm-5.575 10.11c.26-.264.639-.367.994-.27.355.096.633.379.728.74.095.362-.007.748-.267 1.013-.402.41-1.053.41-1.455 0a1.062 1.062 0 010-1.482zm14.845-.294c.359-.09.738.024.992.297.254.274.344.665.237 1.025-.107.36-.396.634-.756.718-.551.128-1.1-.22-1.23-.781a1.05 1.05 0 01.757-1.26zm-.064-4.39c.314.32.49.753.49 1.206 0 .452-.176.886-.49 1.206-.315.32-.74.5-1.185.5-.444 0-.87-.18-1.184-.5a1.727 1.727 0 010-2.412 1.654 1.654 0 012.369 0zm-11.243.163c.364.484.447 1.128.218 1.691a1.665 1.665 0 01-2.188.923c-.855-.36-1.26-1.358-.907-2.228a1.68 1.68 0 011.33-1.038c.593-.08 1.183.169 1.547.652zm11.545-4.221c.368 0 .708.2.892.524.184.324.184.724 0 1.048a1.026 1.026 0 01-.892.524c-.568 0-1.03-.47-1.03-1.048 0-.579.462-1.048 1.03-1.048zm-14.358 0c.368 0 .707.2.891.524.184.324.184.724 0 1.048a1.026 1.026 0 01-.891.524c-.569 0-1.03-.47-1.03-1.048 0-.579.461-1.048 1.03-1.048zm10.031-1.475c.925 0 1.675.764 1.675 1.706s-.75 1.705-1.675 1.705-1.674-.763-1.674-1.705c0-.942.75-1.706 1.674-1.706zm-2.626-.684c.362-.082.653-.356.761-.718a1.062 1.062 0 00-.238-1.028 1.017 1.017 0 00-.996-.294c-.547.14-.881.7-.752 1.257.13.558.675.907 1.225.783zm0 16.876c.359-.087.644-.36.75-.72a1.062 1.062 0 00-.237-1.019 1.018 1.018 0 00-.985-.301 1.037 1.037 0 00-.762.717c-.108.361-.017.754.239 1.028.245.263.606.377.953.305l.043-.01zM17.19 3.5a.631.631 0 00.628-.64c0-.355-.279-.64-.628-.64a.631.631 0 00-.628.64c0 .355.28.64.628.64zm-10.38 0a.631.631 0 00.628-.64c0-.355-.28-.64-.628-.64a.631.631 0 00-.628.64c0 .355.279.64.628.64zm-5.182 7.852a.631.631 0 00-.628.64c0 .354.28.639.628.639a.63.63 0 00.627-.606l.001-.034a.62.62 0 00-.628-.64zm5.182 9.13a.631.631 0 00-.628.64c0 .355.279.64.628.64a.631.631 0 00.628-.64c0-.355-.28-.64-.628-.64zm10.38.018a.631.631 0 00-.628.64c0 .355.28.64.628.64a.631.631 0 00.628-.64c0-.355-.279-.64-.628-.64zm5.182-9.148a.631.631 0 00-.628.64c0 .354.279.639.628.639a.631.631 0 00.628-.64c0-.355-.28-.64-.628-.64zm-.384-4.992a.24.24 0 00.244-.249.24.24 0 00-.244-.249.24.24 0 00-.244.249c0 .142.122.249.244.249zM11.991.497a.24.24 0 00.245-.248A.24.24 0 0011.99 0a.24.24 0 00-.244.249c0 .133.108.236.223.247l.021.001zM2.011 6.36a.24.24 0 00.245-.249.24.24 0 00-.244-.249.24.24 0 00-.244.249.24.24 0 00.244.249zm0 11.263a.24.24 0 00-.243.248.24.24 0 00.244.249.24.24 0 00.244-.249.252.252 0 00-.244-.248zm19.995-.018a.24.24 0 00-.245.248.24.24 0 00.245.25.24.24 0 00.244-.25.252.252 0 00-.244-.248z" />
      </svg>
    ),
  },
  moonshot: {
    grad: ["#6b4ea0", "#a78fd0"],
    logo: (
      <svg viewBox="0 0 24 24" fill="currentColor" fillRule="evenodd" style={{ color: "#1a1a17" }}>
        <path d="M21.846 0a1.923 1.923 0 110 3.846H20.15a.226.226 0 01-.227-.226V1.923C19.923.861 20.784 0 21.846 0z" />
        <path d="M11.065 11.199l7.257-7.2c.137-.136.06-.41-.116-.41H14.3a.164.164 0 00-.117.051l-7.82 7.756c-.122.12-.302.013-.302-.179V3.82c0-.127-.083-.23-.185-.23H3.186c-.103 0-.186.103-.186.23V19.77c0 .128.083.23.186.23h2.69c.103 0 .186-.102.186-.23v-3.25c0-.069.025-.135.069-.178l2.424-2.406a.158.158 0 01.205-.023l6.484 4.772a7.677 7.677 0 003.453 1.283c.108.012.2-.095.2-.23v-3.06c0-.117-.07-.212-.164-.227a5.028 5.028 0 01-2.027-.807l-5.613-4.064c-.117-.078-.132-.279-.028-.381z" />
      </svg>
    ),
  },
  minimax: {
    grad: ["#e2167e", "#fe603c"],
    logo: (
      <svg viewBox="0 0 24 24" fillRule="nonzero">
        <defs>
          <linearGradient id="mm-grad" x1="0%" x2="100.182%" y1="50.057%" y2="50.057%">
            <stop offset="0%" stopColor="#E2167E" />
            <stop offset="100%" stopColor="#FE603C" />
          </linearGradient>
        </defs>
        <path fill="url(#mm-grad)" d="M16.278 2c1.156 0 2.093.927 2.093 2.07v12.501a.74.74 0 00.744.709.74.74 0 00.743-.709V9.099a2.06 2.06 0 012.071-2.049A2.06 2.06 0 0124 9.1v6.561a.649.649 0 01-.652.645.649.649 0 01-.653-.645V9.1a.762.762 0 00-.766-.758.762.762 0 00-.766.758v7.472a2.037 2.037 0 01-2.048 2.026 2.037 2.037 0 01-2.048-2.026v-12.5a.785.785 0 00-.788-.753.785.785 0 00-.789.752l-.001 15.904A2.037 2.037 0 0113.441 22a2.037 2.037 0 01-2.048-2.026V18.04c0-.356.292-.645.652-.645.36 0 .652.289.652.645v1.934c0 .263.142.506.372.638.23.131.514.131.744 0a.734.734 0 00.372-.638V4.07c0-1.143.937-2.07 2.093-2.07zm-5.674 0c1.156 0 2.093.927 2.093 2.07v11.523a.648.648 0 01-.652.645.648.648 0 01-.652-.645V4.07a.785.785 0 00-.789-.78.785.785 0 00-.789.78v14.013a2.06 2.06 0 01-2.07 2.048 2.06 2.06 0 01-2.071-2.048V9.1a.762.762 0 00-.766-.758.762.762 0 00-.766.758v3.8a2.06 2.06 0 01-2.071 2.049A2.06 2.06 0 010 12.9v-1.378c0-.357.292-.646.652-.646.36 0 .653.29.653.646V12.9c0 .418.343.757.766.757s.766-.339.766-.757V9.099a2.06 2.06 0 012.07-2.048 2.06 2.06 0 012.071 2.048v8.984c0 .419.343.758.767.758.423 0 .766-.339.766-.758V4.07c0-1.143.937-2.07 2.093-2.07z" />
      </svg>
    ),
  },
  default:  { grad: ["#8a8880", "#bdbbb2"], mono: "·", monoColor: "#8a8880" },
};

function brandFor(name) {
  if (/GPT/i.test(name)) return "openai";
  if (/Claude/i.test(name)) return "anthropic";
  if (/Gemini/i.test(name)) return "gemini";
  if (/DeepSeek/i.test(name)) return "deepseek";
  if (/GLM/i.test(name)) return "zhipu";
  if (/Kimi/i.test(name)) return "moonshot";
  if (/MiniMax/i.test(name)) return "minimax";
  return "default";
}

function BrandLogo({ name }) {
  const b = BRANDS[brandFor(name)] || BRANDS.default;
  if (b.logo) return <span className="lb-logo">{b.logo}</span>;
  return (
    <span className="lb-logo lb-logo-mono" style={{ background: b.monoColor }}>{b.mono}</span>
  );
}

// Per-failure-bucket accent. Reward hacking is red, consistent with the
// cheat chips elsewhere on the site; the rest get distinct muted hues.
function bucketColor(bucket) {
  if (/reward hack/i.test(bucket)) return "oklch(0.55 0.18 25)";    // red
  if (/premature/i.test(bucket)) return "var(--warn)";              // amber
  if (/implementation/i.test(bucket)) return "oklch(0.52 0.12 250)"; // blue
  if (/self-verif/i.test(bucket)) return "oklch(0.52 0.14 300)";    // purple
  if (/timeout/i.test(bucket)) return "var(--ink-3)";               // gray
  return "var(--accent)";
}

function Leaderboard() {
  const sorted = useMemo(
    () => LEADERBOARD.filter((r) => !r.ref).sort((a, b) => b.pass1 - a.pass1),
    []
  );
  const maxPass = Math.max(...sorted.map((r) => r.pass1), 1);
  const axisMax = Math.max(5, Math.ceil(maxPass / 5) * 5);
  const ticks = [];
  for (let t = 0; t <= axisMax; t += 5) ticks.push(t);

  return (
    <section id="leaderboard">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§01 / Leaderboard</div>
          <h2 className="section-title">SWE-Marathon Pass@1</h2>
        </div>

        <div className="lb-chart">
          <div className="lb-chart-head">
            <span>Model</span>
            <span>Pass@1</span>
          </div>

          {sorted.map((row) => {
            const brand = brandFor(row.name);
            const grad = (BRANDS[brand] || BRANDS.default).grad;
            const pctOf = (v) => `${(v / axisMax) * 100}%`;
            return (
              <div className={"lb-row " + (row.highlight ? "highlight" : "")} key={`${row.name}-${row.scaffold}`}>
                <div className="lb-row-top">
                  <div className="lb-id">
                    <BrandLogo name={row.name} />
                    <span className="lb-model">{row.name}</span>
                    <span className="lb-sep">/</span>
                    <span className="lb-agent">{row.scaffold.replace(/\s+v\d[\d.]*$/i, "")}</span>
                  </div>
                  <div className="lb-score">
                    <b>{row.pass1.toFixed(1)}%</b>
                  </div>
                </div>
                <div className="lb-track">
                  <div
                    className="lb-fill"
                    style={{ width: pctOf(row.pass1), background: `linear-gradient(90deg, ${grad[0]}, ${grad[1]})` }}
                  />
                </div>
              </div>
            );
          })}

          <div className="lb-axis">
            {ticks.map((t) => (
              <span key={t} className="lb-axis-tick" style={{ left: `${(t / axisMax) * 100}%` }}>{t}%</span>
            ))}
          </div>
        </div>

        <p className="lb-foot">
          <sup>†</sup> SWE-Marathon tasks are <strong>binary reward</strong>: 1.0 requires
          passing every verifier test; any failing test gives 0.0. Per-task
          leaderboards also report <strong>uncalibrated partial scores</strong>, which
          measure progress toward a full pass and are generally much higher.
        </p>
      </div>
    </section>);

}

function Tasks() {
  return (
    <section id="tasks">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§03 / Tasks</div>
          <h2 className="section-title">20 marathons. 4 task families.</h2>
        </div>

        {TASK_FAMILIES.filter((f) => f.id !== "all").map((fam) => {
          const famTasks = TASKS.filter((t) => t.cat === fam.id);
          if (famTasks.length === 0) return null;
          return (
            <div className="task-family-group" key={fam.id}>
              <div className="task-family-head">
                <b>{fam.label}</b>
                <span>{famTasks.length} tasks</span>
              </div>
              <div className="tasks-grid">
                {famTasks.map((t) => {
                  const i = TASKS.indexOf(t);
                  return (
                  <a className={"task task-link " + (TASK_DETAILS[t.id] ? "has-detail" : "")} href={`#task/${t.id}`} key={t.id}>
                    <div className="task-head">
                      <div className="task-id">T{String(i + 1).padStart(2, "0")}</div>
                      <div className="task-budget">{t.agentH}h agent timeout</div>
                    </div>
                    <h3 className="task-title">{t.title}</h3>
                    <p className="task-desc">{t.desc}</p>
                    <div className="task-meta">
                      <div><span className="k">verifier</span><span className="v">{t.verifier}</span></div>
                    </div>
                  </a>
                  );
                })}
              </div>
            </div>
          );
        })}
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

// Trial showcase: a trial picker + the selected trial's full agent trajectory.
// When the trials carry a `liveUrl` (the four deployed CUA clones) it also
// embeds the live app in an iframe; otherwise it's a pure trajectory replay,
// which is what every non-deployable task (compilers, ports, kernels) uses.
function TrialShowcase({ artifacts }) {
  const [activeId, setActiveId] = useState(artifacts.trials[0].id);
  const [loadedFor, setLoadedFor] = useState(null);
  const [trajectoryRows, setTrajectoryRows] = useState([]);
  const [trajectoryStatus, setTrajectoryStatus] = useState("loading");
  const active = artifacts.trials.find((t) => t.id === activeId) || artifacts.trials[0];
  const hasLive = Boolean(active.liveUrl);

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
      {hasLive && (
        <div className="artifact-head">
          <div className={"artifact-status " + (loadedFor === active.id ? "live" : "")} style={{ marginLeft: "auto" }}>
            {loadedFor === active.id ? "IFRAME LOADED" : "LOCAL SERVICE"}
          </div>
        </div>
      )}

      <div className="artifact-pick-label">Pick a trial</div>
      <div className="artifact-selector">
        {artifacts.trials.map((trial) => (
          <button
            key={trial.id}
            className={"artifact-option " + (trial.id === active.id ? "active" : "")}
            onClick={() => {
              setActiveId(trial.id);
              setLoadedFor(null);
            }}
          >
            {trial.tag && (
              <span className={"artifact-option-tag " + (/hack/i.test(trial.tag) ? "is-hack" : "is-long")}>
                {trial.tag}
              </span>
            )}
            <b>{trial.model}</b>
            <span>{trial.agent}</span>
            <em>{trial.tokens} tokens · {trial.cost}</em>
          </button>
        ))}
      </div>

      {hasLive && (
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
            title={`Interactive artifact ${active.trial}`}
            src={active.liveUrl}
            onLoad={() => setLoadedFor(active.id)}
            sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
          />
        </div>
      )}

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

  // CUA tasks carry a computer-use UX sub-score; pure test-suite tasks (e.g.
  // compilers) don't, so drop the column entirely rather than show "0.000".
  const hasUX = leaderboard.rows.some((r) => r.ux > 0);

  // Verified reward-hacking trials get flagged on their config row + trial chip.
  const hackSet = new Set(leaderboard.hackIds || []);
  const hasHacks = hackSet.size > 0;

  return (
    <div className="task-lb-card">
      <p className="task-lb-note">{leaderboard.note}</p>
      <div className="task-lb-list">
        {leaderboard.rows.map((row) => {
          const hasTrials = Array.isArray(row.trials) && row.trials.length > 0;
          const isOpen = openRank === row.rank;
          const rowHackCount = hasTrials ? row.trials.filter((t) => hackSet.has(t.id)).length : 0;
          return (
          <div className="task-lb-group" key={`${row.rank}-${row.agent}-${row.model}`}>
            <button
              type="button"
              className={"task-lb-row " + (row.rank === 1 ? "top " : "") + (isOpen ? "open " : "") + (rowHackCount ? "flagged " : "") + (hasTrials ? "clickable" : "")}
              onClick={() => hasTrials && setOpenRank(isOpen ? null : row.rank)}
              aria-expanded={isOpen}
            >
              <span className="rank-badge">{row.rank}</span>
              <div className="task-lb-id">
                <span className="task-lb-name">{row.model}</span>
                <span className="task-lb-agent">{row.agent}</span>
                {rowHackCount > 0 && (
                  <span className="task-lb-hack" title={`${rowHackCount} trial${rowHackCount > 1 ? "s" : ""} flagged for reward hacking`}>
                    ⚠ Reward hack{rowHackCount > 1 ? ` ×${rowHackCount}` : ""}
                  </span>
                )}
              </div>
              <div className="task-lb-metrics">
                <span><b>Reward</b> {row.binary}</span>
                <span><b>Unit tests</b> {row.correctness.toFixed(3)}</span>
                {hasUX && <span><b>UX</b> {row.ux.toFixed(3)}</span>}
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
                  <a className={"trial-chip " + (hackSet.has(t.id) ? "is-hack" : "")} key={t.id} href={`#trajectory/${encodeURIComponent(t.id)}`}>
                    <span className="trial-chip-id">
                      {t.trial}
                      {hackSet.has(t.id) && <span className="trial-chip-hack">⚠ Reward hack</span>}
                    </span>
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
  const trial = TRIAL_BY_ID[trialId];
  const backTask = trial?.task || "slack-clone";
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
    trial.ux > 0 && { label: "CUA UX", value: trial.ux.toFixed(3) },
    { label: "Tokens", value: trial.tokens },
    { label: "Cost", value: trial.cost },
    { label: "Duration", value: trial.duration || "—" },
    { label: "Tool calls", value: String(trial.steps) },
  ].filter(Boolean);

  return (
    <>
      <section className="task-page hero task-hero">
        <div className="container">
          <a className="back-link" href={`#task/${backTask}`}>← Back to leaderboard</a>
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
            <a className="btn ghost" href={`#task/${backTask}`}>Back to task</a>
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
                { title: "Deterministic checks", items: detail.verifier.deterministic },
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
            {detail.artifacts && <TrialShowcase artifacts={detail.artifacts} />}
            {detail.evidence && <TaskEvidence evidence={detail.evidence} />}
          </div>
        </section>
      )}
    </>
  );
}

function CourseProfileSection() {
  const m = MARATHON_ANATOMY;
  return (
    <section id="course">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§04 / The course</div>
          <h2 className="section-title">A marathon is run in the shell.</h2>
        </div>
        <div className="section-body" style={{ marginBottom: 28 }}>
          <div className="sb-side">
            <div className="label-row">Actions<span>Median {m.medianActions} tool calls per trial; the busiest 10% top {m.p90Actions}.</span></div>
            <div className="label-row">Shell<span>{m.shellPct}% of all logged actions are terminal commands — agents barely touch editor tools.</span></div>
            <div className="label-row">Repetition<span>Terminus-2 repeats an earlier (function, args) call {HEADLINE.duplicationTerminusPct}% of the time; tool errors run {HEADLINE.toolErrorRateRange}.</span></div>
          </div>
          <div>
            <p style={{ fontSize: 15, color: "var(--ink-2)", margin: "0 0 18px", maxWidth: 600 }}>
              Across {m.nTrajectories.toLocaleString()} logged rollouts and{" "}
              {Math.round(m.totalActions / 1000)}K tool calls, the work is overwhelmingly
              terminal-driven: agents spend a marathon inspecting the tree, running and
              rebuilding, and babysitting long-lived processes far more than they edit.
              One in eight shell calls is pure process control — starting servers,
              Ctrl-C, <code>sleep</code>, <code>kill</code> — the tax of keeping a
              multi-hour environment alive.
            </p>
            <ShellMixBars />
            <div className="split-chips" style={{ marginTop: 18 }}>
              <div className="split-chip">Logged trajectories: <strong>{m.nTrajectories.toLocaleString()}</strong></div>
              <div className="split-chip">Median actions / trial: <strong>{m.medianActions}</strong></div>
              <div className="split-chip">Reasoning steps (median): <strong>{m.medianSteps}</strong></div>
              <div className="split-chip">Shell share: <strong>{m.shellPct}%</strong></div>
            </div>
          </div>
        </div>
      </div>
    </section>);

}

function Findings() {
  return (
    <section id="findings">
      <div className="container">
        <div className="section-head">
          <div className="section-no"><span className="dot">●</span>§05 / Failure modes</div>
          <h2 className="section-title">Selected failure modes.</h2>
        </div>

        <div>
          {CASE_STUDIES.map((c) => {
            const [agent, model] = c.config.split(" · ");
            const taskId = c.trial.replace(/-\d+$/, "");
            const task = TASKS.find((t) => t.id === taskId);
            const color = bucketColor(c.bucket);
            return (
              <div className="trace" key={c.trial} style={{ borderLeftColor: color }}>
                <div className="tr-head">
                  <span className="tr-bucket" style={{ color }}>{c.bucket}</span>
                  <span className="tr-id">
                    <BrandLogo name={model} />
                    <span className="lb-model">{model}</span>
                    <span className="lb-sep">/</span>
                    <span className="lb-agent">{agent}</span>
                  </span>
                  <a className="tr-task" href={`#trajectory/${encodeURIComponent(c.trial)}`}>
                    {task ? task.title : taskId} <span className="tr-arr">↗</span>
                  </a>
                </div>
                <div className="tr-quote">{c.pattern}</div>
              </div>
            );
          })}
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
          <div className="section-no"><span className="dot">●</span>§06 / Paper</div>
          <h2 className="section-title">If SWE-Marathon is useful,<br />please cite us.</h2>
        </div>
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
              Apache 2.0.
            </p>
          </div>
          <div>
            <div className="foot-h">Project</div>
            <div className="foot-list">
              <a href="#leaderboard">Leaderboard</a>
              <a href="#tasks">Tasks</a>
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
      <Findings />
      <Citation />
      <Footer />
    </>);

}

export default App;
