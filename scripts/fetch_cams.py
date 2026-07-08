#!/usr/bin/env python3
"""
Descarga diaria de CAMS global atmospheric composition forecasts (ADS)
para Peru + Pacifico y genera JSON compacto para el dashboard.

- Variables superficie: PM2.5, PM10 (kg/m3) + presion superficial
- Nivel de modelo 137 (~superficie): O3, SO2, NO2 (kg/kg) + temperatura
- Conversion a ug/m3 usando densidad del aire rho = sp / (R * T)
- Retencion: solo los ultimos 3 dias disponibles (KEEP_DAYS)

Uso:
  ADS_API_KEY=xxxx python scripts/fetch_cams.py
  (o con ~/.cdsapirc configurado)
"""
import datetime as dt
import glob
import json
import os
import sys
import tempfile

import numpy as np

DATASET = "cams-global-atmospheric-composition-forecasts"
AREA = [2, -92, -20, -66]  # Norte, Oeste, Sur, Este (Peru + Pacifico)
LEADS = ["0", "3", "6", "9", "12", "15", "18", "21"]  # horas del run 00Z
HOURS = [int(h) for h in LEADS]
KEEP_DAYS = 3
MAX_TRIES = 5  # dias hacia atras a intentar hasta juntar KEEP_DAYS
R_AIR = 287.058  # J kg-1 K-1

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "site", "data")

# variable -> (nombre corto GRIB, decimales en JSON)
SL_VARS = {"particulate_matter_2.5um": ("pm2p5", 1),
           "particulate_matter_10um": ("pm10", 1)}
ML_VARS = {"ozone": ("go3", 1),
           "sulphur_dioxide": ("so2", 2),
           "nitrogen_dioxide": ("no2", 2)}
OUT_KEYS = {"pm2p5": "pm25", "pm10": "pm10", "go3": "o3",
            "so2": "so2", "no2": "no2"}


def get_client():
    import cdsapi
    key = os.environ.get("ADS_API_KEY")
    if key:
        return cdsapi.Client(url="https://ads.atmosphere.copernicus.eu/api",
                             key=key, quiet=True)
    return cdsapi.Client(quiet=True)  # usa ~/.cdsapirc


def retrieve_day(client, date_str, workdir):
    """Descarga los dos GRIB (superficie y nivel de modelo) para un dia."""
    sl = os.path.join(workdir, f"sl_{date_str}.grib")
    ml = os.path.join(workdir, f"ml_{date_str}.grib")
    common = {"date": f"{date_str}/{date_str}", "time": "00:00",
              "leadtime_hour": LEADS, "type": "forecast",
              "data_format": "grib", "area": AREA}
    client.retrieve(DATASET, dict(common, variable=list(SL_VARS) + ["surface_pressure"]), sl)
    client.retrieve(DATASET, dict(common, variable=list(ML_VARS) + ["temperature"],
                                  model_level="137"), ml)
    return sl, ml


def open_merged(path):
    import cfgrib
    import xarray as xr
    dss = cfgrib.open_datasets(path, backend_kwargs={"indexpath": ""})
    return xr.merge(dss, compat="override")


def to_json(sl_path, ml_path, date_str):
    """Convierte GRIBs a dict JSON con concentraciones en ug/m3."""
    ds_sl = open_merged(sl_path)
    ds_ml = open_merged(ml_path)

    lats = ds_sl["latitude"].values.astype(float)
    lons = ds_sl["longitude"].values.astype(float)
    lons = np.where(lons > 180, lons - 360, lons)

    def steps_sorted(ds):
        order = np.argsort(ds["step"].values)
        return ds.isel(step=order)

    ds_sl, ds_ml = steps_sorted(ds_sl), steps_sorted(ds_ml)

    # densidad del aire (nt, nlat, nlon)
    sp = np.squeeze(ds_sl["sp"].values)
    t = np.squeeze(ds_ml["t"].values)
    rho = sp / (R_AIR * t)

    out_vars = {}
    for gname, (short, dec) in SL_VARS.items():
        v = np.squeeze(ds_sl[short].values) * 1e9  # kg/m3 -> ug/m3
        out_vars[OUT_KEYS[short]] = (v, dec)
    for gname, (short, dec) in ML_VARS.items():
        v = np.squeeze(ds_ml[short].values) * rho * 1e9  # kg/kg -> ug/m3
        out_vars[OUT_KEYS[short]] = (v, dec)

    payload = {
        "date": date_str,
        "run": "00Z",
        "hours": HOURS,
        "lat0": float(lats[0]), "dlat": float(lats[1] - lats[0]), "nlat": len(lats),
        "lon0": float(lons[0]), "dlon": float(lons[1] - lons[0]), "nlon": len(lons),
        "units": "ug/m3",
        "vars": {},
    }
    for key, (arr, dec) in out_vars.items():
        arr = np.nan_to_num(arr, nan=-1.0)
        if arr.ndim == 2:  # un solo paso de tiempo
            arr = arr[None, :, :]
        payload["vars"][key] = [
            [round(float(x), dec) for x in arr[i].ravel()] for i in range(arr.shape[0])
        ]
    return payload


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    client = get_client()
    today = dt.datetime.now(dt.timezone.utc).date()

    got = []
    with tempfile.TemporaryDirectory() as workdir:
        for back in range(MAX_TRIES):
            if len(got) >= KEEP_DAYS:
                break
            d = today - dt.timedelta(days=back)
            ds = d.strftime("%Y-%m-%d")
            out_path = os.path.join(DATA_DIR, f"{ds}.json")
            try:
                print(f"[fetch] {ds} ...", flush=True)
                sl, ml = retrieve_day(client, ds, workdir)
                payload = to_json(sl, ml, ds)
                with open(out_path, "w") as f:
                    json.dump(payload, f, separators=(",", ":"))
                print(f"[ok] {out_path} ({os.path.getsize(out_path)//1024} KB)")
                got.append(ds)
            except Exception as e:
                print(f"[warn] {ds} no disponible: {e}", file=sys.stderr)

    if not got:
        print("[error] no se pudo descargar ningun dia", file=sys.stderr)
        sys.exit(1)

    # retencion: borrar todo lo que no sea los ultimos KEEP_DAYS
    keep = set(f"{d}.json" for d in got)
    for f in glob.glob(os.path.join(DATA_DIR, "*.json")):
        base = os.path.basename(f)
        if base != "manifest.json" and base not in keep:
            os.remove(f)
            print(f"[prune] {base}")

    manifest = {"dates": sorted(got),
                "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    with open(os.path.join(DATA_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    print(f"[done] dias: {manifest['dates']}")


if __name__ == "__main__":
    main()
