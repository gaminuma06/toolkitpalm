# -*- coding: utf-8 -*-
"""
Espacio reservado para la futura herramienta de Fotogrametría (Ortofoto + MDE + Curvas
de nivel a partir de fotos de dron). Hoy solo muestra una pantalla "Próximamente";
no hay procesamiento real todavía.

Decisiones ya conversadas con el usuario (resumen para retomar el desarrollo):

1. Qué hace la herramienta:
   El usuario sube el set de fotos de un vuelo de dron (con traslape, como pide
   cualquier fotogrametría) y la herramienta genera:
     - Ortofoto (mosaico georreferenciado).
     - MDE (Modelo Digital de Elevación).
     - Curvas de nivel (se derivan del MDE con GDAL una vez generado, ej.
       `gdal_contour`, ya usado en el patrón de los otros 3 módulos vía QGIS Processing).

2. Por qué NO puede correr dentro del Python de QGIS:
   Esto es fotogrametría real (Structure-from-Motion + nube densa + ajuste de
   haces), computacionalmente pesado — no es algo que una librería liviana
   pueda hacer bien, y menos con las limitaciones ya conocidas del Python
   embebido de QGIS (pip roto, ver `_find_system_python` en `shell.py`).

3. Arquitectura elegida: igual patrón que Detector/Segmentador/Optimizador —
   el plugin solo prepara/sube las fotos; el procesamiento pesado corre en un
   backend aparte. Motor recomendado: **OpenDroneMap (ODM)**, open-source,
   gratuito, ya genera exactamente ortofoto + MDE + insumos para curvas.
   Se distribuye como imagen Docker.

4. Dónde correr ese backend sin pagar:
   - Opción recomendada: **Oracle Cloud Free Tier** — VM ARM Ampere de 4
     vCPU/24GB RAM, gratis para siempre (no es trial). Alcanza para vuelos
     medianos. ODM corre igual en ARM vía Docker.
   - Alternativa: dejar el propio PC/workstation del usuario como "servidor"
     (sin nube), pero solo disponible mientras esa máquina esté encendida.
   - Se descartó que el plugin descargue el motor e instale/corra ODM en el
     PC del usuario final (implica pedirle instalar Docker Desktop + WSL2 +
     activar virtualización en BIOS — demasiada fricción para "un usuario
     normal que no sabe nada", el mismo criterio ya aplicado en el resto del
     plugin).
   - Encaja con el mismo espíritu que ya tiene `proyecto_replicacion/`
     (backend propio autohospedado en vez de depender de un tercero).

5. Pendiente para cuando se retome (no resuelto aún, decidir en su momento):
   - Definir el flujo asíncrono submit -> status -> result (mismo patrón que
     Detector/Segmentador en `config.py`: *_STATUS_ENDPOINT, *_RESULT_ENDPOINT,
     *_QUEUE_STATUS_ENDPOINT, *_PROGRESS_ENDPOINT).
   - Validar tamaño/cantidad de fotos aceptable antes de subir (los vuelos de
     dron pueden pesar varios GB — probablemente necesite compresión o subida
     por partes, igual que ya se cuidó el tamaño de subida en Detector/Segmentador).
   - Decidir si las curvas de nivel se calculan en el backend (junto al MDE) o
     localmente en QGIS con Processing una vez descargado el MDE.
   - Nombre definitivo de la herramienta en el menú (por ahora "Fotogrametría").
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDockWidget, QWidget, QVBoxLayout, QLabel

from ..common.colors import AZUL_OSCURO


class FotogrametriaDockWidget(QDockWidget):
    """Placeholder: "Próximamente". Sin lógica real, ver notas del módulo."""

    def __init__(self, iface, parent=None):
        super().__init__("Fotogrametría", parent)
        self.iface = iface
        # No hay términos que aceptar todavía (no se procesa ni sube nada aquí).
        self.terms_accepted = True

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("🛩️ Fotogrametría (Ortofoto · MDE · Curvas de nivel)")
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {AZUL_OSCURO}; font-size: 14px; font-weight: bold;")

        body = QLabel(
            "Próximamente: sube las fotos de un vuelo de dron y genera una "
            "ortofoto, un Modelo Digital de Elevación (MDE) y curvas de nivel.\n\n"
            "Esta herramienta todavía está en planeación — el espacio en el menú "
            "ya está listo para cuando se desarrolle."
        )
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {AZUL_OSCURO}; font-size: 12px;")

        layout.addWidget(title)
        layout.addSpacing(12)
        layout.addWidget(body)
        layout.addStretch(1)

        self.setWidget(page)
