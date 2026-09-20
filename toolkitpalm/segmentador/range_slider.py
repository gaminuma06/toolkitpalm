# -*- coding: utf-8 -*-
"""
Control deslizante horizontal de doble punto (rango) para elegir un
sub-rango continuo de valores sobre la rampa de colores de un índice
espectral. Ver docs/superpowers/specs/2026-07-07-range-slider-indices-design.md
"""
from qgis.PyQt.QtCore import Qt, pyqtSignal, QRectF
from qgis.PyQt.QtGui import QPainter, QColor, QLinearGradient, QPen
from qgis.PyQt.QtWidgets import QWidget

from .range_math import clamp_range, value_to_fraction, fraction_to_value

_HANDLE_RADIUS = 7
_TRACK_HEIGHT = 8
_TRACK_MARGIN = 12  # espacio para que las manijas no se corten en los extremos


def _hex_to_qcolor(hex_str):
    hex_str = hex_str.lstrip("#")
    r, g, b = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
    return QColor(r, g, b)


class RangeSliderWidget(QWidget):
    """
    Barra horizontal pintada con la rampa de 5 colores del índice
    (posicionada según los percentiles P5/P25/P50/P75/P95 dentro de
    [minimum, maximum]) con dos manijas circulares arrastrables que
    delimitan un rango [low, high]. Emite rangeChanged(low, high) cada vez
    que una manija se mueve por arrastre o por set_low/set_high.
    """

    rangeChanged = pyqtSignal(float, float)

    def __init__(self, minimum, maximum, percentiles, color_stops, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(40)
        self.setMinimumWidth(150)
        self._active_handle = None
        self.configure(minimum, maximum, percentiles, color_stops)

    def configure(self, minimum, maximum, percentiles, color_stops, low=None, high=None,
                  view_min=None, view_max=None):
        """Reconfigura el widget para un nuevo índice/imagen. No emite rangeChanged.

        minimum/maximum son los límites absolutos contra los que se ajustan
        (clamp) low/high — permiten escribir un valor exacto fuera de la
        ventana visual (ej. desde un spin box). view_min/view_max son la
        ventana que realmente se dibuja y se puede arrastrar con el mouse;
        si no se especifican, se usan minimum/maximum. Se recomienda pasar
        percentiles[0]/percentiles[-1] (P5/P95) como ventana visual: algunos
        índices (ej. VARI, que divide por un denominador que puede acercarse
        a cero) generan outliers extremos que, si se usan como ventana
        visual, comprimen todo el degradado útil en una fracción mínima de
        la barra y hacen que las dos manijas por defecto (P25/P75) queden
        una encima de la otra.
        """
        if maximum <= minimum:
            maximum = minimum + 1e-6
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._percentiles = list(percentiles)
        self._color_stops = list(color_stops)

        self._view_min = float(view_min) if view_min is not None else self._minimum
        self._view_max = float(view_max) if view_max is not None else self._maximum
        if self._view_max <= self._view_min:
            self._view_max = self._view_min + 1e-6

        default_low = low if low is not None else self._percentiles[1]
        default_high = high if high is not None else self._percentiles[3]
        self._low, self._high = clamp_range(
            float(default_low), float(default_high), self._minimum, self._maximum
        )
        self.update()

    def low(self):
        return self._low

    def high(self):
        return self._high

    def set_low(self, value):
        new_low, new_high = clamp_range(float(value), self._high, self._minimum, self._maximum)
        if new_low != self._low:
            self._low = new_low
            self._high = new_high
            self.update()
            self.rangeChanged.emit(self._low, self._high)

    def set_high(self, value):
        new_low, new_high = clamp_range(self._low, float(value), self._minimum, self._maximum)
        if new_high != self._high:
            self._low = new_low
            self._high = new_high
            self.update()
            self.rangeChanged.emit(self._low, self._high)

    def _value_to_x(self, value):
        track_left = _TRACK_MARGIN
        track_right = self.width() - _TRACK_MARGIN
        fraction = value_to_fraction(value, self._view_min, self._view_max)
        return track_left + fraction * (track_right - track_left)

    def _x_to_value(self, x):
        track_left = _TRACK_MARGIN
        track_right = self.width() - _TRACK_MARGIN
        track_width = max(1.0, track_right - track_left)
        fraction = (x - track_left) / track_width
        return fraction_to_value(fraction, self._view_min, self._view_max)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        track_left = _TRACK_MARGIN
        track_right = self.width() - _TRACK_MARGIN
        track_top = (self.height() - _TRACK_HEIGHT) / 2
        track_rect = QRectF(track_left, track_top, track_right - track_left, _TRACK_HEIGHT)

        gradient = QLinearGradient(track_left, 0, track_right, 0)
        for value, hex_color in zip(self._percentiles, self._color_stops):
            fraction = value_to_fraction(value, self._view_min, self._view_max)
            gradient.setColorAt(fraction, _hex_to_qcolor(hex_color))

        painter.setPen(QPen(QColor("#9E9E9E"), 1))
        painter.setBrush(gradient)
        painter.drawRoundedRect(track_rect, 4, 4)

        low_x = self._value_to_x(self._low)
        high_x = self._value_to_x(self._high)
        dim_color = QColor(0, 0, 0, 110)
        if low_x > track_left:
            painter.fillRect(QRectF(track_left, track_top, low_x - track_left, _TRACK_HEIGHT), dim_color)
        if high_x < track_right:
            painter.fillRect(QRectF(high_x, track_top, track_right - high_x, _TRACK_HEIGHT), dim_color)

        for x in (low_x, high_x):
            painter.setPen(QPen(QColor("#002856"), 2))
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(QRectF(
                x - _HANDLE_RADIUS, self.height() / 2 - _HANDLE_RADIUS,
                _HANDLE_RADIUS * 2, _HANDLE_RADIUS * 2,
            ))

        painter.end()

    def mousePressEvent(self, event):
        x = event.pos().x()
        low_x = self._value_to_x(self._low)
        high_x = self._value_to_x(self._high)
        dist_low = abs(x - low_x)
        dist_high = abs(x - high_x)
        tolerance = _HANDLE_RADIUS + 4
        if dist_low <= tolerance or dist_high <= tolerance:
            self._active_handle = "low" if dist_low <= dist_high else "high"
        else:
            self._active_handle = None
            event.ignore()

    def mouseMoveEvent(self, event):
        if self._active_handle is None:
            event.ignore()
            return
        value = self._x_to_value(event.pos().x())
        if self._active_handle == "low":
            self.set_low(value)
        else:
            self.set_high(value)

    def mouseReleaseEvent(self, event):
        self._active_handle = None
