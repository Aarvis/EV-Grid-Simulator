import pytest

from gridlab import market
from gridlab.charging import ChargingConfig
from gridlab.generation import GenerationConfig, dispatch
from gridlab.market import (
    BillingBook, MarketConfig, Window, choose_traders, choose_trading_only, commit, meter_totals, replay,
    shuffled_commitments, windows_for_day,
)
from gridlab.travel import keep_vehicles_home


@pytest.mark.parametrize("allow, balance_ok, solver_error", [
    (False, True, False), (True, True, False), (True, False, False), (True, True, True),
])
@pytest.mark.parametrize("reverse_flow", [False, True])
def test_export_screen_override_keeps_valid_trades(monkeypatch, allow, balance_ok, solver_error, reverse_flow):
    vehicles = [{"id":key, "capacity_kwh":50., "rated_ac_kw":7.2, "home_bus":key,
                 "household_id":key, "away_stops":[]} for key in ("seller", "buyer")]
    diaries = [{"vehicle_id":v["id"], "segments":[{"state":"home", "start_minute":0, "end_minute":1440}]}
               for v in vehicles]
    opening = {"seller":{"opening":{"stored_kwh":25., "value_usd":.25}},
               "buyer":{"opening":{"stored_kwh":0., "value_usd":0.}}}
    config = MarketConfig(trader_pct=100, allow_sales_despite_grid_violations=allow)
    meters = []
    monkeypatch.setattr(market.engine, "set_charger_loads", lambda *args: meters.append(args[-1]))

    def solve(*args):
        if solver_error:
            raise RuntimeError("Power flow did not converge")

    def state(*args):
        net_kw = sum(meters[-1].values()) * 4
        supply = -1. if reverse_flow and len(meters) == 1 else 10.
        loading = 105. if len(meters) == 1 else 90.
        return {"supply_kw":supply, "ev_charge_kw":net_kw, "balance_ok":balance_ok, "loss_kw":0.,
                "timestamp":"2018-01-01T00:15", "balance_residual_kw":0. if balance_ok else .1,
                "balance_tolerance_kw":.01}, {
            "loads":[], "elements":[{"loading_pct":loading}], "voltages":[1.], "max_voltages":[1.]}

    monkeypatch.setattr(market.engine, "solve", solve)
    monkeypatch.setattr(market.engine, "state", state)
    simulation = market.TradingDay({"vehicles":vehicles}, diaries, opening, ChargingConfig(), config,
                                   GenerationConfig(), 3., 50., 1, {"loads":[]}, {}, [], grid=(None, None))
    if not balance_ok or solver_error:
        with pytest.raises(RuntimeError, match="Power balance|did not converge"):
            simulation.step(0, lambda _: {})
        assert len(meters) == 1 and not simulation.rows
        return

    reference = {"elements":[{"loading_pct":90.}], "voltages":[1.], "max_voltages":[1.]}
    row, snapshot = simulation.step(0, lambda _: reference)
    assert len(meters) == (1 if allow else 2)
    assert row["ev_import_kwh"] == pytest.approx(1.8)
    assert row["ev_export_kwh"] == pytest.approx(1.8 if allow else 0.)
    assert row["curtailed_export_kwh"] == pytest.approx(0. if allow else 1.8)
    assert snapshot["elements"][0]["loading_pct"] == (105. if allow else 90.)
    assert row["upstream_export_kwh"] == pytest.approx(.25 if allow and reverse_flow else 0.)


