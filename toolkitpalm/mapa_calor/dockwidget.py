# -*- coding: utf-8 -*-
"""
Mapa de calor: convierte una tabla del usuario (Excel, CSV u ODS) con
coordenadas en un mapa coloreado por rangos de una de sus variables, numérica
o de texto (baja, media, alta…), como superficie interpolada o como zonas por
punto (Thiessen), opcionalmente delimitado por un lote o una finca.

Corre en el equipo del usuario: no usa el servidor ni créditos.
"""
import logging
import os
import tempfile

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDockWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
                                 QComboBox, QSpinBox, QPushButton, QCheckBox, QLineEdit,
                                 QGroupBox, QRadioButton, QListWidget, QFileDialog,
                                 QMessageBox, QApplication, QButtonGroup)
from qgis.core import (QgsProject, QgsCoordinateReferenceSystem, QgsMapLayerProxyModel,
                       QgsRasterLayer, QgsVectorFileWriter)
from qgis.gui import QgsMapLayerComboBox, QgsProjectionSelectionWidget

from ..common.colors import AZUL_MEDIO, AZUL_CLARO, AZUL_OSCURO
from . import lectura, clases

logger = logging.getLogger(__name__)

_METODOS = [
    ("Bajo / Medio / Alto (tercios de los datos)", clases.TERCILES),
    ("Cuantiles (misma cantidad de puntos por rango)", clases.CUANTILES),
    ("Intervalos iguales", clases.IGUALES),
    ("Cortes naturales (agrupa valores parecidos)", clases.NATURALES),
    ("Límites propios", clases.MANUAL),
    ("Según otra columna del Excel", "otra"),
]

_TXT_INTERPOLADA = (
    "<b>Superficie interpolada</b>: una mancha continua y suave entre los puntos; "
    "cada lugar toma un valor estimado a partir de los puntos más cercanos. "
    "Sirve para ver tendencias en el lote, como un mapa de calor clásico.")
_TXT_THIESSEN = (
    "<b>Zonas por punto (Thiessen)</b>: cada punto pinta el área que le queda más "
    "cerca, con bordes rectos. Es fiel al dato: cada zona es exactamente un "
    "punto de muestreo, sin valores estimados.")


