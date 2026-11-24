#!/usr/bin/env python3
"""
Genera el reporte ejecutivo de ventas consolidado desde las 6 tiendas de Shopify
configuradas en .env, inyecta los datos en plantilla.html y abre el reporte
resultante en el navegador.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

API_VERSION = "2026-01"
CREATED_AT_MIN = "2025-01-01T00:00:00Z"
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
PLANTILLA_PATH = BASE_DIR / "plantilla.html"
PLACEHOLDER = "__DATOS_SHOPIFY__"


def cargar_env(ruta: Path) -> dict[str, str]:
    """Lee un archivo .env simple (KEY=VALUE) sin dependencias externas."""
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró el archivo {ruta}")
    env: dict[str, str] = {}
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        clave = clave.strip()
        valor = valor.strip().strip('"').strip("'")
        env[clave] = valor
    return env


def leer_tiendas(env: dict[str, str]) -> list[dict[str, str]]:
    """Extrae TIENDA_1 a TIENDA_6 del diccionario de variables de entorno."""
    tiendas: list[dict[str, str]] = []
    for i in range(1, 7):
        prefijo = f"TIENDA_{i}_"
        nombre = env.get(f"{prefijo}NOMBRE", "").strip()
        url = env.get(f"{prefijo}URL", "").strip()
        client_id = env.get(f"{prefijo}ID", "").strip()
        client_secret = env.get(f"{prefijo}SECRET", "").strip()
        if not all([nombre, url, client_id, client_secret]):
            raise ValueError(
                f"Faltan variables para TIENDA_{i} en .env "
                f"(se requieren {prefijo}NOMBRE, {prefijo}URL, {prefijo}ID y {prefijo}SECRET)."
            )
        url = url.replace("https://", "").replace("http://", "").rstrip("/")
        tiendas.append(
            {
                "nombre": nombre,
                "url": url,
                "id": client_id,
                "secret": client_secret,
            }
        )
    return tiendas


def obtener_token(url: str, client_id: str, client_secret: str) -> str:
    """Solicita un access_token temporal usando client_credentials."""
    endpoint = f"https://{url}/admin/oauth/access_token"
    payload = json.dumps(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ReporteShopifyMensual/2.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Error HTTP {exc.code} al solicitar token para {url}: {detalle}"
        ) from exc

    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"La respuesta de {url} no incluyó 'access_token': {data}")
    return token


def extraer_siguiente_pagina(link_header: str | None) -> str | None:
    """Extrae la URL con rel=\"next\" del header Link de Shopify."""
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return match.group(1) if match else None


def descargar_ordenes(url: str, token: str) -> list[dict]:
    """Descarga todas las órdenes desde CREATED_AT_MIN hasta hoy usando paginación Link."""
    ordenes: list[dict] = []
    params = urllib.parse.urlencode(
        {
            "status": "any",
            "limit": 250,
            "created_at_min": CREATED_AT_MIN,
        }
    )
    next_url: str | None = (
        f"https://{url}/admin/api/{API_VERSION}/orders.json?{params}"
    )
    pagina = 0

    while next_url:
        pagina += 1
        req = urllib.request.Request(
            next_url,
            headers={
                "X-Shopify-Access-Token": token,
                "Accept": "application/json",
                "User-Agent": "ReporteShopifyMensual/2.0",
            },
            method="GET",
        )

        intentos = 0
        while True:
            intentos += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    link_header = resp.headers.get("Link")
                    break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and intentos <= 5:
                    espera = float(exc.headers.get("Retry-After", "2"))
                    print(f"    [429] Límite de tasa en {url}, esperando {espera:.1f}s...")
                    time.sleep(espera)
                    continue
                detalle = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Error HTTP {exc.code} descargando órdenes de {url} (página {pagina}): {detalle}"
                ) from exc

        lote = body.get("orders", [])
        ordenes.extend(lote)
        next_url = extraer_siguiente_pagina(link_header)

    return ordenes


