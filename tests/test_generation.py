import pytest

from gridlab.generation import GenerationConfig, allocate


def test_automatic_dispatch_uses_non_ev_minimum_for_both_cases():
    rows = [{"baseline_supply_kw": 4300., "supply_kw": 6500.},
            {"baseline_supply_kw": 8100., "supply_kw": 9300.}]
    result, summary = allocate(rows, GenerationConfig(), paired=True)
    assert summary["baseline_min_mw"] == 4.3
    assert summary["nuclear_mw"] == pytest.approx(3.7)
    assert result[0]["baseline_coal_gas_mw"] == pytest.approx(.6)
    for row in result:
        for prefix in ("", "baseline_"):
            assert row[prefix + "nuclear_mw"] == summary["nuclear_mw"]
            assert row[prefix + "nuclear_mw"] + row[prefix + "coal_gas_mw"] == pytest.approx(row[prefix + "supply_kw"] / 1000)
            assert row[prefix + "surplus_mw"] == 0
    assert summary["with_ev"]["nuclear_mwh"] == summary["without_ev"]["nuclear_mwh"] == pytest.approx(1.85)
    assert summary["with_ev"]["generation_cost_usd"] - summary["without_ev"]["generation_cost_usd"] == pytest.approx(3.4 * .25 * 31.065)
    expected_cost = (3.7 * 9.16 + .6 * 31.065) * .25
    assert result[0]["baseline_generation_cost_usd"] == pytest.approx(expected_cost)
    assert result[0]["baseline_average_generation_cost_per_mwh"] == pytest.approx(expected_cost / (4.3 * .25))


@pytest.mark.parametrize(("config", "expected_nuclear", "expected_surplus", "expected_cost"), [
    (GenerationConfig(), 0., .2, .6 * 31.065 * .25),
    (GenerationConfig(nuclear_mw=4.5, nuclear_cost_per_mwh=25., coal_gas_cost_per_mwh=50.),
     4.5, 4.7, (4.5 * 25 + .6 * 50) * .25),
])
def test_baseline_dispatch_reports_excess_and_charges_for_generated_energy(config, expected_nuclear, expected_surplus, expected_cost):
    result, summary = allocate([{"supply_kw": 400.}], config, paired=False)
    assert summary["nuclear_mw"] == expected_nuclear
    assert result[0]["surplus_mw"] == pytest.approx(expected_surplus)
    assert summary["without_ev"]["surplus_mwh"] == pytest.approx(expected_surplus * .25)
    assert summary["without_ev"]["generation_cost_usd"] == pytest.approx(expected_cost)
    assert "with_ev" not in summary


def test_solar_is_shared_by_both_cases_and_included_in_cost_and_surplus():
    rows = [{"baseline_supply_kw": 5000., "supply_kw": 6000.}]
    profile = (0.,) * 96 + (1.36,)
    config = GenerationConfig(nuclear_mw=3.696, solar_capacity_mw=2., solar_cost_per_mwh=8.)

    (row,), summary = allocate(rows, config, paired=True, solar=profile, first_day=2)

    assert row["solar_mw"] == row["baseline_solar_mw"] == 1.36
    assert row["coal_gas_mw"] == pytest.approx(.944)
    assert row["baseline_coal_gas_mw"] == .6
    assert row["baseline_surplus_mw"] == pytest.approx(.656)
    assert row["average_generation_cost_per_mwh"] == pytest.approx(12.343453333333333)
    assert summary["with_ev"]["solar_cost_usd"] == pytest.approx(2.72)
    assert summary["without_ev"]["solar_mwh"] == pytest.approx(.34)