class MapaCalorDockWidget(QDockWidget):

    def __init__(self, iface, parent=None):
        super().__init__("Mapa de calor", parent)
        self.iface = iface
        self.terms_accepted = True  # herramienta local: no sube nada
        self._columnas, self._filas, self._tipos = [], [], {}
        self._por_categorias = False   # True si los rangos son categorías de texto
        self._build_ui()

    # ── Interfaz ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        contenido = QtWidgets.QWidget()
        raiz = QVBoxLayout(contenido)

        intro = QLabel(
            "<b>Mapa de calor desde un Excel</b><br>Cargue una tabla con coordenadas "
            "(Excel, CSV u ODS): se detectan solas las columnas de X/Y y las demás "
            "variables. Elija la variable, sus rangos y los colores, y el tipo de mapa.<br>"
            "<i>Se procesa en este equipo: no usa créditos.</i>")
        intro.setWordWrap(True)
        raiz.addWidget(intro)

        # 1. Archivo
        g1 = QGroupBox("1. Archivo")
        f1 = QFormLayout(g1)
        fila = QHBoxLayout()
        self.edit_archivo = QLineEdit()
        self.edit_archivo.setPlaceholderText("Elija un Excel, CSV u ODS…")
        self.edit_archivo.editingFinished.connect(self._cargar_archivo)
        fila.addWidget(self.edit_archivo, 1)
        b = QPushButton("…")
        b.setFixedWidth(32)
        b.clicked.connect(self._elegir_archivo)
        fila.addWidget(b)
        f1.addRow("Tabla:", fila)
        self.combo_hoja = QComboBox()
        self.combo_hoja.currentIndexChanged.connect(self._cargar_hoja)
        f1.addRow("Hoja:", self.combo_hoja)
        raiz.addWidget(g1)

        # 2. Coordenadas
        self.g2 = QGroupBox("2. Coordenadas")
        f2 = QFormLayout(self.g2)
        self.combo_x = QComboBox()
        self.combo_y = QComboBox()
        self.combo_x.currentIndexChanged.connect(self._detectar_crs)
        self.combo_y.currentIndexChanged.connect(self._detectar_crs)
        f2.addRow("Columna X (este / longitud):", self.combo_x)
        f2.addRow("Columna Y (norte / latitud):", self.combo_y)
        self.selector_crs = QgsProjectionSelectionWidget()
        f2.addRow("Sistema de coordenadas:", self.selector_crs)
        self.lbl_coord = QLabel("")
        self.lbl_coord.setWordWrap(True)
        self.lbl_coord.setStyleSheet("color:#555; font-size:11px;")
        f2.addRow(self.lbl_coord)
        raiz.addWidget(self.g2)

        # 3. Variable
        self.g3 = QGroupBox("3. Variable a mapear")
        f3 = QFormLayout(self.g3)
        self.combo_var = QComboBox()
        self.combo_var.currentIndexChanged.connect(self._variable_cambiada)
        f3.addRow("Variable:", self.combo_var)
        self.lbl_var = QLabel("")
        self.lbl_var.setWordWrap(True)
        self.lbl_var.setStyleSheet("color:#555; font-size:11px;")
        f3.addRow(self.lbl_var)
        raiz.addWidget(self.g3)

        # 4. Rangos
        self.g4 = QGroupBox("4. Rangos")
        v4 = QVBoxLayout(self.g4)
        self.panel_numerico = QtWidgets.QWidget()
        fn = QFormLayout(self.panel_numerico)
        fn.setContentsMargins(0, 0, 0, 0)
        self.combo_metodo = QComboBox()
        for texto, clave in _METODOS:
            self.combo_metodo.addItem(texto, clave)
        self.combo_metodo.currentIndexChanged.connect(self._metodo_cambiado)
        fn.addRow("Cómo dividir:", self.combo_metodo)
        self.spin_clases = QSpinBox()
        self.spin_clases.setRange(2, 9)
        self.spin_clases.setValue(3)
        fn.addRow("Cantidad de rangos:", self.spin_clases)
        self.edit_limites = QLineEdit()
        self.edit_limites.setPlaceholderText("Ej.: 2,5 ; 3,0   (límites entre rangos)")
        fn.addRow("Límites:", self.edit_limites)
        self.combo_otra = QComboBox()
        self.combo_otra.currentIndexChanged.connect(self._metodo_cambiado)
        fn.addRow("Columna con los rangos:", self.combo_otra)
        v4.addWidget(self.panel_numerico)

        self.panel_categorias = QtWidgets.QWidget()
        vc = QVBoxLayout(self.panel_categorias)
        vc.setContentsMargins(0, 0, 0, 0)
        self.lbl_orden = QLabel("Orden de los rangos (de primero a último color):")
        self.lbl_orden.setWordWrap(True)
        vc.addWidget(self.lbl_orden)
        filao = QHBoxLayout()
        self.lista_cat = QListWidget()
        self.lista_cat.setMaximumHeight(120)
        filao.addWidget(self.lista_cat, 1)
        botones = QVBoxLayout()
        for texto, paso in (("▲", -1), ("▼", 1)):
            bt = QPushButton(texto)
            bt.setFixedWidth(32)
            bt.clicked.connect(lambda _c=False, p=paso: self._mover_categoria(p))
            botones.addWidget(bt)
        botones.addStretch(1)
        filao.addLayout(botones)
        vc.addLayout(filao)
        v4.addWidget(self.panel_categorias)
        raiz.addWidget(self.g4)

        # 5. Colores
        self.g5 = QGroupBox("5. Colores")
        f5 = QFormLayout(self.g5)
        self.combo_rampa = QComboBox()
        self.combo_rampa.addItems(list(clases.RAMPAS))
        self.combo_rampa.currentIndexChanged.connect(self._muestra_colores)
        f5.addRow("Rampa:", self.combo_rampa)
        self.chk_invertir = QCheckBox("Invertir (usar si en esta variable lo alto es lo malo)")
        self.chk_invertir.toggled.connect(self._muestra_colores)
        f5.addRow(self.chk_invertir)
        self.lbl_muestra = QLabel("")
        f5.addRow("Vista:", self.lbl_muestra)
        self.spin_clases.valueChanged.connect(self._muestra_colores)
        raiz.addWidget(self.g5)

        # 6. Tipo de mapa
        self.g6 = QGroupBox("6. Tipo de mapa")
        v6 = QVBoxLayout(self.g6)
        self.radio_interp = QRadioButton("Superficie interpolada")
        self.radio_thiessen = QRadioButton("Zonas por punto (Thiessen)")
        self.radio_interp.setChecked(True)
        grupo = QButtonGroup(self)
        grupo.addButton(self.radio_interp)
        grupo.addButton(self.radio_thiessen)
        for radio, texto in ((self.radio_interp, _TXT_INTERPOLADA),
                             (self.radio_thiessen, _TXT_THIESSEN)):
            v6.addWidget(radio)
            ex = QLabel(texto)
            ex.setWordWrap(True)
            ex.setStyleSheet("color:#555; font-size:11px; margin-left:18px;")
            v6.addWidget(ex)
        raiz.addWidget(self.g6)

        # 7. Delimitar
        self.g7 = QGroupBox("7. Delimitar el mapa")
        f7 = QFormLayout(self.g7)
        self.chk_delimitar = QCheckBox("Que no se salga de un lote o una finca")
        self.chk_delimitar.toggled.connect(self._delimitar_cambiado)
        f7.addRow(self.chk_delimitar)
        self.combo_limite = QgsMapLayerComboBox()
        self.combo_limite.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.combo_limite.layerChanged.connect(self._delimitar_cambiado)
        f7.addRow("Capa de polígonos:", self.combo_limite)
        self.chk_seleccion = QCheckBox("Solo los polígonos seleccionados")
        self.chk_seleccion.toggled.connect(self._delimitar_cambiado)
        f7.addRow(self.chk_seleccion)
        self.panel_modo = QtWidgets.QWidget()
        vm = QVBoxLayout(self.panel_modo)
        vm.setContentsMargins(0, 0, 0, 0)
        vm.addWidget(QLabel("Hay varios polígonos: ¿qué delimita el mapa?"))
        self.radio_lotes = QRadioButton("Los lotes (quedan fuera vías y espacios entre lotes)")
        self.radio_lindero = QRadioButton("El lindero de la finca (contorno exterior completo)")
        self.radio_lotes.setChecked(True)
        grupo2 = QButtonGroup(self)
        grupo2.addButton(self.radio_lotes)
        grupo2.addButton(self.radio_lindero)
        vm.addWidget(self.radio_lotes)
        vm.addWidget(self.radio_lindero)
        f7.addRow(self.panel_modo)
        raiz.addWidget(self.g7)

        # 8. Guardar
        g8 = QGroupBox("8. Guardar (opcional)")
        f8 = QFormLayout(g8)
        filag = QHBoxLayout()
        self.edit_carpeta = QLineEdit()
        self.edit_carpeta.setPlaceholderText("Vacío: solo en el mapa (temporal)")
        filag.addWidget(self.edit_carpeta, 1)
        bg = QPushButton("…")
        bg.setFixedWidth(32)
        bg.clicked.connect(self._elegir_carpeta)
        filag.addWidget(bg)
        f8.addRow("Carpeta:", filag)
        raiz.addWidget(g8)

        self.btn_crear = QPushButton("🔥 Crear mapa de calor")
        self.btn_crear.setStyleSheet(
            f"QPushButton {{background-color:{AZUL_MEDIO}; color:white; font-weight:bold;"
            f" padding:8px; border-radius:4px;}}"
            f"QPushButton:hover {{background-color:{AZUL_CLARO};}}"
            f"QPushButton:disabled {{background-color:#9aa5b1;}}")
        self.btn_crear.clicked.connect(self._crear)
        raiz.addWidget(self.btn_crear)
        self.lbl_estado = QLabel("")
        self.lbl_estado.setWordWrap(True)
        raiz.addWidget(self.lbl_estado)
        raiz.addStretch(1)

        self.setWidget(contenido)
        self._habilitar(False)
        self._metodo_cambiado()
        self._delimitar_cambiado()
        self._muestra_colores()

    def _habilitar(self, si):
        for g in (self.g2, self.g3, self.g4, self.g5, self.g6, self.g7):
            g.setEnabled(si)
        self.btn_crear.setEnabled(si)

    # ── Archivo y columnas ───────────────────────────────────────────────────

    def _elegir_archivo(self):
        ruta, _ = QFileDialog.getOpenFileName(
            self, "Tabla con coordenadas", self.edit_archivo.text(),
            "Tablas (*.xlsx *.xls *.csv *.ods *.txt)")
        if ruta:
            self.edit_archivo.setText(ruta)
            self._cargar_archivo()

    def _elegir_carpeta(self):
        ruta = QFileDialog.getExistingDirectory(self, "Carpeta para guardar", self.edit_carpeta.text())
        if ruta:
            self.edit_carpeta.setText(ruta)

    def _cargar_archivo(self):
        ruta = self.edit_archivo.text().strip()
        if not ruta or not os.path.exists(ruta):
            return
        try:
            nombres = lectura.hojas(ruta)
        except Exception as e:
            QMessageBox.warning(self, "Mapa de calor", f"No se pudo leer el archivo:\n{e}")
            return
        self.combo_hoja.blockSignals(True)
        self.combo_hoja.clear()
        self.combo_hoja.addItems(nombres)
        self.combo_hoja.blockSignals(False)
        self.combo_hoja.setEnabled(len(nombres) > 1)
        self._cargar_hoja()

    def _cargar_hoja(self):
        ruta = self.edit_archivo.text().strip()
        try:
            self._columnas, self._filas = lectura.leer_tabla(ruta, self.combo_hoja.currentText() or None)
        except Exception as e:
            QMessageBox.warning(self, "Mapa de calor", f"No se pudo leer la hoja:\n{e}")
            return
        self._tipos = {c: lectura.tipo_de_columna(self._filas, c) for c in self._columnas}
        numericas = [c for c in self._columnas if self._tipos[c] == lectura.NUMERICA]
        cx, cy = lectura.detectar_xy(self._columnas, self._filas)
        for combo, elegida in ((self.combo_x, cx), (self.combo_y, cy)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(numericas)
            if elegida:
                combo.setCurrentText(elegida)
            combo.blockSignals(False)
        self._detectar_crs()

        self.combo_var.blockSignals(True)
        self.combo_var.clear()
        self.combo_otra.clear()
        for c in self._columnas:
            if c in (cx, cy) or self._tipos[c] == lectura.OTRA:
                continue
            marca = "123" if self._tipos[c] == lectura.NUMERICA else "abc"
            self.combo_var.addItem(f"{c}   [{marca}]", c)
            if self._tipos[c] == lectura.CATEGORICA:
                self.combo_otra.addItem(c, c)
        self.combo_var.blockSignals(False)
        self._habilitar(bool(cx and cy and self.combo_var.count()))
        if not (cx and cy):
            self.lbl_coord.setText("No se reconocieron columnas de coordenadas: elíjalas a mano.")
            self._habilitar(True)
        self._variable_cambiada()
        self.lbl_estado.setText(f"{len(self._filas)} filas leídas.")

    def _detectar_crs(self):
        cx, cy = self.combo_x.currentText(), self.combo_y.currentText()
        if not cx or not cy or cx == cy:
            return
        xs, ys, _ = lectura.coordenadas(self._filas, cx, cy)
        proyecto = QgsProject.instance().crs().authid() or None
        plausibles = lectura.detectar_crs(xs, ys, proyecto)
        if plausibles:
            self.selector_crs.setCrs(QgsCoordinateReferenceSystem(plausibles[0]))
            otros = ", ".join(plausibles[1:4])
            self.lbl_coord.setText(
                f"{len(xs)} filas con coordenadas. Sistema detectado: {plausibles[0]}"
                + (f" (también posibles: {otros})" if otros else "") + ". Corríjalo si no es.")
        else:
            self.lbl_coord.setText(
                f"{len(xs)} filas con coordenadas, pero no se reconoció el sistema: elíjalo.")

    # ── Variable y rangos ───────────────────────────────────────────────────

    def _variable(self):
        return self.combo_var.currentData()

    def _columna_de_clases(self):
        """Columna que define los rangos: la variable, u otra si así se eligió."""
        var = self._variable()
        if self._tipos.get(var) == lectura.NUMERICA and self.combo_metodo.currentData() == "otra":
            return self.combo_otra.currentData()
        return var

    def _variable_cambiada(self):
        var = self._variable()
        if not var:
            return
        if self._tipos.get(var) == lectura.NUMERICA:
            vals = [lectura.a_numero(f.get(var)) for f in self._filas]
            vals = [v for v in vals if v is not None]
            if vals:
                self.lbl_var.setText(
                    f"Numérica: {len(vals)} valores, de {min(vals):g} a {max(vals):g} "
                    f"(promedio {sum(vals) / len(vals):.3g}).")
            self.panel_numerico.setVisible(True)
            self.panel_categorias.setVisible(False)
            self._por_categorias = False
        else:
            self.panel_numerico.setVisible(False)
            self.panel_categorias.setVisible(True)
            self._por_categorias = True
            self._llenar_categorias(var)
            self.lbl_var.setText(f"Texto: {self.lista_cat.count()} categorías.")
        self._metodo_cambiado()

    def _llenar_categorias(self, columna):
        orden, reconocidas = clases.ordenar_categorias([f.get(columna) for f in self._filas])
        self.lista_cat.clear()
        self.lista_cat.addItems(orden)
        self.lbl_orden.setText(
            "Orden de los rangos (de primero a último color). " +
            ("Se reconocieron como niveles." if reconocidas else
             "<b>No se reconoció un orden: revíselo con ▲ ▼.</b>"))
        self._muestra_colores()

    def _metodo_cambiado(self):
        clave = self.combo_metodo.currentData()
        numerica = self._tipos.get(self._variable()) == lectura.NUMERICA if self._columnas else True
        self.spin_clases.setEnabled(clave in (clases.CUANTILES, clases.IGUALES, clases.NATURALES))
        self.edit_limites.setEnabled(clave == clases.MANUAL)
        self.combo_otra.setEnabled(clave == "otra")
        if clave == clases.TERCILES:
            self.spin_clases.setValue(3)
        if numerica and clave == "otra" and self.combo_otra.currentData():
            self.panel_categorias.setVisible(True)
            self._por_categorias = True
            self._llenar_categorias(self.combo_otra.currentData())
        elif numerica:
            self.panel_categorias.setVisible(False)
            self._por_categorias = False
        self._muestra_colores()

    def _mover_categoria(self, paso):
        fila = self.lista_cat.currentRow()
        nueva = fila + paso
        if fila < 0 or not (0 <= nueva < self.lista_cat.count()):
            return
        item = self.lista_cat.takeItem(fila)
        self.lista_cat.insertItem(nueva, item)
        self.lista_cat.setCurrentRow(nueva)
        self._muestra_colores()

    def _cantidad_de_clases(self):
        if self._por_categorias:
            return max(1, self.lista_cat.count())
        clave = self.combo_metodo.currentData()
        if clave == clases.MANUAL:
            return len(self._limites_manuales()) + 1
        return 3 if clave == clases.TERCILES else self.spin_clases.value()

    def _limites_manuales(self):
        partes = [p for p in self.edit_limites.text().replace("|", ";").split(";") if p.strip()]
        return [v for v in (lectura.a_numero(p) for p in partes) if v is not None]

    def _muestra_colores(self):
        n = max(1, self._cantidad_de_clases()) if self._columnas else 3
        cols = clases.colores(n, self.combo_rampa.currentText(), self.chk_invertir.isChecked())
        self.lbl_muestra.setText("".join(
            f"<span style='background-color:{c}; color:{c};'>&nbsp;&nbsp;&nbsp;&nbsp;</span>"
            for c in cols))

    # ── Delimitar ───────────────────────────────────────────────────────────

    def _delimitar_cambiado(self, *_):
        activo = self.chk_delimitar.isChecked()
        self.combo_limite.setEnabled(activo)
        self.chk_seleccion.setEnabled(activo)
        capa = self.combo_limite.currentLayer()
        # Si el usuario selecciona lotes en el mapa después, la pregunta
        # "lotes o lindero" se actualiza sola.
        anterior = getattr(self, "_capa_vigilada", None)
        if anterior is not capa:
            if anterior is not None:
                try:
                    anterior.selectionChanged.disconnect(self._delimitar_cambiado)
                except (TypeError, RuntimeError):
                    logger.debug("La capa anterior ya no estaba conectada", exc_info=True)
            if capa is not None:
                capa.selectionChanged.connect(self._delimitar_cambiado)
            self._capa_vigilada = capa
        varios = False
        if activo and capa is not None:
            from .generacion import tiene_varios_poligonos
            try:
                varios = tiene_varios_poligonos(capa, self.chk_seleccion.isChecked())
            except Exception:
                logger.debug("No se pudo revisar la capa de límites", exc_info=True)
        self.panel_modo.setVisible(activo and varios)

    # ── Crear ───────────────────────────────────────────────────────────────

    def _crear(self):
        from . import generacion as G
        cx, cy = self.combo_x.currentText(), self.combo_y.currentText()
        var = self._variable()
        crs = self.selector_crs.crs()
        if not (cx and cy and var) or cx == cy:
            QMessageBox.warning(self, "Mapa de calor", "Elija las columnas X, Y y la variable.")
            return
        if not crs.isValid():
            QMessageBox.warning(self, "Mapa de calor", "Elija el sistema de coordenadas.")
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._crear_mapa(G, cx, cy, var, crs)
        except Exception as e:
            logger.warning("No se pudo crear el mapa de calor", exc_info=True)
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Mapa de calor", f"No se pudo crear el mapa:\n{e}")
            return
        QApplication.restoreOverrideCursor()

    def _crear_mapa(self, G, cx, cy, var, crs):
        puntos, sin = G.capa_de_puntos(self._columnas, self._filas, cx, cy, crs.authid(),
                                       f"Puntos – {var}")
        if puntos.featureCount() < 3:
            raise ValueError("Se necesitan al menos 3 filas con coordenadas.")

        # Área: la delimitación elegida, o la envolvente de los puntos.
        area = None
        if self.chk_delimitar.isChecked() and self.combo_limite.currentLayer() is not None:
            modo = "lindero" if self.radio_lindero.isChecked() else "lotes"
            area = G.mascara(self.combo_limite.currentLayer(), crs, modo, self.chk_seleccion.isChecked())
            if area is None or area.isEmpty():
                raise ValueError("La capa para delimitar no tiene polígonos (¿hay alguno seleccionado?).")
        if area is None:
            area = G.envolvente_de_puntos(puntos)

        # Clases y colores.
        columna = self._columna_de_clases()
        categorica = self._por_categorias
        if categorica:
            categorias = [self.lista_cat.item(i).text() for i in range(self.lista_cat.count())]
            codigo = {lectura.normalizar(c): i + 1 for i, c in enumerate(categorias)}
            valores = {f.id(): codigo.get(lectura.normalizar(f[columna])) if f[columna] not in (None, "") else None
                       for f in puntos.getFeatures()}
            n = len(categorias)
        else:
            valores = {f.id(): f[var] for f in puntos.getFeatures() if f[var] is not None}
            datos = list(valores.values())
            clave = self.combo_metodo.currentData()
            limites = clases.cortes(datos, clave, self.spin_clases.value(), self._limites_manuales())
            minimo, maximo = min(datos), max(datos)
            n = len(limites) + 1
            etiquetas = clases.nombres_de_rangos(limites, minimo, maximo)
            nombres = clases.etiquetas_bajo_medio_alto(n) if clave == clases.TERCILES else None
            if nombres:
                etiquetas = [f"{a} ({b})" for a, b in zip(nombres, etiquetas)]
        colores = clases.colores(n, self.combo_rampa.currentText(), self.chk_invertir.isChecked())

        carpeta = self.edit_carpeta.text().strip()
        if carpeta and not os.path.isdir(carpeta):
            os.makedirs(carpeta, exist_ok=True)
        destino = carpeta or tempfile.mkdtemp(prefix="tkp_mapa_calor_")
        base = "".join(ch if ch.isalnum() else "_" for ch in str(var)).strip("_") or "variable"

        raiz = QgsProject.instance().layerTreeRoot()
        grupo = raiz.insertGroup(0, f"Mapa de calor – {var}")

        if self.radio_thiessen.isChecked():
            zonas = G.zonas_thiessen(puntos, area, f"Zonas – {var}")
            if categorica:
                G.estilo_categorias_vector(zonas, columna, categorias, colores)
            else:
                G.estilo_rangos_vector(zonas, var, limites, colores, etiquetas, minimo, maximo)
            if carpeta:
                zonas = self._guardar_vector(zonas, os.path.join(destino, f"zonas_{base}.gpkg")) or zonas
                if categorica:
                    G.estilo_categorias_vector(zonas, columna, categorias, colores)
                else:
                    G.estilo_rangos_vector(zonas, var, limites, colores, etiquetas, minimo, maximo)
            QgsProject.instance().addMapLayer(zonas, False)
            grupo.addLayer(zonas)
            resultado = f"{zonas.featureCount()} zonas"
        else:
            ruta = G.superficie_idw(puntos, var, valores, area,
                                    os.path.join(destino, f"superficie_{base}.tif"))
            capa = QgsRasterLayer(ruta, f"Superficie – {var}")
            if categorica:
                G.estilo_raster_clases(capa, [k + 0.5 for k in range(1, n)] + [float("inf")],
                                       colores, categorias)
            else:
                G.estilo_raster_clases(capa, limites + [float("inf")], colores, etiquetas)
            QgsProject.instance().addMapLayer(capa, False)
            grupo.addLayer(capa)
            resultado = "superficie interpolada"

        if carpeta:
            puntos = self._guardar_vector(puntos, os.path.join(destino, f"puntos_{base}.gpkg")) or puntos
        G.estilo_puntos(puntos)
        QgsProject.instance().addMapLayer(puntos, False)
        grupo.insertLayer(0, puntos)

        try:
            extension = area.boundingBox()
            extension.scale(1.1)
            canvas = self.iface.mapCanvas()
            if canvas.mapSettings().destinationCrs() == crs:
                canvas.setExtent(extension)
            canvas.refresh()
        except Exception:
            logger.debug("No se pudo acercar el mapa", exc_info=True)

        aviso = f" ({sin} filas sin coordenadas no se usaron)" if sin else ""
        self.lbl_estado.setText(
            f"✅ Listo: {puntos.featureCount()} puntos{aviso}, {resultado}, {n} rangos."
            + (f"<br>Guardado en: {destino}" if carpeta else ""))

    def _guardar_vector(self, capa, ruta):
        """Guarda la capa en GeoPackage y devuelve la versión en disco (o None)."""
        from qgis.core import QgsVectorLayer
        opciones = QgsVectorFileWriter.SaveVectorOptions()
        opciones.driverName = "GPKG"
        resultado = QgsVectorFileWriter.writeAsVectorFormatV3(
            capa, ruta, QgsProject.instance().transformContext(), opciones)
        if resultado[0] != QgsVectorFileWriter.NoError:
            logger.warning(f"No se pudo guardar {ruta}: {resultado}")
            return None
        en_disco = QgsVectorLayer(ruta, capa.name(), "ogr")
        return en_disco if en_disco.isValid() else None
