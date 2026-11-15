"""Extract Greensboro GTI from public NSRDB HDF5 using fsspec, h5py and pvlib."""

import json
from pathlib import Path

import fsspec
import h5py
import numpy as np
import pandas as pd
import pvlib


ARCHIVE = "https://nrel-pds-nsrdb.s3.us-west-2.amazonaws.com/GOES/conus/v4.0.0/"
OUTPUT = Path(__file__).resolve().parents[1] / "gridlab/resources/greensboro-2018-gti.json"
LATITUDE, LONGITUDE = 36.07, -79.79
TILT, AZIMUTH = 30, 180
OFFSET_HOURS = -5


def read_irradiance(year, selection, site=None):
    url = ARCHIVE + f"nsrdb_conus_irradiance_{year}.h5"
    with fsspec.open(url, block_size=256 * 1024, cache_type="blockcache") as stream:
        with h5py.File(stream) as source:
            if site is None:
                # Bracket Greensboro by longitude in the CONUS site's metadata ordering.
                first, end = 2_500_000, 2_600_000
                cells = source["meta"][first:end]
                east = (cells["longitude"] - LONGITUDE) * np.cos(np.deg2rad(LATITUDE))
                north = cells["latitude"] - LATITUDE
                site = first + int(np.argmin(east**2 + north**2))
            cell = source["meta"][site]
            coords = float(cell["latitude"]), float(cell["longitude"])
            if abs(coords[0] - LATITUDE) > .03 or abs(coords[1] - LONGITUDE) > .03:
                raise ValueError(f"NSRDB site {site} does not match Greensboro: {coords}")
            if int(cell["timezone"]) != OFFSET_HOURS:
                raise ValueError("Unexpected NSRDB site time zone")
            timestamps = pd.to_datetime(source["time_index"][selection].astype(str), utc=True)
            values = {}
            for name in ("ghi", "dni", "dhi"):
                data = source[name]
                values[name] = data[selection, site] / float(data.attrs["scale_factor"])
                print(f"Read {year} {name} for site {site}", flush=True)
            return pd.DataFrame(values, index=timestamps), site, coords


def main():
    # Greensboro's fixed UTC-05:00 calendar begins five hours into the UTC file.
    offset = -OFFSET_HOURS * 12
    current, site, coords = read_irradiance(2018, slice(offset, None))
    tail, _, tail_coords = read_irradiance(2019, slice(0, offset), site=site)
    if coords != tail_coords:
        raise ValueError("NSRDB site coordinates changed across the year boundary")
    irradiance = pd.concat([current, tail])
    expected = pd.date_range("2018-01-01T05:00:00Z", periods=365 * 24 * 12, freq="5min")
    if not irradiance.index.equals(expected):
        raise ValueError("NSRDB time series has missing or misaligned samples")
    sun = pvlib.solarposition.get_solarposition(irradiance.index, *coords)
    gti = pvlib.irradiance.get_total_irradiance(
        TILT, AZIMUTH, sun["apparent_zenith"], sun["azimuth"],
        irradiance["dni"], irradiance["ghi"], irradiance["dhi"], albedo=.2,
    )["poa_global"]
    samples = gti.to_numpy().reshape(-1, 3).mean(axis=1).round(2).tolist()
    payload = {"year":2018, "interval_minutes":15, "utc_offset_hours":OFFSET_HOURS,
               "source":"NSRDB GOES CONUS PSM v4.0.0", "site_index":site,
               "latitude":coords[0], "longitude":coords[1], "tilt_deg":TILT, "azimuth_deg":AZIMUTH,
               "method":"Mean of three 5-minute GTI samples per interval; pvlib isotropic, albedo 0.2",
               "gti_w_m2":samples}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, allow_nan=False, separators=(",", ":")) + "\n")
    temp.replace(OUTPUT)
    print(f"Wrote {len(samples)} intervals to {OUTPUT}", flush=True)


if __name__ == "__main__":
    main()
