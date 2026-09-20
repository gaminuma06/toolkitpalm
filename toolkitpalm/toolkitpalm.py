# -*- coding: utf-8 -*-
"""
Clase principal del plugin ToolkitPalm.

Registra una única acción de menú/toolbar que abre un solo panel
(ToolkitPalmDockWidget, ver shell.py) con navegación interna por pestañas
hacia Detector de Palmas, Segmentador de Palmas, Optimizador de Acopios y
el futuro Asistente LLM/MCP.
"""

import logging
import os
from qgis.PyQt.QtCore import Qt, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction


class ToolkitPalm:
    """Plugin QGIS: un solo panel para Detector + Segmentador + Optimizador de Acopios."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.actions = []
        self.menu = "ToolkitPalm"
        self.toolbar = self.iface.addToolBar("ToolkitPalm")
        self.toolbar.setObjectName("ToolkitPalm")

        self.dockwidget = None

        # Limpieza de cache/logs heredada del plugin Detector original.
        try:
            from .detector.worker import cleanup_plugin_cache_and_logs
            cleanup_plugin_cache_and_logs()
        except Exception:
            logging.getLogger(__name__).debug(
                "Fallo no crítico; se continúa.", exc_info=True)

    def tr(self, message):
        return QCoreApplication.translate("ToolkitPalm", message)

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path)
        action = QAction(icon, self.tr("ToolkitPalm"), self.iface.mainWindow())
        action.setStatusTip("Detector, Segmentador y Optimizador de Acopios de palma de aceite")
        action.triggered.connect(self.run)
        self.toolbar.addAction(action)
        self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        self.actions = []

        if self.dockwidget is not None:
            # Antes de soltar el panel hay que desengancharlo de las señales
            # globales de QGIS: si no, al recargar el plugin la instancia vieja
            # sigue recibiendo avisos y falla al tocar widgets ya destruidos.
            if hasattr(self.dockwidget, "disconnect_tool_signals"):
                self.dockwidget.disconnect_tool_signals()
            self.iface.removeDockWidget(self.dockwidget)
            self.dockwidget.close()
            # Cerrar no destruye. Sin esto, cada recarga del plugin deja vivos el
            # panel y las herramientas de la vez anterior: se acumulan en memoria
            # y siguen respondiendo a señales de QGIS.
            if hasattr(self.dockwidget, "dispose_pages"):
                self.dockwidget.dispose_pages()
            self.dockwidget.deleteLater()
        self.dockwidget = None

        del self.toolbar

    def run(self):
        just_created = False
        if self.dockwidget is None:
            from .shell import ToolkitPalmDockWidget
            self.dockwidget = ToolkitPalmDockWidget(self.iface)
            # addDockWidget() deja el panel visible automáticamente -- si no
            # distinguimos este caso, el chequeo de abajo lo detecta como "ya
            # visible" y lo oculta en el mismo clic que lo crea (primer clic
            # parece no hacer nada, hay que volver a hacer clic).
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dockwidget)
            just_created = True

        if not just_created and self.dockwidget.isVisible():
            # El botón de la barra de herramientas funciona como interruptor:
            # si el panel ya está visible, lo oculta (sin cerrar la conexión
            # externa — eso solo pasa al cerrar QGIS, ver unload()).
            if hasattr(self.dockwidget, "ocultar_panel"):
                self.dockwidget.ocultar_panel()
            else:
                self.dockwidget.hide()
            return

        self.dockwidget.show()
        self.dockwidget.raise_()
        self.dockwidget.activateWindow()
