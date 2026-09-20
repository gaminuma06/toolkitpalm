# -*- coding: utf-8 -*-
"""
Catálogo de nombres de acciones que expone QgisMCPServer (ver qgis_actions.py),
usado para construir la lista de "tools" que se le pasa al modelo de IA.

La lista de nombres es estática (no se introspecciona el diccionario interno de
_dispatch para no modificar ese código vendorizado): 118 acciones genéricas de
QGIS adaptadas de qgis-mcp + 3 propias de ToolkitPalm al final.

Para la mayoría de las acciones se usa una descripción genérica tomada de la
primera línea del docstring del método y un esquema de parámetros abierto
("cualquier propiedad"), ya que documentar a mano los ~118 esquemas exactos no
es viable en esta primera versión: los modelos actuales infieren razonablemente
bien los parámetros a partir del nombre + la descripción. Las 3 herramientas
propias sí tienen esquema curado, al ser las más importantes para ToolkitPalm.
"""

_HANDLER_NAMES = [
    "ping",
    "get_qgis_info",
    "load_project",
    "get_project_info",
    "execute_code",
    "add_vector_layer",
    "add_raster_layer",
    "get_layers",
    "remove_layer",
    "zoom_to_layer",
    "get_layer_features",
    "execute_processing",
    "save_project",
    "render_map_base64",
    "create_new_project",
    "get_field_statistics",
    "set_layer_visibility",
    "get_canvas_extent",
    "set_canvas_extent",
    "get_raster_info",
    "get_layer_info",
    "get_layer_schema",
    "batch",
    "add_features",
    "update_features",
    "delete_features",
    "set_layer_style",
    "select_features",
    "get_selection",
    "clear_selection",
    "create_memory_layer",
    "list_processing_algorithms",
    "get_algorithm_help",
    "create_processing_model",
    "find_layer",
    "list_layouts",
    "export_layout",
    "get_message_log",
    "list_plugins",
    "get_plugin_info",
    "reload_plugin",
    "get_layer_tree",
    "create_layer_group",
    "move_layer_to_group",
    "set_layer_property",
    "get_layer_extent",
    "get_project_variables",
    "set_project_variable",
    "validate_expression",
    "get_setting",
    "set_setting",
    "get_canvas_screenshot",
    "transform_coordinates",
    "diagnose",
    "get_active_layer",
    "set_active_layer",
    "get_canvas_scale",
    "set_canvas_scale",
    "get_layer_labeling",
    "set_layer_labeling",
    "get_layer_crs",
    "set_layer_crs",
    "get_bookmarks",
    "add_bookmark",
    "remove_bookmark",
    "get_map_themes",
    "add_map_theme",
    "remove_map_theme",
    "apply_map_theme",
    "set_project_crs",
    "add_web_layer",
    "add_table_join",
    "add_field",
    "delete_field",
    "rename_field",
    "apply_style_qml",
    "save_style_qml",
    "create_layout",
    "add_layout_map",
    "list_processing_models",
    "run_model",
    "get_processing_providers",
    "execute_processing_batch",
    "raster_calculator",
    "zonal_statistics",
    "sample_raster_values",
    "export_layer",
    "field_calculator",
    "get_unique_values",
    "spatial_join",
    "get_layout_info",
    "add_layout_label",
    "add_layout_legend",
    "add_layout_scalebar",
    "add_layout_picture",
    "add_layout_table",
    "configure_atlas",
    "export_atlas",
    "remove_layout",
    "execute_sql",
    "evaluate_expression",
    "identify_features",
    "duplicate_layer",
    "set_layer_order",
    "get_3d_screenshot",
    "list_connections",
    "list_connection_tables",
    "add_layer_from_connection",
    "import_layer_to_connection",
    "execute_connection_sql",
    "start_editing",
    "commit_edits",
    "rollback_edits",
    "get_edit_status",
    "undo_edits",
    "redo_edits",
    "update_feature_geometry",
    "set_raster_style",
    "run_detector",
    "run_segmentador",
    "run_optimizador",
]

_GENERIC_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}

