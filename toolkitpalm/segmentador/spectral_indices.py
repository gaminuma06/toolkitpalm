# -*- coding: utf-8 -*-
"""
Índices espectrales para palmas segmentadas.
Asume orden de bandas estándar dron: B1=R, B2=G, B3=B, B4=NIR (si existe).
"""
import logging
import numpy as np

logger = logging.getLogger(__name__)

# Cada entrada: name, description, min_bands, formula callable, color_stops (5 hex P5→P95), url, vmin/vmax (fallback)
SPECTRAL_INDICES = {
    "NDVI": {
        "name": "NDVI — Índice de Vegetación de Diferencia Normalizada",
        "description": "Vigor foliar. Valores altos = vegetación sana.",
        "min_bands": 4,
        "formula": lambda b: _safe_div(b["nir"].astype(float) - b["r"].astype(float),
                                        b["nir"].astype(float) + b["r"].astype(float)),
        "color_stops": ["#8B0000", "#FF8C00", "#FFD700", "#7FFF00", "#006400"],
        "url": "https://www.usgs.gov/special-topics/remote-sensing-phenology/science/ndvi-foundation-remote-sensing-phenology",
        "vmin": -1.0, "vmax": 1.0,
    },
    "NDWI": {
        "name": "NDWI — Índice de Agua de Diferencia Normalizada",
        "description": "Contenido de agua en la vegetación.",
        "min_bands": 4,
        "formula": lambda b: _safe_div(b["g"].astype(float) - b["nir"].astype(float),
                                        b["g"].astype(float) + b["nir"].astype(float)),
        "color_stops": ["#8B4513", "#FF6B6B", "#FFD700", "#87CEEB", "#0066FF"],
        "url": "https://eos.com/make-an-analysis/ndwi/",
        "vmin": -1.0, "vmax": 1.0,
    },
    "SAVI": {
        "name": "SAVI — Índice de Vegetación Ajustado al Suelo",
        "description": "NDVI corregido por exposición de suelo (L=0.5).",
        "min_bands": 4,
        "formula": lambda b: 1.5 * _safe_div(
            b["nir"].astype(float) - b["r"].astype(float),
            b["nir"].astype(float) + b["r"].astype(float) + 0.5,
        ),
        "color_stops": ["#D2691E", "#F4A460", "#FFD700", "#7FFF00", "#228B22"],
        "url": "https://www.usgs.gov/landsat-missions/landsat-soil-adjusted-vegetation-index",
        "vmin": -1.5, "vmax": 1.5,
    },
    "EVI": {
        "name": "EVI — Índice de Vegetación Mejorado",
        "description": "Reducción de efectos atmosféricos y de suelo.",
        "min_bands": 4,
        "formula": lambda b: 2.5 * _safe_div(
            b["nir"].astype(float) - b["r"].astype(float),
            b["nir"].astype(float) + 6.0 * b["r"].astype(float)
            - 7.5 * b["b"].astype(float) + 1.0,
        ),
        "color_stops": ["#800000", "#FF8C00", "#FFD700", "#00C853", "#003D00"],
        "url": "https://en.wikipedia.org/wiki/Enhanced_vegetation_index",
        "vmin": -2.5, "vmax": 2.5,
    },
    "GLI": {
        "name": "GLI — Índice de Luz Verde",
        "description": "Vigor foliar usando solo RGB.",
        "min_bands": 3,
        "formula": lambda b: _safe_div(
            2.0 * b["g"].astype(float) - b["r"].astype(float) - b["b"].astype(float),
            2.0 * b["g"].astype(float) + b["r"].astype(float) + b["b"].astype(float),
        ),
        "color_stops": ["#8B4513", "#FF8C00", "#FFD700", "#9ACD32", "#228B22"],
        "url": "https://www.indexdatabase.de/db/i-single.php?id=375",
        "vmin": -1.0, "vmax": 1.0,
    },
    "ExG": {
        "name": "ExG — Exceso de Verde",
        "description": "Diferencia del verde respecto a rojo y azul.",
        "min_bands": 3,
        "formula": lambda b: (2.0 * b["g"].astype(float)
                               - b["r"].astype(float)
                               - b["b"].astype(float)),
        "color_stops": ["#8B0000", "#FF6347", "#FFD700", "#32CD32", "#006400"],
        "url": "https://docs.plantcv.org/en/latest/spectral_index/",
        "vmin": None, "vmax": None,
    },
    "VARI": {
        "name": "VARI — Índice de Resistencia Atmosférica Visible",
        "description": "Fracción de vegetación verde visible, robusto a iluminación.",
        "min_bands": 3,
        "formula": lambda b: _safe_div(
            b["g"].astype(float) - b["r"].astype(float),
            b["g"].astype(float) + b["r"].astype(float) - b["b"].astype(float),
        ),
        "color_stops": ["#8B4513", "#FF6B6B", "#FFA500", "#FFD700", "#00AA00"],
        "url": "https://www.indexdatabase.de/db/i-single.php?id=356",
        "vmin": -1.0, "vmax": 1.0,
    },
    "GRVI": {
        "name": "GRVI — Índice de Vegetación Verde-Rojo",
        "description": "Relación verde/rojo, sensible a cloroplastos.",
        "min_bands": 3,
        "formula": lambda b: _safe_div(
            b["g"].astype(float) - b["r"].astype(float),
            b["g"].astype(float) + b["r"].astype(float),
        ),
        "color_stops": ["#FF0000", "#FF9500", "#FFD700", "#7FFF00", "#00AA00"],
        "url": "https://www.indexdatabase.de/db/i-single.php?id=317",
        "vmin": -1.0, "vmax": 1.0,
    },
}


