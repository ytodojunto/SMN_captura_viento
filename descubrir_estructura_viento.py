"""
FASE 1 — descubrimiento, no captura definitiva todavía.

El SMN publica pronóstico de viento (entre otras variables) en un
bucket público de AWS: s3://smn-ar-wrf — grilla de 4km sobre
Argentina/Uruguay/Chile/Paraguay, corridas cada 6hs (00/06/12/18 UTC),
72hs de horizonte, generado con el modelo WRF. Incluye magnitud y
dirección de viento a 10m — justo lo que hace falta para relacionar
pronóstico de viento con la sudestada/bajante en el Río de la Plata.

El problema: desde donde armo este script no tengo salida a internet
para bajar el bucket y ver cómo están organizadas las carpetas/archivos
adentro (nombres de fecha, formato GRIB2/NetCDF, etc. — la
documentación pública no lo detalla con el nivel de precisión que
hace falta para escribir el parser final a ciegas).

Este script NO intenta parsear nada todavía. Solo lista lo que hay
en el bucket (sin necesitar credenciales — es de acceso público) y
guarda esa estructura en el repo. Corriendo esto una vez desde
GitHub Actions (que sí tiene internet libre) vamos a poder ver los
nombres reales de carpetas/archivos y con eso termino el script de
captura de verdad.

Cómo correrlo: pestaña Actions → "Descubrir estructura viento SMN" →
Run workflow. Después avisame y sigo con el parser definitivo.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import requests

BUCKET_URL = "https://smn-ar-wrf.s3.amazonaws.com/"
DATA_DIR = Path(__file__).parent / "data" / "discovery"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def listar(prefix: str = "", delimiter: str = "/", max_keys: int = 1000) -> ElementTree.Element:
    params = {"list-type": "2", "max-keys": str(max_keys)}
    if prefix:
        params["prefix"] = prefix
    if delimiter:
        params["delimiter"] = delimiter
    resp = requests.get(BUCKET_URL, params=params, timeout=60)
    resp.raise_for_status()
    return ElementTree.fromstring(resp.text)


def extraer_prefijos(xml_root: ElementTree.Element) -> list[str]:
    return [
        el.text
        for el in xml_root.findall(".//s3:CommonPrefixes/s3:Prefix", NS)
        if el.text
    ]


def extraer_claves(xml_root: ElementTree.Element) -> list[dict]:
    salida = []
    for contenido in xml_root.findall(".//s3:Contents", NS):
        clave = contenido.find("s3:Key", NS)
        tamano = contenido.find("s3:Size", NS)
        modificado = contenido.find("s3:LastModified", NS)
        salida.append(
            {
                "key": clave.text if clave is not None else None,
                "size": tamano.text if tamano is not None else None,
                "last_modified": modificado.text if modificado is not None else None,
            }
        )
    return salida


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    resumen = {"capturado_en": datetime.now(timezone.utc).isoformat(), "niveles": []}

    # Nivel 0: raíz del bucket, agrupado por carpeta (delimiter=/)
    raiz = listar(prefix="", delimiter="/")
    (DATA_DIR / f"raiz_{ts}.xml").write_text(
        ElementTree.tostring(raiz, encoding="unicode"), encoding="utf-8"
    )
    prefijos_raiz = extraer_prefijos(raiz)
    claves_raiz = extraer_claves(raiz)
    resumen["niveles"].append(
        {"prefix": "", "subcarpetas": prefijos_raiz, "archivos_sueltos": claves_raiz}
    )
    print(f"Raíz del bucket: {len(prefijos_raiz)} subcarpetas, {len(claves_raiz)} archivos sueltos")
    print("Subcarpetas:", prefijos_raiz[:20])

    # Nivel 1: bajamos un nivel en las primeras subcarpetas para ver
    # cómo siguen organizándose (fecha/ciclo/variable/archivo, etc.)
    for prefijo in prefijos_raiz[:3]:
        nivel1 = listar(prefix=prefijo, delimiter="/")
        (DATA_DIR / f"nivel1_{prefijo.strip('/').replace('/', '_')}_{ts}.xml").write_text(
            ElementTree.tostring(nivel1, encoding="unicode"), encoding="utf-8"
        )
        subprefijos = extraer_prefijos(nivel1)
        claves = extraer_claves(nivel1)
        resumen["niveles"].append(
            {"prefix": prefijo, "subcarpetas": subprefijos, "archivos_sueltos": claves[:30]}
        )
        print(f"  {prefijo} -> {len(subprefijos)} subcarpetas, {len(claves)} archivos")

        # Nivel 2, solo de la primera subcarpeta de nivel 1, para ver
        # un ejemplo real de nombre de archivo final.
        if subprefijos:
            nivel2 = listar(prefix=subprefijos[0], delimiter="")
            claves2 = extraer_claves(nivel2)[:30]
            resumen["niveles"].append({"prefix": subprefijos[0], "archivos_sueltos": claves2})
            print(f"    ejemplo archivos en {subprefijos[0]}:", [c["key"] for c in claves2[:5]])

    salida = DATA_DIR / f"resumen_{ts}.json"
    salida.write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nGuardado: {salida}")
    print("Mandame este archivo (o avisame que corrió) para terminar el parser real.")


if __name__ == "__main__":
    main()
