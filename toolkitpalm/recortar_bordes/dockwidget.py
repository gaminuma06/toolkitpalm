# -*- coding: utf-8 -*-
"""
Recortar bordes de ortofoto: quita el marco de relleno (gris, blanco o negro) que
rodea la zona fotografiada y deja una copia recortada con ese marco transparente.

Corre en este equipo, sin servidor ni créditos.
"""
import logging
import os

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (QDockWidget, QVBoxLayout, QFormLayout, QLabel,
                                 QComboBox, QSpinBox, QPushButton, QCheckBox,
                                 QProgressBar, QMessageBox, QHBoxLayout, QLineEdit,
                                 QFileDialog)
from qgis.core import (QgsProject, QgsMapLayer, QgsRasterLayer, QgsTask,
                       QgsApplication)

from ..common.colors import AZUL_MEDIO, AZUL_CLARO

logger = logging.getLogger(__name__)


class _TareaRecorte(QgsTask):
    avance = pyqtSignal(float, str)

    def __init__(self, entrada, salida, tolerancia):
        super().__init__("Recortar bordes de ortofoto", QgsTask.CanCancel)
        self.entrada, self.salida, self.tolerancia = entrada, salida, tolerancia
        self.resultado = None
        self.error = None

    def run(self):
        from . import procesamiento
        try:
            self.resultado = procesamiento.recortar(
                self.entrada, self.salida, self.tolerancia,
                progreso=lambda f, t: (self.setProgress(100 * f), self.avance.emit(f, t)),
                cancelado=self.isCanceled)
            return True
        except procesamiento.Cancelado:
            self.error = None
            return False
        except Exception as e:
            logger.debug("Fallo el recorte", exc_info=True)
            self.error = e
            return False


