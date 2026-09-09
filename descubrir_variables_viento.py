"""
FASE 2 — ahora que sabemos que el bucket es s3://smn-ar-wrf/DATA/WRF/DET/
con archivos NetCDF tipo:
  DATA/WRF/DET/<AAAA>/<MM>/<DD>/<HH>/WRFDETAR_01H_<AAAAMMDD>_<HH>_<FFF>.nc
(AAAA/MM/DD/HH = cuándo se inicializó la corrida, FFF = hora de
pronóstico 000..072), este script:

  1. Busca la corrida más reciente que encuentra publicada.
  2. Se baja UN archivo (el de hora de pronóstico más chica) de esa
     corrida.
  3. Lo abre con netCDF4 e imprime: todas las variables y sus formas,
     los atributos de las que tengan pinta de ser viento (nombre con
     "WIND", "U10", "V10", "WSPD", "WDIR", etc.), la grilla de
     lat/lon, y el valor de viento en el punto de grilla más cercano
     a Buenos Aires (-34.6037, -58.3816) — para confirmar que
     podemos ubicar el Río de la Plata dentro de la grilla.

No hace falta entender el resultado — mandámelo tal cual (o
captura/copiado del log) y con eso ya armo el scraper real.
"""
import sys
from datetime import datetime, timezone

import requests

BASE = "https://smn-ar-wrf.s3.amazonaws.com/"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

# Buenos Aires, como referencia del Río de la Plata
LAT_OBJETIVO = -34.6037
LON_OBJETIVO = -58.3816


def listar_subprefijos(prefix: str) -> list[str]:
    from xml.etree import ElementTree

    resp = requests.get(
        BASE, params={"list-type": "2", "prefix": prefix, "delimiter": "/", "max-keys": "1000"}, timeout=60
    )
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.text)
    return sorted(
        el.text for el in root.findall(".//s3:CommonPrefixes/s3:Prefix", NS) if el.text
    )


def listar_archivos(prefix: str) -> list[str]:
    from xml.etree import ElementTree

    resp = requests.get(
        BASE, params={"list-type": "2", "prefix": prefix, "max-keys": "1000"}, timeout=60
    )
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.text)
    return sorted(
        el.text for el in root.findall(".//s3:Contents/s3:Key", NS) if el.text
    )


def encontrar_corrida_mas_reciente() -> str:
    base = "DATA/WRF/DET/"
    anios = listar_subprefijos(base)
    if not anios:
        raise RuntimeError(f"No encontré subcarpetas en {base}")
    anio = anios[-1]
    meses = listar_subprefijos(anio)
    mes = meses[-1]
    dias = listar_subprefijos(mes)
    dia = dias[-1]
    horas = listar_subprefijos(dia)
    hora = horas[-1]
    print(f"Corrida más reciente encontrada: {hora}")
    return hora


def main():
    print("=== Buscando la corrida más reciente ===")
    prefijo_corrida = encontrar_corrida_mas_reciente()

    archivos = listar_archivos(prefijo_corrida)
    print(f"\n{len(archivos)} archivos en esa corrida. Primeros y últimos:")
    print(archivos[:3], "...", archivos[-3:] if len(archivos) > 3 else "")

    if not archivos:
        print("No hay archivos, no puedo seguir.")
        sys.exit(1)

    archivo_clave = archivos[0]  # el de hora de pronóstico más chica
    print(f"\n=== Bajando {archivo_clave} ===")
    resp = requests.get(BASE + archivo_clave, timeout=120)
    resp.raise_for_status()
    ruta_local = "/tmp/muestra.nc"
    with open(ruta_local, "wb") as f:
        f.write(resp.content)
    print(f"Bajado: {len(resp.content) / 1024:.0f} KB")

    print("\n=== Abriendo con netCDF4 ===")
    from netCDF4 import Dataset

    ds = Dataset(ruta_local)
    print("\nDimensiones:")
    for nombre, dim in ds.dimensions.items():
        print(f"  {nombre}: {len(dim)}")

    print("\nVariables:")
    candidatas_viento = []
    for nombre, var in ds.variables.items():
        print(f"  {nombre} {var.dimensions} {var.shape}  attrs={dict(var.__dict__)}")
        if any(clave in nombre.upper() for clave in ("WIND", "WSPD", "WDIR", "U10", "V10", "UV10")):
            candidatas_viento.append(nombre)

    print(f"\nVariables candidatas a viento: {candidatas_viento}")

    print("\nAtributos globales del archivo:")
    for attr in ds.ncattrs():
        print(f"  {attr} = {getattr(ds, attr)}")

    # Intentar ubicar Buenos Aires en la grilla
    lat_var = None
    lon_var = None
    for candidato in ("XLAT", "lat", "latitude", "XLAT_M"):
        if candidato in ds.variables:
            lat_var = candidato
            break
    for candidato in ("XLONG", "lon", "longitude", "XLONG_M"):
        if candidato in ds.variables:
            lon_var = candidato
            break

    if lat_var and lon_var:
        import numpy as np

        lat = ds.variables[lat_var][:]
        lon = ds.variables[lon_var][:]
        lat = np.squeeze(lat)
        lon = np.squeeze(lon)
        dist = (lat - LAT_OBJETIVO) ** 2 + (lon - LON_OBJETIVO) ** 2
        idx = np.unravel_index(np.argmin(dist), dist.shape)
        print(f"\nPunto de grilla más cercano a Buenos Aires: índice {idx}")
        print(f"  lat={lat[idx]:.4f} lon={lon[idx]:.4f}")

        for nombre in candidatas_viento:
            var = ds.variables[nombre]
            try:
                valor = np.squeeze(var[:])[idx] if var.ndim >= 2 else None
                print(f"  {nombre} en ese punto: {valor}")
            except Exception as e:
                print(f"  {nombre}: no pude leer el valor ({e})")
    else:
        print("\nNo encontré variables de lat/lon con los nombres esperados.")
        print("Nombres de variables disponibles (repetido para revisar):", list(ds.variables.keys()))

    print(f"\nListo. Corrido {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
