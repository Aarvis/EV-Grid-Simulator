"""Download only the selected substation and its referenced annual P/Q profiles."""
import concurrent.futures
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .config import DATA, MODEL_DIR, PREFIX, S3, STATION_KEY


def fetch(url):
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={"User-Agent": "GreensboroGridLab/0.1"}), timeout=90) as response:
                return response.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1 + attempt)


def objects(prefix):
    token = None
    while True:
        args = {"list-type": "2", "prefix": prefix}
        if token:
            args["continuation-token"] = token
        root = ET.fromstring(fetch(S3 + "?" + urlencode(args)))
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        for element in root.findall("s:Contents", ns):
            yield element.find("s:Key", ns).text, int(element.find("s:Size", ns).text)
        token = root.findtext("s:NextContinuationToken", namespaces=ns)
        if not token:
            break


def ensure(key, expected=None):
    target = DATA / key
    if not target.is_file() or (expected is not None and target.stat().st_size != expected):
        content = fetch(S3 + key)
        if expected is not None and len(content) != expected:
            raise ValueError(f"Size mismatch: {key}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".part")
        temp.write_bytes(content)
        temp.replace(target)
    return key


def main():
    listing = [(key, size) for key, size in objects(STATION_KEY + "/")
               if key.lower().endswith((".dss", ".csv", ".txt"))]
    print(f"Downloading {len(listing)} model/reference files", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda item: ensure(*item), listing))
    profile_keys = set()
    for path in MODEL_DIR.rglob("LoadShapes.dss"):
        for reference in re.findall(r"file\s*=\s*([^\s)]+)", path.read_text(), re.I):
            resolved = (path.parent / reference.strip('\"')).resolve()
            if not resolved.is_relative_to(DATA.resolve()):
                raise ValueError("Profile path escapes data directory")
            profile_keys.add(resolved.relative_to(DATA.resolve()).as_posix())
    print(f"Downloading {len(profile_keys)} unique annual P/Q profiles", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for count, _ in enumerate(pool.map(ensure, sorted(profile_keys)), 1):
            if count % 50 == 0 or count == len(profile_keys):
                print(f"Profiles {count}/{len(profile_keys)}", flush=True)
    ensure(PREFIX + "/scenarios/base_timeseries/metrics.csv")
    original = (MODEL_DIR / "Master.dss").read_text()
    # Preserve the source. Only suppress its automatic full-year execution and plotting.
    safe = "\n".join(line for line in original.splitlines()
                     if not re.match(r"\s*(solve|export|plot)\b", line, re.I))
    (MODEL_DIR / "Baseline.dss").write_text(safe + "\n")
    files = []
    for key in sorted({key for key, _ in listing} | profile_keys |
                      {PREFIX + "/scenarios/base_timeseries/metrics.csv"}):
        path = DATA / key
        files.append({"key": key, "bytes": path.stat().st_size,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {"dataset": "SMART-DS v1.0", "year": 2018, "station": "uhs19_1247",
                "scenario": "base_timeseries", "source": S3, "license": "CC BY 4.0",
                "profiles": len(profile_keys), "files": files,
                "adaptation": "Baseline.dss omits Solve/Export/Plot; original Master.dss preserved."}
    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Ready: {len(files)} files, {sum(f['bytes'] for f in files)/1e6:.1f} MB", flush=True)

if __name__ == "__main__":
    main()