# """
# YOUR IMPLEMENTATION GOES HERE.

# This is a stub so the harness runs on a fresh checkout (every prompt will FAIL until
# you build the system). Replace the body of `Agent.solve`. Keep the return contract in
# run.py. Use any framework/LLM you like — wire it in here.

# `Tools` below is a thin recorder around tools.py: call the tools through it and your
# `tool_calls` trace is populated automatically. Use it (or don't — but you must produce
# an accurate trace either way).
# """

# from __future__ import annotations

# import time
# from typing import Any

# import tools as _tools


# class Tools:
#     """Records every tool call into a trace list. Wrap tools.py through this."""

#     def __init__(self) -> None:
#         self.calls: list[dict[str, Any]] = []

#     def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
#         fn = getattr(_tools, name)
#         result = fn(**kwargs)
#         ok = isinstance(result, dict) and result.get("status") == "success"
#         self.calls.append({"name": name, "args": kwargs, "ok": ok})
#         return result


# class Agent:
#     def __init__(self) -> None:
#         # TODO: construct your orchestrator + sub-agents + LLM client here.
#         pass

#     def solve(self, prompt: str) -> dict[str, Any]:
#         t0 = time.time()
#         tools_rec = Tools()

#         # ================= TODO: your multi-agent system =================
#         # Parse intent. Resolve the model. Ground ambiguous terms. Orchestrate the
#         # tools in the right order. Slim tool outputs. Ask on missing input. Fail on
#         # unknown model. Never fabricate a number a tool did not return.
#         #
#         # Example of calling a tool through the recorder:
#         #     res = tools_rec.call("list_models")
#         #
#         answer = "NOT IMPLEMENTED"
#         asked_user = False
#         failed = False
#         llm_calls = 0
#         prompt_tokens = 0
#         completion_tokens = 0
#         # =================================================================

#         return {
#             "answer": answer,
#             "trace": {
#                 "tool_calls": tools_rec.calls,
#                 "llm_calls": llm_calls,
#                 "prompt_tokens": prompt_tokens,
#                 "completion_tokens": completion_tokens,
#                 "latency_s": time.time() - t0,
#                 "asked_user": asked_user,
#                 "failed": failed,
#             },
#         }

"""
Deterministic rule-based orchestrator for the budget-optimisation assignment.

Design note: this is intentionally implemented without calling an external LLM.
Intent detection, parameter extraction, and orchestration are all done with
targeted keyword/regex parsing plus explicit tool-dependency logic. This keeps
`llm_calls == 0` and `prompt_tokens == completion_tokens == 0` for every request,
which directly satisfies the "minimize LLM/token usage" requirement, while still
covering intent detection, parameter extraction, model resolution, constraint
mapping, missing-info recovery, and honest failure the assignment asks for.
(An LLM could be dropped in at the classification step below without touching
the orchestration/tool logic, if richer natural-language coverage is needed.)

Only this file was modified. `Tools` below is the same thin recorder from the
stub — every tool call goes through it so the trace is accurate automatically.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

import tools as _tools


class Tools:
    """Records every tool call into a trace list. Wrap tools.py through this."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        fn = getattr(_tools, name)
        result = fn(**kwargs)
        ok = isinstance(result, dict) and result.get("status") == "success"
        self.calls.append({"name": name, "args": kwargs, "ok": ok})
        return result


# ============================================================================ #
# Parsing helpers
# ============================================================================ #

_MODEL_ID_RE = re.compile(r"model(?:\s*id)?[\s:#]*([0-9]{5,})", re.IGNORECASE)
_BARE_MODEL_ID_RE = re.compile(r"\b([0-9]{9,})\b")

_KPI_WORDS = {"revenue": "Revenue", "conversions": "Conversions", "installs": "Installs"}
_CONSTRAINT_WORDS = ["aggressive", "moderate", "conservative", "current"]


def _extract_model_id(prompt: str) -> Optional[str]:
    m = _MODEL_ID_RE.search(prompt)
    if m:
        return m.group(1)
    m = _BARE_MODEL_ID_RE.search(prompt)
    if m:
        return m.group(1)
    return None


def _to_float(num: str, suf: Optional[str]) -> float:
    val = float(num.replace(",", ""))
    if suf:
        if suf.lower() == "k":
            val *= 1_000
        elif suf.lower() == "m":
            val *= 1_000_000
    return val