def _safe_div(num, denom):
    """División segura: retorna NaN donde el denominador es 0."""
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.where(denom == 0, np.nan, num / denom)
    return result.astype(np.float32)


def compute_index_stats(valid_pixels):
    """
    Calcula estadísticas descriptivas sobre los píxeles válidos (no-NaN) de un
    índice espectral, usando los mismos 5 percentiles (P5/P25/P50/P75/P95) que
    definen la rampa de color de 5 colores que se ve en el mapa.

    Devuelve también los 4 puntos medios entre percentiles consecutivos
    (boundaries) que dividen los datos en 5 bandas — una por cada color de la
    rampa — y el % de píxeles que cae en cada una.
    """
    valid_pixels = np.asarray(valid_pixels, dtype=np.float64)
    if valid_pixels.size == 0:
        return None

    p5, p25, p50, p75, p95 = np.percentile(valid_pixels, [5, 25, 50, 75, 95]).tolist()
    boundaries = [
        (p5 + p25) / 2.0,
        (p25 + p50) / 2.0,
        (p50 + p75) / 2.0,
        (p75 + p95) / 2.0,
    ]

    n = valid_pixels.size
    edges = [-np.inf] + boundaries + [np.inf]
    pct_bins = [
        float(np.count_nonzero((valid_pixels > edges[i]) & (valid_pixels <= edges[i + 1]))) / n * 100.0
        for i in range(5)
    ]

    return {
        "mean": float(np.mean(valid_pixels)),
        "median": float(np.median(valid_pixels)),
        "std": float(np.std(valid_pixels)),
        "min": float(np.min(valid_pixels)),
        "max": float(np.max(valid_pixels)),
        "percentiles": [p5, p25, p50, p75, p95],
        "boundaries": boundaries,
        "pct_bins": pct_bins,
    }


def sample_ramp_color(value, percentiles, color_stops):
    """
    Interpola linealmente el color RGB de la rampa de 5 colores (color_stops,
    anclados en percentiles) en el valor dado. Fuera de
    [percentiles[0], percentiles[-1]] se usa el color del extremo más
    cercano (clamp). Retorna una tupla (r, g, b) de enteros 0-255.
    """
    def hex_to_rgb(hex_str):
        hex_str = hex_str.lstrip("#")
        return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))

    if value <= percentiles[0]:
        return hex_to_rgb(color_stops[0])
    if value >= percentiles[-1]:
        return hex_to_rgb(color_stops[-1])

    for i in range(len(percentiles) - 1):
        p_low, p_high = percentiles[i], percentiles[i + 1]
        if p_low <= value <= p_high:
            span = p_high - p_low
            fraction = (value - p_low) / span if span > 0 else 0.0
            rgb_low = hex_to_rgb(color_stops[i])
            rgb_high = hex_to_rgb(color_stops[i + 1])
            return tuple(
                round(rgb_low[c] + fraction * (rgb_high[c] - rgb_low[c]))
                for c in range(3)
            )
    return hex_to_rgb(color_stops[-1])


def _build_range_mask(arr, low, high, nodata=None):
    """
    Retorna una máscara booleana (misma forma que arr), True donde el píxel
    es válido (no-NaN, no-nodata) y su valor cumple low <= valor <= high.
    """
    valid_mask = ~np.isnan(arr)
    if nodata is not None and not np.isnan(nodata):
        valid_mask &= (arr != np.float32(nodata))
    in_range = (arr >= low) & (arr <= high)
    return valid_mask & in_range


