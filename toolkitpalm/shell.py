# -*- coding: utf-8 -*-
"""
Panel único de ToolkitPalm: una barra lateral con buscador y 4 "tarjetas"
(Detector, Segmentador, Optimizador, Asistente LLM/MCP) que funcionan como
pestañas — al hacer clic en una, el área de contenido muestra esa herramienta.

Los 3 dockwidgets reales (detector/segmentador/optimizador) no cambian: aquí
solo se instancian de forma perezosa y se embeben como página de un
QStackedWidget en vez de agregarse como dock flotante propio.
"""

import os
import base64
import html
import mimetypes
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QSettings, QTimer, QUrl, QThread, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QPushButton, QButtonGroup, QStackedWidget, QLabel, QFrame, QSizePolicy,
    QComboBox, QTextBrowser, QFormLayout, QApplication, QFileDialog,
    QSpinBox, QCheckBox, QInputDialog, QDialog, QMessageBox, QScrollArea,
)

from .common.colors import AZUL_OSCURO, AZUL_MEDIO, AZUL_CLARO, BEIGE_ARENA
from .config import (
    ASSISTANT_DEFAULT_PROVIDER, ASSISTANT_CLAUDE_MODEL, ANTHROPIC_API_KEY,
    ASSISTANT_CUSTOM_MODEL, ASSISTANT_CUSTOM_BASE_URL, ASSISTANT_CUSTOM_API_KEY,
    CREDITS_PURCHASE_URL,
)
from . import client_identity

_ASSISTANT_SETTINGS_PREFIX = "toolkitpalm/assistant"


class _CreditsFetcher(QThread):
    """Consulta el saldo de créditos fuera del hilo de la interfaz.

    La consulta va por red y puede tardar segundos (o quedarse esperando si el
    servidor está frío): hacerla en el hilo principal congelaba QGIS entero cada
    vez que se refrescaba la barra de título.
    """

    resultado = pyqtSignal(object)  # dict con el saldo, o None si falló

    def run(self):
        try:
            from . import client_identity
            self.resultado.emit(client_identity.get_credit_balance())
        except Exception:
            self.resultado.emit(None)


class _PagoWatcher(QThread):
    """Espera la confirmación de un pago recién iniciado, en segundo plano.

    El plugin solo abre el checkout en el navegador: desde ahí no se entera de
    si el pago se aprobó, lo rechazaron o el usuario cerró la ventana. Quien
    acredita los créditos es el servidor, cuando Wompi le avisa por su webhook,
    así que la única señal confiable desde acá es ver subir el saldo. Por eso se
    consulta cada pocos segundos durante un rato y se avisa apenas cambie —
    antes de esto el usuario se quedaba sin ningún aviso, pasara lo que pasara.
    """

    acreditado = pyqtSignal(int)   # saldo nuevo
    sin_confirmar = pyqtSignal()

    INTERVALO_SEG = 8
    ESPERA_MAXIMA_SEG = 15 * 60

    def __init__(self, saldo_inicial=None, parent=None):
        super().__init__(parent)
        self._saldo_inicial = saldo_inicial
        self._cancelado = False

    def cancelar(self):
        self._cancelado = True

    def _dormir(self, segundos):
        """Duerme en tramos de 1 s para poder cancelar sin dejar colgado a QGIS."""
        for _ in range(segundos):
            if self._cancelado:
                return False
            self.msleep(1000)
        return not self._cancelado

    def run(self):
        from . import client_identity

        transcurrido = 0
        while transcurrido < self.ESPERA_MAXIMA_SEG:
            if not self._dormir(self.INTERVALO_SEG):
                return
            transcurrido += self.INTERVALO_SEG
            try:
                saldo = client_identity.get_credit_balance(timeout=20)
            except Exception:
                saldo = None
            actual = (saldo or {}).get("creditos_saldo")
            if not isinstance(actual, int):
                continue
            if self._saldo_inicial is None:
                # No se conocía el saldo previo: la primera lectura buena
                # sirve de punto de partida.
                self._saldo_inicial = actual
                continue
            if actual > self._saldo_inicial:
                self.acreditado.emit(actual)
                return
        if not self._cancelado:
            self.sin_confirmar.emit()

# Modelos válidos de Claude para el selector (nombre visible, id real de la API).
# Se eligió una lista fija (en vez de un campo de texto libre) para que el
# usuario no pueda escribir mal el nombre del modelo.
_CLAUDE_MODEL_CHOICES = [
    ("Sonnet 5 (recomendado: buen balance costo/calidad)", "claude-sonnet-5"),
    ("Opus 5 (el más potente, más costoso)", "claude-opus-5"),
    ("Haiku 4.5 (el más rápido y económico)", "claude-haiku-4-5"),
]

_CARD_STYLE = """
QPushButton {{
    text-align: left;
    padding: 10px 12px;
    border: none;
    border-radius: 0px;
    background-color: transparent;
    color: {fg};
    font-size: 12px;
}}
QPushButton:hover {{ background-color: {hover}; }}
QPushButton:checked {{
    background-color: {AZUL_MEDIO};
    color: white;
    font-weight: bold;
}}
""".format(fg=AZUL_OSCURO, hover=BEIGE_ARENA, AZUL_MEDIO=AZUL_MEDIO)

# Tarjeta en gris para herramientas que todavía no se van a desarrollar: no
# reacciona al pasar el mouse ni se puede marcar como seleccionada (el botón
# queda deshabilitado desde _build_sidebar).
_CARD_STYLE_DISABLED = """
QPushButton {
    text-align: left;
    padding: 10px 12px;
    border: none;
    border-radius: 0px;
    background-color: transparent;
    color: #9AA0A6;
    font-size: 12px;
}
"""

# key -> (etiqueta de la tarjeta, texto usado por el filtro de búsqueda)
_TOOLS = [
    ("detector", "📍 Detector de Palmas"),
    ("segmentador", "🌿 Segmentador de Palmas"),
    ("diseno_plantacion", "🌴 Diseño de Plantación"),
    ("llm", "🤖 Asistente (LLM · MCP)"),
    ("optimizador", "🚚 Optimizador de Acopios"),
    ("fotogrametria", "🛩️ Fotogrametría (próximamente)"),
]

# Herramientas visibles pero deshabilitadas por ahora: quedan al final de la
# lista, en gris, sin poder abrirse. Se retoman más adelante.
_DISABLED_TOOLS = {"optimizador", "fotogrametria"}

# Herramientas que se pueden usar (o se usarán) pero todavía están en desarrollo:
# se marcan en su bloque de la lista para que quede claro antes de entrar.
_TOOLS_EN_CONSTRUCCION = {"diseno_plantacion", "optimizador", "fotogrametria"}