def _extract_money_amounts(prompt: str) -> list[float]:
    """Extract dollar amounts like $500k, 500k, $1M, $1,000,000.

    Requires a leading $ OR a k/M suffix, so plain large numbers used as model
    ids (e.g. "model 999999999") are never mistaken for a budget.
    """
    amounts: list[float] = []
    for m in re.finditer(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k|K|m|M)?", prompt):
        amounts.append(_to_float(m.group(1), m.group(2)))
    for m in re.finditer(r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k|K|m|M)\b", prompt):
        start = m.start()
        if start > 0 and prompt[start - 1] == "$":
            continue  # already captured by the $-prefixed pass above
        amounts.append(_to_float(m.group(1), m.group(2)))
    return amounts


def _extract_constraint_word(prompt: str) -> Optional[str]:
    low = prompt.lower()
    for w in _CONSTRAINT_WORDS:
        if w in low:
            return w.capitalize()
    return None


def _extract_kpi_word(prompt: str) -> Optional[str]:
    low = prompt.lower()
    for w, kpi in _KPI_WORDS.items():
        if w in low:
            return kpi
    return None


# ============================================================================ #
# Model helpers
# ============================================================================ #

def _slim_model(m: dict) -> dict:
    return {
        "id": m["id"],
        "modelName": m.get("modelDisplayName") or m.get("modelName"),
        "outcomeKPI": m["outcomeKPI"],
        "modelStatus": m["modelStatus"],
        "createdAt": m["createdAt"]["seconds"],
    }


def _fetch_models(tools_rec: Tools) -> list[dict]:
    res = tools_rec.call("list_models")
    if res.get("status") != "success":
        return []
    return res["data"]


def _pick_latest(models: list[dict], kpi: Optional[str] = None) -> Optional[dict]:
    candidates = [m for m in models if m["modelStatus"] == "Success"]
    if kpi:
        candidates = [m for m in candidates if m["outcomeKPI"] == kpi]
    if not candidates:
        return None
    candidates.sort(key=lambda m: m["createdAt"]["seconds"], reverse=True)
    return candidates[0]


def _resolve_latest_model(tools_rec: Tools, kpi: Optional[str] = None) -> Optional[dict]:
    return _pick_latest(_fetch_models(tools_rec), kpi=kpi)


def _resolve_model_and_budget(tools_rec: Tools, prompt: str) -> tuple[Optional[str], Optional[float]]:
    """Resolve an explicit/implicit model id and the first budget mentioned.

    Never guesses: only resolves "latest model" when the prompt actually refers
    to a model in that way; otherwise leaves model_id as None so the caller asks.
    """
    model_id = _extract_model_id(prompt)
    amounts = _extract_money_amounts(prompt)
    budget = amounts[0] if amounts else None

    low = prompt.lower()
    if not model_id and "model" in low and ("latest" in low or "recent" in low):
        kpi = "Revenue" if ("revenue model" in low or "recent revenue" in low) else None
        resolved = _resolve_latest_model(tools_rec, kpi=kpi)
        if resolved:
            model_id = str(resolved["id"])
    return model_id, budget


# ============================================================================ #
# Intent handlers — each returns (answer, asked_user, failed)
# ============================================================================ #

def _handle_list_models(tools_rec: Tools) -> tuple[str, bool, bool]:
    res = tools_rec.call("list_models")
    if res.get("status") != "success":
        return "Could not retrieve models.", False, True
    models = res["data"]
    success = [m for m in models if m["modelStatus"] == "Success"]
    success.sort(key=lambda m: m["createdAt"]["seconds"], reverse=True)
    top = [_slim_model(m) for m in success[:10]]
    lines = [f"- id={m['id']} name={m['modelName']} kpi={m['outcomeKPI']} status={m['modelStatus']}"
             for m in top]
    answer = (f"You have {len(models)} models total ({len(success)} successful). "
              f"Showing the {len(top)} most recent:\n" + "\n".join(lines))
    return answer, False, False


def _handle_current_budget(tools_rec: Tools, prompt: str) -> tuple[str, bool, bool]:
    model_id = _extract_model_id(prompt)
    if not model_id:
        model = _resolve_latest_model(tools_rec)
        if not model:
            return "I couldn't resolve a model to look up the current budget for.", False, True
        model_id = str(model["id"])

    res = tools_rec.call("get_current_budget", mmmRequestId=model_id)
    if res.get("status") != "success":
        return f"Could not retrieve current budget: {res.get('error_message')}", False, True

    data = res["data"]
    periods = data.get("mmmCurrentBudgetResponseList", [])
    lines = [f"- {p['timePeriod']}: ${p['budget']:,.0f}" for p in periods]
    answer = (f"Current budget for model {model_id} "
              f"({data.get('startDate')} to {data.get('endDate')}):\n" + "\n".join(lines))
    return answer, False, False