# Esquemas curados. Empezó solo con las 3 herramientas propias de ToolkitPalm;
# se fueron sumando las genéricas de uso frecuente porque un `properties: {}`
# vacío hace que algunos clientes MCP (p.ej. Claude Code) descarten cualquier
# argumento no declarado explícitamente, aunque additionalProperties sea True.
_CUSTOM_SCHEMAS = {
    "execute_code": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Código Python a ejecutar en el intérprete de QGIS"},
        },
        "required": ["code"],
    },
    "add_raster_layer": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ruta al archivo ráster (.tif, .ecw, etc.)"},
            "name": {"type": "string", "description": "Nombre de la capa en el proyecto (opcional)"},
            "provider": {"type": "string", "description": "Proveedor de datos, por defecto 'gdal' (opcional)"},
        },
        "required": ["path"],
    },
    "add_vector_layer": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ruta al archivo vectorial (.shp, .gpkg, etc.)"},
            "name": {"type": "string", "description": "Nombre de la capa en el proyecto (opcional)"},
            "provider": {"type": "string", "description": "Proveedor de datos, por defecto 'ogr' (opcional)"},
        },
        "required": ["path"],
    },
    "get_raster_info": {
        "type": "object",
        "properties": {
            "layer_id": {"type": "string", "description": "ID de la capa ráster"},
        },
        "required": ["layer_id"],
    },
    "get_layer_extent": {
        "type": "object",
        "properties": {
            "layer_id": {"type": "string", "description": "ID de la capa"},
        },
        "required": ["layer_id"],
    },
    "get_layer_info": {
        "type": "object",
        "properties": {
            "layer_id": {"type": "string", "description": "ID de la capa"},
        },
        "required": ["layer_id"],
    },
    "zoom_to_layer": {
        "type": "object",
        "properties": {
            "layer_id": {"type": "string", "description": "ID de la capa a la que hacer zoom"},
        },
        "required": ["layer_id"],
    },
    "remove_layer": {
        "type": "object",
        "properties": {
            "layer_id": {"type": "string", "description": "ID de la capa a eliminar"},
        },
        "required": ["layer_id"],
    },
    "get_canvas_screenshot": {
        "type": "object",
        "properties": {},
    },
    "render_map_base64": {
        "type": "object",
        "properties": {
            "width": {"type": "integer", "description": "Ancho en píxeles (opcional, por defecto 800)"},
            "height": {"type": "integer", "description": "Alto en píxeles (opcional, por defecto 600)"},
            "path": {"type": "string", "description": "Ruta donde guardar el PNG (opcional)"},
        },
    },
    "set_canvas_extent": {
        "type": "object",
        "properties": {
            "xmin": {"type": "number", "description": "Coordenada X mínima"},
            "ymin": {"type": "number", "description": "Coordenada Y mínima"},
            "xmax": {"type": "number", "description": "Coordenada X máxima"},
            "ymax": {"type": "number", "description": "Coordenada Y máxima"},
        },
        "required": ["xmin", "ymin", "xmax", "ymax"],
    },
    "run_detector": {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Ruta a la ortoimagen .tif/.tiff"},
            "lotes_path": {"type": "string", "description": "Ruta al shapefile .shp de lotes"},
            "lot_id": {"type": "string", "description": "FID del lote a procesar (número como texto)"},
        },
        "required": ["image_path", "lotes_path", "lot_id"],
    },
    "run_segmentador": {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Ruta a la ortoimagen .tif/.tiff"},
            "lotes_path": {"type": "string", "description": "Ruta al shapefile .shp de lotes"},
            "lot_id": {"type": "string", "description": "FID del lote a procesar (número como texto)"},
            "slice_height": {"type": "integer", "description": "Alto de recorte en píxeles (opcional)"},
            "slice_width": {"type": "integer", "description": "Ancho de recorte en píxeles (opcional)"},
            "overlap_ratio": {"type": "number", "description": "Solape entre recortes, 0-1 (opcional)"},
            "confidence_threshold": {"type": "number", "description": "Umbral de confianza, 0-1 (opcional)"},
        },
        "required": ["image_path", "lotes_path", "lot_id"],
    },
    "run_optimizador": {
        "type": "object",
        "properties": {
            "lots_layer_name": {"type": "string", "description": "Nombre de la capa de lotes ya cargada en QGIS"},
            "roads_layer_name": {"type": "string", "description": "Nombre de la capa de carreteras ya cargada en QGIS"},
            "acopios_layer_name": {"type": "string", "description": "Nombre de la capa de acopios actuales (opcional)"},
            "p": {"type": "integer", "description": "Número de acopios a ubicar (opcional)"},
            "road_interval": {"type": "number", "description": "Distancia en metros entre candidatos (opcional)"},
        },
        "required": ["lots_layer_name", "roads_layer_name"],
    },
}


def build_tool_catalog(server):
    """
    Construye la lista de tools (name/description/parameters) a partir de una
    instancia de QgisMCPServer, para pasarla a llm_client.send(...).
    """
    tools = []
    for name in _HANDLER_NAMES:
        method = getattr(server, name, None)
        if method is None:
            continue
        docstring = (method.__doc__ or name).strip()
        description = docstring.split("\n")[0].strip() if docstring else name
        schema = _CUSTOM_SCHEMAS.get(name, _GENERIC_SCHEMA)
        tools.append({"name": name, "description": description, "parameters": schema})
    return tools