def parsear_fecha_orden(created_at: str) -> datetime | None:
    """Convierte el string ISO-8601 created_at de Shopify a datetime."""
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def consolidar_ordenes(ordenes_por_tienda: list[tuple[str, list[dict]]]) -> dict:
    """
    Consolida todas las tiendas en el formato RAW:
    {
      "meta": {"fecha_corte": "YYYY-MM-DD", "mes_actual": int, "anio_actual": int},
      "p": {sku: [titulo, marca, sku]},
      "r": [[anio, mes, sku, venta_bruta, unidades]],
      "o": [[anio, mes, marca, num_pedidos, venta_bruta, descuentos_totales,
             unidades, clientes_nuevos, clientes_recurrentes]],
      "dow": [[anio, marca, dia_semana_0_a_6, num_pedidos, venta_bruta]]
    }
    """
    productos: dict[str, list[str]] = {}
    agregado_sku: dict[tuple[int, int, str], list[float | int]] = defaultdict(
        lambda: [0.0, 0]
    )
    agregado_ordenes: dict[tuple[int, int, str], list[float | int]] = defaultdict(
        lambda: [0, 0.0, 0.0, 0, 0, 0]
    )
    agregado_dow: dict[tuple[int, str, int], list[float | int]] = defaultdict(
        lambda: [0, 0.0]
    )

    for marca, ordenes in ordenes_por_tienda:
        # Filtrar canceladas y de prueba, y ordenar cronológicamente para rastrear recompra
        validas: list[tuple[datetime, dict]] = []
        for orden in ordenes:
            if orden.get("cancelled_at") is not None:
                continue
            if orden.get("test") is True:
                continue
            dt = parsear_fecha_orden(orden.get("created_at") or "")
            if dt is None:
                continue
            validas.append((dt, orden))

        validas.sort(key=lambda par: par[0])
        clientes_vistos: set[str] = set()

        for dt, orden in validas:
            anio = dt.year
            mes = dt.month
            dow = dt.weekday()  # 0=Lunes ... 6=Domingo

            # Rastrear cliente nuevo vs recurrente por tienda
            customer = orden.get("customer") or {}
            customer_id = customer.get("id")
            if customer_id is not None:
                cid = str(customer_id)
                if cid in clientes_vistos:
                    es_recurrente = True
                else:
                    es_recurrente = False
                    clientes_vistos.add(cid)
            else:
                es_recurrente = False

            line_items = orden.get("line_items") or []
            venta_orden = 0.0
            unidades_orden = 0

            for item in line_items:
                sku = (item.get("sku") or "").strip()
                if not sku:
                    sku = str(
                        item.get("variant_id")
                        or item.get("product_id")
                        or item.get("id")
                        or f"{marca}-SIN-SKU"
                    )
                titulo = (item.get("title") or item.get("name") or sku).strip()
                unidades = int(item.get("quantity") or 0)
                precio = float(item.get("price") or 0.0)
                venta_bruta = precio * unidades

                if sku not in productos:
                    productos[sku] = [titulo, marca, sku]

                clave_sku = (anio, mes, sku)
                agregado_sku[clave_sku][0] += venta_bruta
                agregado_sku[clave_sku][1] += unidades

                venta_orden += venta_bruta
                unidades_orden += unidades

            if not line_items:
                venta_orden = float(
                    orden.get("total_line_items_price")
                    or orden.get("subtotal_price")
                    or 0.0
                )

            descuentos_orden = float(orden.get("total_discounts") or 0.0)

            clave_ord = (anio, mes, marca)
            o_row = agregado_ordenes[clave_ord]
            o_row[0] += 1
            o_row[1] += venta_orden
            o_row[2] += descuentos_orden
            o_row[3] += unidades_orden
            if es_recurrente:
                o_row[5] += 1
            else:
                o_row[4] += 1

            clave_dow = (anio, marca, dow)
            agregado_dow[clave_dow][0] += 1
            agregado_dow[clave_dow][1] += venta_orden

    registros_r = [
        [anio, mes, sku, round(float(venta), 2), int(unidades)]
        for (anio, mes, sku), (venta, unidades) in sorted(agregado_sku.items())
    ]

    registros_o = [
        [
            anio,
            mes,
            marca,
            int(num_ped),
            round(float(venta), 2),
            round(float(desc), 2),
            int(uds),
            int(nuevos),
            int(recurr),
        ]
        for (anio, mes, marca), (
            num_ped,
            venta,
            desc,
            uds,
            nuevos,
            recurr,
        ) in sorted(agregado_ordenes.items())
    ]

    registros_dow = [
        [anio, marca, dow, int(num_ped), round(float(venta), 2)]
        for (anio, marca, dow), (num_ped, venta) in sorted(agregado_dow.items())
    ]

    ahora = datetime.now()
    return {
        "meta": {
            "fecha_corte": ahora.strftime("%Y-%m-%d"),
            "mes_actual": ahora.month,
            "anio_actual": ahora.year,
        },
        "p": productos,
        "r": registros_r,
        "o": registros_o,
        "dow": registros_dow,
    }


