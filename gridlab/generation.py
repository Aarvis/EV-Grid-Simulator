"""Allocate upstream generation against the solved substation draw."""
from pydantic import BaseModel, ConfigDict, Field

INTERVAL_HOURS = .25


class GenerationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    nuclear_mw: float | None = Field(default=None, ge=0)
    coal_gas_min_mw: float = Field(default=.6, ge=0)
    nuclear_cost_per_mwh: float = Field(default=9.16, ge=0)
    coal_gas_cost_per_mwh: float = Field(default=31.065, ge=0)
    solar_capacity_mw: float = Field(default=0., ge=0)
    solar_performance_factor: float = Field(default=.85, ge=0, le=1)
    solar_cost_per_mwh: float = Field(default=0., ge=0)


def dispatch(supply_kw, nuclear_mw, config, solar_mw=0.):
    # Negative feeder draw is upstream export, counted separately from generation surplus.
    demand_mw = max(0., supply_kw) / 1000
    coal_gas_mw = max(config.coal_gas_min_mw, demand_mw - nuclear_mw - solar_mw)
    generation_mw = nuclear_mw + coal_gas_mw + solar_mw
    nuclear_cost = nuclear_mw * INTERVAL_HOURS * config.nuclear_cost_per_mwh
    coal_gas_cost = coal_gas_mw * INTERVAL_HOURS * config.coal_gas_cost_per_mwh
    solar_cost = solar_mw * INTERVAL_HOURS * config.solar_cost_per_mwh
    cost = nuclear_cost + coal_gas_cost + solar_cost
    return {"nuclear_mw": nuclear_mw, "coal_gas_mw": coal_gas_mw, "solar_mw": solar_mw,
            "surplus_mw": max(0., nuclear_mw + config.coal_gas_min_mw + solar_mw - demand_mw),
            "generation_cost_usd": cost,
            "average_generation_cost_per_mwh": cost / (generation_mw * INTERVAL_HOURS) if generation_mw else None}


def totals(rows, prefix, config):
    nuclear_mwh = sum(row[prefix + "nuclear_mw"] for row in rows) * INTERVAL_HOURS
    coal_gas_mwh = sum(row[prefix + "coal_gas_mw"] for row in rows) * INTERVAL_HOURS
    solar_mwh = sum(row.get(prefix + "solar_mw", 0.) for row in rows) * INTERVAL_HOURS
    nuclear_cost = nuclear_mwh * config.nuclear_cost_per_mwh
    coal_gas_cost = coal_gas_mwh * config.coal_gas_cost_per_mwh
    solar_cost = solar_mwh * config.solar_cost_per_mwh
    generation_mwh = nuclear_mwh + coal_gas_mwh + solar_mwh
    cost = nuclear_cost + coal_gas_cost + solar_cost
    return {"nuclear_mwh": nuclear_mwh, "coal_gas_mwh": coal_gas_mwh, "solar_mwh": solar_mwh,
            "surplus_mwh": sum(row[prefix + "surplus_mw"] for row in rows) * INTERVAL_HOURS,
            "nuclear_cost_usd": nuclear_cost, "coal_gas_cost_usd": coal_gas_cost, "solar_cost_usd": solar_cost,
            "generation_cost_usd": cost,
            "average_generation_cost_per_mwh": cost / generation_mwh if generation_mwh else None}


def solar_at(profile, day, index):
    return profile[(day-1)*96 + index] if profile else 0.


def allocate(rows, config, paired, solar=(), first_day=1):
    baseline_prefix = "baseline_" if paired else ""
    baseline_min_mw = min(row[baseline_prefix + "supply_kw"] for row in rows) / 1000
    nuclear_mw = config.nuclear_mw
    if nuclear_mw is None:
        nuclear_mw = max(0., baseline_min_mw - config.coal_gas_min_mw)
    result = []
    for index, row in enumerate(rows):
        solar_mw = solar_at(solar, first_day, index)
        allocated = {**row, **dispatch(row["supply_kw"], nuclear_mw, config, solar_mw)}
        if paired:
            baseline = dispatch(row["baseline_supply_kw"], nuclear_mw, config, solar_mw)
            allocated.update({"baseline_" + key: value for key, value in baseline.items()})
        result.append(allocated)
    summary = {"baseline_min_mw": baseline_min_mw, "nuclear_mw": nuclear_mw,
               "without_ev": totals(result, baseline_prefix, config)}
    if paired:
        summary["with_ev"] = totals(result, "", config)
    return result, summary