class RecortarBordesDockWidget(QDockWidget):

    def __init__(self, iface, parent=None):
        super().__init__("Recortar bordes de ortofoto", parent)
        self.iface = iface
        self.terms_accepted = True  # herramienta local, sin aviso legal ni login
        self._tarea = None
        self._build_ui()
        QgsProject.instance().layersAdded.connect(self._refrescar_capas)
        QgsProject.instance().layersRemoved.connect(self._refrescar_capas)

    def _build_ui(self):
        content = QtWidgets.QWidget()
        layout = QVBoxLayout(content)

        intro = QLabel(
            "<b>Quita el marco de relleno de una ortofoto</b><br>"
            "Algunas ortofotos traen un relleno de color plano —gris, blanco, negro o "
            "cualquier otro— alrededor de la zona fotografiada, o huecos sin foto "
            "dentro del mosaico. Esta herramienta los detecta solos y genera una copia "
            "recortada, con ese relleno transparente. Los caminos, techos o zonas de "
            "arena del mismo color dentro de la foto se conservan.<br>"
            "<i>Se procesa en este equipo: no usa créditos.</i>")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()

        fila = QHBoxLayout()
        self.combo_capa = QComboBox()
        self.combo_capa.currentIndexChanged.connect(self._sugerir_salida)
        fila.addWidget(self.combo_capa, 1)
        btn = QPushButton("🔄")
        btn.setToolTip("Actualizar lista de capas")
        btn.setFixedWidth(32)
        btn.clicked.connect(self._refrescar_capas)
        fila.addWidget(btn)
        form.addRow("Ortofoto:", fila)

        fila = QHBoxLayout()
        self.edit_salida = QLineEdit()
        fila.addWidget(self.edit_salida, 1)
        btn = QPushButton("…")
        btn.setFixedWidth(32)
        btn.clicked.connect(self._elegir_salida)
        fila.addWidget(btn)
        form.addRow("Guardar como:", fila)

        self.spin_tol = QSpinBox()
        self.spin_tol.setRange(1, 60)
        self.spin_tol.setValue(12)
        self.spin_tol.setToolTip(
            "Cuánto se puede alejar un píxel del color del marco y seguir contándose "
            "como marco. Súbela si queda un filo del marco; bájala si se come parte "
            "de la foto.")
        form.addRow("Tolerancia de color:", self.spin_tol)

        self.chk_agregar = QCheckBox("Agregar el resultado al mapa")
        self.chk_agregar.setChecked(True)
        form.addRow("", self.chk_agregar)
        layout.addLayout(form)

        self.btn_recortar = QPushButton("✂️ Recortar bordes")
        self.btn_recortar.setStyleSheet(
            f"QPushButton {{background-color:{AZUL_MEDIO}; color:white; font-weight:bold;"
            f" padding:8px; border-radius:4px;}}"
            f"QPushButton:hover {{background-color:{AZUL_CLARO};}}"
            f"QPushButton:disabled {{background-color:#9aa5b1;}}")
        self.btn_recortar.clicked.connect(self._recortar)
        layout.addWidget(self.btn_recortar)

        self.barra = QProgressBar()
        self.barra.setVisible(False)
        layout.addWidget(self.barra)
        self.estado = QLabel("")
        self.estado.setWordWrap(True)
        layout.addWidget(self.estado)
        layout.addStretch(1)

        self.setWidget(content)
        self._refrescar_capas()

    def _capas_raster(self):
        return [l for l in QgsProject.instance().mapLayers().values()
                if l.type() == QgsMapLayer.RasterLayer and l.providerType() == "gdal"]

    def _refrescar_capas(self, *_):
        actual = self.combo_capa.currentData()
        self.combo_capa.blockSignals(True)
        self.combo_capa.clear()
        for capa in self._capas_raster():
            self.combo_capa.addItem(capa.name(), capa.id())
        i = self.combo_capa.findData(actual)
        if i >= 0:
            self.combo_capa.setCurrentIndex(i)
        self.combo_capa.blockSignals(False)
        self._sugerir_salida()

    def _capa(self):
        return QgsProject.instance().mapLayer(self.combo_capa.currentData() or "")

    def _sugerir_salida(self):
        capa = self._capa()
        if capa:
            base, _ = os.path.splitext(capa.source().split("|")[0])
            self.edit_salida.setText(base + "_recortado.tif")

    def _elegir_salida(self):
        ruta, _ = QFileDialog.getSaveFileName(
            self, "Guardar ortofoto recortada", self.edit_salida.text(), "GeoTIFF (*.tif)")
        if ruta:
            self.edit_salida.setText(ruta if ruta.lower().endswith(".tif") else ruta + ".tif")

    def _recortar(self):
        capa = self._capa()
        salida = self.edit_salida.text().strip()
        if not capa:
            QMessageBox.warning(self, "Recortar bordes", "Elige una ortofoto cargada en el mapa.")
            return
        entrada = capa.source().split("|")[0]
        if not salida:
            QMessageBox.warning(self, "Recortar bordes", "Indica dónde guardar el resultado.")
            return
        if os.path.normcase(os.path.abspath(salida)) == os.path.normcase(os.path.abspath(entrada)):
            QMessageBox.warning(self, "Recortar bordes",
                                "El resultado no puede sobrescribir la ortofoto original.")
            return
        if os.path.exists(salida):
            r = QMessageBox.question(self, "Recortar bordes",
                                     f"Ya existe:\n{salida}\n\n¿Reemplazarlo?")
            if r != QMessageBox.Yes:
                return
            for l in QgsProject.instance().mapLayers().values():
                if os.path.normcase(l.source().split("|")[0]) == os.path.normcase(salida):
                    QgsProject.instance().removeMapLayer(l.id())

        self.btn_recortar.setEnabled(False)
        self.barra.setValue(0)
        self.barra.setVisible(True)
        self.estado.setText("Iniciando…")

        self._tarea = _TareaRecorte(entrada, salida, self.spin_tol.value())
        self._tarea.avance.connect(self._on_avance)
        self._tarea.taskCompleted.connect(lambda: self._on_fin(True))
        self._tarea.taskTerminated.connect(lambda: self._on_fin(False))
        QgsApplication.taskManager().addTask(self._tarea)

    def _on_avance(self, fraccion, texto):
        self.barra.setValue(int(100 * fraccion))
        self.estado.setText(texto)

    def _on_fin(self, ok):
        tarea, self._tarea = self._tarea, None
        self.btn_recortar.setEnabled(True)
        self.barra.setVisible(False)
        if not ok:
            mensaje = str(tarea.error) if tarea and tarea.error else "El recorte se canceló."
            self.estado.setText("❌ " + mensaje)
            QMessageBox.warning(self, "Recortar bordes", mensaje)
            return

        r = tarea.resultado
        colores = ", ".join(f"RGB{c}" for c in r["colores"]) or "zonas ya vacías del archivo"
        try:
            peso = f" ({os.path.getsize(tarea.salida) / 2**20:,.0f} MB)"
        except OSError:
            peso = ""
        self.estado.setText(
            f"✅ Listo. Relleno detectado: {colores}. "
            f"Se quitó el {r['recorte_pct']:.0f}% del área.<br>"
            f"Guardado en: {tarea.salida}{peso}")
        if self.chk_agregar.isChecked():
            nombre = os.path.splitext(os.path.basename(tarea.salida))[0]
            capa = QgsRasterLayer(tarea.salida, nombre)
            if capa.isValid():
                QgsProject.instance().addMapLayer(capa)
