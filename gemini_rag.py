"""
Motor RAG Estricto con Gemini API (REST v1beta) para despliegue en Hugging Face Spaces
conectado a Google Drive (drive_connector) y a la base compactada SQLite FTS5.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from drive_connector import get_drive_view_url
from motor_consulta import MotorConsultaEstricto, normalize_for_match

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "gemini_config.json"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"

EXCLUDED_MODEL_KEYWORDS = (
    "embedding",
    "imagen",
    "-image",
    "veo",
    "aqa",
    "tts",
    "audio",
    "native-audio",
    "transcribe",
    "nano-banana",
    "omni",
    "customtools",
    "robotics",
    "computer-use",
    "bisoci",
    "gemma",
)

PREFERRED_MODEL_ORDER = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-flash-lite-latest",
    "gemini-3-flash-preview",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.1-pro-preview",
    "gemini-pro-latest",
)

_RUNTIME_SELECTED_MODEL: str = ""


class GeminiHTTPError(RuntimeError):
    """Error HTTP de Gemini API con código explícito para failover automático."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_public_mode() -> bool:
    return os.environ.get("PUBLIC_MODE", "0").strip() in ("1", "true", "True", "yes")


def mask_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def _read_config_file() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_gemini_config(
    api_key_override: str = "",
    model_override: str = "",
) -> dict[str, str]:
    file_cfg = _read_config_file()
    env_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    file_key = str(file_cfg.get("api_key") or "").strip()

    if api_key_override.strip():
        eff_key = api_key_override.strip()
        source = "parametro"
    elif env_key:
        eff_key = env_key
        source = "entorno"
    elif file_key:
        eff_key = file_key
        source = "archivo_local"
    else:
        eff_key = ""
        source = "no_configurada"

    eff_model = (
        model_override.strip()
        or _RUNTIME_SELECTED_MODEL
        or str(file_cfg.get("model") or "").strip()
        or os.environ.get("GEMINI_MODEL", "").strip()
        or DEFAULT_GEMINI_MODEL
    )
    eff_model = eff_model.removeprefix("models/")

    return {
        "api_key": eff_key,
        "model": eff_model,
        "source": source,
    }


def save_gemini_config(api_key: str | None = None, model: str | None = None) -> dict[str, Any]:
    global _RUNTIME_SELECTED_MODEL
    if model is not None and model.strip():
        _RUNTIME_SELECTED_MODEL = model.strip().removeprefix("models/")
    if is_public_mode():
        return get_public_config()

    cfg = _read_config_file()
    if api_key is not None and api_key.strip():
        cfg["api_key"] = api_key.strip()
    if model is not None and model.strip():
        cfg["model"] = _RUNTIME_SELECTED_MODEL
    if not cfg.get("model"):
        cfg["model"] = DEFAULT_GEMINI_MODEL
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return get_public_config()


def get_public_config() -> dict[str, Any]:
    cfg = load_gemini_config()
    return {
        "configured": bool(cfg["api_key"]),
        "masked_key": mask_api_key(cfg["api_key"]),
        "model": cfg["model"],
        "source": cfg["source"],
        "public_mode": is_public_mode(),
    }


def _model_sort_key(model_id: str) -> tuple[int, str]:
    low = model_id.lower()
    for idx, pref in enumerate(PREFERRED_MODEL_ORDER):
        if low == pref:
            return (idx, low)
    if "flash-lite" in low:
        return (len(PREFERRED_MODEL_ORDER), low)
    if "flash" in low:
        return (len(PREFERRED_MODEL_ORDER) + 1, low)
    if "pro" in low:
        return (len(PREFERRED_MODEL_ORDER) + 2, low)
    return (len(PREFERRED_MODEL_ORDER) + 3, low)