def main() -> int:
    ahora = datetime.now()
    print(
        f"=== Generador de Reporte Ejecutivo Shopify "
        f"(Corte al {ahora.strftime('%Y-%m-%d')}) ==="
    )
    env = cargar_env(ENV_PATH)
    tiendas = leer_tiendas(env)

    ordenes_por_tienda: list[tuple[str, list[dict]]] = []
    for tienda in tiendas:
        nombre = tienda["nombre"]
        url = tienda["url"]
        print(f"\n[{nombre}] Conectando con {url}...")
        token = obtener_token(url, tienda["id"], tienda["secret"])
        print(f"[{nombre}] Token obtenido. Descargando órdenes desde {CREATED_AT_MIN}...")
        ordenes = descargar_ordenes(url, token)
        fechas = sorted(
            o.get("created_at", "")
            for o in ordenes
            if o.get("created_at")
        )
        if fechas:
            mas_antigua = fechas[0][:10]
            mas_reciente = fechas[-1][:10]
            print(
                f"[{nombre}] {len(ordenes)} órdenes descargadas "
                f"| Más antigua: {mas_antigua} | Más reciente: {mas_reciente}"
            )
        else:
            print(f"[{nombre}] 0 órdenes descargadas.")
        ordenes_por_tienda.append((nombre, ordenes))

    print("\nConsolidando métricas ejecutivas...")
    raw_data = consolidar_ordenes(ordenes_por_tienda)
    print(
        f"Consolidación completa: {len(raw_data['p'])} SKUs, "
        f"{len(raw_data['r'])} filas de producto, "
        f"{len(raw_data['o'])} bloques mensuales por marca."
    )

    if not PLANTILLA_PATH.exists():
        raise FileNotFoundError(f"No se encontró {PLANTILLA_PATH}")

    plantilla = PLANTILLA_PATH.read_text(encoding="utf-8")
    if PLACEHOLDER not in plantilla:
        raise RuntimeError(
            f"No se encontró el marcador {PLACEHOLDER} en {PLANTILLA_PATH.name}"
        )

    json_datos = json.dumps(raw_data, ensure_ascii=False, separators=(",", ":"))
    html_final = plantilla.replace(PLACEHOLDER, json_datos, 1)

    fecha_hoy = ahora.strftime("%Y-%m-%d")
    salida_path = BASE_DIR / f"Reporte_Shopify_{fecha_hoy}.html"
    salida_path.write_text(html_final, encoding="utf-8")
    index_path = BASE_DIR / "index.html"
    index_path.write_text(html_final, encoding="utf-8")
    print(f"\nReporte generado: {salida_path}")
    print(f"Copia actualizada: {index_path}")

    uri = salida_path.resolve().as_uri()
    print("Abriendo reporte en el navegador...")
    webbrowser.open(uri)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
