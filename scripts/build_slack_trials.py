#!/usr/bin/env python3
"""Build slack-clone trial trajectories + leaderboard index from ralphbench-logs.

Reads the canonical slack-clone trial logs (ATIF-v1.2 agent trajectories +
verifier metrics), converts each trajectory into the compact row format the
website's TrajectoryExplorer consumes, and emits:

  * public/trajectories/<trial>.json   — one per trial: {trial, rows:[...]}
  * src/slack-trials.json              — leaderboard configs + per-trial metadata

This is a DEV-ONLY tool. Its outputs are committed to the repo, so the built
site never reads the logs folder (or S3) at runtime — the trajectories ship
bundled inside `dist/`. The logs location is resolved at build time only, from
(in priority order): the --logs flag, the $RALPHBENCH_LOGS env var, or a
sibling `ralphbench-logs/` checkout next to this repo.

Run from anywhere:

    python3 swe-marathon-site/scripts/build_slack_trials.py [--logs PATH]
    RALPHBENCH_LOGS=/path/to/ralphbench-logs python3 .../build_slack_trials.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib

SITE = pathlib.Path(__file__).resolve().parent.parent


def default_logs() -> pathlib.Path:
    """Resolve the logs dir at build time without baking in any personal path."""
    env = os.environ.get("RALPHBENCH_LOGS")
    if env:
        root = pathlib.Path(env).expanduser()
        return root / "slack-clone" if root.name != "slack-clone" else root
    # Fall back to a `ralphbench-logs` checkout sitting next to the repo root.
    for base in (SITE.parent, SITE.parent.parent, pathlib.Path.cwd()):
        cand = base / "ralphbench-logs" / "slack-clone"
        if cand.exists():
            return cand
    return pathlib.Path("ralphbench-logs/slack-clone")


DEFAULT_LOGS = default_logs()

# Pretty display names, matching the existing site copy.
AGENTS = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "terminus-2": "Terminus 2",
    "gemini-cli": "Gemini CLI",
    "kimi-cli": "Kimi CLI",
}
MODELS = {
    "anthropic/claude-opus-4-7": "Claude Opus 4.7",
    "openai/gpt-5.5": "GPT-5.5",
    "gemini/gemini-3.1-pro-preview": "Gemini 3.1 Pro",
    "openrouter/deepseek/deepseek-v4-pro": "DeepSeek V4 Pro",
    "openrouter/minimax/minimax-m2.7": "MiniMax M2.7",
    "openrouter/moonshotai/kimi-k2.6": "Kimi K2.6",
    "openrouter/z-ai/glm-5.1": "GLM 5.1",
}
# Reference agents excluded from the ranked leaderboard.
SKIP_AGENTS = {"oracle", "nop"}

# Trials that also have a deployed, interactive Railway app.
LIVE_URLS = {
    "slack-clone-217": "https://swe-marathon-slack-trial-1-production.up.railway.app/",
    "slack-clone-219": "https://swe-marathon-slack-trial-2-production.up.railway.app/",
    "slack-clone-293": "https://swe-marathon-slack-trial-3-production.up.railway.app/",
}


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def fmt_duration(started: str, finished: str) -> str:
    try:
        a = dt.datetime.fromisoformat(started)
        b = dt.datetime.fromisoformat(finished)
        secs = max(0, int((b - a).total_seconds()))
    except Exception:
        return ""
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def first_line(s: str, n: int = 96) -> str:
    s = (s or "").strip()
    line = s.splitlines()[0].strip() if s else ""
    if len(line) > n:
        line = line[: n - 1].rstrip() + "…"
    return line


# Map each scaffold's raw tool vocabulary onto the canonical kinds the site
# colours + categorises (TOOL_META in App.jsx), and normalise arguments into the
# keys StepBody knows how to render richly (command / content / file_path /
# old_string+new_string / pattern / subject+description).
SHELL_FNS = {"bash", "shell", "exec_command", "run_shell_command", "bash_command", "sh"}
WRITE_FNS = {"write", "write_file", "create_file", "create", "fs_write"}
EDIT_FNS = {"edit", "replace", "str_replace", "edit_file", "apply_patch", "patch"}
READ_FNS = {"read", "read_file", "view", "open", "cat"}
GREP_FNS = {"grep", "search_file_content", "ripgrep", "rg"}
SEARCH_FNS = {"toolsearch", "tool_search", "web_search", "search"}
DONE_FNS = {"mark_task_complete", "task_complete", "complete", "finish", "submit"}
PLAN_CREATE_FNS = {"taskcreate", "update_topic", "set_plan", "create_plan"}
PLAN_UPDATE_FNS = {"taskupdate", "update_plan", "update_task"}


def normalize(fn: str, args: dict):
    """Return (kind, normalized_args, title) for one tool call."""
    f = (fn or "").strip()
    fl = f.lower()
    a = args if isinstance(args, dict) else {}

    if fl in SHELL_FNS:
        cmd = a.get("command") or a.get("cmd") or a.get("keystrokes") or a.get("input") or ""
        na = {"command": cmd}
        if a.get("workdir"):
            na["workdir"] = a["workdir"]
        if a.get("description"):
            na["description"] = a["description"]
        title = a.get("description") or first_line(cmd) or "shell"
        return "Bash", na, title
    if fl == "write_stdin":
        chars = a.get("chars") or a.get("input") or ""
        return "Bash", {"command": chars}, "stdin · " + (first_line(chars) or "(keys)")
    if fl in WRITE_FNS:
        fp = a.get("file_path") or a.get("path") or "file"
        return "Write", {"file_path": fp, "content": a.get("content", "")}, fp
    if fl == "apply_patch" or fl == "patch":
        patch = a.get("input") or a.get("patch") or a.get("content") or ""
        return "Edit", {"file_path": "patch", "content": patch}, "apply patch · " + (first_line(patch) or "diff")
    if fl in EDIT_FNS:
        fp = a.get("file_path") or a.get("path") or ""
        na = {}
        if fp:
            na["file_path"] = fp
        na["old_string"] = a.get("old_string", "")
        na["new_string"] = a.get("new_string", "")
        return "Edit", na, a.get("instruction") or fp or "edit"
    if fl in READ_FNS:
        fp = a.get("file_path") or a.get("path") or a.get("absolute_path") or "file"
        return "Read", {"file_path": fp}, fp
    if fl in GREP_FNS:
        pat = a.get("pattern") or a.get("query") or ""
        na = {"pattern": pat}
        if a.get("path"):
            na["path"] = a["path"]
        return "Grep", na, pat or "search"
    if fl in SEARCH_FNS:
        q = a.get("query") or a.get("pattern") or ""
        return "ToolSearch", {"query": q}, q or "search tools"
    if fl in PLAN_CREATE_FNS:
        subj = a.get("subject") or a.get("title") or ""
        desc = a.get("description") or a.get("summary") or a.get("strategic_intent") or ""
        return "TaskCreate", {"subject": subj, "description": desc}, desc or subj or "plan"
    if fl in PLAN_UPDATE_FNS:
        na = {"subject": a.get("subject", ""), "description": a.get("description", "")}
        st = a.get("status") or a.get("state")
        title = (f"status · {st}" if st else (a.get("subject") or "update plan"))
        return "TaskUpdate", na, title
    if fl in DONE_FNS:
        msg = a.get("summary") or a.get("message") or a.get("reason") or ""
        return "Submit", {"subject": "Mark task complete", "description": msg}, "Mark task complete"

    # Unknown tool: keep its name but still surface anything useful.
    return (f or "Tool"), a, (first_line(a.get("description") or a.get("command") or "") or (f or "Tool"))


def convert_trajectory(raw: dict) -> list[dict]:
    """ATIF steps -> compact rows the site renders (one row per tool call)."""
    rows: list[dict] = []
    for step in raw.get("steps", []):
        calls = step.get("tool_calls") or []
        if not calls:
            continue
        message = step.get("message") or ""
        for ci, call in enumerate(calls, start=1):
            fn = call.get("function_name") or step.get("extra", {}).get("tool_use_name") or "Tool"
            kind, norm_args, title = normalize(fn, call.get("arguments") or {})
            detail = (
                "Agent message:\n" + message
                + "\n\nTool arguments:\n" + json.dumps(norm_args, indent=2, ensure_ascii=False)
            )
            rows.append({
                "step": step.get("step_id"),
                "call": ci,
                "kind": kind,
                "title": title,
                "detail": detail,
            })
    return rows


def attempt_dir(trial_dir: pathlib.Path, meta: dict) -> pathlib.Path | None:
    cand = meta.get("canonical_attempt_dir")
    if cand and (trial_dir / cand).is_dir():
        return trial_dir / cand
    hits = sorted(trial_dir.glob("task-*"))
    return hits[0] if hits else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", type=pathlib.Path, default=DEFAULT_LOGS)
    args = ap.parse_args()
    logs = args.logs

    manifest = json.loads((logs / "_manifest.json").read_text())
    trials = manifest["trials"]

    out_traj = SITE / "public" / "trajectories"
    out_traj.mkdir(parents=True, exist_ok=True)

    configs: dict[tuple[str, str], list[dict]] = {}
    n_written = 0
    for trial_key, meta in trials.items():
        agent_raw = meta.get("agent", "")
        model_raw = meta.get("model", "")
        if agent_raw in SKIP_AGENTS:
            continue
        agent = AGENTS.get(agent_raw, agent_raw)
        model = MODELS.get(model_raw, model_raw)

        trial_dir = logs / trial_key
        adir = attempt_dir(trial_dir, meta)
        if adir is None:
            print(f"  ! no attempt dir for {trial_key}")
            continue

        traj_path = adir / "agent" / "trajectory.json"
        metrics_path = adir / "verifier" / "metrics.json"
        if not traj_path.exists():
            print(f"  ! no trajectory for {trial_key}")
            continue

        raw = json.loads(traj_path.read_text())
        rows = convert_trajectory(raw)

        metrics = {}
        if metrics_path.exists():
            metrics = json.loads(metrics_path.read_text())

        short = meta.get("source_trial_name") or trial_key
        (out_traj / f"{short}.json").write_text(
            json.dumps({"trial": short, "rows": rows}, ensure_ascii=False)
        )
        n_written += 1

        in_tok = meta.get("input_tokens", 0) or 0
        out_tok = meta.get("output_tokens", 0) or 0
        correctness = metrics.get("correctness_partial_score") or 0.0
        ux = metrics.get("ux_partial_score") or 0.0
        partial = metrics.get("partial_score")
        if partial is None:
            partial = meta.get("reward") or 0.0
        reward = meta.get("reward") or 0.0
        cost = meta.get("cost_usd") or 0.0
        trial_entry = {
            "id": short,
            "trial": short,
            "agent": agent,
            "model": model,
            "reward": round(reward, 3),
            "partial": round(partial, 3),
            "correctness": round(correctness, 3),
            "ux": round(ux, 3),
            "gates": f"{metrics.get('gates_passed', 0)} / {metrics.get('gates_total', 5)}",
            "tokens": fmt_tokens(in_tok + out_tok),
            "cost": f"${cost:.2f}",
            "duration": fmt_duration(meta.get("started_at", ""), meta.get("finished_at", "")),
            "startedAt": (meta.get("started_at") or "")[:16].replace("T", " ") + " UTC",
            "status": meta.get("status", ""),
            "trajectoryUrl": f"/trajectories/{short}.json",
            "steps": len(rows),
        }
        if short in LIVE_URLS:
            trial_entry["liveUrl"] = LIVE_URLS[short]
        configs.setdefault((agent, model), []).append(trial_entry)

    # Aggregate per config and rank by mean partial score.
    out_configs = []
    for (agent, model), tr in configs.items():
        tr.sort(key=lambda t: (-t["partial"], -t["ux"]))
        n = len(tr)
        mean = lambda key: round(sum(t[key] for t in tr) / n, 3) if n else 0.0
        n_pass = sum(1 for t in tr if t["reward"] >= 1.0)
        out_configs.append({
            "agent": agent,
            "model": model,
            "n": n,
            "binary": f"{n_pass} / {n}",
            "partial": mean("partial"),
            "best": round(max(t["partial"] for t in tr), 3) if tr else 0.0,
            "correctness": mean("correctness"),
            "ux": mean("ux"),
            "trials": tr,
        })
    out_configs.sort(key=lambda c: (-c["partial"], -c["ux"], -c["best"]))
    for i, c in enumerate(out_configs, start=1):
        c["rank"] = i

    index = {
        "task": "slack-clone",
        "note": "Tasks are binary reward. Uncalibrated partial scores show how far each agent progressed.",
        "configs": out_configs,
    }
    (SITE / "src" / "slack-trials.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    print(f"Wrote {n_written} trajectory files and {len(out_configs)} configs to slack-trials.json")


if __name__ == "__main__":
    main()