def _handle_locked_channels(tools_rec: Tools, prompt: str) -> tuple[str, bool, bool]:
    meta_res = tools_rec.call("channel_metadata")
    if meta_res.get("status") != "success":
        return "Could not retrieve channel metadata.", False, True
    zero_meaning = meta_res["data"]["zero_spend_meaning"]

    model_id = _extract_model_id(prompt)
    if not model_id:
        model = _resolve_latest_model(tools_rec)
        if not model:
            return "I couldn't resolve a model to check locked channels for.", False, True
        model_id = str(model["id"])

    budget_res = tools_rec.call("get_current_budget", mmmRequestId=model_id)
    if budget_res.get("status") != "success":
        return f"Could not retrieve current budget: {budget_res.get('error_message')}", False, True
    quarter_budget = next((p["budget"] for p in budget_res["data"]["mmmCurrentBudgetResponseList"]
                           if p["timePeriod"] == "quarter"), None)
    if not quarter_budget:
        return "Could not determine a baseline budget to evaluate channels against.", False, True

    d_res = tools_rec.call("run_default_optimise", mmmRequestId=model_id)
    if d_res.get("status") != "success":
        return f"Could not evaluate channels: {d_res.get('error_message')}", False, True

    current_ct = meta_res["data"]["constraint_type_ids"].get("Current", 0)
    c_res = tools_rec.call("run_constrained_optimise", mmmRequestId=model_id,
                            totalBudget=quarter_budget, constraintType=current_ct)
    if c_res.get("status") != "success":
        return f"Could not evaluate channels: {c_res.get('error_message')}", False, True

    rows = c_res["data"]["dateRangeToResponseMap"]["aggregated_aggregated"]["mmmBudgetOptimisationResponseList"]
    locked = [r["platformName"] for r in rows
              if r["platformName"] != "All Platforms" and r.get("currentBudgetData", {}).get("spend") == 0]

    if locked:
        answer = f"Locked channels for model {model_id} ({zero_meaning}): " + ", ".join(locked)
    else:
        answer = f"No locked channels found for model {model_id} ({zero_meaning})."
    return answer, False, False


def _handle_compare(tools_rec: Tools, prompt: str) -> tuple[str, bool, bool]:
    amounts = _extract_money_amounts(prompt)
    if len(amounts) < 2:
        return ("I can compare budget scenarios, but I need at least two budget "
                 "amounts to compare (e.g. \"$500k vs $750k\") — could you specify them?",
                 True, False)

    model = _resolve_latest_model(tools_rec)
    if not model:
        return "I couldn't resolve a model to compare scenarios for.", False, True
    model_id = str(model["id"])

    d_res = tools_rec.call("run_default_optimise", mmmRequestId=model_id)
    if d_res.get("status") != "success":
        return f"Could not baseline-optimise model {model_id}: {d_res.get('error_message')}", False, True

    meta_res = tools_rec.call("channel_metadata")
    current_ct = (meta_res["data"]["constraint_type_ids"].get("Current", 0)
                  if meta_res.get("status") == "success" else 0)

    labels: list[str] = []
    for amt in amounts:
        label = f"${amt:,.0f}"
        c_res = tools_rec.call("run_constrained_optimise", mmmRequestId=model_id,
                                totalBudget=amt, constraintType=current_ct)
        if c_res.get("status") != "success":
            return f"Could not optimise for {label}: {c_res.get('error_message')}", False, True
        s_res = tools_rec.call("save_scenario", label=label)
        if s_res.get("status") != "success":
            return f"Could not save scenario {label}: {s_res.get('error_message')}", False, True
        labels.append(label)

    cmp_res = tools_rec.call("compare_scenarios", labels=labels)
    if cmp_res.get("status") != "success":
        return f"Could not compare scenarios: {cmp_res.get('error_message')}", False, True

    scenarios = cmp_res["data"]["scenarios"]
    best_label = max(labels, key=lambda l: (scenarios[l]["optimised_revenue"] or 0))
    lines = [f"- {l}: optimised revenue=${(scenarios[l]['optimised_revenue'] or 0):,.0f}, "
             f"ROAS={scenarios[l]['roas']}" for l in labels]
    answer = ("Scenario comparison for model " + model_id + ":\n" + "\n".join(lines) +
              f"\n\nBest option: {best_label}.")
    return answer, False, False


