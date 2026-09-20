# -*- coding: utf-8 -*-
from qgis.PyQt.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
                                  QLabel, QLineEdit, QMessageBox)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPalette, QPainter
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import (QgsFeature, QgsGeometry, QgsPointXY,
                       QgsProject, QgsCoordinateTransform, QgsWkbTypes)

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

        self._build_ui()
        self._build_tools()
        self._connect_undo_stack()
        self.layer.startEditing()
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

        for b in (self.btn_add, self.btn_move, self.btn_del, self.btn_id,
                  self.btn_undo, self.btn_redo, self.btn_save):
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

    def _add_palm(self, point, palm_id, linea=None, palma=None):
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs  = self.layer.crs()
        if canvas_crs != layer_crs:
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
            pass
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
        self._cleanup_and_close()

    def _cleanup_and_close(self):
        try:
            self.layer.geometryChanged.disconnect(self._on_geom_changed)
        except TypeError:
            pass
        if self._prev_tool is not None:
            self.canvas.setMapTool(self._prev_tool)
        else:
            self.canvas.unsetMapTool(self.canvas.mapTool())
        try:
            self.iface.mainWindow().statusBar().showMessage(
                "Palmas guardadas correctamente", 3000)
        except Exception:
            pass
        self.closed.emit()
        self.hide()
        self.deleteLater()
