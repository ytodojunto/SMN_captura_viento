"""
Captura de viento pronosticado (magnitud + dirección a 10m) del SMN,
en los 8 puntos del Río de la Plata, desde s3://smn-ar-wrf/DATA/WRF/DET/.

v2 (2026-10-07) — arregla las capturas incompletas / faltantes / repetidas
de la v1:

  - Solo captura ciclos COMPLETOS: los 73 archivos horarios (_01H_, FFF 000..072)
    tienen que estar publicados. Un ciclo a medio publicar se saltea y se
    reintenta en la próxima corrida del Action (que ahora es horaria).
  - No repite: si el ciclo ya está en data/historico_viento.jsonl con 73
    archivos, no lo vuelve a escribir.
  - Mira los últimos LOOKBACK_H horas (no solo la última carpeta), así rellena
    ciclos que se hayan perdido (si el bucket los sigue teniendo).
  - Si un archivo falla al bajarse, NO escribe un snapshot parcial: sale con
    error y la próxima corrida reintenta el ciclo entero.
  - Registra `publicado_en` (LastModified del último archivo en S3): es cuándo
    el pronóstico estuvo realmente disponible. Para validar modelos hay que
    usar ESTE campo y no `capturado_en` (un ciclo rellenado se captura horas
    después de publicado). `retraso_captura_h` = capturado_en - publicado_en.

El formato de cada línea de data/historico_viento.jsonl es el de la v1 más
los campos nuevos `publicado_en` y `retraso_captura_h`. Las líneas viejas
(sin esos campos, algunas incompletas) quedan como están: si hay varias
líneas con el mismo `ciclo_init`, quedarse con la de mayor
`archivos_procesados`.

Fuente (confirmado 2026-09-09):
  - Carpeta de cada corrida: DATA/WRF/DET/<AAAA>/<MM>/<DD>/<HH>/ (HH = 00/06/12/18)
  - Archivos: WRFDETAR_01H_<AAAAMMDD>_<HH>_<FFF>.nc  (FFF = hora de pronóstico)
  - Grilla Lambert de 999x1249 puntos, 4 km; variables 'lat'/'lon' 2D,
    'magViento10' (m/s) y 'dirViento10' (grados, de dónde viene)
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
HISTORICO = DATA_DIR / "historico_viento.jsonl"

CICLOS_HORAS = (0, 6, 12, 18)
ESPERADOS = frozenset(range(0, 73))  # FFF 000..072 -> 73 archivos
LOOKBACK_H = 48        # cuánto para atrás buscar ciclos sin capturar
MAX_CICLOS = 3         # tope de ciclos por corrida (los más nuevos primero)

# Coordenadas de los mareógrafos del Río de la Plata (SHN, Chino). Martín
# García es referencia aproximada. Con 4 km de grilla no hace falta más.
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

_indices_estacion = None  # la grilla es fija: se calcula una sola vez


# ---------- S3 ----------

def listar_archivos(prefix: str) -> list[tuple[str, datetime]]:
    """[(clave, LastModified UTC)] de todo lo que hay bajo `prefix` (con paginado)."""
    salida, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        resp = requests.get(BASE, params=params, timeout=60)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.text)
        for el in root.findall(".//s3:Contents", NS):
            clave = el.findtext("s3:Key", namespaces=NS)
            lm = el.findtext("s3:LastModified", namespaces=NS)
            if clave and lm:
                salida.append((clave, datetime.fromisoformat(lm.replace("Z", "+00:00"))))
        if root.findtext("s3:IsTruncated", namespaces=NS) == "true":
            token = root.findtext("s3:NextContinuationToken", namespaces=NS)
            if not token:
                break
        else:
            break
    return salida


def hora_pronostico(clave: str) -> int:
    m = re.search(r"_(\d{3})\.nc$", clave)
    return int(m.group(1)) if m else 9999


def descargar(clave: str) -> bytes | None:
    resp = requests.get(BASE + clave, timeout=120)
    if resp.status_code != 200:
        print(f"  [{clave}] HTTP {resp.status_code}")
        return None
    c = resp.content
    if not (c.startswith(b"CDF") or c.startswith(b"\x89HDF")):
        print(f"  [{clave}] contenido no parece NetCDF")
        return None
    return c


# ---------- selección de ciclos ----------

def ciclos_completos_ya_capturados() -> set[str]:
    """ciclo_init (ISO) de los snapshots del histórico que ya tienen los 73 archivos."""
    hechos = set()
    if not HISTORICO.exists():
        return hechos
    with HISTORICO.open(encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                s = json.loads(linea)
            except json.JSONDecodeError:
                continue
            if s.get("archivos_procesados", 0) >= len(ESPERADOS) and s.get("ciclo_init"):
                hechos.add(datetime.fromisoformat(s["ciclo_init"]).astimezone(timezone.utc).isoformat())
    return hechos


def ciclos_candidatos(ahora: datetime) -> list[datetime]:
    """Inits 00/06/12/18Z de las últimas LOOKBACK_H horas, del más viejo al más nuevo."""
    t = ahora.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    desde = ahora - timedelta(hours=LOOKBACK_H)
    out = []
    while t >= desde:
        out.append(t)
        t -= timedelta(hours=6)
    return sorted(out)


def prefijo_de(init: datetime) -> str:
    return f"DATA/WRF/DET/{init:%Y/%m/%d/%H}/"


def evaluar_ciclo(init: datetime, archivos: list[tuple[str, datetime]]):
    """(completo, items_a_procesar, faltantes). Solo cuenta _01H_ con FFF 0..72."""
    items = {}
    for clave, lm in archivos:
        if "_01H_" not in clave:
            continue
        fff = hora_pronostico(clave)
        if fff in ESPERADOS:
            items[fff] = (clave, lm)
    faltantes = sorted(ESPERADOS - set(items))
    return (not faltantes), [items[f] for f in sorted(items)], faltantes


def ciclos_pendientes(ahora: datetime) -> list[tuple[datetime, list[tuple[str, datetime]]]]:
    hechos = ciclos_completos_ya_capturados()
    pend = []
    for init in ciclos_candidatos(ahora):
        if init.isoformat() in hechos:
            continue
        try:
            archivos = listar_archivos(prefijo_de(init))
        except requests.RequestException as e:
            print(f"  {init:%Y-%m-%d %HZ}: error listando ({e}), sigo")
            continue
        completo, items, faltantes = evaluar_ciclo(init, archivos)
        if not archivos:
            print(f"  {init:%Y-%m-%d %HZ}: todavía no publicado / ya no está en el bucket")
        elif not completo:
            print(f"  {init:%Y-%m-%d %HZ}: incompleto ({len(items)}/{len(ESPERADOS)}), reintento en la próxima")
        else:
            print(f"  {init:%Y-%m-%d %HZ}: COMPLETO, pendiente de captura")
            pend.append((init, items))
    return pend[-MAX_CICLOS:]


# ---------- procesamiento ----------

def procesar_ciclo(init: datetime, items: list[tuple[str, datetime]]) -> dict | None:
    """Baja y procesa los 73 archivos. Devuelve el snapshot o None si algo falló."""
    global _indices_estacion
    from netCDF4 import Dataset

    resultados = {n: [] for n in ESTACIONES}
    ruta_tmp = Path("/tmp/wrf_tmp.nc")
    for i, (clave, _lm) in enumerate(items):
        fff = hora_pronostico(clave)
        contenido = descargar(clave)
        if contenido is None:
            return None
        ruta_tmp.write_bytes(contenido)
        try:
            with Dataset(ruta_tmp) as ds:
                if _indices_estacion is None:
                    lat = np.squeeze(ds.variables["lat"][:])
                    lon = np.squeeze(ds.variables["lon"][:])
                    _indices_estacion = {}
                    for nombre, (la, lo) in ESTACIONES.items():
                        dist = (lat - la) ** 2 + (lon - lo) ** 2
                        _indices_estacion[nombre] = np.unravel_index(np.argmin(dist), dist.shape)
                mag = np.squeeze(ds.variables["magViento10"][:])
                direc = np.squeeze(ds.variables["dirViento10"][:])
                valido = (init + timedelta(hours=fff)).isoformat()
                for nombre, idx in _indices_estacion.items():
                    resultados[nombre].append({
                        "hora_pronostico": fff,
                        "valido_para": valido,
                        "viento_10m_ms": float(mag[idx]),
                        "direccion_10m_deg": float(direc[idx]),
                    })
        except Exception as e:
            print(f"  [{clave}] error procesando: {e}")
            return None
        finally:
            ruta_tmp.unlink(missing_ok=True)
        if (i + 1) % 20 == 0 or i == len(items) - 1:
            print(f"  ...{i + 1}/{len(items)}")

    publicado = max(lm for _, lm in items)
    ahora = datetime.now(timezone.utc)
    return {
        "capturado_en": ahora.isoformat(),
        "ciclo_init": init.isoformat(),
        "publicado_en": publicado.isoformat(),
        "retraso_captura_h": round((ahora - publicado).total_seconds() / 3600, 2),
        "archivos_procesados": len(items),
        "archivos_fallidos": [],
        "estaciones": resultados,
    }


def guardar(snapshot: dict, init: datetime):
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"viento_{init:%Y%m%dT%H}Z.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with HISTORICO.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    ahora = datetime.now(timezone.utc)
    print(f"=== {ahora.isoformat()} — buscando ciclos completos sin capturar ===")
    pendientes = ciclos_pendientes(ahora)
    if not pendientes:
        print("Nada nuevo para capturar.")
        return 0

    hubo_error = False
    for init, items in pendientes:  # del más viejo al más nuevo
        print(f"\n--- Procesando ciclo {init.isoformat()} ({len(items)} archivos) ---")
        snap = procesar_ciclo(init, items)
        if snap is None:
            print("  FALLÓ: no guardo snapshot parcial; se reintenta en la próxima corrida")
            hubo_error = True
            continue
        guardar(snap, init)
        print(f"  Guardado. Publicado {snap['publicado_en']}, capturado con {snap['retraso_captura_h']} h de retraso")
    return 1 if hubo_error else 0


if __name__ == "__main__":
    sys.exit(main())