class ToolkitPalmDockWidget(QDockWidget):
    """Único dockwidget del plugin: sidebar de navegación + contenido apilado."""

    _COLLAPSED_WIDTH = 32

    def __init__(self, iface, parent=None):
        super().__init__("ToolkitPalm", parent)
        self.iface = iface
        self.setObjectName("ToolkitPalmDockWidget")
        self._collapsed = False
        self._expanded_width = None
        self._apply_custom_title_bar()

        self._pages = {}       # key -> widget ya instanciado
        self._buttons = {}     # key -> QPushButton (tarjeta)
        self._current_key = None

        self._full_body = QWidget()
        cuerpo = QVBoxLayout(self._full_body)
        cuerpo.setContentsMargins(0, 0, 0, 0)
        cuerpo.setSpacing(0)

        contenido = QWidget()
        layout = QHBoxLayout(contenido)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())

        self._stacked = QStackedWidget()
        layout.addWidget(self._stacked, 1)

        cuerpo.addWidget(contenido, 1)

        # Capa que cubre el panel mientras no haya sesión iniciada. Va como hija
        # del cuerpo (no dentro del layout) para poder taparlo completo y, de
        # paso, interceptar los clics: así las herramientas quedan a la vista
        # pero no se pueden usar sin iniciar sesión.
        self._overlay_login = self._build_overlay_login()
        self._full_body.installEventFilter(self)
        # La barra de título se construye antes que el cuerpo, así que su llamada
        # a _refresh_login_ui no alcanzó a ver esta capa: se sincroniza ahora.
        QTimer.singleShot(0, self._sincronizar_overlay_login)

        self._empty_page = self._build_empty_page()
        self._stacked.addWidget(self._empty_page)

        self._llm_page = self._build_llm_page()
        self._stacked.addWidget(self._llm_page)

        self._collapsed_strip = self._build_collapsed_strip()

        # El dock lleva directamente el cuerpo completo. Al contraer se le pone
        # la franja en su lugar (ver _on_toggle_collapse): con un QStackedWidget
        # que contuviera las dos, el ancho mínimo seguiría siendo el de la página
        # más grande y el panel no lograba encogerse.
        self.setWidget(self._full_body)

    def closeEvent(self, event):
        """Apaga la conexión externa (MCP) si estaba activa, para no dejar un
        socket abierto huérfano cuando se cierra el panel o se descarga el plugin."""
        if getattr(self, "_mcp_server", None) is not None:
            self._mcp_server.stop()
        # El vigilante de pagos duerme entre consultas: si no se le avisa, la
        # recarga del plugin se queda esperando a que termine su ciclo.
        vigilante = getattr(self, "_pago_watcher", None)
        if vigilante is not None and vigilante.isRunning():
            vigilante.cancelar()
        super().closeEvent(event)

    def dispose_pages(self):
        """Destruye las herramientas que se hayan instanciado.

        Cada página es el contenido de un dockwidget propio que se mantiene vivo
        por la referencia _owner_dock. Cerrar el panel no destruye nada: sin esta
        limpieza, cada recarga del plugin deja en memoria el juego completo de la
        vez anterior (llegaron a acumularse diez), con sus señales y sus widgets,
        y cuesta saber cuál es la instancia que el usuario está viendo."""
        for page in self._pages.values():
            owner = getattr(page, "_owner_dock", None)
            if owner is not None:
                page._owner_dock = None
                owner.deleteLater()
            page.setParent(None)
            page.deleteLater()
        self._pages.clear()

    def disconnect_tool_signals(self):
        """Desengancha cada herramienta de las señales globales de QGIS.

        Esas señales viven en QGIS y no se enteran de que el panel se destruyó,
        así que una instancia vieja seguiría respondiendo y reventaría al tocar
        widgets ya borrados. Se llama al descargar el plugin (ver unload), no al
        cerrar el panel: cerrarlo y volverlo a abrir debe seguir funcionando."""
        for page in self._pages.values():
            owner = getattr(page, "_owner_dock", None)
            if owner is not None and hasattr(owner, "disconnect_global_signals"):
                try:
                    owner.disconnect_global_signals()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Construcción de la interfaz
    # ------------------------------------------------------------------

    def _title_bar_button(self, text, tooltip):
        button = QPushButton(text)
        button.setFlat(True)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setStyleSheet(
            "QPushButton { color: #cfe3f2; background: transparent; border: none; "
            "font-size: 13px; padding: 0 6px; }"
            "QPushButton:hover { color: #ffffff; }"
            # Sin esto el globo de ayuda hereda el fondo azul oscuro de la barra de
            # título y queda con letras negras sobre oscuro, ilegible.
            "QToolTip { color: #ffffff; background-color: #002856; "
            "border: 1px solid #cfe3f2; padding: 4px; font-size: 11px; }"
        )
        return button

    def _apply_custom_title_bar(self):
        """Barra de título propia: 'ToolkitPalm' a la izquierda, 'Iniciar sesión' a la
        derecha (por ahora solo texto, sin conectar — gancho visual para la Fase 4:
        autenticación real), un botón para contraer el panel hacia el borde derecho
        (sin ocultarlo del todo, ver _on_toggle_collapse) y un botón para ocultarlo
        por completo sin cerrar la conexión externa (MCP) que pueda estar activa —
        esa solo se apaga al cerrar QGIS (ver closeEvent).

        Hay dos barras de título: la completa (título + botones) y una mínima que
        se usa mientras el panel está contraído, porque a 32px de ancho no cabe
        el texto — el botón para volver a expandir vive en la franja angosta
        (_build_collapsed_strip), no aquí."""
        title_widget = QWidget(self)
        title_widget.setStyleSheet("background-color: #002856; min-height: 26px;")
        layout = QHBoxLayout(title_widget)
        layout.setContentsMargins(10, 4, 4, 4)

        name_label = QLabel("ToolkitPalm")
        name_label.setObjectName("tituloPlugin")
        name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # Sin esto el diseño lo encoge cuando el panel está angosto y el texto
        # sale cortado (se veía "alm" en vez de "ToolkitPalm").
        name_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        name_label.setStyleSheet(
            "color: #ffffff; font-size: 14px; font-weight: bold; background: transparent;"
        )
        # El color también por paleta: con solo la hoja de estilos el texto salía
        # del mismo color del fondo azul (invisible) según el tema de QGIS.
        from qgis.PyQt.QtGui import QPalette, QColor, QFont
        paleta = name_label.palette()
        paleta.setColor(QPalette.WindowText, QColor("#ffffff"))
        paleta.setColor(QPalette.Text, QColor("#ffffff"))
        name_label.setPalette(paleta)
        fuente = name_label.font()
        fuente.setBold(True)
        fuente.setPointSize(11)
        name_label.setFont(fuente)
        name_label.setMinimumWidth(name_label.fontMetrics().horizontalAdvance("ToolkitPalm") + 8)
        name_label.setToolTip("ToolkitPalm — herramientas de palma de aceite para QGIS")
        layout.addWidget(name_label, 0, Qt.AlignLeft | Qt.AlignVCenter)

        layout.addStretch(1)

        self._name_label = QLabel("")
        self._name_label.setStyleSheet("color: #ffffff; font-size: 11px; background: transparent;")
        layout.addWidget(self._name_label, 0, Qt.AlignRight)

        self._credits_label = QPushButton("")
        self._credits_label.setFlat(True)
        self._credits_label.setCursor(Qt.PointingHandCursor)
        self._credits_label.setStyleSheet(
            "QPushButton { color: #9fe6a0; background: transparent; border: none; "
            "font-size: 11px; font-weight: bold; padding: 0 4px; }"
            "QPushButton:hover { color: #ffffff; text-decoration: underline; }"
            "QToolTip { color: #ffffff; background-color: #002856; "
            "border: 1px solid #cfe3f2; padding: 4px; font-size: 11px; }"
        )
        self._credits_label.setToolTip("Ver saldo y comprar más créditos")
        self._credits_label.clicked.connect(self._on_credits_clicked)
        layout.addWidget(self._credits_label, 0, Qt.AlignRight)

        self._login_label = QLabel("Iniciar sesión")
        self._login_label.setStyleSheet(
            "color: #cfe3f2; font-size: 11px; background: transparent; text-decoration: underline;"
        )
        self._login_label.setCursor(Qt.PointingHandCursor)
        self._login_label.mousePressEvent = lambda event: self._on_login_clicked()
        layout.addWidget(self._login_label, 0, Qt.AlignRight)

        self._refresh_login_ui()

        reload_button = self._title_bar_button(
            "⟳",
            "Recargar el plugin (para aplicar una actualización sin cerrar QGIS)",
        )
        reload_button.clicked.connect(self._on_reload_plugin_clicked)
        layout.addWidget(reload_button, 0, Qt.AlignRight)

        collapse_button = self._title_bar_button(
            "»",
            "Contraer hacia la derecha (sigue conectado, solo se reduce a una franja)",
        )
        collapse_button.clicked.connect(self._on_toggle_collapse)
        layout.addWidget(collapse_button, 0, Qt.AlignRight)

        hide_button = self._title_bar_button(
            "✕",
            "Ocultar panel (la conexión con Claude Desktop/Code sigue activa; "
            "vuelve a abrir el panel desde el botón de la barra de herramientas)",
        )
        hide_button.clicked.connect(self.ocultar_panel)
        layout.addWidget(hide_button, 0, Qt.AlignRight)

        self._title_bar_full = title_widget

        self._title_bar_collapsed = QWidget(self)
        self._title_bar_collapsed.setStyleSheet("background-color: #002856; min-height: 26px;")
        # Se crea ahora pero solo se usa al contraer el panel: si se deja visible,
        # al ser hija del dock sin estar en ningún layout, se dibuja en la esquina
        # superior izquierda TAPANDO el nombre del plugin (mismo color de fondo,
        # así que parecía que el texto simplemente no aparecía).
        self._title_bar_collapsed.hide()

        self.setTitleBarWidget(self._title_bar_full)

    def _build_overlay_login(self):
        """Pantalla que cubre el panel cuando no hay sesión iniciada."""
        capa = QWidget(self._full_body)
        capa.setAutoFillBackground(True)
        capa.setStyleSheet(
            "QWidget#capaLogin { background-color: rgba(255, 255, 255, 235); }"
        )
        capa.setObjectName("capaLogin")

        disposicion = QVBoxLayout(capa)
        disposicion.setAlignment(Qt.AlignCenter)
        disposicion.setContentsMargins(24, 24, 24, 24)
        disposicion.setSpacing(14)

        titulo = QLabel("Inicia sesión para usar ToolkitPalm")
        titulo.setAlignment(Qt.AlignCenter)
        titulo.setWordWrap(True)
        titulo.setStyleSheet(
            f"color: {AZUL_OSCURO}; font-size: 15px; font-weight: bold; background: transparent;"
        )
        disposicion.addWidget(titulo)

        explicacion = QLabel(
            "Tu cuenta de Google identifica tus créditos de procesamiento y guarda "
            "tu saldo aunque cambies de computador."
        )
        explicacion.setAlignment(Qt.AlignCenter)
        explicacion.setWordWrap(True)
        explicacion.setStyleSheet("color: #4b5563; font-size: 12px; background: transparent;")
        disposicion.addWidget(explicacion)

        boton = QPushButton("Iniciar sesión con Google")
        boton.setCursor(Qt.PointingHandCursor)
        boton.setStyleSheet(
            "QPushButton { background-color: #1a73e8; color: white; border: none; "
            "padding: 10px 18px; border-radius: 4px; font-size: 13px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1765cc; }"
        )
        boton.clicked.connect(self._on_login_clicked)
        disposicion.addWidget(boton, 0, Qt.AlignCenter)

        capa.hide()
        return capa

    def eventFilter(self, objeto, evento):
        """Mantiene la capa de login cubriendo todo el panel cuando cambia de tamaño."""
        try:
            from qgis.PyQt.QtCore import QEvent
            if (objeto is self._full_body and evento.type() == QEvent.Resize
                    and getattr(self, "_overlay_login", None)):
                self._overlay_login.setGeometry(self._full_body.rect())
        except Exception:
            pass
        return super().eventFilter(objeto, evento)

    def _sincronizar_overlay_login(self):
        """Muestra u oculta la pantalla de inicio de sesión según el estado actual."""
        capa = getattr(self, "_overlay_login", None)
        if capa is None:
            return
        from . import google_auth
        # Si aún no hay credenciales de Google configuradas no se bloquea nada:
        # el plugin sigue usable en modo local mientras se termina de configurar.
        debe_bloquear = google_auth.esta_configurado() and not google_auth.sesion_activa()
        if debe_bloquear:
            capa.setGeometry(self._full_body.rect())
            capa.show()
            capa.raise_()
        else:
            capa.hide()

    def _es_administrador(self):
        """True si la cuenta con sesión iniciada es del dueño del servicio."""
        try:
            from .config import ADMIN_EMAILS, ADMIN_API_KEY
            if not ADMIN_EMAILS or not ADMIN_API_KEY:
                return False
            from . import google_auth
            datos = google_auth.perfil() or {}
            return (datos.get("email", "") or "").lower() in ADMIN_EMAILS
        except Exception:
            return False

    def _on_regalar_creditos(self):
        """Regala créditos a un cliente, buscándolo por su correo de Google."""
        from .config import CREDITS_REGALAR_ENDPOINT, ADMIN_API_KEY

        correo, ok = QInputDialog.getText(
            self, "Regalar créditos",
            "Correo de Google del cliente:\n(debe haber iniciado sesión al menos una vez)")
        if not ok or not correo.strip():
            return

        cantidad, ok = QInputDialog.getInt(
            self, "Regalar créditos",
            f"¿Cuántos créditos le regalas a {correo.strip()}?\n"
            "(1 crédito = hasta 5 ha; un paquete son 60)",
            60, 1, 10000)
        if not ok:
            return

        try:
            import requests
            respuesta = requests.post(
                CREDITS_REGALAR_ENDPOINT,
                headers={"X-Admin-Key": ADMIN_API_KEY},
                data={"client_id": "", "email": correo.strip(), "cantidad": cantidad,
                      "nota": "regalo del dueño"},
                timeout=30,
            )
            if respuesta.status_code == 200:
                datos = respuesta.json()
                QMessageBox.information(
                    self, "Créditos regalados",
                    f"Se le regalaron {cantidad} créditos a {correo.strip()}.\n"
                    f"Su saldo quedó en {datos.get('creditos_saldo', '—')} créditos.")
            else:
                detalle = respuesta.text
                try:
                    detalle = respuesta.json().get("detail", detalle)
                except Exception:
                    pass
                QMessageBox.warning(self, "No se pudo regalar", str(detalle))
        except Exception as e:
            QMessageBox.warning(self, "No se pudo regalar", f"Error de conexión: {e}")

    def _peticion_creditos(self, url, datos, con_admin=False, timeout=30):
        """Llama al backend de créditos y devuelve (ok, datos_o_mensaje)."""
        import requests
        from .config import API_KEY, ADMIN_API_KEY
        cabeceras = {"X-Admin-Key": ADMIN_API_KEY} if con_admin else {"X-API-Key": API_KEY}
        if not con_admin:
            try:
                from . import google_auth
                token = google_auth.obtener_id_token()
                if token:
                    cabeceras["Authorization"] = f"Bearer {token}"
            except Exception:
                pass
        try:
            respuesta = requests.post(url, headers=cabeceras, data=datos, timeout=timeout)
            if respuesta.status_code == 200:
                return True, respuesta.json()
            detalle = respuesta.text
            try:
                detalle = respuesta.json().get("detail", detalle)
            except Exception:
                pass
            return False, str(detalle)
        except Exception as e:
            return False, f"Error de conexión: {e}"

    def _on_comprar_creditos(self):
        """Abre el checkout de pago (PSE, tarjetas, Nequi...) para comprar un paquete.

        El enlace lo genera el servidor porque va firmado: así el monto no se
        puede alterar desde el plugin.
        """
        from .config import PAGOS_CHECKOUT_ENDPOINT, CREDITS_PURCHASE_URL

        ok, datos = self._peticion_creditos(
            PAGOS_CHECKOUT_ENDPOINT,
            {"client_id": client_identity.get_client_id(), "codigo_descuento": ""},
        )
        if not ok:
            # Sin pagos configurados todavía: se abre la página informativa.
            QDesktopServices.openUrl(QUrl(CREDITS_PURCHASE_URL))
            return

        precio = datos.get("precio_cop", 0)
        creditos = datos.get("creditos", 0)
        respuesta = QMessageBox.question(
            self, "Comprar créditos",
            f"Paquete de {creditos} créditos (hasta {creditos * 5} ha) "
            f"por ${precio:,.0f} COP.\n\n"
            "Se abrirá tu navegador para pagar con PSE, tarjeta, Nequi o Bancolombia.\n"
            "Los créditos se acreditan solos apenas se apruebe el pago, y aquí en "
            "el plugin te avisamos cuando lleguen.\n\n"
            "Si cierras la ventana del pago o lo rechazan, no se cobra nada y tu "
            "saldo queda igual.",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        if respuesta != QMessageBox.Ok:
            return
        QDesktopServices.openUrl(QUrl(datos["url"]))
        self._vigilar_pago()

    def _vigilar_pago(self):
        """Queda pendiente del saldo tras abrir el checkout, para avisarle al usuario."""
        anterior = getattr(self, "_pago_watcher", None)
        if anterior is not None and anterior.isRunning():
            anterior.cancelar()

        saldo_actual = (getattr(self, "_ultimo_saldo", None) or {}).get("creditos_saldo")
        self._pago_watcher = _PagoWatcher(
            saldo_actual if isinstance(saldo_actual, int) else None, self)
        self._pago_watcher.acreditado.connect(self._on_pago_acreditado)
        self._pago_watcher.sin_confirmar.connect(self._on_pago_sin_confirmar)
        self._pago_watcher.start()
        self._avisar_en_barra(
            "Pago en proceso: te avisamos aquí apenas se confirme.", "info", 10)

    def _on_pago_acreditado(self, saldo_nuevo):
        self._refresh_credits_label()
        self._avisar_en_barra(f"¡Pago confirmado! Tu saldo quedó en {saldo_nuevo} créditos.",
                              "exito", 30)
        QMessageBox.information(
            self, "Pago confirmado",
            f"¡Listo! Tu pago se aprobó y ya tienes {saldo_nuevo} créditos "
            f"(hasta {saldo_nuevo * 5} ha de procesamiento).")

    def _on_pago_sin_confirmar(self):
        self._avisar_en_barra(
            "No llegó la confirmación del pago. Si cerraste la ventana o lo "
            "rechazaron, no se te cobró nada. Si sí pagaste, haz clic en tus "
            "créditos en unos minutos para volver a consultar.", "aviso", 30)

    def _avisar_en_barra(self, mensaje, nivel="info", segundos=10):
        """Muestra un aviso no bloqueante en la barra de mensajes de QGIS."""
        try:
            from qgis.core import Qgis
            niveles = {"info": Qgis.Info, "exito": Qgis.Success, "aviso": Qgis.Warning}
            self.iface.messageBar().pushMessage(
                "ToolkitPalm", mensaje, level=niveles.get(nivel, Qgis.Info),
                duration=segundos)
        except Exception:
            pass

    def _on_canjear_codigo(self):
        """Canjea un código de créditos o descuento (disponible para cualquier usuario)."""
        from .config import CREDITS_CODIGO_CANJEAR_ENDPOINT

        codigo, ok = QInputDialog.getText(self, "Canjear código", "Escribe tu código:")
        if not ok or not codigo.strip():
            return

        ok, datos = self._peticion_creditos(
            CREDITS_CODIGO_CANJEAR_ENDPOINT,
            {"codigo": codigo.strip(), "client_id": client_identity.get_client_id()},
        )
        if not ok:
            QMessageBox.warning(self, "No se pudo canjear", str(datos))
            return

        if datos.get("tipo") == "creditos":
            QMessageBox.information(
                self, "Código canjeado",
                f"¡Listo! Se agregaron {datos.get('valor')} créditos.\n"
                f"Tu saldo quedó en {datos.get('creditos_saldo', '—')} créditos.")
            self._refresh_credits_label()
        else:
            QMessageBox.information(
                self, "Código canjeado",
                f"Descuento del {datos.get('valor')}% activado para tu próxima compra.")

    def _on_crear_codigo(self):
        """Crea un código canjeable (solo el dueño)."""
        from .config import CREDITS_CODIGO_CREAR_ENDPOINT

        opciones = ["Créditos gratis", "Descuento en la compra (%)"]
        eleccion, ok = QInputDialog.getItem(
            self, "Crear código", "¿Qué otorga el código?", opciones, 0, False)
        if not ok:
            return
        tipo = "creditos" if eleccion == opciones[0] else "descuento"

        etiqueta = "¿Cuántos créditos regala?" if tipo == "creditos" else "¿Qué porcentaje de descuento?"
        valor, ok = QInputDialog.getInt(self, "Crear código", etiqueta,
                                        10 if tipo == "creditos" else 20, 1,
                                        10000 if tipo == "creditos" else 100)
        if not ok:
            return

        usos, ok = QInputDialog.getInt(
            self, "Crear código",
            "¿Cuántas veces se puede usar en total?\n(0 = ilimitado, 1 = un solo uso)",
            0, 0, 100000)
        if not ok:
            return

        dias, ok = QInputDialog.getInt(
            self, "Crear código",
            "¿Cuántos días será válido?\n(0 = permanente, 7 = una semana)",
            0, 0, 3650)
        if not ok:
            return

        personalizado, ok = QInputDialog.getText(
            self, "Crear código",
            "Código personalizado (opcional).\nDéjalo vacío para que se genere solo:")
        if not ok:
            return

        ok, datos = self._peticion_creditos(
            CREDITS_CODIGO_CREAR_ENDPOINT,
            {"codigo": personalizado.strip(), "tipo": tipo, "valor": valor,
             "usos_maximos": usos, "dias_validez": dias, "nota": "creado desde el plugin"},
            con_admin=True,
        )
        if not ok:
            QMessageBox.warning(self, "No se pudo crear", str(datos))
            return

        codigo = datos.get("codigo", "")
        detalle_usos = "ilimitados" if not usos else ("un solo uso" if usos == 1 else f"{usos} usos")
        detalle_vigencia = "permanente" if not dias else f"vence en {dias} días"
        otorga = (f"{valor} créditos" if tipo == "creditos" else f"{valor}% de descuento")

        QApplication.clipboard().setText(codigo)
        QMessageBox.information(
            self, "Código creado",
            f"Código: {codigo}\n\n"
            f"Otorga: {otorga}\n"
            f"Usos: {detalle_usos}\n"
            f"Vigencia: {detalle_vigencia}\n\n"
            "Ya quedó copiado al portapapeles.")

    def _refresh_login_ui(self):
        """Actualiza nombre / créditos / texto de iniciar-cerrar sesión en la barra
        de título según el estado guardado en QSettings (ver client_identity.py)."""
        name = client_identity.get_display_name()
        if name:
            self._name_label.setText(name)
            self._login_label.setText("Cerrar sesión")
            self._credits_label.setVisible(True)
            self._credits_label.setText("créditos: …")
            QTimer.singleShot(0, self._refresh_credits_label)
            # Refresco periódico: el saldo cambia cuando termina un procesamiento,
            # y el panel no se entera solo. Consulta en segundo plano, no bloquea.
            if not getattr(self, "_temporizador_creditos", None):
                self._temporizador_creditos = QTimer(self)
                self._temporizador_creditos.timeout.connect(self._refresh_credits_label)
                self._temporizador_creditos.start(60000)
        else:
            self._name_label.setText("")
            self._login_label.setText("Iniciar sesión")
            self._credits_label.setVisible(False)
        self._sincronizar_overlay_login()

    def _refresh_credits_label(self):
        """Lanza la consulta del saldo en segundo plano (ver _CreditsFetcher)."""
        if getattr(self, "_credits_thread", None) and self._credits_thread.isRunning():
            return
        self._credits_thread = _CreditsFetcher()
        self._credits_thread.resultado.connect(self._on_credits_fetched)
        self._credits_thread.start()

    def _on_credits_fetched(self, saldo):
        self._ultimo_saldo = saldo
        if saldo is None:
            self._credits_label.setText("créditos: —")
            self._credits_label.setToolTip(
                "No se pudo consultar el saldo. Se reintentará solo; también puedes "
                "hacer clic aquí para intentar de nuevo."
            )
            # Reintento automático: lo más común es que el servidor estuviera
            # apagado y la primera consulta lo haya despertado.
            QTimer.singleShot(20000, self._refresh_credits_label)
        else:
            self._credits_label.setText(f"{saldo.get('creditos_saldo', '—')} créditos")
            self._credits_label.setToolTip("Ver saldo y comprar más créditos")

    def _on_login_clicked(self):
        from . import google_auth

        if client_identity.is_logged_in():
            reply = QMessageBox.question(
                self, "Cerrar sesión",
                "¿Cerrar sesión? Tu saldo de créditos no se pierde — vuelve a "
                "aparecer si inicias sesión con la misma cuenta.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            google_auth.cerrar_sesion()
            client_identity.clear_display_name()
            self._refresh_login_ui()
            return

        if google_auth.esta_configurado():
            self._iniciar_sesion_google()
            return

        # Sin credenciales de Google configuradas: modo local, solo para poder
        # identificar la sesión en la interfaz mientras se termina de configurar.
        nombre, ok = QInputDialog.getText(
            self, "Iniciar sesión", "Nombre o usuario:",
            text=client_identity.get_display_name(),
        )
        if not ok or not nombre.strip():
            return
        client_identity.set_display_name(nombre)
        self._refresh_login_ui()

    def _iniciar_sesion_google(self):
        from . import google_auth

        espera = QMessageBox(self)
        espera.setIcon(QMessageBox.Information)
        espera.setWindowTitle("Iniciar sesión con Google")
        espera.setText(
            "Se abrió tu navegador para que inicies sesión con Google.\n\n"
            "Cuando termines allá, esta ventana se cierra sola."
        )
        espera.setStandardButtons(QMessageBox.Cancel)
        espera.show()
        QApplication.processEvents()

        try:
            datos = google_auth.iniciar_sesion(al_esperar=QApplication.processEvents)
        except Exception as e:
            espera.close()
            QMessageBox.warning(self, "No se pudo iniciar sesión", str(e))
            return
        espera.close()

        if not datos:
            return
        client_identity.set_display_name(datos.get("name") or datos.get("email") or "Usuario")
        self._refresh_login_ui()

    def _on_credits_clicked(self):
        # Usa el último saldo consultado en segundo plano; no consulta aquí para
        # no congelar la interfaz mientras se abre la ventana.
        saldo = getattr(self, "_ultimo_saldo", None)
        self._refresh_credits_label()
        if saldo is None:
            texto_saldo = "No se pudo consultar el saldo (revisa tu conexión)."
        else:
            texto_saldo = (
                f"Créditos disponibles: {saldo.get('creditos_saldo', '—')}\n"
                f"Hectáreas disponibles: {saldo.get('ha_disponibles', '—')}"
            )

        dialog = QDialog(self)
        dialog.setWindowTitle("Créditos de procesamiento")
        dialog.setMinimumWidth(360)
        layout = QVBoxLayout(dialog)

        info_label = QLabel(texto_saldo)
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        explicacion = QLabel(
            "1 crédito cubre hasta 5 ha de procesamiento (Detector o Segmentador).\n"
            "Un paquete de 60 créditos (hasta 300 ha) cuesta 25.000 COP y se puede "
            "comprar en cualquier momento — se suma a tu saldo actual."
        )
        explicacion.setWordWrap(True)
        explicacion.setStyleSheet("color: #555; font-size: 11px;")
        layout.addWidget(explicacion)

        comprar_button = QPushButton("Comprar más créditos")
        comprar_button.clicked.connect(self._on_comprar_creditos)
        layout.addWidget(comprar_button)

        canjear_button = QPushButton("Canjear un código")
        canjear_button.clicked.connect(self._on_canjear_codigo)
        layout.addWidget(canjear_button)

        if self._es_administrador():
            regalar_button = QPushButton("Regalar créditos a un cliente")
            regalar_button.setStyleSheet("font-weight: bold;")
            regalar_button.clicked.connect(self._on_regalar_creditos)
            layout.addWidget(regalar_button)

            crear_codigo_button = QPushButton("Crear código de regalo o descuento")
            crear_codigo_button.setStyleSheet("font-weight: bold;")
            crear_codigo_button.clicked.connect(self._on_crear_codigo)
            layout.addWidget(crear_codigo_button)

        cerrar_button = QPushButton("Cerrar")
        cerrar_button.clicked.connect(dialog.close)
        layout.addWidget(cerrar_button)

        dialog.show()
        self._credits_dialog = dialog  # evitar que el garbage collector lo cierre

    def _on_reload_plugin_clicked(self):
        """Recarga el plugin desde disco sin tener que cerrar QGIS. Equivale a
        usar el complemento "Plugin Reloader" (purga los módulos en caché de
        Python para que se vuelvan a importar desde los archivos actuales),
        pero desde un botón propio en la esquina del panel."""
        reply = QtWidgets.QMessageBox.question(
            self,
            "Recargar ToolkitPalm",
            "¿Recargar el plugin ahora?\n\nEsto vuelve a cargar el código desde disco "
            "(por ejemplo, después de una actualización). El panel se cerrará y volverá "
            "a abrirse; las capas de tu proyecto no se ven afectadas.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        # Se difiere un instante para que este método termine de manejar la
        # señal del botón antes de que el propio widget (dueño del botón) se
        # destruya como parte de la descarga/recarga del plugin.
        QTimer.singleShot(0, self._do_reload_plugin)

    def _do_reload_plugin(self):
        import sys
        package_name = "toolkitpalm"
        try:
            from qgis.utils import unloadPlugin, loadPlugin, startPlugin

            # El orden importa: primero descargar (ahí el plugin se desengancha de
            # las señales globales de QGIS, ver unload), recién después borrar los
            # módulos de la caché de Python. Al revés, unload() podría quedarse sin
            # los módulos que necesita y dejar conexiones colgando.
            unloadPlugin(package_name)
            for modname in list(sys.modules.keys()):
                if modname == package_name or modname.startswith(package_name + "."):
                    del sys.modules[modname]
            loadPlugin(package_name)
            startPlugin(package_name)
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "ToolkitPalm", f"No se pudo recargar el plugin:\n{e}")
            return
        QtWidgets.QMessageBox.information(None, "ToolkitPalm", "✅ Plugin recargado.")

    def _build_collapsed_strip(self):
        """Franja angosta que reemplaza todo el panel cuando está contraído: un
        solo botón para volver a expandirlo. El dock nunca se oculta del todo,
        solo se reduce a esto — así siempre queda visible que sigue ahí."""
        strip = QFrame()
        strip.setStyleSheet("QFrame { background-color: #f4f5f7; border-left: 1px solid #d9dcdf; }")
        layout = QVBoxLayout(strip)
        layout.setContentsMargins(2, 8, 2, 8)

        expand_button = QPushButton("«")
        expand_button.setFlat(True)
        expand_button.setCursor(Qt.PointingHandCursor)
        expand_button.setToolTip("Expandir ToolkitPalm")
        expand_button.setFixedWidth(26)
        expand_button.setStyleSheet(
            f"QPushButton {{ color: {AZUL_OSCURO}; background: transparent; border: none; font-size: 14px; }}"
            f"QPushButton:hover {{ color: {AZUL_MEDIO}; }}"
        )
        expand_button.clicked.connect(self._on_toggle_collapse)
        layout.addWidget(expand_button, 0, Qt.AlignHCenter)
        layout.addStretch(1)
        return strip

    def _on_toggle_collapse(self):
        if getattr(self, "_collapsed", False):
            self._expandir_panel()
        else:
            self._expanded_width = self.width()
            self._full_body.setParent(None)          # sale del dock, deja de imponer su ancho
            self.setWidget(self._collapsed_strip)
            self._collapsed_strip.show()
            self.setTitleBarWidget(self._title_bar_collapsed)
            self._collapsed = True
            # Con el cuerpo completo fuera, el mínimo del dock ya es el de la
            # franja, así que este ancho sí se aplica.
            self.setFixedWidth(self._COLLAPSED_WIDTH)

    def _expandir_panel(self):
        """Devuelve el panel a su tamaño normal y suelta la restricción de ancho."""
        self._collapsed_strip.setParent(None)
        self.setWidget(self._full_body)
        self._full_body.show()
        self.setTitleBarWidget(self._title_bar_full)
        self._collapsed = False
        self.setFixedWidth(self._expanded_width or 380)
        # Se libera la restricción un momento después (no de una vez) para que
        # QGIS ya haya aplicado el ancho antes de permitir arrastrar el borde.
        QTimer.singleShot(0, self._relax_width_constraints)

    def ocultar_panel(self):
        """Oculta el panel, expandiéndolo antes si estaba contraído.

        Contraído, el panel fija su ancho en 32 px y el área de acoplamiento de
        QGIS queda con ese ancho. Si se oculta así, el ancho angosto se queda y
        el siguiente panel que se abra de ese lado aparece aplastado contra el
        borde. Se expande primero para dejar el área en un ancho usable.

        Va acá y no en hideEvent a propósito: cambiar el tamaño dentro de
        hideEvent vuelve a disparar el ciclo de layout de QGIS y lo cuelga."""
        if getattr(self, "_collapsed", False):
            self._expandir_panel()
        self.hide()

    def _relax_width_constraints(self):
        self.setMinimumWidth(0)
        self.setMaximumWidth(16777215)

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet(f"QFrame {{ background-color: #f4f5f7; border-right: 1px solid #d9dcdf; }}")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 8, 0, 8)
        sidebar_layout.setSpacing(6)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("🔍 Buscar herramienta...")
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._on_search_changed)
        sidebar_layout.addWidget(self._search_box)

        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        for key, label in _TOOLS:
            button = QPushButton(label)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumHeight(38)
            if key in _DISABLED_TOOLS:
                button.setCheckable(False)
                button.setFlat(True)
                button.setEnabled(False)
                button.setCursor(Qt.ArrowCursor)
                button.setStyleSheet(_CARD_STYLE_DISABLED)
                button.setToolTip("Todavía no está disponible — se desarrollará más adelante.")
            else:
                button.setCheckable(True)
                button.setFlat(True)
                button.setCursor(Qt.PointingHandCursor)
                button.setStyleSheet(_CARD_STYLE)
                button.clicked.connect(lambda _checked, k=key: self._on_card_clicked(k))
                self._button_group.addButton(button)
            if key in _TOOLS_EN_CONSTRUCCION:
                button.setText(label + "\n⚠ en construcción")
                button.setMinimumHeight(48)
                if key not in _DISABLED_TOOLS:
                    button.setToolTip("Funciona, pero sigue en desarrollo: revisa los "
                                      "resultados antes de usarlos en campo.")

            self._buttons[key] = button
            sidebar_layout.addWidget(button)

        sidebar_layout.addStretch(1)

        # Firma del autor, al pie de la columna de herramientas.
        autoria = QLabel("Desarrollado por Ing. Adán Arias")
        autoria.setAlignment(Qt.AlignCenter)
        autoria.setWordWrap(True)
        autoria.setStyleSheet(
            f"color: {AZUL_OSCURO}; font-size: 10px; font-weight: bold; "
            "background: #eef2f6; padding: 7px 4px; "
            "border-top: 1px solid #d9dcdf; border-bottom: 1px solid #d9dcdf;"
        )
        sidebar_layout.addWidget(autoria)

        return sidebar

    def _build_empty_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel("Selecciona una herramienta en el panel de la izquierda.")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(f"color: {AZUL_OSCURO}; font-size: 13px; padding: 24px;")
        layout.addStretch(1)
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    def _build_placeholder_page(self, titulo, mensaje):
        """
        Página genérica para una herramienta que todavía no tiene desarrollo
        propio, pero que sí se puede abrir (a diferencia de las que quedaron
        en gris al final de la lista): solo muestra un aviso de "en planeación".

        Devuelve un QDockWidget mínimo (con terms_accepted=True) para que
        encaje con lo que espera _get_or_create_page de cualquier herramienta.
        """
        instance = QDockWidget()
        instance.terms_accepted = True

        content = QWidget()
        layout = QVBoxLayout(content)
        label = QLabel(f"<b style='font-size:15px;'>{titulo}</b><br><br>{mensaje}")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(f"color: {AZUL_OSCURO}; font-size: 13px; padding: 24px;")
        layout.addStretch(1)
        layout.addWidget(label)
        layout.addStretch(1)
        instance.setWidget(content)
        return instance

    def _build_llm_page(self):
        self._assistant = None  # instancia perezosa de assistant.agent.Assistant
        self._pending_attachments = []  # [{"path", "kind", "media_type"}, ...] pendientes de enviar
        self._mcp_server = None  # instancia perezosa de assistant.qgis_actions.QgisMCPServer
        self._mcp_last_activity = None  # hora (texto) de la última consulta real recibida

        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("🤖 Asistente")
        title.setStyleSheet(f"color: {AZUL_OSCURO}; font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        # La conexión externa va primero y desplegada: es la vía que se usa a
        # diario (Claude Desktop / Claude Code con la suscripción). La clave de
        # API es la alternativa, así que queda abajo y plegada.
        layout.addWidget(self._build_mcp_connection_box())
        layout.addWidget(self._build_assistant_settings_box())

        # El chat solo se muestra una vez que hay un proveedor configurado (clave
        # de API de Claude, o URL de un modelo propio) — mientras tanto se ve este
        # aviso, para no desperdiciar espacio con una ventana de chat inutilizable.
        self._chat_placeholder = QLabel(
            "Configura tu proveedor de IA arriba (clave de API de Claude, o la URL "
            "de tu modelo propio) y presiona \"Guardar configuración\" para activar el chat."
        )
        self._chat_placeholder.setWordWrap(True)
        self._chat_placeholder.setAlignment(Qt.AlignCenter)
        self._chat_placeholder.setStyleSheet(f"color: {AZUL_MEDIO}; font-size: 12px; padding: 24px;")
        layout.addWidget(self._chat_placeholder, 1)

        self._chat_container = QWidget()
        chat_layout = QVBoxLayout(self._chat_container)
        chat_layout.setContentsMargins(0, 0, 0, 0)

        self._chat_history = QTextBrowser()
        self._chat_history.setOpenExternalLinks(True)
        self._chat_history.setStyleSheet("background-color: white; border: 1px solid #d9dcdf;")
        self._append_chat_html(
            f"<i style='color:{AZUL_MEDIO};'>Escribe algo como \"qué capas tengo cargadas\" o "
            f"\"detecta las palmas del lote 12\".</i>"
        )
        chat_layout.addWidget(self._chat_history, 1)

        attach_row = QHBoxLayout()
        self._attach_button = QPushButton("📎 Adjuntar")
        self._attach_button.setToolTip("Adjuntar imágenes o documentos PDF para que el asistente los lea")
        self._attach_button.clicked.connect(self._on_attach_files)
        attach_row.addWidget(self._attach_button)

        self._attachments_label = QLabel("")
        self._attachments_label.setStyleSheet(f"color: {AZUL_MEDIO}; font-size: 11px;")
        self._attachments_label.setWordWrap(True)
        attach_row.addWidget(self._attachments_label, 1)

        self._clear_attachments_button = QPushButton("Quitar")
        self._clear_attachments_button.setVisible(False)
        self._clear_attachments_button.clicked.connect(self._on_clear_attachments)
        attach_row.addWidget(self._clear_attachments_button)
        chat_layout.addLayout(attach_row)

        input_row = QHBoxLayout()
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Escribe tu mensaje...")
        self._chat_input.returnPressed.connect(self._on_send_chat_message)
        input_row.addWidget(self._chat_input, 1)

        self._chat_send_button = QPushButton("Enviar")
        self._chat_send_button.setStyleSheet(
            f"QPushButton {{ background-color: {AZUL_MEDIO}; color: white; padding: 6px 14px; border: none; }}"
            f"QPushButton:hover {{ background-color: {AZUL_CLARO}; }}"
            f"QPushButton:disabled {{ background-color: #9ca3af; }}"
        )
        self._chat_send_button.clicked.connect(self._on_send_chat_message)
        input_row.addWidget(self._chat_send_button)
        chat_layout.addLayout(input_row)

        layout.addWidget(self._chat_container, 1)

        self._update_chat_visibility()
        # Se difiere a que la ventana ya esté armada: levantar el socket a mitad
        # de la construcción del panel deja la interfaz a medio dibujar si falla.
        QTimer.singleShot(0, self._restaurar_conexion_externa)
        return page

    def _build_collapsible_section(self, title, expanded=True):
        """Encabezado (checkbox + título, sin marco) + un widget de contenido
        que se muestra/oculta al marcar/desmarcar el checkbox. A diferencia de
        un QGroupBox plegable, al contraerse no deja ningún recuadro vacío:
        solo queda el texto del título.

        Devuelve (container, content, checkbox).
        """
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        checkbox = QCheckBox(title)
        checkbox.setChecked(expanded)
        checkbox.setStyleSheet(f"QCheckBox {{ font-weight: bold; color: {AZUL_OSCURO}; font-size: 12px; }}")
        outer.addWidget(checkbox)

        content = QWidget()
        content.setVisible(expanded)
        outer.addWidget(content)
        checkbox.toggled.connect(content.setVisible)

        return container, content, checkbox

    def _build_assistant_settings_box(self):
        """Selector de proveedor (Claude / modelo propio) + credenciales, con
        guardado en QSettings. Sección plegable (clic en el checkbox) para no
        ocupar espacio una vez que ya quedó configurada."""
        container, content, checkbox = self._build_collapsible_section(
            "Configuración del modelo", expanded=False
        )
        self._model_settings_checkbox = checkbox
        box_layout = QVBoxLayout(content)
        box_layout.setContentsMargins(0, 4, 0, 0)

        self._provider_combo = QComboBox()
        self._provider_combo.addItem("Claude (API de Anthropic)", "claude")
        self._provider_combo.addItem("Modelo propio (URL personalizada)", "openai_compatible")
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        box_layout.addWidget(self._provider_combo)

        # --- Campos Claude ---
        self._claude_form = QWidget()
        claude_form_layout = QFormLayout(self._claude_form)
        claude_form_layout.setContentsMargins(0, 4, 0, 0)
        self._claude_api_key_edit = QLineEdit()
        self._claude_api_key_edit.setEchoMode(QLineEdit.Password)
        self._claude_api_key_edit.setPlaceholderText("sk-ant-...")
        claude_form_layout.addRow("Clave de API:", self._claude_api_key_edit)
        self._claude_model_combo = QComboBox()
        for label, model_id in _CLAUDE_MODEL_CHOICES:
            self._claude_model_combo.addItem(label, model_id)
        claude_model_row = QHBoxLayout()
        claude_model_row.addWidget(self._claude_model_combo, 1)
        self._refresh_models_button = QPushButton("🔄")
        self._refresh_models_button.setToolTip(
            "Consultar en la API de Anthropic la lista de modelos disponible en este momento"
        )
        self._refresh_models_button.setMaximumWidth(32)
        self._refresh_models_button.clicked.connect(self._on_refresh_claude_models)
        claude_model_row.addWidget(self._refresh_models_button)
        claude_form_layout.addRow("Modelo:", claude_model_row)
        box_layout.addWidget(self._claude_form)

        # --- Campos modelo propio (OpenAI-compatible) ---
        self._custom_form = QWidget()
        custom_form_layout = QFormLayout(self._custom_form)
        custom_form_layout.setContentsMargins(0, 4, 0, 0)
        self._custom_base_url_edit = QLineEdit()
        self._custom_base_url_edit.setPlaceholderText("http://tu-servidor:8000/v1")
        custom_form_layout.addRow("URL del servidor:", self._custom_base_url_edit)
        self._custom_model_edit = QLineEdit()
        custom_form_layout.addRow("Modelo:", self._custom_model_edit)
        self._custom_api_key_edit = QLineEdit()
        self._custom_api_key_edit.setEchoMode(QLineEdit.Password)
        self._custom_api_key_edit.setPlaceholderText("(opcional)")
        custom_form_layout.addRow("Clave de API:", self._custom_api_key_edit)
        box_layout.addWidget(self._custom_form)

        save_button = QPushButton("Guardar configuración")
        save_button.clicked.connect(self._on_save_assistant_settings)
        box_layout.addWidget(save_button)

        self._load_assistant_settings()
        self._on_provider_changed()
        return container

    def _build_mcp_connection_box(self):
        """Interruptor + instrucciones para conectar Claude Desktop / Claude Code
        directamente a QGIS (por fuera de este plugin), usando la suscripción de
        Claude en vez de pagar por la API — ver mcp_bridge/toolkitpalm_bridge.py.
        Plegable y colapsada por defecto: es una función avanzada/opcional."""
        container, content, _checkbox = self._build_collapsible_section(
            "Conexión externa", expanded=True
        )
        box_layout = QVBoxLayout(content)

        info = QLabel(
            "Permite que Claude Desktop o Claude Code controlen QGIS directamente, "
            "usando tu suscripción de Claude en vez de una clave de API. Requiere "
            "instalar un pequeño puente aparte (ver botón de instrucciones abajo)."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {AZUL_OSCURO}; font-size: 11px;")
        box_layout.addWidget(info)

        toggle_row = QHBoxLayout()
        toggle_row.addWidget(QLabel("Puerto:"))
        self._mcp_port_spin = QSpinBox()
        self._mcp_port_spin.setRange(1024, 65535)
        self._mcp_port_spin.setValue(9876)
        toggle_row.addWidget(self._mcp_port_spin)

        self._mcp_toggle_button = QPushButton("Activar conexión")
        self._mcp_toggle_button.setCheckable(True)
        self._mcp_toggle_button.clicked.connect(self._on_toggle_mcp_server)
        toggle_row.addWidget(self._mcp_toggle_button)
        toggle_row.addStretch(1)
        box_layout.addLayout(toggle_row)

        self._mcp_status_label = QLabel("Detenido.")
        self._mcp_status_label.setStyleSheet(f"color: {AZUL_MEDIO}; font-size: 11px;")
        box_layout.addWidget(self._mcp_status_label)

        instructions_button = QPushButton("📋 Generar instrucciones de configuración")
        instructions_button.clicked.connect(self._on_show_mcp_instructions)
        box_layout.addWidget(instructions_button)

        # Las instrucciones se muestran aquí mismo (no en el chat de arriba, que
        # puede estar oculto si el modelo de IA todavía no está configurado).
        # Sin scroll horizontal: solo vertical, para que nunca se corte texto.
        self._mcp_copyable_texts = {}  # key -> texto real a copiar (sin adornos visuales)
        self._mcp_instructions_browser = QTextBrowser()
        self._mcp_instructions_browser.setOpenExternalLinks(False)
        self._mcp_instructions_browser.setOpenLinks(False)
        self._mcp_instructions_browser.anchorClicked.connect(self._on_mcp_instructions_link_clicked)
        self._mcp_instructions_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._mcp_instructions_browser.setStyleSheet(
            "background-color: white; border: 1px solid #d9dcdf; font-size: 13px;"
        )
        self._mcp_instructions_browser.setMinimumHeight(280)
        self._mcp_instructions_browser.setMaximumHeight(360)
        self._mcp_instructions_browser.setVisible(False)
        box_layout.addWidget(self._mcp_instructions_browser)

        self._mcp_copy_feedback = QLabel("")
        self._mcp_copy_feedback.setStyleSheet("color: #1e8e3e; font-size: 11px; font-weight: bold;")
        self._mcp_copy_feedback.setVisible(False)
        box_layout.addWidget(self._mcp_copy_feedback)

        return container

    # ------------------------------------------------------------------
    # Asistente: configuración
    # ------------------------------------------------------------------

    def _on_provider_changed(self):
        is_claude = self._provider_combo.currentData() == "claude"
        self._claude_form.setVisible(is_claude)
        self._custom_form.setVisible(not is_claude)

    def _load_assistant_settings(self):
        settings = QSettings()
        provider = settings.value(f"{_ASSISTANT_SETTINGS_PREFIX}/provider", ASSISTANT_DEFAULT_PROVIDER)
        index = self._provider_combo.findData(provider)
        self._provider_combo.setCurrentIndex(index if index >= 0 else 0)

        self._claude_api_key_edit.setText(settings.value(f"{_ASSISTANT_SETTINGS_PREFIX}/claude_api_key", ANTHROPIC_API_KEY))
        saved_claude_model = settings.value(f"{_ASSISTANT_SETTINGS_PREFIX}/claude_model", ASSISTANT_CLAUDE_MODEL)
        model_index = self._claude_model_combo.findData(saved_claude_model)
        self._claude_model_combo.setCurrentIndex(model_index if model_index >= 0 else 0)

        self._custom_base_url_edit.setText(settings.value(f"{_ASSISTANT_SETTINGS_PREFIX}/custom_base_url", ASSISTANT_CUSTOM_BASE_URL))
        self._custom_model_edit.setText(settings.value(f"{_ASSISTANT_SETTINGS_PREFIX}/custom_model", ASSISTANT_CUSTOM_MODEL))
        self._custom_api_key_edit.setText(settings.value(f"{_ASSISTANT_SETTINGS_PREFIX}/custom_api_key", ASSISTANT_CUSTOM_API_KEY))

    def _on_save_assistant_settings(self):
        settings = QSettings()
        settings.setValue(f"{_ASSISTANT_SETTINGS_PREFIX}/provider", self._provider_combo.currentData())
        settings.setValue(f"{_ASSISTANT_SETTINGS_PREFIX}/claude_api_key", self._claude_api_key_edit.text().strip())
        settings.setValue(f"{_ASSISTANT_SETTINGS_PREFIX}/claude_model", self._claude_model_combo.currentData())
        settings.setValue(f"{_ASSISTANT_SETTINGS_PREFIX}/custom_base_url", self._custom_base_url_edit.text().strip())
        settings.setValue(f"{_ASSISTANT_SETTINGS_PREFIX}/custom_model", self._custom_model_edit.text().strip())
        settings.setValue(f"{_ASSISTANT_SETTINGS_PREFIX}/custom_api_key", self._custom_api_key_edit.text().strip())
        self._append_chat_html(f"<i style='color:{AZUL_MEDIO};'>Configuración guardada.</i>")
        self._update_chat_visibility()

    def _on_refresh_claude_models(self):
        """Reemplaza la lista fija por la lista real y vigente que reporta la API de Anthropic."""
        api_key = self._claude_api_key_edit.text().strip()
        if not api_key:
            self._append_chat_html(
                f"<span style='color:#c0392b;'>Ingresa primero tu clave de API de Claude para poder "
                f"consultar los modelos disponibles.</span>"
            )
            return

        current = self._claude_model_combo.currentData()
        self._refresh_models_button.setEnabled(False)
        QApplication.processEvents()
        try:
            from .assistant import llm_client
            models = llm_client.list_models(api_key)
        except Exception as e:
            self._append_chat_html(
                f"<span style='color:#c0392b;'>No se pudo actualizar la lista de modelos: {e}</span>"
            )
            return
        finally:
            self._refresh_models_button.setEnabled(True)

        if not models:
            return

        self._claude_model_combo.blockSignals(True)
        self._claude_model_combo.clear()
        for label, model_id in models:
            self._claude_model_combo.addItem(label, model_id)
        self._claude_model_combo.blockSignals(False)
        index = self._claude_model_combo.findData(current)
        self._claude_model_combo.setCurrentIndex(index if index >= 0 else 0)
        self._append_chat_html(
            f"<i style='color:{AZUL_MEDIO};'>Lista de modelos actualizada ({len(models)} modelos disponibles).</i>"
        )

    def _get_provider_config(self):
        provider = self._provider_combo.currentData()
        if provider == "claude":
            return {
                "provider": "claude",
                "api_key": self._claude_api_key_edit.text().strip(),
                "model": self._claude_model_combo.currentData() or ASSISTANT_CLAUDE_MODEL,
            }
        return {
            "provider": "openai_compatible",
            "base_url": self._custom_base_url_edit.text().strip(),
            "model": self._custom_model_edit.text().strip(),
            "api_key": self._custom_api_key_edit.text().strip() or None,
        }

    def _is_provider_configured(self, provider_config):
        if provider_config["provider"] == "claude":
            return bool(provider_config.get("api_key"))
        return bool(provider_config.get("base_url"))

    def _update_chat_visibility(self):
        """Muestra el chat solo si ya hay un proveedor configurado; mientras
        tanto se ve el aviso. Al activarse por primera vez, colapsa la caja de
        configuración del modelo para darle más espacio al chat."""
        configured = self._is_provider_configured(self._get_provider_config())
        self._chat_container.setVisible(configured)
        self._chat_placeholder.setVisible(not configured)
        if configured:
            self._model_settings_checkbox.setChecked(False)

    # ------------------------------------------------------------------
    # Asistente: conexión externa (MCP para Claude Desktop / Claude Code)
    # ------------------------------------------------------------------

    def _on_toggle_mcp_server(self, checked):
        if checked:
            from .assistant.qgis_actions import QgisMCPServer
            if self._mcp_server is None:
                self._mcp_server = QgisMCPServer(
                    port=self._mcp_port_spin.value(),
                    iface=self.iface,
                    on_clients_changed=self._on_mcp_clients_changed,
                )
            else:
                self._mcp_server.port = self._mcp_port_spin.value()

            ok = self._mcp_server.start()
            if ok:
                self._mcp_port_spin.setEnabled(False)
                self._mcp_toggle_button.setText("Desactivar conexión")
                self._mcp_status_label.setText(
                    f"Escuchando en localhost:{self._mcp_server.port} (0 clientes conectados)."
                )
                self._recordar_estado_conexion(True)
            else:
                self._mcp_toggle_button.setChecked(False)
                self._mcp_status_label.setText(f"No se pudo iniciar: {self._mcp_server.start_error}")
        else:
            if self._mcp_server is not None:
                self._mcp_server.stop()
            self._mcp_port_spin.setEnabled(True)
            self._mcp_toggle_button.setText("Activar conexión")
            self._mcp_status_label.setText("Detenido.")
            self._recordar_estado_conexion(False)

    def _recordar_estado_conexion(self, activa):
        """Guarda si la conexión externa quedó encendida, y en qué puerto."""
        settings = QSettings()
        settings.setValue(f"{_ASSISTANT_SETTINGS_PREFIX}/mcp_activa", activa)
        settings.setValue(f"{_ASSISTANT_SETTINGS_PREFIX}/mcp_puerto", self._mcp_port_spin.value())

    def _restaurar_conexion_externa(self):
        """Vuelve a levantar la conexión externa si estaba encendida.

        Recargar el plugin apaga el servidor (se cierra el socket al descargar),
        y tener que reactivarlo a mano después de cada recarga interrumpe el
        trabajo. Se restaura el último estado elegido por el usuario: si la había
        apagado, sigue apagada."""
        settings = QSettings()
        if not settings.value(f"{_ASSISTANT_SETTINGS_PREFIX}/mcp_activa", False, type=bool):
            return
        puerto = settings.value(f"{_ASSISTANT_SETTINGS_PREFIX}/mcp_puerto", 9876, type=int)
        self._mcp_port_spin.setValue(puerto)
        self._mcp_toggle_button.setChecked(True)
        self._on_toggle_mcp_server(True)

    def _on_mcp_clients_changed(self, count):
        """Actualiza el estado mostrado. El puente externo abre una conexión
        nueva y corta por cada consulta (no deja una sesión abierta), así que
        pasar por "0 clientes" entre una consulta y otra es normal — se agrega
        la hora de la última vez que hubo actividad real para que quede claro
        que sí se está usando, aunque el contador esté en 0 en este instante."""
        if self._mcp_server is None or not self._mcp_server.running:
            return
        base = f"Escuchando en localhost:{self._mcp_server.port} ({count} cliente(s) conectado(s))."
        if count == 0 and getattr(self, "_mcp_last_activity", None):
            base += f" Última actividad: {self._mcp_last_activity}."
        elif count > 0:
            import datetime
            self._mcp_last_activity = datetime.datetime.now().strftime("%H:%M:%S")
        self._mcp_status_label.setText(base)

    def _find_system_python(self):
        """Busca un Python "normal" instalado en este equipo (NO el de QGIS,
        que trae la instalación de pip rota), para configurar Claude Code.

        No usa shutil.which("python") porque, corriendo dentro de QGIS, el
        PATH del propio proceso puede hacer que encuentre el Python de QGIS
        por error — en cambio, busca directamente en las rutas típicas de
        instalación de python.org bajo el usuario actual (portable: usa
        os.path.expanduser, no una ruta fija de una persona en particular).
        """
        import glob

        home = os.path.expanduser("~")
        patterns = [
            os.path.join(home, "AppData", "Local", "Programs", "Python", "Python3*", "python.exe"),
            r"C:\Python3*\python.exe",
        ]
        candidates = []
        for pattern in patterns:
            candidates.extend(glob.glob(pattern))
        candidates.sort(reverse=True)  # versiones más nuevas primero (Python313 > Python312...)
        return candidates[0] if candidates else None

    def _wrap_hint(self, text):
        """Inserta espacios de ancho cero (invisibles) después de cada '\\' o
        '/', para que las rutas largas se puedan partir en varias líneas en
        vez de generar una barra de desplazamiento horizontal. Es solo para
        mostrar en pantalla — nunca se usa para el texto que se copia."""
        return text.replace("\\", "\\\u200b").replace("/", "/\u200b")

    def _copy_button_html(self, key, label="📋 Copiar"):
        """Un enlace estilizado como botoncito. Al hacer clic, el texto real
        (guardado en self._mcp_copyable_texts[key]) se copia al portapapeles —
        ver _on_mcp_instructions_link_clicked."""
        return (
            f'<a href="copy:{key}" style="background-color:{AZUL_MEDIO}; color:#ffffff; '
            f'padding:5px 14px; border-radius:4px; text-decoration:none; font-weight:bold; '
            f'font-size:12px;">{label}</a>'
        )

    def _on_mcp_instructions_link_clicked(self, url):
        href = url.toString()
        if not href.startswith("copy:"):
            return
        text = self._mcp_copyable_texts.get(href[len("copy:"):])
        if not text:
            return
        QApplication.clipboard().setText(text)
        self._mcp_copy_feedback.setText("✅ Copiado. Ahora pégalo donde te indica el paso (Ctrl+V).")
        self._mcp_copy_feedback.setVisible(True)
        # Se oculta solo a los 3 segundos, para que no se quede tapando el
        # texto de las instrucciones para siempre.
        QTimer.singleShot(3000, lambda: self._mcp_copy_feedback.setVisible(False))

    def _on_show_mcp_instructions(self):
        """Genera, en lenguaje simple y paso a paso, las instrucciones para
        conectar Claude Desktop y Claude Code con QGIS — pensadas para alguien
        que nunca ha programado y solo tiene Claude instalado. Se muestran en
        su propio cuadro (no en el chat, que puede estar oculto si el modelo de
        IA aún no está configurado), con botones para copiar cada comando.

        Claude Desktop instala esto como una "extensión" (archivo .mcpb) con un
        clic — no requiere editar ningún archivo de configuración a mano ni
        instalar Python aparte (Claude Desktop lo hace por su cuenta). Claude
        Code sigue usando el flujo de terminal de siempre, con un Python normal."""
        port = self._mcp_port_spin.value()
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        bridge_dir = os.path.normpath(os.path.join(plugin_dir, "..", "mcp_bridge"))
        bridge_script = os.path.join(bridge_dir, "toolkitpalm_bridge.py")
        requirements_file = os.path.join(bridge_dir, "requirements.txt")
        mcpb_file = os.path.join(bridge_dir, "toolkitpalm.mcpb")

        # Ruta a un Python "normal" (NO el de QGIS), solo para Claude Code —
        # se busca en este mismo equipo, para que sirva en cualquier instalación,
        # no solo en la de quien escribió el plugin.
        python_hint = self._find_system_python() or (
            "<no se encontró Python en este equipo — instálalo desde python.org "
            "y vuelve a generar estas instrucciones>"
        )

        pip_cmd = f'pip install -r "{requirements_file}"'
        claude_code_cmd = f'claude mcp add toolkitpalm -- "{python_hint}" "{bridge_script}" --port {port}'

        self._mcp_copyable_texts = {
            "mcpb_path": mcpb_file,
            "pip": pip_cmd,
            "code": claude_code_cmd,
        }
        self._mcp_copy_feedback.setVisible(False)

        def code_block(text):
            return (
                '<div style="background-color:#f4f4f4; border:1px solid #e0e0e0; border-radius:6px; '
                'padding:10px; margin:6px 0; font-family:Consolas,monospace; font-size:12px; '
                f'white-space:pre-wrap;">{html.escape(self._wrap_hint(text))}</div>'
            )

        instructions_html = f"""
        <div style="font-family: Segoe UI, Arial, sans-serif; font-size: 13px; line-height: 1.55;">

        <p><b>¿Qué es esto?</b> Es una forma de que Claude (la app de escritorio, o la de
        terminal) pueda ver y controlar QGIS directamente cuando tú se lo pidas por chat,
        usando tu cuenta de Claude normal — sin pagar aparte por una clave de API. Se
        configura una sola vez.</p>

        <p><b>Paso 1 — Activa la conexión aquí en QGIS</b><br>
        Justo arriba de este texto, presiona el botón <b>"Activar conexión"</b>. Debe quedar
        encendido cada vez que quieras hablarle a QGIS desde Claude (si cierras QGIS, al
        volver a abrirlo tendrás que presionarlo otra vez).</p>

        <p><b>Paso 2 — Conecta tu Claude</b><br>
        Sigue solo la parte que uses (puedes hacer las dos si usas ambas apps). Son procesos
        distintos y no dependen uno del otro.</p>

        <p>🖥️ <b>Claude Desktop</b> (la app de escritorio, con ventana de chat) — no necesitas
        instalar nada de Python para esta:</p>
        <ol>
        <li>Copia la ruta del archivo de abajo (ya la generamos por ti).</li>
        </ol>
        {code_block(mcpb_file)}
        <p>{self._copy_button_html("mcpb_path", "📋 Copiar esta ruta")}</p>
        <ol start="2">
        <li>Abre Claude Desktop.</li>
        <li>Ve al menú (arriba a la izquierda) → <b>Configuración</b> → <b>Extensiones</b>.</li>
        <li>Busca una opción de <b>"Configuración avanzada"</b> (puede estar como un enlace o un
        ícono, generalmente abajo de la página). Ábrela.</li>
        <li>Dentro debería aparecer una sección para desarrolladores con un botón
        <b>"Instalar extensión..."</b>. Al presionarlo se abrirá el explorador de archivos de
        Windows: pega ahí la ruta que copiaste (Ctrl+V en la barra de direcciones) y presiona
        Enter, o navega hasta ese archivo y ábrelo.</li>
        <li>Confirma la instalación cuando Claude Desktop te lo pida.</li>
        </ol>
        <p style="color:#888888; font-size:11px;"><i>Nota: la app de Claude Desktop cambia de
        vez en cuando de apariencia — si no encuentras exactamente estos textos, cuéntame qué
        ves en la pantalla y te digo dónde hacer clic.</i></p>

        <p>⌨️ <b>Claude Code</b> (el que usas en una terminal) — esta parte sí necesita un
        Python normal instalado en tu computador (ya detectamos uno):</p>
        <ol>
        <li>Presiona la tecla de Windows (la del logo), escribe <b>PowerShell</b> y presiona
        Enter para abrirlo. Es una ventana donde se escriben instrucciones de texto.</li>
        <li>Copia este comando (instala el puente, una sola vez), pégalo con <b>Ctrl+V</b> y
        presiona Enter:</li>
        </ol>
        {code_block(pip_cmd)}
        <p>{self._copy_button_html("pip", "📋 Copiar este comando")}</p>
        <ol start="3">
        <li>Espera a que termine (si no aparece la palabra "error" en rojo, salió bien). Luego
        copia y pega este otro comando, y presiona Enter:</li>
        </ol>
        {code_block(claude_code_cmd)}
        <p>{self._copy_button_html("code", "📋 Copiar este comando")}</p>

        <p><b>Paso 3 — Pruébalo</b><br>
        Abre una conversación nueva en Claude y escríbele algo como:
        <i>"¿qué capas tengo cargadas en QGIS?"</i>. Si te responde con información real de tu
        proyecto, ¡ya quedó funcionando!</p>

        </div>
        """

        self._mcp_instructions_browser.setHtml(instructions_html)
        self._mcp_instructions_browser.setVisible(True)

    # ------------------------------------------------------------------
    # Asistente: chat
    # ------------------------------------------------------------------

    def _append_chat_html(self, html):
        self._chat_history.append(html)

    _ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB por archivo

    def _on_attach_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Adjuntar imágenes o documentos",
            "",
            "Imágenes y documentos (*.png *.jpg *.jpeg *.webp *.gif *.pdf)",
        )
        if not paths:
            return
        for path in paths:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > self._ATTACHMENT_MAX_BYTES:
                self._append_chat_html(
                    f"<span style='color:#c0392b;'>{os.path.basename(path)} pesa más de 20 MB, no se adjuntó.</span>"
                )
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext == ".pdf":
                kind, media_type = "document", "application/pdf"
            elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                kind, media_type = "image", (mimetypes.guess_type(path)[0] or "image/png")
            else:
                continue
            self._pending_attachments.append({"path": path, "kind": kind, "media_type": media_type})
        self._update_attachments_label()

    def _on_clear_attachments(self):
        self._pending_attachments = []
        self._update_attachments_label()

    def _update_attachments_label(self):
        if not self._pending_attachments:
            self._attachments_label.setText("")
            self._clear_attachments_button.setVisible(False)
            return
        names = ", ".join(os.path.basename(a["path"]) for a in self._pending_attachments)
        self._attachments_label.setText(f"📎 {names}")
        self._clear_attachments_button.setVisible(True)

    def _on_send_chat_message(self):
        text = self._chat_input.text().strip()
        if not text and not self._pending_attachments:
            return

        provider_config = self._get_provider_config()
        if provider_config["provider"] == "claude" and not provider_config.get("api_key"):
            self._append_chat_html(f"<span style='color:#c0392b;'>Falta la clave de API de Claude. Configúrala arriba.</span>")
            return
        if provider_config["provider"] == "openai_compatible" and not provider_config.get("base_url"):
            self._append_chat_html(f"<span style='color:#c0392b;'>Falta la URL del servidor de tu modelo. Configúrala arriba.</span>")
            return

        attachments_payload = []
        for att in self._pending_attachments:
            try:
                with open(att["path"], "rb") as f:
                    data_b64 = base64.b64encode(f.read()).decode("ascii")
            except OSError as e:
                self._append_chat_html(
                    f"<span style='color:#c0392b;'>No se pudo leer {os.path.basename(att['path'])}: {e}</span>"
                )
                continue
            attachments_payload.append({
                "kind": att["kind"],
                "media_type": att["media_type"],
                "data_b64": data_b64,
                "filename": os.path.basename(att["path"]),
            })

        self._chat_input.clear()
        self._pending_attachments = []
        self._update_attachments_label()

        display_text = text or "(sin texto)"
        if attachments_payload:
            names = ", ".join(a["filename"] for a in attachments_payload)
            display_text += f" <i>[adjunto: {names}]</i>"
        self._append_chat_html(f"<b style='color:{AZUL_OSCURO};'>Tú:</b> {display_text}")
        self._append_chat_html(f"<i style='color:{AZUL_MEDIO};'>Pensando...</i>")
        self._chat_send_button.setEnabled(False)
        self._chat_input.setEnabled(False)
        QApplication.processEvents()

        try:
            if self._assistant is None:
                from .assistant.agent import Assistant
                self._assistant = Assistant(self.iface)
            reply = self._assistant.send_message(text, provider_config, attachments=attachments_payload)
        except Exception as e:
            reply = None
            self._append_chat_html(f"<span style='color:#c0392b;'>Error: {e}</span>")
        finally:
            self._chat_send_button.setEnabled(True)
            self._chat_input.setEnabled(True)
            self._chat_input.setFocus()

        if reply is not None:
            self._append_chat_html(f"<b style='color:{AZUL_MEDIO};'>Asistente:</b> {reply}")

    # ------------------------------------------------------------------
    # Navegación entre tarjetas
    # ------------------------------------------------------------------

    def _on_search_changed(self, text):
        needle = text.strip().lower()
        for key, label in _TOOLS:
            button = self._buttons[key]
            button.setVisible(needle in label.lower())

    def _on_card_clicked(self, key):
        if key == "llm":
            self._current_key = key
            self._stacked.setCurrentWidget(self._llm_page)
            return

        page = self._get_or_create_page(key)
        if page is None:
            # No se aceptaron los términos y condiciones: no navegar, restaurar selección previa.
            previous_button = self._buttons.get(self._current_key)
            if previous_button:
                previous_button.setChecked(True)
            else:
                self._button_group.setExclusive(False)
                self._buttons[key].setChecked(False)
                self._button_group.setExclusive(True)
            return

        self._current_key = key
        self._stacked.setCurrentWidget(page)
        # Refresca el saldo al cambiar de herramienta (consulta en segundo plano,
        # no bloquea): así se ve actualizado después de correr un procesamiento.
        if client_identity.is_logged_in():
            self._refresh_credits_label()

    def _get_or_create_page(self, key):
        if key in self._pages:
            return self._pages[key]

        instance = None
        if key == "detector":
            from .detector.dockwidget import DetectorPalmasDockWidget
            instance = DetectorPalmasDockWidget(self.iface)
        elif key == "segmentador":
            from .segmentador.dockwidget import SegmentadorPalmasDockWidget
            instance = SegmentadorPalmasDockWidget(self.iface)
        elif key == "optimizador":
            from .optimizador.dockwidget import OptimizadorAcopiosDockWidget
            instance = OptimizadorAcopiosDockWidget(self.iface)
        elif key == "fotogrametria":
            from .fotogrametria.dockwidget import FotogrametriaDockWidget
            instance = FotogrametriaDockWidget(self.iface)
        elif key == "diseno_plantacion":
            from .diseno_plantacion.dockwidget import DisenoPlantacionDockWidget
            instance = DisenoPlantacionDockWidget(self.iface)

        if instance is None:
            return None

        if not getattr(instance, "terms_accepted", False):
            instance.deleteLater()
            return None

        # Las herramientas heredan de QDockWidget y se construyen sin padre, así que
        # Qt las trata como ventanas de nivel superior y puede crearles una ventana
        # nativa propia. Si el dock se embebe tal cual como página, esa ventana nativa
        # queda huérfana en una posición fuera de la pantalla y el sistema operativo
        # entrega los clics ahí: los botones del panel se vuelven inertes aunque se
        # dibujen bien. Por eso se embebe su contenido (un QWidget plano) y el dock
        # queda solo como dueño de la lógica y de las conexiones de señales.
        contenido = instance.widget()

        # Cada herramienta va dentro de un área con scroll: sus paneles son más
        # altos que el espacio disponible (sobre todo el Detector con todos sus
        # botones), y sin esto el contenido de abajo queda cortado sin forma de
        # llegar a él.
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page.setWidget(contenido)      # reparenta el contenido y lo saca del dock

        self._stacked.addWidget(page)
        page._owner_dock = instance    # evita que el dock (y su lógica) se recolecte

        self._pages[key] = page
        return page