def _format_gemini_http_error(resp: requests.Response) -> str:
    try:
        err_json = resp.json()
        msg = err_json.get("error", {}).get("message") or resp.text[:300]
    except Exception:
        msg = resp.text[:300]
    if resp.status_code in (400, 401, 403):
        return f"Error de autenticación o permisos con Gemini API (HTTP {resp.status_code}): {msg}"
    if resp.status_code == 404:
        return f"Modelo Gemini no disponible (HTTP 404): {msg}"
    if resp.status_code == 429:
        return f"Cuota de Gemini API alcanzada (HTTP 429): {msg}"
    return f"Error HTTP {resp.status_code} de Gemini API: {msg}"


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        fb = payload.get("promptFeedback") or {}
        reason = fb.get("blockReason") or "sin candidatos en la respuesta"
        raise RuntimeError(f"Gemini no devolvió respuesta ({reason}).")
    cand0 = candidates[0]
    parts = (cand0.get("content") or {}).get("parts") or []
    texts = [str(p.get("text") or "") for p in parts if p.get("text")]
    out = "\n".join(texts).strip()
    if not out:
        finish = cand0.get("finishReason") or "desconocido"
        raise RuntimeError(f"Gemini devolvió una respuesta vacía (finishReason={finish}).")
    return out


def list_gemini_models(api_key: str, timeout: int = 15) -> list[dict[str, str]]:
    key = (api_key or "").strip()
    if not key:
        raise ValueError("Debes indicar una API Key de Gemini.")
    url = f"{GEMINI_API_BASE}/models"
    resp = requests.get(url, headers={"x-goog-api-key": key}, timeout=timeout)
    if resp.status_code != 200:
        raise GeminiHTTPError(resp.status_code, _format_gemini_http_error(resp))

    payload = resp.json()
    raw_models = payload.get("models") or []
    filtered: list[dict[str, str]] = []
    for m in raw_models:
        name = str(m.get("name") or "")
        model_id = name.removeprefix("models/")
        low = model_id.lower()
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        if "gemini" not in low:
            continue
        if any(ex in low for ex in EXCLUDED_MODEL_KEYWORDS):
            continue
        filtered.append(
            {
                "id": model_id,
                "display_name": str(m.get("displayName") or model_id),
                "description": str(m.get("description") or "")[:160],
            }
        )

    filtered.sort(key=lambda item: _model_sort_key(item["id"]))
    return filtered


def _probe_single_model(api_key: str, model_id: str, timeout: int = 8) -> dict[str, Any]:
    t0 = time.perf_counter()
    url = f"{GEMINI_API_BASE}/models/{model_id}:generateContent"
    try:
        resp = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "Responde únicamente OK"}]}],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 256},
            },
            timeout=timeout,
        )
        ms = round((time.perf_counter() - t0) * 1000)
        if resp.status_code == 200:
            try:
                txt = _extract_gemini_text(resp.json())
                if txt:
                    return {"id": model_id, "status": 200, "ok": True, "latency_ms": ms, "detail": "Operativo"}
            except Exception as exc:
                return {"id": model_id, "status": 200, "ok": False, "latency_ms": ms, "detail": str(exc)[:100]}
        return {
            "id": model_id,
            "status": resp.status_code,
            "ok": False,
            "latency_ms": ms,
            "detail": _format_gemini_http_error(resp)[:140],
        }
    except Exception as exc:
        ms = round((time.perf_counter() - t0) * 1000)
        return {"id": model_id, "status": 0, "ok": False, "latency_ms": ms, "detail": str(exc)[:120]}


