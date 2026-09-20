# -*- coding: utf-8 -*-
"""
Dock widget del plugin Optimizador de Acopios.
Autenticación idéntica a Detector de Palmas y Segmentador.
Contiene: autenticación, selección de capas (lotes, carreteras, acopios),
parámetros (p, intervalo, CRS, columnas opcionales) y ejecución vía API.
"""

import os
import re
import logging
import tempfile
import zipfile
from datetime import datetime

import requests
from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import pyqtSignal, QSettings, QTimer
from qgis.PyQt.QtWidgets import (
    QMessageBox,
    QLineEdit,
    QLabel,
    QRadioButton,
    QPushButton,
    QProgressDialog,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QScrollArea,
    QApplication,
    QComboBox,
)
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QImage, QPixmap, QColor, QDesktopServices
from qgis.core import (
    QgsProject,
    QgsMapLayerProxyModel,
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsField,
    QgsLayerTreeLayer,
    QgsSingleSymbolRenderer,
    QgsFillSymbol,
    QgsSimpleFillSymbolLayer,
    QgsMarkerSymbol,
    QgsSimpleMarkerSymbolLayer,
    QgsWkbTypes,
    Qgis,
)

from ..config import (
    AUTH_ENDPOINT,
    AUTH_API_KEY,
    AUTH_TIMEOUT,
    AUTH_ENABLED,
    DEFAULT_P_ACOPIOS,
    DEFAULT_ROAD_INTERVAL_M,
    API_KEY,
    OPTIMIZE_ENDPOINT,
    REQUESTS_TIMEOUT,
    SKIP_AUTH_FOR_NOW,
)
from ..common.colors import AZUL_MEDIO, AZUL_CLARO

# Cargar UI
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), "optimizador_dockwidget_base.ui"))

# Logger (mismo patrón que Detector)
logger = logging.getLogger(__name__)

# Clave QSettings para este plugin (cada plugin usa la suya)
AUTH_SETTINGS_PREFIX = "optimizador_acopios/auth"

# Si True, se carga y añade al proyecto la capa "Sub-lotes asignados" (desactivada). Si False, no se muestra en QGIS.
INCLUDE_SUBLOTS_LAYER = False

# Enlace al documento científico del método (optimización de puntos de recolección en palma de aceite, OCL).
DOCUMENTO_METODO_URL = "https://www.ocl-journal.org/articles/ocl/pdf/2025/01/ocl250010.pdf"


