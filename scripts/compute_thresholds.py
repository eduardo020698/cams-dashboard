"""Umbrales climatologicos para olas de calor y friajes (metodologia Russo et al. 2014).

Descarga ERA5 diario (Tmax/Tmin 2m, huso utc-05:00) 2003-2022 para el dominio
del dashboard, calcula por celda y por dia del anio:
  - p90 de Tmax diaria (umbral de ola de calor)
  - p10 de Tmin diaria (umbral de friaje)
con ventana movil de 31 dias centrada en el dia evaluado. El 29 de febrero se
obtiene como promedio de los umbrales del 28-feb y 1-mar (muestra reducida de
anios bisiestos), igual que en el paper.

Interpola los umbrales a la grilla CAMS de 0.4 grados y guarda thresholds.npz.
Se ejecuta UNA VEZ (workflow compute-thresholds.yml). Requiere haber aceptado
la licencia de ERA5 en cds.climate.copernicus.eu (misma cuenta ECMWF del ADS).
"""
import calendar
import datetime as dt
import os
import sys
import tempfile

import numpy as np
import xarray as xr

YEARS = list(range(2003, 2023))          # periodo de referencia de 20 anios
AREA = [5, -95, -25, -60]                # N, W, S, E (igual que fetch_cams)
CAMS_LATS = np.arange(5, -25 - 1e-6, -0.4)
CAMS_LONS = np.arange(-95, -60 + 1e-6, 0.4)
WIN = 15                                 # +-15 dias => ventana de 31
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "thresholds.npz")


def get_client():
    import cdsapi
    key = os.environ.get("ADS_API_KEY")  # el token ECMWF sirve para CDS y ADS
    return cdsapi.Client(url="https://cds.climate.copernicus.eu/api",
                         key=key, quiet=True)


CHUNKS = [YEARS[i:i + 5] for i in range(0, len(YEARS), 5)]  # 4 bloques de 5 anios


def _chunk_path(workdir, stat, years):
    return os.path.join(workdir, f"{stat}_{years[0]}_{years[-1]}.nc")


def request_chunk(stat, years, workdir):
    """Una peticion de 5 anios (cada hilo con su propio cliente)."""
    f = _chunk_path(workdir, stat, years)
    if os.path.exists(f):
        return f
    print(f"[era5] {stat} {years[0]}-{years[-1]} en cola...", flush=True)
    get_client().retrieve(
        "derived-era5-single-levels-daily-statistics",
        {
            "product_type": "reanalysis",
            "variable": ["2m_temperature"],
            "year": [str(y) for y in years],
            "month": [f"{m:02d}" for m in range(1, 13)],
            "day": [f"{d:02d}" for d in range(1, 32)],
            "daily_statistic": f"daily_{stat}",
            "time_zone": "utc-05:00",
            "frequency": "1-hourly",
            "area": AREA,
        }, f)
    print(f"[era5] {stat} {years[0]}-{years[-1]} descargado", flush=True)
    return f


def fetch_all(workdir):
    """Envia las 8 peticiones EN PARALELO: la cola del CDS las procesa a la vez."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = [(s, ch) for s in ("maximum", "minimum") for ch in CHUNKS]
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = [ex.submit(request_chunk, s, ch, workdir) for s, ch in jobs]
        for fut in futs:
            fut.result()  # propaga cualquier error


def load_stat(stat, workdir):
    """Concatena los bloques descargados de una estadistica."""
    arrays, dates = [], []
    for ch in CHUNKS:
        ds = xr.open_dataset(_chunk_path(workdir, stat, ch))
        var = [v for v in ds.data_vars if ds[v].ndim >= 3][0]
        tdim = [d for d in ds[var].dims if "time" in d][0]
        arrays.append(ds[var].values.astype(np.float32))
        dates += [np.datetime64(t, "D").astype(dt.date) for t in ds[tdim].values]
        lats, lons = ds["latitude"].values, ds["longitude"].values
        ds.close()
    return np.concatenate(arrays, axis=0), dates, lats, lons


def doy_noleap(d):
    """Dia del anio 0-364 en calendario sin bisiestos (29-feb -> 28-feb)."""
    if d.month == 2 and d.day == 29:
        d = d.replace(day=28)
    return (dt.date(2001, d.month, d.day) - dt.date(2001, 1, 1)).days


def percentile_climatology(data, dates, q):
    """Percentil q por celda y dia del anio con ventana de 31 dias (366 filas)."""
    doys = np.array([doy_noleap(d) for d in dates])
    out = np.empty((366,) + data.shape[1:], dtype=np.float32)
    for d in range(365):
        window = {(d + k) % 365 for k in range(-WIN, WIN + 1)}
        m = np.isin(doys, list(window))
        out[d] = np.percentile(data[m], q, axis=0)
        if d % 60 == 0:
            print(f"  doy {d}/365 ({m.sum()} muestras)", flush=True)
    # 29 de febrero: promedio de 28-feb (doy 58) y 1-mar (doy 59)
    out[365] = 0.5 * (out[58] + out[59])
    return out


def to_cams_grid(field366, lats, lons):
    da = xr.DataArray(field366, dims=("doy", "lat", "lon"),
                      coords={"lat": lats, "lon": lons})
    return da.interp(lat=CAMS_LATS, lon=CAMS_LONS,
                     kwargs={"fill_value": None}).values.astype(np.float32)


def main():
    with tempfile.TemporaryDirectory() as wd:
        fetch_all(wd)  # 8 peticiones en paralelo (4 bloques x 2 estadisticas)
        print("== Tmax diaria (umbral p90, olas de calor) ==")
        tmax, dates, lats, lons = load_stat("maximum", wd)
        p90 = to_cams_grid(percentile_climatology(tmax, dates, 90), lats, lons)
        del tmax
        print("== Tmin diaria (umbral p10, friajes) ==")
        tmin, dates, lats, lons = load_stat("minimum", wd)
        p10 = to_cams_grid(percentile_climatology(tmin, dates, 10), lats, lons)
        del tmin
    np.savez_compressed(OUT, p90=p90, p10=p10,
                        lats=CAMS_LATS.astype(np.float32),
                        lons=CAMS_LONS.astype(np.float32),
                        ref="ERA5 2003-2022, utc-05, ventana 31d, Russo et al. 2014")
    print(f"[done] {OUT} ({os.path.getsize(OUT)//1024//1024} MB)")


if __name__ == "__main__":
    main()
