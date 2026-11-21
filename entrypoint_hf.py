"""
Punto de entrada para Docker / Hugging Face Spaces.
Descomprime base_datos/legislacion_galicia_web.db.gz en /tmp/legislacion_galicia.db
(o descarga desde DRIVE_DB_FILE_ID si se configura), carga el mapa de Google Drive
y arranca el servidor web en 0.0.0.0:7860.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sys
import time
from pathlib import Path

from drive_connector import download_drive_file_to_path, load_drive_map

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
LOCAL_DB = BASE_DIR / "base_datos" / "legislacion_galicia_web.db"
LOCAL_GZ = BASE_DIR / "base_datos" / "legislacion_galicia_web.db.gz"
TMP_DB = Path(os.environ.get("TMP_DB_PATH", "/tmp/legislacion_galicia.db"))
TMP_GZ = Path("/tmp/legislacion_galicia_web.db.gz")


def prepare_database() -> Path:
    env_db = os.environ.get("LEGISLACION_DB_PATH", "").strip()
    if env_db and Path(env_db).is_file():
        print(f"[DB] Usando LEGISLACION_DB_PATH existente: {env_db}")
        return Path(env_db)

    if LOCAL_DB.is_file():
        os.environ["LEGISLACION_DB_PATH"] = str(LOCAL_DB)
        print(f"[DB] Usando base local descomprimida: {LOCAL_DB}")
        return LOCAL_DB

    gz_source = LOCAL_GZ
    chunks = sorted((BASE_DIR / "base_datos").glob("db_chunk_*.bin"))
    if not gz_source.is_file() and chunks:
        print(f"[DB] Reensamblando base comprimida desde {len(chunks)} partes...")
        with TMP_GZ.open("wb") as out_gz:
            for ch in chunks:
                with ch.open("rb") as in_ch:
                    shutil.copyfileobj(in_ch, out_gz, length=1024 * 1024)
        gz_source = TMP_GZ
    elif not gz_source.is_file():
        drive_db_id = os.environ.get("DRIVE_DB_FILE_ID", "").strip()
        if not drive_db_id:
            raise FileNotFoundError(
                "No existe base_datos/legislacion_galicia_web.db.gz ni se configuró DRIVE_DB_FILE_ID."
            )
        print(f"[DB] Descargando índice comprimido desde Google Drive ({drive_db_id})...")
        download_drive_file_to_path(drive_db_id, TMP_GZ)
        gz_source = TMP_GZ

    marker = TMP_DB.with_suffix(".db.marker")
    expected_sig = f"{gz_source.stat().st_size}:{int(gz_source.stat().st_mtime)}"
    if TMP_DB.is_file() and marker.is_file() and marker.read_text(encoding="utf-8").strip() == expected_sig:
        os.environ["LEGISLACION_DB_PATH"] = str(TMP_DB)
        print(f"[DB] Reutilizando base descomprimida en caché: {TMP_DB}")
        return TMP_DB

    t0 = time.time()
    print(f"[DB] Descomprimiendo {gz_source.name} -> {TMP_DB}...")
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = TMP_DB.with_suffix(".db.tmp")
    with gzip.open(gz_source, "rb") as f_in, tmp_out.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
    tmp_out.replace(TMP_DB)
    marker.write_text(expected_sig, encoding="utf-8")
    os.environ["LEGISLACION_DB_PATH"] = str(TMP_DB)
    print(f"[DB] Lista en {time.time() - t0:.2f}s ({TMP_DB.stat().st_size / 1024 / 1024:.1f} MB).")
    return TMP_DB


def main() -> None:
    prepare_database()
    dmap = load_drive_map()
    print(f"[Drive] Mapa cargado: {dmap.get('total_files', 0)} PDFs (fuente: {dmap.get('source')}).")

    import web_app

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "7860"))
    srv = web_app.create_server(host=host, port=port)
    print(f"[Web] Servidor listo en http://{host}:{port} | PUBLIC_MODE={os.environ.get('PUBLIC_MODE', '0')}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
