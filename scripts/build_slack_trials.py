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
import re

SITE = pathlib.Path(__file__).resolve().parent.parent


def load_metrics_tolerant(path: pathlib.Path) -> dict:
    """Load a metrics.json, repairing a few malformed-but-recoverable cases.

    Some verifier runs emit non-standard JSON (e.g. a bare ``.674`` float with
    no leading zero). Try strict parse first, then a minimal regex repair.
    """
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # ": .674" / "[.5," -> add the leading zero JSON requires.
        repaired = re.sub(r"([:\[,]\s*)(\.\d)", r"\g<1>0\g<2>", text)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            print(f"  ! unparseable metrics: {path}")
            return {}


def load_result_stage_metrics(path: pathlib.Path) -> dict:
    """Normalize top-level result.json stage metrics into verifier metrics keys.

    Newer product-clone imports may not have verifier/metrics.json in the
    selected attempt directory, but Oddish's top-level result.json still carries
    ``stage:correctness`` and ``stage:ux``. The site expects the older
    verifier/metrics.json shape, where partial_score is the average of those two
    components.
    """
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    evals = (result.get("stats") or {}).get("evals") or {}
    for ev in evals.values():
        metric_rows = ev.get("metrics") or []
        if not metric_rows:
            continue
        metrics = metric_rows[0] or {}
        correctness = metrics.get("stage:correctness")
        ux = metrics.get("stage:ux")
        if correctness is None and ux is None:
            continue
        correctness = float(correctness or 0.0)
        ux = float(ux or 0.0)
        return {
            "partial_score": 0.5 * correctness + 0.5 * ux,
            "correctness_partial_score": correctness,
            "ux_partial_score": ux,
            "reward": metrics.get("reward", 0.0),
        }
    return {}


def default_logs() -> pathlib.Path:
    """Resolve the ralphbench-logs ROOT at build time (no personal path baked in)."""
    env = os.environ.get("RALPHBENCH_LOGS")
    if env:
        root = pathlib.Path(env).expanduser()
        # Accept either the logs root or a single task subdir; normalise to root.
        return root.parent if (root / "_manifest.json").exists() else root
    # Fall back to a `ralphbench-logs` checkout sitting next to the repo root.
    for base in (SITE.parent, SITE.parent.parent, pathlib.Path.cwd(),
                 pathlib.Path.home() / "Documents"):
        cand = base / "ralphbench-logs"
        if cand.exists():
            return cand
    return pathlib.Path("ralphbench-logs")


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
    "anthropic/claude-opus-4-8": "Claude Opus 4.8",
    "openai/gpt-5.5": "GPT-5.5",
    "gemini/gemini-3.1-pro-preview": "Gemini 3.1 Pro",
    "gemini/gemini-3.5-flash": "Gemini 3.5 Flash",
    "openrouter/deepseek/deepseek-v4-pro": "DeepSeek V4 Pro",
    "openrouter/minimax/minimax-m2.7": "MiniMax M2.7",
    "openrouter/moonshotai/kimi-k2.6": "Kimi K2.6",
    "openrouter/z-ai/glm-5.1": "GLM 5.1",
}
# Reference agents excluded from the ranked leaderboard.
SKIP_AGENTS = {"oracle", "nop"}

