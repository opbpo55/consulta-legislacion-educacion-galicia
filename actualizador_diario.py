"""
Actualizador diario autónomo del corpus legislativo educativo de Galicia (Opción 2 + ciclo anual).
Diseñado para ejecutarse en GitHub Actions (o en local) de forma programada.

Flujo:
1. Reensambla y abre la base de datos SQLite (si está en chunks).
2. Rastrea fuentes oficiales filtradas estrictamente a ESO, Bachillerato y FP:
   - Portal Educativo da Xunta (/portal/normativa por tid_level 83, 84, 85, 933, 215, 216, 217, 218)
   - Portal Educativo da Xunta (/portal/anuncios por tid_level 83, 84, 85, 933, 218)
   - Portal FP da Xunta (/fp/novas)
   - DOG (Diario Oficial de Galicia RSS) con filtrado estricto Opción 2
3. Gestiona ciclo anual e historial de derogaciones (sin borrar PDFs):
   - Detecta instrucciones/calendarios/admisión/pruebas de nuevo curso y marca versiones
     de cursos anteriores como `Superada (curso anterior ...)`
   - Sincroniza derogaciones recientes desde Lex.gal marcando `Derrogada (Lex.gal)`
   - Archiva los PDFs superados/derogados en `99_Historico_Derogadas` de Drive
4. Sube nuevos PDFs a Google Drive vía Apps Script Web App (`DRIVE_WEBHOOK_URL`) y actualiza `drive_map.json`.
5. Recomprime y refragmenta SQLite en `db_chunk_*.bin` y envía correo a `oscar.perez.instituto@gmail.com`.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import smtplib
import sqlite3
import time
import unicodedata
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "base_datos"
TMP_DIR = Path(os.environ.get("RUNNER_TEMP", "/tmp" if os.name != "nt" else str(BASE_DIR / ".tmp_update")))
RECIPIENT_EMAIL = os.environ.get("NOTIFY_EMAIL", "oscar.perez.instituto@gmail.com")
TARGET_SUBFOLDER = "08_Resolucions_Instrucions_Anuncios"
HISTORICO_SUBFOLDER = "99_Historico_Derogadas"
MAX_NEW_PER_RUN = 25
_DRIVE_WEBHOOK_UNCONFIGURED_LOGGED = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ActualizadorLegislativoGalicia/2.1 (Educacion)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_text(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text or "")
    plain = "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")
    return _clean_space(plain).lower()


def parse_date_iso(text: str, default_today: bool = False) -> str:
    t = _clean_space(text)
    m_es = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", t)
    if m_es:
        return f"{m_es.group(3)}-{m_es.group(2)}-{m_es.group(1)}"
    m_iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m_iso:
        return m_iso.group(0)
    m_num = re.search(r"\b(20\d{2})(\d{2})(\d{2})\b", t)
    if m_num:
        return f"{m_num.group(1)}-{m_num.group(2)}-{m_num.group(3)}"
    return datetime.now().strftime("%Y-%m-%d") if default_today else ""


def infer_doc_type(title: str, default: str = "Resolución / Anuncio") -> str:
    t = _clean_space(title).lower()
    if t.startswith("lei ") or "lei orgánica" in t or "ley orgánica" in t or t.startswith("ley "):
        return "Lei"
    if t.startswith("real decreto") or "real decreto" in t[:40]:
        return "Real Decreto"
    if t.startswith("decreto") or "decreto " in t[:35]:
        return "Decreto"
    if t.startswith(("orde ", "orden ")) or "orde do " in t[:35]:
        return "Orde"
    if "corrección de erros" in t or "corrección de errores" in t:
        return "Corrección de erros"
    if t.startswith("resolución") or "resolución" in t[:40]:
        return "Resolución"
    if "instrución" in t[:55] or "instrucción" in t[:55]:
        return "Instrución"
    if "circular" in t[:55]:
        return "Circular"
    if "acordo" in t[:45] or "acuerdo" in t[:45]:
        return "Acordo"
    if "protocolo" in t[:60]:
        return "Protocolo"
    if "convocatoria" in t[:60] or "listaxe" in t[:45] or "relación" in t[:45]:
        return "Anuncio / Convocatoria"
    return default


def infer_etapas(title: str, default_etapa: str = "Transversal") -> list[str]:
    t = _clean_space(title).lower()
    etapas: set[str] = set()
    if re.search(r"\beso\b", t) or any(
        k in t
        for k in (
            "educación secundaria obrigatoria",
            "educación secundaria obligatoria",
            "secundaria obrigatoria",
            "pmar",
            "diversificación curricular",
        )
    ):
        etapas.add("ESO")
    if any(k in t for k in ("bacharelato", "bachillerato")) or re.search(r"\b(abau|pau|ebau)\b", t):
        etapas.add("Bacharelato")
    if re.search(r"\b(fp|cifp|fct|pcpi)\b", t) or any(
        k in t
        for k in (
            "formación profesional",
            "fp dual",
            "grao básico",
            "grao medio",
            "grao superior",
            "ciclo formativo",
            "ciclos formativos",
            "curso de especialización",
            "especialización de fp",
            "acreditación de competencias",
            "cualificacións profesionais",
        )
    ):
        etapas.add("FP")
    if any(
        k in t
        for k in (
            "centros docentes",
            "atención á diversidade",
            "inclusión educativa",
            "convivencia",
            "calendario escolar",
            "admisión",
            "avaliación",
            "evaluación",
            "réxime xeral",
            "plurilingü",
            "consello escolar",
            "dirección dos centros",
            "inspección educativa",
            "profesorado",
            "non universitario",
            "sistema educativo",
        )
    ) or re.search(r"\bneae\b", t):
        etapas.add("Transversal")
    if not etapas and default_etapa:
        etapas.add(default_etapa)
    if not etapas:
        etapas.add("Transversal")
    return sorted(etapas)


def is_relevant_edu_option2(
    title: str,
    extra_text: str = "",
    etapa_hint: str = "",
    is_dog: bool = False,
) -> tuple[bool, str]:
    """
    Filtro estricto Opción 2:
    Permite normativa y convocatorias/programas de ESO, Bachillerato y FP.
    Excluye música/danza/artes plásticas/deportivas/EOI, universidad/máster/investigación
    (excepto ABAU/PAU/acceso a la universidad), Infantil/Primaria exclusiva y cultura ajena.
    """
    t = _clean_space(f"{title} {extra_text}").lower()
    if not t:
        return False, "empty_title"

    is_abau_pau = bool(re.search(r"\b(abau|pau|ebau)\b", t)) or any(
        k in t
        for k in (
            "proba de acceso á universidade",
            "prueba de acceso a la universidad",
            "acceso ás ensinanzas universitarias",
            "acceso a las enseñanzas universitarias",
        )
    )

    has_explicit_stage = (
        bool(re.search(r"\b(eso|fp|cifp|fct)\b", t))
        or any(
            k in t
            for k in (
                "educación secundaria obrigatoria",
                "educación secundaria obligatoria",
                "secundaria obrigatoria",
                "bacharelato",
                "bachillerato",
                "formación profesional",
                "fp dual",
                "ciclo formativo",
                "ciclos formativos",
                "grao básico",
                "grao medio",
                "grao superior",
                "curso de especialización",
                "ciberfp",
            )
        )
        or is_abau_pau
    )

    if not has_explicit_stage:
        artistic_excl = (
            "ensinanzas artísticas",
            "enseñanzas artísticas",
            "artísticas profesionais",
            "artísticas profesionales",
            "de música",
            "música e danza",
            "de danza",
            "artes plásticas",
            "arte dramática",
            "conservatorio",
            "ensinanzas deportivas",
            "enseñanzas deportivas",
            "escolas oficiais de idiomas",
            "escuelas oficiales de idiomas",
        )
        if any(k in t for k in artistic_excl) or re.search(r"\beoi\b", t):
            return False, "excluded_artistic_sports_eoi"

        if not is_abau_pau:
            uni_research_excl = (
                "titulacións universitarias",
                "máster universitario",
                "master universitario",
                "mención de excelencia",
                "sistema universitario de galicia",
                "doutoramento",
                "doctorado",
                "posdoutoral",
                "predoutoral",
                "talento investigador",
                "galtia",
                "fundación pública galega",
                "axencia galega de innovación",
                "universidade da coruña",
                "universidade de santiago",
                "universidade de vigo",
                "persoal investigador",
                "bolsas de investigación",
                "posto de persoal directivo",
            )
            if any(k in t for k in uni_research_excl):
                return False, "excluded_university_research"

        culture_excl = (
            "ben de interese cultural",
            "bens de interese cultural",
            "patrimonio cultural",
            "museo",
            "bibliotecas públicas",
            "federacións deportivas",
            "instalacións deportivas",
            "subvencións a concellos",
            "real academia",
        )
        if any(k in t for k in culture_excl):
            return False, "excluded_culture_non_edu"

    # Excluir Infantil / Primaria exclusiva incluso con etiqueta de portal equivocada
    has_ip = any(
        k in t
        for k in (
            "educación infantil",
            "educación primaria",
            "escolas infantís",
            "escuelas infantiles",
            "primeiro ciclo de infantil",
        )
    ) or bool(re.search(r"\bceip\b", t))
    has_target_general = (
        has_explicit_stage
        or bool(re.search(r"\bies\b", t))
        or any(
            k in t
            for k in (
                "secundaria",
                "centros docentes",
                "centros educativos",
                "todos os niveis",
                "réxime xeral",
                "non universitario",
                "no universitario",
            )
        )
    )
    if has_ip and not has_target_general:
        return False, "excluded_infantil_primaria_only"

    if etapa_hint in ("ESO", "Bacharelato", "FP") and not is_dog:
        return True, f"etapa_oficial:{etapa_hint}"

    transversal_signals = (
        "centros docentes",
        "centros públicos",
        "centros educativos",
        "alumnado",
        "profesorado",
        "corpos docentes",
        "calendario escolar",
        "admisión",
        "avaliación",
        "evaluación",
        "currículo",
        "curriculo",
        "convivencia",
        "diversidade",
        "inclusión educativa",
        "orientación educativa",
        "plurilingü",
        "eduemprende",
        "transporte escolar",
        "comedor escolar",
        "libros de texto",
        "non universitario",
        "no universitario",
        "instituto de educación secundaria",
    )
    if has_explicit_stage or any(k in t for k in transversal_signals) or bool(re.search(r"\b(neae|ies)\b", t)):
        return True, "matched_option2"

    return False, "no_target_stage_signal"


def get_filter_mode() -> str:
    """Devuelve el modo activo: 'opcion1' (por defecto) o 'opcion2' según FILTER_MODE."""
    raw = os.environ.get("FILTER_MODE", "opcion1").strip().lower()
    if raw in ("opcion2", "2", "completa"):
        return "opcion2"
    return "opcion1"


def is_relevant_edu(
    title: str,
    extra_text: str = "",
    etapa_hint: str = "",
    is_dog: bool = False,
    mode: str | None = None,
    from_normativa: bool = False,
) -> tuple[bool, str]:
    """
    Aplica el interruptor configurable:
      - opcion2: normativa + programas/convocatorias de ESO, Bachillerato y FP.
      - opcion1 (por defecto): estrictamente legislación y normativa oficial de ESO, Bachillerato y FP.
    """
    ok2, reason2 = is_relevant_edu_option2(
        title, extra_text=extra_text, etapa_hint=etapa_hint, is_dog=is_dog
    )
    if not ok2:
        return False, reason2

    raw_mode = (mode or get_filter_mode()).strip().lower()
    eff_mode = "opcion2" if raw_mode in ("opcion2", "2", "completa") else "opcion1"
    if eff_mode == "opcion2":
        return True, reason2

    t = _clean_space(f"{title} {extra_text}").lower()
    non_normative_excl = (
        "listaxe provisional",
        "listaxe definitiva",
        "relación provisional",
        "relación definitiva",
        "centros seleccionados",
        "centros excluídos",
        "proxectos seleccionados",
        "equipos presentados",
        "desafío",
        "concurso",
        "certame",
        "premios extraordinarios",
        "premios ",
        "eduemprende",
        "lanzadeiras",
        "ciberfp",
        "centros referentes",
        "proxectos de inclusión educativa",
        "(pie)",
        "plan proxecta",
        "polos creativos",
        "clubes de lectura",
        "club de lectura",
        "olimpíada",
        "olimpiada",
        "campamento",
        "xornadas ",
        "baremo provisional",
        "baremo definitivo",
        "proxectos de formación",
        "contrato-programa",
        "contratos-programa",
        "adxudicación definitiva de destinos",
        "adxudicación provisional",
    )
    if any(k in t for k in non_normative_excl):
        return False, "excluded_non_normative_option1"

    normative_ranks = (
        "lei ",
        "ley ",
        "real decreto",
        "decreto ",
        "orde do ",
        "orde pola ",
        "orden ",
        "instrución",
        "instrucción",
        "circular",
        "protocolo",
        "corrección de erros",
        "corrección de errores",
        "acordo do consello",
    )
    core_reg_topics = (
        "calendario escolar",
        "currículo",
        "curriculo",
        "avaliación",
        "evaluación",
        "promoción",
        "titulación",
        "admisión",
        "matrícula",
        "ordenación",
        "organización",
        "funcionamento",
        "comezo de curso",
        "inicio de curso",
        "inicio do curso",
        "horario",
        "horarios",
        "convivencia",
        "atención á diversidade",
        "diversidade",
        "orientación",
        "fp dual",
        "fct",
        "formación en centros de traballo",
        "probas libres",
        "probas de acceso",
        "pruebas de acceso",
        "acreditación de competencias",
        "convalidación",
        "validación",
        "abau",
        "pau",
        "acceso á universidade",
        "acceso ás ensinanzas universitarias",
        "plurilingü",
        "transporte escolar",
        "comedor escolar",
        "libros de texto",
        "centros docentes",
        "inspección educativa",
        "consello escolar",
        "xade",
    )

    if (
        from_normativa
        or any(k in t for k in normative_ranks)
        or (
            "resolución" in t
            and (any(k in t for k in core_reg_topics) or bool(re.search(r"\bneae\b", t)))
        )
    ):
        return True, "matched_option1"

    return False, "excluded_non_normative_option1"


def build_attachment_title(base_title: str, link_label: str, pdf_url: str, total_links: int) -> str:
    base = _clean_space(base_title) or "Resolución Portal Educativo"
    if total_links <= 1:
        return base
    lbl = _clean_space(link_label)
    generic_labels = {
        "pdf",
        "descargar",
        "descarga",
        "ver",
        "documento",
        "enlace",
        "arquivo",
        "archivo",
        "abrir",
        "premer",
    }
    if lbl and lbl.lower() not in generic_labels and lbl.lower() != base.lower() and not lbl.lower().endswith(".pdf"):
        return f"{base} — {lbl}"
    path_name = unquote(Path(urlparse(pdf_url).path).stem)
    stem = _clean_space(re.sub(r"[_\-]+", " ", path_name))
    if stem and stem.lower() != base.lower():
        return f"{base} — {stem}"
    return base


def extract_academic_year(title: str) -> tuple[int, int] | None:
    """Extrae el curso académico (p. ej. 2026/2027 o 2026-27) de un título oficial."""
    t = _clean_space(title)
    m = re.search(r"\b(20\d{2})\s*[-/]\s*(20\d{2}|\d{2})\b", t)
    if not m:
        return None
    y1 = int(m.group(1))
    y2_raw = m.group(2)
    y2 = int(y2_raw) if len(y2_raw) == 4 else (y1 // 100) * 100 + int(y2_raw)
    if y2 != y1 + 1:
        return None
    return (y1, y2)


def classify_annual_series(title: str) -> str | None:
    """Identifica las series documentales periódicas de renovación anual."""
    t = _normalize_text(title)
    if not t:
        return None

    has_instruc = any(k in t for k in ("instrucion", "instrucions", "instruccion", "instrucciones", "comenzo de curso", "inicio do curso", "inicio de curso"))
    has_fp = bool(re.search(r"\bfp\b", t)) or "formacion profesional" in t or "ciclos formativos" in t or "grao basico" in t or "grao medio" in t or "grao superior" in t
    has_eso_bach = bool(re.search(r"\beso\b", t)) or "secundaria" in t or "bacharelato" in t or "bachillerato" in t

    if "calendario escolar" in t:
        return "calendario_escolar"
    if has_instruc and has_fp and not has_eso_bach:
        return "instrucions_curso_fp"
    if has_instruc and (has_eso_bach or "centros docentes" in t or "ensenanzas" in t):
        return "instrucions_curso_eso_bach"
    if "admision" in t and has_fp:
        return "admision_fp"
    if "admision" in t and ("alumnado" in t or "centros docentes" in t or has_eso_bach):
        return "admision_alumnado"
    if ("proba" in t or "probas" in t) and "acceso" in t and has_fp:
        return "probas_acceso_fp"
    if ("probas libres" in t or "proba libre" in t) and (bool(re.search(r"\beso\b", t)) or "educacion secundaria" in t or "graduado en educacion secundaria" in t):
        return "probas_libres_eso"
    if ("probas libres" in t or "proba libre" in t) and ("bacharelato" in t or "bachillerato" in t):
        return "probas_libres_bach"
    if ("probas libres" in t or "proba libre" in t) and has_fp:
        return "probas_libres_fp"
    if bool(re.search(r"\b(abau|pau|ebau)\b", t)) or "acceso as ensinanzas universitarias" in t or "acceso a las ensenanzas universitarias" in t:
        return "pau_abau"
    if "premios extraordinarios" in t and (has_eso_bach or has_fp):
        return "premios_eso_bach_fp"
    return None


def _extract_lead_norm_id(title: str) -> str:
    t = _normalize_text(title)
    m = re.match(r"^(real decreto|decreto|lei organica|lei|ley organica|ley)\s+(\d+/\d{4})\b", t)
    if m:
        kind = m.group(1).replace("ley ", "lei ")
        return f"{kind} {m.group(2)}"
    m_date = re.match(r"^(orde|orden|resolucion)\s+do\s+(\d{1,2}\s+de\s+[a-z]+\s+de\s+\d{4})\b", t)
    if m_date:
        kind = "orde" if m_date.group(1) in ("orde", "orden") else "resolucion"
        return f"{kind} do {m_date.group(2)}"
    return ""


def prepare_local_sqlite() -> Path:
    """Asegura que la base SQLite esté descomprimida y lista para actualizar."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    db_path = TMP_DIR / "legislacion_actualizable.db"

    local_db = DB_DIR / "legislacion_galicia_web.db"
    if local_db.is_file():
        shutil.copy2(local_db, db_path)
        return db_path

    chunks = sorted(DB_DIR.glob("db_chunk_*.bin"))
    gz_tmp = TMP_DIR / "temp_web.db.gz"

    if chunks:
        print(f"[Init] Reensamblando base desde {len(chunks)} fragmentos...")
        with gz_tmp.open("wb") as out_f:
            for ch in chunks:
                with ch.open("rb") as in_f:
                    shutil.copyfileobj(in_f, out_f, 1024 * 1024)
        with gzip.open(gz_tmp, "rb") as in_gz, db_path.open("wb") as out_db:
            shutil.copyfileobj(in_gz, out_db, 1024 * 1024)
        return db_path

    gz_file = DB_DIR / "legislacion_galicia_web.db.gz"
    if gz_file.is_file():
        with gzip.open(gz_file, "rb") as in_gz, db_path.open("wb") as out_db:
            shutil.copyfileobj(in_gz, out_db, 1024 * 1024)
        return db_path

    raise FileNotFoundError("No se encontró base de datos SQLite ni fragmentos en base_datos/")


