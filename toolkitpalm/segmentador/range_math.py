# -*- coding: utf-8 -*-
"""
Funciones matemáticas puras (sin dependencias de Qt) para el control
deslizante de rango (RangeSliderWidget). Separadas del widget para poder
probarlas fuera de QGIS, donde PyQt5 no está disponible.
"""


def clamp_range(low, high, minimum, maximum):
    """Ajusta low/high para que minimum <= low <= high <= maximum."""
    low = max(minimum, min(low, maximum))
    high = max(minimum, min(high, maximum))
    if low > high:
        low, high = high, low
    return low, high


def value_to_fraction(value, minimum, maximum):
    """Convierte un valor a una fracción 0.0-1.0 dentro de [minimum, maximum].

    Si el rango tiene ancho cero (minimum == maximum), retorna 0.0 en vez de
    dividir por cero.
    """
    span = maximum - minimum
    if span <= 0:
        return 0.0
    fraction = (value - minimum) / span
    return max(0.0, min(1.0, fraction))


def fraction_to_value(fraction, minimum, maximum):
    """Convierte una fracción 0.0-1.0 a un valor dentro de [minimum, maximum]."""
    fraction = max(0.0, min(1.0, fraction))
    return minimum + fraction * (maximum - minimum)
