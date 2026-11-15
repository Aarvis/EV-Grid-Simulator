"""Official NHTS download and conservative, auditable vehicle-day diary extraction."""
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from urllib.request import urlopen
import zipfile
from .config import DATA
from .fleet import digest

FOLDER = DATA / "nhts" / "2022-v2.1"
URL = "https://nhts.ornl.gov/media/2022/download/csv.zip"
CODEBOOK = "https://nhts.ornl.gov/media/2022/doc/codebook.xlsx"
HOME = {1, 2}
PURPOSES = {1:"Home", 2:"Work from home", 3:"Work", 4:"Work pickup/drop-off",
    5:"Other work", 6:"School", 7:"Care", 8:"Volunteering", 9:"Transfer",
    10:"Personal pickup/drop-off", 11:"Health care", 12:"Meals", 13:"Shopping",
    14:"Errands", 15:"Recreation", 16:"Exercise", 17:"Visit", 18:"Vacation/rest",
    19:"Community/religion", 97:"Other"}
PARSER_VERSION = 1


def integer(value):
    return int(value)


def minute(value):
    value = integer(value)
    hour, mins = divmod(value, 100)
    if not 0 <= hour < 24 or not 0 <= mins < 60:
        raise ValueError("Invalid local trip time")
    return hour * 60 + mins


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, allow_nan=False))
    temp.replace(path)


def parse_archive(path):
    """One diary per rostered household vehicle; passenger copies never count twice.

    Reject whole vehicle-days when a driver leg is invalid, rather than silently
    dropping a leg and claiming the remaining chain is complete.
    """
    with zipfile.ZipFile(path) as archive:
        def records(name):
            member = next(n for n in archive.namelist() if Path(n).name.lower() == name)
            return list(csv.DictReader(io.TextIOWrapper(archive.open(member), encoding="utf-8-sig")))
        households = {r["HOUSEID"]:r for r in records("hhv2pub.csv")}
        vehicles = {(r["HOUSEID"], integer(r["VEHID"])):r for r in records("vehv2pub.csv")}
        trips = records("tripv2pub.csv")
    grouped = defaultdict(list)
    for r in trips:
        if integer(r["DRVR_FLG"]) == 1 and integer(r["VEHID"]) > 0:
            grouped[r["HOUSEID"], integer(r["VEHID"])].append(r)
    rejected = Counter()
    diaries = []
    for key, rows in sorted(grouped.items()):
        try:
            hh, vehicle = households[key[0]], vehicles[key]
            if integer(vehicle["VEHTYPE"]) not in {1,2,3,4}:
                raise ValueError("non_light_vehicle")
            if integer(hh["FLAG100"]) != 1:
                raise ValueError("incomplete_household_diaries")
            weight = float(vehicle["WTHHFIN"])
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError("invalid_vehicle_weight")
            # A respondent's reported sequence may cross midnight; do not reorder
            # that into an apparently valid midnight-to-midnight vehicle-day.
            people = defaultdict(list)
            for r in rows:
                people[r["PERSONID"]].append(r)
            for rr in people.values():
                rr.sort(key=lambda r:integer(r["SEQ_TRIPID"]))
                starts = [minute(r["STRTTIME"]) for r in rr]
                if starts != sorted(starts):
                    raise ValueError("overnight_sequence")
            legs = []
            for r in rows:
                if integer(r["TRPHHVEH"]) != 1 or integer(r["TRPTRANS"]) not in {1,2,3,4}:
                    raise ValueError("ineligible_driver_leg")
                start, end = minute(r["STRTTIME"]), minute(r["ENDTIME"])
                distance = float(r["TRPMILES"]) * 1.609344
                duration = float(r["TRVLCMIN"])
                origin, destination = integer(r["WHYFROM"]), integer(r["WHYTO"])
                if (not math.isfinite(distance) or distance <= 0 or end <= start or
                    not math.isfinite(duration) or abs(duration-(end-start)) > 5):
                    raise ValueError("invalid_distance_or_duration")
                if distance / ((end-start)/60) > 160:
                    raise ValueError("implausible_average_speed")
                if origin not in PURPOSES or destination not in PURPOSES:
                    raise ValueError("unknown_purpose")
                if r["TDAYDATE"] != hh["TDAYDATE"] or r["TRAVDAY"] != hh["TRAVDAY"]:
                    raise ValueError("inconsistent_day")
                legs.append({"depart_minute":start, "arrive_minute":end,
                    "distance_km":distance, "origin_code":origin, "destination_code":destination,
                    "origin":PURPOSES[origin], "destination":PURPOSES[destination],
                    "source_person_id":r["PERSONID"], "source_trip_id":r["TRIPID"]})
            legs.sort(key=lambda t:(t["depart_minute"],t["arrive_minute"]))
            if legs[0]["origin_code"] not in HOME or legs[-1]["destination_code"] not in HOME:
                raise ValueError("not_home_to_home")
            for previous, current in zip(legs, legs[1:]):
                if previous["arrive_minute"] > current["depart_minute"]:
                    raise ValueError("overlapping_vehicle_trips")
                a, b = previous["destination_code"], current["origin_code"]
                if a != b and not (a in HOME and b in HOME):
                    raise ValueError("broken_purpose_chain")
            total = sum(t["distance_km"] for t in legs)
            diaries.append({"id":f"{key[0]}:{key[1]}", "month":int(hh["TDAYDATE"][-2:]),
                "survey_yyyymm":hh["TDAYDATE"], "weekday":integer(hh["TRAVDAY"]),
                "weight":weight, "total_km":total, "trips":legs})
        except (ValueError, KeyError, TypeError) as exc:
            rejected[str(exc)] += 1
    if not diaries:
        raise ValueError("No valid home-to-home NHTS vehicle-day diaries")
    return {"parser_version":PARSER_VERSION, "diaries":diaries,
        "audit":{"raw_trip_records":len(trips), "driver_vehicle_days":len(grouped),
            "accepted_vehicle_days":len(diaries), "rejected_vehicle_days":dict(rejected),
            "by_month":dict(sorted(Counter(d["month"] for d in diaries).items())),
            "by_weekday":dict(sorted(Counter(d["weekday"] for d in diaries).items()))}}


