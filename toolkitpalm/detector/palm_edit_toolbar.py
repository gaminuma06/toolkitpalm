# -*- coding: utf-8 -*-
import logging
from qgis.PyQt.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
                                  QLabel, QLineEdit, QMessageBox)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QVariant
from qgis.PyQt.QtGui import QColor, QPalette, QPainter
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import (QgsFeature, QgsGeometry, QgsPointXY, QgsField, QgsVectorLayer,
                       QgsProject, QgsCoordinateTransform, QgsWkbTypes)

# Ciclo del botón "Estado": lo que está para revisar se confirma como viva o
# muerta; viva y muerta se alternan.
_SIGUIENTE_ESTADO = {"revisar": "viva", "viva": "muerta", "muerta": "viva"}

_BTN = """
QPushButton {{
    background-color: {bg};
    color: white;
    border: none;
    padding: 6px 10px;
    border-radius: 4px;
    font-weight: bold;
    font-size: 11px;
    font-family: Lato, 'Open Sans', sans-serif;
}}
QPushButton:hover {{ background-color: {hv}; }}
QPushButton:disabled {{ background-color: #9ca3af; color: #e5e7eb; }}
"""


def _make_btn(text, bg='#005587', hv='#004470'):
    b = QPushButton(text)
    b.setStyleSheet(_BTN.format(bg=bg, hv=hv))
    return b


class _NearestMixin:
    def _find_nearest(self, map_point, tolerance):
        """map_point y tolerance están en el CRS del canvas (map units)."""
        canvas_crs  = self.canvas().mapSettings().destinationCrs()
        layer_crs   = self._layer.crs()
        needs_xform = canvas_crs != layer_crs
        xform = (QgsCoordinateTransform(layer_crs, canvas_crs, QgsProject.instance())
                 if needs_xform else None)

        search = QgsGeometry.fromPointXY(map_point)
        best, best_dist = None, float('inf')
        for f in self._layer.getFeatures():
            geom = QgsGeometry(f.geometry())
            if needs_xform:
                geom.transform(xform)
            d = geom.distance(search)
            if d < tolerance and d < best_dist:
                best_dist = d
                best = QgsFeature(f)
        return best