# The four CUA product-clone tasks. For each, the three trials that also have a
# deployed, interactive Railway app (the artifacts shown in the live viewer).
CUA_TASKS = {
    "slack-clone": {
        "slack-clone-217": "https://swe-marathon-slack-trial-1-production.up.railway.app/",
        "slack-clone-219": "https://swe-marathon-slack-trial-2-production.up.railway.app/",
        "slack-clone-293": "https://swe-marathon-slack-trial-3-production.up.railway.app/",
    },
    "excel-clone": {
        "excel-clone-220": "https://swe-marathon-excel-trial-1-production.up.railway.app/",
        "excel-clone-223": "https://swe-marathon-excel-trial-2-production.up.railway.app/",
        "excel-clone-251": "https://swe-marathon-excel-trial-3-production.up.railway.app/",
    },
    "s3-clone": {
        "s3-clone-214": "https://swe-marathon-s3-trial-1-production.up.railway.app/console/",
        "s3-clone-144": "https://swe-marathon-s3-trial-2-production.up.railway.app/console/",
        "s3-clone-172": "https://swe-marathon-s3-trial-3-production.up.railway.app/console/",
    },
    "mastodon-clone": {
        "mastodon-clone-173": "https://swe-marathon-mastodon-trial-1-production.up.railway.app/",
        "mastodon-clone-177": "https://swe-marathon-mastodon-trial-2-production.up.railway.app/",
        "mastodon-clone-207": "https://swe-marathon-mastodon-trial-3-production.up.railway.app/",
    },
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


def _canonical_attempt_names(trial_dir: pathlib.Path) -> list[str]:
    """Attempt-dir basenames the run's result.json marks as the real trial(s)."""
    rj = trial_dir / "result.json"
    if not rj.exists():
        return []
    try:
        r = json.loads(rj.read_text())
    except Exception:
        return []
    names: list[str] = []
    for ev in r.get("stats", {}).get("evals", {}).values():
        for lst in ev.get("reward_stats", {}).get("reward", {}).values():
            if isinstance(lst, list):
                names += [str(x) for x in lst]
    return names


def attempt_dir(trial_dir: pathlib.Path, meta: dict) -> pathlib.Path | None:
    """Locate the attempt directory holding the agent trajectory.

    Harbor lays out each trial as one or more attempt subdirs. Their names vary
    ("task-<task>__<hash>" or a bare "<task>__<hash>"), and retried trials carry
    several — only one of which actually persisted a trajectory. We therefore
    pick among the subdirs that contain agent/trajectory.json, preferring the
    canonical attempt recorded in result.json, then one with verifier metrics,
    then the most recent. Globbing only "task-*" silently dropped the bare-named
    and retried attempts, so whole trials went missing from the leaderboard.
    """
    cand = meta.get("canonical_attempt_dir")
    if cand and (trial_dir / cand / "agent" / "trajectory.json").exists():
        return trial_dir / cand
    # Backward-compatible: the original glob took the first "task-*" attempt, so
    # keep that selection for every trial it already resolved (don't perturb the
    # committed CUA trajectories). Only fall through when no task-* attempt holds
    # a trajectory — that's the bare-named / retried case that went missing.
    for d in sorted(trial_dir.glob("task-*")):
        if (d / "agent" / "trajectory.json").exists():
            return d
    have_traj = [d for d in trial_dir.iterdir()
                 if d.is_dir() and (d / "agent" / "trajectory.json").exists()]
    if not have_traj:
        return None
    if len(have_traj) == 1:
        return have_traj[0]
    canon = set(_canonical_attempt_names(trial_dir))
    for d in have_traj:
        if d.name in canon:
            return d
    with_metrics = [d for d in have_traj if (d / "verifier" / "metrics.json").exists()]
    pool = with_metrics or have_traj
    return max(pool, key=lambda d: (d / "agent" / "trajectory.json").stat().st_mtime)


def build_task(task: str, logs_root: pathlib.Path, out_traj: pathlib.Path) -> None:
    """Build trajectories + <task>-trials.json for one task.

    Works for any task in ralphbench-logs. Tasks listed in CUA_TASKS also get
    `liveUrl`s wired onto their three deployed trials; everything else is a
    pure trajectory/leaderboard task (no live artifact).
    """
    live_urls = CUA_TASKS.get(task, {})
    logs = logs_root / task
    manifest = json.loads((logs / "_manifest.json").read_text())
    trials = manifest["trials"]

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
            metrics = load_metrics_tolerant(metrics_path)
        if "partial_score" not in metrics:
            metrics.update(load_result_stage_metrics(trial_dir / "result.json"))

        # Clean, stable trial id like "rust-c-compiler-210". Prefer a tidy
        # source_trial_name when present; otherwise derive from the manifest
        # key ("<task>-<hash>-<num>"), dropping the internal hash segment.
        src = meta.get("source_trial_name") or ""
        if src.startswith(f"{task}-") and "__" not in src:
            short = src
        else:
            short = f"{task}-{trial_key.rsplit('-', 1)[-1]}"
        (out_traj / f"{short}.json").write_text(
            json.dumps({"trial": short, "rows": rows}, ensure_ascii=False)
        )
        n_written += 1

        in_tok = meta.get("input_tokens", 0) or 0
        out_tok = meta.get("output_tokens", 0) or 0
        partial = metrics.get("partial_score")
        if partial is None:
            partial = meta.get("reward") or 0.0
        # "Unit tests" column: CUA tasks expose a correctness sub-score; pure
        # test-suite tasks (e.g. compilers) expose only a pass_rate / partial.
        correctness = metrics.get("correctness_partial_score")
        if correctness is None:
            correctness = metrics.get("pass_rate")
        if correctness is None:
            correctness = partial
        ux = metrics.get("ux_partial_score") or 0.0
        # Tests-passed fraction: CUA gates, else the generic new_passed/new_total.
        if metrics.get("gates_total") is not None:
            gates = f"{metrics.get('gates_passed', 0)} / {metrics.get('gates_total', 0)}"
        elif metrics.get("new_total") is not None:
            gates = f"{metrics.get('new_passed', 0)} / {metrics.get('new_total', 0)}"
        elif metrics.get("total_tests") is not None:
            gates = f"{metrics.get('total_passed', 0)} / {metrics.get('total_tests', 0)}"
        else:
            gates = ""
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
            "gates": gates,
            "tokens": fmt_tokens(in_tok + out_tok),
            "cost": f"${cost:.2f}",
            "duration": fmt_duration(meta.get("started_at", ""), meta.get("finished_at", "")),
            "startedAt": (meta.get("started_at") or "")[:16].replace("T", " ") + " UTC",
            "status": meta.get("status", ""),
            "trajectoryUrl": f"/trajectories/{short}.json",
            "steps": len(rows),
        }
        if short in live_urls:
            trial_entry["liveUrl"] = live_urls[short]
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
        "task": task,
        "note": "Tasks are binary reward. Uncalibrated partial scores show how far each agent progressed.",
        "configs": out_configs,
    }
    (SITE / "src" / f"{task}-trials.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    print(f"  {task}: wrote {n_written} trajectory files and {len(out_configs)} configs to {task}-trials.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", type=pathlib.Path, default=DEFAULT_LOGS,
                    help="ralphbench-logs root (containing per-task subdirs)")
    ap.add_argument("--task", default=None,
                    help="build a single task by slug, e.g. rust-c-compiler "
                         "(default: all four CUA tasks)")
    args = ap.parse_args()

    out_traj = SITE / "public" / "trajectories"
    out_traj.mkdir(parents=True, exist_ok=True)

    tasks = [args.task] if args.task else list(CUA_TASKS)
    for task in tasks:
        build_task(task, args.logs, out_traj)


if __name__ == "__main__":
    main()
