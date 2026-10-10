# -*- coding: utf-8 -*-
"""Simbología compartida de las capas de palmas numeradas."""
from qgis.core import (QgsCategorizedSymbolRenderer, QgsRendererCategory,
                       QgsSingleSymbolRenderer, QgsSymbol)
from qgis.PyQt.QtGui import QColor

ESTADOS = (("viva", QColor(0, 0, 255), "Viva"),
           ("muerta", QColor(220, 0, 0), "Muerta"),
           ("revisar", QColor(255, 140, 0), "Para revisar"))


def simbolos_por_estado(capa, tamano=3):
    """Vivas en azul, muertas en rojo, para revisar en naranja. Si la capa no
    tiene el campo Estado, todas en azul."""
    if capa.fields().indexOf("Estado") < 0:
        simbolo = QgsSymbol.defaultSymbol(capa.geometryType())
        simbolo.setColor(QColor(0, 0, 255))
        simbolo.setSize(tamano)
        capa.setRenderer(QgsSingleSymbolRenderer(simbolo))
        return
    categorias = []
    for valor, color, etiqueta in ESTADOS:
        simbolo = QgsSymbol.defaultSymbol(capa.geometryType())
        simbolo.setColor(color)
        simbolo.setSize(tamano)
        categorias.append(QgsRendererCategory(valor, simbolo, etiqueta))
    capa.setRenderer(QgsCategorizedSymbolRenderer("Estado", categorias))
