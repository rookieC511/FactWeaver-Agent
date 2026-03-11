from __future__ import annotations

from typing import Any, Mapping

from core.config import USD_TO_RMB_RATE


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _round_money(value: float) -> float:
    return round(float(value), 6)


def usd_to_rmb(amount_usd: Any) -> float:
    return _round_money(_as_float(amount_usd) * USD_TO_RMB_RATE)


def enrich_cost_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    llm_cost_rmb = _as_float(data.get("llm_cost_rmb"))
    external_cost_usd_est = _as_float(data.get("external_cost_usd_est"))
    serper_cost_usd_est = _as_float(data.get("serper_cost_usd_est"))
    tavily_cost_usd_est = _as_float(data.get("tavily_cost_usd_est"))

    data["llm_cost_rmb"] = _round_money(llm_cost_rmb)
    data["external_cost_usd_est"] = _round_money(external_cost_usd_est)
    data["serper_cost_usd_est"] = _round_money(serper_cost_usd_est)
    data["tavily_cost_usd_est"] = _round_money(tavily_cost_usd_est)
    data["external_cost_rmb_est"] = usd_to_rmb(external_cost_usd_est)
    data["serper_cost_rmb_est"] = usd_to_rmb(serper_cost_usd_est)
    data["tavily_cost_rmb_est"] = usd_to_rmb(tavily_cost_usd_est)
    data["total_cost_rmb_est"] = _round_money(llm_cost_rmb + data["external_cost_rmb_est"])
    return data
