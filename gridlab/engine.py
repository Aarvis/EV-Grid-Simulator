"""Electrical calculations; every run uses a fresh, isolated DSS context."""
from datetime import datetime, timedelta
import json
import math
import re
import time
import numpy as np
import networkx as nx
import opendssdirect as odd
from .config import DATA, MODEL, STATION
from .fleet import rng


def context():
    if not MODEL.exists():
        raise FileNotFoundError("Dataset missing. Run: python -m gridlab.download")
    d = odd.NewContext()
    d.Basic.AllowChangeDir(False)
    d.Basic.AllowForms(False)
    d.Basic.AllowEditor(False)
    d.Basic.AllowDOScmd(False)
    d.Text.Command(f'compile "{MODEL}"')
    d.Solution.Mode(2)  # YEARLY: use the source annual loadshapes.
    d.Solution.StepSize(900)
    d.Solution.Number(1)
    d.Solution.ControlMode(0)  # STATIC: settle controls at each 15-minute state.
    d.Solution.MaxControlIterations(100)
    d.Solution.MaxIterations(100)
    d.Solution.Convergence(1e-8)
    return d


def finite(value):
    return float(value) if math.isfinite(float(value)) else None


def snapshot_time(day, index):
    # End-of-interval convention: first sample is Jan 1 00:15 (DSS index 1).
    return (day - 1) * 24 + (index + 1) * .25


def solve(d, day, index):
    d.Solution.DblHour(snapshot_time(day, index))
    d.Solution.SolveSnap()
    if not d.Solution.Converged():
        raise RuntimeError(f"Power flow did not converge at interval {index + 1}")
    if not d.Solution.ControlActionsDone():
        raise RuntimeError(f"Controls did not settle at interval {index + 1}")


def topology(d):
    nodes = []
    for name in d.Circuit.AllBusNames():
        d.Circuit.SetActiveBus(name)
        nodes.append({"id": name, "x": d.Bus.X(), "y": d.Bus.Y(),
                      "base_kv_ln": d.Bus.kVBase(), "phases": d.Bus.Nodes(),
                      "customers": []})
    by_bus = {n["id"]: n for n in nodes}
    loads = []
    for name in d.Loads.AllNames():
        d.Loads.Name(name)
        bus = d.CktElement.BusNames()[0].split('.')[0].lower()
        shape = d.Loads.Yearly()
        category = "residential" if shape.lower().startswith("res_") else "commercial profile" if shape.lower().startswith("com_") else "unclassified"
        item = {"id": name, "bus": bus, "rated_kw": d.Loads.kW(),
                "rated_kvar": d.Loads.kvar(), "profile": shape, "profile_class": category,
                "phases": d.CktElement.NumPhases(), "connection_kv": d.Loads.kV(),
                "terminal_bus": d.CktElement.BusNames()[0], "is_delta": bool(d.Loads.IsDelta())}
        loads.append(item)
        if bus in by_bus:
            by_bus[bus]["customers"].append(name)
    edges, elements, ratings = [], [], {}
    for name in d.Circuit.AllElementNames():
        kind, _, short = name.partition('.')
        if kind.lower() not in ("line", "transformer"):
            continue
        d.Circuit.SetActiveElement(name)
        buses = [b.split('.')[0].lower() for b in d.CktElement.BusNames()]
        enabled = bool(d.CktElement.Enabled())
        phases = d.CktElement.NumPhases()
        closed = not any(d.CktElement.IsOpen(1, p) for p in range(1, phases+1))
        item = {"id": name, "kind": kind.lower(), "buses": buses,
                "phases": phases, "enabled": enabled, "closed": closed}
        if kind.lower() == "line":
            d.Lines.Name(short)
            item.update(length=d.Lines.Length(), units=int(d.Lines.Units()),
                        normal_amps=d.CktElement.NormalAmps(), linecode=d.Lines.LineCode())
            ratings[name] = {"kind": "line", "amps": d.CktElement.NormalAmps()}
        else:
            d.Transformers.Name(short)
            winds = []
            for winding in range(1, d.Transformers.NumWindings()+1):
                d.Transformers.Wdg(winding)
                kva, kv = d.Transformers.kVA(), d.Transformers.kV()
                amps = kva / kv / (math.sqrt(3) if phases == 3 else 1) if kv else 0
                winds.append({"kva": kva, "kv": kv, "rated_amps": amps})
            item["windings"] = winds
            ratings[name] = {"kind": "transformer", "windings": winds}
        elements.append(item)
        for terminal, bus in enumerate(buses[1:], 2):
            if buses[0] != bus:
                edges.append({"id": name, "from": buses[0], "to": bus,
                              "terminal": terminal, "kind": kind.lower(),
                              "enabled": enabled, "closed": closed})
    g = nx.Graph()
    g.add_nodes_from(by_bus)
    g.add_edges_from((e["from"], e["to"]) for e in edges if e["enabled"] and e["closed"])
    reachable = nx.node_connected_component(g, "uhs19_69")
    for n in nodes:
        n["reachable"] = n["id"] in reachable
    manifest = json.loads((DATA / "manifest.json").read_text())
    return {"station": STATION, "nodes": nodes, "edges": edges, "elements": elements,
            "loads": loads, "source_bus": "uhs19_69", "customer_count_metadata": 732,
            "feeder_count": 2, "load_objects": len(loads), "bus_count": len(nodes),
            "unreachable_buses": sum(not n["reachable"] for n in nodes),
            "profile_count": manifest["profiles"], "year": 2018,
            "dataset": manifest["dataset"], "source": "External grid at 69 kV; generation mix unspecified",
            "notes": ["Synthetic grid and demand; not utility meter observations.",
                      "Residential split-phase customers can have multiple DSS load objects.",
                      "Profile classes are not verified building or industry identities.",
                      "Upstream generation and transmission losses are outside this boundary."]}, ratings