class LegalNoticeDialog(QDialog):
    """Aviso Legal y Disclaimer (misma estructura y estilos que Detector de Palmas)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Aviso Legal")
        self.setModal(True)

        WINDOW_WIDTH = 580
        WINDOW_HEIGHT = 520
        self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self.setStyleSheet("""
            QDialog {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
            }
        """)

        self.layout = QVBoxLayout()
        self.layout.setSpacing(12)
        self.layout.setContentsMargins(25, 25, 25, 25)
        self.show_first_screen()
        self.setLayout(self.layout)

    def clear_layout(self):
        while self.layout.count():
            child = self.layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def show_first_screen(self):
        logo_label = self.create_logo()
        self.layout.addWidget(logo_label, alignment=Qt.AlignCenter)

        title_label = QLabel("Optimizador de Acopios")
        title_label.setStyleSheet("""
            QLabel {
                font-size: 22px;
                font-weight: bold;
                color: #2c3e50;
                margin: 8px 0px;
                background-color: transparent;
            }
        """)
        title_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(title_label)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("""
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical {
                background-color: #e9ecef;
                width: 12px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background-color: #6c757d;
                border-radius: 6px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover { background-color: #495057; }
        """)

        scroll_widget = QLabel()
        scroll_widget.setWordWrap(True)
        scroll_widget.setTextFormat(Qt.RichText)
        scroll_widget.setAlignment(Qt.AlignTop)

        content = """
        <div style='font-size: 11px; line-height: 1.4; color: #333;'>

        <p style='margin-bottom: 6px;'><strong>Herramienta para optimizar la ubicación de puntos de acopio en plantaciones de palma de aceite, reduciendo las distancias que recorre la fruta desde los lotes hasta el acopio.</strong></p>

        <p style='margin: 0 0 1px 0;'>Versión 0.1.0 – 2026</p>
        <p style='margin: 0 0 1px 0;'>Desarrollado por el Ingeniero Adán Arias.</p>
        <p style='margin: 0 0 6px 0;'>Para ampliar el conocimiento sobre el método aplicado, consulte el <a href="{doc_url}">artículo</a>.</p>

        <h3 style='color: #2c3e50; font-size: 13px; margin-bottom: 6px; margin-top: 12px;'>Instrucciones de uso:</h3>
        <ol style='margin-left: 15px; margin-bottom: 8px; padding-left: 5px;'>
            <li style='margin-bottom: 3px;'>Cargue en QGIS las capas de lotes (polígonos) y carreteras (líneas). Opcional: acopios actuales (puntos) para comparar con el escenario óptimo.</li>
            <li style='margin-bottom: 3px;'>En el panel del plugin "Optimizador de Acopios": autentíquese con su Documento y Contraseña.</li>
            <li style='margin-bottom: 3px;'>Seleccione la capa de Lotes y la de Carreteras. Si tiene capa de Acopios actuales, puede seleccionarla (opcional).</li>
            <li style='margin-bottom: 3px;'>Indique el número de acopios a ubicar (p) y la distancia entre candidatos (m).</li>
            <li style='margin-bottom: 3px;'>Si la tabla de lotes incluye columnas de <strong>productividad</strong> (rendimiento, t/ha) y/o <strong>precios</strong>, estos valores se tendrán en cuenta y en los resultados aparecerá más información relacionada (subdivisión por productividad, distancias ponderadas por precio).</li>
            <li style='margin-bottom: 3px;'>Haga clic en "Ejecutar optimización". Los resultados se guardan en una carpeta con fecha y hora en el mismo directorio que la capa de lotes y se añaden al mapa.</li>
        </ol>

        <h3 style='color: #2c3e50; font-size: 13px; margin-bottom: 6px; margin-top: 12px;'>Notas importantes:</h3>
        <ul style='margin-left: 15px; margin-bottom: 8px; padding-left: 5px;'>
            <li style='margin-bottom: 3px;'><strong>Formatos:</strong> Se admiten capas en GeoPackage (.gpkg), Shapefile (.shp) o GeoJSON. El procesamiento se realiza en el servidor.</li>
            <li style='margin-bottom: 3px;'><strong>Columnas opcionales:</strong> Los nombres "productividad" y "Precios" se reconocen sin importar mayúsculas o minúsculas.</li>
            <li style='margin-bottom: 3px;'><strong>Tiempo de procesamiento:</strong> La optimización puede tardar varios minutos según el tamaño de los datos y la conexión a internet.</li>
            <li style='margin-bottom: 3px;'><strong>Conexión a Internet:</strong> Se requiere conexión estable para enviar las capas y recibir los resultados.</li>
            <li style='margin-bottom: 3px;'><strong>Resultados:</strong> Se generan una capa de puntos óptimos, una de sub-lotes con asignación y un reporte en texto.</li>
        </ul>

        <h3 style='color: #2c3e50; font-size: 13px; margin-bottom: 6px; margin-top: 12px;'>Limitaciones técnicas:</h3>
        <ul style='margin-left: 15px; margin-bottom: 12px; padding-left: 5px;'>
            <li style='margin-bottom: 3px;'>El modelo de optimización minimiza la distancia total; los resultados dependen de la calidad y cobertura de las capas de entrada.</li>
            <li style='margin-bottom: 3px;'>El rendimiento depende de la carga del servidor y de la conexión a internet del usuario.</li>
        </ul>

        </div>
        """
        scroll_widget.setText(content.format(doc_url=DOCUMENTO_METODO_URL))
        scroll_widget.setStyleSheet("QLabel { margin: 10px; padding: 10px; background-color: transparent; }")
        scroll_widget.setOpenExternalLinks(True)
        scroll_area.setWidget(scroll_widget)
        self.layout.addWidget(scroll_area)

        button_layout = QHBoxLayout()
        self.continue_button = QPushButton("Continuar")
        self.continue_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {AZUL_MEDIO};
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                font-size: 13px;
                min-width: 80px;
                margin: 8px;
            }}
            QPushButton:hover {{ background-color: {AZUL_CLARO}; }}
        """)
        self.continue_button.clicked.connect(self.show_second_screen)

        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                font-size: 13px;
                min-width: 80px;
                margin: 8px;
            }
            QPushButton:hover { background-color: #d32f2f; }
        """)
        self.cancel_button.clicked.connect(self.reject)

        button_layout.addWidget(self.continue_button)
        button_layout.addWidget(self.cancel_button)
        button_layout.setAlignment(Qt.AlignCenter)
        self.layout.addLayout(button_layout)

    def show_second_screen(self):
        if hasattr(self, "continue_button"):
            self.continue_button.deleteLater()
        if hasattr(self, "cancel_button"):
            self.cancel_button.deleteLater()
        self.clear_layout()

        logo_label = self.create_logo()
        self.layout.addWidget(logo_label, alignment=Qt.AlignCenter)

        title_label = QLabel("Optimizador de Acopios")
        title_label.setStyleSheet("""
            QLabel {
                font-size: 22px;
                font-weight: bold;
                color: #2c3e50;
                margin: 8px 0px;
                background-color: transparent;
            }
        """)
        title_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(title_label)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("""
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical {
                background-color: #e9ecef;
                width: 12px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background-color: #6c757d;
                border-radius: 6px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover { background-color: #495057; }
        """)

        scroll_widget = QLabel()
        scroll_widget.setWordWrap(True)
        scroll_widget.setTextFormat(Qt.RichText)
        scroll_widget.setAlignment(Qt.AlignTop)

        content = """
        <div style='font-size: 11px; line-height: 1.4; color: #333;'>

        <p style='margin-bottom: 6px;'><strong>Disclaimer / Aviso Legal:</strong></p>

        <p style='margin-bottom: 8px;'>Este plugin ha sido desarrollado con fines informativos y técnicos como apoyo complementario en la optimización de ubicación de puntos de acopio en plantaciones de palma de aceite, para técnicos, profesionales y entidades del sector. No pretende reemplazar el criterio técnico humano, la experiencia profesional ni las decisiones de planeación en campo.</p>

        <p style='margin-bottom: 6px;'><strong>El desarrollador no se hace responsable por:</strong></p>
        <ul style='margin-left: 15px; margin-bottom: 8px; padding-left: 5px;'>
            <li style='margin-bottom: 3px;'>Decisiones basadas exclusivamente en los resultados obtenidos con este plugin. Se recomienda validar las ubicaciones sugeridas con criterio técnico y verificación en campo.</li>
            <li style='margin-bottom: 3px;'>Inexactitudes en la ubicación óptima debido a limitaciones de los datos de entrada o las condiciones específicas del área.</li>
            <li style='margin-bottom: 3px;'>Interrupciones del servicio, errores de conexión o fallos en el servidor que afecten la disponibilidad o el rendimiento del plugin.</li>
        </ul>

        <p style='margin-bottom: 6px;'><strong>Responsabilidades del usuario:</strong></p>
        <ul style='margin-left: 15px; margin-bottom: 8px; padding-left: 5px;'>
            <li style='margin-bottom: 3px;'>Validar los resultados mediante análisis técnico y, cuando corresponda, verificación en campo antes de tomar decisiones operativas.</li>
            <li style='margin-bottom: 3px;'>Asegurar la calidad y adecuación de las capas de entrada (lotes, carreteras y opcionalmente acopios actuales) conforme a las recomendaciones del plugin.</li>
            <li style='margin-bottom: 3px;'>Comprender las limitaciones del modelo de optimización y considerar los resultados como una propuesta de apoyo, no como una solución definitiva sin revisión.</li>
            <li style='margin-bottom: 3px;'>Mantener copia de seguridad de sus datos originales antes de cualquier procesamiento.</li>
        </ul>

        <p style='margin-bottom: 6px;'><strong>Soporte y actualizaciones:</strong></p>
        <p style='margin-bottom: 8px;'>El desarrollador proporcionará soporte técnico y posibles actualizaciones al plugin según su disponibilidad.</p>

        <p style='margin-bottom: 6px;'><strong>Derechos de autor y licencia:</strong></p>
        <p style='margin-bottom: 12px;'>© 2026 Adán Arias. Todos los derechos reservados. Este software se distribuye bajo los términos de la Licencia Pública General de GNU v2.0 (GPL v2.0), cuyos detalles puede consultar en el archivo de licencia que acompaña al plugin o en el sitio web de la Free Software Foundation. Al utilizar este plugin, usted declara haber leído, comprendido y aceptado en su totalidad los términos, condiciones y limitaciones expuestas en este Aviso Legal.</p>

        </div>
        """
        scroll_widget.setText(content)
        scroll_widget.setStyleSheet("QLabel { margin: 10px; padding: 10px; background-color: transparent; }")
        scroll_area.setWidget(scroll_widget)
        self.layout.addWidget(scroll_area)

        self.accept_button = QPushButton("Acepto los términos y condiciones")
        self.accept_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {AZUL_MEDIO};
                color: white;
                border: none;
                padding: 10px;
                border-radius: 5px;
                font-size: 13px;
                min-width: 220px;
                margin: 8px;
            }}
            QPushButton:hover {{ background-color: {AZUL_CLARO}; }}
        """)
        self.accept_button.clicked.connect(self.accept)
        self.layout.addWidget(self.accept_button, alignment=Qt.AlignCenter)

    def create_logo(self):
        """Título del plugin para el Aviso Legal y el Disclaimer."""
        logo_label = QLabel("ToolkitPalm")
        logo_label.setAlignment(Qt.AlignCenter)
        logo_label.setStyleSheet(
            "QLabel { color: #002856; font-size: 22px; font-weight: bold; "
            "background-color: transparent; border: none; margin: 8px; padding: 0px; }"
        )
        return logo_label


class OptimizadorAcopiosDockWidget(QtWidgets.QDockWidget, FORM_CLASS):

    closingPlugin = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.iface = iface

        # Barra de título personalizada: logo sobre fondo azul
        self._apply_custom_title_bar()
        # Invitación a consultar el documento del método (entre descripción y Autenticación)
        self._add_document_link_label()

        # Estado de autenticación (mismo modelo que Detector de Palmas)
        self.auth_credentials = {
            "document": "",
            "credential": "",
        }
        self.is_authenticated = False
        self.stored_credentials = {"document": "", "credential": ""}

        self._setup_layer_combo_boxes()
        self._connect_layer_list_updates()
        self._set_defaults()
        self.setup_auth_fields()
        self._connect_optimize()

        if SKIP_AUTH_FOR_NOW:
            self.auth_credentials["document"] = "dev-sin-auth"
            self.auth_credentials["credential"] = "dev-sin-auth"
            self.is_authenticated = True
            for widget_name in ("authTitleLabel", "documentLabel", "documentLineEdit",
                                "credentialLabel", "credentialLineEdit", "authenticateButton"):
                widget = self.findChild(QtWidgets.QWidget, widget_name)
                if widget:
                    widget.setVisible(False)
            self.set_post_auth_fields_enabled(True)
        else:
            # Habilitar campos de capas/parámetros solo tras autenticación
            self.set_post_auth_fields_enabled(False)
        # Cargar credenciales guardadas (opcional, como en Detector)
        # self.load_saved_credentials()
        self.validate_auth_fields()

        # Mostrar Aviso Legal una sola vez por instalación, no en cada apertura de
        # QGIS: se recuerda la aceptación en la configuración del usuario.
        from qgis.PyQt.QtCore import QSettings
        settings = QSettings()
        already_accepted = settings.value("ToolkitPalm/Optimizador/terms_accepted", False, type=bool)

        if already_accepted:
            self.terms_accepted = True
            result = QDialog.Accepted
        else:
            legal_dialog = LegalNoticeDialog(self)
            result = legal_dialog.exec_()

        if result == QDialog.Accepted:
            self.terms_accepted = True
            settings.setValue("ToolkitPalm/Optimizador/terms_accepted", True)
            logger.info("Usuario aceptó los términos y condiciones")
        else:
            self.terms_accepted = False
            logger.warning("Usuario rechazó los términos y condiciones")
            QMessageBox.warning(
                self,
                "Aviso",
                "Debe aceptar los términos y condiciones para usar el plugin.",
            )
            self.closingPlugin.emit()
            self.close()
            return

    def _apply_custom_title_bar(self):
        """Barra del dock con el título del plugin sobre fondo azul (#002856)."""
        title_widget = QtWidgets.QWidget(self)
        title_widget.setStyleSheet(
            "background-color: #002856; min-height: 28px;"
        )
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 4, 4, 4)
        layout.setSpacing(0)

        label = QLabel("ToolkitPalm")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #ffffff; font-size: 15px; font-weight: bold; background-color: transparent;")

        layout.addWidget(label, 1)

        close_btn = QtWidgets.QToolButton()
        close_btn.setText("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setStyleSheet("""
            QToolButton {
                color: #ffffff;
                background-color: transparent;
                border: none;
                font-size: 14px;
                font-weight: bold;
            }
            QToolButton:hover {
                background-color: #c42b1c;
                border-radius: 3px;
            }
        """)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        title_widget.setLayout(layout)
        self.setTitleBarWidget(title_widget)

    def _add_document_link_label(self):
        """Añade entre la descripción y Autenticación una invitación con enlace al documento del método."""
        contents = self.findChild(QtWidgets.QWidget, "scrollAreaWidgetContents")
        if contents is None:
            return
        layout = contents.layout()
        if layout is None:
            return
        doc_label = QLabel()
        info_label = self.findChild(QtWidgets.QLabel, "infoLabel")
        if info_label is not None:
            doc_label.setFont(info_label.font())
        doc_label.setWordWrap(True)
        doc_label.setTextFormat(Qt.RichText)
        doc_label.setOpenExternalLinks(True)
        doc_label.setStyleSheet("color: #2c3e50; background-color: transparent; margin-top: 6px; margin-bottom: 8px;")
        doc_label.setText(
            'Para ampliar el conocimiento sobre el método aplicado, consulte el '
            '<a href="{}">artículo</a>.'.format(DOCUMENTO_METODO_URL)
        )
        # Insertar antes del bloque Autenticación (índice 2: tras titleLabel e infoLabel)
        layout.insertWidget(2, doc_label)

    def _setup_layer_combo_boxes(self):
        """Configura los combos para seleccionar capas del proyecto."""
        if hasattr(self, "lotsCombo"):
            self.lotsCombo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
            self.lotsCombo.layerChanged.connect(self._populate_column_combos)
        if hasattr(self, "roadsCombo"):
            self.roadsCombo.setFilters(QgsMapLayerProxyModel.LineLayer)
        if hasattr(self, "acopiosCombo") and isinstance(self.acopiosCombo, QComboBox):
            self._populate_acopios_combo()
        self._populate_column_combos()

    def _connect_layer_list_updates(self):
        """Conecta a las señales del proyecto para actualizar los listados de capas al cargar/quitar capas."""
        project = QgsProject.instance()
        try:
            project.layersAdded.connect(self._on_project_layers_changed)
        except AttributeError:
            pass
        try:
            project.layersRemoved.connect(self._on_project_layers_changed)
        except AttributeError:
            pass

    def _on_project_layers_changed(self, *args):
        """Refresca los combos de capas cuando se añaden o quitan capas en el proyecto (con delay para que el árbol se actualice)."""
        QTimer.singleShot(100, self._refresh_layer_combos)

    def _refresh_layer_combos(self):
        """Actualiza los listados de lotes, carreteras y acopios preservando la selección actual si sigue válida."""
        # Guardar selección actual
        lots_layer_id = self.lotsCombo.currentLayer().id() if hasattr(self, "lotsCombo") and self.lotsCombo.currentLayer() else None
        roads_layer_id = self.roadsCombo.currentLayer().id() if hasattr(self, "roadsCombo") and self.roadsCombo.currentLayer() else None
        acopios_layer_id = self.acopiosCombo.currentData() if hasattr(self, "acopiosCombo") and isinstance(self.acopiosCombo, QComboBox) else None

        # Refrescar lotes y carreteras (QgsMapLayerComboBox)
        if hasattr(self, "lotsCombo"):
            self.lotsCombo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
            if lots_layer_id and QgsProject.instance().mapLayer(lots_layer_id):
                self.lotsCombo.setLayer(QgsProject.instance().mapLayer(lots_layer_id))
        if hasattr(self, "roadsCombo"):
            self.roadsCombo.setFilters(QgsMapLayerProxyModel.LineLayer)
            if roads_layer_id and QgsProject.instance().mapLayer(roads_layer_id):
                self.roadsCombo.setLayer(QgsProject.instance().mapLayer(roads_layer_id))

        # Refrescar acopios (QComboBox manual)
        if hasattr(self, "acopiosCombo") and isinstance(self.acopiosCombo, QComboBox):
            self._populate_acopios_combo()
            if acopios_layer_id and QgsProject.instance().mapLayer(acopios_layer_id):
                for i in range(self.acopiosCombo.count()):
                    if self.acopiosCombo.itemData(i) == acopios_layer_id:
                        self.acopiosCombo.setCurrentIndex(i)
                        break

    def _populate_acopios_combo(self):
        """Rellena el combo de acopios con 'Sin acopios' y las capas de puntos del proyecto."""
        combo = self.acopiosCombo
        combo.clear()
        combo.addItem("Sin acopios", None)
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.geometryType() == QgsWkbTypes.PointGeometry:
                combo.addItem(layer.name(), layer.id())
        combo.setCurrentIndex(0)

    def _populate_column_combos(self, layer=None):
        """Rellena productividadCombo y preciosCombo con los campos de la capa de lotes seleccionada.

        Se llama al cambiar lotsCombo. Si el campo coincide (sin distinción de mayúsculas) con
        nombres conocidos de productividad o precios, lo preselecciona automáticamente.
        """
        prod_combo = getattr(self, "productividadCombo", None)
        precio_combo = getattr(self, "preciosCombo", None)
        if prod_combo is None and precio_combo is None:
            return

        if layer is None and hasattr(self, "lotsCombo"):
            layer = self.lotsCombo.currentLayer()

        if prod_combo is not None:
            prod_combo.clear()
            prod_combo.addItem("Sin producción", None)
        if precio_combo is not None:
            precio_combo.clear()
            precio_combo.addItem("Sin precios", None)

        if layer is None or not layer.isValid():
            return

        YIELD_NAMES = {"productividad", "produccion", "producción", "rendimiento", "rff",
                       "yield", "produccion_rff", "producción_rff", "t_rff", "ton_rff"}
        PRICE_NAMES = {"precios", "precio", "price", "prices", "valor", "cop_t", "cop"}

        best_prod = -1
        best_precio = -1

        for idx, field in enumerate(layer.fields()):
            fname = field.name()
            fname_lower = fname.lower()
            if prod_combo is not None:
                prod_combo.addItem(fname, fname)
                if best_prod == -1 and fname_lower in YIELD_NAMES:
                    best_prod = idx + 1  # +1 por el item "Sin producción"
            if precio_combo is not None:
                precio_combo.addItem(fname, fname)
                if best_precio == -1 and fname_lower in PRICE_NAMES:
                    best_precio = idx + 1  # +1 por el item "Sin precios"

        if prod_combo is not None and best_prod > 0:
            prod_combo.setCurrentIndex(best_prod)
        if precio_combo is not None and best_precio > 0:
            precio_combo.setCurrentIndex(best_precio)

    def _get_acopios_layer(self):
        """Devuelve la capa de acopios actuales seleccionada o None si eligió 'Sin acopios'."""
        if not hasattr(self, "acopiosCombo"):
            return None
        if isinstance(self.acopiosCombo, QComboBox):
            layer_id = self.acopiosCombo.currentData()
            if layer_id is None:
                return None
            return QgsProject.instance().mapLayer(layer_id)
        return self.acopiosCombo.currentLayer()

    def _set_defaults(self):
        """Valores por defecto de parámetros."""
        if hasattr(self, "pSpinBox"):
            self.pSpinBox.setValue(DEFAULT_P_ACOPIOS)
        if hasattr(self, "intervalDoubleSpinBox"):
            self.intervalDoubleSpinBox.setValue(DEFAULT_ROAD_INTERVAL_M)

    def _connect_optimize(self):
        """Conecta el botón de optimizar y el de reiniciar."""
        if hasattr(self, "optimizeButton"):
            self.optimizeButton.clicked.connect(self._on_optimize)
        if hasattr(self, "resetButton"):
            self.resetButton.clicked.connect(self._on_reset)

    # -------------------------------------------------------------------------
    # Autenticación (misma lógica que Detector de Palmas)
    # -------------------------------------------------------------------------

    def setup_auth_fields(self):
        """Obtiene referencias a los controles de autenticación y conecta señales."""
        self.documentLineEdit = self.findChild(QLineEdit, "documentLineEdit")
        self.credentialLineEdit = self.findChild(QLineEdit, "credentialLineEdit")
        self.credentialLabel = self.findChild(QLabel, "credentialLabel")
        self.authenticateButton = self.findChild(QPushButton, "authenticateButton")

        if not all([
            self.documentLineEdit,
            self.credentialLineEdit,
            self.credentialLabel,
            self.authenticateButton,
        ]):
            logger.warning("No se encontraron todos los controles de autenticación en la UI")
            return

        self.documentLineEdit.textChanged.connect(self.on_document_changed)
        self.credentialLineEdit.textChanged.connect(self.on_credential_changed)
        self.authenticateButton.clicked.connect(self.on_authenticate_clicked)

        self.authenticateButton.setEnabled(False)
        self.set_post_auth_fields_enabled(False)

    def on_document_changed(self, text):
        """Actualiza stored_credentials y valida si se puede habilitar el botón Autenticar."""
        self.stored_credentials["document"] = text
        self.validate_auth_fields()

    def on_credential_changed(self, text):
        """Actualiza stored_credentials y valida si se puede habilitar el botón Autenticar."""
        self.stored_credentials["credential"] = text
        self.validate_auth_fields()

    def validate_auth_fields(self):
        """Habilita el botón Autenticar solo si documento y credencial tienen contenido."""
        document = self.stored_credentials.get("document", "").strip()
        credential = self.stored_credentials.get("credential", "").strip()
        if hasattr(self, "authenticateButton"):
            self.authenticateButton.setEnabled(bool(document and credential))

    def on_authenticate_clicked(self):
        """Maneja el clic en Autenticar: llama a la API y actualiza estado."""
        document = self.stored_credentials.get("document", "").strip()
        credential = self.stored_credentials.get("credential", "").strip()

        if not document or not credential:
            QMessageBox.warning(
                self,
                "Campos Incompletos",
                "Por favor complete los campos de documento y credencial.",
            )
            return

        self.authenticateButton.setEnabled(False)
        self.authenticateButton.setText("Autenticando...")
        QtWidgets.QApplication.processEvents()

        auth_result = self.authenticate_with_api(document, credential)

        if auth_result["success"]:
            self.is_authenticated = True
            self.auth_credentials["document"] = document
            self.auth_credentials["credential"] = credential
            self.authenticateButton.setText("✓ Autenticado")
            self.authenticateButton.setStyleSheet(f"""
                QPushButton {{
                    background-color: {AZUL_MEDIO};
                    color: white;
                    border: none;
                    padding: 8px;
                    border-radius: 4px;
                    font-size: 12px;
                }}
            """)
            self.set_post_auth_fields_enabled(True)
            self.save_credentials()
            QMessageBox.information(
                self,
                "Autenticación Exitosa",
                "Credenciales validadas correctamente.\n\nAhora puede seleccionar las capas y ejecutar la optimización.",
            )
        else:
            self.is_authenticated = False
            self.authenticateButton.setEnabled(True)
            self.authenticateButton.setText("Autenticar")
            error_message = auth_result["message"]
            if "demasiados intentos" in error_message.lower() or "excedido el límite" in error_message.lower():
                QMessageBox.warning(
                    self,
                    "Límite de Intentos Excedido",
                    f"{error_message}\n\nEspere unos minutos antes de intentar nuevamente.",
                )
            else:
                QMessageBox.critical(
                    self,
                    "Autenticación Fallida",
                    f"No se pudieron validar las credenciales:\n\n{error_message}",
                )

    def authenticate_with_api(self, document, credential):
        """
        Autentica las credenciales (documento + contraseña) con la API. Sin distinción
        de rol: cualquier usuario registrado puede usar la herramienta.
        """
        try:
            if not AUTH_ENABLED:
                return {"success": True, "message": "Credenciales verificadas localmente"}
            auth_url = AUTH_ENDPOINT
            api_key = AUTH_API_KEY
            auth_data = {
                "documento": str(document).strip(),
                "clave": str(credential).strip(),
            }

            headers = {"Content-Type": "application/json", "X-API-Key": api_key}
            response = requests.post(
                auth_url,
                json=auth_data,
                timeout=AUTH_TIMEOUT,
                headers=headers,
            )

            if response.status_code != 200:
                try:
                    err = response.json()
                    msg = err.get("message", err.get("error", "Error desconocido"))
                    if "demasiados intentos" in msg.lower() or "demasiados intentos" in err.get("error", "").lower():
                        return {
                            "success": False,
                            "message": err.get("message", "Has excedido el límite de intentos. Intenta más tarde."),
                        }
                    return {"success": False, "message": msg}
                except (ValueError, KeyError):
                    return {
                        "success": False,
                        "message": f"Error del servidor de autenticación (código {response.status_code})",
                    }

            result = response.json()
            if result.get("autenticado") == 1:
                return {
                    "success": True,
                    "message": result.get("mensaje", "Autenticación exitosa"),
                    "user_info": result.get("user_info", result),
                }
            error_message = result.get("mensaje", "Credenciales incorrectas o usuario no encontrado.")
            return {"success": False, "message": error_message}

        except ValueError:
            return {"success": False, "message": "Respuesta inválida del servidor de autenticación"}
        except requests.exceptions.Timeout:
            return {"success": False, "message": "Tiempo de espera agotado. Verifique su conexión a internet."}
        except requests.exceptions.ConnectionError:
            return {"success": False, "message": "Error de conexión con el servidor de autenticación."}
        except Exception as e:
            logger.exception("Error durante autenticación")
            return {"success": False, "message": f"Error inesperado: {str(e)}"}

    def set_post_auth_fields_enabled(self, enabled):
        """Habilita o deshabilita capas y parámetros (solo tras autenticación correcta)."""
        for name in ("lotsCombo", "roadsCombo", "acopiosCombo",
                     "productividadCombo", "preciosCombo",
                     "pSpinBox", "intervalDoubleSpinBox", "optimizeButton", "resetButton"):
            w = getattr(self, name, None)
            if w is not None:
                w.setEnabled(enabled)

    def save_credentials(self):
        """Guarda en QSettings que hay credenciales (y valores para esta sesión)."""
        try:
            settings = QSettings()
            doc = self.auth_credentials.get("document", "").strip()
            cred = self.auth_credentials.get("credential", "").strip()
            if doc and cred:
                settings.setValue(f"{AUTH_SETTINGS_PREFIX}/has_document", True)
                settings.setValue(f"{AUTH_SETTINGS_PREFIX}/has_credential", True)
                settings.setValue(f"{AUTH_SETTINGS_PREFIX}/document", doc)
                settings.setValue(f"{AUTH_SETTINGS_PREFIX}/credential", cred)
            else:
                settings.setValue(f"{AUTH_SETTINGS_PREFIX}/has_document", False)
                settings.setValue(f"{AUTH_SETTINGS_PREFIX}/has_credential", False)
                settings.remove(f"{AUTH_SETTINGS_PREFIX}/document")
                settings.remove(f"{AUTH_SETTINGS_PREFIX}/credential")
        except Exception as e:
            logger.warning("No se pudieron guardar credenciales: %s", e)

    def load_saved_credentials(self):
        """Carga credenciales guardadas y rellena campos (enmascarados)."""
        try:
            settings = QSettings()
            has_doc = settings.value(f"{AUTH_SETTINGS_PREFIX}/has_document", False, type=bool)
            has_cred = settings.value(f"{AUTH_SETTINGS_PREFIX}/has_credential", False, type=bool)
            if has_doc and has_cred:
                doc = settings.value(f"{AUTH_SETTINGS_PREFIX}/document", "")
                cred = settings.value(f"{AUTH_SETTINGS_PREFIX}/credential", "")
                if doc and cred:
                    self.documentLineEdit.setText("*" * min(len(doc), 12))
                    self.credentialLineEdit.setText("*" * min(len(cred), 12))
                    self.auth_credentials["document"] = doc
                    self.auth_credentials["credential"] = cred
                    self.stored_credentials = {"document": doc, "credential": cred}
                    self.validate_auth_fields()
        except Exception as e:
            logger.warning("No se pudieron cargar credenciales guardadas: %s", e)

    def clear_credentials(self):
        """Borra credenciales de QSettings y limpia campos."""
        try:
            settings = QSettings()
            for key in ("has_document", "has_credential", "document", "credential"):
                settings.remove(f"{AUTH_SETTINGS_PREFIX}/{key}")
            if hasattr(self, "documentLineEdit"):
                self.documentLineEdit.clear()
            if hasattr(self, "credentialLineEdit"):
                self.credentialLineEdit.clear()
            self.auth_credentials = {"document": "", "credential": ""}
            self.stored_credentials = {"document": "", "credential": ""}
            self.is_authenticated = False
            if hasattr(self, "authenticateButton"):
                self.authenticateButton.setText("Autenticar")
                self.authenticateButton.setEnabled(False)
            self.set_post_auth_fields_enabled(False)
            self.validate_auth_fields()
        except Exception as e:
            logger.warning("Error al limpiar credenciales: %s", e)

    # -------------------------------------------------------------------------
    # Reiniciar panel
    # -------------------------------------------------------------------------

    def _on_reset(self):
        """Limpia el cuadro de resultados y repuebla las columnas opcionales desde la capa de lotes activa."""
        if hasattr(self, "resultsSummaryText"):
            self.resultsSummaryText.clear()
        if hasattr(self, "resultsSummaryGroup"):
            self.resultsSummaryGroup.setVisible(False)
        self._populate_column_combos()

    # -------------------------------------------------------------------------
    # Optimización (llamada a API con X-API-Key, igual que Detector de Palmas)
    # -------------------------------------------------------------------------

    def _output_dir_from_layer(self, layer):
        """
        Carpeta base para resultados: mismo directorio que la capa de origen.
        Si la capa no tiene ruta de archivo (p. ej. memoria), usa carpeta del proyecto o temporal.
        """
        try:
            src = layer.source()
            if not src:
                return None
            # Quitar parámetros tipo "|layerid=0" o "?..." en conexiones
            path = src.split("|")[0].split("?")[0].strip()
            if path.startswith("file://"):
                path = path[7:]
            dir_path = os.path.dirname(path)
            if dir_path and os.path.isdir(dir_path):
                return dir_path
        except Exception:
            pass
        try:
            project_path = QgsProject.instance().fileName()
            if project_path:
                return os.path.dirname(project_path)
        except Exception:
            pass
        return os.path.expanduser("~")

    def _sanitize_fid_field(self, layer):
        """
        GeoPackage reserva el nombre de campo 'fid' para su columna interna de
        identificador (debe ser entero). Capas resultantes de fusionar capas KML
        (p. ej. con "Combinar capas vectoriales") suelen traer un campo de
        atributo literal llamado 'fid' con tipo mixto/texto, lo que hace que GDAL
        falle al exportar a GPKG con "Wrong field type for fid". Si se detecta ese
        campo, se devuelve una copia en memoria con el campo renombrado a
        'fid_origen'; si no existe, se devuelve la capa original sin cambios.
        """
        fid_idx = layer.fields().lookupField("fid")
        if fid_idx < 0:
            return layer

        mem_layer = QgsVectorLayer(
            f"{QgsWkbTypes.displayString(layer.wkbType())}?crs={layer.crs().authid()}",
            layer.name(),
            "memory",
        )
        mem_provider = mem_layer.dataProvider()

        new_fields = []
        for field in layer.fields():
            if field.name().lower() == "fid":
                renamed = QgsField(field)
                renamed.setName("fid_origen")
                new_fields.append(renamed)
            else:
                new_fields.append(field)
        mem_provider.addAttributes(new_fields)
        mem_layer.updateFields()

        mem_provider.addFeatures(list(layer.getFeatures()))
        mem_layer.updateExtents()
        return mem_layer

    def _layer_to_temp_gpkg(self, layer, prefix):
        """Exporta una capa a un archivo GPKG temporal. Retorna la ruta (mismo estilo que Detector)."""
        export_layer = self._sanitize_fid_field(layer)
        fd, path = tempfile.mkstemp(suffix=".gpkg", prefix=prefix)
        os.close(fd)
        err_code, err_msg = QgsVectorFileWriter.writeAsVectorFormat(
            export_layer,
            path,
            "UTF-8",
            export_layer.crs(),
            "GPKG",
        )
        if err_code != QgsVectorFileWriter.NoError:
            try:
                os.remove(path)
            except Exception:
                pass
            raise RuntimeError(err_msg or "Error exportando capa a GPKG")
        return path

    def _reorder_result_layers(
        self, layer_opt, layer_sub, lots_layer, roads_layer, acopios_layer
    ):
        """
        Reordenar capas en el panel está deshabilitado: group.reorderGroupLayers()
        puede provocar un access violation en código nativo de QGIS (crash). Las capas
        se añaden al proyecto y el usuario puede reordenarlas manualmente si lo desea.
        """
        # No llamar a group.reorderGroupLayers(new_order): causa crash en QGIS 3.34
        # (Windows fatal exception: access violation en QgsLayerTreeGroup::reorderGroupLayers).
        pass

    def _disable_sublots_layer_in_panel(self, layer_sub):
        """Desactiva (quita el check) la capa Sub-lotes asignados en el panel de capas.
        La capa se sigue generando y se añade al proyecto, pero el usuario no la ve por defecto;
        quedan activas: lotes, carreteras y acopios óptimos."""
        if layer_sub is None or not layer_sub.isValid():
            return
        try:
            root = QgsProject.instance().layerTreeRoot()
            if root is None:
                return
            node = root.findLayer(layer_sub.id())
            if node is not None:
                node.setItemVisibilityChecked(False)
        except Exception as e:
            logger.warning("No se pudo desactivar la capa Sub-lotes en el panel: %s", e)

    def _apply_result_layer_styles(self, layer_opt, layer_sub):
        """Aplica estilos por defecto: Sub-lotes = outline blue, Acopios óptimos = diamond red."""
        if layer_sub is not None and layer_sub.isValid():
            try:
                sym = QgsFillSymbol()
                sym.deleteSymbolLayer(0)
                fill_layer = QgsSimpleFillSymbolLayer()
                fill_layer.setStrokeColor(QColor(0, 0, 255))
                fill_layer.setStrokeWidth(0.52)
                fill_layer.setBrushStyle(Qt.BrushStyle.NoBrush)
                sym.appendSymbolLayer(fill_layer)
                layer_sub.setRenderer(QgsSingleSymbolRenderer(sym))
                layer_sub.triggerRepaint()
            except Exception as e:
                logger.warning("No se pudo aplicar estilo a Sub-lotes asignados: %s", e)
        if layer_opt is not None and layer_opt.isValid():
            try:
                sym = QgsMarkerSymbol()
                sym.deleteSymbolLayer(0)
                marker_layer = QgsSimpleMarkerSymbolLayer()
                marker_layer.setShape(Qgis.MarkerShape.Diamond)
                marker_layer.setSize(4.0)
                marker_layer.setColor(QColor(255, 0, 0))
                marker_layer.setStrokeColor(QColor(0, 0, 0))
                marker_layer.setStrokeWidth(0.2)
                sym.appendSymbolLayer(marker_layer)
                layer_opt.setRenderer(QgsSingleSymbolRenderer(sym))
                layer_opt.triggerRepaint()
            except Exception as e:
                logger.warning("No se pudo aplicar estilo a Acopios óptimos: %s", e)

    def _parse_optimization_report_to_html(self, report_path):
        """
        Lee optimization_report.txt y devuelve HTML con Resultados y tabla
        Distancias (m) para mostrar en el widget.
        """
        if not os.path.isfile(report_path):
            return ""
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.warning("No se pudo leer el reporte: %s", e)
            return ""

        puntos_demanda = ""
        candidatos = ""
        table_rows = []
        escenario_simulado = False  # True cuando no hay acopios actuales: solo Métrica y Óptimo

        lines = content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.strip() == "Resultados":
                i += 1
                while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith("Distancias"):
                    m = re.match(r"\s*Puntos de demanda:\s*(\d+)", lines[i])
                    if m:
                        puntos_demanda = m.group(1)
                    m = re.match(r"\s*Candidatos evaluados:\s*(\d+)", lines[i])
                    if m:
                        candidatos = m.group(1)
                    i += 1
                continue
            if "Distancias (m)" in line and "Escenario" in line:
                # Sin acopios actuales: tabla con solo Métrica y Óptimo
                escenario_simulado = True
                i += 1
                if i >= len(lines):
                    break
                i += 1  # skip header "  Métrica     Óptimo"
                if i < len(lines) and "---" in lines[i]:
                    i += 1
                while i < len(lines) and lines[i].strip():
                    row = lines[i]
                    m = re.match(r"\s*(Media|Mediana|Máximo)\s+([\d.]+)\s*$", row)
                    if m:
                        table_rows.append((m.group(1), m.group(2)))
                    i += 1
                break
            if "Distancias (m)" in line and "Escenario" not in line:
                i += 1
                if i >= len(lines):
                    break
                i += 1  # skip header line "  Métrica ..."
                if i < len(lines) and "---" in lines[i]:
                    i += 1
                while i < len(lines) and lines[i].strip():
                    row = lines[i]
                    m = re.match(r"\s*(Media|Mediana|Máximo)\s+([\d.]+)\s+([\d.]+)(?:\s+([+\d.-]+%?))?", row)
                    if m:
                        mejora = m.group(4) if m.lastindex >= 4 and m.group(4) else "—"
                        table_rows.append((m.group(1), m.group(2), m.group(3), mejora))
                    i += 1
                break
            i += 1

        html_parts = [
            "<div style='font-family: Segoe UI, sans-serif; font-size: 11px; color: #333;'>",
        ]
        if escenario_simulado and table_rows:
            html_parts.append("<p style='margin: 0 0 6px 0;'><b style='color: #002856;'>Distancias del escenario óptimo (m)</b></p>")
            html_parts.append(
                "<table style='border-collapse: collapse; border: 1px solid #dee2e6; border-radius: 4px; margin-bottom: 12px;'>"
                "<thead><tr style='background-color: #002856; color: white;'>"
                "<th style='padding: 6px 10px; text-align: left;'>Métrica</th>"
                "<th style='padding: 6px 10px; text-align: right;'>Distancia (m)</th>"
                "</tr></thead><tbody>"
            )
            for idx, (metrica, optimo) in enumerate(table_rows):
                bg = "#f8f9fa" if idx % 2 == 1 else "#fff"
                html_parts.append(
                    f"<tr style='background-color: {bg};'>"
                    f"<td style='padding: 5px 10px;'>{metrica}</td>"
                    f"<td style='padding: 5px 10px; text-align: right;'>{optimo}</td>"
                    "</tr>"
                )
        else:
            html_parts.append("<p style='margin: 0 0 6px 0;'><b style='color: #002856;'>Distancias (m)</b></p>")
            html_parts.append(
                "<table style='border-collapse: collapse; border: 1px solid #dee2e6; border-radius: 4px; margin-bottom: 12px;'>"
                "<thead><tr style='background-color: #002856; color: white;'>"
                "<th style='padding: 6px 10px; text-align: left;'>Métrica</th>"
                "<th style='padding: 6px 10px; text-align: right;'>Actual</th>"
                "<th style='padding: 6px 10px; text-align: right;'>Óptimo</th>"
                "<th style='padding: 6px 10px; text-align: right;'>Mejora</th>"
                "</tr></thead><tbody>"
            )
            for idx, row in enumerate(table_rows):
                if len(row) != 4:
                    continue
                metrica, actual, optimo, mejora = row
                bg = "#f8f9fa" if idx % 2 == 1 else "#fff"
                html_parts.append(
                    f"<tr style='background-color: {bg};'>"
                    f"<td style='padding: 5px 10px;'>{metrica}</td>"
                    f"<td style='padding: 5px 10px; text-align: right;'>{actual}</td>"
                    f"<td style='padding: 5px 10px; text-align: right;'>{optimo}</td>"
                    f"<td style='padding: 5px 10px; text-align: right;'>{mejora}</td>"
                    "</tr>"
                )
        html_parts.append("</tbody></table>")
        html_parts.append("</div>")
        return "".join(html_parts)

    def _on_optimize(self):
        """Ejecuta la optimización vía API: exporta capas, POST con X-API-Key, carga resultados."""
        if not self.is_authenticated:
            QMessageBox.warning(
                self,
                "Optimizador de Acopios",
                "Debe autenticarse antes de ejecutar la optimización.",
            )
            return

        lots_layer = self.lotsCombo.currentLayer() if hasattr(self, "lotsCombo") else None
        roads_layer = self.roadsCombo.currentLayer() if hasattr(self, "roadsCombo") else None
        acopios_layer = self._get_acopios_layer()

        if not lots_layer or not roads_layer:
            QMessageBox.warning(
                self,
                "Optimizador de Acopios",
                "Seleccione las capas de Lotes y Carreteras. La capa de Acopios actuales es opcional.",
            )
            return

        if not API_KEY:
            QMessageBox.warning(
                self,
                "Optimizador de Acopios",
                "No está configurada la X-API-Key. Configure config_secrets.py o la variable OPTIMIZADOR_ACOPIOS_API_KEY.",
            )
            return

        p_val = self.pSpinBox.value() if hasattr(self, "pSpinBox") else DEFAULT_P_ACOPIOS
        # Distancia entre candidatos fija en 50 m; la opción está oculta en la interfaz
        interval_val = DEFAULT_ROAD_INTERVAL_M

        progress = QProgressDialog("Preparando capas...", None, 0, 0, self)
        progress.setWindowTitle("Optimizador de Acopios")
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents()

        try:
            tmp_dir = tempfile.mkdtemp(prefix="optimizador_acopios_")
            progress.setLabelText("Exportando capas a GPKG...")
            QtWidgets.QApplication.processEvents()

            lots_path = self._layer_to_temp_gpkg(lots_layer, "lots_")
            roads_path = self._layer_to_temp_gpkg(roads_layer, "roads_")
            acopios_path = None
            if acopios_layer:
                acopios_path = self._layer_to_temp_gpkg(acopios_layer, "acopios_")

            progress.setLabelText("Enviando a la API...")
            QtWidgets.QApplication.processEvents()

            files = {
                "lots": ("lotes.gpkg", open(lots_path, "rb"), "application/octet-stream"),
                "roads": ("carreteras.gpkg", open(roads_path, "rb"), "application/octet-stream"),
            }
            if acopios_path:
                files["current_acopios"] = ("acopios.gpkg", open(acopios_path, "rb"), "application/octet-stream")
            yield_col = None
            if hasattr(self, "productividadCombo"):
                yield_col = self.productividadCombo.currentData()  # None = "Sin producción"

            price_col = None
            if hasattr(self, "preciosCombo"):
                price_col = self.preciosCombo.currentData()  # None = "Sin precios"

            data = {
                "p": p_val,
                "road_interval": interval_val,
                "target_crs": "EPSG:3116",
                "time_limit": 600,
                "gap_rel": 0.0,
            }
            if yield_col:
                # El usuario seleccionó una columna específica
                data["yield_col"] = yield_col
            else:
                # El usuario eligió "Sin producción": deshabilitar auto-detección
                data["no_yield_col"] = "1"

            if price_col:
                data["price_col"] = price_col
            else:
                data["no_price_col"] = "1"
            from ..client_identity import cabeceras_autenticacion
            headers = cabeceras_autenticacion()
            try:
                response = requests.post(
                    OPTIMIZE_ENDPOINT,
                    files=files,
                    data=data,
                    headers=headers,
                    timeout=REQUESTS_TIMEOUT,
                )
            finally:
                for fh in files.values():
                    if hasattr(fh[1], "close"):
                        fh[1].close()
            for path in (lots_path, roads_path) + ((acopios_path,) if acopios_path else ()):
                try:
                    if path:
                        os.remove(path)
                except Exception:
                    pass
            try:
                os.rmdir(tmp_dir)
            except Exception:
                pass

            progress.setLabelText("Procesando respuesta...")
            QtWidgets.QApplication.processEvents()

            if response.status_code == 401:
                progress.close()
                QMessageBox.warning(
                    self,
                    "Optimizador de Acopios",
                    "X-API-Key inválida o ausente. Verifique la configuración del plugin.",
                )
                return

            if response.status_code != 200:
                progress.close()
                try:
                    detail = response.json().get("detail", response.text)
                except Exception:
                    detail = response.text or f"Código {response.status_code}"
                QMessageBox.critical(
                    self,
                    "Error en la API",
                    f"La API respondió con error:\n\n{detail}",
                )
                return

            fd_zip, zip_path = tempfile.mkstemp(suffix=".zip", prefix="optimizador_acopios_")
            os.close(fd_zip)
            with open(zip_path, "wb") as f:
                f.write(response.content)

            base_dir = self._output_dir_from_layer(lots_layer)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            results_folder_name = f"resultados_optimizador_{timestamp}"
            results_dir = os.path.join(base_dir, results_folder_name)
            os.makedirs(results_dir, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(results_dir)

            try:
                os.remove(zip_path)
            except Exception:
                pass

            opt_points_path = os.path.join(results_dir, "optimal_collection_points.gpkg")
            sublots_path = os.path.join(results_dir, "sublot_assignments.gpkg")

            layer_opt = None
            layer_sub = None
            if os.path.isfile(opt_points_path):
                layer_opt = QgsVectorLayer(opt_points_path, "Acopios óptimos", "ogr")
                if layer_opt.isValid():
                    QgsProject.instance().addMapLayer(layer_opt)
            if INCLUDE_SUBLOTS_LAYER and os.path.isfile(sublots_path):
                layer_sub = QgsVectorLayer(sublots_path, "Sub-lotes asignados", "ogr")
                if layer_sub.isValid():
                    QgsProject.instance().addMapLayer(layer_sub)
                    self._disable_sublots_layer_in_panel(layer_sub)

            self._apply_result_layer_styles(layer_opt, layer_sub)

            # Orden deseado en el panel (arriba = se dibuja encima): Acopios óptimos, acopios, carreteras, Sub-lotes asignados, lotes
            self._reorder_result_layers(
                layer_opt, layer_sub, lots_layer, roads_layer, acopios_layer
            )

            report_path = os.path.join(results_dir, "optimization_report.txt")
            html = self._parse_optimization_report_to_html(report_path)
            if html and hasattr(self, "resultsSummaryText"):
                self.resultsSummaryText.setHtml(html)
            if hasattr(self, "resultsSummaryGroup"):
                self.resultsSummaryGroup.setVisible(True)

            progress.close()
            msg = QMessageBox(self)
            msg.setWindowTitle("Optimizador de Acopios")
            msg.setIcon(QMessageBox.Information)
            msg.setText(
                "✓ Optimización completada.\n\n"
                "📁 Carpeta de resultados:\n"
                "La misma de las capas base \\" + results_folder_name + "\n\n"
                "🗺 La capa 'Acopios óptimos' se ha añadido al mapa."
            )
            open_btn = msg.addButton("Abrir carpeta", QMessageBox.ActionRole)
            msg.addButton(QMessageBox.Ok)
            msg.exec_()
            if msg.clickedButton() == open_btn:
                QDesktopServices.openUrl(QUrl.fromLocalFile(results_dir))

        except requests.exceptions.Timeout:
            progress.close()
            QMessageBox.critical(
                self,
                "Optimizador de Acopios",
                "Tiempo de espera agotado. La optimización puede tardar varios minutos.",
            )
        except requests.exceptions.ConnectionError:
            progress.close()
            QMessageBox.critical(
                self,
                "Optimizador de Acopios",
                "Error de conexión con la API. Compruebe la URL y que el servidor esté disponible.",
            )
        except Exception as e:
            progress.close()
            logger.exception("Error en optimización")
            QMessageBox.critical(
                self,
                "Optimizador de Acopios",
                f"Error inesperado:\n\n{str(e)}",
            )

    def closeEvent(self, event):
        self.closingPlugin.emit()
        event.accept()