def get_existing_signatures(conn: sqlite3.Connection) -> tuple[set[str], set[str], set[str]]:
    """Obtiene los URLs, nombres de archivo y hashes SHA256 ya indexados para no duplicar."""
    c = conn.cursor()
    c.execute("SELECT pdf_url, detail_url, file_name, sha256 FROM documentos")
    urls: set[str] = set()
    files: set[str] = set()
    hashes: set[str] = set()
    for p_url, d_url, f, sh in c.fetchall():
        if p_url:
            urls.add(p_url.strip().lower())
        if d_url:
            urls.add(d_url.strip().lower())
        if f:
            files.add(f.strip().lower())
        if sh:
            hashes.add(sh.strip().lower())
    return urls, files, hashes


def _collect_pdf_links_from_node(session: requests.Session, node_url: str, base_title: str) -> tuple[str, str, list[dict]]:
    r = session.get(node_url, headers=HEADERS, timeout=18)
    if r.status_code != 200:
        return base_title, "", []
    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.select_one("h1.page-header, h1#page-title, article h1, h1")
    node_title = _clean_space(h1.get_text(" ", strip=True)) if h1 else ""
    final_title = node_title or base_title

    pub_date = ""
    date_el = soup.select_one(".field-name-post-date, .submitted, time")
    if date_el:
        pub_date = parse_date_iso(date_el.get_text(" ", strip=True))

    seen_urls: set[str] = set()
    links: list[dict] = []
    content_scope = soup.select_one("article, .node, .region-content, main") or soup
    for a in content_scope.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        abs_url = urljoin(node_url, href)
        if "/sites/fp/files/" in abs_url and "/fp/sites/fp/files/" not in abs_url:
            abs_url = abs_url.replace("/sites/fp/files/", "/fp/sites/fp/files/")
        low = abs_url.lower()
        is_pdf = ".pdf" in urlparse(low).path
        is_dog_html = "xunta.gal/dog/" in low and low.endswith(".html")
        if not is_pdf and not is_dog_html:
            continue
        if is_dog_html:
            abs_url = abs_url[:-5] + ".pdf"
        if abs_url.lower() in seen_urls:
            continue
        seen_urls.add(abs_url.lower())
        links.append({"url": abs_url, "label": _clean_space(a.get_text(" ", strip=True))})

    return final_title, pub_date, links


