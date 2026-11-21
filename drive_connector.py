"""
Conector de Google Drive + caché LRU en /tmp para servir PDFs en Hugging Face Spaces.
Orden de resolución de PDF:
  1. Caché local en /tmp/pdf_cache (LRU máx. 400 MB)
  2. Carpeta local corpus_pdf/ si existe
  3. Google Drive (mediante drive_map.json local o DRIVE_MAP_FILE_ID)
  4. Fallback automático a la URL oficial de publicación (DOG / BOE / Lex.gal / Xunta)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import requests

BASE_DIR = Path(__file__).resolve().parent
CORPUS_DIR = BASE_DIR / "corpus_pdf"
PDF_CACHE_DIR = Path(os.environ.get("PDF_CACHE_DIR", "/tmp/pdf_cache"))
MAX_CACHE_BYTES = int(os.environ.get("PDF_CACHE_MAX_MB", "400")) * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_CACHE_LOCK = threading.Lock()
_MAP_LOCK = threading.Lock()
_DRIVE_MAP: dict[str, Any] | None = None


def _trim_pdf_header(raw_bytes: bytes) -> bytes | None:
    if not raw_bytes:
        return None
    idx = raw_bytes.find(b"%PDF-", 0, 2048)
    if idx == -1:
        return None
    return raw_bytes[idx:]


def download_drive_file_bytes(file_id: str, timeout: int = 45) -> bytes:
    """Descarga un fichero público/compartido de Google Drive manejando archivos grandes."""
    fid = (file_id or "").strip()
    if not fid:
        raise ValueError("file_id vacío")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    urls = [
        f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t",
        f"https://drive.google.com/uc?export=download&confirm=t&id={fid}",
    ]
    last_err = ""
    for u in urls:
        try:
            r = session.get(u, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.content:
                ct = (r.headers.get("Content-Type") or "").lower()
                if "text/html" in ct and b"%PDF-" not in r.content[:2048] and not r.content.lstrip().startswith(b"{"):
                    # Comprobar formulario de confirmación de Drive
                    m_conf = re.search(r'name="confirm"\s+value="([^"]+)"', r.text)
                    m_uuid = re.search(r'name="uuid"\s+value="([^"]+)"', r.text)
                    if m_conf:
                        params = {"id": fid, "export": "download", "confirm": m_conf.group(1)}
                        if m_uuid:
                            params["uuid"] = m_uuid.group(1)
                        r2 = session.get(
                            "https://drive.usercontent.google.com/download",
                            params=params,
                            timeout=timeout,
                            allow_redirects=True,
                        )
                        if r2.status_code == 200 and r2.content:
                            return r2.content
                    last_err = f"Google Drive devolvió HTML en lugar del archivo ({fid})"
                    continue
                return r.content
            last_err = f"HTTP {r.status_code} en {u}"
        except Exception as exc:
            last_err = str(exc)
    raise RuntimeError(last_err or f"No se pudo descargar de Google Drive ({fid})")


def download_drive_file_to_path(file_id: str, dest_path: Path, timeout: int = 120) -> Path:
    """Descarga un archivo de Google Drive directamente a disco en streaming."""
    fid = (file_id or "").strip()
    if not fid:
        raise ValueError("file_id vacío")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_suffix(dest_path.suffix + ".tmp")
    url = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    with session.get(url, stream=True, timeout=timeout, allow_redirects=True) as r:
        if r.status_code != 200:
            raise RuntimeError(f"Error HTTP {r.status_code} descargando Drive ID {fid}")
        ct = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ct:
            raise RuntimeError(f"Drive ID {fid} devolvió HTML (comprueba que esté compartido como 'Cualquier persona con el enlace').")
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    tmp.replace(dest_path)
    return dest_path


def load_drive_map(force_reload: bool = False) -> dict[str, Any]:
    """Carga el mapa drive_map.json desde disco local o desde DRIVE_MAP_FILE_ID."""
    global _DRIVE_MAP
    with _MAP_LOCK:
        if _DRIVE_MAP is not None and not force_reload:
            return _DRIVE_MAP

        empty = {"by_relative_path": {}, "by_file_name": {}, "total_files": 0, "source": "none"}
        local_candidates = [
            Path(os.environ.get("DRIVE_MAP_PATH", "")) if os.environ.get("DRIVE_MAP_PATH") else BASE_DIR / "drive_map.json",
            Path("/tmp/drive_map.json"),
        ]
        for cand in local_candidates:
            if cand.is_file():
                try:
                    raw = json.loads(cand.read_text(encoding="utf-8"))
                    by_rel = {str(k).replace("\\", "/"): str(v) for k, v in (raw.get("by_relative_path") or {}).items()}
                    by_name = {str(k): str(v) for k, v in (raw.get("by_file_name") or {}).items()}
                    _DRIVE_MAP = {
                        "by_relative_path": by_rel,
                        "by_file_name": by_name,
                        "total_files": len(by_rel) or len(by_name),
                        "source": str(cand),
                    }
                    return _DRIVE_MAP
                except Exception:
                    pass

        map_fid = os.environ.get("DRIVE_MAP_FILE_ID", "").strip()
        if map_fid:
            try:
                raw_bytes = download_drive_file_bytes(map_fid, timeout=30)
                raw = json.loads(raw_bytes.decode("utf-8"))
                Path("/tmp/drive_map.json").write_bytes(raw_bytes)
                by_rel = {str(k).replace("\\", "/"): str(v) for k, v in (raw.get("by_relative_path") or {}).items()}
                by_name = {str(k): str(v) for k, v in (raw.get("by_file_name") or {}).items()}
                _DRIVE_MAP = {
                    "by_relative_path": by_rel,
                    "by_file_name": by_name,
                    "total_files": len(by_rel) or len(by_name),
                    "source": f"drive:{map_fid}",
                }
                return _DRIVE_MAP
            except Exception:
                pass

        _DRIVE_MAP = empty
        return _DRIVE_MAP


def get_drive_file_id(relative_path: str, file_name: str = "") -> str:
    dmap = load_drive_map()
    rel = (relative_path or "").replace("\\", "/").lstrip("/")
    fid = dmap["by_relative_path"].get(rel, "")
    if not fid and file_name:
        fid = dmap["by_file_name"].get(file_name, "")
    if not fid and rel:
        fid = dmap["by_file_name"].get(Path(rel).name, "")
    return fid


def get_drive_view_url(relative_path: str, file_name: str = "") -> str:
    fid = get_drive_file_id(relative_path, file_name)
    return f"https://drive.google.com/file/d/{fid}/view" if fid else ""


def _enforce_cache_limit() -> None:
    try:
        files = [p for p in PDF_CACHE_DIR.glob("*.pdf") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= MAX_CACHE_BYTES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files:
            if total <= MAX_CACHE_BYTES * 0.8:
                break
            sz = p.stat().st_size
            p.unlink(missing_ok=True)
            total -= sz
    except Exception:
        pass


def fetch_pdf_bytes(
    relative_path: str,
    file_name: str = "",
    official_pdf_url: str = "",
) -> tuple[bytes, str]:
    """Obtiene los bytes válidos de un PDF desde caché, disco local, Google Drive o URL oficial."""
    rel = (relative_path or "").replace("\\", "/").lstrip("/")
    cache_key = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = PDF_CACHE_DIR / f"{cache_key}.pdf"

    with _CACHE_LOCK:
        if cache_file.is_file():
            try:
                cache_file.touch()
                return cache_file.read_bytes(), "cache"
            except Exception:
                pass

    # 1. Local disk (si existe corpus_pdf completo)
    local_target = (CORPUS_DIR / rel).resolve()
    if CORPUS_DIR.exists() and local_target.is_relative_to(CORPUS_DIR.resolve()) and local_target.is_file():
        return local_target.read_bytes(), "local"

    # 2. Google Drive via drive_map.json
    fid = get_drive_file_id(rel, file_name)
    if fid:
        try:
            raw = download_drive_file_bytes(fid, timeout=45)
            pdf_bytes = _trim_pdf_header(raw)
            if pdf_bytes:
                with _CACHE_LOCK:
                    cache_file.write_bytes(pdf_bytes)
                    _enforce_cache_limit()
                return pdf_bytes, "google_drive"
        except Exception:
            pass

    # 3. Fallback a la URL oficial (DOG / Lex.gal / BOE / Xunta)
    if official_pdf_url:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        try:
            r = session.get(official_pdf_url, timeout=35, allow_redirects=True)
            if r.status_code == 200:
                pdf_bytes = _trim_pdf_header(r.content)
                if pdf_bytes:
                    with _CACHE_LOCK:
                        cache_file.write_bytes(pdf_bytes)
                        _enforce_cache_limit()
                    return pdf_bytes, "official_fallback"
        except Exception as exc:
            raise FileNotFoundError(f"Non se puido recuperar o PDF nin de Drive nin da fonte oficial ({exc}).")

    raise FileNotFoundError(f"PDF non dispoñible para {rel}")
