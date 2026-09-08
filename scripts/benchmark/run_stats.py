#!/usr/bin/env python3
"""Per-category statistics for an archived run, with uncertainty on every rate.

    scripts/benchmark/run_stats.py <run-dir> [--json]

score_run.py answers "what did this run score". This answers "how much of
that number can you trust, and does it hold within each target application
or only on average". It reuses score_run.py's row loading and its score()
function verbatim, calling score() once on every attempt and once per
category, so the overall and per-category numbers can never drift from each
other or from `score_run.py`'s own output.

## Category, and why it is resolved rather than parsed from the id

A problem's target application (hotel_reservation / social_network /
astronomy_shop / ...) is the natural grouping: it is what the paper itself
groups by, and it is a real property of the problem, not an invented one.

The problem_id string is not a reliable source for it. Checked directly:
6 of the 21 sregym-lite ids (network_policy_block, namespace_memory_limit,
kafka_poison_pill_hol_block, valkey_auth_disruption,
unschedulable_incorrect_port_assignment, edge_request_filter_cpu_saturation)
carry no app name anywhere in the id, and across the full 123-entry registry
58 ids fail a suffix match. The app is determined by what the problem class
actually constructs, not by its name.

So this reads it from the source of truth: registry.py's PROBLEM_REGISTRY
maps each id to a class, sometimes via a lambda passing an explicit
app_name= (used directly), sometimes via a bare class reference whose
__init__ either defaults app_name= or hardcodes `super().__init__(app=X())`
/ `self.app = X()`. Both shapes are real and both occur in sregym-lite
(NetworkPolicyBlock takes no app_name parameter at all; it hardcodes
HotelReservation in its own __init__). Resolving this statically means
parsing the actual class body, not guessing from a string a second time.

An id this cannot resolve is never silently dropped into a wrong or empty
category: it is reported as unresolved, both on stderr and in the output, so
a broken resolution reads as broken rather than as a quietly wrong grouping.

## Statistical significance

Two different intervals, because a rate and a duration are different kinds
of measurement:

- Every accuracy figure (diagnosis / mitigation / e2e, strict and graded) is
  a Wilson score interval, not a Wald interval built from mean +/- 1.96*sd.
  Wald can produce a confidence bound outside [0, 100]% and is a poor
  approximation exactly where these numbers live, small n and rates near
  either edge. Wilson does not have either problem and is what this size of
  sample calls for.
- TTD, TTM, and every token/tool-call count use a t-distribution interval,
  not a normal one, because n is frequently in the single digits per
  category (3 attempts per problem is the common case). A t critical value
  is wider than 1.96 at low n specifically to account for the extra
  uncertainty in estimating sd from a small sample; substituting the normal
  value there would silently understate the interval.

A category with n < 3 gets no interval at all (labelled, not printed as a
false 0-width band), because an interval computed from fewer than two
degrees of freedom is not a real result.

None of this replaces judgment. A wide interval says the number is not
pinned down, not that it should be ignored -- that is a fact about how much
data exists, not a defect in the run.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_run import _val, load, score  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "sregym" / "conductor" / "problems" / "registry.py"

# Hand-verified against sregym/service/apps/*.py. resolve_categories() below
# reports (never silently swallows) any app class it finds that is missing
# from this table, so an app added later fails loud rather than mislabeling.
_APP_CLASS_TO_CATEGORY = {
    "AstronomyShop": "astronomy_shop",
    "HotelReservation": "hotel_reservation",
    "SocialNetwork": "social_network",
    "TrainTicket": "train_ticket",
    "FlightTicket": "flight_ticket",
    "FleetCast": "fleet_cast",
    "BlueprintHotelReservation": "blueprint_hotel_reservation",
    "TiDBClusterDeployer": "tidb_cluster",
}


def _dict_literal(tree: ast.AST, attr_name: str) -> ast.Dict | None:
    """The ast.Dict assigned to self.<attr_name> or a module-level <attr_name>."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for t in node.targets:
                name = t.attr if isinstance(t, ast.Attribute) else (t.id if isinstance(t, ast.Name) else None)
                if name == attr_name:
                    return node.value
    return None


