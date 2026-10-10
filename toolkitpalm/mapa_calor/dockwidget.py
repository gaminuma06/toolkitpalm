# -*- coding: utf-8 -*-
"""
Espacio reservado para la herramienta "Mapa de calor". Hoy solo muestra una
pantalla de "en construcción": todavía no hay lógica.

Queda en gris y bloqueada para los usuarios, y abierta solo para el
administrador (ver _TOOLS_SOLO_ADMIN en shell.py), para ir construyéndola
antes de pasarla a producción.
"""

from qgis.PyQt.QtWidgets import QDockWidget, QWidget, QVBoxLayout, QLabel

from ..common.colors import AZUL_OSCURO


class MapaCalorDockWidget(QDockWidget):
    """Placeholder: "En construcción". Sin lógica real todavía."""

    def __init__(self, iface, parent=None):
        super().__init__("Mapa de calor", parent)
        self.iface = iface
        self.terms_accepted = True  # no procesa ni sube nada todavía

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("🔥 Mapa de calor")
        title.setStyleSheet(f"color: {AZUL_OSCURO}; font-size: 14px; font-weight: bold;")
        body = QLabel("Herramienta en construcción.")
        body.setWordWrap(True)
        body.setStyleSheet("color: #555555; font-size: 12px;")

        layout.addWidget(title)
        layout.addWidget(body)
        layout.addStretch(1)
        self.setWidget(page)
