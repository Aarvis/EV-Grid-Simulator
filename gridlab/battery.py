"""Value stored EV electricity using interval generation costs and metered charging."""
import math


def build_ledger(schedule, rows, opening_price_per_mwh, efficiency, settlement=None):
    # Attribute net changes in modeled grid losses in proportion to charger draw.
    # A grid loss saving can reduce attributed energy to zero. Keep any residual visible.
    meters = [interval["grid_kwh"] for interval in schedule["intervals"]]
    attributed = [max(-meter, row["delta_loss_kwh"]) if meter > 0 else 0.
                  for meter, row in zip(meters, rows, strict=True)]
    loss_rates = [loss / meter if meter > 0 else 0. for loss, meter in zip(attributed, meters)]
    opening_loss_rate = sum(attributed) / sum(meters) if sum(meters) else None
    if settlement is not None:
        loss_rates = [rate for _, rate in settlement]
        attributed = [meter * rate for meter, rate in zip(meters, loss_rates, strict=True)]
    interval_totals = [{"ev_attributed_loss_kwh": loss,
                        "ev_unallocated_loss_change_kwh": row["delta_loss_kwh"] - loss if settlement is None else 0.,
                        "ev_charging_cost_usd": 0., "ev_driving_cost_usd": 0., "ev_battery_value_usd": 0.}
                       for loss, row in zip(attributed, rows)]
    vehicles = {}
    for vehicle in schedule["vehicles"]:
        stored = vehicle["capacity_kwh"] * vehicle["initial_soc"]
        opening_draw = stored / efficiency
        opening_loss = opening_draw * (opening_loss_rate or 0.)
        value = (opening_draw + opening_loss) * opening_price_per_mwh / 1000
        opening = {"stored_kwh": stored, "estimated_charger_kwh": opening_draw,
                   "estimated_distribution_loss_kwh": opening_loss, "value_usd": value}
        intervals = []
        for index, events in enumerate(vehicle["energy_events"]):
            price = rows[index]["average_generation_cost_per_mwh"]
            quote = price if settlement is None else settlement[index][0]
            charged = drawn = losses = used = unmet = charge_cost = drive_cost = 0.
            # Replay the original within-interval order, which can mix driving and charging.
            for change, meter, shortfall in events:
                if meter > 0:
                    loss = meter * loss_rates[index]
                    cost = (meter * quote + loss * price) / 1000
                    value += cost
                    charged += change
                    drawn += meter
                    losses += loss
                    charge_cost += cost
                else:
                    used -= change
                    unmet += shortfall
                    cost = 0. if not stored else value * min(1., -change / stored)
                    value -= cost
                    drive_cost += cost
                stored += change
            home = vehicle["home_grid_kwh"][index]
            intervals.append({"stored_kwh": stored, "value_usd": value,
                              "average_cost_usd_per_kwh": value / stored if stored > 0 else None,
                              "home_draw_kwh": home, "away_draw_kwh": max(0., drawn - home),
                              "distribution_loss_kwh": losses, "charged_kwh": charged,
                              "driving_kwh": used, "unmet_kwh": unmet,
                              "charging_cost_usd": charge_cost, "driving_cost_usd": drive_cost,
                              "driving_cost_usd_per_km": drive_cost / used * vehicle["driving_kwh_per_km"] if used > 0 else None})
            interval_totals[index]["ev_charging_cost_usd"] += charge_cost
            interval_totals[index]["ev_driving_cost_usd"] += drive_cost
            interval_totals[index]["ev_battery_value_usd"] += value
            if not math.isclose(stored, vehicle["capacity_kwh"] * vehicle["end_soc"][index], abs_tol=1e-8):
                raise RuntimeError(f"Battery ledger energy differs from the schedule for {vehicle['id']}")
        vehicles[vehicle["id"]] = {"id": vehicle["id"], "opening": opening, "intervals": intervals}
    summary = {"opening_loss_kwh_per_charger_kwh": opening_loss_rate,
               "opening_value_usd": sum(v["opening"]["value_usd"] for v in vehicles.values()),
               "closing_value_usd": interval_totals[-1]["ev_battery_value_usd"],
               "charging_cost_usd": sum(row["ev_charging_cost_usd"] for row in interval_totals),
               "driving_cost_usd": sum(row["ev_driving_cost_usd"] for row in interval_totals),
               "unallocated_loss_change_kwh": sum(row["ev_unallocated_loss_change_kwh"] for row in interval_totals)}
    return vehicles, summary, interval_totals