def _import_map(tree: ast.AST) -> dict[str, str]:
    """Name this file imports -> the dotted module it came from."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = node.module
    return out


def _module_file(dotted: str) -> Path:
    return REPO_ROOT / Path(*dotted.split(".")).with_suffix(".py")


def _call_class_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _app_from_init(init_node: ast.FunctionDef) -> tuple[str, str] | None:
    """What app this __init__ constructs: an explicit app_name default, or the
    class passed to super().__init__(app=...) / assigned to self.app."""
    args = init_node.args
    named = args.args[len(args.args) - len(args.defaults) :] if args.defaults else []
    for arg, default in zip(named, args.defaults, strict=True):
        if arg.arg == "app_name" and isinstance(default, ast.Constant) and isinstance(default.value, str):
            return ("app_name", default.value)

    for node in ast.walk(init_node):
        if isinstance(node, ast.Call):
            is_super_init = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "__init__"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "super"
            )
            if is_super_init:
                for kw in node.keywords:
                    if kw.arg == "app":
                        cls = _call_class_name(kw.value)
                        if cls:
                            return ("app_class", cls)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Attribute)
                    and t.attr == "app"
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "self"
                ):
                    cls = _call_class_name(node.value)
                    if cls:
                        return ("app_class", cls)
    return None


def resolve_categories(problem_ids: set[str]) -> tuple[dict[str, str], list[str]]:
    """problem_id -> target-application category, plus any id it could not resolve."""
    reg_tree = ast.parse(REGISTRY_PATH.read_text())
    reg_dict = _dict_literal(reg_tree, "PROBLEM_REGISTRY")
    reg_imports = _import_map(reg_tree)

    entries: dict[str, tuple[str, str | None]] = {}
    if reg_dict is not None:
        for k, v in zip(reg_dict.keys, reg_dict.values, strict=True):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            pid = k.value
            if isinstance(v, ast.Name):
                entries[pid] = (v.id, None)
            elif isinstance(v, ast.Lambda) and isinstance(v.body, ast.Call) and isinstance(v.body.func, ast.Name):
                explicit = None
                for kw in v.body.keywords:
                    if kw.arg == "app_name" and isinstance(kw.value, ast.Constant):
                        explicit = kw.value.value
                entries[pid] = (v.body.func.id, explicit)
            # A lambda whose body is not a single Call (e.g. a multi-problem
            # composite) is left unresolved rather than guessed at.

    categories: dict[str, str] = {}
    unresolved: list[str] = []
    module_cache: dict[str, ast.AST | None] = {}

    for pid in sorted(problem_ids):
        if pid not in entries:
            unresolved.append(pid)
            continue
        cls, explicit = entries[pid]
        if explicit:
            categories[pid] = explicit
            continue

        module = reg_imports.get(cls)
        if not module:
            unresolved.append(pid)
            continue
        if module not in module_cache:
            f = _module_file(module)
            module_cache[module] = ast.parse(f.read_text()) if f.exists() else None
        mod_tree = module_cache[module]
        if mod_tree is None:
            unresolved.append(pid)
            continue

        found = None
        for node in ast.walk(mod_tree):
            if isinstance(node, ast.ClassDef) and node.name == cls:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        found = _app_from_init(item)
                break
        if found is None:
            unresolved.append(pid)
            continue

        kind, value = found
        if kind == "app_name":
            categories[pid] = value
        else:
            label = _APP_CLASS_TO_CATEGORY.get(value)
            if label:
                categories[pid] = label
            else:
                categories[pid] = f"unmapped_app_class:{value}"
                unresolved.append(pid)

    return categories, unresolved


# ---------------------------------------------------------------------------
# Statistics


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial rate, as a percentage."""
    if n == 0:
        return None
    phat = successes / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    lo = max(0.0, center - half) * 100
    hi = min(1.0, center + half) * 100
    return (round(lo, 1), round(hi, 1))


# Two-tailed 95% t critical values, df 1-30. A df beyond this table uses the
# normal approximation (1.96), which is accurate to three figures by df=30.
_T_TABLE = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def _t_crit(df: int) -> float:
    return _T_TABLE.get(df, 1.96)


