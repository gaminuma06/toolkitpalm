# -*- coding: utf-8 -*-
"""
Clasificación de la variable en rangos y colores de la rampa.

No depende de QGIS: solo listas y números, para poder probarse aparte.
"""
import re

from .lectura import normalizar

# ── Orden de categorías de texto ─────────────────────────────────────────────

# Rango de cada palabra: así 'media baja' queda entre 'baja' y 'media'.
_NIVELES = [
    (("nula", "nulo", "ninguna", "ninguno", "sin", "ausente", "cero"), 0),
    (("muy baja", "muy bajo", "muy deficiente", "muy pobre"), 1),
    (("baja", "bajo", "leve", "deficiente", "pobre", "malo", "mala", "escasa", "escaso"), 2),
    (("media baja", "medio bajo", "medio baja", "moderada baja", "regular baja"), 3),
    (("media", "medio", "moderada", "moderado", "regular", "normal", "intermedia",
      "intermedio", "adecuada", "adecuado", "optimo", "optima"), 4),
    (("media alta", "medio alto", "medio alta", "moderada alta", "regular alta"), 5),
    (("alta", "alto", "severa", "severo", "buena", "bueno", "abundante", "excesivo",
      "excesiva"), 6),
    (("muy alta", "muy alto", "muy severa", "muy severo", "critica", "critico",
      "muy buena", "muy bueno", "excelente"), 7),
]


def _rango(categoria):
    n = normalizar(categoria)
    numero = re.match(r"^(\d+(?:\.\d+)?)\b", n)   # '1. Baja', '2 media'
    if numero:
        return float(numero.group(1))
    mejor = None
    for palabras, nivel in _NIVELES:
        for p in palabras:
            if n == p or n.endswith(" " + p) or n.startswith(p + " "):
                # La frase más larga gana: 'media baja' antes que 'baja'.
                if mejor is None or len(p) > mejor[0]:
                    mejor = (len(p), nivel)
    return None if mejor is None else float(mejor[1])


def ordenar_categorias(valores):
    """(categorias_ordenadas, reconocidas).

    Si todas se reconocen como niveles (baja, media, alta…) se ordenan de menor a
    mayor; si no, alfabéticamente, y `reconocidas` es False para que el usuario
    revise el orden."""
    distintas = []
    vistas = set()
    for v in valores:
        if v in (None, ""):
            continue
        clave = normalizar(v)
        if clave not in vistas:
            vistas.add(clave)
            distintas.append(str(v).strip())
    rangos = {c: _rango(c) for c in distintas}
    if distintas and all(r is not None for r in rangos.values()):
        return sorted(distintas, key=lambda c: (rangos[c], normalizar(c))), True
    return sorted(distintas, key=normalizar), False


# ── Cortes de valores numéricos ──────────────────────────────────────────────

TERCILES, CUANTILES, IGUALES, NATURALES, MANUAL = (
    "terciles", "cuantiles", "iguales", "naturales", "manual")


def _cuantil(ordenados, q):
    if not ordenados:
        return 0.0
    pos = q * (len(ordenados) - 1)
    i = int(pos)
    f = pos - i
    if i + 1 < len(ordenados):
        return ordenados[i] * (1 - f) + ordenados[i + 1] * f
    return ordenados[i]


def _jenks(valores, n):
    """Cortes naturales de Jenks (agrupa valores parecidos)."""
    datos = sorted(valores)
    if len(datos) > 1500:   # una muestra basta para ubicar los cortes
        paso = len(datos) / 1500.0
        datos = [datos[int(i * paso)] for i in range(1500)]
    m = len(datos)
    if n >= m:
        return sorted(set(datos))[1:]
    inf = float("inf")
    lim = [[0] * (n + 1) for _ in range(m + 1)]
    var = [[inf] * (n + 1) for _ in range(m + 1)]
    for j in range(1, n + 1):
        lim[1][j] = 1
        var[1][j] = 0.0
    for l in range(2, m + 1):
        s1 = s2 = w = 0.0
        v = 0.0
        for k in range(1, l + 1):
            i3 = l - k + 1
            val = datos[i3 - 1]
            s2 += val * val
            s1 += val
            w += 1
            v = s2 - (s1 * s1) / w
            i4 = i3 - 1
            if i4 != 0:
                for j in range(2, n + 1):
                    if var[l][j] >= v + var[i4][j - 1]:
                        lim[l][j] = i3
                        var[l][j] = v + var[i4][j - 1]
        lim[l][1] = 1
        var[l][1] = v
    cortes = []
    k = m
    for j in range(n, 1, -1):
        idx = lim[k][j] - 1
        cortes.append(datos[idx])
        k = lim[k][j] - 1
    return sorted(cortes)


