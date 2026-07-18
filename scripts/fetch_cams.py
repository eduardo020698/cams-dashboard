#!/usr/bin/env python3
"""
Descarga diaria de CAMS global atmospheric composition forecasts (ADS)
para Peru + Pacifico y genera JSON compacto para el dashboard.

- Variables superficie: PM2.5, PM10 (kg/m3) + presion superficial
- Nivel de modelo 137 (~superficie): O3, SO2, NO2 (kg/kg) + temperatura
- Conversion a ug/m3 usando densidad del aire rho = sp / (R * T)
- Retencion: solo los ultimos 10 dias disponibles (KEEP_DAYS)

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
AREA = [5, -95, -25, -60]  # Norte, Oeste, Sur, Este (Peru + Pacifico, ampliado)
LEADS = ["0", "3", "6", "9", "12", "15", "18", "21"]  # horas del run 00Z
FC_LEADS = [str(h) for h in range(24, 121, 3)]  # pronostico: +24h a +120h
KEEP_DAYS = 10
MAX_TRIES = 13  # dias hacia atras a intentar hasta juntar KEEP_DAYS
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


def retrieve_day(client, date_str, workdir, leads=LEADS):
    """Descarga los dos GRIB (superficie y nivel de modelo) para un dia."""
    sl = os.path.join(workdir, f"sl_{date_str}_{len(leads)}.grib")
    ml = os.path.join(workdir, f"ml_{date_str}_{len(leads)}.grib")
    common = {"date": f"{date_str}/{date_str}", "time": "00:00",
              "leadtime_hour": leads, "type": "forecast",
              "data_format": "grib", "area": AREA}
    client.retrieve(DATASET, dict(common, variable=list(SL_VARS) + ["surface_pressure", "2m_temperature"]), sl)
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

    # horas de pronostico reales presentes en el archivo
    hours = (ds_sl["step"].values / np.timedelta64(1, "h")).astype(int).tolist()
    if not isinstance(hours, list):
        hours = [int(hours)]

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
    # temperatura del aire a 2 m (K -> grados C)
    out_vars["t2m"] = (np.squeeze(ds_sl["t2m"].values) - 273.15, 1)

    payload = {
        "date": date_str,
        "run": "00Z",
        "hours": hours,
        "lat0": float(lats[0]), "dlat": float(lats[1] - lats[0]), "nlat": len(lats),
        "lon0": float(lons[0]), "dlon": float(lons[1] - lons[0]), "nlon": len(lons),
        "units": "ug/m3",
        "vars": {},
    }
    for key, (arr, dec) in out_vars.items():
        arr = np.nan_to_num(arr, nan=-9999.0)  # centinela de dato faltante
        if arr.ndim == 2:  # un solo paso de tiempo
            arr = arr[None, :, :]
        payload["vars"][key] = [
            [round(float(x), dec) for x in arr[i].ravel()] for i in range(arr.shape[0])
        ]
    return payload


def _doy_index(d):
    """Indice 0-364 (calendario sin bisiestos) o 365 para el 29-feb."""
    if d.month == 2 and d.day == 29:
        return 365
    return (dt.date(2001, d.month, d.day) - dt.date(2001, 1, 1)).days


def _day_t2m_stats(arr):
    """Tmax/Tmin diarios a partir de (nt, ncell) con centinela -9999."""
    a = np.array(arr, dtype=np.float32)
    a[a <= -9000] = np.nan
    with np.errstate(all="ignore"):
        return np.nanmax(a, axis=0), np.nanmin(a, axis=0)


def add_thermal_events(payloads, fc_payload, fc_base):
    """Olas de calor / friajes (Russo et al. 2014) con intensidad continua.

    Agrega la variable 'hw' a cada payload: grados por encima del p90 (calor,
    positivo) o por debajo del p10 (friaje, negativo) durante rachas de >=3
    dias consecutivos; 0 = sin evento. Umbrales climatologicos de ERA5.
    """
    path = os.path.join(ROOT, "thresholds.npz")
    if not os.path.exists(path):
        print("[hw] thresholds.npz no existe aun (corre el workflow de "
              "umbrales); se omite la capa de eventos")
        return
    th = np.load(path)
    p90 = th["p90"].reshape(366, -1)
    p10 = th["p10"].reshape(366, -1)

    entries = []  # (fecha, tmax, tmin, [(payload, indices_de_timestep)])
    for ds_str, pl in sorted(payloads.items()):
        d = dt.date.fromisoformat(ds_str)
        tmax, tmin = _day_t2m_stats(pl["vars"]["t2m"])
        entries.append((d, tmax, tmin, [(pl, list(range(len(pl["hours"]))))]))
    if fc_payload is not None:
        base = dt.date.fromisoformat(fc_base)
        groups = {}
        for i, h in enumerate(fc_payload["hours"]):
            groups.setdefault(base + dt.timedelta(days=int(h) // 24), []).append(i)
        for day, idxs in sorted(groups.items()):
            sub = [fc_payload["vars"]["t2m"][i] for i in idxs]
            tmax, tmin = _day_t2m_stats(sub)
            entries.append((day, tmax, tmin, [(fc_payload, idxs)]))

    TX = np.stack([e[1] for e in entries])
    TN = np.stack([e[2] for e in entries])
    P90 = np.stack([p90[_doy_index(e[0])] for e in entries])
    P10 = np.stack([p10[_doy_index(e[0])] for e in entries])
    with np.errstate(invalid="ignore"):
        hot, cold = TX >= P90, TN <= P10
    hotev = np.zeros_like(hot)
    coldev = np.zeros_like(cold)
    for i in range(len(entries) - 2):  # racha minima de 3 dias consecutivos
        tri = hot[i] & hot[i + 1] & hot[i + 2]
        hotev[i] |= tri; hotev[i + 1] |= tri; hotev[i + 2] |= tri
        tri = cold[i] & cold[i + 1] & cold[i + 2]
        coldev[i] |= tri; coldev[i + 1] |= tri; coldev[i + 2] |= tri
    inten = (np.where(hotev, TX - P90, 0.0) +
             np.where(coldev, TN - P10, 0.0))
    inten = np.round(np.nan_to_num(inten, nan=0.0), 1)

    for i, (day, _, _, sinks) in enumerate(entries):
        row = inten[i].tolist()
        for pl, idxs in sinks:
            hw = pl["vars"].setdefault("hw", [None] * len(pl["hours"]))
            for j in idxs:
                hw[j] = row
    for pl in list(payloads.values()) + ([fc_payload] if fc_payload else []):
        if "hw" in pl["vars"]:  # sanea huecos por si algun timestep quedo sin dia
            zeros = [0.0] * len(pl["vars"]["t2m"][0])
            pl["vars"]["hw"] = [r if r is not None else zeros
                                for r in pl["vars"]["hw"]]
    n_ev = int(hotev.any(axis=0).sum() + coldev.any(axis=0).sum())
    print(f"[hw] eventos detectados en {n_ev} celdas del dominio")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    client = get_client()
    today = dt.datetime.now(dt.timezone.utc).date()

    payloads = {}
    with tempfile.TemporaryDirectory() as workdir:
        for back in range(MAX_TRIES):
            if len(payloads) >= KEEP_DAYS:
                break
            d = today - dt.timedelta(days=back)
            ds = d.strftime("%Y-%m-%d")
            try:
                print(f"[fetch] {ds} ...", flush=True)
                sl, ml = retrieve_day(client, ds, workdir)
                payloads[ds] = to_json(sl, ml, ds)
            except Exception as e:
                print(f"[warn] {ds} no disponible: {e}", file=sys.stderr)
    got = sorted(payloads)

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

    # pronostico: +24h a +120h desde el run mas reciente disponible
    fc_base, fc_payload = None, None
    try:
        base = max(got)
        print(f"[fc] pronostico desde run {base} 00Z (+24h a +120h) ...", flush=True)
        with tempfile.TemporaryDirectory() as wd:
            sl, ml = retrieve_day(client, base, wd, leads=FC_LEADS)
            fc_payload = to_json(sl, ml, base)
            fc_payload["forecast"] = True
        fc_base = base
    except Exception as e:
        print(f"[warn] pronostico no disponible: {e}", file=sys.stderr)
        fc_payload = None

    # olas de calor y friajes sobre la serie completa (pasado + pronostico)
    try:
        add_thermal_events(payloads, fc_payload, fc_base)
    except Exception as e:
        print(f"[warn] capa de eventos termicos fallo: {e}", file=sys.stderr)

    # escribir todos los archivos
    for ds_str, pl in payloads.items():
        out_path = os.path.join(DATA_DIR, f"{ds_str}.json")
        with open(out_path, "w") as f:
            json.dump(pl, f, separators=(",", ":"))
        print(f"[ok] {out_path} ({os.path.getsize(out_path)//1024} KB)")
    fc_path = os.path.join(DATA_DIR, "forecast.json")
    if fc_payload is not None:
        with open(fc_path, "w") as f:
            json.dump(fc_payload, f, separators=(",", ":"))
        print(f"[ok] forecast.json (base {fc_base})")
    elif os.path.exists(fc_path):
        os.remove(fc_path)  # evitar pronostico viejo

    manifest = {"dates": sorted(got), "forecast": fc_base,
                "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    with open(os.path.join(DATA_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    print(f"[done] dias: {manifest['dates']} + forecast: {fc_base}")


if __name__ == "__main__":
    main()