def auto_select_working_model(
    api_key: str = "",
    preferred_model: str = "",
    save: bool = True,
    exclude_models: set[str] | None = None,
) -> dict[str, Any]:
    global _RUNTIME_SELECTED_MODEL
    cfg = load_gemini_config(api_key_override=api_key, model_override=preferred_model)
    eff_key = cfg["api_key"]
    if not eff_key:
        raise ValueError("No hay ninguna API Key configurada.")

    models = list_gemini_models(eff_key)
    if not models:
        raise RuntimeError("La API Key es válida pero no devolvió modelos Gemini compatibles.")

    excluded = exclude_models or set()
    ordered_ids: list[str] = []
    pref = cfg["model"]
    all_ids = [m["id"] for m in models if m["id"] not in excluded]
    if pref and pref in all_ids:
        ordered_ids.append(pref)
    for mid in all_ids:
        if mid not in ordered_ids:
            ordered_ids.append(mid)

    probe_results: list[dict[str, Any]] = []
    winner: str | None = None
    best_slow: dict[str, Any] | None = None
    for mid in ordered_ids[:12]:
        res = _probe_single_model(eff_key, mid, timeout=7)
        probe_results.append(res)
        if res["ok"]:
            if res["latency_ms"] <= 3500:
                winner = mid
                break
            if best_slow is None or res["latency_ms"] < best_slow["latency_ms"]:
                best_slow = res
            if len(probe_results) >= 4:
                break

    if not winner and best_slow:
        winner = best_slow["id"]

    if not winner:
        summary = "; ".join(f"{r['id']}: HTTP {r['status']}" for r in probe_results[:6])
        raise RuntimeError(f"Ningún modelo Gemini respondió operativo en la prueba automática ({summary}).")

    _RUNTIME_SELECTED_MODEL = winner
    if save:
        save_gemini_config(model=winner)

    return {
        "ok": True,
        "selected_model": winner,
        "models": models,
        "probe_results": probe_results,
        "message": (
            f"Modelo operativo seleccionado automáticamente: {winner} "
            f"(probados {len(probe_results)} de {len(models)} modelos listados)."
        ),
    }


def test_gemini_connection(api_key: str = "", model: str = "") -> dict[str, Any]:
    return auto_select_working_model(api_key=api_key, preferred_model=model, save=False)