def stats_ci(xs: list[float]) -> dict | None:
    """n / mean / median / sd, plus a 95% CI on the mean when df >= 1."""
    if not xs:
        return None
    n = len(xs)
    mean = statistics.mean(xs)
    out = {"n": n, "mean": round(mean, 1), "median": round(statistics.median(xs), 1)}
    if n > 1:
        sd = statistics.stdev(xs)
        out["sd"] = round(sd, 1)
        se = sd / math.sqrt(n)
        tc = _t_crit(n - 1)
        out["ci95"] = (round(mean - tc * se, 1), round(mean + tc * se, 1))
    else:
        out["sd"] = 0.0
        out["ci95"] = None
    return out


def _floats(rows: list[dict], key: str) -> list[float]:
    out = []
    for r in rows:
        try:
            out.append(float(_val(r, key)))
        except ValueError:
            continue
    return out


_CONTINUOUS_COLUMNS = {
    "ttd_seconds": "TTL",
    "ttm_seconds": "TTM",
    "llm_calls": "llm_calls",
    "tool_calls": "tool_calls",
    "tokens_input": "tokens_input",
    "tokens_output": "tokens_output",
    "tokens_cache_read": "tokens_cache_read",
    "tokens_cache_write": "tokens_cache_write",
}


def rate_with_ci(correct: int, total: int) -> dict | None:
    if total == 0:
        return None
    return {
        "pct": round(100.0 * correct / total, 1),
        "n": total,
        "correct": correct,
        "ci95": wilson_ci(correct, total),
    }


def group_report(rows: list[dict]) -> dict:
    """Everything score_run.py's score() gives, plus a Wilson CI on each rate
    and a t-interval on every continuous measure, for one set of rows."""
    s = score(rows)
    out = {
        "attempts": s["attempts"],
        "problems": s["problems"],
        "diagnosis": {
            **s["diagnosis"],
            "strict_ci95": wilson_ci(s["diagnosis"]["correct"], s["attempts"]),
            "graded_ci95": wilson_ci(s["diagnosis"]["correct"], s["diagnosis"]["graded"]),
        },
        "mitigation": {
            **s["mitigation"],
            "strict_ci95": wilson_ci(s["mitigation"]["correct"], s["attempts"]),
            "graded_ci95": wilson_ci(s["mitigation"]["correct"], s["mitigation"]["graded"]),
        },
        "e2e": {
            **s["e2e"],
            "strict_ci95": wilson_ci(s["e2e"]["correct"], s["attempts"]),
            "graded_ci95": wilson_ci(s["e2e"]["correct"], s["e2e"]["graded"]),
        },
        "censored": s["censored"],
    }
    for label, column in _CONTINUOUS_COLUMNS.items():
        out[label] = stats_ci(_floats(rows, column))
    return out


# ---------------------------------------------------------------------------
# Rendering


def _pct(value) -> str:
    return "  n/a" if value is None else f"{value:>5.1f}%"


def _ci(interval) -> str:
    """Plain-language framing: not "the 95% CI is (x, y)" but "could reasonably
    be anywhere from x to y", which needs no statistics background to read."""
    if interval is None:
        return "(too few attempts to say)"
    return f"(likely {interval[0]:.1f}-{interval[1]:.1f})"


