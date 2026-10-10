# -*- coding: utf-8 -*-
"""
Utilidades de las capas de palmas numeradas que corren en el plugin: el trazo
de cada línea sobre sus palmas, y el reordenar de una línea después de editarla
a mano.

El armado de las filas y el cálculo de las palmas muertas no están aquí: corren
en el servidor de ToolkitPalm (ver DetectorPalmasDockWidget._numerar_en_servidor).
"""
import math
from collections import defaultdict

def _ajustar_linea(P, indices, ux, uy):
    """Recta por mínimos cuadrados de unos puntos: (cx, cy, vx, vy), orientada como (ux, uy)."""
    n = len(indices)
    cx = sum(P[i][0] for i in indices) / n
    cy = sum(P[i][1] for i in indices) / n
    if n < 2:
        return cx, cy, ux, uy
    sxx = sum((P[i][0] - cx) ** 2 for i in indices)
    syy = sum((P[i][1] - cy) ** 2 for i in indices)
    sxy = sum((P[i][0] - cx) * (P[i][1] - cy) for i in indices)
    ang = 0.5 * math.atan2(2 * sxy, sxx - syy)
    vx, vy = math.cos(ang), math.sin(ang)
    if vx * ux + vy * uy < 0:
        vx, vy = -vx, -vy
    # Con pocos puntos muy juntos la recta ajustada es poco fiable: se mezcla con
    # la dirección general de las filas.
    if n < 4:
        vx, vy = (vx + ux) / 2.0, (vy + uy) / 2.0
        norma = math.hypot(vx, vy) or 1.0
        vx, vy = vx / norma, vy / norma
    return cx, cy, vx, vy

def _mediana(valores):
    v = sorted(valores)
    return v[len(v) // 2] if v else 0.0

VENTANA_LINEA = 3        # palmas a cada lado para suavizar el trazo de la fila

TOL_SIMPLIFICAR = 0.15   # m: vértices que se pueden quitar sin mover la línea

def _simplificar(puntos, tolerancia):
    """Douglas-Peucker: quita vértices que no cambian la forma de la línea."""
    if len(puntos) < 3:
        return list(puntos)
    (x0, y0), (x1, y1) = puntos[0], puntos[-1]
    vx, vy = x1 - x0, y1 - y0
    largo = math.hypot(vx, vy) or 1.0
    k, dmax = 0, -1.0
    for i in range(1, len(puntos) - 1):
        d = abs((puntos[i][0] - x0) * vy - (puntos[i][1] - y0) * vx) / largo
        if d > dmax:
            k, dmax = i, d
    if dmax <= tolerancia:
        return [puntos[0], puntos[-1]]
    return _simplificar(puntos[:k + 1], tolerancia)[:-1] + _simplificar(puntos[k:], tolerancia)

def linea_de_fila(fila, ux, uy):
    """Trazo de una fila: pasa sobre sus palmas siguiendo su curva real.

    Cada palma se lleva a la recta ajustada con sus vecinas (VENTANA_LINEA a cada
    lado), de modo que la línea acompaña la fila pero una palma suelta corrida
    no la tuerce. `fila` es una lista ordenada de (x, y).
    """
    P = [(float(x), float(y)) for x, y in fila]
    if len(P) < 3:
        return P
    trazo = []
    for k in range(len(P)):
        ventana = list(range(max(0, k - VENTANA_LINEA), min(len(P), k + VENTANA_LINEA + 1)))
        cx, cy, vx, vy = _ajustar_linea(P, ventana, ux, uy)
        t = (P[k][0] - cx) * vx + (P[k][1] - cy) * vy
        trazo.append((cx + t * vx, cy + t * vy))
    return _simplificar(trazo, TOL_SIMPLIFICAR)

VIVA, MUERTA, REVISAR = "viva", "muerta", "revisar"

DETECTADA, CALCULADA, MANUAL = "detectada", "calculada", "manual"

DISTANCIA_MAX_ASIGNAR = 40.0   # m: más lejos que esto de toda fila, la palma abre una fila nueva

def renumerar(puntos):
    """Reasigna línea y número de palma después de agregar, borrar o mover palmas.

    puntos: lista de (fid, x, y, linea, palma); linea vacía o 0 = palma nueva sin
    fila. Las filas existentes se conservan: solo se ordena dentro de cada una,
    en el mismo sentido en que ya estaba numerada. Cada palma nueva se suma a la
    fila cuya recta local le pasa más cerca.

    Devuelve (asignacion, filas): asignacion es {fid: (linea, palma)} y filas es
    {linea: [fid, ...]} en orden de numeración.
    """
    por_fila = defaultdict(list)
    sueltas = []
    for fid, x, y, linea, palma in puntos:
        if linea:
            por_fila[int(linea)].append((fid, float(x), float(y), palma or 0))
        else:
            sueltas.append((fid, float(x), float(y)))

    def direccion(lista):
        numeradas = sorted((q for q in lista if q[3]), key=lambda q: q[3])
        if len(numeradas) >= 2:
            vx, vy = numeradas[-1][1] - numeradas[0][1], numeradas[-1][2] - numeradas[0][2]
            n = math.hypot(vx, vy)
            if n > 1e-6:
                return vx / n, vy / n
        P = [(q[1], q[2]) for q in lista]
        _cx, _cy, vx, vy = _ajustar_linea(P, list(range(len(P))), 1.0, 0.0)
        return vx, vy

    direcciones = {linea: direccion(lista) for linea, lista in por_fila.items()}

    for fid, x, y in sueltas:
        mejor, linea_mejor = None, None
        for linea, lista in por_fila.items():
            cercanas = sorted(lista, key=lambda q: (q[1] - x) ** 2 + (q[2] - y) ** 2)[:4]
            if not cercanas or math.hypot(cercanas[0][1] - x, cercanas[0][2] - y) > DISTANCIA_MAX_ASIGNAR:
                continue
            P = [(q[1], q[2]) for q in cercanas]
            ux, uy = direcciones[linea]
            cx, cy, vx, vy = _ajustar_linea(P, list(range(len(P))), ux, uy)
            lateral = abs(-(x - cx) * vy + (y - cy) * vx)
            if mejor is None or lateral < mejor:
                mejor, linea_mejor = lateral, linea
        if linea_mejor is None:
            linea_mejor = (max(por_fila) if por_fila else 0) + 1
            direcciones.setdefault(linea_mejor, (1.0, 0.0))
        por_fila[linea_mejor].append((fid, x, y, 0))

    asignacion, filas = {}, {}
    for linea in sorted(por_fila):
        ux, uy = direcciones.get(linea, (1.0, 0.0))
        orden = sorted(por_fila[linea], key=lambda q: q[1] * ux + q[2] * uy)
        filas[linea] = [q[0] for q in orden]
        for palma, q in enumerate(orden, start=1):
            asignacion[q[0]] = (linea, palma)
    return asignacion, filas
