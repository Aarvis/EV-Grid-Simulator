import pytest
from gridlab.charging import ChargingConfig, available_discharge_kw, build_schedule, charge_energy, driving_consumption
from gridlab.travel import segments


def test_taper_crosses_bands_and_stops_at_full():
    assert charge_energy(10, 50, 8, .25, .9) == pytest.approx(11.8)
    # Starting at 90%, a quarter-hour crosses into the second taper band.
    tapered = charge_energy(31.5, 35, 11.52, .25, .90)
    assert 33.25 < tapered < 31.5 + 11.52*.25*.90
    assert charge_energy(34.99, 35, 11.52, .25, .90) == 35
    assert charge_energy(35, 35, 11.52, .25, .90) == 35


@pytest.mark.parametrize(("start_full_pct", "expected_full"), [(None, 1), (0, 0), (25, 1), (100, 2)])
def test_partial_interval_home_time_and_vehicle_energy_balance(start_full_pct, expected_full):
    trips = [{"depart_minute":0, "arrive_minute":10, "distance_km":5.,
              "destination":"Home", "destination_code":1}]
    vehicles = [{"id":name, "is_ev":True, "home_access":True, "bus":"bus", "household_id":"house",
                 "battery_kwh":50., "potential_home_charging_kw":6., "charger_voltage_v":240}
                for name in ("one", "two")]
    diaries = [{"vehicle_id":vehicle["id"], "home_bus":"bus", "household_id":"house",
                "trips":trips, "segments":segments(trips)} for vehicle in vehicles]
    settings = {} if start_full_pct is None else {"initial_full_pct": start_full_pct}
    config = ChargingConfig(driving_kwh_per_km=.18, **settings)
    result = build_schedule({"vehicles":vehicles}, {"vehicles":diaries}, config)
    assert result == build_schedule({"vehicles":vehicles}, {"vehicles":diaries}, config)
    assert result["initial_full_count"] == expected_full
    assert sum(v["initial_soc"] == 1. for v in result["vehicles"]) == expected_full
    assert all(.3 <= v["initial_soc"] < 1 for v in result["vehicles"] if v["initial_soc"] < 1)
    first = result["intervals"][0]
    assert first["grid_kwh"] <= 2 * 6 * 5/60 + 1e-9
    assert first["drive_kwh"] == pytest.approx(1.8)
    assert first["battery_end_kwh"] == pytest.approx(
        result["initial_battery_kwh"] + .9 * first["grid_kwh"] - first["drive_kwh"])
    assert all(0 <= soc <= 1 for vehicle in result["vehicles"] for soc in vehicle["end_soc"])


def test_future_export_is_disabled_and_protects_trip_energy_and_taper():
    assert available_discharge_kw(30, 50, 10, .25, 0.) == 0.
    assert available_discharge_kw(30, 50, 10, .25, 29.5, enabled=True) == pytest.approx(1.8)
    p = available_discharge_kw(12.5, 50, 10, .25, 0., enabled=True)
    assert 0 < p < 5
    assert p == pytest.approx(10 * ((12.5 - p*.25/.90)/50-.20)/.10)


def test_destination_ports_fill_at_arrival_and_energy_goes_to_the_away_bus():
    network = {"nodes":[{"id":bus, "reachable":True} for bus in ("home", "shop")],
               "loads":[{"bus":"home", "profile_class":"residential"},
                        {"bus":"shop", "profile_class":"commercial profile"}]}
    vehicles, diaries = [], []
    for index in range(7):
        vid = f"ev-{index}"
        vehicles.append({"id":vid, "is_ev":True, "home_access":True, "bus":"home", "household_id":"house", "battery_category":"compact",
                         "battery_kwh":50., "potential_home_charging_kw":6., "charger_voltage_v":240})
        arrival = 30 if index == 6 else 10
        departure = 60 if index >= 5 else 30
        trips = [{"depart_minute":0, "arrive_minute":arrival, "distance_km":40.,
                  "destination":"Shop", "destination_code":8},
                 {"depart_minute":departure, "arrive_minute":departure+10, "distance_km":5.,
                  "destination":"Home", "destination_code":1}]
        diaries.append({"vehicle_id":vid, "home_bus":"home", "household_id":"house",
                        "trips":trips, "segments":segments(trips)})
    inventory, travel = {"vehicles":vehicles}, {"vehicles":diaries}
    enabled = build_schedule(inventory, travel, ChargingConfig(away_coverage_pct=100), network)
    disabled = build_schedule(inventory, travel, ChargingConfig(away_charging_enabled=False), network)
    ports = enabled["destination_sites"]["shop"]
    assert 1 <= ports <= 5
    assert enabled["destination_summary"]["plugged_in_stops"] == ports + 1
    assert enabled["destination_summary"]["full_site_stops"] == 6 - ports
    assert enabled["vehicles"][5]["away_stops"][0]["grid_kwh"] == 0
    assert enabled["vehicles"][6]["away_stops"][0]["grid_kwh"] > 0
    assert enabled["intervals"][0]["away_grid_kwh"] > 0
    assert enabled["intervals"][0]["home_grid_kwh"] == 0
    for index, interval in enumerate(enabled["intervals"]):
        assert interval["away_grid_kwh"] == pytest.approx(enabled["away_grid_kwh_by_bus"]["shop"][index])
        assert interval["grid_kwh"] == pytest.approx(interval["home_grid_kwh"] + interval["away_grid_kwh"])
    assert disabled["away_grid_kwh_by_bus"] == {}
    assert all(v["away_stops"][0]["bus"] == "shop" for v in disabled["vehicles"])


def test_vehicle_rate_is_seeded_by_id_and_class():
    vehicle = {"id":"one", "battery_category":"small"}
    config = ChargingConfig(charge_efficiency=.8)
    rate = driving_consumption(vehicle, config)
    assert rate > 0
    assert driving_consumption(vehicle, ChargingConfig(charge_efficiency=.9)) == rate
    assert driving_consumption({**vehicle, "id":"two"}, config) != rate
    assert driving_consumption({**vehicle, "battery_category":"large"}, config) != rate
    assert driving_consumption(vehicle, ChargingConfig(driving_kwh_per_km=.18)) == .18
