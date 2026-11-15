import pytest

from gridlab.battery import build_ledger
from gridlab.charging import ChargingConfig, build_schedule
from gridlab.travel import segments


def test_ledger_replays_driving_before_charging_and_accounts_for_losses():
    vehicle = {"id":"ev", "is_ev":True, "home_access":True, "bus":"home", "household_id":"house",
               "battery_category":"compact", "battery_kwh":50., "potential_home_charging_kw":6.,
               "charger_voltage_v":240}
    trips = [{"depart_minute":0, "arrive_minute":10, "distance_km":10.,
              "destination":"Home", "destination_code":1}]
    diary = {"vehicle_id":"ev", "home_bus":"home", "household_id":"house", "trips":trips,
             "segments":segments(trips)}
    config = ChargingConfig(initial_full_pct=100, charge_efficiency=.8, driving_kwh_per_km=.2)
    schedule = build_schedule({"vehicles":[vehicle]}, {"vehicles":[diary]}, config)
    rows = [{"delta_loss_kwh":interval["grid_kwh"]*.05, "average_generation_cost_per_mwh":45.}
            for interval in schedule["intervals"]]
    # Differences in grid losses when no EV is charging remain unattributed.
    rows[-1]["delta_loss_kwh"] = .01
    records, summary, costs = build_ledger(schedule, rows, opening_price_per_mwh=32., efficiency=config.charge_efficiency)
    record = records["ev"]
    assert record["opening"]["value_usd"] == pytest.approx((50 / .8) * 1.05 * .032)
    first = record["intervals"][0]
    assert first["driving_kwh"] == pytest.approx(2.)
    assert first["driving_cost_usd"] == pytest.approx(record["opening"]["value_usd"] / 50 * 2)
    assert first["driving_cost_usd_per_km"] == pytest.approx(first["driving_cost_usd"] / 10)
    assert first["charged_kwh"] == pytest.approx(first["home_draw_kwh"] * .8)
    assert first["charging_cost_usd"] == pytest.approx(first["home_draw_kwh"] * 1.05 * .045)
    assert first["distribution_loss_kwh"] == pytest.approx(costs[0]["ev_attributed_loss_kwh"])
    assert summary["unallocated_loss_change_kwh"] == pytest.approx(.01)
    assert summary["closing_value_usd"] == pytest.approx(
        summary["opening_value_usd"] + summary["charging_cost_usd"] - summary["driving_cost_usd"])
