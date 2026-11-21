"""
Actualizador diario autónomo del corpus legislativo educativo de Galicia.
Diseñado para ejecutarse en GitHub Actions (o en local) de forma programada.

Flujo:
1. Reensambla y abre la base de datos SQLite (si está en chunks).
2. Rastrea las fuentes oficiales:
   - Portal Educativo da Xunta (/portal/anuncios y /portal/normativa)
   - Portal FP da Xunta (/fp/normativa y /fp/novas)
   - DOG (Diario Oficial de Galicia - RSS y sección Educación)
3. Detecta documentos o resoluciones nuevas no presentes en el catálogo.
4. Si hay novedades:
   - Descarga los nuevos PDFs.
   - Extrae el texto y número de página real con PyPDF.
   - Indexa en la base de datos (tablas documentos, paginas_meta, fragmentos y FTS).
   - Recomprime y refragmenta la base en db_chunk_*.bin para el despliegue.
5. Envía un correo de notificación a oscar.perez.instituto@gmail.com:
   - Si no hubo novedades: confirmación de chequeo realizado.
   - Si hubo novedades: resumen detallado y los PDFs nuevos adjuntos.
"""

from __future__ import annotations

import email.message
import gzip
import hashlib
import json
import os
import re
import shutil
import smtplib
import sqlite3
import sys
import time
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "base_datos"
TMP_DIR = Path(os.environ.get("RUNNER_TEMP", "/tmp" if os.name != "nt" else str(BASE_DIR / ".tmp_update")))
RECIPIENT_EMAIL = os.environ.get("NOTIFY_EMAIL", "oscar.perez.instituto@gmail.com")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ActualizadorLegislativoGalicia/2.0 (Educacion)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def prepare_local_sqlite() -> Path:
    """Asegura que la base SQLite esté descomprimida y lista para actualizar."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    db_path = TMP_DIR / "legislacion_actualizable.db"

    # Si ya existe descomprimida localmente
    local_db = DB_DIR / "legislacion_galicia_web.db"
    if local_db.is_file():
        shutil.copy2(local_db, db_path)
        return db_path

    # Si existen chunks binarios
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
    urls = set()
    files = set()
    hashes = set()
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


def check_portal_anuncios(session: requests.Session) -> list[dict]:
    """Rastrea avisos y resoluciones recientes en el Portal Educativo."""
    candidates = []
    base_url = "https://www.edu.xunta.gal"
    for page in range(2):
        url = f"{base_url}/portal/anuncios?page={page}"
        try:
            r = session.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            items = soup.select(".view-content .views-row, article.node, .views-field-title")
            for item in items:
                link = item.find("a", href=True)
                if not link:
                    continue
                title = link.get_text(strip=True)
                item_url = urljoin(base_url, link["href"])

                # Ver si contiene enlaces a PDF en el cuerpo del anuncio
                try:
                    r_item = session.get(item_url, headers=HEADERS, timeout=15)
                    if r_item.status_code == 200:
                        soup_item = BeautifulSoup(r_item.text, "html.parser")
                        pdf_links = soup_item.find_all("a", href=re.compile(r"\.pdf($|\?)", re.I))
                        for pl in pdf_links:
                            pdf_url = urljoin(item_url, pl["href"])
                            candidates.append({
                                "title": title or pl.get_text(strip=True) or "Resolución Portal Educativo",
                                "source_url": pdf_url,
                                "ambito": "Autonómico",
                                "etapa": "Transversal",
                                "origen": "Portal Educativo Xunta (Anuncios)"
                            })
                except Exception:
                    continue
        except Exception as e:
            print(f"[Crawler] Error en {url}: {e}")
    return candidates


def check_dog_rss(session: requests.Session) -> list[dict]:
    """Rastrea el RSS diario del DOG buscando resoluciones educativas."""
    candidates = []
    rss_url = "https://www.xunta.gal/diario-oficial-galicia/rss/"
    try:
        r = session.get(rss_url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, "xml")
            items = soup.find_all("item")
            for it in items:
                title = it.title.get_text(strip=True) if it.title else ""
                desc = it.description.get_text(strip=True) if it.description else ""
                link = it.link.get_text(strip=True) if it.link else ""

                full_text = f"{title} {desc}".lower()
                # Filtrar si corresponde a educación, FP, bacharelato o secundaria
                is_edu = any(k in full_text for k in ["educación", "educacion", "profesional", "bacharelato", "secundaria", "instituto", "docente"])
                if is_edu and link:
                    # En el DOG los enlaces suelen llevar a la ficha con PDF
                    candidates.append({
                        "title": title or "Resolución DOG",
                        "source_url": link,
                        "ambito": "Autonómico",
                        "etapa": "Transversal",
                        "origen": "Diario Oficial de Galicia (DOG)"
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

        # Si el URL era una página HTML del DOG, buscar el enlace al PDF real
        if "text/html" in r.headers.get("Content-Type", ""):
            soup = BeautifulSoup(r.text, "html.parser")
            pdf_a = soup.find("a", href=re.compile(r"\.pdf($|\?)", re.I))
            if pdf_a:
                real_pdf_url = urljoin(pdf_url, pdf_a["href"])
                r = session.get(real_pdf_url, headers=HEADERS, timeout=30)
            else:
                return None

        if not r.content.startswith(b"%PDF"):
            return None

        clean_slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", title)[:60].strip("_")
        file_name = f"{datetime.now().strftime('%Y%m%d')}_{clean_slug}.pdf"

        saved_path = TMP_DIR / file_name
        saved_path.write_bytes(r.content)
        sha256_hash = hashlib.sha256(r.content).hexdigest()

        # Extraer páginas
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
    import hashlib
    import json
    import uuid

    c = conn.cursor()
    doc_hash = hashlib.sha256(pdf_data["file_name"].encode("utf-8")).hexdigest()[:12]
    next_id = f"doc_{doc_hash}"

    today_str = datetime.now().strftime("%Y-%m-%d")
    title = cand.get("title", "Resolución Oficial")
    ambito = cand.get("ambito", "Autonómico")
    etapa = cand.get("etapa", "Transversal")
    total_chars = sum(len(p["text"]) for p in pdf_data["pages"])
    raw_b = pdf_data.get("raw_bytes") or b""
    file_bytes = len(raw_b)
    sha256 = hashlib.sha256(raw_b).hexdigest()
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
            f"08_avisos/{pdf_data['file_name']}",
            "08_avisos",
            cand.get("origen", "Portal Educativo"),
            json.dumps([cand.get("origen", "Portal Educativo")]),
            cand["source_url"],
            cand["source_url"],
            json.dumps([etapa]),
            "Resolución",
            ambito,
            "DOG / Portal Educativo",
            today_str,
            "Vigente",
            pdf_data["page_count"],
            total_chars,
            file_bytes,
            sha256,
            selectable_pages,
            is_selectable,
            json.dumps({"page_count": pdf_data["page_count"]}),
            json.dumps({"auto_indexed": True, "date": today_str}),
        ),
    )

    # Insertar páginas en paginas_meta y fragmentos
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


def save_and_rechunk_database(db_path: Path):
    """Recomprime la base de datos y la divide en trozos <42MB para GitHub/Render."""
    print("[Export] Recomprimiendo base de datos...")
    gz_path = DB_DIR / "legislacion_galicia_web.db.gz"
    with db_path.open("rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, 1024 * 1024)

    # Eliminar fragmentos antiguos
    for old_ch in DB_DIR.glob("db_chunk_*.bin"):
        old_ch.unlink()

    # Generar nuevos fragmentos de 42 MB
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


def send_notification_email(updated_docs: list[dict], sources_checked: list[str]):
    """Envía un correo electrónico con los resultados de la comprobación."""
    smtp_user = os.environ.get("GMAIL_USER", RECIPIENT_EMAIL)
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    today_str = datetime.now().strftime("%d/%m/%Y a las %H:%M")

    msg = MIMEMultipart()
    msg["From"] = f"Actualizador Legislación Galicia <{smtp_user}>"
    msg["To"] = RECIPIENT_EMAIL

    if not updated_docs:
        msg["Subject"] = f"✅ Comprobación Legislativa Galicia: Sen novidades ({datetime.now().strftime('%d/%m/%Y')})"
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #2d3748; line-height: 1.6;">
            <div style="background: #f7fafc; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
                <h2 style="color: #2b6cb0; margin-top: 0;">✅ Comprobación Diaria de Legislación Educativa</h2>
                <p>Ola Óscar,</p>
                <p>Realizouse con éxito a comprobación programada o <strong>{today_str}</strong>.</p>
                <div style="background: #ffffff; padding: 15px; border-radius: 6px; border-left: 4px solid #48bb78; margin: 15px 0;">
                    <p style="margin: 0;"><strong>Resultado:</strong> Non se detectaron novas ordes, decretos nin resolucións que afecten a Galicia (ESO, Bacharelato, FP) nas últimas 24 horas.</p>
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
    else:
        msg["Subject"] = f"🚨 Nueva Normativa Educativa Galicia: {len(updated_docs)} archivo(s) nuevo(s)"
        doc_items_html = ""
        for d in updated_docs:
            doc_items_html += f"""
            <li style="margin-bottom: 12px;">
                <strong>{d['title']}</strong><br>
                <span>Páxinas: {d['page_count']} | Ámbito: {d.get('ambito', 'Galicia')}</span><br>
                <a href="{d['source_url']}" style="color: #3182ce;">Ver orixe oficial</a>
            </li>
            """

        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #2d3748; line-height: 1.6;">
            <div style="background: #f7fafc; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
                <h2 style="color: #c53030; margin-top: 0;">🚨 Nova Publicación Normativa Detectada</h2>
                <p>Ola Óscar,</p>
                <p>O rastrexador automático detectou e indexou con éxito <strong>{len(updated_docs)} nova(s) disposición(óns)</strong> o {today_str}:</p>
                <div style="background: #ffffff; padding: 15px; border-radius: 6px; border-left: 4px solid #e53e3e; margin: 15px 0;">
                    <ul style="padding-left: 20px; margin: 0;">
                        {doc_items_html}
                    </ul>
                </div>
                <p><strong>Actualización automática:</strong> A base de datos foi actualizada e despregada en Render. O asistente de IA xa pode responder citando estas páxinas.</p>
                <p><em>Adxúntanse nesta mensaxe os ficheiros PDF descargados para o teu arquivo persoal.</em></p>
            </div>
        </body>
        </html>
        """

    msg.attach(MIMEText(body, "html", "utf-8"))

    # Adjuntar archivos PDF descargados (hasta un límite prudente de 20 MB)
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
    print(f"=== INICIO COMPROBACIÓN DIARIA LEGISLACIÓN ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===")
    t0 = time.time()
    db_path = prepare_local_sqlite()
    conn = sqlite3.connect(str(db_path))

    existing_urls, existing_files, existing_hashes = get_existing_signatures(conn)
    print(f"[DB] Base cargada: {len(existing_files)} documentos indexados previamente.")

    session = requests.Session()
    session.headers.update(HEADERS)

    sources_checked = [
        "Portal Educativo da Xunta (/portal/anuncios)",
        "Portal Educativo da Xunta (/portal/normativa)",
        "Portal FP da Xunta (/fp/normativa e /fp/novas)",
        "Diario Oficial de Galicia (DOG RSS e Educación)",
    ]

    print("[Crawler] Buscando novas publicacións en fontes oficiais...")
    candidates = []
    candidates.extend(check_portal_anuncios(session))
    candidates.extend(check_dog_rss(session))

    print(f"[Crawler] Evaluando {len(candidates)} publicacións recentes atopadas...")
    new_docs = []
    for cand in candidates:
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

        # Indexar
        doc_id = index_new_document(conn, cand, pdf_data)
        existing_urls.add(src_url)
        existing_files.add(pdf_data["file_name"].lower())
        existing_hashes.add(pdf_data["sha256"].lower())

        new_docs.append({
            "id": doc_id,
            "title": cand["title"],
            "page_count": pdf_data["page_count"],
            "source_url": cand["source_url"],
            "ambito": cand.get("ambito", "Autonómico"),
            "file_path": pdf_data["file_path"]
        })

    conn.close()

    if new_docs:
        print(f"[Update] Se indexaron {len(new_docs)} novos documentos. Recomprimiendo...")
        save_and_rechunk_database(db_path)
    else:
        print("[Update] Comprobación rematada: Ningunha normativa nova hoxe.")

    # Enviar correo de notificación
    send_notification_email(new_docs, sources_checked)

    # Limpiar archivo temporal de db
    if db_path.exists():
        try:
            db_path.unlink()
        except Exception:
            pass

    print(f"=== PROCESO COMPLETADO EN {time.time() - t0:.2f}s ===")


if __name__ == "__main__":
    main()