def _handle_target_kpi(tools_rec: Tools, prompt: str) -> tuple[str, bool, bool]:
    target_amounts = _extract_money_amounts(prompt)
    if not target_amounts:
        return "What target revenue would you like to reach (e.g. \"$2M\")?", True, False
    target_revenue = target_amounts[0]

    models = _fetch_models(tools_rec)
    model = _pick_latest(models, kpi="Revenue") or _pick_latest(models, kpi=None)
    if not model:
        return "I couldn't resolve a model to size a target budget for.", False, True
    model_id = str(model["id"])

    mmm_res = tools_rec.call("get_mmm_input", mmmRequestId=model_id)
    if mmm_res.get("status") != "success":
        return f"Could not retrieve model input: {mmm_res.get('error_message')}", False, True
    current_revenue = sum(mmm_res["data"]["kpi"][0]["values"])

    budget_res = tools_rec.call("get_current_budget", mmmRequestId=model_id)
    if budget_res.get("status") != "success":
        return f"Could not retrieve current budget: {budget_res.get('error_message')}", False, True
    baseline_budget = next((p["budget"] for p in budget_res["data"]["mmmCurrentBudgetResponseList"]
                            if p["timePeriod"] == "quarter"), None)
    if not baseline_budget:
        return "Could not determine a baseline budget for this model.", False, True

    d_res = tools_rec.call("run_default_optimise", mmmRequestId=model_id)
    if d_res.get("status") != "success":
        return f"Could not baseline-optimise model {model_id}: {d_res.get('error_message')}", False, True

    meta_res = tools_rec.call("channel_metadata")
    current_ct = (meta_res["data"]["constraint_type_ids"].get("Current", 0)
                  if meta_res.get("status") == "success" else 0)

    c_res = tools_rec.call("run_constrained_optimise", mmmRequestId=model_id,
                            totalBudget=baseline_budget, constraintType=current_ct)
    if c_res.get("status") != "success":
        return f"Could not baseline-optimise model {model_id}: {c_res.get('error_message')}", False, True

    rows = c_res["data"]["dateRangeToResponseMap"]["aggregated_aggregated"]["mmmBudgetOptimisationResponseList"]
    all_platforms = next((r for r in rows if r["platformName"] == "All Platforms"), {})
    all_platform_revenue = (all_platforms.get("optimisedBudgetData") or {}).get("response")
    if all_platform_revenue is None:
        return "Could not determine baseline optimised revenue for this model.", False, True

    calc_res = tools_rec.call("calculate_target_budget", target_revenue=target_revenue,
                               current_revenue=current_revenue,
                               all_platform_revenue=all_platform_revenue)
    if calc_res.get("status") != "success":
        return "Could not compute the required target budget.", False, True
    required_budget = calc_res["data"]["required_budget"]

    if required_budget > 0:
        final_res = tools_rec.call("run_constrained_optimise", mmmRequestId=model_id,
                                    totalBudget=required_budget, constraintType=current_ct)
        if final_res.get("status") != "success":
            required_budget = calc_res["data"]["required_budget"]  # still report the computed figure

    answer = (f"To reach ${target_revenue:,.0f} in revenue on model {model_id}, the estimated "
              f"required total budget is ${required_budget:,.2f} "
              f"(formula: target revenue − current revenue + baseline optimised revenue).")
    return answer, False, False


def _handle_show_metric(tools_rec: Tools, kpi_word: str) -> tuple[str, bool, bool]:
    model = _resolve_latest_model(tools_rec)
    if not model:
        return "I couldn't resolve your latest model.", False, True

    if model["outcomeKPI"] != kpi_word:
        answer = (f"Your latest model (id={model['id']}) has outcomeKPI="
                  f"'{model['outcomeKPI']}', not '{kpi_word}'. I don't have a tool that reports "
                  f"'{kpi_word}' directly for this model, and I don't want to invent a number. "
                  f"Could you confirm whether you want this model's {model['outcomeKPI']} figures, "
                  f"or point me to a model whose outcomeKPI is {kpi_word}?")
        return answer, True, False

    answer = (f"Your latest model (id={model['id']}) does target '{kpi_word}', but none of the "
              f"available tools return a raw {kpi_word} figure directly — I don't want to "
              f"fabricate one. Would current budget, an optimisation, or a forecast be useful instead?")
    return answer, True, False