def prepare():
    FOLDER.mkdir(parents=True, exist_ok=True)
    for name, url in [("csv.zip", URL), ("codebook.xlsx", CODEBOOK)]:
        path = FOLDER / name
        if not path.exists():
            with urlopen(url, timeout=90) as response:
                content = response.read()
            # Avoid caching an HTML error page as a dataset.
            if not zipfile.is_zipfile(io.BytesIO(content)):
                raise ValueError(f"NHTS {name} was not a valid archive")
            temp = path.with_suffix(".part")
            temp.write_bytes(content)
            temp.replace(path)
    archive = FOLDER / "csv.zip"
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    cached = FOLDER / "diaries.json"
    item = json.loads(cached.read_text()) if cached.exists() else None
    if not item or item.get("archive_sha256") != checksum or item.get("parser_version") != PARSER_VERSION:
        item = parse_archive(archive)
        item.update(archive_sha256=checksum, source_url=URL,
            citation="Federal Highway Administration. (2022). 2022 National Household Travel Survey, U.S. Department of Transportation, Washington, DC. https://nhts.ornl.gov.",
            source_scope="National light household vehicles, not measured Greensboro EV travel",
            filtering="All household members completed diaries; driver legs only; rostered car/van/SUV/pickup; valid local times and positive distances; no overlaps; purpose-continuous home-to-home chain; no cross-midnight journeys",
            weighting="Vehicle WTHHFIN weights used within nearby-distance diary matches; not trip weights. Distance distribution is imposed by scenario.")
        item["digest"] = digest(item)
        atomic_json(cached, item)
    return item


if __name__ == "__main__":
    item = prepare()
    print(json.dumps({"digest":item["digest"],"audit":item["audit"]},indent=2))
