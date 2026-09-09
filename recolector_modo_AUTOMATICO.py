#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AhorraAcá - Recolector MODO V9.7 - últimos rubros confirmados

ETAPA 2
-------
1. Descubre promociones desde la API que utiliza el buscador web de MODO.
2. Descarga cada ficha.
3. Extrae campos básicos.
4. Guarda/actualiza en Supabase como "pendiente".
5. Valida y publica automáticamente al finalizar el recolector.

REQUISITOS
----------
pip install requests beautifulsoup4

VARIABLES DE ENTORNO
--------------------
SUPABASE_URL=https://TU-PROYECTO.supabase.co
SUPABASE_SECRET_KEY=TU_SECRET_KEY

IMPORTANTE:
La SECRET KEY se usa solamente en este proceso externo.
NUNCA debe ir dentro de la app Android.
"""

import os
import re
import sys
import json
import time
import hashlib
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "").strip()

FUENTE = "MODO"

# Argentina usa UTC-3 de forma permanente actualmente.
# Usamos offset fijo para no depender del paquete tzdata en Windows/servidores.
TZ_ARGENTINA = timezone(timedelta(hours=-3))

MODO_API_URL = "https://www.modo.com.ar/promos/api/rewards/slots"

MODO_SLOTS = ",".join([
    "web-modo-hub-carrousel_principal",
    "web-modo-hub-destacadas",
    "web-modo-hub-supermercados",
    "web-modo-hub-exclusivas-online",
    "web-modo-hub-promos-financiacion",
    "web-modo-hub-mas-promos",
]) + ","

MODO_PAGE_LIMIT = 50

# Datos estructurados de cada promo obtenidos desde la API de MODO.
MODO_CARDS_POR_URL: dict[str, dict] = {}


HUBS = [
    "https://www.modo.com.ar/promos",
    "https://promoshub.modo.com.ar/",
]

SITEMAPS = [
    "https://www.modo.com.ar/sitemap.xml",
    "https://www.modo.com.ar/sitemap_index.xml",
    "https://www.modo.com.ar/sitemap-0.xml",
]

TIMEOUT = (8, 15)
REINTENTOS = 2

HEADERS_WEB = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AhorraAcaPromoCollector/0.1; "
        "+https://www.modo.com.ar/)"
    ),
    "Accept-Language": "es-AR,es;q=0.9",
}

MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

DIAS = {
    "lunes": 1,
    "martes": 2,
    "miercoles": 3,
    "miércoles": 3,
    "jueves": 4,
    "viernes": 5,
    "sabado": 6,
    "sábado": 6,
    "domingo": 7,
}


def validar_configuracion():
    if not SUPABASE_URL:
        raise RuntimeError("Falta SUPABASE_URL.")
    if not SUPABASE_SECRET_KEY:
        raise RuntimeError("Falta SUPABASE_SECRET_KEY.")


def normalizar(texto: str) -> str:
    texto = texto or ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower()
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()



def clave_clasificacion(texto: str) -> str:
    """
    Convierte cualquier texto a una clave alfanumérica continua.
    Elimina espacios, guiones, signos y caracteres invisibles.

    Ejemplo:
        "REPUESTOS CHELY" -> "repuestoschely"
    """
    texto = texto or ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(
        c
        for c in texto
        if not unicodedata.combining(c)
    )
    texto = texto.lower()
    return "".join(
        c
        for c in texto
        if c.isalnum()
    )


def limpiar_texto(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    texto = soup.get_text(" ", strip=True)
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def pedir_con_reintentos(
    metodo: str,
    url: str,
    **kwargs,
):
    """
    Evita que una URL lenta o caída congele todo el recolector.
    Hace pocos reintentos y luego devuelve el error al bucle principal,
    que continúa con la promoción siguiente.
    """
    ultimo_error = None

    for intento in range(
        1,
        REINTENTOS + 2,
    ):
        try:
            return requests.request(
                metodo,
                url,
                timeout=TIMEOUT,
                **kwargs,
            )

        except requests.RequestException as exc:
            ultimo_error = exc
            print(
                f"   Reintento {intento}/{REINTENTOS + 1} "
                f"por error de red: {exc}"
            )

    raise RuntimeError(
        f"No respondió después de {REINTENTOS + 1} intentos: {url}"
    ) from ultimo_error


def descargar(url: str) -> str:
    respuesta = pedir_con_reintentos(
        "GET",
        url,
        headers=HEADERS_WEB,
    )
    respuesta.raise_for_status()
    return respuesta.text


def normalizar_url_promo(url: str) -> str | None:
    if not url:
        return None

    url = url.strip()
    url = url.replace("\u002F", "/")
    url = url.replace("\\/", "/")
    url = url.replace("&amp;", "&")

    if url.startswith("/"):
        url = urljoin(
            "https://www.modo.com.ar",
            url,
        )

    if not url.startswith("http"):
        return None

    if "/promos-banco/" not in url:
        return None

    url = url.split("#")[0].split("?")[0]

    return url.rstrip("/")


def extraer_urls_desde_texto(
    contenido: str,
    base_url: str,
) -> set[str]:
    encontrados = set()

    # 1) Enlaces HTML normales.
    try:
        soup = BeautifulSoup(
            contenido,
            "html.parser",
        )

        for a in soup.find_all(
            "a",
            href=True,
        ):
            href = a.get(
                "href",
                "",
            ).strip()

            url = normalizar_url_promo(
                urljoin(
                    base_url,
                    href,
                )
            )

            if url:
                encontrados.add(
                    url
                )

    except Exception:
        pass

    # 2) Rutas escondidas dentro de JSON / scripts / estado de la SPA.
    variantes = [
        contenido,
        contenido.replace("\\/", "/"),
        contenido.replace("\u002F", "/"),
    ]

    patrones = [
        r'https?://(?:www\.)?modo\.com\.ar/promos-banco/[A-Za-z0-9_\-%]+',
        r'["\'](/promos-banco/[A-Za-z0-9_\-%]+)["\']',
        r'(/promos-banco/[A-Za-z0-9_\-%]+)',
    ]

    for texto in variantes:
        for patron in patrones:
            for match in re.findall(
                patron,
                texto,
                flags=re.I,
            ):
                if isinstance(
                    match,
                    tuple,
                ):
                    match = match[0]

                url = normalizar_url_promo(
                    urljoin(
                        base_url,
                        match,
                    )
                )

                if url:
                    encontrados.add(
                        url
                    )

    return encontrados


def recorrer_sitemap(
    url: str,
    visitados: set[str],
    profundidad: int = 0,
) -> set[str]:
    encontrados = set()

    if (
        url in visitados
        or profundidad > 3
    ):
        return encontrados

    visitados.add(
        url
    )

    try:
        contenido = descargar(
            url
        )

    except Exception as exc:
        print(
            f"[INFO] Sitemap no disponible {url}: {exc}"
        )
        return encontrados

    # Por si el sitemap contiene URLs en texto aunque el XML tenga detalles raros.
    encontrados.update(
        extraer_urls_desde_texto(
            contenido,
            url,
        )
    )

    try:
        raiz = ET.fromstring(
            contenido
        )

    except Exception:
        return encontrados

    # Ignoramos namespaces usando sólo el nombre local de la etiqueta.
    locs = []

    for elemento in raiz.iter():
        nombre = elemento.tag.split(
            "}"
        )[-1].lower()

        if (
            nombre == "loc"
            and elemento.text
        ):
            locs.append(
                elemento.text.strip()
            )

    for loc in locs:
        promo = normalizar_url_promo(
            loc
        )

        if promo:
            encontrados.add(
                promo
            )
            continue

        if (
            "sitemap" in loc.lower()
            and loc not in visitados
        ):
            encontrados.update(
                recorrer_sitemap(
                    loc,
                    visitados,
                    profundidad + 1,
                )
            )

    return encontrados


def descubrir_urls_promos() -> list[str]:
    """
    Consulta la misma API que utiliza el buscador web de MODO.

    La respuesta contiene:
    - data.cards
    - slug de cada promoción
    - calculated_status
    - metadata.pagination

    Recorremos todas las páginas y construimos la URL pública
    /promos-banco/{slug} para que el analizador existente lea
    posteriormente la ficha completa.
    """

    encontrados = set()

    pagina = 1
    total_paginas = None

    print(
        "Consultando API de promociones de MODO..."
    )

    while True:
        params = {
            "slots": MODO_SLOTS,
            "banks": "",
            "user_bank_ids": "",
            "limit": MODO_PAGE_LIMIT,
            "page": pagina,
            "search_text": "",
            "source": "web_modo",
            "origin": "web_modo",
            "fcalcstatus": (
                "running,"
                "finished_for_product,"
                "next_for_product"
            ),
            "fdoweeks": "",
            "fflow": "",
            "slot_info": "true",
            "categories": "",
        }

        headers = dict(
            HEADERS_WEB
        )
        headers["Accept"] = (
            "application/json, text/plain, */*"
        )
        headers["Referer"] = (
            "https://www.modo.com.ar/promos"
        )

        respuesta = pedir_con_reintentos(
            "GET",
            MODO_API_URL,
            params=params,
            headers=headers,
        )

        respuesta.raise_for_status()

        try:
            payload = respuesta.json()

        except Exception as exc:
            raise RuntimeError(
                "La API de MODO respondió algo que no es JSON."
            ) from exc

        data = (
            payload.get(
                "data",
                {},
            )
            if isinstance(
                payload,
                dict,
            )
            else {}
        )

        cards = data.get(
            "cards",
            [],
        )

        if not isinstance(
            cards,
            list,
        ):
            cards = []

        nuevos_en_pagina = 0
        activos_o_proximos = 0
        finalizados = 0

        for card in cards:
            if not isinstance(
                card,
                dict,
            ):
                continue

            estado = str(
                card.get(
                    "calculated_status",
                    "",
                )
                or ""
            ).upper()

            if estado == "FINISHED_FOR_PRODUCT":
                finalizados += 1
                continue

            activos_o_proximos += 1

            slug = str(
                card.get(
                    "slug",
                    "",
                )
                or ""
            ).strip()

            if not slug:
                continue

            # Evitar cualquier valor raro antes de formar la URL.
            slug = re.sub(
                r"[^A-Za-z0-9_\-%]+",
                "-",
                slug,
            ).strip("-")

            if not slug:
                continue

            url = (
                "https://www.modo.com.ar/"
                f"promos-banco/{slug}"
            )

            # Conservamos la tarjeta estructurada para que después
            # el analizador use bancos/tarjetas informados por MODO.
            MODO_CARDS_POR_URL[url] = card

            if url not in encontrados:
                encontrados.add(
                    url
                )
                nuevos_en_pagina += 1

        metadata = data.get(
            "metadata",
            {},
        )

        if not isinstance(
            metadata,
            dict,
        ):
            metadata = {}

        pagination = metadata.get(
            "pagination",
            {},
        )

        if not isinstance(
            pagination,
            dict,
        ):
            pagination = {}

        try:
            total_paginas = int(
                pagination.get(
                    "total_pages",
                    total_paginas or pagina,
                )
            )

        except Exception:
            total_paginas = (
                total_paginas
                or pagina
            )

        try:
            total_resultados = int(
                pagination.get(
                    "total_results",
                    len(cards),
                )
            )

        except Exception:
            total_resultados = len(
                cards
            )

        print(
            f"  Página {pagina}/{total_paginas}: "
            f"{len(cards)} resultados | "
            f"{activos_o_proximos} activos/próximos | "
            f"{finalizados} finalizados | "
            f"{nuevos_en_pagina} URLs nuevas"
        )

        if pagina == 1:
            print(
                f"  Total informado por MODO: "
                f"{total_resultados}"
            )

        # Condiciones de salida defensivas.
        if not cards:
            break

        if (
            total_paginas is not None
            and pagina >= total_paginas
        ):
            break

        pagina += 1

        # Protección por si la API devuelve una paginación incorrecta.
        if pagina > 200:
            print(
                "[WARN] Se alcanzó el límite de seguridad de 200 páginas."
            )
            break

        # Pequeña pausa entre páginas.
        time.sleep(
            0.25
        )

    return sorted(
        encontrados
    )


def extraer_porcentaje(texto: str) -> int | None:
    candidatos = re.findall(
        r"(?<!\d)(\d{1,3})\s*%",
        texto,
        flags=re.I,
    )

    for valor in candidatos:
        numero = int(valor)
        if 1 <= numero <= 100:
            return numero

    return None


def dinero_a_entero(valor: str) -> int | None:
    if not valor:
        return None

    limpio = re.sub(r"[^\d]", "", valor)
    if not limpio:
        return None

    return int(limpio)


def extraer_tope(texto: str):
    patron = re.search(
        r"Tope de reintegro\s+por\s+"
        r"(Usuario|Banco)?\s*"
        r"(?:por\s+)?"
        r"(mes|semana|promo|compra|transacci[oó]n)?"
        r"\s*\$?\s*([\d\.\,]+)",
        texto,
        flags=re.I,
    )

    if not patron:
        patron = re.search(
            r"tope(?: de reintegro| de devoluci[oó]n)?"
            r".{0,80}?\$?\s*([\d\.\,]+)",
            texto,
            flags=re.I,
        )

        if not patron:
            return None, None, None

        return (
            dinero_a_entero(patron.group(1)),
            None,
            None,
        )

    sujeto = normalizar(patron.group(1) or "")
    periodo = normalizar(patron.group(2) or "")
    monto = dinero_a_entero(patron.group(3))

    tipo_tope = None
    if sujeto == "usuario":
        tipo_tope = "por_usuario"
    elif sujeto == "banco":
        tipo_tope = "por_banco"

    periodo_tope = None
    if periodo == "mes":
        periodo_tope = "mensual"
    elif periodo == "semana":
        periodo_tope = "semanal"
    elif periodo == "promo":
        periodo_tope = "por_promo"
    elif periodo in ("compra", "transaccion"):
        periodo_tope = "por_compra"

    return monto, periodo_tope, tipo_tope


def extraer_monto_minimo(texto: str) -> int | None:
    patrones = [
        r"monto\s+m[ií]nimo(?:\s+de\s+compra)?\s*\$?\s*([\d\.\,]+)",
        r"monto\s+mayor\s+o\s+igual\s+a\s+\$?\s*([\d\.\,]+)",
        r"compra\s+m[ií]nima\s*\$?\s*([\d\.\,]+)",
    ]

    for patron in patrones:
        match = re.search(
            patron,
            texto,
            flags=re.I,
        )

        if match:
            return dinero_a_entero(
                match.group(1)
            )

    return None


def parsear_fecha_es(dia: str, mes: str, anio: str) -> str | None:
    numero_mes = MESES.get(normalizar(mes))

    if not numero_mes:
        return None

    try:
        fecha = datetime(
            int(anio),
            numero_mes,
            int(dia),
        ).date()

        return fecha.isoformat()

    except Exception:
        return None


def extraer_vigencia(texto: str):
    # Prioriza términos y condiciones cuando están disponibles.
    patrones = [
        (
            r"desde\s+(?:las\s+\d{1,2}:\d{2}\s+horas\s+del\s+d[ií]a\s+)?"
            r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})"
            r".{0,120}?"
            r"(?:hasta|al)\s+(?:las\s+\d{1,2}:\d{2}\s+horas\s+del\s+d[ií]a\s+)?"
            r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})"
        ),
        (
            r"Vigencia\s+Del\s+"
            r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+del\s+(\d{4})"
            r"\s+al\s+"
            r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+del\s+(\d{4})"
        ),
    ]

    candidatos = []

    for patron in patrones:
        for match in re.finditer(
            patron,
            texto,
            flags=re.I,
        ):
            desde = parsear_fecha_es(
                match.group(1),
                match.group(2),
                match.group(3),
            )
            hasta = parsear_fecha_es(
                match.group(4),
                match.group(5),
                match.group(6),
            )

            if desde and hasta:
                candidatos.append(
                    (desde, hasta)
                )

    if not candidatos:
        return None, None

    # Al haber discrepancias entre encabezado y TyC,
    # elegimos la vigencia más conservadora:
    # inicio más tardío + fin más temprano.
    desde = max(x[0] for x in candidatos)
    hasta = min(x[1] for x in candidatos)

    return desde, hasta


def extraer_dias(texto: str) -> list[int]:
    normal = normalizar(texto)

    if (
        "todos los dias" in normal
        or "usalo todos los dias" in normal
    ):
        return [1, 2, 3, 4, 5, 6, 7]

    dias = []

    # Los términos suelen ser más fiables que las letras visuales L M X...
    for nombre, numero in DIAS.items():
        nombre_n = normalizar(nombre)

        if re.search(
            rf"\b{re.escape(nombre_n)}s?\b",
            normal,
        ):
            dias.append(numero)

    return sorted(set(dias))



def extraer_contexto_condiciones(texto: str) -> str:
    """
    Reduce el texto de la página a las zonas donde suelen estar
    las condiciones reales de la promoción.

    Evita tomar bancos, tarjetas o marcas que aparezcan en menús,
    footer, carruseles o recomendaciones de otras promociones.
    """
    if not texto:
        return ""

    texto = re.sub(r"\s+", " ", texto).strip()
    normal = normalizar(texto)

    marcadores = [
        "terminos y condiciones",
        "terminos y condiciones de la promocion",
        "condiciones de la promocion",
        "vigencia",
        "tope de reintegro",
        "monto minimo",
        "medio de pago",
        "tarjetas",
    ]

    posiciones = []

    for marcador in marcadores:
        pos = normal.find(
            normalizar(marcador)
        )
        if pos >= 0:
            posiciones.append(pos)

    if not posiciones:
        # Si no encontramos una sección clara, usamos solamente
        # una porción inicial razonable, nunca toda la página.
        return texto[:7000]

    inicio = max(
        0,
        min(posiciones) - 700,
    )

    fin = min(
        len(texto),
        inicio + 9000,
    )

    return texto[inicio:fin]


def contiene_patron_banco(texto_normal: str, banco: str) -> bool:
    """
    Exige que el banco aparezca en un contexto relacionado con la promo.
    No alcanza con que el nombre esté suelto en cualquier parte.
    """
    b = re.escape(
        normalizar(banco)
    )

    patrones = [
        rf"\b{b}\b.{0,100}\b(?:visa|mastercard|debito|credito|tarjeta|modo|clientes?)\b",
        rf"\b(?:visa|mastercard|debito|credito|tarjeta|modo|clientes?)\b.{0,100}\b{b}\b",
        rf"\b(?:banco|clientes? de|usuarios? de|emitidas? por|con)\s+{b}\b",
    ]

    return any(
        re.search(
            patron,
            texto_normal,
            flags=re.I,
        )
        for patron in patrones
    )


def promo_es_generica_o_incompleta(promo: dict) -> tuple[bool, str]:
    titulo = normalizar(
        promo.get(
            "titulo",
            "",
        )
    )

    comercio = normalizar(
        promo.get(
            "comercio_detectado",
            "",
        )
    )

    genericos = {
        "",
        "promociones modo",
        "promociones",
        "modo",
        "beneficios modo",
        "beneficios",
    }

    if titulo in genericos or comercio in genericos:
        return True, "título/comercio genérico"

    if promo.get(
        "porcentaje_detectado"
    ) is None:
        return True, "sin porcentaje"

    if not promo.get(
        "vigencia_desde_detectada"
    ):
        return True, "sin fecha de inicio"

    if not promo.get(
        "vigencia_hasta_detectada"
    ):
        return True, "sin fecha de finalización"

    if not promo.get(
        "dias_semana_detectados"
    ):
        return True, "sin días de aplicación"

    # MODO tiene que estar presente como billetera/canal base.
    billeteras = set(
        promo.get(
            "billeteras_detectadas",
            []
        )
    )

    medios = set(
        promo.get(
            "medios_detectados",
            []
        )
    )

    if "modo" not in billeteras and "modo" not in medios:
        return True, "sin medio MODO"

    return False, ""




BANCO_ALIAS = {
    "supervielle": "supervielle",
    "banco supervielle": "supervielle",
    "macro": "macro",
    "banco macro": "macro",
    "hipotecario": "hipotecario",
    "banco hipotecario": "hipotecario",
    "galicia": "galicia",
    "banco galicia": "galicia",
    "bbva": "bbva",
    "santander": "santander",
    "banco santander": "santander",
    "icbc": "icbc",
    "credicoop": "credicoop",
    "banco credicoop": "credicoop",
    "nacion": "nacion",
    "nación": "nacion",
    "banco nacion": "nacion",
    "banco nación": "nacion",
    "provincia": "provincia",
    "banco provincia": "provincia",
    "ciudad": "ciudad",
    "banco ciudad": "ciudad",
    "comafi": "comafi",
    "banco comafi": "comafi",
    "brubank": "brubank",
    "patagonia": "patagonia",
    "banco patagonia": "patagonia",
    "yoy": "yoy",
    "bancor": "bancor",
    "banco de cordoba": "bancor",
    "banco de córdoba": "bancor",
    "buepp": "buepp",
    "banco santa fe": "santa_fe",
    "banco entre rios": "entre_rios",
    "banco entre ríos": "entre_rios",
    "banco santa cruz": "santa_cruz",
    "banco san juan": "san_juan",
    "banco del sol": "del_sol",
}


def normalizar_banco_api(valor: str) -> str | None:
    texto = normalizar(
        str(valor or "")
    ).strip()

    if not texto:
        return None

    # Coincidencia exacta primero.
    if texto in BANCO_ALIAS:
        return BANCO_ALIAS[texto]

    # Luego coincidencia por nombre contenido, pero sólo
    # dentro de un campo de la API identificado como banco.
    for alias, banco_id in sorted(
        BANCO_ALIAS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if re.search(
            rf"(^|[^a-z0-9]){re.escape(alias)}([^a-z0-9]|$)",
            texto,
        ):
            return banco_id

    return None


def recorrer_valores_estructurados(
    objeto,
    ruta: str = "",
):
    """
    Devuelve pares (ruta, valor) recorriendo el JSON de MODO.
    La ruta permite saber si un nombre vino de un campo de banco,
    tarjeta, crédito o débito, evitando leer texto decorativo.
    """
    if isinstance(
        objeto,
        dict,
    ):
        for clave, valor in objeto.items():
            nueva_ruta = (
                f"{ruta}.{clave}"
                if ruta
                else str(clave)
            )
            yield from recorrer_valores_estructurados(
                valor,
                nueva_ruta,
            )

    elif isinstance(
        objeto,
        list,
    ):
        for indice, valor in enumerate(
            objeto
        ):
            yield from recorrer_valores_estructurados(
                valor,
                f"{ruta}[{indice}]",
            )

    else:
        yield ruta.lower(), objeto



def obtener_bancos_adheridos_card(
    card: dict | None,
) -> list[str]:
    """
    Lee únicamente content.row[*].extra_data cuando la fila está
    etiquetada por MODO como 'Bancos adheridos'.
    """
    if not isinstance(card, dict):
        return []

    content = card.get("content", {})
    if not isinstance(content, dict):
        return []

    rows = content.get("row", [])
    if not isinstance(rows, list):
        return []

    bancos = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        etiqueta = normalizar(
            str(row.get("text", "") or "")
        )

        if "bancos adheridos" not in etiqueta:
            continue

        extra_data = row.get("extra_data", [])
        if not isinstance(extra_data, list):
            continue

        for item in extra_data:
            if not isinstance(item, dict):
                continue

            nombre = str(
                item.get("name_bank", "") or ""
            ).strip()

            if not nombre:
                continue

            banco_id = normalizar_banco_api(
                nombre
            )

            if banco_id:
                bancos.add(banco_id)

    return sorted(bancos)


def parsear_fecha_api(
    valor: str | None,
) -> str | None:
    """
    Convierte el instante ISO de MODO a fecha local de Argentina.

    Ejemplo:
    2026-11-13T02:59:59.999Z -> 2026-11-12 en Buenos Aires.
    """
    if not valor:
        return None

    texto = str(valor).strip()

    try:
        # datetime.fromisoformat acepta offsets; convertimos Z a +00:00.
        iso = texto
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"

        fecha_hora = datetime.fromisoformat(iso)

        # Si excepcionalmente llega sin zona, MODO normalmente expresa
        # estos timestamps en UTC. Lo tratamos como UTC de forma explícita.
        if fecha_hora.tzinfo is None:
            fecha_hora = fecha_hora.replace(tzinfo=timezone.utc)

        return fecha_hora.astimezone(TZ_ARGENTINA).date().isoformat()

    except Exception:
        # Fallback conservador para formatos inesperados.
        match = re.match(
            r"(\d{4}-\d{2}-\d{2})",
            texto,
        )
        return match.group(1) if match else None

def dias_semana_desde_api(
    valor: str | None,
) -> list[int]:
    """
    MODO devuelve letras en español:
    L M X J V S D.
    """
    if not valor:
        return []

    mapa = {
        "L": 1,
        "M": 2,
        "X": 3,
        "J": 4,
        "V": 5,
        "S": 6,
        "D": 7,
    }

    dias = set()

    for letra in str(valor).upper():
        if letra in mapa:
            dias.add(mapa[letra])

    return sorted(dias)


def extraer_porcentaje_card(
    card: dict | None,
) -> int | None:
    if not isinstance(card, dict):
        return None

    candidatos = [
        str(card.get("title", "") or ""),
        str(card.get("short_description", "") or ""),
    ]

    content = card.get("content", {})
    if isinstance(content, dict):
        rows = content.get("row", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    candidatos.append(
                        str(row.get("text", "") or "")
                    )

    return extraer_porcentaje(
        " ".join(candidatos)
    )


def comercio_desde_card(
    card: dict | None,
) -> str | None:
    if not isinstance(card, dict):
        return None

    content = card.get("content", {})
    if isinstance(content, dict):
        rows = content.get("row", [])
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                texto = str(row.get("text", "") or "").strip()
                if texto:
                    # La primera fila suele ser el comercio visible.
                    return texto

    titulo = str(card.get("title", "") or "").strip()
    if titulo:
        limpio = re.sub(
            r"^\s*\d+\s*%\s+(?:de\s+)?(?:reintegro\s+)?(?:en\s+)?",
            "",
            titulo,
            flags=re.I,
        ).strip()
        if limpio:
            return limpio

    return None



def extraer_tarjetas_lista_api(valores) -> set[str]:
    tarjetas = set()

    if not isinstance(valores, list):
        return tarjetas

    for tarjeta in valores:
        t = normalizar(str(tarjeta or ""))

        if t == "visa":
            tarjetas.add("visa")
        elif t in {"master", "mastercard"}:
            tarjetas.add("mastercard")
        elif t in {"american_express", "american express", "amex"}:
            tarjetas.add("amex")
        elif t == "cabal":
            tarjetas.add("cabal")

    return tarjetas


def extraer_medios_desde_api(
    card: dict | None,
) -> tuple[
    list[str],
    list[str],
    list[str],
    list[str],
    list[str],
    str,
    list[str],
]:
    """
    V7:
    - Billeteras: MODO.
    - Bancos: exclusivamente desde la fila 'Bancos adheridos'.
    - Tarjetas: exclusivamente desde debit_list / credit_list.
    - Conserva medios_detectados por compatibilidad con datos anteriores.
    """
    billeteras = {"modo"}
    bancos = set()
    tarjetas = set()
    tipos = set()
    evidencias = []

    if not isinstance(card, dict):
        medios_legacy = sorted(billeteras)
        return (
            medios_legacy,
            sorted(billeteras),
            [],
            [],
            [],
            "MODO",
            evidencias,
        )

    bancos.update(
        obtener_bancos_adheridos_card(card)
    )

    for banco in sorted(bancos):
        evidencias.append(
            f"banco_adherido={banco}"
        )

    debit_list = card.get(
        "debit_list",
        []
    )

    if isinstance(debit_list, list) and debit_list:
        tipos.add("debito")
        tarjetas.update(
            extraer_tarjetas_lista_api(debit_list)
        )

    credit_list = card.get(
        "credit_list",
        []
    )

    if isinstance(credit_list, list) and credit_list:
        tipos.add("credito")
        tarjetas.update(
            extraer_tarjetas_lista_api(credit_list)
        )

    # Segmentos especiales siguen quedando en medios_detectados legacy.
    # Los campos separados expresan solamente billetera/banco/marca.
    especiales = set()

    identidad = normalizar(
        " ".join([
            str(card.get("title", "") or ""),
            str(card.get("slug", "") or ""),
            str(card.get("short_description", "") or ""),
        ])
    )

    if "macro selecta" in identidad or "macroselecta" in identidad:
        bancos.add("macro")
        especiales.add("macro_selecta")

    if "macro platinum" in identidad or "macroplatinum" in identidad:
        bancos.add("macro")
        especiales.add("macro_platinum")

    payment_flow = normalizar(
        str(card.get("payment_flow", "") or "")
    )

    if payment_flow == "instore":
        canal = "MODO presencial"
    elif payment_flow:
        canal = f"MODO {payment_flow}"
    else:
        canal = "MODO"

    medios_legacy = sorted(
        billeteras |
        bancos |
        tarjetas |
        especiales
    )

    return (
        medios_legacy,
        sorted(billeteras),
        sorted(bancos),
        sorted(tarjetas),
        sorted(tipos),
        canal,
        evidencias,
    )

def extraer_medios(texto: str) -> tuple[list[str], list[str], str]:
    contexto = extraer_contexto_condiciones(
        texto
    )
    normal = normalizar(
        contexto
    )

    medios = ["modo"]
    tipos = []
    canal = ""

    # Tipo de tarjeta/medio.
    if re.search(
        r"credito",
        normal,
    ):
        tipos.append(
            "credito"
        )

    if re.search(
        r"debito",
        normal,
    ):
        tipos.append(
            "debito"
        )

    if re.search(
        r"prepaga",
        normal,
    ):
        tipos.append(
            "prepaga"
        )

    # Marcas de tarjeta: sólo en el contexto reducido.
    if re.search(
        r"visa",
        normal,
    ):
        medios.append(
            "visa"
        )

    if re.search(
        r"mastercard",
        normal,
    ):
        medios.append(
            "mastercard"
        )

    if (
        "american express" in normal
        or re.search(
            r"amex",
            normal,
        )
    ):
        medios.append(
            "amex"
        )

    # Bancos. Se agregan únicamente si aparecen asociados
    # a conceptos de tarjeta/cliente/MODO dentro de las condiciones.
    bancos = [
        ("supervielle", "supervielle"),
        ("banco macro", "macro"),
        ("macro", "macro"),
        ("hipotecario", "hipotecario"),
        ("galicia", "galicia"),
        ("bbva", "bbva"),
        ("santander", "santander"),
        ("icbc", "icbc"),
        ("credicoop", "credicoop"),
        ("banco nacion", "nacion"),
        ("banco nación", "nacion"),
        ("banco provincia", "provincia"),
        ("banco ciudad", "ciudad"),
    ]

    for texto_banco, id_banco in bancos:
        if contiene_patron_banco(
            normal,
            texto_banco,
        ):
            medios.append(
                id_banco
            )

    # Segmentos específicos, sólo con evidencia explícita.
    if (
        "macro selecta" in normal
        and contiene_patron_banco(
            normal,
            "macro",
        )
    ):
        medios.append(
            "macro_selecta"
        )

    if (
        "platinum" in normal
        and contiene_patron_banco(
            normal,
            "macro",
        )
    ):
        medios.append(
            "macro_platinum"
        )

    # Canal de pago.
    if (
        re.search(
            r"qr",
            normal,
        )
        and re.search(
            r"nfc",
            normal,
        )
    ):
        canal = "QR / NFC MODO"

    elif re.search(
        r"qr",
        normal,
    ):
        canal = "QR MODO"

    elif re.search(
        r"nfc",
        normal,
    ):
        canal = "NFC MODO"

    elif "boton de pago" in normal:
        canal = "Botón de pago MODO"

    else:
        canal = "MODO"

    return (
        sorted(
            set(medios)
        ),
        sorted(
            set(tipos)
        ),
        canal,
    )


def detectar_categoria(
    titulo: str,
    texto: str,
    url: str = "",
) -> str:
    """
    Clasificación V8.7.

    Mejoras:
    - analiza título + texto + URL;
    - usa reglas genéricas;
    - agrega comercios/rubros conocidos detectados en nuestras promos;
    - mantiene "otros" si no hay evidencia suficiente.

    Se priorizan rubros muy específicos antes de rubros más amplios.
    """
    normal = normalizar(
        " ".join(
            [
                titulo or "",
                texto[:5000] if texto else "",
                url or "",
            ]
        )
    )

    reglas = [
        (
            "combustible",
            [
                r"\bypf\b",
                r"\bshell\b",
                r"\baxion\b",
                r"\bpuma energy\b",
                r"\bcombustible\b",
                r"\bestacion(?:es)? de servicio\b",
            ],
        ),
        (
            "farmacia",
            [
                r"\bfarmacia(?:s)?\b",
                r"\bfarmacity\b",
                r"\bfarmaplus\b",
                r"\bdr\.? ahorro\b",
                r"\bopenfarma\b",
                r"\btop farma\b",
                r"\bmi farma\b",
            ],
        ),
        (
            "automotor",
            [
                r"\brepuesto(?:s)?\b",
                r"\brepuestoschely\b",
                r"\bautoparte(?:s)?\b",
                r"\bautomotor\b",
                r"\bautomotriz\b",
                r"\baccesorios? para (?:auto|autos|vehiculos)\b",
            ],
        ),
        (
            "supermercado",
            [
                r"\bsupermercado(?:s)?\b",
                r"\bhipermercado(?:s)?\b",
                r"\bmayorista(?:s)?\b",
                r"\bcoto\b",
                r"\bjumbo\b",
                r"\bvea\b",
                r"\bchangomas\b",
                r"\bchango mas\b",
                r"\bla anonima\b",
                r"\bcarrefour\b",
                r"\bdisco\b",
                r"\bmakro\b",
                r"\bcooperativa obrera\b",
                r"\bunisur\b",
                r"\bla cumbre sanjuanina\b",
                r"\bmayorista vital\b",
                r"\btiendas? de cercania\b",
                r"\bmercado municipal\b",
            ],
        ),
        (
            "alimentos",
            [
                r"\bcarniceria(?:s)?\b",
                r"\bcarnes\b",
                r"\bbodega(?:s)?\b",
                r"\bvinos?\b",
                r"\bwine\b",
                r"\bvinoteca(?:s)?\b",
                r"\balimentos?\b",
                r"\bbebidas?\b",
                r"\bfiambreria\b",
                r"\bqueseria\b",
                r"\brapanui\b",
                r"\bfrioteka\b",
                r"\bcongelados?\b",
                r"\bhavanna\b",
                r"\bvinobien\b",
                r"\bfinca savina\b",
                r"\bviñas? de cafayate\b",
            ],
        ),
        (
            "gastronomia",
            [
                r"\brestaurante(?:s)?\b",
                r"\bgastronomia\b",
                r"\bcerveceria\b",
                r"\bcafeteria\b",
                r"\bcafe\b",
                r"\bpizzeria\b",
                r"\bheladeria\b",
                r"\bhamburgues",
                r"\blomito(?:s)?\b",
                r"\bbar(?:es)?\b",
                r"\bparrilla\b",
                r"\bpatagonia\b",
                r"\bbeto(?:s)? lomito(?:s)?\b",
                r"\bmoon beer\b",
                r"\btanta\b",
                r"\bresto\b",
                r"\blehonor\b",
                r"\bdashi\b",
                r"\boporto\b",
                r"\bvalentinos\b",
                r"\bwine\s*bar\b",
                r"\bbrasas?\b",
                r"\bdesayunos?\b",
                r"\bcasa cumbre\b",
                r"\bel paso oeste uru\b",
                r"\btressen\b",
            ],
        ),
        (
            "ropa",
            [
                r"\bindumentaria\b",
                r"\bropa\b",
                r"\bmoda\b",
                r"\bcalzado\b",
                r"\bzapatilla",
                r"\bjeans?\b",
                r"\bstock center\b",
                r"\btienda(?:s)? de ropa\b",
                r"\bsalvaje jeans\b",
                r"\blola indumentaria\b",
                r"\bcheeky\b",
                r"\bshopgallery\b",
                r"\bthis week\b",
                r"\boassian\b",
                r"\bfortunato\b",
            ],
        ),
        (
            "deportes",
            [
                r"\bdeporte(?:s)?\b",
                r"\bdeportivo",
                r"\bsport\b",
                r"\bfutbol\b",
                r"\brunning\b",
                r"\bmontagne\b",
                r"\blinks pinamar\b",
                r"\bgolf\b",
            ],
        ),
        (
            "tecnologia",
            [
                r"\btecnologia\b",
                r"\belectronica\b",
                r"\belectrodomestico",
                r"\bcelular(?:es)?\b",
                r"\bsmartphone",
                r"\bcomputacion\b",
                r"\binformatica\b",
                r"\bfravega\b",
                r"\bdale me gusta\b",
            ],
        ),
        (
            "gimnasio",
            [
                r"\bgimnasio(?:s)?\b",
                r"\blagree\b",
                r"\bpur lagree studio\b",
                r"\bfitness\b",
                r"\bmegathlon\b",
                r"\bmegatlon\b",
                r"\bsportclub\b",
            ],
        ),
        (
            "hogar",
            [
                r"\bhogar\b",
                r"\bmueble",
                r"\bdecoracion\b",
                r"\bbazar\b",
                r"\bferreteria\b",
                r"\bcolchon",
                r"\bconstruccion\b",
                r"\bmateriales? de construccion\b",
                r"\baberturas?\b",
                r"\bpintureria(?:s)?\b",
                r"\ballegra srl\b",
                r"\bvexar srl\b",
                r"\bsanitarios\b",
                r"\bmadeco\b",
            ],
        ),
        (
            "belleza",
            [
                r"\bbelleza\b",
                r"\bpeluqueria(?:s)?\b",
                r"\bestetica\b",
                r"\bcosmetica",
                r"\bperfumeria\b",
                r"\bspa\b",
            ],
        ),
        (
            "mascotas",
            [
                r"\bmascota",
                r"\bpet shop\b",
                r"\bpetshop\b",
                r"\bpet house\b",
                r"\bveterinaria\b",
            ],
        ),
        (
            "viajes",
            [
                r"\bhotel(?:es)?\b",
                r"\bturismo\b",
                r"\bviaje(?:s)?\b",
                r"\balojamiento\b",
                r"\baerolinea",
                r"\bhostel\b",
            ],
        ),
        (
            "entretenimiento",
            [
                r"\bcine(?:s)?\b",
                r"\bteatro\b",
                r"\bentrada(?:s)?\b",
                r"\bshow\b",
                r"\bbowling\b",
                r"\bentretenimiento\b",
                r"\bparque de diversiones\b",
                r"\bjuegos\b",
                r"\brio juegos\b",
                r"\bclub britanico\b",
                r"\bcuspide\b",
                r"\blibreria\b",
                r"\blibros?\b",
            ],
        ),
    ]

    for categoria, patrones in reglas:
        if any(
            re.search(
                patron,
                normal,
                flags=re.I,
            )
            for patron in patrones
        ):
            return categoria

    return "otros"

def extraer_titulo_y_comercio(soup: BeautifulSoup, url: str):
    titulo = ""

    h1 = soup.find("h1")
    if h1:
        titulo = h1.get_text(" ", strip=True)

    if not titulo:
        title = soup.find("title")
        if title:
            titulo = title.get_text(" ", strip=True)

    titulo = re.sub(
        r"\s*\|\s*MODO.*$",
        "",
        titulo,
        flags=re.I,
    ).strip()

    comercio = titulo

    # Limpia prefijos típicos "20% en", "25% de reintegro en", etc.
    comercio = re.sub(
        r"^\s*\d{1,3}\s*%\s*(?:de\s+reintegro\s+)?(?:en\s+)?",
        "",
        comercio,
        flags=re.I,
    ).strip()

    # En títulos tipo "COTO con Supervielle", el comercio es COTO.
    comercio = re.split(
        r"\s+con\s+",
        comercio,
        maxsplit=1,
        flags=re.I,
    )[0].strip()

    if not comercio:
        comercio = url.rstrip("/").split("/")[-1]

    return titulo, comercio



def es_promocion_general(
    titulo: str,
    texto: str,
    url: str,
) -> bool:
    identidad = clave_clasificacion(
        " ".join(
            [
                titulo or "",
                texto[:5000] if texto else "",
                url or "",
            ]
        )
    )

    return (
        "primeracompra" in identidad
        and (
            "comerciosqueaceptenmodo" in identidad
            or "cualquiercomercio" in identidad
            or "primerpago" in identidad
        )
    )


def analizar_promo(
    url: str,
    card_api: dict | None = None,
) -> dict:
    html = descargar(url)
    soup = BeautifulSoup(
        html,
        "html.parser",
    )
    texto = limpiar_texto(
        soup
    )

    titulo_web, comercio_web = extraer_titulo_y_comercio(
        soup,
        url,
    )

    comercio_api = comercio_desde_card(
        card_api
    )

    comercio = (
        comercio_api
        or comercio_web
    )

    titulo = (
        str(
            card_api.get("title", "")
            if isinstance(card_api, dict)
            else ""
        ).strip()
        or titulo_web
    )

    porcentaje = (
        extraer_porcentaje_card(
            card_api
        )
        or extraer_porcentaje(
            titulo + " " + texto
        )
    )

    contexto_condiciones = extraer_contexto_condiciones(
        texto
    )

    monto_minimo = extraer_monto_minimo(
        contexto_condiciones
    )

    (
        tope,
        periodo_tope,
        tipo_tope,
    ) = extraer_tope(
        contexto_condiciones
    )

    # API primero para fechas/días.
    desde = None
    hasta = None
    dias = []

    if isinstance(card_api, dict):
        desde = parsear_fecha_api(
            card_api.get("start_date")
        )
        hasta = parsear_fecha_api(
            card_api.get("stop_date")
        )
        dias = dias_semana_desde_api(
            card_api.get("days_of_week")
        )

    # Fallback a ficha si faltara algo.
    if not desde or not hasta:
        desde_web, hasta_web = extraer_vigencia(
            contexto_condiciones
        )
        desde = desde or desde_web
        hasta = hasta or hasta_web

    if not dias:
        dias = extraer_dias(
            contexto_condiciones
        )

    (
        medios,
        billeteras,
        bancos,
        tarjetas,
        tipos,
        canal,
        evidencias_api,
    ) = extraer_medios_desde_api(
        card_api
    )

    categoria = detectar_categoria(
        titulo,
        texto,
        url,
    )

    if es_promocion_general(
        titulo,
        texto,
        url,
    ):
        categoria = "general"
        comercio = "Comercios que acepten MODO"

    hash_base = {
        "texto": texto,
        "api": card_api or {},
    }

    hash_contenido = hashlib.sha256(
        json.dumps(
            hash_base,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    estado_api = ""
    if isinstance(card_api, dict):
        estado_api = str(
            card_api.get(
                "calculated_status",
                "",
            )
            or ""
        ).upper()

    observaciones = (
        "V9.7 API MODO | medios separados | fechas Argentina UTC-3 | últimos rubros confirmados | resistente"
    )

    if estado_api:
        observaciones += (
            f" | status={estado_api}"
        )

    if evidencias_api:
        observaciones += (
            " | "
            + "; ".join(
                evidencias_api[:30]
            )
        )

    return {
        "fuente": FUENTE,
        "fuente_url": url,
        "titulo": titulo,
        "comercio_detectado": comercio,
        "categoria_detectada": categoria,
        "porcentaje_detectado": porcentaje,
        "monto_minimo_detectado": monto_minimo,
        "tope_reintegro_detectado": tope,
        "periodo_tope_detectado": periodo_tope,
        "tipo_tope_detectado": tipo_tope,
        "vigencia_desde_detectada": desde,
        "vigencia_hasta_detectada": hasta,
        "dias_semana_detectados": dias,
        "medios_detectados": medios,
        "billeteras_detectadas": billeteras,
        "bancos_detectados": bancos,
        "tarjetas_detectadas": tarjetas,
        "tipos_medio_detectados": tipos,
        "canal_pago_detectado": canal,
        "descripcion_detectada": titulo,
        "texto_fuente": texto,
        "hash_contenido": hash_contenido,
        "estado": "pendiente",
        "observaciones": observaciones,
        "ultima_revision_at": datetime.utcnow().isoformat() + "Z",
    }


def upsert_supabase(datos: dict):
    url = (
        SUPABASE_URL
        + "/rest/v1/promociones_detectadas"
        + "?on_conflict=fuente_url"
    )

    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    respuesta = pedir_con_reintentos(
        "POST",
        url,
        headers=headers,
        data=json.dumps(
            datos,
            ensure_ascii=False,
        ).encode("utf-8"),
    )

    if respuesta.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"Supabase {respuesta.status_code}: {respuesta.text}"
        )



def obtener_promos_vigentes_para_reclasificar() -> list[dict]:
    """
    Recupera promociones vigentes que todavía siguen como "otros".

    A diferencia de V8.5, trae también:
    - comercio_detectado
    - descripcion_detectada
    - titulo
    - texto_fuente

    Esto permite reclasificar usando la evidencia que ya guardamos,
    incluso si la ficha pública actual de MODO cambió o quedó incompleta.
    """
    hoy = datetime.now(
        TZ_ARGENTINA
    ).date().isoformat()

    endpoint = (
        SUPABASE_URL
        + "/rest/v1/promociones_detectadas"
    )

    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Accept": "application/json",
    }

    params = {
        "select": (
            "id,"
            "fuente_url,"
            "comercio_detectado,"
            "categoria_detectada,"
            "descripcion_detectada,"
            "titulo,"
            "texto_fuente,"
            "vigencia_hasta_detectada,"
            "observaciones"
        ),
        "categoria_detectada": "eq.otros",
        "vigencia_hasta_detectada": f"gte.{hoy}",
        "fuente_url": "not.is.null",
        "limit": "5000",
    }

    respuesta = pedir_con_reintentos(
        "GET",
        endpoint,
        headers=headers,
        params=params,
    )
    respuesta.raise_for_status()

    filas = respuesta.json()

    if not isinstance(filas, list):
        return []

    resultado = []

    for fila in filas:
        if not isinstance(fila, dict):
            continue

        promo_url = normalizar_url_promo(
            str(
                fila.get(
                    "fuente_url",
                    "",
                )
                or ""
            )
        )

        if not promo_url:
            continue

        copia = dict(fila)
        copia["fuente_url_normalizada"] = promo_url
        resultado.append(copia)

    return resultado


def detectar_categoria_historica_directa(
    comercio: str,
    titulo: str,
    descripcion: str,
    texto: str,
    url: str,
) -> str:
    """
    Clasificador histórico V9.5.

    Usa comercio + título + descripción + texto de la ficha + URL.
    Aplica reglas inequívocas primero y luego un sistema simple de puntaje.
    Si no hay evidencia suficiente, conserva "otros".
    """

    identidad = normalizar(
        " ".join(
            [
                comercio or "",
                titulo or "",
                descripcion or "",
                texto[:7000] if texto else "",
                url or "",
            ]
        )
    )
    clave = clave_clasificacion(identidad)

    # ---------------------------------------------------------
    # 1) REGLAS INEQUÍVOCAS
    # ---------------------------------------------------------
    if any(
        x in clave
        for x in [
            "repuestos",
            "repuesto",
            "respuestos",
            "respuesto",
            "repuestoschely",
            "respuestoschely",
            "autoparte",
            "automotor",
            "automotriz",
        ]
    ):
        return "automotor"

    # Promociones generales: no corresponden a un comercio puntual.
    if (
        "primeracompra" in clave
        and (
            "comerciosqueaceptenmodo" in clave
            or "cualquiercomercio" in clave
            or "primerpago" in clave
        )
    ):
        return "general"

    directos = [
        # supermercado / alimentos
        ("makro", "supermercado"),
        ("lacumbresanjuanina", "supermercado"),
        ("cooperativaobrera", "supermercado"),
        ("unisur", "supermercado"),
        ("frioteka", "alimentos"),
        ("havanna", "alimentos"),
        ("rapanui", "alimentos"),

        # gastronomía
        ("freddo", "gastronomia"),
        ("lacasona", "gastronomia"),
        ("lehonor", "gastronomia"),
        ("dashi", "gastronomia"),
        ("oporto", "gastronomia"),
        ("valentinos", "gastronomia"),
        ("casacumbre", "gastronomia"),
        ("elpasooesteuru", "gastronomia"),
        ("tressen", "gastronomia"),

        # ropa / calzado / accesorios
        ("mimo", "ropa"),
        ("milveintinuo", "ropa"),
        ("secondski", "ropa"),
        ("secondskin", "ropa"),
        ("shoesocial", "ropa"),
        ("shopgallery", "ropa"),
        ("fortunato", "ropa"),
        ("varekai", "ropa"),
        ("thisweekoassian", "ropa"),
        ("thisweek", "ropa"),

        # tecnología
        ("fravega", "tecnologia"),
        ("dalemegusta", "tecnologia"),

        # hogar / construcción
        ("allegrasrl", "hogar"),
        ("vexarsrl", "hogar"),
        ("pinturerias", "hogar"),
        ("pintureria", "hogar"),

        # deportes / gimnasio
        ("montagne", "deportes"),
        ("linkspinamar", "deportes"),
        ("purlagreestudio", "gimnasio"),
        ("lagree", "gimnasio"),

        # mascotas
        ("pethouse", "mascotas"),
        ("puppis", "mascotas"),

        # belleza
        ("peluqueria", "belleza"),

        # entretenimiento / libros
        ("cuspide", "entretenimiento"),
        ("libreria", "entretenimiento"),
    ]

    for patron, categoria in directos:
        if patron in clave:
            return categoria

    # ---------------------------------------------------------
    # 2) PUNTAJE POR EVIDENCIAS DE RUBRO
    # ---------------------------------------------------------
    reglas = {
        "supermercado": [
            r"\bsupermercado(?:s)?\b",
            r"\bhipermercado(?:s)?\b",
            r"\bmayorista(?:s)?\b",
            r"\bautoservicio(?:s)?\b",
            r"\bmercado(?:s)?\b",
        ],
        "alimentos": [
            r"\balimentos?\b",
            r"\bcongelados?\b",
            r"\bcomestibles?\b",
            r"\bbebidas?\b",
            r"\bgolosinas?\b",
            r"\bcarnes?\b",
            r"\bbodega\b",
            r"\bvinos?\b",
        ],
        "gastronomia": [
            r"\brestaurante(?:s)?\b",
            r"\bresto\b",
            r"\bbar(?:es)?\b",
            r"\bcafe\b",
            r"\bcafeteria\b",
            r"\bheladeria\b",
            r"\bpizzeria\b",
            r"\bparrilla\b",
            r"\bhamburgues",
            r"\bbrasas?\b",
        ],
        "ropa": [
            r"\bropa\b",
            r"\bindumentaria\b",
            r"\bmoda\b",
            r"\bcalzado\b",
            r"\bzapatilla",
            r"\bjeans?\b",
            r"\bprendas?\b",
            r"\bjoyeria\b",
            r"\bjoyas?\b",
            r"\banillos?\b",
            r"\baros?\b",
            r"\bpulseras?\b",
        ],
        "tecnologia": [
            r"\btecnologia\b",
            r"\belectronica\b",
            r"\belectrodomestico",
            r"\bcelular(?:es)?\b",
            r"\bsmartphone",
            r"\bcomputacion\b",
            r"\binformatica\b",
        ],
        "deportes": [
            r"\bdeporte(?:s)?\b",
            r"\bdeportivo\b",
            r"\bgolf\b",
            r"\brunning\b",
            r"\bfutbol\b",
        ],
        "gimnasio": [
            r"\bgimnasio(?:s)?\b",
            r"\bfitness\b",
            r"\blagree\b",
            r"\bpilates\b",
        ],
        "hogar": [
            r"\bhogar\b",
            r"\bmueble",
            r"\bdecoracion\b",
            r"\bbazar\b",
            r"\bferreteria\b",
            r"\bcolchon",
            r"\bconstruccion\b",
            r"\bsanitarios\b",
        ],
        "belleza": [
            r"\bbelleza\b",
            r"\bpeluqueria(?:s)?\b",
            r"\bestetica\b",
            r"\bcosmetica",
            r"\bperfumeria\b",
            r"\bspa\b",
        ],
        "mascotas": [
            r"\bmascota",
            r"\bpet\s*shop\b",
            r"\bpetshop\b",
            r"\bveterinaria\b",
        ],
        "entretenimiento": [
            r"\bcine(?:s)?\b",
            r"\bteatro\b",
            r"\bentrada(?:s)?\b",
            r"\bshow\b",
            r"\bjuegos\b",
            r"\blibreria\b",
            r"\blibros?\b",
        ],
    }

    puntajes = {}

    for categoria, patrones in reglas.items():
        puntos = 0
        for patron in patrones:
            if re.search(patron, identidad, flags=re.I):
                puntos += 1
        puntajes[categoria] = puntos

    mejor_categoria = max(
        puntajes,
        key=puntajes.get,
    )
    mejor_puntaje = puntajes[mejor_categoria]

    # Si sólo hay una señal genérica, mantenemos "otros".
    # Dos o más evidencias coincidentes son suficientes.
    if mejor_puntaje >= 2:
        return mejor_categoria

    # ---------------------------------------------------------
    # 3) FALLBACK AL CLASIFICADOR GENERAL
    # ---------------------------------------------------------
    categoria_general = detectar_categoria(
        " ".join(
            [
                comercio or "",
                titulo or "",
                descripcion or "",
            ]
        ),
        texto or "",
        url or "",
    )

    if categoria_general != "otros":
        return categoria_general

    return "otros"


def reprocesar_categoria_historica(
    fila: dict,
) -> tuple[bool, str]:
    """
    Reclasifica una promo histórica usando primero la información
    ya guardada en Supabase y, como refuerzo, la ficha pública actual.

    Esto resuelve casos como REPUESTOS CHELY, donde la URL es
    "...repuestoschely..." y la ficha pública puede no exponer
    claramente el nombre separado.
    """
    registro_id = str(
        fila.get(
            "id",
            "",
        )
        or ""
    ).strip()

    url_original = str(
        fila.get(
            "fuente_url",
            "",
        )
        or ""
    )

    url = str(
        fila.get(
            "fuente_url_normalizada",
            "",
        )
        or url_original
    )

    if not registro_id:
        raise RuntimeError(
            "La promoción histórica no trae id; no se actualiza para evitar tocar una fila incorrecta."
        )

    comercio_guardado = str(
        fila.get(
            "comercio_detectado",
            "",
        )
        or ""
    )

    descripcion_guardada = str(
        fila.get(
            "descripcion_detectada",
            "",
        )
        or ""
    )

    titulo_guardado = str(
        fila.get(
            "titulo",
            "",
        )
        or ""
    )

    texto_guardado = str(
        fila.get(
            "texto_fuente",
            "",
        )
        or ""
    )

    titulo_web = ""
    texto_web = ""

    try:
        html = descargar(
            url
        )

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        texto_web = limpiar_texto(
            soup
        )

        titulo_web, _ = extraer_titulo_y_comercio(
            soup,
            url,
        )

    except Exception as exc:
        print(
            f"   [INFO] Ficha pública no disponible, uso datos guardados: {exc}"
        )

    titulo_clasificacion = " ".join(
        x
        for x in [
            comercio_guardado,
            titulo_guardado,
            descripcion_guardada,
            titulo_web,
        ]
        if x
    )

    texto_clasificacion = " ".join(
        x
        for x in [
            texto_guardado,
            texto_web,
            comercio_guardado,
            descripcion_guardada,
        ]
        if x
    )

    # Normalización fuerte: elimina incluso separadores/caracteres invisibles.
    clave_comercio = clave_clasificacion(comercio_guardado)
    clave_titulo = clave_clasificacion(titulo_guardado)
    clave_descripcion = clave_clasificacion(descripcion_guardada)
    clave_url = clave_clasificacion(url)

    claves = [
        clave_comercio,
        clave_titulo,
        clave_descripcion,
        clave_url,
    ]

    # Regla inequívoca de automotor.
    if any(
        (
            "repuestos" in clave
            or "repuesto" in clave
            or "respuestos" in clave
            or "respuesto" in clave
            or "autoparte" in clave
            or "automotor" in clave
            or "automotriz" in clave
        )
        for clave in claves
    ):
        categoria = "automotor"
    else:
        categoria = detectar_categoria_historica_directa(
            comercio_guardado,
            titulo_guardado,
            descripcion_guardada,
            texto_clasificacion,
            url,
        )

    print(
        f"   DIAG -> comercio={comercio_guardado!r} | "
        f"categoria_calculada={categoria}"
    )

    if (
        "chely" in clave_comercio
        or "chely" in clave_titulo
        or "chely" in clave_descripcion
        or "chely" in clave_url
    ):
        print(
            "   DIAG CHELY -> "
            f"clave_comercio={clave_comercio!r} | "
            f"clave_titulo={clave_titulo!r} | "
            f"clave_descripcion={clave_descripcion!r} | "
            f"clave_url={clave_url!r}"
        )

    if categoria == "otros":
        return False, categoria

    endpoint = (
        SUPABASE_URL
        + "/rest/v1/promociones_detectadas"
    )

    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    # Actualización por ID exacto de Supabase.
    # Evita fallos silenciosos por diferencias en la URL guardada
    # (slash final, codificación, etc.).
    params = {
        "id": f"eq.{registro_id}",
    }

    payload = {
        "categoria_detectada": categoria,
        "estado": "pendiente",
        "observaciones": (
            "V9.7 reclasificación histórica vigente | "
            "categoría recalculada usando datos guardados + ficha pública MODO"
        ),
        "ultima_revision_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    respuesta = pedir_con_reintentos(
        "PATCH",
        endpoint,
        headers=headers,
        params=params,
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8"),
    )

    if respuesta.status_code not in (
        200,
        204,
    ):
        raise RuntimeError(
            f"Supabase {respuesta.status_code}: {respuesta.text}"
        )

    try:
        filas_actualizadas = respuesta.json()
    except Exception:
        filas_actualizadas = []

    if not isinstance(filas_actualizadas, list) or len(filas_actualizadas) != 1:
        raise RuntimeError(
            "Supabase no confirmó la actualización de exactamente 1 fila histórica."
        )

    categoria_confirmada = str(
        filas_actualizadas[0].get(
            "categoria_detectada",
            "",
        )
        or ""
    )

    print(
        f"   PATCH OK -> id={registro_id} | "
        f"categoria_guardada={categoria_confirmada}"
    )

    if categoria_confirmada != categoria:
        raise RuntimeError(
            f"Supabase devolvió categoria_detectada={categoria_confirmada!r}, "
            f"esperaba {categoria!r}."
        )

    return True, categoria

def main():
    validar_configuracion()

    # Autoprueba local antes de tocar la red o Supabase.
    prueba_chely = detectar_categoria_historica_directa(
        "REPUESTOS CHELY",
        "REPUESTOS CHELY",
        "REPUESTOS CHELY",
        "REPUESTOS CHELY",
        "https://www.modo.com.ar/promos-banco/2608-macro-repuestoschely-6csi",
    )

    prueba_chely_typo = detectar_categoria_historica_directa(
        "RESPUESTOS CHELY",
        "RESPUESTOS CHELY",
        "RESPUESTOS CHELY",
        "RESPUESTOS CHELY",
        "https://www.modo.com.ar/promos-banco/2608-macro-respuestoschely-6csi",
    )

    pruebas_v95 = {
        "FRIOTEKA NEUQUEN": "alimentos",
        "FORTUNATO": "ropa",
        "LEHONOR": "gastronomia",
        "LA CUMBRE SANJUANINA": "supermercado",
        "LINKS PINAMAR": "deportes",
        "PET HOUSE": "mascotas",
        "Pur Lagree Studio": "gimnasio",
        "CUSPIDE": "entretenimiento",
        "FREDDO": "gastronomia",
        "MIMO": "ropa",
        "PUPPIS": "mascotas",
        "SHOE SOCIAL": "ropa",
        "VAREKAI": "ropa",
        "ALLEGRA SRL": "hogar",
        "DALE ME GUSTA": "tecnologia",
        "THIS WEEK OASSIAN": "ropa",
        "Tressen": "gastronomia",
        "VEXAR SRL": "hogar",
        "Pinturerias": "hogar",
        "Casa Cumbre": "gastronomia",
        "EL PASO OESTE URU": "gastronomia",
        "UNISUR": "supermercado",
    }

    for comercio_prueba, categoria_esperada in pruebas_v95.items():
        categoria_prueba = detectar_categoria_historica_directa(
            comercio_prueba,
            comercio_prueba,
            comercio_prueba,
            comercio_prueba,
            "",
        )
        print(
            f"Autoprueba {comercio_prueba} -> {categoria_prueba}"
        )
        if categoria_prueba != categoria_esperada:
            raise RuntimeError(
                f"Falló autoprueba V9.5: {comercio_prueba} "
                f"devolvió {categoria_prueba!r}, esperaba {categoria_esperada!r}."
            )

    prueba_general = detectar_categoria_historica_directa(
        "Primera compra",
        "100% en primera compra",
        "Comercios que acepten MODO",
        "Primer pago en cualquier comercio que acepte MODO",
        "https://www.modo.com.ar/promos/100-primeracompra",
    )
    print(
        f"Autoprueba Primera compra -> {prueba_general}"
    )
    if prueba_general != "general":
        raise RuntimeError(
            "Falló autoprueba V9.6 para promoción general."
        )

    clave_prueba_chely = clave_clasificacion(
        "REPUESTOS CHELY"
    )

    print(
        f"Autoprueba REPUESTOS CHELY -> {prueba_chely}"
    )
    print(
        f"Autoprueba RESPUESTOS CHELY -> {prueba_chely_typo}"
    )
    print(
        f"Autoprueba clave CHELY -> {clave_prueba_chely}"
    )

    if (
        prueba_chely != "automotor"
        or prueba_chely_typo != "automotor"
        or clave_prueba_chely != "repuestoschely"
    ):
        raise RuntimeError(
            "Falló la autoprueba del clasificador histórico para REPUESTOS CHELY."
        )

    print(
        "AhorraAcá - recolector MODO V9.7 ULTIMOS RUBROS CONFIRMADOS"
    )
    print(
        "Descubriendo promociones actuales..."
    )

    urls = descubrir_urls_promos()

    print(
        f"Encontré {len(urls)} enlaces actuales/próximos."
    )

    guardadas = 0
    descartadas = 0
    errores = 0

    # ---------------------------------------------------------
    # 1) PROCESO NORMAL DE LA API ACTUAL
    # ---------------------------------------------------------
    for indice, url in enumerate(
        urls,
        start=1,
    ):
        print(
            f"[{indice}/{len(urls)}] {url}"
        )

        try:
            promo = analizar_promo(
                url,
                MODO_CARDS_POR_URL.get(
                    url
                ),
            )

            descartar, motivo = promo_es_generica_o_incompleta(
                promo
            )

            if descartar:
                descartadas += 1

                print(
                    "  DESCARTADA -> "
                    f"{promo.get('comercio_detectado') or '(sin comercio)'} | "
                    f"{motivo}"
                )
                continue

            upsert_supabase(
                promo
            )

            guardadas += 1

            print(
                "  OK -> "
                f"{promo['comercio_detectado']} | "
                f"{promo['categoria_detectada']} | "
                f"{promo['porcentaje_detectado']}% | "
                f"{promo['vigencia_hasta_detectada']} | "
                f"Billetera: {', '.join(promo['billeteras_detectadas']) or '-'} | "
                f"Bancos: {', '.join(promo['bancos_detectados']) or '-'} | "
                f"Tarjetas: {', '.join(promo['tarjetas_detectadas']) or '-'}"
            )

        except Exception as exc:
            errores += 1
            print(
                f"  ERROR: {exc}"
            )

        time.sleep(
            0.8
        )

    # ---------------------------------------------------------
    # 2) RECLASIFICACIÓN DE HISTÓRICAS VIGENTES
    #
    # Se consulta DESPUÉS de procesar la API actual.
    # Así sólo quedan candidatas las filas que todavía continúan
    # en categoría "otros".
    # ---------------------------------------------------------
    print()
    print(
        "Buscando promociones vigentes que todavía siguen en 'otros'..."
    )

    try:
        promos_reclasificar = obtener_promos_vigentes_para_reclasificar()
    except Exception as exc:
        print(
            f"[WARN] No pude consultar promociones para reclasificar: {exc}"
        )
        promos_reclasificar = []

    print(
        f"Promociones vigentes en 'otros' para revisar: "
        f"{len(promos_reclasificar)}"
    )

    reclasificadas = 0
    sin_cambio = 0
    errores_historicos = 0

    if promos_reclasificar:
        print()
        print(
            "Reclasificando promociones vigentes..."
        )

    for indice, fila in enumerate(
        promos_reclasificar,
        start=1,
    ):
        url = str(
            fila.get(
                "fuente_url",
                "",
            )
            or ""
        )

        comercio = str(
            fila.get(
                "comercio_detectado",
                "",
            )
            or ""
        )

        print(
            f"[R {indice}/{len(promos_reclasificar)}] "
            f"{comercio or url}"
        )

        try:
            cambio, categoria = reprocesar_categoria_historica(
                fila
            )

            if cambio:
                reclasificadas += 1
                print(
                    f"  RECLASIFICADA -> {categoria}"
                )
            else:
                sin_cambio += 1
                print(
                    "  SIN CAMBIO -> otros"
                )

        except Exception as exc:
            errores_historicos += 1
            print(
                f"  ERROR RECLASIFICANDO: {exc}"
            )

        time.sleep(
            0.8
        )

    print()
    print("Finalizado.")
    print(
        f"Guardadas/actualizadas API actual: {guardadas}"
    )
    print(
        f"Vigentes reclasificadas desde 'otros': {reclasificadas}"
    )
    print(
        f"Vigentes que siguen en 'otros': {sin_cambio}"
    )
    print(
        f"Errores de reclasificación: {errores_historicos}"
    )
    print(
        f"Descartadas por calidad: {descartadas}"
    )
    print(
        f"Errores API actual: {errores}"
    )


def ejecutar_rpc_supabase(nombre_funcion: str):
    """Ejecuta una función RPC de Supabase y muestra su resultado."""
    endpoint = f"{SUPABASE_URL}/rest/v1/rpc/{nombre_funcion}"
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    print()
    print(f"Ejecutando RPC: {nombre_funcion}...")
    respuesta = pedir_con_reintentos(
        "POST",
        endpoint,
        headers=headers,
        json={},
    )

    if not respuesta.ok:
        detalle = respuesta.text[:1200]
        raise RuntimeError(
            f"Falló RPC {nombre_funcion}: HTTP {respuesta.status_code} - {detalle}"
        )

    try:
        resultado = respuesta.json()
    except ValueError:
        resultado = respuesta.text.strip()

    print(f"RPC {nombre_funcion} OK")
    if resultado not in (None, "", []):
        print(json.dumps(resultado, ensure_ascii=False, indent=2) if not isinstance(resultado, str) else resultado)
    return resultado


def ejecutar_ciclo_automatico():
    """Recolecta, valida y publica las promociones en un único ciclo."""
    inicio = datetime.now(TZ_ARGENTINA)
    print("=" * 70)
    print("AHORRAACÁ - CICLO AUTOMÁTICO")
    print(f"Inicio Argentina: {inicio:%Y-%m-%d %H:%M:%S}")
    print("=" * 70)

    # 1) Recolectar/actualizar promociones detectadas.
    main()

    # 2) Validar lo detectado con la función SQL existente.
    ejecutar_rpc_supabase("validar_promociones_detectadas")

    # 3) Publicar las promociones revisadas a la tabla consumida por Android.
    ejecutar_rpc_supabase("publicar_promociones_revisadas")

    fin = datetime.now(TZ_ARGENTINA)
    print()
    print("=" * 70)
    print("CICLO AUTOMÁTICO FINALIZADO CORRECTAMENTE")
    print(f"Fin Argentina: {fin:%Y-%m-%d %H:%M:%S}")
    print(f"Duración: {fin - inicio}")
    print("=" * 70)


if __name__ == "__main__":
    ejecutar_ciclo_automatico()