class AddPalmTool(QgsMapTool):
    point_clicked = pyqtSignal(object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.setCursor(Qt.CrossCursor)

    def canvasPressEvent(self, event):
        self.point_clicked.emit(self.toMapCoordinates(event.pos()))


class DeletePalmTool(_NearestMixin, QgsMapTool):
    def __init__(self, canvas, layer, id_field='id'):
        QgsMapTool.__init__(self, canvas)
        self._layer    = layer
        self._id_field = id_field
        self.setCursor(Qt.ForbiddenCursor)

    def canvasPressEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        tol   = self.canvas().mapUnitsPerPixel() * 15
        feat  = self._find_nearest(point, tol)
        if feat is None:
            return
        reply = QMessageBox.question(
            None, "Eliminar palma",
            f"¿Eliminar esta palma (ID: {feat[self._id_field]})?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self._layer.deleteFeature(feat.id())


class EditIdTool(_NearestMixin, QgsMapTool):
    feature_clicked = pyqtSignal(object)

    def __init__(self, canvas, layer):
        QgsMapTool.__init__(self, canvas)
        self._layer = layer
        self.setCursor(Qt.PointingHandCursor)

    def canvasPressEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        tol   = self.canvas().mapUnitsPerPixel() * 15
        feat  = self._find_nearest(point, tol)
        if feat is not None:
            self.feature_clicked.emit(feat)


class MovePalmTool(_NearestMixin, QgsMapTool):
    _MIN_DRAG_PX = 5  # píxeles mínimos para considerar que hubo arrastre

    def __init__(self, canvas, layer):
        QgsMapTool.__init__(self, canvas)
        self._layer       = layer
        self._moving_feat = None
        self._rubber_band = None
        self._press_pos   = None
        self.setCursor(Qt.SizeAllCursor)

    # ── CRS helpers ───────────────────────────────────────────────────────────

    def _map_to_layer(self, point):
        src = self.canvas().mapSettings().destinationCrs()
        dst = self._layer.crs()
        if src == dst:
            return point
        return QgsCoordinateTransform(src, dst, QgsProject.instance()).transform(point)

    def _layer_to_map(self, point):
        src = self._layer.crs()
        dst = self.canvas().mapSettings().destinationCrs()
        if src == dst:
            return point
        return QgsCoordinateTransform(src, dst, QgsProject.instance()).transform(point)

    # ── Events ────────────────────────────────────────────────────────────────

    def canvasPressEvent(self, event):
        map_point = self.toMapCoordinates(event.pos())
        tol       = self.canvas().mapUnitsPerPixel() * 15
        feat      = self._find_nearest(map_point, tol)
        if feat is None:
            return
        self._moving_feat = feat
        self._press_pos   = event.pos()

        self._rubber_band = QgsRubberBand(self.canvas(), QgsWkbTypes.PointGeometry)
        self._rubber_band.setColor(QColor('#00A9E0'))
        self._rubber_band.setIconSize(14)
        self._rubber_band.setWidth(3)
        map_pt = self._layer_to_map(feat.geometry().asPoint())
        self._rubber_band.addPoint(map_pt)

    def canvasMoveEvent(self, event):
        if self._moving_feat is None or self._rubber_band is None:
            return
        self._rubber_band.reset(QgsWkbTypes.PointGeometry)
        self._rubber_band.addPoint(self.toMapCoordinates(event.pos()))

    def canvasReleaseEvent(self, event):
        if self._moving_feat is None:
            return

        # Cancelar si no hubo arrastre real (evita mover la palma con un simple clic)
        if self._press_pos is not None:
            d = event.pos() - self._press_pos
            if d.x() ** 2 + d.y() ** 2 < self._MIN_DRAG_PX ** 2:
                self._cancel()
                return

        layer_pt = self._map_to_layer(self.toMapCoordinates(event.pos()))
        self._layer.changeGeometry(
            self._moving_feat.id(),
            QgsGeometry.fromPointXY(layer_pt)
        )
        self.canvas().refresh()
        self._cancel()

    def _cancel(self):
        if self._rubber_band:
            self._rubber_band.reset()
            self._rubber_band = None
        self._moving_feat = None
        self._press_pos   = None


class PalmEditToolbar(QWidget):
    closed = pyqtSignal()

    _ACTIVE = _BTN.format(bg='#00A9E0', hv='#0095c7')
    _NORMAL = _BTN.format(bg='#005587', hv='#004470')
    _GREEN  = _BTN.format(bg='#059669', hv='#047857')

    def __init__(self, canvas, layer, iface):
        super().__init__(canvas)
        self.canvas = canvas
        self.layer  = layer
        self.iface  = iface
        self._prev_tool     = canvas.mapTool()
        self._pending_point = None
        self._pending_feat  = None
        self._active_btn    = None

        # Resolver nombres reales de campos (pueden ser mayúsculas en capas numeradas)
        _fmap = {f.name().lower(): f.name() for f in layer.fields()}
        self._id_field = _fmap.get('id', 'id')
        self._x_field  = _fmap.get('x', 'x')
        self._y_field  = _fmap.get('y', 'y')

        self._numbered_mode = (
            layer.fields().indexOf('Linea') >= 0 and
            layer.fields().indexOf('Palma') >= 0
        )

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)

        if self._numbered_mode:
            self._asegurar_campos_estado()

        self._build_ui()
        self._build_tools()
        self._connect_undo_stack()
        self.layer.startEditing()
        if self._numbered_mode:
            self._completar_estados_vacios()
        self._reposition()
        self.show()
        self.raise_()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(6)
        lbl = QLabel("✏ Edición de Palmas")
        lbl.setStyleSheet(
            "color: #1A1A1A; font-weight: bold; font-size: 11px;"
            " font-family: Lato, 'Open Sans', sans-serif;")
        top.addWidget(lbl)
        top.addSpacing(8)

        self.btn_add  = _make_btn("+ Agregar")
        self.btn_move = _make_btn("↔ Mover")
        self.btn_del  = _make_btn("✕ Eliminar")
        self.btn_id   = _make_btn("🔢 Editar" if self._numbered_mode else "🔢 Editar ID")
        self.btn_undo = _make_btn("↩ Deshacer")
        self.btn_redo = _make_btn("↪ Rehacer")
        self.btn_save = _make_btn("✓ Guardar y salir", bg='#059669', hv='#047857')

        botones = [self.btn_add, self.btn_move, self.btn_del, self.btn_id]
        self.btn_dead = self.btn_state = None
        if self._numbered_mode:
            # Palmas muertas: se agregan con un clic y, al guardar, se renumera
            # cada línea para que la secuencia quede corrida y sin huecos.
            self.btn_dead = _make_btn("☠ Muerta", bg='#b91c1c', hv='#991b1b')
            self.btn_dead.setToolTip("Clic en el mapa para agregar una palma muerta (roja).")
            self.btn_state = _make_btn("⇄ Estado")
            self.btn_state.setToolTip(
                "Clic sobre una palma para cambiar su estado: "
                "para revisar → viva → muerta → viva.")
            botones += [self.btn_dead, self.btn_state]
        self.btn_fill = None
        if self._numbered_mode and self._capa_huecos() is not None:
            self.btn_fill = _make_btn("▭ Rellenar hueco", bg='#c2410c', hv='#9a3412')
            self.btn_fill.setToolTip(
                "Clic sobre una línea naranja punteada (hueco largo) para llenarlo "
                "de palmas, eligiendo si son muertas o vivas.")
            botones.append(self.btn_fill)
        for b in botones + [self.btn_undo, self.btn_redo, self.btn_save]:
            top.addWidget(b)
        root.addLayout(top)

        # Inline row (hidden until a tool needs it)
        self._id_row = QWidget()
        rl = QHBoxLayout(self._id_row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)

        self._id_label = QLabel("ID:")
        self._id_label.setStyleSheet("color: #1A1A1A; font-size: 11px;")
        self._id_input = QLineEdit()
        self._id_input.setFixedWidth(60)
        self._id_input.setStyleSheet(
            "background: white; border: 2px solid #00A9E0;"
            " border-radius: 3px; padding: 3px; font-size: 11px;")
        rl.addWidget(self._id_label)
        rl.addWidget(self._id_input)

        if self._numbered_mode:
            _linea_lbl = QLabel("Línea:")
            _linea_lbl.setStyleSheet("color: #1A1A1A; font-size: 11px;")
            self._linea_input = QLineEdit()
            self._linea_input.setFixedWidth(60)
            self._linea_input.setStyleSheet(
                "background: white; border: 2px solid #00A9E0;"
                " border-radius: 3px; padding: 3px; font-size: 11px;")
            _palma_lbl = QLabel("Palma:")
            _palma_lbl.setStyleSheet("color: #1A1A1A; font-size: 11px;")
            self._palma_input = QLineEdit()
            self._palma_input.setFixedWidth(60)
            self._palma_input.setStyleSheet(
                "background: white; border: 2px solid #00A9E0;"
                " border-radius: 3px; padding: 3px; font-size: 11px;")
            rl.addWidget(_linea_lbl)
            rl.addWidget(self._linea_input)
            rl.addWidget(_palma_lbl)
            rl.addWidget(self._palma_input)
        else:
            self._linea_input = None
            self._palma_input = None

        self._btn_confirm = _make_btn("✓ Confirmar", bg='#00A9E0', hv='#0095c7')
        rl.addWidget(self._btn_confirm)
        self._id_row.hide()
        root.addWidget(self._id_row)

        self.adjustSize()

        self.btn_add.clicked.connect(self._on_add)
        self.btn_move.clicked.connect(self._on_move)
        self.btn_del.clicked.connect(self._on_delete)
        self.btn_id.clicked.connect(self._on_edit_id)
        if self.btn_dead is not None:
            self.btn_dead.clicked.connect(self._on_dead)
            self.btn_state.clicked.connect(self._on_state)
        if self.btn_fill is not None:
            self.btn_fill.clicked.connect(self._on_fill)
        self.btn_undo.clicked.connect(lambda: self.layer.undoStack().undo())
        self.btn_redo.clicked.connect(lambda: self.layer.undoStack().redo())
        self.btn_save.clicked.connect(self._save)
        self._btn_confirm.clicked.connect(self._on_confirm)
        self._id_input.returnPressed.connect(self._on_confirm)

    def _build_tools(self):
        self._tool_add  = AddPalmTool(self.canvas)
        self._tool_move = MovePalmTool(self.canvas, self.layer)
        self._tool_del  = DeletePalmTool(self.canvas, self.layer, self._id_field)
        self._tool_edit = EditIdTool(self.canvas, self.layer)

        self._tool_add.point_clicked.connect(self._on_point_selected)
        self._tool_edit.feature_clicked.connect(self._on_feature_for_edit)

        self._tool_dead = AddPalmTool(self.canvas)
        self._tool_dead.point_clicked.connect(self._on_dead_point)
        self._tool_state = EditIdTool(self.canvas, self.layer)
        self._tool_state.feature_clicked.connect(self._on_toggle_state)

        huecos = self._capa_huecos()
        self._tool_fill = EditIdTool(self.canvas, huecos) if huecos is not None else None
        if self._tool_fill is not None:
            self._tool_fill.feature_clicked.connect(self._on_fill_gap)

    def _connect_undo_stack(self):
        stack = self.layer.undoStack()
        self.btn_undo.setEnabled(stack.canUndo())
        self.btn_redo.setEnabled(stack.canRedo())
        stack.canUndoChanged.connect(self.btn_undo.setEnabled)
        stack.canRedoChanged.connect(self.btn_redo.setEnabled)

    # ── Background (paintEvent es el único método confiable para widgets hijos del canvas) ──

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#e8e8e8'))
        p.setPen(QColor('#b0b0b0'))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    # ── Position ──────────────────────────────────────────────────────────────

    def _reposition(self):
        self.adjustSize()
        self.move(10, 10)

    # ── Tool activation ───────────────────────────────────────────────────────

    def _set_active(self, btn):
        if self._active_btn:
            style = self._GREEN if self._active_btn is self.btn_save else self._NORMAL
            self._active_btn.setStyleSheet(style)
        self._active_btn = btn
        if btn:
            btn.setStyleSheet(self._ACTIVE)

    def _on_add(self):
        self._hide_id_row()
        self._set_active(self.btn_add)
        self.canvas.setMapTool(self._tool_add)

    def _on_move(self):
        self._hide_id_row()
        self._set_active(self.btn_move)
        self.canvas.setMapTool(self._tool_move)
        self._connect_geom_changed()

    def _on_delete(self):
        self._hide_id_row()
        self._set_active(self.btn_del)
        self.canvas.setMapTool(self._tool_del)

    def _on_edit_id(self):
        self._hide_id_row()
        self._set_active(self.btn_id)
        self.canvas.setMapTool(self._tool_edit)

    def _on_dead(self):
        self._hide_id_row()
        self._set_active(self.btn_dead)
        self.canvas.setMapTool(self._tool_dead)

    def _on_state(self):
        self._hide_id_row()
        self._set_active(self.btn_state)
        self.canvas.setMapTool(self._tool_state)

    def _on_fill(self):
        self._hide_id_row()
        self._set_active(self.btn_fill)
        self.canvas.setMapTool(self._tool_fill)

    # ── Rellenar hueco largo ─────────────────────────────────────────────────

    def _capa_huecos(self):
        nombre = self.layer.name()
        if not nombre.startswith("PalmasNumeradas_"):
            return None
        buscado = "HuecosRevisar_" + nombre[len("PalmasNumeradas_"):]
        for capa in QgsProject.instance().mapLayers().values():
            if isinstance(capa, QgsVectorLayer) and capa.name() == buscado:
                return capa
        return None

    def _paso_de_linea(self, linea):
        """Distancia típica entre palmas consecutivas de una línea."""
        # Solo las ya numeradas: las recién agregadas aún no tienen número
        # (Palma = 0) y no indican el orden de la fila.
        puntos = sorted(((f['Palma'], f.geometry().asPoint())
                         for f in self.layer.getFeatures()
                         if f['Linea'] == linea and f['Palma']),
                        key=lambda t: t[0])
        distancias = sorted(a[1].distance(b[1]) for a, b in zip(puntos, puntos[1:]))
        return distancias[len(distancias) // 2] if distancias else None

    def _extremos_hueco(self, hueco):
        """Inicio y fin del hueco en el sistema de la capa de palmas."""
        geom = QgsGeometry(hueco.geometry())
        if geom.isMultipart():
            vertices = [p for parte in geom.asMultiPolyline() for p in parte]
        else:
            vertices = geom.asPolyline()
        if len(vertices) < 2:
            return None
        huecos = self._capa_huecos()
        if huecos is not None and huecos.crs() != self.layer.crs():
            xf = QgsCoordinateTransform(huecos.crs(), self.layer.crs(), QgsProject.instance())
            vertices = [xf.transform(p) for p in vertices]
        return vertices[0], vertices[-1]

    def _hueco_relleno(self, hueco):
        """True si hoy hay palmas dentro del hueco (sin contar las de sus extremos).

        Se mira la capa tal como está, y no una anotación aparte: así deshacer
        el relleno deja el hueco pendiente otra vez."""
        extremos = self._extremos_hueco(hueco)
        linea = hueco['Linea']
        if extremos is None or not linea:
            return False
        inicio, fin = extremos
        tramo = QgsGeometry.fromPolylineXY([inicio, fin])
        paso = self._paso_de_linea(linea) or 9.0
        for f in self.layer.getFeatures():
            if f['Linea'] != linea:
                continue
            p = f.geometry().asPoint()
            if (tramo.distance(QgsGeometry.fromPointXY(p)) < 1.5
                    and p.distance(inicio) > 0.25 * paso and p.distance(fin) > 0.25 * paso):
                return True
        return False

    def _on_fill_gap(self, hueco):
        if self._hueco_relleno(hueco):
            QMessageBox.information(self, "Rellenar hueco", "Ese hueco ya tiene palmas.")
            return
        linea = hueco['Linea']
        extremos = self._extremos_hueco(hueco)
        if extremos is None or not linea:
            return
        inicio, fin = extremos
        largo = inicio.distance(fin)
        paso = self._paso_de_linea(linea)
        if not paso or largo < paso * 0.5:
            return

        # Hueco entre dos palmas: posiciones repartidas en partes iguales. Hueco
        # hacia el borde: una posición cada paso, desde la última palma.
        def hay_palma(p):
            return any(f.geometry().asPoint().distance(p) < 1.5
                       for f in self.layer.getFeatures() if f['Linea'] == linea)

        if hay_palma(inicio) and hay_palma(fin):
            n = max(1, int(round(largo / paso)) - 1)
            fracciones = [j / float(n + 1) for j in range(1, n + 1)]
        else:
            n = int(largo / paso + 0.1)
            fracciones = [j * paso / largo for j in range(1, n + 1)]
        if n < 1:
            return

        dialogo = QMessageBox(self)
        dialogo.setWindowTitle("Rellenar hueco")
        dialogo.setText(f"En este hueco de la línea {linea} caben {n} palmas.\n\n"
                        "¿Qué es este hueco?")
        b_muertas = dialogo.addButton("Muertas (erradicadas)", QMessageBox.AcceptRole)
        b_vivas = dialogo.addButton("Vivas", QMessageBox.AcceptRole)
        b_quitar = dialogo.addButton("No son palmas: quitar hueco", QMessageBox.DestructiveRole)
        dialogo.addButton("Cancelar", QMessageBox.RejectRole)
        dialogo.exec_()
        elegido = dialogo.clickedButton()
        if elegido is b_quitar:
            self._quitar_hueco(hueco)
            return
        if elegido not in (b_muertas, b_vivas):
            return
        estado = "muerta" if elegido is b_muertas else "viva"

        siguiente = self._next_id()
        for k, f in enumerate(fracciones):
            punto = QgsPointXY(inicio.x() + (fin.x() - inicio.x()) * f,
                               inicio.y() + (fin.y() - inicio.y()) * f)
            self._add_palm(punto, siguiente + k, linea, 0, estado=estado,
                           origen="manual", en_capa=True)

    # ── Palmas muertas ────────────────────────────────────────────────────────

    def _asegurar_campos_estado(self):
        """Las capas numeradas antes de existir las palmas muertas no traen
        Estado ni Origen: se agregan al abrir la edición."""
        faltan = [n for n in ("Estado", "Origen") if self.layer.fields().indexOf(n) < 0]
        if not faltan:
            return
        self.layer.dataProvider().addAttributes(
            [QgsField(n, QVariant.String, len=10) for n in faltan])
        self.layer.updateFields()

    def _completar_estados_vacios(self):
        i_est = self.layer.fields().indexOf("Estado")
        i_ori = self.layer.fields().indexOf("Origen")
        for f in self.layer.getFeatures():
            if not f["Estado"]:
                self.layer.changeAttributeValue(f.id(), i_est, "viva")
            if not f["Origen"]:
                self.layer.changeAttributeValue(f.id(), i_ori, "detectada")

    def _on_dead_point(self, point):
        # Línea y palma se calculan al guardar (ver _renumerar).
        self._add_palm(point, self._next_id(), 0, 0, estado="muerta", origen="manual")

    def _on_toggle_state(self, feat):
        actual = feat["Estado"] or "viva"
        self.layer.changeAttributeValue(
            feat.id(), self.layer.fields().indexOf("Estado"),
            _SIGUIENTE_ESTADO.get(actual, "viva"))
        self.canvas.refresh()

    # ── Add palm flow ─────────────────────────────────────────────────────────

    def _next_id(self):
        ids = [f[self._id_field] for f in self.layer.getFeatures() if f[self._id_field] is not None]
        return (max(ids) + 1) if ids else 1

    def _on_point_selected(self, point):
        self._pending_point = point
        self._pending_feat  = None
        self._id_label.setText("ID nueva palma:")
        self._id_input.setText(str(self._next_id()))
        if self._numbered_mode:
            self._linea_input.setText('0')
            self._palma_input.setText('0')
        self._id_input.selectAll()
        self._id_row.show()
        self.adjustSize()
        self._id_input.setFocus()

    # ── Edit ID flow ──────────────────────────────────────────────────────────

    def _on_feature_for_edit(self, feat):
        self._pending_feat  = feat
        self._pending_point = None
        self._id_label.setText(f"ID ({feat[self._id_field]}):")
        self._id_input.setText(str(feat[self._id_field]) if feat[self._id_field] is not None else '')
        if self._numbered_mode:
            self._linea_input.setText(str(feat['Linea']) if feat['Linea'] is not None else '0')
            self._palma_input.setText(str(feat['Palma']) if feat['Palma'] is not None else '0')
        self._id_input.selectAll()
        self._id_row.show()
        self.adjustSize()
        self._id_input.setFocus()

    # ── Confirm ───────────────────────────────────────────────────────────────

    def _on_confirm(self):
        try:
            new_id = int(self._id_input.text())
            if self._numbered_mode:
                new_linea = int(self._linea_input.text())
                new_palma = int(self._palma_input.text())
        except ValueError:
            return

        if self._pending_point is not None:
            if self._numbered_mode:
                self._add_palm(self._pending_point, new_id, new_linea, new_palma)
            else:
                self._add_palm(self._pending_point, new_id)
            self._pending_point = None
        elif self._pending_feat is not None:
            fields = self.layer.fields()
            self.layer.changeAttributeValue(
                self._pending_feat.id(), fields.indexOf(self._id_field), new_id)
            if self._numbered_mode:
                self.layer.changeAttributeValue(
                    self._pending_feat.id(), fields.indexOf('Linea'), new_linea)
                self.layer.changeAttributeValue(
                    self._pending_feat.id(), fields.indexOf('Palma'), new_palma)
            self.canvas.refresh()
            self._pending_feat = None

        self._hide_id_row()

    def _add_palm(self, point, palm_id, linea=None, palma=None, estado="viva", origen="manual",
                  en_capa=False):
        """Agrega una palma. `point` viene en el sistema del mapa, salvo que
        `en_capa` indique que ya está en el de la capa."""
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs  = self.layer.crs()
        if not en_capa and canvas_crs != layer_crs:
            point = QgsCoordinateTransform(
                canvas_crs, layer_crs, QgsProject.instance()).transform(point)

        fields = self.layer.fields()
        feat = QgsFeature(fields)
        feat.setGeometry(QgsGeometry.fromPointXY(point))
        feat[self._id_field] = palm_id
        # Los campos x/y son opcionales: las capas de detección propias no los
        # traen (solo id/confianza/Lote/tipo), solo se completan si existen.
        if fields.indexOf(self._x_field) >= 0:
            feat[self._x_field] = point.x()
        if fields.indexOf(self._y_field) >= 0:
            feat[self._y_field] = point.y()
        if self._numbered_mode and linea is not None:
            feat['Linea'] = linea
        if self._numbered_mode and palma is not None:
            feat['Palma'] = palma
        if fields.indexOf('Estado') >= 0:
            feat['Estado'] = estado
        if fields.indexOf('Origen') >= 0:
            feat['Origen'] = origen
        self.layer.addFeature(feat)
        self.canvas.refresh()

    def _hide_id_row(self):
        self._id_row.hide()
        self.adjustSize()

    # ── Geometry changed → update x, y ───────────────────────────────────────

    def _connect_geom_changed(self):
        try:
            self.layer.geometryChanged.disconnect(self._on_geom_changed)
        except TypeError:
            logging.getLogger(__name__).debug(
                "Fallo no crítico; se continúa.", exc_info=True)
        self.layer.geometryChanged.connect(self._on_geom_changed)

    def _on_geom_changed(self, fid, geometry):
        pt    = geometry.asPoint()
        idx_x = self.layer.fields().indexOf(self._x_field)
        idx_y = self.layer.fields().indexOf(self._y_field)
        if idx_x >= 0:
            self.layer.changeAttributeValue(fid, idx_x, pt.x())
        if idx_y >= 0:
            self.layer.changeAttributeValue(fid, idx_y, pt.y())

    # ── Save & exit ───────────────────────────────────────────────────────────

    def _save(self):
        if not self.layer.commitChanges():
            QMessageBox.critical(
                self, "Error al guardar",
                "No se pudieron guardar los cambios en el shapefile.\n"
                "Revise que el archivo no esté bloqueado por otra aplicación.")
            return
        if self._numbered_mode:
            try:
                self._quitar_huecos_rellenados()
                self._renumerar()
            except Exception as e:
                logging.getLogger(__name__).warning("No se pudo renumerar", exc_info=True)
                QMessageBox.warning(
                    self, "Renumeración incompleta",
                    f"Los cambios se guardaron, pero no se pudo renumerar:\n{e}")
        self._cleanup_and_close()

    def _quitar_hueco(self, hueco):
        """Quita la línea naranja de un hueco que no corresponde a palmas (un dren,
        una vía…). Se pide confirmación porque no entra en Deshacer: Deshacer
        trabaja sobre las palmas, y el hueco es otra capa."""
        respuesta = QMessageBox.question(
            self, "Quitar hueco",
            f"¿Quitar el hueco de la línea {hueco['Linea']}?\n\n"
            "Se quita ahora y no se puede deshacer con el botón Deshacer. "
            "Si se quita por error, se recupera numerando de nuevo el lote.",
            QMessageBox.Yes | QMessageBox.No)
        if respuesta != QMessageBox.Yes:
            return
        huecos = self._capa_huecos()
        if huecos is None:
            return
        huecos.startEditing()
        huecos.deleteFeature(hueco.id())
        if not huecos.commitChanges():
            QMessageBox.warning(self, "Quitar hueco", "No se pudo quitar el hueco.")
            return
        huecos.triggerRepaint()
        self.canvas.refresh()

    def _quitar_huecos_rellenados(self):
        """Un hueco que quedó con palmas deja de estar pendiente: se quita su
        línea naranja. Se decide con las palmas guardadas, así que un relleno
        deshecho no borra el hueco."""
        huecos = self._capa_huecos()
        if huecos is None:
            return
        rellenos = [h.id() for h in huecos.getFeatures() if self._hueco_relleno(h)]
        if not rellenos:
            return
        huecos.startEditing()
        for fid in rellenos:
            huecos.deleteFeature(fid)
        huecos.commitChanges()
        huecos.triggerRepaint()

    # ── Renumerar después de editar ──────────────────────────────────────────

    def _capa_de_lineas(self):
        nombre = self.layer.name()
        if not nombre.startswith("PalmasNumeradas_"):
            return None
        buscado = "LineasLote_" + nombre[len("PalmasNumeradas_"):]
        for capa in QgsProject.instance().mapLayers().values():
            if isinstance(capa, QgsVectorLayer) and capa.name() == buscado:
                return capa
        return None

    def _renumerar(self):
        """Ordena cada línea, numera las palmas desde 1 sin huecos, asigna a su
        línea las palmas nuevas y redibuja las líneas pasando por todas."""
        from .numeracion_filas import renumerar, linea_de_fila
        from .estilos import simbolos_por_estado

        feats = {f.id(): f for f in self.layer.getFeatures()}
        puntos = []
        for fid, f in feats.items():
            p = f.geometry().asPoint()
            puntos.append((fid, p.x(), p.y(), f['Linea'] or 0, f['Palma'] or 0))
        asignacion, filas = renumerar(puntos)

        campos = self.layer.fields()
        i_lin, i_pal = campos.indexOf('Linea'), campos.indexOf('Palma')
        i_id = campos.indexOf(self._id_field)
        self.layer.startEditing()
        nuevo_id = 1
        for linea in sorted(filas):
            for fid in filas[linea]:
                l, p = asignacion[fid]
                self.layer.changeAttributeValues(fid, {i_lin: l, i_pal: p, i_id: nuevo_id})
                nuevo_id += 1
        if not self.layer.commitChanges():
            raise RuntimeError("no se pudieron escribir los números nuevos")

        lineas = self._capa_de_lineas()
        if lineas is not None:
            faltan = [n for n in ("Vivas", "Muertas", "Revisar")
                      if lineas.fields().indexOf(n) < 0]
            if faltan:
                lineas.dataProvider().addAttributes([QgsField(n, QVariant.Int) for n in faltan])
                lineas.updateFields()
            lote = next((f['Lote'] for f in feats.values() if f['Lote']), "")
            lineas.startEditing()
            for f in list(lineas.getFeatures()):
                lineas.deleteFeature(f.id())
            for linea in sorted(filas):
                coords = [feats[fid].geometry().asPoint() for fid in filas[linea]]
                if len(coords) < 2:
                    continue
                xy = [(c.x(), c.y()) for c in coords]
                ux, uy = xy[-1][0] - xy[0][0], xy[-1][1] - xy[0][1]
                norma = (ux * ux + uy * uy) ** 0.5 or 1.0
                trazo = linea_de_fila(xy, ux / norma, uy / norma)
                estados = [feats[fid]['Estado'] or 'viva' for fid in filas[linea]]
                nueva = QgsFeature(lineas.fields())
                nueva.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in trazo]))
                valores = {'ID': linea, 'Linea': linea, 'Palmas': len(xy), 'Lote': lote,
                           'Vivas': estados.count('viva'), 'Muertas': estados.count('muerta'),
                           'Revisar': estados.count('revisar')}
                for campo, valor in valores.items():
                    if lineas.fields().indexOf(campo) >= 0:
                        nueva[campo] = valor
                lineas.addFeature(nueva)
            if not lineas.commitChanges():
                raise RuntimeError("no se pudieron redibujar las líneas")
            lineas.triggerRepaint()

        simbolos_por_estado(self.layer)
        self.layer.triggerRepaint()
        self.canvas.refresh()

    def _cleanup_and_close(self):
        try:
            self.layer.geometryChanged.disconnect(self._on_geom_changed)
        except TypeError:
            logging.getLogger(__name__).debug(
                "Fallo no crítico; se continúa.", exc_info=True)
        if self._prev_tool is not None:
            self.canvas.setMapTool(self._prev_tool)
        else:
            self.canvas.unsetMapTool(self.canvas.mapTool())
        try:
            self.iface.mainWindow().statusBar().showMessage(
                "Palmas guardadas correctamente", 3000)
        except Exception:
            logging.getLogger(__name__).debug(
                "Fallo no crítico; se continúa.", exc_info=True)
        self.closed.emit()
        self.hide()
        self.deleteLater()
