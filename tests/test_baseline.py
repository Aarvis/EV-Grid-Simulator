"""Checks against the downloaded electrical model, not mocked solver output."""
import math
import numpy as np
import pytest
from fastapi.testclient import TestClient
from gridlab import engine
from gridlab.config import MODEL, MODEL_DIR
from gridlab.server import app


@pytest.fixture(scope="module")
def model():
    if not MODEL.exists():
        pytest.skip("Download SMART-DS first: python -m gridlab.download")
    d = engine.context()
    engine.solve(d, 1, 0)
    network, ratings = engine.topology(d)
    return d, network, ratings


def test_inventory_and_disconnected_buses_have_no_customer_loads(model):
    d, network, _ = model
    assert network["customer_count_metadata"] == 732
    assert network["load_objects"] == 1329
    assert network["feeder_count"] == 2
    assert network["bus_count"] == 2087
    disconnected = {n["id"] for n in network["nodes"] if not n["reachable"]}
    assert len(disconnected) == 4
    assert not any(load["bus"] in disconnected for load in network["loads"])


@pytest.mark.parametrize("day,index", [(1,0), (1,95), (182,48), (365,95)])
def test_conservation_loss_units_and_sample_alignment(model, day, index):
    d, network, ratings = model
    engine.solve(d, day, index)
    row, snapshot = engine.state(d, network, ratings, day, index)
    assert row["converged"] and row["balance_ok"]
    # Independently reconstruct passive-network losses. Bulk losses are kW, and
    # include PC elements (loads/sources), which must not be counted as dissipation.
    real_losses_kw = sum(loss for name, loss in zip(d.Circuit.AllElementNames(), d.Circuit.AllElementLosses()[::2])
                         if name.lower().startswith(('line.', 'transformer.')))
    assert row["loss_kw"] == pytest.approx(real_losses_kw, abs=1e-6)
    assert row["loss_kw"] == pytest.approx(row["line_loss_kw"]+row["transformer_loss_kw"], abs=1e-6)
    # A constant-power residential load should match the original annual multiplier.
    candidate = next(l for l in network["loads"] if l["profile_class"] == "residential")
    shape = MODEL_DIR.parents[3] / "profiles" / (candidate["profile"] + ".csv")
    profile = np.loadtxt(shape, delimiter=",")
    d.Loads.Name(candidate["id"])
    actual = d.CktElement.TotalPowers()[0]
    expected = candidate["rated_kw"] * profile[(day-1)*96+index]
    assert actual == pytest.approx(expected, rel=1e-4, abs=1e-5)
    assert len(snapshot["voltages"]) == network["bus_count"]
    assert all(math.isfinite(v) for v in snapshot["voltages"] if v is not None)
    if day == 365:
        assert row["timestamp"] == "2019-01-01T00:00"


def test_profile_integrity():
    if not MODEL.exists():
        pytest.skip("Dataset not downloaded")
    result = engine.validate_profiles()
    assert result["passed"] and result["checked"] == 444


def test_api_rejects_invalid_dates_ranges_and_cross_origin_requests():
    client = TestClient(app)
    assert client.post("/api/runs", json={"date":"2019-01-01", "days":1}).status_code == 422
    assert client.post("/api/runs", json={"date":"2018-12-31", "days":2}).status_code == 422
    assert client.post("/api/runs", json={"date":"2018-01-01", "days":0}).status_code == 422
    assert client.post("/api/runs", json={"date":"2018-01-01", "days":151}).status_code == 422
    assert client.get("/api/runs/not-a-run").status_code == 404
    assert client.post("/api/runs", headers={"Origin":"https://unrelated.example"}, json={"date":"2018-01-01", "days":1}).status_code == 403
    assert client.get("/api/status", headers={"Host":"unrelated.example"}).status_code == 403


def test_energy_is_integrated_over_intervals():
    series = [{"supply_kw":110., "load_kw":100., "loss_kw":10., "line_loss_kw":6.,
               "transformer_loss_kw":4., "balance_residual_kw":0., "low_voltage_buses":0,
               "high_voltage_buses":0, "max_line_loading_pct":80., "max_transformer_loading_pct":75.,
               "balance_ok":True}]*4
    result = engine.totals(series)
    assert result["supply_kwh"] == 110.
    assert result["load_kwh"] == 100.
    assert result["loss_kwh"] == 10.
    assert result["loss_pct"] == pytest.approx(1000/110)
