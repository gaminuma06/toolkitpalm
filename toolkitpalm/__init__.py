# -*- coding: utf-8 -*-
"""
ToolkitPalm - Plugin QGIS unificado: Detector de Palmas, Segmentador de Palmas
y Optimizador de Acopios, contra un backend propio (ver proyecto_replicacion/backend).
"""


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Carga la clase ToolkitPalm.

    :param iface: instancia de la interfaz de QGIS.
    :type iface: QgsInterface
    """
    from .toolkitpalm import ToolkitPalm
    return ToolkitPalm(iface)