def test_trading_only_selection_and_trip_override():
    ids = [f"ev-{i}" for i in range(10)]
    config = MarketConfig(trader_pct=30, trading_only_pct=50)
    selected = set(choose_trading_only(ids, config))
    assert len(selected) == 2
    assert selected <= choose_traders(ids, config)
    assert selected == set(choose_trading_only(reversed(ids), config))
    assert choose_trading_only(ids, MarketConfig()) == []
    assert choose_trading_only(ids, MarketConfig(trader_pct=0, trading_only_pct=100)) == []

    plan = {"vehicles":[{"vehicle_id":key, "distance_km":10., "trips":[{"distance_km":10.}]} for key in ids]}
    parked = keep_vehicles_home(plan, selected)
    for original, effective in zip(plan["vehicles"], parked["vehicles"], strict=True):
        assert original["distance_km"] == 10.
        if original["vehicle_id"] in selected:
            assert effective["distance_km"] == 0.
            assert effective["trips"] == []
            assert effective["segments"] == [{"start_minute":0, "end_minute":1440, "state":"home", "purpose":"Home"}]
            vehicle = {"id":effective["vehicle_id"], "away_stops":[]}
            assert windows_for_day(vehicle, effective, .9, 0.) == [
                [Window(.25, ("home", vehicle["id"]), overnight=True)] for _ in range(96)]
        else:
            assert original == effective


def test_traders_are_reproducible_and_passive_evs_do_not_sell():
    vehicles = [f"ev-{i}" for i in range(10)]
    config = MarketConfig(trader_pct=30, export_kw=20)
    assert choose_traders(vehicles, config) == choose_traders(reversed(vehicles), config)
    assert len(choose_traders(vehicles, config)) == 3
    vehicle = {"capacity_kwh":50., "rated_ac_kw":20., "driving_kwh_per_km":.2}
    parking = [Window(.25, ("home", "ev-0"), remaining_drive_kwh=17.)]
    passive = commit(parking, 24., .72, vehicle, ChargingConfig(), config, False, 50.)
    active = commit(parking, 24., .72, vehicle, ChargingConfig(), config, True, 50.)
    assert passive[0].kind == "buy"
    assert active[0].kind == "sell"
    assert active[0].meter_kwh == pytest.approx(1.8)
    assert meter_totals({"ev-0":active})[0]["home", "ev-0"] == pytest.approx(-1.8)
    end = replay(active, 24., .72, vehicle, 50., 60., .05, 0.)
    assert end["stored_kwh"] == pytest.approx(22.)
    assert end["value_usd"] == pytest.approx(.66)
    assert end["export_credit_usd"] == pytest.approx(.09)
    assert end["distribution_loss_kwh"] == 0.
    assert commit(parking, 24., .72, vehicle, ChargingConfig(), config, True, 30.)[0].kind == "buy"
    assert commit(parking, 24., .72, vehicle, ChargingConfig(), MarketConfig(wear_usd_per_battery_kwh=.02), True, 50.)[0].kind == "buy"


def test_billing_allocates_loss_cost_to_gross_imports_and_counts_vehicle_once():
    services = [{"id":"home", "bus":"h", "profile_class":"residential", "load_ids":["a"]},
                {"id":"shop", "bus":"s", "profile_class":"commercial profile", "load_ids":["b"]}]
    vehicles = [{"id":"ev", "home_bus":"h", "household_id":"home"}]
    book = BillingBook(services, [{"id":"a"}, {"id":"b"}], vehicles)
    assert book.settle([[80.,0.],[80.,0.]], {"ev":(10.,0.)}, 30., 40., 5.) == .1
    book.settle([[80.,0.],[80.,0.]], {"ev":(0.,8.)}, 50., 60., 4.)
    result = book.payload({"ev"})
    bill = result["vehicles"][0]
    assert bill["loss_kwh"] == pytest.approx(1.)
    assert bill["loss_cost_usd"] == pytest.approx(.04)
    assert bill["energy_cost_usd"] == pytest.approx(.30)
    assert bill["export_credit_usd"] == pytest.approx(.40)
    assert bill["net_usd"] == pytest.approx(-.06)
    assert result["households"][0]["net_usd"] == pytest.approx(result["services"][0]["net_usd"] + bill["net_usd"])
    assert result["totals"]["loss_cost_usd"] == pytest.approx(5*.04 + 4*.06)
    assert result["totals"]["import_kwh"] == pytest.approx(90.)


