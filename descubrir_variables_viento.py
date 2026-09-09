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


def _solo_numericas(prefijos: list[str]) -> list[str]:
    """Se queda solo con carpetas cuyo último segmento es puramente
    numérico (año/mes/día/hora reales) — descarta cosas tipo
    'testing/' que hay mezcladas en el bucket y que si no, al
    ordenar alfabéticamente, quedan "después" de los años y se
    toman por error como lo más reciente."""
    salida = [p for p in prefijos if p.strip("/").split("/")[-1].isdigit()]
    return salida or prefijos  # si por algún motivo no queda ninguna, no rompas


def encontrar_corrida_mas_reciente() -> str:
    base = "DATA/WRF/DET/"
    anios = _solo_numericas(listar_subprefijos(base))
    if not anios:
        raise RuntimeError(f"No encontré subcarpetas numéricas en {base}")
    anio = anios[-1]
    meses = _solo_numericas(listar_subprefijos(anio))
    mes = meses[-1]
    dias = _solo_numericas(listar_subprefijos(mes))
    dia = dias[-1]
    horas = _solo_numericas(listar_subprefijos(dia))
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

    # Preferimos el producto horario "01H" (el que vimos en el
    # descubrimiento anterior). Si esta corrida no lo tiene (algunas
    # carpetas mezclan otros productos: 00H, 24M, etc.), avisamos y
    # seguimos con lo que haya para no colgarnos.
    horarios = [a for a in archivos if "_01H_" in a]
    if horarios:
        archivos_a_usar = horarios
    else:
        print("\n(No encontré archivos '_01H_' en esta corrida, uso lo que haya.)")
        archivos_a_usar = archivos

    archivo_clave = sorted(archivos_a_usar)[0]  # el de hora de pronóstico más chica
    print(f"\n=== Bajando {archivo_clave} ===")
    resp = requests.get(BASE + archivo_clave, timeout=120)
    print(f"HTTP {resp.status_code}, content-type={resp.headers.get('content-type')}, "
          f"content-length={resp.headers.get('content-length')}")
    resp.raise_for_status()

    contenido = resp.content
    print(f"Bajado: {len(contenido) / 1024:.0f} KB, primeros bytes: {contenido[:16]!r}")

    # Un NetCDF clásico arranca con b'CDF', uno HDF5/NetCDF4 con la
    # firma HDF5 (\x89HDF). Si no viene ninguna de las dos, algo salió
    # mal en la descarga (bucket, permisos, red) y no tiene sentido
    # que netCDF4 intente abrirlo — mejor un mensaje claro que un
    # traceback confuso.
    if not (contenido.startswith(b"CDF") or contenido.startswith(b"\x89HDF")):
        print("\nEl contenido bajado NO parece un NetCDF válido (revisar HTTP/permisos del bucket).")
        print("Primeros 200 bytes como texto (por si es un mensaje de error en XML/HTML):")
        print(contenido[:200])
        sys.exit(1)

    ruta_local = "/tmp/muestra.nc"
    with open(ruta_local, "wb") as f:
        f.write(contenido)

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