def cortes(valores, metodo, n=3, manuales=None):
    """Límites internos entre clases (n clases -> n-1 cortes), de menor a mayor."""
    datos = sorted(v for v in valores if v is not None)
    if not datos:
        return []
    if metodo == MANUAL:
        return sorted(float(c) for c in (manuales or []))
    if metodo == TERCILES:
        n = 3
    n = max(2, int(n))
    if metodo in (TERCILES, CUANTILES):
        resultado = [_cuantil(datos, k / float(n)) for k in range(1, n)]
    elif metodo == IGUALES:
        a, b = datos[0], datos[-1]
        resultado = [a + (b - a) * k / float(n) for k in range(1, n)]
    elif metodo == NATURALES:
        resultado = _jenks(datos, n)
    else:
        raise ValueError(f"Método de clasificación desconocido: {metodo}")
    # Sin cortes repetidos (pasa cuando muchos datos tienen el mismo valor).
    unicos = []
    for c in resultado:
        if not unicos or c > unicos[-1]:
            unicos.append(c)
    return unicos


def nombres_de_rangos(limites, minimo, maximo, decimales=2):
    """Etiquetas legibles de cada clase: '0,50 – 1,20'."""
    bordes = [minimo] + list(limites) + [maximo]
    fmt = "{:,.%df}" % decimales
    texto = lambda v: fmt.format(v).replace(",", "X").replace(".", ",").replace("X", ".")
    return [f"{texto(a)} – {texto(b)}" for a, b in zip(bordes, bordes[1:])]


def etiquetas_bajo_medio_alto(n):
    if n == 3:
        return ["Bajo", "Medio", "Alto"]
    if n == 5:
        return ["Bajo", "Medio bajo", "Medio", "Medio alto", "Alto"]
    return None


# ── Colores ──────────────────────────────────────────────────────────────────

RAMPAS = {
    "Rojo → Naranja → Amarillo → Verde": ["#d73027", "#fc8d59", "#fee08b", "#1a9850"],
    "Verde → Amarillo → Rojo": ["#1a9850", "#fee08b", "#d73027"],
    "Azul → Blanco → Rojo": ["#2c7bb6", "#ffffbf", "#d7191c"],
    "Azules (claro → oscuro)": ["#deebf7", "#9ecae1", "#3182bd", "#08519c"],
    "Verdes (claro → oscuro)": ["#e5f5e0", "#a1d99b", "#31a354", "#006d2c"],
    "Morado → Amarillo (viridis)": ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
}
RAMPA_POR_DEFECTO = "Rojo → Naranja → Amarillo → Verde"


def _hex_a_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def colores(n, rampa=RAMPA_POR_DEFECTO, invertir=False):
    """n colores repartidos a lo largo de la rampa (lista de '#rrggbb')."""
    puntos = [_hex_a_rgb(c) for c in RAMPAS.get(rampa, RAMPAS[RAMPA_POR_DEFECTO])]
    if invertir:
        puntos = puntos[::-1]
    if n <= 1:
        return ["#%02x%02x%02x" % puntos[0]]
    salida = []
    for k in range(n):
        t = k / float(n - 1) * (len(puntos) - 1)
        i = min(int(t), len(puntos) - 2)
        f = t - i
        rgb = [round(a + (b - a) * f) for a, b in zip(puntos[i], puntos[i + 1])]
        salida.append("#%02x%02x%02x" % tuple(rgb))
    return salida