def check_portal_normativa(session: requests.Session, filter_mode: str | None = None) -> list[dict]:
    """Rastrea normativa reciente filtrada por códigos oficiales tid_level de ESO, Bach y FP."""
    level_queries = [
        ("ESO", {"tid_level": "83"}),
        ("Bacharelato", {"tid_level": "84"}),
        ("FP", {"tid_level": "85"}),
        ("FP", {"tid_level": "933"}),
        ("ESO", {"tid_level": "215"}),
        ("FP", {"tid_level": "216"}),
        ("Bacharelato", {"tid_level": "217"}),
        ("Transversal", {"tid_level": "218"}),
    ]
    nodes: dict[str, dict] = {}
    base_url = "https://www.edu.xunta.gal/portal/normativa"

    for etapa_label, params in level_queries:
        try:
            r = session.get(base_url, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 404:
                q_full = {
                    "title": "",
                    "tid_tematic": "All",
                    "tid_department": "All",
                    "tid_group": "All",
                    "tid_level": params.get("tid_level", "All"),
                    "date_filter_from[value][date]": "",
                    "date_filter_to[value][date]": "",
                    "tid_boletin": "All",
                    "tid_rango": "All",
                    "numero": "",
                }
                r = session.get(base_url, params=q_full, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.select(".view-content .views-row")[:15]:
                a = row.select_one(".views-field-title a")
                if not a or not a.get("href"):
                    continue
                node_url = urljoin("https://www.edu.xunta.gal", a["href"])
                title = _clean_space(a.get_text(" ", strip=True))
                d_el = row.select_one(".views-field-created")
                pub_date = parse_date_iso(d_el.get_text(" ", strip=True)) if d_el else ""
                ok_pre, _ = is_relevant_edu(title, etapa_hint=etapa_label, mode=filter_mode, from_normativa=True)
                if not ok_pre:
                    continue
                entry = nodes.setdefault(node_url, {"title": title, "pub_date": pub_date, "etapas": set(), "hint": etapa_label})
                entry["etapas"].add(etapa_label)
        except Exception as e:
            print(f"[Crawler] Error en portal/normativa ({etapa_label}): {e}")

    candidates: list[dict] = []
    for node_url, info in nodes.items():
        try:
            full_title, node_date, pdf_links = _collect_pdf_links_from_node(session, node_url, info["title"])
            pub_date = info["pub_date"] or node_date
            for lk in pdf_links:
                doc_title = build_attachment_title(full_title, lk["label"], lk["url"], len(pdf_links))
                ok, _ = is_relevant_edu(doc_title, full_title, etapa_hint=info["hint"], mode=filter_mode, from_normativa=True)
                if not ok:
                    continue
                etapas = sorted(set(info["etapas"]) | set(infer_etapas(doc_title, info["hint"])))
                candidates.append({
                    "title": doc_title,
                    "source_url": lk["url"],
                    "detail_url": node_url,
                    "pub_date": pub_date,
                    "ambito": "Autonómico (Galicia)",
                    "etapa": etapas[0],
                    "etapas": etapas,
                    "origen": "Portal Educativo Xunta (Normativa)",
                })
        except Exception:
            continue
    return candidates


def check_portal_anuncios(session: requests.Session, filter_mode: str | None = None) -> list[dict]:
    """Rastrea anuncios y resoluciones recientes filtrados por tid_level de ESO, Bach y FP."""
    levels = [
        ("ESO", "83"),
        ("Bacharelato", "84"),
        ("FP", "85"),
        ("FP", "933"),
        ("Transversal", "218"),
    ]
    nodes: dict[str, dict] = {}
    base_url = "https://www.edu.xunta.gal/portal/anuncios"

    for etapa_label, tid in levels:
        try:
            r = session.get(base_url, params={"tid_level": tid}, headers=HEADERS, timeout=20)
            if r.status_code == 404:
                params_full = {
                    "title": "",
                    "tid_tematic": "All",
                    "tid_department": "All",
                    "tid_group": "All",
                    "tid_level": tid,
                    "date_filter_from[value][date]": "",
                    "date_filter_to[value][date]": "",
                }
                r = session.get(base_url, params=params_full, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".grid-advertisements")
            if not cards:
                cards = [el.parent for el in soup.select(".views-field-title") if el.parent]
            for card in cards[:15]:
                a = card.select_one(".views-field-title a")
                if not a or not a.get("href"):
                    continue
                node_url = urljoin("https://www.edu.xunta.gal", a["href"])
                title = _clean_space(a.get_text(" ", strip=True))
                d_el = card.select_one(".views-field-created")
                pub_date = parse_date_iso(d_el.get_text(" ", strip=True)) if d_el else ""
                ok_pre, _ = is_relevant_edu(title, etapa_hint=etapa_label, mode=filter_mode, from_normativa=False)
                if not ok_pre:
                    continue
                entry = nodes.setdefault(node_url, {"title": title, "pub_date": pub_date, "etapas": set(), "hint": etapa_label})
                entry["etapas"].add(etapa_label)
        except Exception as e:
            print(f"[Crawler] Error en portal/anuncios ({etapa_label}): {e}")

    candidates: list[dict] = []
    for node_url, info in nodes.items():
        try:
            full_title, node_date, pdf_links = _collect_pdf_links_from_node(session, node_url, info["title"])
            pub_date = info["pub_date"] or node_date
            for lk in pdf_links:
                doc_title = build_attachment_title(full_title, lk["label"], lk["url"], len(pdf_links))
                ok, _ = is_relevant_edu(doc_title, full_title, etapa_hint=info["hint"], mode=filter_mode, from_normativa=False)
                if not ok:
                    continue
                etapas = sorted(set(info["etapas"]) | set(infer_etapas(doc_title, info["hint"])))
                candidates.append({
                    "title": doc_title,
                    "source_url": lk["url"],
                    "detail_url": node_url,
                    "pub_date": pub_date,
                    "ambito": "Autonómico (Galicia)",
                    "etapa": etapas[0],
                    "etapas": etapas,
                    "origen": "Portal Educativo Xunta (Anuncios)",
                })
        except Exception:
            continue
    return candidates


def check_fp_portal(session: requests.Session, filter_mode: str | None = None) -> list[dict]:
    """Rastrea novedades y resoluciones recientes del Portal de Formación Profesional."""
    candidates: list[dict] = []
    novas_url = "https://www.edu.xunta.gal/fp/novas"
    try:
        r = session.get(novas_url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return candidates
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select(".view-content .views-row, article.node")
        visited: set[str] = set()
        for item in items[:15]:
            a = item.select_one("h2 a, h3 a, .views-field-title a, a[href]")
            if not a or not a.get("href"):
                continue
            node_url = urljoin(novas_url, a["href"])
            if node_url in visited:
                continue
            visited.add(node_url)
            title = _clean_space(a.get_text(" ", strip=True))
            full_title, node_date, pdf_links = _collect_pdf_links_from_node(session, node_url, title)
            for lk in pdf_links:
                doc_title = build_attachment_title(full_title, lk["label"], lk["url"], len(pdf_links))
                ok, _ = is_relevant_edu(doc_title, full_title, etapa_hint="FP", mode=filter_mode, from_normativa=False)
                if not ok:
                    continue
                etapas = sorted({"FP"} | set(infer_etapas(doc_title, "FP")))
                candidates.append({
                    "title": doc_title,
                    "source_url": lk["url"],
                    "detail_url": node_url,
                    "pub_date": node_date,
                    "ambito": "Autonómico (Galicia)",
                    "etapa": "FP",
                    "etapas": etapas,
                    "origen": "Portal FP Xunta",
                })
    except Exception as e:
        print(f"[Crawler] Error en fp/novas: {e}")
    return candidates


def check_dog_rss(session: requests.Session, filter_mode: str | None = None) -> list[dict]:
    """Rastrea el RSS diario del DOG aplicando el filtro configurable (opcion1 / opcion2)."""
    candidates: list[dict] = []
    rss_url = "https://www.xunta.gal/diario-oficial-galicia/rss/"
    try:
        r = session.get(rss_url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, "xml")
            for it in soup.find_all("item"):
                title = _clean_space(it.title.get_text(strip=True)) if it.title else ""
                desc = _clean_space(it.description.get_text(strip=True)) if it.description else ""
                link = _clean_space(it.link.get_text(strip=True)) if it.link else ""
                pub_raw = _clean_space(it.pubDate.get_text(strip=True)) if it.pubDate else ""
                if not link:
                    continue
                ok, _ = is_relevant_edu(title, desc, is_dog=True, mode=filter_mode, from_normativa=False)
                if not ok:
                    continue
                etapas = infer_etapas(f"{title} {desc}")
                candidates.append({
                    "title": title or "Resolución DOG",
                    "source_url": link,
                    "detail_url": link,
                    "pub_date": parse_date_iso(pub_raw),
                    "ambito": "Autonómico (Galicia)",
                    "etapa": etapas[0],
                    "etapas": etapas,
                    "origen": "Diario Oficial de Galicia (DOG)",
                })
    except Exception as e:
        print(f"[Crawler] Error en DOG RSS: {e}")
    return candidates


def download_and_extract_pdf(session: requests.Session, pdf_url: str, title: str) -> dict | None:
    """Descarga el PDF y extrae el texto página a página con PyPDF."""
    import pypdf

    try:
        r = session.get(pdf_url, headers=HEADERS, timeout=30)
        if r.status_code != 200 or len(r.content) < 1000:
            return None

        if "text/html" in (r.headers.get("Content-Type") or "").lower():
            soup = BeautifulSoup(r.text, "html.parser")
            pdf_a = soup.find("a", href=re.compile(r"\.pdf($|\?)", re.I))
            if pdf_a:
                real_pdf_url = urljoin(pdf_url, pdf_a["href"])
                r = session.get(real_pdf_url, headers=HEADERS, timeout=30)
            else:
                return None

        if not r.content.startswith(b"%PDF"):
            return None

        sha256_hash = hashlib.sha256(r.content).hexdigest()
        clean_slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", title)[:55].strip("_") or "resolucion"
        file_name = f"{datetime.now().strftime('%Y%m%d')}_{clean_slug}_{sha256_hash[:8]}.pdf"

        saved_path = TMP_DIR / file_name
        saved_path.write_bytes(r.content)

        reader = pypdf.PdfReader(str(saved_path))
        pages_text = []
        for idx, page in enumerate(reader.pages, start=1):
            txt = page.extract_text() or ""
            pages_text.append({"page_num": idx, "text": txt.strip()})

        return {
            "file_name": file_name,
            "file_path": saved_path,
            "page_count": len(reader.pages),
            "pages": pages_text,
            "raw_bytes": r.content,
            "sha256": sha256_hash,
        }
    except Exception as e:
        print(f"[PDF] Error procesando {pdf_url}: {e}")
        return None


def index_new_document(conn: sqlite3.Connection, cand: dict, pdf_data: dict) -> str:
    """Inserta el nuevo documento y sus páginas en la base SQLite compactada."""
    c = conn.cursor()
    raw_b = pdf_data.get("raw_bytes") or b""
    doc_hash = (
        hashlib.sha256(raw_b).hexdigest()[:12]
        if raw_b
        else hashlib.sha256(pdf_data["file_name"].encode("utf-8")).hexdigest()[:12]
    )
    next_id = f"doc_{doc_hash}"

    today_str = datetime.now().strftime("%Y-%m-%d")
    pub_date = cand.get("pub_date") or today_str
    title = cand.get("title", "Resolución Oficial")
    ambito = cand.get("ambito", "Autonómico (Galicia)")
    etapas = cand.get("etapas") or infer_etapas(title, cand.get("etapa", "Transversal"))
    doc_type = infer_doc_type(title)
    total_chars = sum(len(p["text"]) for p in pdf_data["pages"])
    file_bytes = len(raw_b)
    sha256 = pdf_data.get("sha256") or hashlib.sha256(raw_b).hexdigest()
    selectable_pages = sum(1 for p in pdf_data["pages"] if len(p["text"]) > 20)
    is_selectable = 1 if selectable_pages > 0 else 0

    c.execute(
        """
        INSERT INTO documentos (
            doc_id, title, file_name, original_filename, relative_path, category_folder,
            source_portal, source_portals_json, pdf_url, detail_url, etapas_json,
            doc_type, ambito, boletin, pub_date, vigencia_status, page_count, total_chars,
            file_size_bytes, sha256, selectable_pages, is_selectable,
            pdf_metadata_json, extra_meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            next_id,
            title,
            pdf_data["file_name"],
            pdf_data["file_name"],
            f"{TARGET_SUBFOLDER}/{pdf_data['file_name']}",
            TARGET_SUBFOLDER,
            cand.get("origen", "Portal Educativo"),
            json.dumps([cand.get("origen", "Portal Educativo")], ensure_ascii=False),
            cand["source_url"],
            cand.get("detail_url") or cand["source_url"],
            json.dumps(etapas, ensure_ascii=False),
            doc_type,
            ambito,
            cand.get("origen", "DOG / Portal Educativo"),
            pub_date,
            "Vixente",
            pdf_data["page_count"],
            total_chars,
            file_bytes,
            sha256,
            selectable_pages,
            is_selectable,
            json.dumps({"page_count": pdf_data["page_count"]}),
            json.dumps({"auto_indexed": True, "date": today_str, "filter_mode": get_filter_mode()}),
        ),
    )

    for p in pdf_data["pages"]:
        c.execute(
            """
            INSERT OR REPLACE INTO paginas_meta (doc_id, pdf_page, boletin_page, char_count)
            VALUES (?, ?, ?, ?)
            """,
            (next_id, p["page_num"], str(p["page_num"]), len(p["text"])),
        )
        if p["text"]:
            chunk_id = f"{next_id}_p{p['page_num']}_c0"
            c.execute(
                """
                INSERT INTO fragmentos (chunk_id, doc_id, pdf_page, boletin_page, article_ref, title, chunk_index_in_page, chunk_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, next_id, p["page_num"], str(p["page_num"]), "", title, 0, p["text"][:3800]),
            )

    try:
        c.execute("INSERT INTO fragmentos_fts(fragmentos_fts) VALUES('rebuild');")
    except Exception:
        pass

    conn.commit()
    print(f"[DB] Indexado con éxito: ID {next_id} - '{title}' ({pdf_data['page_count']} páxs)")
    return next_id


def _is_already_inactive(vigencia_status: str | None) -> bool:
    low = (vigencia_status or "").strip().lower()
    return low.startswith("derrogad") or low.startswith("derogad") or low.startswith("superad")


def mark_superseded_annual_docs(conn: sqlite3.Connection, new_doc_id: str, new_title: str) -> list[dict]:
    """Marca como superadas versiones de cursos anteriores pertenecientes a la misma serie anual."""
    new_series = classify_annual_series(new_title)
    new_year = extract_academic_year(new_title)
    if not new_series or not new_year:
        return []

    rows = conn.execute(
        "SELECT doc_id, title, file_name, relative_path, vigencia_status FROM documentos WHERE doc_id != ?",
        (new_doc_id,),
    ).fetchall()

    superseded: list[dict] = []
    ny1, ny2 = new_year
    for doc_id, title, file_name, rel_path, vig in rows:
        if _is_already_inactive(vig):
            continue
        if classify_annual_series(title or "") != new_series:
            continue
        old_year = extract_academic_year(title or "")
        if not old_year or old_year >= new_year:
            continue
        oy1, oy2 = old_year
        new_status = f"Superada (curso anterior {oy1}/{oy2}; vixente {ny1}/{ny2})"
        conn.execute("UPDATE documentos SET vigencia_status = ? WHERE doc_id = ?", (new_status, doc_id))
        superseded.append({
            "doc_id": doc_id,
            "title": title,
            "file_name": file_name,
            "relative_path": rel_path,
            "reason": f"Sustituida pola edición {ny1}/{ny2}",
        })

    if superseded:
        conn.commit()
        print(f"[Ciclo Anual] Marcados {len(superseded)} documentos anteriores de '{new_series}' como superados.")
    return superseded


def sync_lexgal_derogations(session: requests.Session, conn: sqlite3.Connection) -> list[dict]:
    """Consulta derogaciones recientes en Lex.gal (pág. 1) y actualiza vigencia_status."""
    derog_norm_ids: set[str] = set()
    derog_titles: set[str] = set()

    for dept in ("CECUFP---", "CEFPU----"):
        try:
            r = session.get(
                "https://www.lex.gal/normativa",
                params={"departamento": dept, "vixencia": "2", "numElementos": "100", "pax": "1"},
                headers=HEADERS,
                timeout=20,
            )
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select('a[href*="/normativa/detalle/"]'):
                t = _clean_space(a.get_text(" ", strip=True))
                if not t:
                    continue
                derog_titles.add(_normalize_text(t))
                nid = _extract_lead_norm_id(t)
                if nid:
                    derog_norm_ids.add(nid)
        except Exception as exc:
            print(f"[Lex.gal] Aviso comprobando derogaciones ({dept}): {exc}")

    if not derog_titles and not derog_norm_ids:
        return []

    rows = conn.execute("SELECT doc_id, title, file_name, relative_path, vigencia_status FROM documentos").fetchall()
    newly_derogated: list[dict] = []
    for doc_id, title, file_name, rel_path, vig in rows:
        if _is_already_inactive(vig):
            continue
        tnorm = _normalize_text(title or "")
        nid = _extract_lead_norm_id(title or "")
        if tnorm in derog_titles or (nid and nid in derog_norm_ids):
            conn.execute("UPDATE documentos SET vigencia_status = 'Derrogada (Lex.gal)' WHERE doc_id = ?", (doc_id,))
            newly_derogated.append({
                "doc_id": doc_id,
                "title": title,
                "file_name": file_name,
                "relative_path": rel_path,
                "reason": "Derrogada segundo Lex.gal",
            })

    if newly_derogated:
        conn.commit()
        print(f"[Lex.gal] Marcados {len(newly_derogated)} documentos como 'Derrogada (Lex.gal)'.")
    return newly_derogated


def save_and_rechunk_database(db_path: Path):
    """Recomprime la base de datos y la divide en trozos <42MB para GitHub/Render."""
    print("[Export] Recomprimiendo base de datos...")
    gz_path = DB_DIR / "legislacion_galicia_web.db.gz"
    with db_path.open("rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, 1024 * 1024)

    for old_ch in DB_DIR.glob("db_chunk_*.bin"):
        old_ch.unlink()

    chunk_sz = 42 * 1024 * 1024
    part = 0
    with gz_path.open("rb") as f_gz:
        while True:
            data = f_gz.read(chunk_sz)
            if not data:
                break
            part += 1
            part_path = DB_DIR / f"db_chunk_{part:02d}.bin"
            part_path.write_bytes(data)
            print(f"[Export] Generado {part_path.name}: {len(data)/1024/1024:.2f} MB")


def update_local_drive_map(
    relative_path: str,
    file_name: str,
    file_id: str,
    map_path: Path | None = None,
    remove_relative_path: str = "",
) -> bool:
    """Actualiza de forma atómica el drive_map.json del repositorio conservando las entradas previas."""
    target = map_path or (BASE_DIR / "drive_map.json")
    try:
        if target.is_file():
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {}
        else:
            payload = {}

        by_rel = payload.get("by_relative_path")
        if not isinstance(by_rel, dict):
            by_rel = {}
        by_name = payload.get("by_file_name")
        if not isinstance(by_name, dict):
            by_name = {}

        if remove_relative_path:
            old_norm = remove_relative_path.replace("\\", "/").lstrip("/")
            if old_norm != relative_path.replace("\\", "/").lstrip("/"):
                by_rel.pop(old_norm, None)

        rel_norm = relative_path.replace("\\", "/").lstrip("/")
        by_rel[rel_norm] = file_id
        by_name[file_name] = file_id

        payload["by_relative_path"] = by_rel
        payload["by_file_name"] = by_name
        payload["total_files"] = len(by_rel)
        payload["generated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

        tmp_target = target.with_suffix(".json.tmp")
        tmp_target.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp_target.replace(target)
        return True
    except Exception as exc:
        print(f"[Drive] Error actualizando {target.name}: {exc}")
        return False


def _post_to_drive_webhook(payload: dict) -> dict | None:
    global _DRIVE_WEBHOOK_UNCONFIGURED_LOGGED
    webhook_url = os.environ.get("DRIVE_WEBHOOK_URL", "").strip()
    webhook_token = os.environ.get("DRIVE_WEBHOOK_TOKEN", "").strip()
    if not webhook_url or not webhook_token:
        if not _DRIVE_WEBHOOK_UNCONFIGURED_LOGGED:
            print("[Drive] AVISO: DRIVE_WEBHOOK_URL o DRIVE_WEBHOOK_TOKEN no configurados; se omite operación en Drive.")
            _DRIVE_WEBHOOK_UNCONFIGURED_LOGGED = True
        return None

    req_payload = dict(payload)
    req_payload["token"] = webhook_token
    try:
        r = requests.post(webhook_url, json=req_payload, timeout=90, allow_redirects=True)
        if r.status_code != 200:
            print(f"[Drive] Error HTTP {r.status_code} llamando al webhook de Drive.")
            return None
        data = r.json()
        if data.get("ok") is True:
            return data
        print(f"[Drive] Error devuelto por Apps Script: {data.get('error', data)}")
        return None
    except Exception as exc:
        print(f"[Drive] Excepción llamando al webhook de Drive: {exc}")
        return None


def upload_pdf_to_drive_webhook(
    pdf_data: dict,
    subfolder: str = TARGET_SUBFOLDER,
    drive_map_path: Path | None = None,
) -> dict | None:
    """Sube un PDF nuevo a Google Drive llamando al Web App de Apps Script."""
    raw_bytes = pdf_data.get("raw_bytes") or b""
    if not raw_bytes and pdf_data.get("file_path") and Path(pdf_data["file_path"]).is_file():
        raw_bytes = Path(pdf_data["file_path"]).read_bytes()

    if not raw_bytes:
        print(f"[Drive] AVISO: Sin bytes válidos para subir {pdf_data.get('file_name', '')}.")
        return None

    max_bytes = 35 * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        print(f"[Drive] AVISO: '{pdf_data.get('file_name')}' supera 35 MB ({len(raw_bytes)/1024/1024:.1f} MB); se omite subida vía Apps Script.")
        return None

    data = _post_to_drive_webhook({
        "action": "upload",
        "file_name": pdf_data["file_name"],
        "subfolder": subfolder,
        "pdf_base64": base64.b64encode(raw_bytes).decode("ascii"),
    })
    if data and data.get("file_id"):
        print(f"[Drive] Subido a Google Drive: {pdf_data['file_name']} (ID: {data['file_id']})")
        return data
    return None


def archive_pdf_in_drive_webhook(doc_info: dict, conn: sqlite3.Connection | None = None) -> dict | None:
    """Mueve un PDF superado o derogado a 99_Historico_Derogadas en Google Drive sin borrarlo."""
    rel_path = (doc_info.get("relative_path") or "").replace("\\", "/").lstrip("/")
    file_name = doc_info.get("file_name") or (Path(rel_path).name if rel_path else "")
    if not file_name:
        return None

    drive_fid = ""
    map_file = BASE_DIR / "drive_map.json"
    if map_file.is_file():
        try:
            dm = json.loads(map_file.read_text(encoding="utf-8"))
            drive_fid = (dm.get("by_relative_path") or {}).get(rel_path, "") or (dm.get("by_file_name") or {}).get(file_name, "")
        except Exception:
            pass

    res = _post_to_drive_webhook({
        "action": "archive",
        "file_name": file_name,
        "relative_path": rel_path,
        "file_id": drive_fid,
        "target_subfolder": HISTORICO_SUBFOLDER,
    })
    if res and res.get("file_id"):
        new_rel = res.get("new_relative_path") or f"{HISTORICO_SUBFOLDER}/{file_name}"
        update_local_drive_map(new_rel, file_name, str(res["file_id"]), remove_relative_path=rel_path)
        if conn is not None and doc_info.get("doc_id"):
            conn.execute(
                "UPDATE documentos SET category_folder = ?, relative_path = ? WHERE doc_id = ?",
                (HISTORICO_SUBFOLDER, new_rel, doc_info["doc_id"]),
            )
            conn.commit()
        print(f"[Drive] Archivado en {HISTORICO_SUBFOLDER}: {file_name}")
        return res
    return None


def send_notification_email(
    updated_docs: list[dict],
    sources_checked: list[str],
    archived_docs: list[dict] | None = None,
    filter_mode: str = "opcion1",
):
    """Envía un correo electrónico con los resultados de la comprobación."""
    smtp_user = os.environ.get("GMAIL_USER", RECIPIENT_EMAIL)
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    archived = archived_docs or []
    eff_mode = "opcion2" if (filter_mode or "").strip().lower() == "opcion2" else "opcion1"
    mode_label = (
        "Opción 2 (Lexislación + convocatorias/programas ESO/Bach/FP)"
        if eff_mode == "opcion2"
        else "Opción 1 (Solo lexislación e normativa ESO/Bach/FP)"
    )

    today_str = datetime.now().strftime("%d/%m/%Y a las %H:%M")

    msg = MIMEMultipart()
    msg["From"] = f"Actualizador Legislación Galicia <{smtp_user}>"
    msg["To"] = RECIPIENT_EMAIL

    archived_html = ""
    if archived:
        items_arch = "".join(
            f"<li><strong>{a.get('title', a.get('file_name'))}</strong> — <em>{a.get('reason', 'Histórico')}</em></li>"
            for a in archived
        )
        archived_html = f"""
        <div style="background: #fffaf0; padding: 14px; border-radius: 6px; border-left: 4px solid #dd6b20; margin: 15px 0;">
            <p style="margin-top: 0;"><strong>📁 Normas anteriores pasadas a histórico (non borradas):</strong></p>
            <ul style="margin-bottom: 0; padding-left: 20px;">{items_arch}</ul>
        </div>
        """

    if not updated_docs and not archived:
        msg["Subject"] = f"✅ Comprobación Legislativa Galicia: Sen novidades ({datetime.now().strftime('%d/%m/%Y')})"
        none_detail = (
            "Non se detectaron novas ordes, decretos, resolucións nin convocatorias de ESO, Bacharelato ou FP nas últimas 24 horas."
            if eff_mode == "opcion2"
            else "Non se detectaron novas leis, decretos, ordes, instrucións nin resolucións normativas de ESO, Bacharelato ou FP nas últimas 24 horas."
        )
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #2d3748; line-height: 1.6;">
            <div style="background: #f7fafc; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
                <h2 style="color: #2b6cb0; margin-top: 0;">✅ Comprobación Diaria de Legislación Educativa</h2>
                <p>Ola Óscar,</p>
                <p>Realizouse con éxito a comprobación programada o <strong>{today_str}</strong>.</p>
                <p style="margin-top: 0;"><strong>Modo activo:</strong> {mode_label}</p>
                <div style="background: #ffffff; padding: 15px; border-radius: 6px; border-left: 4px solid #48bb78; margin: 15px 0;">
                    <p style="margin: 0;"><strong>Resultado:</strong> {none_detail}</p>
                </div>
                <p><strong>Fontes oficiais auditadas hoxe:</strong></p>
                <ul>
                    {"".join(f"<li>{s}</li>" for s in sources_checked)}
                </ul>
                <p style="font-size: 13px; color: #718096; margin-top: 25px;">O teu sistema en Render segue 100% actualizado co corpus vixente.</p>
            </div>
        </body>
        </html>
        """
    elif not updated_docs and archived:
        msg["Subject"] = f"📁 [Lexislación Galicia] Actualización de histórico/vixencia ({len(archived)} norma/s)"
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #2d3748; line-height: 1.6;">
            <div style="background: #f7fafc; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
                <h2 style="color: #2b6cb0; margin-top: 0;">📁 Actualización de Vixencia (Sen documentos novos)</h2>
                <p>Ola Óscar,</p>
                <p>Realizouse a comprobación o <strong>{today_str}</strong> | <strong>Modo activo:</strong> {mode_label}.</p>
                {archived_html}
                <p>As normas anteriores consérvanse marcadas como históricas (non se borran) para manter sempre vixente a resposta do buscador.</p>
            </div>
        </body>
        </html>
        """
    else:
        msg["Subject"] = f"🚨 Nueva Normativa Educativa Galicia: {len(updated_docs)} archivo(s) nuevo(s)"
        doc_items_html = ""
        synced_drive_count = sum(1 for d in updated_docs if d.get("drive_url"))
        for d in updated_docs:
            drive_link_html = ""
            if d.get("drive_url"):
                drive_link_html = f' · <a href="{d["drive_url"]}" style="color: #2f855a; font-weight: 600;">Abrir en Google Drive</a>'
            doc_items_html += f"""
            <li style="margin-bottom: 12px;">
                <strong>{d['title']}</strong><br>
                <span>Páxinas: {d['page_count']} | Etapa(s): {", ".join(d.get("etapas") or [d.get("etapa", "Transversal")])}</span><br>
                <a href="{d['source_url']}" style="color: #3182ce;">Ver orixe oficial</a>{drive_link_html}
            </li>
            """

        if synced_drive_count == len(updated_docs):
            drive_status_msg = f"Sincronización con Google Drive: gardáronse os {synced_drive_count} PDFs na túa carpeta <code>corpus_pdf/{TARGET_SUBFOLDER}</code>."
        elif synced_drive_count > 0:
            drive_status_msg = f"Sincronización con Google Drive: gardáronse {synced_drive_count} de {len(updated_docs)} PDFs na túa carpeta."
        elif os.environ.get("DRIVE_WEBHOOK_URL", "").strip():
            drive_status_msg = "Sincronización con Google Drive: non se puideron subir os ficheiros nesta execución (revisar rexistro)."
        else:
            drive_status_msg = "Sincronización automática de subida a Google Drive aínda non configurada (DRIVE_WEBHOOK_URL pendente)."

        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #2d3748; line-height: 1.6;">
            <div style="background: #f7fafc; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
                <h2 style="color: #c53030; margin-top: 0;">🚨 Nova Publicación Normativa Detectada (ESO/Bach/FP)</h2>
                <p>Ola Óscar,</p>
                <p>O rastrexador automático detectou e indexou con éxito <strong>{len(updated_docs)} nova(s) disposición(óns)</strong> o {today_str} | <strong>Modo activo:</strong> {mode_label}.</p>
                <div style="background: #ffffff; padding: 15px; border-radius: 6px; border-left: 4px solid #e53e3e; margin: 15px 0;">
                    <ul style="padding-left: 20px; margin: 0;">
                        {doc_items_html}
                    </ul>
                </div>
                {archived_html}
                <p><strong>Actualización automática:</strong> A base de datos foi actualizada e despregada en Render. {drive_status_msg}</p>
                <p><em>Adxúntanse nesta mensaxe os ficheiros PDF descargados para o teu arquivo persoal.</em></p>
            </div>
        </body>
        </html>
        """

    msg.attach(MIMEText(body, "html", "utf-8"))

    total_attachment_bytes = 0
    for d in updated_docs:
        fpath = d.get("file_path")
        if fpath and Path(fpath).is_file():
            content = Path(fpath).read_bytes()
            if total_attachment_bytes + len(content) < 20 * 1024 * 1024:
                part = MIMEApplication(content, Name=Path(fpath).name)
                part["Content-Disposition"] = f'attachment; filename="{Path(fpath).name}"'
                msg.attach(part)
                total_attachment_bytes += len(content)

    if not smtp_pass:
        print("[Email] AVISO: No se configuró GMAIL_APP_PASSWORD en las variables de entorno.")
        print(f"[Email] Correo preparado para {RECIPIENT_EMAIL}:\n  Asunto: {msg['Subject']}")
        return

    try:
        print(f"[Email] Conectando con smtp.gmail.com para enviar a {RECIPIENT_EMAIL}...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print("[Email] Correo enviado satisfactoriamente!")
    except Exception as e:
        print(f"[Email] Error enviando correo: {e}")


def main():
    filter_mode = get_filter_mode()
    print(f"=== INICIO COMPROBACIÓN DIARIA LEGISLACIÓN ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) | MODO={filter_mode} ===")
    t0 = time.time()
    db_path = prepare_local_sqlite()
    conn = sqlite3.connect(str(db_path))

    existing_urls, existing_files, existing_hashes = get_existing_signatures(conn)
    print(f"[DB] Base cargada: {len(existing_files)} documentos indexados previamente.")

    session = requests.Session()
    session.headers.update(HEADERS)

    sources_checked = [
        "Portal Educativo da Xunta (/portal/normativa — ESO, Bacharelato, FP e Réxime xeral)",
        "Portal Educativo da Xunta (/portal/anuncios — ESO, Bacharelato, FP e Réxime xeral)",
        "Portal FP da Xunta (/fp/novas)",
        f"Diario Oficial de Galicia (DOG RSS con filtro {filter_mode})",
        "Rexistro de normas derrogadas en Lex.gal",
    ]

    print(f"[Crawler] Buscando novas publicacións en fontes oficiais (Modo activo: {filter_mode})...")
    raw_candidates: list[dict] = []
    raw_candidates.extend(check_portal_normativa(session, filter_mode=filter_mode))
    raw_candidates.extend(check_portal_anuncios(session, filter_mode=filter_mode))
    raw_candidates.extend(check_fp_portal(session, filter_mode=filter_mode))
    raw_candidates.extend(check_dog_rss(session, filter_mode=filter_mode))

    # Deduplicar por source_url mantendo o primeiro título descritivo
    dedup_candidates: list[dict] = []
    seen_cand_urls: set[str] = set()
    for cand in raw_candidates:
        u = cand["source_url"].strip().lower()
        if u and u not in seen_cand_urls:
            seen_cand_urls.add(u)
            dedup_candidates.append(cand)

    print(f"[Crawler] Evaluando {len(dedup_candidates)} publicacións recentes filtradas ({filter_mode})...")
    new_docs: list[dict] = []
    archived_docs: list[dict] = []

    for cand in dedup_candidates:
        if len(new_docs) >= MAX_NEW_PER_RUN:
            print(f"[Crawler] Alcanzado límite de {MAX_NEW_PER_RUN} documentos novos nesta execución.")
            break

        src_url = cand["source_url"].strip().lower()
        if src_url in existing_urls:
            continue

        print(f"[Crawler] Posible documento novo: {cand['title']} ({cand['source_url']})")
        pdf_data = download_and_extract_pdf(session, cand["source_url"], cand["title"])
        if not pdf_data:
            continue

        if pdf_data["file_name"].lower() in existing_files:
            continue

        if pdf_data["sha256"].lower() in existing_hashes:
            print(f"[Crawler] Documento ya existente por contenido (SHA256): {cand['title']}")
            continue

        doc_id = index_new_document(conn, cand, pdf_data)
        existing_urls.add(src_url)
        existing_files.add(pdf_data["file_name"].lower())
        existing_hashes.add(pdf_data["sha256"].lower())

        # Marcar versiones de cursos anteriores si corresponde a serie anual
        superseded_list = mark_superseded_annual_docs(conn, doc_id, cand["title"])
        for sup in superseded_list:
            archive_pdf_in_drive_webhook(sup, conn=conn)
            archived_docs.append(sup)

        # Subir nuevo PDF a Google Drive mediante Web App de Apps Script
        drive_res = upload_pdf_to_drive_webhook(pdf_data, TARGET_SUBFOLDER)
        drive_url = ""
        drive_fid = ""
        if drive_res and drive_res.get("file_id"):
            drive_fid = str(drive_res["file_id"])
            drive_url = str(drive_res.get("view_url") or f"https://drive.google.com/file/d/{drive_fid}/view")
            rel_path = f"{TARGET_SUBFOLDER}/{pdf_data['file_name']}"
            update_local_drive_map(rel_path, pdf_data["file_name"], drive_fid)

        new_docs.append({
            "id": doc_id,
            "title": cand["title"],
            "page_count": pdf_data["page_count"],
            "source_url": cand["source_url"],
            "ambito": cand.get("ambito", "Autonómico (Galicia)"),
            "etapa": cand.get("etapa", "Transversal"),
            "etapas": cand.get("etapas", ["Transversal"]),
            "file_path": pdf_data["file_path"],
            "drive_url": drive_url,
            "drive_file_id": drive_fid,
        })

    # Comprobar derogaciones en Lex.gal
    derogated_list = sync_lexgal_derogations(session, conn)
    for der in derogated_list:
        archive_pdf_in_drive_webhook(der, conn=conn)
        archived_docs.append(der)

    conn.close()

    if new_docs or archived_docs:
        print(f"[Update] Novos: {len(new_docs)} | Históricos/derrogados: {len(archived_docs)}. Recomprimiendo...")
        save_and_rechunk_database(db_path)
    else:
        print("[Update] Comprobación rematada: Ningunha normativa nova hoxe.")

    send_notification_email(new_docs, sources_checked, archived_docs=archived_docs, filter_mode=filter_mode)

    if db_path.exists():
        try:
            db_path.unlink()
        except Exception:
            pass

    print(f"=== PROCESO COMPLETADO EN {time.time() - t0:.2f}s ===")


if __name__ == "__main__":
    main()