def test_dynamic_price_replaces_the_forecast_in_shuffled_order():
    vehicles = {f"ev-{i}":{"capacity_kwh":50., "rated_ac_kw":7.2} for i in range(5)}
    traders = set(vehicles) - {"ev-4"}
    windows = {key:[[Window(.25, ("home", key))]] * 2 for key in vehicles}
    starting = {key:{"stored_kwh":50., "value_usd":0.} for key in vehicles}
    config = MarketConfig(pricing_mode="dynamic")
    source = GenerationConfig()

    def bids(index, day=325):
        return shuffled_commitments(vehicles, traders, windows, starting, ChargingConfig(), config,
                                    17., 60., 5500., 1., 3., source, day, index)

    plans, quotes, positions, closing = bids(0)
    assert bids(0) == (plans, quotes, positions, closing)
    assert bids(1)[2] != positions
    assert bids(0, day=326)[2] != positions
    assert set(positions) == traders
    assert not plans["ev-4"]
    order = sorted(positions, key=positions.get)
    for i, key in enumerate(order):
        expected_kw = 5500. + 4 * (60. * (4-i)/4 - 1.8 * i)
        assert quotes[key] == pytest.approx(dispatch(expected_kw, 3., source, 1.)["average_generation_cost_per_mwh"])
        assert plans[key][0].kind == "sell"
    assert closing == pytest.approx(dispatch(5500. - 4 * 4 * 1.8, 3., source, 1.)["average_generation_cost_per_mwh"])


def test_billing_uses_per_vehicle_quotes_and_reconciles_totals():
    vehicles = [{"id":key, "home_bus":"h", "household_id":"home"} for key in ("buyer", "seller")]
    book = BillingBook([], [], vehicles)
    book.settle([], {"buyer":(10., 0.), "seller":(0., 8.)}, 30., 40., 5.,
                {"buyer":20., "seller":50.})
    live = book.totals()
    assert live["energy_cost_usd"] == pytest.approx(.2)
    assert live["export_credit_usd"] == pytest.approx(.4)
    assert live == pytest.approx(book.payload()["totals"])


def test_flexible_trader_tops_up_then_uses_efficiency_adjusted_buy_and_sell_prices():
    vehicle = {"capacity_kwh":50., "rated_ac_kw":20.}
    charging = ChargingConfig(charge_efficiency=.9)
    market = MarketConfig(behavior="buy_sell_wait", reserve_pct=10., travel_buffer_pct=5.,
                          discharge_efficiency=.9)
    windows = [Window(.25, ("home", "ev"), planned_drive_kwh=20., overnight=True)]

    def decision(stored, quote, expected_loss=0., trader=True):
        return commit(windows, stored, stored*.020, vehicle, charging, market, trader, quote, expected_loss)

    needed = decision(21., 40.)
    assert needed[0].kind == "buy"
    assert needed[0].battery_kwh == pytest.approx(1.5)
    assert needed[0].meter_kwh == pytest.approx(1.5/.9)
    assert decision(24., 19.) == []
    assert decision(24., 17., expected_loss=2.) == []
    assert decision(24., 17.)[0].kind == "buy"
    assert decision(24., 21.) == []
    sale = decision(24., 23.)
    assert sale[0].kind == "sell"
    assert sale[0].battery_kwh == pytest.approx(-1.5)
    assert decision(22.5, 40.) == []
    assert decision(24., 40., trader=False)[0].battery_kwh == pytest.approx(4.5)


def test_flexible_windows_use_tomorrow_only_after_the_final_return_home():
    vehicle = {"id":"ev", "away_stops":[], "driving_kwh_per_km":.2, "rated_ac_kw":7.2}
    diary = {"trips":[{"distance_km":10.}, {"distance_km":15.}], "segments":[
        {"state":"home", "start_minute":0, "end_minute":480},
        {"state":"driving", "start_minute":480, "end_minute":495, "trip_index":0},
        {"state":"parked_away", "start_minute":495, "end_minute":1035},
        {"state":"driving", "start_minute":1035, "end_minute":1050, "trip_index":1},
        {"state":"home", "start_minute":1050, "end_minute":1440},
    ]}
    windows = windows_for_day(vehicle, diary, .9, tomorrow_kwh=11.)
    assert windows[0][0].planned_drive_kwh == 5.
    assert not windows[0][0].overnight
    assert windows[70][0].planned_drive_kwh == 11.
    assert windows[70][0].overnight