def install_chargers(d, network, schedule, seed):
    """Group interval energy by grid terminal for the power-flow solve."""
    nodes = {node["id"]:node for node in network["nodes"]}
    connections = {}

    def add_load(bus, terminal, connection, phases, kv, energy, meter):
        if terminal not in connections:
            name = f"ev_charger_{len(connections)}"
            d.Text.Command(f'New Load.{name} bus1="{terminal}" phases={phases} conn={connection} '
                           f'kV={kv} kW=0 kvar=0 model=1 status=fixed vminpu=.5 vmaxpu=2')
            connections[terminal] = {"name":name, "bus":bus, "grid_kwh":[0.] * len(energy), "meters":[]}
        connections[terminal]["meters"].append(meter)
        profile = connections[terminal]["grid_kwh"]
        for index, draw in enumerate(energy):
            profile[index] += draw

    for vehicle in schedule["vehicles"]:
        bus = vehicle["home_bus"]
        node = nodes[bus]
        if not node["reachable"] or not {1, 2}.issubset(node["phases"]):
            raise ValueError(f"Home charger requires an energized split-phase bus: {bus}")
        voltage = vehicle["voltage_v"]
        if voltage == 240:
            terminal, connection = f"{bus}.1.2", "delta"
        elif voltage == 120:
            leg = rng(seed, f"charger-leg|{vehicle['id']}").choice((1, 2))
            terminal, connection = f"{bus}.{leg}.0", "wye"
        else:
            raise ValueError(f"Unsupported home charger voltage at {bus}: {voltage}")
        add_load(bus, terminal, connection, 1, voltage/1000, vehicle.get("home_grid_kwh", vehicle["grid_kwh"]),
                 ("home", vehicle["id"]))

    commercial = {load["bus"]:load for load in network["loads"] if load["profile_class"] == "commercial profile"}
    for bus, energy in schedule.get("away_grid_kwh_by_bus", {}).items():
        load = commercial[bus]
        # The site's conversion equipment is represented as a load on its supply side.
        if load["phases"] == 3:
            connection = "delta" if load["is_delta"] else "wye"
            add_load(bus, load["terminal_bus"], connection, 3, load["connection_kv"], energy, ("away", bus))
        elif load["phases"] == 1 and {1, 2}.issubset(nodes[bus]["phases"]) and math.isclose(load["connection_kv"], .12):
            add_load(bus, f"{bus}.1.2", "delta", 1, .24, energy, ("away", bus))
        else:
            raise ValueError(f"Unsupported destination charger supply at {bus}")
    return list(connections.values())


def set_charger_loads(d, connections, index, meter_kwh=None):
    for connection in connections:
        energy = connection["grid_kwh"][index] if meter_kwh is None else sum(
            meter_kwh.get(meter, 0.) for meter in connection["meters"])
        d.Loads.Name(connection["name"])
        d.Loads.kW(4 * energy)
        d.Loads.kvar(0.)


