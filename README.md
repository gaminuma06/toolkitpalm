# ToolkitPalm

Complemento de QGIS para plantaciones de palma de aceite. Reúne en un solo panel
tres herramientas que trabajan sobre ortomosaicos de dron:

- **Detector de Palmas** — cuenta e identifica cada palma, con edición manual de
  los puntos y numeración automática por líneas de siembra.
- **Segmentador de Palmas** — aísla el dosel foliar y calcula índices
  espectrales (NDVI, NDWI, SAVI y otros).
- **Optimizador de Acopios** — ubica los puntos de acopio óptimos sobre la red
  de vías.

Desarrollado por **Ing. Adán Arias**.

## Instalación

Desde QGIS: *Complementos → Administrar e instalar complementos*, buscar
**ToolkitPalm**.

Requiere QGIS 3.16 o posterior. No hace falta GPU ni instalar librerías de
aprendizaje profundo: el procesamiento pesado corre en un servidor.

## Cómo funciona

El complemento prepara los datos localmente (recorta la ortoimagen al lote,
ajusta la resolución, divide los lotes grandes en bloques) y envía cada bloque a
un servicio de procesamiento. Los resultados vuelven como capas que se cargan en
el proyecto.

El uso requiere iniciar sesión con una cuenta de Google y consumir créditos de
procesamiento: **1 crédito por cada 5 hectáreas**, cobrados por lote completo y
no por bloque. Cada cuenta nueva recibe un crédito de prueba gratuito.

## Qué cubre este repositorio

Acá está el código del complemento, publicado bajo **GPL v2 o posterior** (ver
[LICENSE](toolkitpalm/LICENSE)).

Los modelos de detección y segmentación, el código del servidor y la
infraestructura de procesamiento **no forman parte de esta distribución**: son
propiedad del autor y se ejecutan en su propio servidor. Ver
[AVISO_LEGAL.txt](toolkitpalm/AVISO_LEGAL.txt).

## Desarrollo

```
toolkitpalm/
  shell.py              Panel único: barra lateral, login, créditos
  google_auth.py        Inicio de sesión (OAuth 2.0 con PKCE)
  client_identity.py    Identidad del cliente y cabeceras de autenticación
  config.py             URLs y endpoints (sin secretos)
  detector/             Detector de Palmas
  segmentador/          Segmentador de Palmas
  optimizador/          Optimizador de Acopios
```

Para trabajar sobre el código, copiar la carpeta `toolkitpalm/` dentro de
`python/plugins/` del perfil de QGIS y recargar el complemento.

El complemento **no contiene ningún secreto**: el `client_id` de Google es
público por diseño y el intercambio de credenciales lo completa el servidor.
Si necesitas apuntar a otro backend, copia `config_secrets.example.py` a
`config_secrets.py` (ese archivo está en `.gitignore` y nunca se publica).

Para generar el paquete distribuible:

```
python empaquetar_plugin.py
```

Verifica que no se cuele ninguna credencial y que el `.zip` no supere los 20 MB
que acepta el repositorio de QGIS.

## Soporte

Reportes de errores y sugerencias: pestaña *Issues* de este repositorio.

Contacto: ingarias9006@gmail.com
