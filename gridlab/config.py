from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RUNS = ROOT / "runs"
STATION = "uhs19_1247"
MAX_SIMULATION_DAYS = 150
PREFIX = "SMART-DS/v1.0/2018/GSO/urban-suburban"
STATION_KEY = f"{PREFIX}/scenarios/base_timeseries/opendss/{STATION}"
MODEL_DIR = DATA / STATION_KEY
MODEL = MODEL_DIR / "Baseline.dss"
S3 = "https://oedi-data-lake.s3.amazonaws.com/"
RUNS.mkdir(exist_ok=True)