def state(d, network, ratings, day, index, chargers=()):
    voltages = []
    max_voltages = []
    low = high = invalid = deenergized = 0
    for node in network["nodes"]:
        d.Circuit.SetActiveBus(node["id"])
        values = np.asarray(d.Bus.puVmagAngle()[::2], dtype=float)
        values = values[np.isfinite(values)]
        # A disconnected or unknown-base bus must not silently disappear from diagnostics.
        if node["base_kv_ln"] <= 0 or len(values) == 0:
            voltages.append(None)
            max_voltages.append(None)
            invalid += 1
        else:
            vmin, vmax = float(min(values)), float(max(values))
            voltages.append(vmin)
            max_voltages.append(vmax)
            deenergized += int(vmax < .1)
            low += int(.1 <= vmin < .95)
            high += int(vmax > 1.05)
    element_states = []
    line_max = transformer_max = 0.
    for element in network["elements"]:
        name = element["id"]
        d.Circuit.SetActiveElement(name)
        loss = d.CktElement.Losses()[0] / 1000
        nc, np_ = d.CktElement.NumConductors(), d.CktElement.NumPhases()
        currents = d.CktElement.CurrentsMagAng()[::2]
        rating = ratings[name]
        if rating["kind"] == "line":
            amps = max(currents[:np_], default=0.)
            loading = 100 * amps / rating["amps"] if rating["amps"] else None
            line_max = max(line_max, loading or 0)
        else:
            fractions = []
            for term, winding in enumerate(rating["windings"]):
                amps = max(currents[term*nc:term*nc+np_], default=0.)
                if winding["rated_amps"]:
                    fractions.append(amps/winding["rated_amps"])
            loading = 100 * max(fractions, default=0.)
            transformer_max = max(transformer_max, loading)
        element_states.append({"loading_pct": finite(loading) if loading is not None else None,
                               "loss_kw": finite(loss)})
    load_states, load_kw, load_kvar = [], 0., 0.
    for load in network["loads"]:
        d.Loads.Name(load["id"])
        pq = d.CktElement.TotalPowers()
        p, q = float(pq[0]), float(pq[1])
        load_kw += p
        load_kvar += q
        load_states.append([p, q])
    charging_by_bus = {}
    for charger in chargers:
        d.Loads.Name(charger["name"])
        p, q = d.CktElement.TotalPowers()[:2]
        load_kw += p
        load_kvar += q
        if p:
            bus = charger["bus"]
            charging_by_bus[bus] = charging_by_bus.get(bus, 0.) + p
    supply_kw, supply_kvar = [-float(v) for v in d.Circuit.TotalPower()]
    losses_kw = float(d.Circuit.Losses()[0]) / 1000
    line_losses_kw = float(d.Circuit.LineLosses()[0])
    transformer_losses_kw = sum(s["loss_kw"] for e, s in zip(network["elements"], element_states) if e["kind"] == "transformer")
    residual = supply_kw - load_kw - losses_kw
    balance_tolerance_kw = max(.01, abs(supply_kw) * 1e-4)
    valid_voltages = [v for v in voltages if v is not None]
    interval_end = datetime(2018, 1, 1) + timedelta(hours=snapshot_time(day, index))
    summary = {"index": index, "timestamp": interval_end.isoformat(timespec="minutes"),
               "supply_kw": supply_kw, "load_kw": load_kw, "load_kvar": load_kvar,
               "supply_kvar": supply_kvar, "loss_kw": losses_kw,
               "line_loss_kw": line_losses_kw, "transformer_loss_kw": transformer_losses_kw,
               "balance_residual_kw": residual, "min_voltage_pu": min((v for v in valid_voltages if v >= .1), default=None),
               "max_voltage_pu": None,
               "low_voltage_buses": low, "high_voltage_buses": high,
               "invalid_voltage_buses": invalid, "deenergized_buses": deenergized,
               "max_line_loading_pct": line_max, "max_transformer_loading_pct": transformer_max,
               "iterations": d.Solution.Iterations(), "control_iterations": d.Solution.ControlIterations(),
               "converged": bool(d.Solution.Converged()),
               "balance_tolerance_kw": balance_tolerance_kw,
               "balance_ok": abs(residual) <= balance_tolerance_kw}
    # Max phase voltage, rather than max of per-bus minima.
    node_mags = d.Circuit.AllBusMagPu()
    summary["max_voltage_pu"] = max((v for v in node_mags if math.isfinite(v)), default=None)
    snapshot = {"index": index, "voltages": voltages, "max_voltages": max_voltages,
                "elements": element_states, "loads": load_states}
    if chargers:
        summary["ev_charge_kw"] = sum(charging_by_bus.values())
        snapshot["ev_kw_by_bus"] = charging_by_bus
    return summary, snapshot


def totals(series):
    energy_in = sum(row["supply_kw"]*.25 for row in series)
    losses = sum(row["loss_kw"]*.25 for row in series)
    return {"supply_kwh": energy_in, "load_kwh": sum(row["load_kw"]*.25 for row in series),
            "loss_kwh": losses, "loss_pct": 100*losses/energy_in if energy_in else 0,
            "line_loss_kwh": sum(row["line_loss_kw"]*.25 for row in series),
            "transformer_loss_kwh": sum(row["transformer_loss_kw"]*.25 for row in series),
            "peak_supply_kw": max((row["supply_kw"] for row in series), default=0),
            "max_balance_residual_kw": max((abs(row["balance_residual_kw"]) for row in series), default=0),
            "voltage_violation_intervals": sum(row["low_voltage_buses"] > 0 or row["high_voltage_buses"] > 0 for row in series),
            "overload_intervals": sum(row["max_line_loading_pct"] > 100 or row["max_transformer_loading_pct"] > 100 for row in series),
            "balance_failures": sum(not row["balance_ok"] for row in series)}


def validate_profiles():
    manifest = json.loads((DATA / "manifest.json").read_text())
    count = 0
    errors = []
    for item in manifest["files"]:
        if "/profiles/" not in item["key"]:
            continue
        path = DATA / item["key"]
        try:
            values = np.loadtxt(path, delimiter=",")
            if values.shape != (35040,) or not np.isfinite(values).all():
                errors.append(f"{path.name}: expected 35040 finite values; got {values.shape}")
            count += 1
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    return {"checked": count, "expected": manifest["profiles"], "errors": errors,
            "passed": count == manifest["profiles"] and not errors}
