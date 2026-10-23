"""
Mis noticias — genera la edición del día a partir de los RSS de
El País, El Mundo, ABC y La Vanguardia, resumida con Claude.

Salida:
  noticias.json            -> última edición (la que abre la app)
  ediciones/<fecha>-<franja>.json
  ediciones/indice.json    -> lista de ediciones guardadas (últimas 30)

Variables de entorno:
  ANTHROPIC_API_KEY  clave de la API (secreto del repo). Sin ella, solo titulares.
  MODELO             modelo de Claude (por defecto claude-haiku-5-5)
  HORA_PROGRAMADA    expresión cron que disparó el workflow (github.event.schedule)
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

MADRID = ZoneInfo("Europe/Madrid")
HORAS_EDICION = (7, 18)          # hora local de Madrid
MAX_NOTICIAS_POR_MEDIO = 18      # las que se le pasan a Claude
EDICIONES_GUARDADAS = 30
RAIZ = Path(__file__).resolve().parent

MEDIOS = {
    "elpais": {
        "nombre": "El País",
        "feeds": [
            "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada",
            "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/ultimas-noticias/portada",
        ],
    },
    "elmundo": {
        "nombre": "El Mundo",
        "feeds": [
            "https://e00-elmundo.uecdn.es/rss/portada.xml",
            "https://e00-elmundo.uecdn.es/elmundo/rss/espana.xml",
        ],
    },
    "abc": {
        "nombre": "ABC",
        "feeds": [
            "https://www.abc.es/rss/2.0/portada/",
            "https://www.abc.es/rss/feeds/abc_ultima.xml",
        ],
    },
    "lavanguardia": {
        "nombre": "La Vanguardia",
        "feeds": [
            "https://www.lavanguardia.com/rss/home.xml",
        ],
    },
}

CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 mis-noticias",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


# ---------------------------------------------------------------- horario

def toca_edicion():
    """En ejecuciones programadas, solo sigue si la hora programada
    corresponde a las 7:00 o las 18:00 de Madrid (cubre horario de verano
    e invierno). Usa la hora programada, no la real, para que un retraso
    de GitHub no haga saltar la edición."""
    cron = os.environ.get("HORA_PROGRAMADA", "").strip()
    if not cron:
        return True  # ejecución manual
    try:
        minuto, hora = cron.split()[:2]
        hoy = datetime.now(timezone.utc).date()
        programada = datetime(hoy.year, hoy.month, hoy.day, int(hora), int(minuto),
                              tzinfo=timezone.utc)
        local = programada.astimezone(MADRID)
        print(f"Programada {programada:%H:%M} UTC = {local:%H:%M} Madrid")
        return local.hour in HORAS_EDICION
    except Exception as e:
        print(f"No pude interpretar el cron '{cron}': {e}. Sigo igualmente.")
        return True


# ---------------------------------------------------------------- RSS

def limpiar(texto, maximo=None):
    texto = html.unescape(re.sub(r"<[^>]+>", " ", texto or ""))
    texto = re.sub(r"\s+", " ", texto).strip()
    if maximo and len(texto) > maximo:
        texto = texto[:maximo].rsplit(" ", 1)[0] + "…"
    return texto


def descargar(url):
    local = os.environ.get("FEEDS_LOCAL")  # solo para pruebas
    if local:
        ruta = Path(local) / (re.sub(r"[^a-z0-9]+", "_", url.lower()) + ".xml")
        return ruta.read_bytes() if ruta.exists() else b""
    r = requests.get(url, headers=CABECERAS, timeout=30)
    r.raise_for_status()
    return r.content


def leer_medio(clave, medio):
    for url in medio["feeds"]:
        try:
            feed = feedparser.parse(descargar(url))
        except Exception as e:
            print(f"  {medio['nombre']}: error en {url}: {e}")
            continue
        noticias, vistos = [], set()
        for e in feed.entries:
            titulo = limpiar(e.get("title"))
            enlace = (e.get("link") or "").strip()
            if not titulo or not enlace or titulo.lower() in vistos:
                continue
            vistos.add(titulo.lower())
            seccion = ""
            if e.get("tags"):
                seccion = limpiar(e.tags[0].get("term", ""))
            noticias.append({
                "titular": titulo,
                "url": enlace,
                "seccion": seccion,
                "entradilla": limpiar(e.get("summary"), 350),  # solo para Claude, no se publica
            })
            if len(noticias) >= MAX_NOTICIAS_POR_MEDIO:
                break
        if len(noticias) >= 5:
            print(f"  {medio['nombre']}: {len(noticias)} noticias ({url})")
            return noticias
        print(f"  {medio['nombre']}: solo {len(noticias)} en {url}, pruebo el siguiente")
    return []


# ---------------------------------------------------------------- Claude

HERRAMIENTA = {
    "name": "guardar_edicion",
    "description": "Guarda la edición resumida del día.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resumen_dia": {
                "type": "string",
                "description": "3-4 frases: lo más importante del día según el conjunto de los cuatro diarios.",
            },
            "temas_comunes": {
                "type": "array",
                "description": "De 3 a 6 temas que aparecen en al menos dos diarios, del más al menos relevante.",
                "items": {
                    "type": "object",
                    "properties": {
                        "titulo": {"type": "string", "description": "Nombre corto del tema (máx. 8 palabras)."},
                        "resumen": {"type": "string", "description": "1-2 frases con los hechos, neutrales."},
                        "medios": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "medio": {"type": "string", "enum": list(MEDIOS)},
                                    "enfoque": {"type": "string", "description": "Una frase: qué destaca o cómo lo enfoca ese diario."},
                                    "ids": {"type": "array", "items": {"type": "string"},
                                            "description": "Ids de las noticias de ese diario sobre el tema."},
                                },
                                "required": ["medio", "enfoque", "ids"],
                            },
                        },
                    },
                    "required": ["titulo", "resumen", "medios"],
                },
            },
            "medios": {
                "type": "array",
                "description": "Un elemento por cada diario recibido.",
                "items": {
                    "type": "object",
                    "properties": {
                        "medio": {"type": "string", "enum": list(MEDIOS)},
                        "resumen": {"type": "string", "description": "2 frases: qué prioriza hoy ese diario."},
                        "noticias": {
                            "type": "array",
                            "description": "Sus 6-8 noticias más relevantes, en orden de importancia.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "resumen": {"type": "string", "description": "Una frase que explique la noticia más allá del titular."},
                                },
                                "required": ["id", "resumen"],
                            },
                        },
                    },
                    "required": ["medio", "resumen", "noticias"],
                },
            },
        },
        "required": ["resumen_dia", "temas_comunes", "medios"],
    },
}

INSTRUCCIONES = """Eres el editor de un resumen de prensa personal en español.
Recibes los titulares y entradillas de las portadas de varios diarios españoles.