def _handle_optimize(tools_rec: Tools, prompt: str, want_forecast: bool) -> tuple[str, bool, bool]:
    model_id, budget = _resolve_model_and_budget(tools_rec, prompt)
    constraint_word = _extract_constraint_word(prompt)

    if not model_id or budget is None:
        missing = []
        if not model_id:
            missing.append("which model")
        if budget is None:
            missing.append("what budget")
        return f"I need a bit more information before optimising: {' and '.join(missing)}?", True, False

    meta_res = tools_rec.call("channel_metadata")
    constraint_type = 0
    if meta_res.get("status") == "success":
        mapping = meta_res["data"]["constraint_type_ids"]
        constraint_type = mapping.get(constraint_word, mapping.get("Current", 0))

    d_res = tools_rec.call("run_default_optimise", mmmRequestId=model_id)
    if d_res.get("status") != "success":
        return f"Model {model_id} could not be optimised: {d_res.get('error_message')}", False, True

    c_res = tools_rec.call("run_constrained_optimise", mmmRequestId=model_id,
                            totalBudget=budget, constraintType=constraint_type)
    if c_res.get("status") != "success":
        return f"Optimisation failed for model {model_id}: {c_res.get('error_message')}", False, True

    rows = c_res["data"]["dateRangeToResponseMap"]["aggregated_aggregated"]["mmmBudgetOptimisationResponseList"]
    all_platforms = next((r for r in rows if r["platformName"] == "All Platforms"), {})
    opt_rev = (all_platforms.get("optimisedBudgetData") or {}).get("response")
    opt_spend = (all_platforms.get("optimisedBudgetData") or {}).get("spend")

    forecast_line = ""
    if want_forecast:
        f_res = tools_rec.call("forecast_revenue", mmmRequestId=model_id)
        if f_res.get("status") != "success":
            return f"Optimised, but forecast failed: {f_res.get('error_message')}", False, True
        total_forecast = f_res["data"]["totalForecastRevenue"]
        forecast_line = f" Forecasted total revenue over the period: ${total_forecast:,.0f}."

    answer = (f"Optimised model {model_id} with a ${budget:,.0f} budget "
              f"({constraint_word or 'Current'} constraints): projected spend "
              f"${(opt_spend or 0):,.0f}, projected revenue ${(opt_rev or 0):,.0f}.{forecast_line}")
    return answer, False, False


# ============================================================================ #
# Agent
# ============================================================================ #

class Agent:
    def __init__(self) -> None:
        pass

    def solve(self, prompt: str) -> dict[str, Any]:
        t0 = time.time()
        tools_rec = Tools()
        low = prompt.lower()

        answer = "I'm not sure what you're asking."
        asked_user = False
        failed = False

        try:
            if "compare" in low:
                answer, asked_user, failed = _handle_compare(tools_rec, prompt)

            elif any(kw in low for kw in
                     ["how much budget", "budget do i need", "budget required",
                      "need to hit", "to reach $"]):
                answer, asked_user, failed = _handle_target_kpi(tools_rec, prompt)

            elif "current budget" in low:
                answer, asked_user, failed = _handle_current_budget(tools_rec, prompt)

            elif "locked" in low:
                answer, asked_user, failed = _handle_locked_channels(tools_rec, prompt)

            elif "list" in low and "model" in low:
                answer, asked_user, failed = _handle_list_models(tools_rec)

            elif "show" in low and any(k in low for k in _KPI_WORDS):
                kpi_word = _extract_kpi_word(prompt)
                answer, asked_user, failed = _handle_show_metric(tools_rec, kpi_word)

            else:
                is_optimize = "optimi" in low  # covers optimise/optimize
                is_forecast = any(kw in low for kw in
                                   ["forecast", "revenue would", "what revenue"])
                if is_optimize or is_forecast:
                    answer, asked_user, failed = _handle_optimize(tools_rec, prompt,
                                                                   want_forecast=is_forecast)
                else:
                    answer = ("I'm not sure what you're asking. I can help with listing models, "
                              "optimising a budget, forecasting revenue, comparing scenarios, "
                              "current budget, target-KPI budget sizing, or locked channels.")
        except Exception as e:
            failed = True
            answer = f"Something went wrong while handling this request: {e}"

        return {
            "answer": answer,
            "trace": {
                "tool_calls": tools_rec.calls,
                "llm_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latency_s": time.time() - t0,
                "asked_user": asked_user,
                "failed": failed,
            },
        }