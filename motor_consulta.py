"""
Motor de consulta estricto compatible con la base compactada (external-content FTS5)
para despliegue gratuito en Hugging Face Spaces + Google Drive.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent


def resolve_db_path() -> Path:
    env_p = os.environ.get("LEGISLACION_DB_PATH", "").strip()
    if env_p and Path(env_p).exists():
        return Path(env_p)
    for cand in [
        BASE_DIR / "base_datos" / "legislacion_galicia_web.db",
        Path("/tmp/legislacion_galicia.db"),
        BASE_DIR / "base_datos" / "legislacion_galicia.db",
    ]:
        if cand.exists():
            return cand
    return BASE_DIR / "base_datos" / "legislacion_galicia_web.db"


STOPWORDS = {
    "a", "al", "algo", "algun", "alguna", "algunos", "algunha", "algunhas", "ante",
    "asi", "como", "con", "contra", "cual", "cuales", "cuando", "cuanto", "cuantos",
    "cando", "canto", "cantos", "cales", "cal", "da", "das", "de", "del", "desde",
    "do", "dos", "e", "el", "en", "entre", "era", "es", "esa", "ese", "eso_stop",
    "esta", "estamos", "estan", "este", "esto", "fue", "ha", "hai", "hay", "la",
    "las", "le", "les", "lo", "los", "mas", "mais", "me", "mi", "mis", "na", "nas",
    "no", "nos", "o", "os", "ou", "para", "pero", "pola", "polas", "polo", "polos",
    "por", "porque", "que", "quien", "quen", "se", "segun", "segundo", "sen", "ser",
    "si", "sin", "sobre", "son", "su", "sus", "sua", "suas", "te", "ten", "tiene",
    "tienen", "todo", "todos", "un", "una", "unas", "uno", "unos", "unha", "unhas",
    "y", "ya", "xa", "galicia", "galego", "galega", "norma", "normativa",
    "legislacion", "lexislacion", "vigente", "vixente", "actual", "oficial",
    "establece", "regula", "dice", "di", "indica", "explica", "quiero", "saber",
    "puedo", "puede", "pueden", "pode", "poden", "hace", "fai", "donde", "onde",
    "cuanta", "cuantas", "canta", "cantas", "permite", "permiten", "permitira",
    "debe", "deben", "necesita", "necesitan", "exige", "exigen", "tener", "ter",
    "obtener", "obter", "conseguir", "estar",
}

EDU_ANCHOR_TOKENS = {
    "eso", "bacharelato", "bachillerato", "fp", "educacion", "educativo", "educativa",
    "ensino", "ensinanza", "ensenanza", "alumnado", "alumnos", "alumna", "profesorado",
    "docente", "docentes", "escolar", "escolares", "centro", "centros", "avaliacion",
    "evaluacion", "cualificacion", "cualificacions", "calificacion", "calificaciones",
    "promocion", "titulacion", "curriculo", "diversidade", "diversidad", "neae",
    "convivencia", "admision", "matricula", "titoria", "tutoria", "obxectividade",
    "objetividad", "reclamacion", "instrucions", "instrucciones", "galicia", "xunta",
    "lomloe", "loe", "abau", "pau", "fct", "cifp", "xade", "proens", "materias",
    "asignaturas", "optativas", "acreditacion", "dual", "grao", "grado",
}

BILINGUAL_GROUPS: list[set[str]] = [
    {"evaluacion", "avaliacion", "evaluar", "avaliar"},
    {"calificacion", "calificaciones", "cualificacion", "cualificacions", "nota", "notas"},
    {"reclamacion", "reclamacions", "reclamaciones", "revision"},
    {"objetividad", "obxectividade"},
    {"bachillerato", "bacharelato"},
    {"secundaria", "obrigatoria", "obligatoria"},
    {"asignatura", "asignaturas", "materia", "materias"},
    {"optativa", "optativas"},
    {"promocion", "promocionar", "pasar"},
    {"repeticion", "repetir", "permanencia", "repetira"},
    {"titulacion", "titular", "titulo", "titulos", "graduado", "obtencion"},
    {"suspensa", "suspensas", "suspenso", "suspensos", "pendiente", "pendientes", "pendente", "pendentes", "superada", "superadas", "negativa", "negativas"},
    {"recuperacion", "recuperar", "extraordinaria", "ordinaria"},
    {"plazo", "plazos", "prazo", "prazos", "fecha", "fechas", "data", "datas"},
    {"formacion", "profesional"},
    {"grado", "grao"},
    {"basico", "basica"},
    {"medio", "media"},
    {"superior", "superiores"},
    {"practicas", "fct", "fcts", "empresa", "empresas", "dual", "intensiva", "intensivo", "xeral"},
    {"convalidacion", "convalidacions", "convalidaciones", "validacion", "validacions", "exencion", "exencions"},
    {"diversidad", "diversidade"},
    {"necesidades", "necesidade", "neae", "nee", "apoyo", "apoio"},
    {"adaptacion", "adaptacions", "adaptaciones", "curricular", "curriculares"},
    {"convivencia", "acoso", "protocolo", "disciplina", "correccion"},
    {"instrucciones", "instrucions", "instrucion", "instruccion"},
    {"orden", "orde"},
    {"resolucion", "resolucions"},
    {"decreto", "decretos"},
    {"ley", "lei", "organica"},
    {"articulo", "artigo"},
    {"disposicion", "adicional", "transitoria", "derradeira", "final"},
    {"horario", "horarios", "horas", "lectivas", "lectivo"},
    {"calendario", "escolar", "lectivo", "inicio"},
    {"admision", "matricula", "prazas", "plazas", "solicitude", "solicitudes"},
    {"prueba", "pruebas", "proba", "probas", "libre", "libres", "acceso"},
    {"profesorado", "docente", "docentes", "claustro", "departamento"},
    {"tutoria", "titoria", "tutor", "titor", "orientacion"},
    {"consejo", "consello", "direccion", "equipo", "directivo"},
    {"modalidad", "modalidades", "ciencias", "tecnoloxia", "tecnologia", "humanidades", "artes"},
    {"proyecto", "proxecto", "educativo", "programacion", "anual", "pga", "pec", "proens"},
    {"centros", "centro", "publicos", "privados", "concertados"},
    {"lengua", "lingua", "linguas", "lenguas", "plurilingue", "idiomas"},
    {"acreditacion", "competencias", "cualificacions"},
    {"absentismo", "asistencia", "faltas"},
    {"becas", "bolsas", "ayudas", "axudas", "transporte", "comedor"},
    {"curriculo", "curriculos", "curriculum", "ensenanzas", "ensinanzas", "minimas"},
    {"excelencia", "mencion", "premio", "premios"},
    {"abau", "pau", "ebau", "universidade", "universidad"},
    {"xade", "aplicacion", "dixital", "digital"},
    {"adultos", "adultas", "distancia"},
    {"especializacion", "curso", "cursos", "modulos", "modulo"},
]

TERM_TO_SYNONYMS: dict[str, set[str]] = {}
for _grp in BILINGUAL_GROUPS:
    for _tok in _grp:
        TERM_TO_SYNONYMS.setdefault(_tok, set()).update(_grp)


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in nfkd if unicodedata.category(ch) != "Mn")


def normalize_for_match(text: str) -> str:
    t = strip_accents(text).lower()
    return re.sub(r"\s+", " ", t).strip()


def detect_stage_hints(query_norm: str) -> set[str]:
    stages: set[str] = set()
    if re.search(r"\b(eso|secundaria obrigatoria|secundaria obligatoria)\b", query_norm):
        stages.add("ESO")
    if re.search(r"\b(bacharelato|bachillerato|abau|pau)\b", query_norm):
        stages.add("Bacharelato")
    if re.search(r"\b(fp|formacion profesional|grao medio|grado medio|grao superior|grado superior|grao basico|grado basico|fct|cifp|fp dual)\b", query_norm):
        stages.add("FP")
    return stages


def extract_norm_refs(query_norm: str) -> list[str]:
    refs: list[str] = []
    for m in re.finditer(r"\b\d{1,4}/\d{4}\b", query_norm):
        refs.append(m.group(0))
    for m in re.finditer(r"\b(?:artigo|articulo)\s+(\d+[a-z]?)\b", query_norm):
        refs.append(m.group(1))
    return refs


def build_concept_groups(query: str) -> tuple[list[list[str]], list[str], set[str]]:
    q_norm = normalize_for_match(query)
    stage_hints = detect_stage_hints(q_norm)
    norm_refs = extract_norm_refs(q_norm)
    raw_tokens = re.findall(r"[a-z0-9]+", q_norm)

    seen_groups: list[list[str]] = []
    seen_keys: set[str] = set()

    for ref in norm_refs:
        if "/" in ref:
            parts = ref.split("/")
            key = f"ref_{ref}"
            if key not in seen_keys:
                seen_keys.add(key)
                seen_groups.append(parts)

    stage_tokens_to_soften = {"eso", "fp", "bacharelato", "bachillerato"}
    for tok in raw_tokens:
        if len(tok) < 2 or tok in STOPWORDS:
            continue
        if tok in stage_tokens_to_soften and stage_hints and len(raw_tokens) >= 4:
            continue
        syns = set(TERM_TO_SYNONYMS.get(tok, {tok}))
        for base in list(syns):
            if base.endswith("s") and len(base) > 4:
                syns.add(base[:-1])
            elif not base.endswith("s") and len(base) > 3 and not base.isdigit():
                syns.add(base + "s")
        key = "|".join(sorted(syns))
        if key not in seen_keys:
            seen_keys.add(key)
            seen_groups.append(sorted(syns))

    if not seen_groups:
        for tok in raw_tokens:
            if tok not in STOPWORDS and len(tok) >= 2:
                seen_groups.append(sorted(TERM_TO_SYNONYMS.get(tok, {tok})))

    return seen_groups, norm_refs, stage_hints


def _fts_group_expr(terms: list[str]) -> str:
    clean = [re.sub(r"[^a-z0-9]", "", t) for t in terms]
    clean = [t for t in clean if len(t) >= 2]
    if not clean:
        return ""
    if len(clean) == 1:
        return f'"{clean[0]}"'
    return "(" + " OR ".join(f'"{t}"' for t in clean[:12]) + ")"


@dataclass
class VerifiedClaim:
    afirmacion: str
    documento: str
    titulo: str
    ruta_relativa: str
    category_folder: str
    ambito: str
    doc_type: str
    etapas: list[str]
    vigencia_status: str
    pub_date: str
    pagina_pdf: int
    page_count: int
    pagina_boletin: str
    referencia_interna: str
    extracto_literal: str
    cita_formateada: str
    score: float
    verificada: bool


class MotorConsultaEstricto:
    """Motor extractivo estricto sobre SQLite FTS5 external-content."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else resolve_db_path()
        if not self.db_path.exists():
            raise FileNotFoundError(f"Base de datos no encontrada: {self.db_path}")
        self._has_id_join = self._detect_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _detect_schema(self) -> bool:
        conn = self._connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(fragmentos)").fetchall()}
            return "id" in cols
        finally:
            conn.close()

    def get_document_info(self, relative_path: str) -> dict[str, Any] | None:
        rel = (relative_path or "").replace("\\", "/").lstrip("/")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT doc_id, title, file_name, relative_path, pdf_url, page_count "
                "FROM documentos WHERE relative_path = ?",
                (rel,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_stats(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS docs, COALESCE(SUM(page_count),0) AS pages, "
                "COALESCE(SUM(total_chars),0) AS chars, COALESCE(SUM(file_size_bytes),0) AS bytes "
                "FROM documentos"
            ).fetchone()
            chunks = conn.execute("SELECT COUNT(*) AS c FROM fragmentos").fetchone()["c"]
            by_folder = [
                dict(r)
                for r in conn.execute(
                    "SELECT category_folder, COUNT(*) AS count, SUM(page_count) AS pages "
                    "FROM documentos GROUP BY category_folder ORDER BY category_folder"
                ).fetchall()
            ]
            by_ambito = [
                dict(r)
                for r in conn.execute(
                    "SELECT ambito, COUNT(*) AS count FROM documentos GROUP BY ambito ORDER BY count DESC"
                ).fetchall()
            ]
            return {
                "documents": row["docs"],
                "pages": row["pages"],
                "chunks": chunks,
                "total_chars": row["chars"],
                "total_size_mb": round(row["bytes"] / (1024 * 1024), 2),
                "by_folder": by_folder,
                "by_ambito": by_ambito,
            }
        finally:
            conn.close()

    def list_documents(
        self,
        search: str = "",
        etapa: str = "",
        ambito: str = "",
        doc_type: str = "",
        folder: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            where = ["1=1"]
            params: list[Any] = []
            if search:
                where.append("(lower(title) LIKE ? OR lower(file_name) LIKE ? OR lower(boletin) LIKE ?)")
                like = f"%{search.lower()}%"
                params.extend([like, like, like])
            if etapa:
                where.append("etapas_json LIKE ?")
                params.append(f'%"{etapa}"%')
            if ambito:
                where.append("ambito LIKE ?")
                params.append(f"%{ambito}%")
            if doc_type:
                where.append("doc_type LIKE ?")
                params.append(f"%{doc_type}%")
            if folder:
                where.append("category_folder = ?")
                params.append(folder)

            sql_where = " AND ".join(where)
            total = conn.execute(f"SELECT COUNT(*) AS c FROM documentos WHERE {sql_where}", params).fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT doc_id, title, file_name, original_filename, relative_path,
                       category_folder, source_portal, etapas_json, doc_type, ambito,
                       boletin, pub_date, vigencia_status, page_count, selectable_pages,
                       file_size_bytes, pdf_url, detail_url
                FROM documentos
                WHERE {sql_where}
                ORDER BY pub_date DESC, category_folder ASC, title ASC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                d["etapas"] = json.loads(d.pop("etapas_json"))
                items.append(d)
            return {"total": total, "items": items}
        finally:
            conn.close()

    @staticmethod
    def _query_mentions_historical(query: str) -> bool:
        qn = normalize_for_match(query)
        if re.search(r"\b20(?:1\d|2[0-4])\b", qn) or re.search(r"\bcurso\s+202[0-4]\b", qn):
            return True
        return any(
            k in qn
            for k in (
                "curso anterior",
                "cursos anteriores",
                "historic",
                "derrogad",
                "derogad",
                "antigu",
                "antiga",
            )
        )

    def _run_fts_search(
        self,
        conn: sqlite3.Connection,
        fts_query: str,
        etapa: str = "",
        ambito: str = "",
        doc_type: str = "",
        limit: int = 60,
        include_historical: bool = False,
    ) -> list[sqlite3.Row]:
        if not fts_query.strip():
            return []
        where = ["fragmentos_fts MATCH ?"]
        params: list[Any] = [fts_query]
        if not include_historical:
            where.append(
                "(d.vigencia_status IS NULL OR ("
                "LOWER(d.vigencia_status) NOT LIKE 'derrogad%' "
                "AND LOWER(d.vigencia_status) NOT LIKE 'derogad%' "
                "AND LOWER(d.vigencia_status) NOT LIKE 'superad%'))"
            )
        if etapa:
            where.append("(d.etapas_json LIKE ? OR d.etapas_json LIKE '%\"Transversal\"%')")
            params.append(f'%"{etapa}"%')
        if ambito:
            where.append("d.ambito LIKE ?")
            params.append(f"%{ambito}%")
        if doc_type:
            where.append("d.doc_type LIKE ?")
            params.append(f"%{doc_type}%")

        join_cond = "f.id = fragmentos_fts.rowid" if self._has_id_join else "f.chunk_id = fragmentos_fts.chunk_id"
        sql = f"""
            SELECT
                f.chunk_id, f.doc_id, f.pdf_page, f.boletin_page, f.article_ref, f.chunk_text,
                d.title, d.file_name, d.relative_path, d.category_folder, d.ambito,
                d.doc_type, d.etapas_json, d.vigencia_status, d.pub_date, d.page_count,
                bm25(fragmentos_fts, 1.5, 1.2, 4.0) AS bm25_score
            FROM fragmentos_fts
            JOIN fragmentos f ON {join_cond}
            JOIN documentos d ON d.doc_id = f.doc_id
            WHERE {' AND '.join(where)}
            ORDER BY bm25_score ASC
            LIMIT ?
        """
        try:
            return conn.execute(sql, params + [limit]).fetchall()
        except sqlite3.OperationalError:
            return []

    def _collect_fts_candidates(
        self,
        conn: sqlite3.Connection,
        group_exprs: list[str],
        effective_etapa: str,
        etapa: str,
        ambito: str,
        doc_type: str,
        include_historical: bool,
    ) -> dict[str, sqlite3.Row]:
        candidates_map: dict[str, sqlite3.Row] = {}
        and_expr = " AND ".join(group_exprs[:4])
        for row in self._run_fts_search(
            conn, and_expr, effective_etapa, ambito, doc_type, limit=60, include_historical=include_historical
        ):
            candidates_map[row["chunk_id"]] = row

        if len(candidates_map) < 5 and effective_etapa and not etapa:
            for row in self._run_fts_search(
                conn, and_expr, "", ambito, doc_type, limit=50, include_historical=include_historical
            ):
                candidates_map.setdefault(row["chunk_id"], row)

        if len(candidates_map) < 6 and len(group_exprs) >= 2:
            or_expr = " OR ".join(group_exprs[:7])
            for row in self._run_fts_search(
                conn, or_expr, effective_etapa, ambito, doc_type, limit=80, include_historical=include_historical
            ):
                candidates_map.setdefault(row["chunk_id"], row)
            if len(candidates_map) < 6 and effective_etapa and not etapa:
                for row in self._run_fts_search(
                    conn, or_expr, "", ambito, doc_type, limit=60, include_historical=include_historical
                ):
                    candidates_map.setdefault(row["chunk_id"], row)
        return candidates_map

    def _extract_best_sentences(
        self, chunk_text: str, concept_groups: list[list[str]], norm_refs: list[str]
    ) -> str:
        clean_chunk = re.sub(r"\s+", " ", chunk_text).strip()
        if len(clean_chunk) <= 460:
            return clean_chunk

        sentences = re.split(r"(?<=[\.\;\:])\s+(?=[A-ZÁÉÍÓÚÑ0-9\(])", chunk_text.strip())
        if len(sentences) <= 2:
            return clean_chunk[:620].rsplit(" ", 1)[0] + "..."

        scored: list[tuple[int, float, str]] = []
        for idx, sent in enumerate(sentences):
            snorm = normalize_for_match(sent)
            hits = 0.0
            for grp in concept_groups:
                if any(re.search(rf"\b{re.escape(t)}\b", snorm) for t in grp):
                    hits += 1
            for ref in norm_refs:
                if ref in snorm:
                    hits += 1.5
            scored.append((idx, hits, sent.strip()))

        best_idx = max(scored, key=lambda x: (x[1], -x[0]))[0]
        start = max(0, best_idx - 1 if len(sentences[best_idx]) < 150 else best_idx)
        end = min(len(sentences), best_idx + 2)
        snippet = " ".join(s.strip() for s in sentences[start:end] if s.strip())
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if len(snippet) > 680:
            snippet = snippet[:680].rsplit(" ", 1)[0] + "..."
        return snippet

    def reconstruct_page_text(self, conn: sqlite3.Connection, doc_id: str, pdf_page: int) -> str:
        rows = conn.execute(
            "SELECT chunk_text FROM fragmentos WHERE doc_id = ? AND pdf_page = ? ORDER BY chunk_index_in_page",
            (doc_id, pdf_page),
        ).fetchall()
        if rows:
            return "\n".join(str(r["chunk_text"]) for r in rows)
        try:
            prow = conn.execute(
                "SELECT page_text FROM paginas WHERE doc_id = ? AND pdf_page = ?",
                (doc_id, pdf_page),
            ).fetchone()
            return str(prow["page_text"]) if prow else ""
        except sqlite3.OperationalError:
            return ""

    def _verify_claim_against_page(
        self,
        conn: sqlite3.Connection,
        doc_id: str,
        pdf_page: int,
        page_count: int,
        extracto_literal: str,
    ) -> bool:
        if pdf_page < 1 or pdf_page > page_count:
            return False
        page_text = self.reconstruct_page_text(conn, doc_id, pdf_page)
        if not page_text:
            return False
        page_norm = normalize_for_match(page_text)
        ext_norm = normalize_for_match(extracto_literal.rstrip("."))
        probe = ext_norm[:120].strip()
        if len(probe) >= 20 and probe in page_norm:
            return True
        words = [w for w in probe.split() if len(w) >= 4][:6]
        return bool(words) and all(w in page_norm for w in words)

    def consultar(
        self,
        query: str,
        etapa: str = "",
        ambito: str = "",
        doc_type: str = "",
        top_k: int = 6,
        include_historical: bool = False,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return self._abstain_response(query, "La consulta está vacía.")

        concept_groups, norm_refs, stage_hints = build_concept_groups(query)
        if not concept_groups:
            return self._abstain_response(
                query,
                "La consulta no contiene términos normativos o educativos identificables en el corpus.",
            )

        effective_etapa = etapa or (next(iter(stage_hints)) if len(stage_hints) == 1 else "")
        group_exprs = [g for g in (_fts_group_expr(grp) for grp in concept_groups) if g]
        if not group_exprs:
            return self._abstain_response(query, "No se pudieron extraer términos de búsqueda válidos.")

        eff_hist = include_historical or self._query_mentions_historical(query)
        conn = self._connect()
        try:
            candidates_map = self._collect_fts_candidates(
                conn, group_exprs, effective_etapa, etapa, ambito, doc_type, include_historical=eff_hist
            )
            if not candidates_map and not eff_hist:
                candidates_map = self._collect_fts_candidates(
                    conn, group_exprs, effective_etapa, etapa, ambito, doc_type, include_historical=True
                )

            if not candidates_map:
                return self._abstain_response(
                    query,
                    "No se encontró ningún fragmento literal en el corpus legislativo de Galicia que respalde esta consulta.",
                )

            q_tokens_set = set(re.findall(r"[a-z0-9]+", normalize_for_match(query)))
            has_edu_anchor = bool(stage_hints or (q_tokens_set & EDU_ANCHOR_TOKENS))
            total_concepts = len(concept_groups)
            if not has_edu_anchor and total_concepts >= 3:
                min_combined_matches = total_concepts
                min_chunk_matches = total_concepts - 1
            elif total_concepts >= 3:
                min_combined_matches = math.ceil(0.65 * total_concepts) if not norm_refs else max(2, math.ceil(0.5 * total_concepts))
                min_chunk_matches = max(2, math.ceil(0.55 * total_concepts)) if not norm_refs else 1
            elif total_concepts == 2:
                min_combined_matches = 2 if not norm_refs else 1
                min_chunk_matches = 2 if not norm_refs else 1
            else:
                min_combined_matches = 1
                min_chunk_matches = 1

            ranked: list[tuple[float, int, sqlite3.Row]] = []
            for row in candidates_map.values():
                chunk_norm = normalize_for_match(row["chunk_text"])
                title_norm = normalize_for_match(row["title"])
                ref_norm = normalize_for_match(row["article_ref"] or "")
                combined_norm = f"{title_norm} {ref_norm} {chunk_norm}"

                chunk_matches = 0
                combined_matches = 0
                for grp in concept_groups:
                    in_chunk = any(re.search(rf"\b{re.escape(t)}\b", chunk_norm) for t in grp)
                    in_comb = in_chunk or any(re.search(rf"\b{re.escape(t)}\b", combined_norm) for t in grp)
                    if in_chunk:
                        chunk_matches += 1
                    if in_comb:
                        combined_matches += 1

                if combined_matches < min_combined_matches or chunk_matches < min_chunk_matches:
                    continue

                if total_concepts == 1 and chunk_matches == 1:
                    grp0 = concept_groups[0]
                    occ = sum(len(re.findall(rf"\b{re.escape(t)}\b", chunk_norm)) for t in grp0)
                    in_title = any(re.search(rf"\b{re.escape(t)}\b", title_norm) for t in grp0)
                    if occ < 2 and not in_title:
                        continue

                coverage = combined_matches / total_concepts
                chunk_cov = chunk_matches / total_concepts
                bm25_pos = max(0.0, -float(row["bm25_score"]))
                score = (coverage * 4.5) + (chunk_cov * 4.0) + min(bm25_pos, 12.0) * 0.45

                for nref in norm_refs:
                    if nref in title_norm:
                        score += 3.5
                    elif nref in chunk_norm or nref in ref_norm:
                        score += 2.0

                doc_etapas = set(json.loads(row["etapas_json"]))
                if stage_hints:
                    if doc_etapas & stage_hints:
                        score += 2.2
                    elif "Transversal" in doc_etapas:
                        score += 0.6
                    else:
                        score -= 1.2

                folder = row["category_folder"]
                if folder == "03_Autonomica_Consolidada_LexGal":
                    score += 1.6
                elif folder in ("04_ESO", "05_Bacharelato", "06_Formacion_Profesional", "07_Transversal_Centros_Diversidade"):
                    score += 1.3
                elif folder == "08_Resolucions_Instrucions_Anuncios":
                    score += 1.1
                elif folder == "02_Estatal_BOE_Normas":
                    score += 0.8
                elif folder == "01_Estatal_BOE_Codigos":
                    score -= 1.2

                pub = row["pub_date"] or ""
                if pub >= "2026":
                    score += 1.4
                elif pub >= "2025":
                    score += 1.1
                elif pub >= "2024":
                    score += 0.8
                elif pub >= "2022":
                    score += 0.5

                ranked.append((score, combined_matches, row))

            if not ranked:
                return self._abstain_response(
                    query,
                    "Los resultados recuperados no alcanzan el umbral estricto de coincidencia conceptual en el corpus oficial.",
                )

            ranked.sort(key=lambda x: x[0], reverse=True)

            claims: list[VerifiedClaim] = []
            per_doc_count: dict[str, int] = {}
            seen_snippets: set[str] = set()

            for score, _, row in ranked:
                doc_id = row["doc_id"]
                if per_doc_count.get(doc_id, 0) >= 2:
                    continue

                snippet = self._extract_best_sentences(row["chunk_text"], concept_groups, norm_refs)
                snip_sig = normalize_for_match(snippet)[:130]
                if snip_sig in seen_snippets:
                    continue

                if not self._verify_claim_against_page(
                    conn, doc_id, int(row["pdf_page"]), int(row["page_count"]), snippet
                ):
                    continue

                seen_snippets.add(snip_sig)
                per_doc_count[doc_id] = per_doc_count.get(doc_id, 0) + 1

                etapas_list = json.loads(row["etapas_json"])
                bol_page = row["boletin_page"] or ""
                art_ref = row["article_ref"] or ""
                cita_parts = [
                    f"Documento: {row['file_name']}",
                    f"Pág. PDF: {row['pdf_page']}/{row['page_count']}",
                ]
                if bol_page:
                    cita_parts.append(bol_page)
                if art_ref:
                    cita_parts.append(art_ref)
                cita_str = "[" + " | ".join(cita_parts) + "]"

                claims.append(
                    VerifiedClaim(
                        afirmacion=snippet,
                        documento=row["file_name"],
                        titulo=row["title"],
                        ruta_relativa=row["relative_path"],
                        category_folder=row["category_folder"],
                        ambito=row["ambito"],
                        doc_type=row["doc_type"],
                        etapas=etapas_list,
                        vigencia_status=row["vigencia_status"],
                        pub_date=row["pub_date"] or "",
                        pagina_pdf=int(row["pdf_page"]),
                        page_count=int(row["page_count"]),
                        pagina_boletin=bol_page,
                        referencia_interna=art_ref,
                        extracto_literal=snippet,
                        cita_formateada=cita_str,
                        score=round(score, 3),
                        verificada=True,
                    )
                )
                if len(claims) >= top_k:
                    break

            if not claims:
                return self._abstain_response(
                    query,
                    "Ningún candidato superó la verificación estricta de cita en página real.",
                )

            lines = [
                f"Respuesta estricta basada exclusivamente en {len(claims)} evidencias verificadas del corpus legislativo oficial:"
            ]
            for idx, cl in enumerate(claims, start=1):
                lines.append(
                    f"{idx}. {cl.afirmacion}\n   Fuente verificada: {cl.titulo} {cl.cita_formateada}"
                )

            return {
                "encontrado": True,
                "abstencion": False,
                "consulta": query,
                "etapa_detectada": sorted(stage_hints),
                "total_afirmaciones": len(claims),
                "afirmaciones": [asdict(c) for c in claims],
                "respuesta_texto": "\n\n".join(lines),
            }
        finally:
            conn.close()

    @staticmethod
    def _abstain_response(query: str, motivo: str) -> dict[str, Any]:
        msg = (
            "ABSTENCIÓN ESTRICTA: No existe información verificable suficiente en el corpus "
            f"legislativo educativo de Galicia para responder a esta consulta. Motivo: {motivo}"
        )
        return {
            "encontrado": False,
            "abstencion": True,
            "consulta": query,
            "etapa_detectada": [],
            "total_afirmaciones": 0,
            "afirmaciones": [],
            "motivo_abstencion": motivo,
            "respuesta_texto": msg,
        }