Reglas:
- Escribe siempre con tus propias palabras; no copies frases de las entradillas.
- Tono neutro y directo, sin adjetivos valorativos ni opinión propia.
- En "enfoque" describe qué subraya cada diario (dato, protagonista, encuadre) sin calificarlo.
- Prioriza actualidad política, económica, internacional y social; deja deportes,
  ocio y servicio solo si son de primer orden.
- Usa únicamente los ids que aparecen en la lista; no inventes noticias.
- Llama a la herramienta guardar_edicion con el resultado."""


def resumir_con_claude(datos):
    import anthropic

    bloques = []
    for clave, noticias in datos.items():
        lineas = [f"## {MEDIOS[clave]['nombre']} (medio: {clave})"]
        for i, n in enumerate(noticias):
            extra = f" [{n['seccion']}]" if n["seccion"] else ""
            lineas.append(f"- id {clave}-{i}{extra}: {n['titular']}. {n['entradilla']}")
        bloques.append("\n".join(lineas))

    cliente = anthropic.Anthropic()
    respuesta = cliente.messages.create(
        model=os.environ.get("MODELO") or "claude-haiku-5-5",
        max_tokens=8000,
        system=INSTRUCCIONES,
        tools=[HERRAMIENTA],
        tool_choice={"type": "tool", "name": "guardar_edicion"},
        messages=[{"role": "user", "content": "\n\n".join(bloques)}],
    )
    for bloque in respuesta.content:
        if bloque.type == "tool_use":
            return bloque.input
    raise RuntimeError("Claude no devolvió la edición")


# ---------------------------------------------------------------- montaje

def buscar(datos, ident):
    try:
        clave, i = ident.rsplit("-", 1)
        n = datos[clave][int(i)]
        return {"titular": n["titular"], "url": n["url"], "seccion": n["seccion"]}
    except (KeyError, ValueError, IndexError):
        return None


def montar(datos, ia, ahora):
    edicion = {
        "fecha": ahora.strftime("%Y-%m-%d"),
        "franja": "mañana" if ahora.hour < 13 else "tarde",
        "generada": ahora.isoformat(timespec="minutes"),
        "con_ia": ia is not None,
        "resumen_dia": "",
        "temas_comunes": [],
        "medios": [],
    }

    if ia:
        edicion["resumen_dia"] = ia.get("resumen_dia", "")
        for t in ia.get("temas_comunes", []):
            medios = []
            for m in t.get("medios", []):
                if m.get("medio") not in datos:
                    continue
                enlaces = [x for x in (buscar(datos, i) for i in m.get("ids", [])) if x]
                medios.append({"medio": m["medio"], "enfoque": m.get("enfoque", ""), "noticias": enlaces})
            if len(medios) >= 2:
                edicion["temas_comunes"].append({"titulo": t.get("titulo", ""), "resumen": t.get("resumen", ""), "medios": medios})

    por_medio = {m["medio"]: m for m in (ia or {}).get("medios", []) if m.get("medio") in datos}
    for clave in MEDIOS:
        if clave not in datos:
            edicion["medios"].append({"medio": clave, "nombre": MEDIOS[clave]["nombre"], "error": True,
                                      "resumen": "", "noticias": []})
            continue
        bloque = {"medio": clave, "nombre": MEDIOS[clave]["nombre"], "resumen": "", "noticias": []}
        if clave in por_medio:
            bloque["resumen"] = por_medio[clave].get("resumen", "")
            for n in por_medio[clave].get("noticias", []):
                base = buscar(datos, n.get("id", ""))
                if base and base["url"] not in {x["url"] for x in bloque["noticias"]}:
                    base["resumen"] = n.get("resumen", "")
                    bloque["noticias"].append(base)
        if not bloque["noticias"]:  # sin IA o fallo: titulares tal cual
            bloque["noticias"] = [{"titular": n["titular"], "url": n["url"], "seccion": n["seccion"], "resumen": ""}
                                  for n in datos[clave][:10]]
        edicion["medios"].append(bloque)
    return edicion


def guardar(edicion):
    carpeta = RAIZ / "ediciones"
    carpeta.mkdir(exist_ok=True)
    franja = "manana" if edicion["franja"] == "mañana" else "tarde"
    nombre = f"{edicion['fecha']}-{franja}.json"
    texto = json.dumps(edicion, ensure_ascii=False, indent=1)
    (carpeta / nombre).write_text(texto, encoding="utf-8")
    (RAIZ / "noticias.json").write_text(texto, encoding="utf-8")

    archivos = sorted((p for p in carpeta.glob("20*.json")), reverse=True)
    for viejo in archivos[EDICIONES_GUARDADAS:]:
        viejo.unlink()
    indice = []
    for p in archivos[:EDICIONES_GUARDADAS]:
        fecha, fr = p.stem.rsplit("-", 1)
        indice.append({"archivo": f"ediciones/{p.name}", "fecha": fecha,
                       "franja": "mañana" if fr == "manana" else "tarde"})
    (carpeta / "indice.json").write_text(json.dumps(indice, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Guardada {nombre} ({len(indice)} ediciones en el archivo)")


def main():
    if not toca_edicion():
        print("No es hora de edición en Madrid; nada que hacer.")
        return 0

    print("Leyendo diarios…")
    datos = {}
    for clave, medio in MEDIOS.items():
        noticias = leer_medio(clave, medio)
        if noticias:
            datos[clave] = noticias
    if not datos:
        print("No se pudo leer ningún diario.")
        return 1

    ia = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            print("Resumiendo con Claude…")
            ia = resumir_con_claude(datos)
        except Exception as e:
            print(f"Fallo al resumir ({e}); publico solo titulares.")
    else:
        print("Sin ANTHROPIC_API_KEY: publico solo titulares.")

    guardar(montar(datos, ia, datetime.now(MADRID)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