def generate_range_raster(index_tiff_path, output_path, low, high):
    """
    Genera un GeoTIFF de 1 banda que conserva solo los píxeles de
    index_tiff_path cuyo valor cumple low <= valor <= high (rango continuo
    elegido en el RangeSliderWidget). El resto queda en NaN.
    """
    from osgeo import gdal

    if low > high:
        raise ValueError(f"low ({low}) no puede ser mayor que high ({high})")

    ds = gdal.Open(index_tiff_path)
    if ds is None:
        raise Exception(f"No se pudo abrir el raster de índice: {index_tiff_path}")

    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    width = ds.RasterXSize
    height = ds.RasterYSize
    geo_transform = ds.GetGeoTransform()
    projection = ds.GetProjection()
    ds = None

    keep_mask = _build_range_mask(arr, low, high, nodata)
    result = np.where(keep_mask, arr, np.nan).astype(np.float32)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(output_path, width, height, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(geo_transform)
    out_ds.SetProjection(projection)
    out_band = out_ds.GetRasterBand(1)
    out_band.WriteArray(result)
    out_band.SetNoDataValue(float("nan"))
    out_ds.FlushCache()
    out_ds = None

    logger.info(f"Capa de rango [{low}, {high}] generada en {output_path}")
    return output_path


# Umbrales aproximados (literatura general de índices de vegetación) usados
# para clasificar el promedio del índice en "alto/medio/bajo". Son un cálculo
# de programación fijo (no un modelo de IA): la IA de este plugin solo detecta
# y segmenta palmas; esta clasificación es matemática simple sobre el resultado.
_DIAGNOSIS_THRESHOLDS = {
    "NDVI": (0.6, 0.3),
    "SAVI": (0.5, 0.25),
    "EVI": (0.5, 0.2),
    "GLI": (0.15, 0.0),
    "ExG": (20.0, 0.0),
    "VARI": (0.2, 0.0),
    "GRVI": (0.15, 0.0),
    "NDWI": (0.0, -0.2),
}

_DIAGNOSIS_TEXTS = {
    "vigor": {
        "alto": "Buen vigor general: la mayor parte del follaje segmentado se ve saludable.",
        "medio": "Vigor moderado: hay zonas saludables, pero también áreas con menor vigor que conviene revisar en campo.",
        "bajo": "Vigor bajo en general: buena parte del área segmentada muestra señales de estrés, poca clorofila o follaje escaso.",
    },
    "agua": {
        "alto": "Buen contenido de agua en el follaje segmentado.",
        "medio": "Contenido de agua moderado en el follaje.",
        "bajo": "Posible déficit hídrico: el follaje muestra señales de tener poca agua. Vale la pena revisar riego o lluvias recientes.",
    },
}

_INDEX_CATEGORY = {
    "NDVI": "vigor", "SAVI": "vigor", "EVI": "vigor", "GLI": "vigor",
    "ExG": "vigor", "VARI": "vigor", "GRVI": "vigor", "NDWI": "agua",
}


def diagnose_index(index_key, stats):
    """
    Clasifica el promedio del índice en alto/medio/bajo usando umbrales fijos
    (programación simple, no un modelo de IA) y arma un diagnóstico en texto
    plano. Incluye qué porcentaje del área quedó en la banda más baja de la
    rampa de color, para dar una idea de cuánta superficie está en la zona de
    alerta. Retorna un string listo para mostrar al usuario.
    """
    categoria = _INDEX_CATEGORY.get(index_key, "vigor")
    alto_umbral, medio_umbral = _DIAGNOSIS_THRESHOLDS.get(index_key, (stats["percentiles"][3], stats["percentiles"][1]))
    mean = stats["mean"]

    if mean >= alto_umbral:
        nivel = "alto"
    elif mean >= medio_umbral:
        nivel = "medio"
    else:
        nivel = "bajo"

    texto = _DIAGNOSIS_TEXTS[categoria][nivel]

    # pct_bins[0] y [1] son las dos bandas de color más bajas de la rampa (P5-P25 y P25-P50)
    pct_bajo = stats["pct_bins"][0] + stats["pct_bins"][1]

    resultado = (
        f"Diagnóstico automático — nivel {nivel.upper()}\n\n"
        f"{texto}\n\n"
        f"Aproximadamente {pct_bajo:.0f}% del área segmentada cae en el rango más bajo "
        f"del índice (color rojo/naranja del mapa).\n\n"
        f"Promedio calculado: {mean:.3f}\n\n"
        "Nota: este diagnóstico es una clasificación automática por umbrales "
        "numéricos fijos sobre el resultado del índice — no usa inteligencia "
        "artificial (la IA del plugin solo detecta y segmenta palmas) y no "
        "reemplaza una inspección técnica en campo."
    )
    return resultado


def detect_raster_bands(tiff_path):
    """Retorna el número de bandas del raster. Retorna 0 si falla."""
    try:
        from osgeo import gdal
        ds = gdal.Open(tiff_path)
        if ds:
            n = ds.RasterCount
            ds = None
            logger.info(f"detect_raster_bands: {tiff_path} -> {n} bandas")
            return n
    except Exception as e:
        logger.warning(f"No se pudo detectar bandas de {tiff_path}: {e}")
    return 0


def detect_spectral_band_count(tiff_path):
    """
    Retorna el número de bandas espectrales (excluye alfa y bandas extra ambiguas).

    Reglas:
    - Si la última banda tiene GCI_AlphaBand: excluir.
    - Si es un TIFF de exactamente 4 bandas donde las 3 primeras son R/G/B
      y la 4ª es GCI_Undefined: asumir que la 4ª es alfa (no NIR) → retorna 3.
    - En todos los demás casos: contar las bandas que no sean GCI_AlphaBand.
    """
    try:
        from osgeo import gdal
        ds = gdal.Open(tiff_path)
        if ds is None:
            return 0
        n = ds.RasterCount
        if n == 0:
            ds = None
            return 0

        interps = [ds.GetRasterBand(i).GetColorInterpretation() for i in range(1, n + 1)]
        ds = None

        # Caso 1: la última banda es alfa explícita
        if interps[-1] == gdal.GCI_AlphaBand:
            count = n - 1
            logger.info(f"detect_spectral_band_count: {tiff_path} -> {count} (banda alfa en posición {n})")
            return count

        # Caso 2: TIFF de 4 bandas con primeras 3 = R/G/B y banda 4 sin definición
        rgb_set = {gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand}
        if n == 4 and set(interps[:3]) == rgb_set and interps[3] == gdal.GCI_Undefined:
            logger.info(f"detect_spectral_band_count: {tiff_path} -> 3 (banda 4 indefinida asumida alfa en TIFF RGB+X)")
            return 3

        # Caso general: contar todo excepto GCI_AlphaBand
        count = sum(1 for c in interps if c != gdal.GCI_AlphaBand)
        logger.info(f"detect_spectral_band_count: {tiff_path} -> {count} bandas espectrales")
        return count
    except Exception as e:
        logger.warning(f"No se pudo detectar bandas espectrales de {tiff_path}: {e}")
        return 0


def get_available_indices(band_count):
    """
    Retorna lista de (key, display_name) de índices disponibles según band_count.
    band_count >= 4: todos; band_count == 3: solo RGB.
    """
    return [
        (key, info["name"])
        for key, info in SPECTRAL_INDICES.items()
        if band_count >= info["min_bands"]
    ]


def apply_spectral_index(tiff_path, index_key, output_path):
    """
    Calcula el índice espectral sobre tiff_path y guarda en output_path (float32, 1 banda).
    Asume orden de bandas: B1=R, B2=G, B3=B, B4=NIR.
    Retorna output_path si tiene éxito, lanza Exception si falla.
    """
    from osgeo import gdal

    if index_key not in SPECTRAL_INDICES:
        raise ValueError(f"Índice desconocido: {index_key}")

    info = SPECTRAL_INDICES[index_key]

    ds = gdal.Open(tiff_path)
    if ds is None:
        raise Exception(f"No se pudo abrir el raster: {tiff_path}")

    band_count = ds.RasterCount
    if band_count < info["min_bands"]:
        ds = None
        raise ValueError(
            f"{index_key} requiere al menos {info['min_bands']} bandas; "
            f"el raster tiene {band_count}."
        )

    r = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    g = ds.GetRasterBand(2).ReadAsArray().astype(np.float32)
    b = ds.GetRasterBand(3).ReadAsArray().astype(np.float32)
    nir = ds.GetRasterBand(4).ReadAsArray().astype(np.float32) if band_count >= 4 else None

    nodata_val = ds.GetRasterBand(1).GetNoDataValue()
    nodata_mask = (r == 0) & (g == 0) & (b == 0)
    if nodata_val is not None:
        nodata_mask |= (r == np.float32(nodata_val))

    bands = {"r": r, "g": g, "b": b}
    if nir is not None:
        bands["nir"] = nir

    result = info["formula"](bands).astype(np.float32)
    result[nodata_mask] = np.nan

    width = ds.RasterXSize
    height = ds.RasterYSize
    geo_transform = ds.GetGeoTransform()
    projection = ds.GetProjection()
    ds = None

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(output_path, width, height, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(geo_transform)
    out_ds.SetProjection(projection)
    out_band = out_ds.GetRasterBand(1)
    out_band.WriteArray(result)
    out_band.SetNoDataValue(float("nan"))
    out_ds.FlushCache()
    out_ds = None

    logger.info(f"Índice {index_key} calculado y guardado en {output_path}")
    return output_path
