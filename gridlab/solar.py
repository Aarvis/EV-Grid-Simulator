"""Load the fixed-calendar Greensboro solar resource for upstream dispatch."""

import hashlib
import json
import math
from pathlib import Path


PROFILE = Path(__file__).with_name("resources") / "greensboro-2018-gti.json"


def load_solar(config):
    if config.solar_capacity_mw == 0:
        return (), None
    raw = PROFILE.read_bytes()
    resource = json.loads(raw)
    irradiance = resource.pop("gti_w_m2")
    if (resource["year"] != 2018 or resource["interval_minutes"] != 15
            or resource["utc_offset_hours"] != -5 or len(irradiance) != 365 * 96):
        raise ValueError("Expected a full Greensboro 2018 solar profile at 15-minute resolution")
    if any(not math.isfinite(value) or value < 0 for value in irradiance):
        raise ValueError("Solar irradiance must be finite and nonnegative")
    scale = config.solar_capacity_mw * config.solar_performance_factor / 1000
    resource["sha256"] = hashlib.sha256(raw).hexdigest()
    return tuple(value * scale for value in irradiance), resource
