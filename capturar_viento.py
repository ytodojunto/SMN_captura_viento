"""
Captura de verdad: viento pronosticado (magnitud + dirección a 10m)
del SMN, para la corrida más reciente disponible en
s3://smn-ar-wrf/DATA/WRF/DET/, en los mismos 8 puntos del Río de la
Plata que ya usamos para las alturas horarias del SHN. Con esto
acumulado, más adelante se puede relacionar viento pronosticado
para una fecha con la proyección de marea de esa fecha.

Cómo viene la fuente (confirmado 2026-09-09 con un archivo real):
  - Bucket: s3://smn-ar-wrf (público, sin credenciales)
  - Carpeta de cada corrida: DATA/WRF/DET/<AAAA>/<MM>/<DD>/<HH>/
    (ojo: hay una carpeta "testing/" mezclada ahí adentro que hay
    que ignorar — no es una fecha real)
  - Archivos horarios: WRFDETAR_01H_<AAAAMMDD>_<HH>_<FFF>.nc
    (FFF = hora de pronóstico, 000 a ~072)
  - Grilla Lambert Conformal de 999x1249 puntos, 4km, variables
    'lat'/'lon' de 2D (y, x)
  - Viento: 'magViento10' (m/s) y 'dirViento10' (grados, de dónde
    viene el viento) — nombres en español, ojo si el SMN los cambia

Guarda un snapshot por corrida en data/viento_<fecha>.json y lo
agrega a data/historico_viento.jsonl.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import requests

BASE = "https://smn-ar-wrf.s3.amazonaws.com/"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
DATA_DIR = Path(__file__).parent / "data"

# Coordenadas de los mareógrafos del Río de la Plata. San Fernando,
# Buenos Aires (Palermo), La Plata, Atalaya, Oyarvide y San Clemente
# son las oficiales del SHN (códigos H-155/H-156/H-157/H-117/H-116/
# H-159) que pasó Chino. Martín García es una referencia aproximada
# (no está en la tabla oficial que tengo) y Pilote Norden es la que
# Chino confirmó directamente. Con 4km de grilla no hace falta más
# precisión que esta.
ESTACIONES = {
    "Martín García": (-34.1833, -58.2500),
    "San Fernando": (-34.434167, -58.540000),
    "Buenos Aires (Palermo)": (-34.560833, -58.398889),
    "Pilote Norden": (-34.629167, -57.927417),
    "La Plata": (-34.833889, -57.880278),
    "Atalaya": (-35.015278, -57.536111),
    "Oyarvide (Canal Punta Indio)": (-35.100278, -57.1275),
    "San Clemente del Tuyú": (-36.354722, -56.715),
}


def listar_subprefijos(prefix: str) -> list[str]:
    resp = requests.get(
        BASE, params={"list-type": "2", "prefix": prefix, "delimiter": "/", "max-keys": "1000"}, timeout=60
    )
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.text)
    return sorted(el.text for el in root.findall(".//s3:CommonPrefixes/s3:Prefix", NS) if el.text)


def listar_archivos(prefix: str) -> list[str]:
    resp = requests.get(BASE, params={"list-type": "2", "prefix": prefix, "max-keys": "1000"}, timeout=60)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.text)
    return sorted(el.text for el in root.findall(".//s3:Contents/s3:Key", NS) if el.text)


def _solo_numericas(prefijos: list[str]) -> list[str]:
    salida = [p for p in prefijos if p.strip("/").split("/")[-1].isdigit()]
    return salida or prefijos


def encontrar_corrida_mas_reciente() -> tuple[str, datetime]:
    base = "DATA/WRF/DET/"
    anios = _solo_numericas(listar_subprefijos(base))
    anio = anios[-1]
    meses = _solo_numericas(listar_subprefijos(anio))
    mes = meses[-1]
    dias = _solo_numericas(listar_subprefijos(mes))
    dia = dias[-1]
    horas = _solo_numericas(listar_subprefijos(dia))
    hora = horas[-1]

    aaaa = int(anio.strip("/").split("/")[-1])
    mm = int(mes.strip("/").split("/")[-1])
    dd = int(dia.strip("/").split("/")[-1])
    hh = int(hora.strip("/").split("/")[-1])
    ciclo_init = datetime(aaaa, mm, dd, hh, tzinfo=timezone.utc)
    return hora, ciclo_init


def descargar(clave: str) -> bytes | None:
    resp = requests.get(BASE + clave, timeout=120)
    if resp.status_code != 200:
        print(f"  [{clave}] HTTP {resp.status_code}, salto este archivo")
        return None
    contenido = resp.content
    if not (contenido.startswith(b"CDF") or contenido.startswith(b"\x89HDF")):
        print(f"  [{clave}] contenido no parece NetCDF, salto")
        return None
    return contenido


def main():
    DATA_DIR.mkdir(exist_ok=True)

    print("=== Buscando la corrida más reciente ===")
    prefijo_corrida, ciclo_init = encontrar_corrida_mas_reciente()
    print(f"Corrida: {prefijo_corrida} (inicializada {ciclo_init.isoformat()})")

    archivos = [a for a in listar_archivos(prefijo_corrida) if "_01H_" in a]
    if not archivos:
        print("No encontré archivos horarios '_01H_' en esta corrida.")
        sys.exit(1)

    # Ordenar por la hora de pronóstico (FFF) que está en el nombre
    def hora_pronostico(clave: str) -> int:
        m = re.search(r"_(\d{3})\.nc$", clave)
        return int(m.group(1)) if m else 9999

    archivos.sort(key=hora_pronostico)
    print(f"{len(archivos)} archivos horarios a procesar (de {hora_pronostico(archivos[0])} a {hora_pronostico(archivos[-1])})")

    indices_estacion = None  # se calcula con el primer archivo que abramos
    resultados = {nombre: [] for nombre in ESTACIONES}
    fallidos = []

    for i, clave in enumerate(archivos):
        fff = hora_pronostico(clave)
        contenido = descargar(clave)
        if contenido is None:
            fallidos.append(clave)
            continue

        ruta_tmp = Path("/tmp/wrf_tmp.nc")
        ruta_tmp.write_bytes(contenido)

        try:
            from netCDF4 import Dataset

            with Dataset(ruta_tmp) as ds:
                if indices_estacion is None:
                    lat = np.squeeze(ds.variables["lat"][:])
                    lon = np.squeeze(ds.variables["lon"][:])
                    indices_estacion = {}
                    for nombre, (lat_obj, lon_obj) in ESTACIONES.items():
                        dist = (lat - lat_obj) ** 2 + (lon - lon_obj) ** 2
                        idx = np.unravel_index(np.argmin(dist), dist.shape)
                        indices_estacion[nombre] = idx
                        print(
                            f"  {nombre}: punto de grilla {idx}, "
                            f"lat={lat[idx]:.4f} lon={lon[idx]:.4f} "
                            f"(objetivo {lat_obj:.4f},{lon_obj:.4f})"
                        )

                mag = np.squeeze(ds.variables["magViento10"][:])
                direc = np.squeeze(ds.variables["dirViento10"][:])
                valid_time = (ciclo_init + timedelta(hours=fff)).isoformat()

                for nombre, idx in indices_estacion.items():
                    resultados[nombre].append(
                        {
                            "hora_pronostico": fff,
                            "valido_para": valid_time,
                            "viento_10m_ms": float(mag[idx]),
                            "direccion_10m_deg": float(direc[idx]),
                        }
                    )
        except Exception as e:
            print(f"  [{clave}] error procesando: {e}")
            fallidos.append(clave)
        finally:
            ruta_tmp.unlink(missing_ok=True)

        if (i + 1) % 10 == 0 or i == len(archivos) - 1:
            print(f"  ...procesados {i + 1}/{len(archivos)}")

    snapshot = {
        "capturado_en": datetime.now(timezone.utc).isoformat(),
        "ciclo_init": ciclo_init.isoformat(),
        "archivos_procesados": len(archivos) - len(fallidos),
        "archivos_fallidos": fallidos,
        "estaciones": resultados,
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    salida = DATA_DIR / f"viento_{ts}.json"
    salida.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nGuardado: {salida}")

    historico_path = DATA_DIR / "historico_viento.jsonl"
    with historico_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

    if fallidos:
        print(f"\nOjo: {len(fallidos)} archivos fallaron: {fallidos}")


if __name__ == "__main__":
    main()
