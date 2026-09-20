# -*- coding: utf-8 -*-
"""
DiseñoPlantacionDockWidget — Fase 1 del Diseñador de Plantación: orientación
del cultivo (a partir del DEM) + división del predio en lotes/bloques con
corredores de vía y drenaje. Las fases siguientes (ubicación de drenajes reales
sobre el terreno, vías, malla de puntos de siembra) se agregan sobre esta base.
"""
import logging

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDockWidget, QVBoxLayout, QFormLayout, QLabel,
                                 QComboBox, QDoubleSpinBox, QSpinBox, QPushButton,
                                 QGroupBox, QMessageBox, QApplication)
from qgis.core import QgsProject, QgsMapLayer, QgsWkbTypes

from . import lot_division

logger = logging.getLogger(__name__)


class DisenoPlantacionDockWidget(QDockWidget):

    def __init__(self, iface, parent=None):
        super().__init__("Diseño de Plantación", parent)
        self.iface = iface
        self.terms_accepted = True  # herramienta propia, sin aviso legal ni login
        self._build_ui()

    def _build_ui(self):
        content = QtWidgets.QWidget()
        layout = QVBoxLayout(content)

        intro = QLabel(
            "<b>Fase 1 — Orientación y división del predio en lotes</b><br>"
            "Con el polígono del predio completo y un DEM, decide la orientación "
            "de siembra según la pendiente del terreno y divide el predio en "
            "lotes de tamaño uniforme, dejando corredores para vías y drenajes."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()

        self.combo_predio = QComboBox()
        form.addRow("Polígono del predio:", self.combo_predio)

        self.combo_dem = QComboBox()
        form.addRow("DEM (modelo de elevación):", self.combo_dem)

        btn_refresh = QPushButton("🔄 Actualizar lista de capas")
        btn_refresh.clicked.connect(self._refresh_layer_combos)
        form.addRow("", btn_refresh)

        layout.addLayout(form)

        params_group = QGroupBox("Parámetros (ajustables)")
        params_form = QFormLayout(params_group)

        self.spin_lot_width = QDoubleSpinBox()
        self.spin_lot_width.setRange(20.0, 2000.0)
        self.spin_lot_width.setValue(590.0)
        self.spin_lot_width.setSuffix(" m")
        self.spin_lot_width.setToolTip(
            "Ancho del lote, perpendicular a las líneas de siembra. Independiente "
            "del área — este es el valor que controla qué tan alargado sale el lote. "
            "590 m por defecto, medido directamente sobre un lote real de la finca "
            "Tucurinca (13.21 ha, ~593 m de ancho x ~223 m de largo)."
        )
        params_form.addRow("Ancho del lote:", self.spin_lot_width)

        self.spin_max_length = QDoubleSpinBox()
        self.spin_max_length.setRange(20.0, 2000.0)
        self.spin_max_length.setValue(350.0)
        self.spin_max_length.setSuffix(" m")
        self.spin_max_length.setToolTip(
            "Largo del lote en el sentido de las líneas de siembra — la distancia "
            "que camina un cosechero cortando el lote de punta a punta."
        )
        params_form.addRow("Largo (sentido de corte):", self.spin_max_length)

        self.spin_area_max = QDoubleSpinBox()
        self.spin_area_max.setRange(0.5, 200.0)
        self.spin_area_max.setValue(12.0)
        self.spin_area_max.setSuffix(" ha")
        self.spin_area_max.setToolTip(
            "Tope de seguridad: si ancho × largo supera esta área, se recorta el "
            "largo (nunca el ancho) para no pasarse."
        )
        params_form.addRow("Área máxima por lote:", self.spin_area_max)

        self.spin_slope_threshold = QDoubleSpinBox()
        self.spin_slope_threshold.setRange(0.0, 100.0)
        self.spin_slope_threshold.setValue(12.0)
        self.spin_slope_threshold.setSuffix(" %")
        self.spin_slope_threshold.setToolTip(
            "Por debajo de este umbral, las filas se orientan Norte-Sur.\n"
            "Por encima, se orientan siguiendo las curvas de nivel."
        )
        params_form.addRow("Umbral de pendiente:", self.spin_slope_threshold)

        self.spin_main_road = QDoubleSpinBox()
        self.spin_main_road.setRange(1.0, 30.0)
        self.spin_main_road.setValue(8.0)
        self.spin_main_road.setSuffix(" m")
        params_form.addRow("Ancho vía principal:", self.spin_main_road)

        self.spin_drainage = QDoubleSpinBox()
        self.spin_drainage.setRange(0.5, 15.0)
        self.spin_drainage.setValue(3.0)
        self.spin_drainage.setSuffix(" m")
        self.spin_drainage.setToolTip(
            "Franja reservada al costado de cada vía (y entre lotes contiguos) "
            "para el drenaje."
        )
        params_form.addRow("Ancho de drenaje:", self.spin_drainage)

        self.spin_lots_per_block = QSpinBox()
        self.spin_lots_per_block.setRange(1, 20)
        self.spin_lots_per_block.setValue(2)
        self.spin_lots_per_block.setToolTip(
            "Cada cuántos lotes en línea aparece una vía (borde de bloque). "
            "Entre lotes dentro del mismo bloque NUNCA hay vía, solo drenaje."
        )
        params_form.addRow("Lotes por bloque:", self.spin_lots_per_block)

        layout.addWidget(params_group)

        self.btn_run = QPushButton("▶ Generar orientación y división de lotes")
        self.btn_run.setStyleSheet(
            "background-color: #2E7D32; color: white; font-weight: bold; "
            "padding: 8px 12px; border-radius: 4px;"
        )
        self.btn_run.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.btn_run)

        self.label_resultado = QLabel("")
        self.label_resultado.setWordWrap(True)
        self.label_resultado.setStyleSheet("color: #333; font-size: 11px; padding-top: 6px;")
        layout.addWidget(self.label_resultado)

        layout.addStretch(1)
        self.setWidget(content)

        self._refresh_layer_combos()

    def _refresh_layer_combos(self):
        """
        Solo se listan como DEM los rásteres de una sola banda: un DEM real de
        elevación tiene 1 banda, mientras que una ortofoto (RGB) tiene 3+.
        Así se evita seleccionar por error la ortofoto donde debía ir el DEM
        (justo lo que pasó la primera vez: intentó leer una ortofoto de
        ~125k x 129k px como si fuera elevación y pidió 119 GB de RAM).
        """
        self.combo_predio.clear()
        self.combo_dem.clear()
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.type() == QgsMapLayer.VectorLayer and lyr.geometryType() == QgsWkbTypes.PolygonGeometry:
                self.combo_predio.addItem(lyr.name(), lyr)
            elif lyr.type() == QgsMapLayer.RasterLayer and lyr.bandCount() == 1:
                self.combo_dem.addItem(lyr.name(), lyr)

        if self.combo_dem.count() == 0:
            self.combo_dem.addItem("(sin rásteres de 1 banda cargados — carga un DEM)", None)

    def _on_run_clicked(self):
        predio_layer = self.combo_predio.currentData()
        dem_layer = self.combo_dem.currentData()

        if predio_layer is None:
            QMessageBox.warning(self, "Falta el predio", "Selecciona la capa con el polígono del predio.")
            return
        if dem_layer is None:
            QMessageBox.warning(self, "Falta el DEM", "Selecciona la capa ráster del DEM.")
            return

        area_max = self.spin_area_max.value()
        max_length = self.spin_max_length.value()

        features = list(predio_layer.getFeatures())
        if not features:
            QMessageBox.warning(self, "Sin datos", "La capa del predio no tiene ninguna geometría.")
            return

        from qgis.core import QgsGeometry
        geoms = [f.geometry() for f in features if f.geometry() and not f.geometry().isEmpty()]
        polygon_geom = QgsGeometry.unaryUnion(geoms) if len(geoms) > 1 else geoms[0]

        QApplication.setOverrideCursor(Qt.WaitCursor)
        tmp_dir = None
        from qgis.PyQt.QtWidgets import QProgressDialog
        progress = QProgressDialog("Calculando orientación a partir del DEM...", None, 0, 100, self)
        progress.setWindowTitle("Generando división de lotes")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        def _on_progress(fraccion, mensaje):
            progress.setValue(int(fraccion * 100))
            progress.setLabelText(mensaje)
            QApplication.processEvents()

        try:
            # CRS de trabajo: si el predio o el DEM vienen en grados (geográfico),
            # se reproyecta todo internamente a la zona UTM correspondiente — el
            # usuario no tiene que hacerlo a mano, y los cálculos en metros/hectáreas
            # quedan correctos.
            working_crs = lot_division.resolve_working_crs(predio_layer.crs(), dem_layer.crs(), polygon_geom)
            reproyectado = working_crs.authid() != predio_layer.crs().authid()

            polygon_geom = lot_division.reproject_geometry(polygon_geom, predio_layer.crs(), working_crs)

            dem_path = dem_layer.source()
            if working_crs.authid() != dem_layer.crs().authid():
                import tempfile
                tmp_dir = tempfile.mkdtemp(prefix="diseno_plantacion_")
                dem_path = lot_division.reproject_dem_if_needed(dem_path, dem_layer.crs(), working_crs, tmp_dir)

            bearing, criterio, pendiente = lot_division.determine_orientation(
                dem_path, polygon_geom,
                slope_threshold_pct=self.spin_slope_threshold.value()
            )
            _on_progress(0.05, "Dividiendo el predio en lotes...")

            resultado = lot_division.divide_property(
                polygon_geom, bearing,
                lot_width_m=self.spin_lot_width.value(),
                max_length_m=max_length,
                max_area_ha=area_max,
                progress_callback=_on_progress,
                main_road_width_m=self.spin_main_road.value(),
                drainage_width_m=self.spin_drainage.value(),
                lots_per_block=self.spin_lots_per_block.value(),
            )

            lotes_layer, vias_layer, drenajes_layer = lot_division.build_output_layers(
                resultado, working_crs.authid()
            )

            QgsProject.instance().addMapLayer(lotes_layer)
            QgsProject.instance().addMapLayer(vias_layer)
            QgsProject.instance().addMapLayer(drenajes_layer)

            n_lotes = len(resultado["lotes"])
            n_bloques = len({b for _, _, b, _ in resultado["lotes"]}) if resultado["lotes"] else 0
            areas = [a for _, a, _, _ in resultado["lotes"]]
            area_prom = sum(areas) / len(areas) if areas else 0.0

            nota_reproyeccion = (
                f"<br><i>Nota: el predio y/o el DEM estaban en coordenadas geográficas; "
                f"se trabajó internamente en {working_crs.authid()} (proyección métrica) "
                f"y las capas de resultado quedaron en ese mismo sistema.</i>"
                if reproyectado else ""
            )
            self.label_resultado.setText(
                f"✅ {n_lotes} lotes generados en {n_bloques} bloque(s). "
                f"Área promedio: {area_prom:.1f} ha.<br>"
                f"Pendiente media del predio: {pendiente:.1f}%.<br>"
                f"Criterio de orientación aplicado: {criterio} "
                f"(rumbo {bearing:.1f}°).{nota_reproyeccion}"
            )
            self.iface.setActiveLayer(lotes_layer)
            self.iface.zoomToActiveLayer()

        except Exception as e:
            logger.error(f"Error al generar la división de lotes: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"No se pudo generar la división: {e}")
        finally:
            progress.close()
            QApplication.restoreOverrideCursor()
            if tmp_dir:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