class GeminiRAGEngine:
    """Motor RAG Estricto sobre DB compactada + enlaces directos a Google Drive."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.motor = MotorConsultaEstricto(db_path=db_path)
        self.db_path = self.motor.db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _call_gemini_generate(
        self,
        system_prompt: str,
        user_prompt: str,
        api_key: str,
        model: str,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
        response_mime_type: str | None = None,
        timeout: int = 55,
    ) -> str:
        clean_model = model.removeprefix("models/")
        url = f"{GEMINI_API_BASE}/models/{clean_model}:generateContent"
        gen_cfg: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        }
        if response_mime_type:
            gen_cfg["responseMimeType"] = response_mime_type

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": gen_cfg,
        }
        if system_prompt:
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        resp = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise GeminiHTTPError(resp.status_code, _format_gemini_http_error(resp))
        return _extract_gemini_text(resp.json())

    def _plan_search_queries(
        self,
        query: str,
        history: list[dict[str, str]],
        api_key: str,
        model: str,
    ) -> dict[str, Any]:
        fallback = {"fuera_de_dominio": False, "etapa_sugerida": "", "consultas": [query]}
        if not api_key:
            return fallback

        hist_lines = []
        for msg in (history or [])[-4:]:
            role = "Usuario" if msg.get("role") == "user" else "Asistente"
            content = str(msg.get("content") or "").strip()
            if content:
                hist_lines.append(f"{role}: {content[:350]}")
        hist_block = "\n".join(hist_lines) if hist_lines else "(sen historial previo)"

        sys_prompt = (
            "Eres un planificador de búsqueda documental para un corpus oficial de legislación educativa "
            "vigente de Galicia (ESO, Bacharelato y FP) y normativa básica estatal española (BOE). "
            "Devuelve EXCLUSIVAMENTE un objeto JSON válido con:\n"
            '- "fuera_de_dominio": boolean (true SOLO si la pregunta no guarda relación con educación, centros docentes, profesorado, alumnado, Galicia/España o normativa).\n'
            '- "etapa_sugerida": "" | "ESO" | "Bacharelato" | "FP" | "Transversal".\n'
            '- "consultas": array de 2 a 4 cadenas cortas de búsqueda combinando términos jurídicos en gallego y castellano.'
        )
        usr_prompt = f"Historial reciente:\n{hist_block}\n\nPregunta actual: {query}"

        try:
            raw = self._call_gemini_generate(
                system_prompt=sys_prompt,
                user_prompt=usr_prompt,
                api_key=api_key,
                model=model,
                temperature=0.0,
                max_output_tokens=1024,
                response_mime_type="application/json",
                timeout=14,
            )
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return fallback
            queries = [str(q).strip() for q in (parsed.get("consultas") or []) if str(q).strip()]
            if query not in queries:
                queries.insert(0, query)
            etapa_sug = str(parsed.get("etapa_sugerida") or "").strip()
            if etapa_sug not in ("", "ESO", "Bacharelato", "FP", "Transversal"):
                etapa_sug = ""
            return {
                "fuera_de_dominio": bool(parsed.get("fuera_de_dominio", False)),
                "etapa_sugerida": etapa_sug,
                "consultas": queries[:4],
            }
        except Exception:
            return fallback

    def _load_full_page_text(self, conn: sqlite3.Connection, doc_id: str, pdf_page: int, extracto: str) -> str:
        full_text = self.motor.reconstruct_page_text(conn, doc_id, pdf_page).strip() or extracto
        if len(full_text) <= 3800:
            return full_text

        probe = normalize_for_match(extracto[:80])
        full_norm = normalize_for_match(full_text)
        pos = full_norm.find(probe[:40]) if probe else -1
        if pos == -1:
            return full_text[:3800] + "\n[...]"
        start = max(0, pos - 1200)
        end = min(len(full_text), start + 3800)
        prefix = "[...]\n" if start > 0 else ""
        suffix = "\n[...]" if end < len(full_text) else ""
        return prefix + full_text[start:end] + suffix

    def retrieve_evidence_pages(
        self,
        query: str,
        history: list[dict[str, str]] | None = None,
        etapa: str = "",
        ambito: str = "",
        doc_type: str = "",
        max_pages: int = 8,
        api_key: str = "",
        model: str = DEFAULT_GEMINI_MODEL,
    ) -> dict[str, Any]:
        direct_res = self.motor.consultar(
            query, etapa=etapa or "", ambito=ambito, doc_type=doc_type, top_k=10
        )

        needs_planner = bool(history) or len(direct_res.get("afirmaciones", [])) < 3
        if needs_planner:
            plan = self._plan_search_queries(query, history or [], api_key, model)
        else:
            det_etapas = direct_res.get("etapa_detectada") or []
            plan = {
                "fuera_de_dominio": False,
                "etapa_sugerida": det_etapas[0] if len(det_etapas) == 1 else "",
                "consultas": [query],
            }

        eff_etapa = etapa or plan.get("etapa_sugerida", "")
        search_queries = plan["consultas"]

        if plan.get("fuera_de_dominio") and not direct_res.get("encontrado"):
            return {
                "evidences": [],
                "search_queries": search_queries,
                "etapa_aplicada": eff_etapa,
                "fuera_de_dominio": True,
            }

        page_pool: dict[tuple[str, int], dict[str, Any]] = {}

        def _ingest_claims(res: dict[str, Any], weight: float) -> None:
            for cl in res.get("afirmaciones", []):
                key = (cl["documento"], int(cl["pagina_pdf"]))
                adj_score = float(cl["score"]) * weight
                existing = page_pool.get(key)
                if not existing or adj_score > existing["rank_score"]:
                    page_pool[key] = {**cl, "rank_score": adj_score}

        _ingest_claims(direct_res, weight=1.15)

        for sq in search_queries:
            if sq == query:
                continue
            r_sub = self.motor.consultar(sq, etapa=eff_etapa, ambito=ambito, doc_type=doc_type, top_k=6)
            _ingest_claims(r_sub, weight=1.0)
            if not r_sub.get("encontrado") and eff_etapa and not etapa:
                r_broad = self.motor.consultar(sq, etapa="", ambito=ambito, doc_type=doc_type, top_k=5)
                _ingest_claims(r_broad, weight=0.92)

        if not page_pool:
            return {
                "evidences": [],
                "search_queries": search_queries,
                "etapa_aplicada": eff_etapa,
                "fuera_de_dominio": False,
            }

        ranked_pages = sorted(page_pool.values(), key=lambda x: x["rank_score"], reverse=True)
        selected: list[dict[str, Any]] = []
        per_file: dict[str, int] = {}
        limit_pages = min(max(int(max_pages or 8), 4), 18)

        conn = self._connect()
        try:
            for item in ranked_pages:
                fn = item["documento"]
                if per_file.get(fn, 0) >= 3:
                    continue
                doc_row = conn.execute(
                    "SELECT doc_id FROM documentos WHERE file_name = ?",
                    (fn,),
                ).fetchone()
                if not doc_row:
                    continue
                doc_id = doc_row["doc_id"]
                page_text = self._load_full_page_text(
                    conn, doc_id, int(item["pagina_pdf"]), item["extracto_literal"]
                )
                per_file[fn] = per_file.get(fn, 0) + 1
                eid = f"E{len(selected) + 1}"
                encoded_rel = quote(item["ruta_relativa"], safe="/")
                selected.append(
                    {
                        "cite_id": eid,
                        "doc_id": doc_id,
                        "documento": fn,
                        "titulo": item["titulo"],
                        "ruta_relativa": item["ruta_relativa"],
                        "pdf_url_local": f"/pdf/{encoded_rel}#page={item['pagina_pdf']}",
                        "drive_url": get_drive_view_url(item["ruta_relativa"], fn),
                        "category_folder": item["category_folder"],
                        "ambito": item["ambito"],
                        "doc_type": item["doc_type"],
                        "etapas": item["etapas"],
                        "vigencia_status": item["vigencia_status"],
                        "pub_date": item["pub_date"],
                        "pagina_pdf": int(item["pagina_pdf"]),
                        "page_count": int(item["page_count"]),
                        "pagina_boletin": item["pagina_boletin"],
                        "referencia_interna": item["referencia_interna"],
                        "extracto_literal": item["extracto_literal"],
                        "texto_pagina": page_text,
                        "score": round(float(item["rank_score"]), 3),
                    }
                )
                if len(selected) >= limit_pages:
                    break
        finally:
            conn.close()

        return {
            "evidences": selected,
            "search_queries": search_queries,
            "etapa_aplicada": eff_etapa,
            "fuera_de_dominio": False,
        }

    def _verify_and_map_citations(
        self,
        raw_answer: str,
        evidences: list[dict[str, Any]],
    ) -> dict[str, Any]:
        answer = (raw_answer or "").strip()
        if answer.upper().startswith("NO_CONSTA_EN_CORPUS"):
            reason = answer.split(":", 1)[1].strip() if ":" in answer else (
                "Los fragmentos recuperados del corpus no contienen información suficiente para responder con rigor."
            )
            return {
                "encontrado": False,
                "abstencion": True,
                "respuesta_markdown": f"ABSTENCIÓN ESTRICTA: {reason}",
                "fuentes_citadas": [],
                "citas_verificadas": True,
                "advertencia_citas": "",
            }

        answer = re.sub(r"\[\[(E\d+)\]([^\[\]]+?)\]", r"[[\1]]\2", answer)
        answer = re.sub(r"(?<!\[)\[(E\d+)\](?!\])", r"[[\1]]", answer)
        answer = re.sub(
            r"\[\[(E\d+)(?:\s*,\s*(E\d+))+\]\]",
            lambda m: " ".join(f"[[{x}]]" for x in re.findall(r"E\d+", m.group(0))),
            answer,
        )
        ev_by_id = {e["cite_id"]: e for e in evidences}

        verified_ids: set[str] = set()
        conn = self._connect()
        try:
            for e in evidences:
                ptxt = self.motor.reconstruct_page_text(conn, e["doc_id"], e["pagina_pdf"])
                if ptxt and 1 <= e["pagina_pdf"] <= e["page_count"]:
                    verified_ids.add(e["cite_id"])
        finally:
            conn.close()

        cited_order: list[str] = []

        def _filter_marker(m: re.Match[str]) -> str:
            cid = f"E{m.group(1)}"
            if cid in ev_by_id and cid in verified_ids:
                if cid not in cited_order:
                    cited_order.append(cid)
                return f"[[{cid}]]"
            return ""

        clean_answer = re.sub(r"\[\[E(\d+)\]\]", _filter_marker, answer).strip()

        fuentes_citadas = []
        for cid in cited_order:
            ev = dict(ev_by_id[cid])
            ev.pop("texto_pagina", None)
            ev["verificada"] = True
            fuentes_citadas.append(ev)

        advertencia = ""
        if not fuentes_citadas:
            advertencia = (
                "El modelo no insertó marcadores [[E#]] explícitos en línea; se adjuntan debajo las "
                "páginas exactas verificadas del corpus sobre las que se generó la consulta."
            )

        return {
            "encontrado": True,
            "abstencion": False,
            "respuesta_markdown": clean_answer,
            "fuentes_citadas": fuentes_citadas,
            "citas_verificadas": bool(fuentes_citadas),
            "advertencia_citas": advertencia,
        }

    def preguntar(
        self,
        query: str,
        history: list[dict[str, str]] | None = None,
        etapa: str = "",
        ambito: str = "",
        doc_type: str = "",
        max_pages: int = 8,
        api_key: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {
                "encontrado": False,
                "abstencion": True,
                "consulta": query,
                "respuesta_markdown": "ABSTENCIÓN ESTRICTA: La consulta está vacía.",
                "fuentes_citadas": [],
                "evidencias_consultadas": [],
                "busquedas_ejecutadas": [],
                "modelo_usado": "",
                "citas_verificadas": False,
            }

        cfg = load_gemini_config(api_key_override=api_key, model_override=model)
        eff_key = cfg["api_key"]
        eff_model = cfg["model"]
        if not eff_key:
            raise PermissionError("Falta configurar la API Key de Gemini (GEMINI_API_KEY).")

        retrieval = self.retrieve_evidence_pages(
            query=query,
            history=history or [],
            etapa=etapa,
            ambito=ambito,
            doc_type=doc_type,
            max_pages=max_pages,
            api_key=eff_key,
            model=eff_model,
        )
        evidences = retrieval["evidences"]
        if not evidences:
            return {
                "encontrado": False,
                "abstencion": True,
                "consulta": query,
                "respuesta_markdown": (
                    "ABSTENCIÓN ESTRICTA: No se encontró información verificable en los 3.734 PDFs "
                    "del corpus legislativo educativo de Galicia para responder a esta pregunta."
                ),
                "fuentes_citadas": [],
                "evidencias_consultadas": [],
                "busquedas_ejecutadas": retrieval["search_queries"],
                "etapa_aplicada": retrieval["etapa_aplicada"],
                "modelo_usado": eff_model,
                "citas_verificadas": True,
            }

        evidence_blocks: list[str] = []
        for ev in evidences:
            bol_info = f" | {ev['pagina_boletin']}" if ev["pagina_boletin"] else ""
            ref_info = f" | Sección: {ev['referencia_interna']}" if ev["referencia_interna"] else ""
            evidence_blocks.append(
                f"[{ev['cite_id']}]\n"
                f"ARCHIVO: {ev['documento']}\n"
                f"TÍTULO: {ev['titulo']}\n"
                f"ÁMBITO: {ev['ambito']} | VIGENCIA: {ev['vigencia_status']} | DATA: {ev['pub_date'] or '-'}\n"
                f"PÁGINA_PDF: {ev['pagina_pdf']} de {ev['page_count']}{bol_info}{ref_info}\n"
                f"TEXTO_LITERAL_DE_LA_PÁGINA:\n{ev['texto_pagina']}"
            )

        hist_text = ""
        if history:
            turns = []
            for h in history[-4:]:
                role = "Usuario" if h.get("role") == "user" else "Asistente"
                txt = str(h.get("content") or "").strip()
                if txt:
                    turns.append(f"{role}: {txt[:500]}")
            if turns:
                hist_text = "CONTEXTO PREVIO DE LA CONVERSACIÓN:\n" + "\n".join(turns) + "\n\n"

        system_prompt = (
            "Eres un asistente jurídico-educativo ESTRICTO especializado en la legislación vigente de Galicia "
            "(ESO, Bachillerato y FP) y la normativa básica estatal española.\n"
            "REGLAS INQUEBRANTABLES:\n"
            "1. Responde ÚNICAMENTE con información literal o directamente deducible de los bloques [E1]..[En] proporcionados. "
            "PROHIBIDO usar memoria previa, conocimiento externo o suposiciones.\n"
            "2. En CADA afirmación, regla, plazo o requisito debes incluir su marcador exacto de cita [[E1]], [[E2]], etc., "
            "y mencionar explícitamente el nombre de la norma/archivo y el número de página PDF (ej.: 'según el Decreto 156/2022, pág. PDF 25 [[E1]]').\n"
            "3. Si existen normas autonómicas de Galicia y normas estatales, prioriza indicar claramente lo que establece Galicia y cita ambos documentos si se complementan.\n"
            "4. Si la normativa recuperada explica expresamente el criterio aplicable (por ejemplo, que no existe un número fijo de materias suspensas o remite a la decisión del equipo docente), RESPONDE explicando con exactitud lo que dice la norma con sus citas [[E#]] — NUNCA uses 'NO_CONSTA_EN_CORPUS:' en ese caso. Usa 'NO_CONSTA_EN_CORPUS: ' ÚNICAMENTE cuando los documentos no guarden relación o falte por completo la regulación preguntada.\n"
            "5. Responde en el mismo idioma que la pregunta del usuario (español o gallego), de forma clara, estructurada con viñetas o apartados, y sin inventar jamás un número de artículo ni página.\n"
            "6. Si coexisten varias versiones anuales de instrucciones o calendarios escolares, prioriza siempre la del curso más reciente salvo que el usuario pregunte por un curso pasado, e indica el curso correspondiente."
        )

        user_prompt = (
            f"{hist_text}"
            f"PREGUNTA DEL USUARIO:\n{query}\n\n"
            f"DOCUMENTOS Y PÁGINAS EXACTAS RECUPERADAS DEL CORPUS:\n\n"
            + "\n\n---\n\n".join(evidence_blocks)
        )

        try:
            raw_answer = self._call_gemini_generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                api_key=eff_key,
                model=eff_model,
                temperature=0.1,
                max_output_tokens=4096,
                timeout=55,
            )
        except GeminiHTTPError as exc:
            if exc.status_code in (404, 429, 503):
                auto = auto_select_working_model(
                    api_key=eff_key,
                    preferred_model="",
                    save=True,
                    exclude_models={eff_model},
                )
                eff_model = auto["selected_model"]
                raw_answer = self._call_gemini_generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    api_key=eff_key,
                    model=eff_model,
                    temperature=0.1,
                    max_output_tokens=4096,
                    timeout=55,
                )
            else:
                raise

        verified = self._verify_and_map_citations(raw_answer, evidences)
        evidences_public = []
        for ev in evidences:
            item = dict(ev)
            item.pop("texto_pagina", None)
            item["verificada"] = True
            evidences_public.append(item)

        return {
            "encontrado": verified["encontrado"],
            "abstencion": verified["abstencion"],
            "consulta": query,
            "respuesta_markdown": verified["respuesta_markdown"],
            "fuentes_citadas": verified["fuentes_citadas"],
            "evidencias_consultadas": evidences_public,
            "busquedas_ejecutadas": retrieval["search_queries"],
            "etapa_aplicada": retrieval["etapa_aplicada"],
            "modelo_usado": eff_model,
            "citas_verificadas": verified["citas_verificadas"],
            "advertencia_citas": verified["advertencia_citas"],
        }