def _render_group(name: str, g: dict) -> list[str]:
    lines = [f"  {name}  -- {g['attempts']} attempts, {g['problems']} problems"]
    for metric, key in (("Diagnosis", "diagnosis"), ("Mitigation", "mitigation"), ("End to end", "e2e")):
        m = g[key]
        lines.append(
            f"    {metric:<11} strict {_pct(m['strict_pct'])} {_ci(m['strict_ci95']):<20}"
            f" graded {_pct(m['graded_pct'])} {_ci(m['graded_ci95']):<20} (from {g['attempts']} attempts)"
        )
    for label, col_key in (("TTD", "ttd_seconds"), ("TTM", "ttm_seconds")):
        st = g[col_key]
        if st is None:
            lines.append(f"    {label:<11} not enough data")
        else:
            ci = _ci(st["ci95"])
            lines.append(
                f"    {label:<11} typically {st['mean']:>8.1f}s  (middle value {st['median']:>8.1f}s)  {ci}  (from {st['n']} attempts)"
            )
    # Every column here except tool_calls comes from the per-attempt token
    # delta, which overcounts: see the INFLATED note below. Marked inline
    # rather than only in a footer, because a mean with a confidence interval
    # beside it reads as more authoritative than a bare number, and someone
    # skimming one row should not have to find the footer to learn it is wrong.
    inflated = {"llm_calls", "tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write"}
    any_inflated = False

    for label, col_key in (
        ("LLM calls", "llm_calls"),
        ("Tool calls", "tool_calls"),
        ("Tokens in", "tokens_input"),
        ("Tokens out", "tokens_output"),
        ("Cache read", "tokens_cache_read"),
        ("Cache write", "tokens_cache_write"),
    ):
        st = g[col_key]
        if st is None:
            lines.append(f"    {label:<11} not enough data")
        else:
            ci = _ci(st["ci95"])
            mark = ""
            if col_key in inflated:
                mark = "  [INFLATED]"
                any_inflated = True
            lines.append(
                f"    {label:<11} typically {st['mean']:>10.1f}  (middle value {st['median']:>10.1f})  {ci}  (from {st['n']} attempts){mark}"
            )

    if any_inflated:
        lines.append("")
        lines.append("    [INFLATED] brain-go serves all slots on a box from one process and its")
        lines.append("    Prometheus counters are process-wide, so each attempt's before/after token")
        lines.append("    delta also picks up whatever the other slots on that box spent at the same")
        lines.append("    time. Measured at about 2.41x on run-20260908-0739. Tool calls are exempt:")
        lines.append("    the driver counts those off its own SSE stream. For a token figure you can")
        lines.append("    trust, use the whole-run total from score_run.py, which is taken per counter")
        lines.append("    series and is not affected.")

    lines.append("")
    return lines


def render(overall: dict, by_category: dict[str, dict], unresolved: list[str]) -> str:
    out = []
    out.append("=" * 78)
    out.append("OVERALL")
    out.append("=" * 78)
    out.extend(_render_group("all attempts", overall))

    out.append("=" * 78)
    out.append("BY CATEGORY (target application)")
    out.append("=" * 78)
    for cat in sorted(by_category):
        out.extend(_render_group(cat, by_category[cat]))

    if unresolved:
        out.append("-" * 78)
        out.append(f"{len(unresolved)} problem id(s) could not be resolved to a category (excluded above):")
        for pid in unresolved:
            out.append(f"  - {pid}")
        out.append("These attempts still count in OVERALL; only the per-category split omits them.")

    out.append("-" * 78)
    out.append("How to read the ranges: each number has a range next to it showing how")
    out.append("much it could reasonably move if this exact test ran again. A narrow")
    out.append('range means the number is solid. A wide range, or "too few attempts to')
    out.append('say", means there is not yet enough data to trust the number on its own')
    out.append("-- treat it as a rough estimate, not a precise score. That is a fact")
    out.append("about how much data exists, not a sign that the run went wrong.")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Per-category statistics for an archived run.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows = load(args.run_dir)
    if not rows:
        print(f"no result rows found under {args.run_dir}", file=sys.stderr)
        return 1

    problem_ids = {_val(r, "problem_id") for r in rows}
    categories, unresolved = resolve_categories(problem_ids)
    if unresolved:
        print(f"warning: {len(unresolved)} problem id(s) unresolved to a category: {unresolved}", file=sys.stderr)

    overall = group_report(rows)

    grouped_rows: dict[str, list[dict]] = {}
    for r in rows:
        pid = _val(r, "problem_id")
        cat = categories.get(pid)
        if cat is None:
            continue
        grouped_rows.setdefault(cat, []).append(r)
    by_category = {cat: group_report(rs) for cat, rs in grouped_rows.items()}

    if args.json:
        print(json.dumps({"overall": overall, "by_category": by_category, "unresolved": unresolved}, indent=2))
    else:
        print(render(overall, by_category, unresolved))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
