# -*- coding: utf-8 -*-
"""
Arma el .zip de ToolkitPalm listo para publicar en el repositorio de plugins de QGIS.

Se ejecuta con:  python empaquetar_plugin.py

Deja fuera todo lo que no debe salir del computador:
  - config_secrets.py  (claves de API y credenciales)
  - client.log y cualquier registro
  - __pycache__ / .pyc
  - carpetas de resultados o temporales que hayan quedado sueltas
  - los GIFs del instructivo (ver recursos_remotos.py): pesan ~56 MB entre
    todos y el repositorio de QGIS acepta 20 MB por paquete, así que se sirven
    aparte y el plugin los descarga la primera vez que se abre el instructivo.

Al final verifica dos cosas y aborta si alguna falla:
  - que no haya quedado ninguna credencial dentro del paquete;
  - que el peso no supere el límite del repositorio de QGIS.
"""
import os
import re
import sys
import zipfile

RAIZ = os.path.dirname(os.path.abspath(__file__))
ORIGEN = os.path.join(RAIZ, "toolkitpalm")
DESTINO = os.path.join(RAIZ, "toolkitpalm.zip")

EXCLUIR_ARCHIVOS = {"config_secrets.py", "client.log", ".write_test"}
EXCLUIR_EXTENSIONES = {".pyc", ".pyo", ".log"}
EXCLUIR_CARPETAS = {"__pycache__", ".git", ".idea", "temp", "resultados"}

# Los GIFs del instructivo se sirven desde almacenamiento estático, no dentro
# del paquete (ver recursos_remotos.py). El resto de la carpeta sí se incluye.
EXCLUIR_PATRONES = [re.compile(r"instructivo[/\\].*\.gif$", re.I)]

# Límite del repositorio oficial de complementos de QGIS.
LIMITE_MB = 20

# Patrones que delatan una credencial dentro del paquete.
PATRONES_SOSPECHOSOS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}"),          # clave de Anthropic
    re.compile(r"AIza[0-9A-Za-z\-_]{30,}"),              # clave de Google
    re.compile(r"GOCSPX-[0-9A-Za-z\-_]{10,}"),           # client_secret de Google
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),   # llave privada
]


def debe_incluirse(ruta_relativa, nombre):
    if nombre in EXCLUIR_ARCHIVOS:
        return False
    if os.path.splitext(nombre)[1].lower() in EXCLUIR_EXTENSIONES:
        return False
    partes = ruta_relativa.replace("\\", "/").split("/")
    if any(parte in EXCLUIR_CARPETAS for parte in partes):
        return False
    return not any(p.search(ruta_relativa) for p in EXCLUIR_PATRONES)


def revisar_credenciales(ruta_archivo):
    """Devuelve el texto sospechoso encontrado, o None."""
    if os.path.splitext(ruta_archivo)[1].lower() not in {".py", ".txt", ".json", ".cfg", ".md"}:
        return None
    try:
        contenido = open(ruta_archivo, encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    for patron in PATRONES_SOSPECHOSOS:
        encontrado = patron.search(contenido)
        if encontrado:
            return encontrado.group(0)[:20] + "..."
    return None


def main():
    if not os.path.isdir(ORIGEN):
        print(f"No se encontró la carpeta del plugin: {ORIGEN}")
        return 1

    incluidos, hallazgos = [], []
    for carpeta, _subcarpetas, archivos in os.walk(ORIGEN):
        for nombre in archivos:
            completo = os.path.join(carpeta, nombre)
            relativo = os.path.relpath(completo, RAIZ)
            if not debe_incluirse(relativo, nombre):
                continue
            sospecha = revisar_credenciales(completo)
            if sospecha:
                hallazgos.append((relativo, sospecha))
            incluidos.append((completo, relativo))

    if hallazgos:
        print("\nSE ABORTA: hay posibles credenciales dentro de lo que se iba a empaquetar:")
        for relativo, muestra in hallazgos:
            print(f"  - {relativo}: {muestra}")
        print("\nSácalas a config_secrets.py (que no se empaqueta) y vuelve a intentar.")
        return 2

    if os.path.exists(DESTINO):
        os.remove(DESTINO)
    with zipfile.ZipFile(DESTINO, "w", zipfile.ZIP_DEFLATED) as zf:
        for completo, relativo in incluidos:
            zf.write(completo, relativo)

    tamano_mb = os.path.getsize(DESTINO) / (1024 * 1024)
    if tamano_mb > LIMITE_MB:
        print(f"\nSE ABORTA: el paquete pesa {tamano_mb:.1f} MB y el repositorio "
              f"de QGIS acepta hasta {LIMITE_MB} MB.")
        print("Revisa qué archivo grande se coló (los GIFs del instructivo van aparte).")
        os.remove(DESTINO)
        return 3

    print(f"Paquete listo: {DESTINO}")
    print(f"  {len(incluidos)} archivos, {tamano_mb:.1f} MB (límite: {LIMITE_MB} MB)")
    print("  Sin config_secrets.py, sin registros ni cachés, sin los GIFs del instructivo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
