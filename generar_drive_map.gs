/**
 * Google Apps Script para conectar tu carpeta "Antigravity ID Legislación" de Google Drive
 * con el buscador en Render (lectura) y con el actualizador diario de GitHub Actions (escritura).
 *
 * CÓMO USARLO:
 * A) Indexar mapa inicial / regenerar mapa manual:
 *    1. Sube la carpeta "Antigravity ID Legislación" (al menos `corpus_pdf`) a tu Google Drive.
 *    2. Abre https://script.google.com -> "Nuevo proyecto" (o abre el existente).
 *    3. Pega TODO este archivo, guarda y ejecuta la función `generarMapaDrive`.
 *    4. Acepta los permisos de Google Drive la primera vez.
 *
 * B) Activar subida automática diaria desde GitHub Actions (Webhook):
 *    1. Cambia WEBHOOK_SECRET_TOKEN abajo por una clave secreta inventada por ti
 *       (o guárdala en Configuración del proyecto -> Propiedades del script -> WEBHOOK_TOKEN).
 *    2. Guarda y pulsa "Implementar" -> "Nueva implementación".
 *    3. Tipo: "Aplicación web" | Ejecutar como: "Yo" | Quién tiene acceso: "Cualquier persona".
 *    4. Copia la URL de la aplicación web y guárdala en GitHub Secrets como `DRIVE_WEBHOOK_URL`,
 *       junto con tu clave secreta como `DRIVE_WEBHOOK_TOKEN`.
 */

// Cambia a `true` si quieres que el script haga compartible con enlace de solo lectura
// la carpeta `corpus_pdf`, `drive_map.json` y los nuevos PDFs subidos automáticamente.
const COMPARTIR_SOLO_LECTURA_CON_ENLACE = true;

// Cambia este valor por una contraseña secreta larga y privada (NO la subas a GitHub).
const WEBHOOK_SECRET_TOKEN = "CAMBIAR_POR_TU_CLAVE_SECRETA";
const DEFAULT_UPLOAD_SUBFOLDER = "08_Resolucions_Instrucions_Anuncios";

function _getExpectedToken() {
  try {
    const prop = PropertiesService.getScriptProperties().getProperty("WEBHOOK_TOKEN");
    if (prop && String(prop).trim()) {
      return String(prop).trim();
    }
  } catch (e) {
    // Si no hay propiedad configurada, se usa la constante local
  }
  const fallback = String(WEBHOOK_SECRET_TOKEN || "").trim();
  if (!fallback || fallback === "CAMBIAR_POR_TU_CLAVE_SECRETA") {
    return "";
  }
  return fallback;
}

function _jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  return _jsonResponse({
    ok: true,
    service: "Antigravity Drive Uploader",
    token_configured: Boolean(_getExpectedToken())
  });
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return _jsonResponse({ ok: false, error: "empty_request_body" });
    }

    let payload;
    try {
      payload = JSON.parse(e.postData.contents);
    } catch (err) {
      return _jsonResponse({ ok: false, error: "invalid_json" });
    }

    const expectedToken = _getExpectedToken();
    const providedToken = payload && payload.token ? String(payload.token).trim() : "";
    if (!expectedToken || !providedToken || providedToken !== expectedToken) {
      return _jsonResponse({ ok: false, error: "unauthorized" });
    }

    // Sanitizar nombre de archivo
    let rawName = payload.file_name ? String(payload.file_name) : "";
    let safeFileName = rawName
      .replace(/\.\./g, "")
      .replace(/[\/\\:*?"<>|\x00-\x1f]/g, "_")
      .trim();
    if (!safeFileName) {
      return _jsonResponse({ ok: false, error: "missing_file_name" });
    }
    if (!safeFileName.toLowerCase().endsWith(".pdf")) {
      safeFileName += ".pdf";
    }

    // Sanitizar subcarpeta destino
    let rawSub = payload.subfolder ? String(payload.subfolder) : DEFAULT_UPLOAD_SUBFOLDER;
    let subfolder = rawSub
      .replace(/\.\./g, "")
      .replace(/[\/\\:*?"<>|\x00-\x1f]/g, "")
      .trim();
    if (!subfolder) {
      subfolder = DEFAULT_UPLOAD_SUBFOLDER;
    }

    const pdfBase64 = payload.pdf_base64 ? String(payload.pdf_base64).trim() : "";
    if (!pdfBase64) {
      return _jsonResponse({ ok: false, error: "missing_pdf_base64" });
    }

    const rootName = "Antigravity ID Legislación";
    const rootIter = DriveApp.getFoldersByName(rootName);
    if (!rootIter.hasNext()) {
      return _jsonResponse({ ok: false, error: 'root_folder_not_found: ' + rootName });
    }
    const rootFolder = rootIter.next();

    const subIter = rootFolder.getFoldersByName("corpus_pdf");
    if (!subIter.hasNext()) {
      return _jsonResponse({ ok: false, error: "corpus_pdf_folder_not_found" });
    }
    const corpusFolder = subIter.next();

    const targetIter = corpusFolder.getFoldersByName(subfolder);
    const targetFolder = targetIter.hasNext()
      ? targetIter.next()
      : corpusFolder.createFolder(subfolder);

    let file;
    const existingFiles = targetFolder.getFilesByName(safeFileName);
    if (existingFiles.hasNext()) {
      file = existingFiles.next();
    } else {
      const bytes = Utilities.base64Decode(pdfBase64);
      const blob = Utilities.newBlob(bytes, "application/pdf", safeFileName);
      file = targetFolder.createFile(blob);
    }

    if (COMPARTIR_SOLO_LECTURA_CON_ENLACE) {
      try {
        file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
      } catch (err) {
        Logger.log("Aviso compartiendo PDF: " + err.message);
      }
    }

    const fid = file.getId();
    const rel = subfolder + "/" + safeFileName;

    // Actualizar drive_map.json incrementalmente
    let mapPayload = {
      generated_at: new Date().toISOString(),
      root_folder_id: rootFolder.getId(),
      corpus_folder_id: corpusFolder.getId(),
      total_files: 0,
      by_relative_path: {},
      by_file_name: {}
    };

    const existingMap = rootFolder.getFilesByName("drive_map.json");
    let mapFile = null;
    if (existingMap.hasNext()) {
      mapFile = existingMap.next();
      try {
        const parsedMap = JSON.parse(mapFile.getBlob().getDataAsString("UTF-8"));
        if (parsedMap && typeof parsedMap === "object") {
          mapPayload = parsedMap;
        }
      } catch (err) {
        Logger.log("Aviso leyendo drive_map.json previo: " + err.message);
      }
    }

    if (!mapPayload.by_relative_path || typeof mapPayload.by_relative_path !== "object") {
      mapPayload.by_relative_path = {};
    }
    if (!mapPayload.by_file_name || typeof mapPayload.by_file_name !== "object") {
      mapPayload.by_file_name = {};
    }

    mapPayload.by_relative_path[rel] = fid;
    mapPayload.by_file_name[safeFileName] = fid;
    mapPayload.total_files = Object.keys(mapPayload.by_relative_path).length;
    mapPayload.generated_at = new Date().toISOString();

    const updatedJson = JSON.stringify(mapPayload);
    if (mapFile) {
      mapFile.setContent(updatedJson);
    } else {
      mapFile = rootFolder.createFile("drive_map.json", updatedJson, MimeType.PLAIN_TEXT);
      if (COMPARTIR_SOLO_LECTURA_CON_ENLACE) {
        try {
          mapFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
        } catch (err) {
          Logger.log("Aviso compartiendo drive_map.json: " + err.message);
        }
      }
    }

    return _jsonResponse({
      ok: true,
      file_id: fid,
      view_url: "https://drive.google.com/file/d/" + fid + "/view",
      relative_path: rel,
      drive_map_file_id: mapFile.getId()
    });
  } catch (err) {
    return _jsonResponse({ ok: false, error: err && err.message ? err.message : String(err) });
  }
}

function generarMapaDrive() {
  const rootName = "Antigravity ID Legislación";
  const rootIter = DriveApp.getFoldersByName(rootName);
  if (!rootIter.hasNext()) {
    throw new Error('No se encontró la carpeta "' + rootName + '" en tu Google Drive.');
  }
  const rootFolder = rootIter.next();

  let corpusFolder = null;
  const subIter = rootFolder.getFoldersByName("corpus_pdf");
  if (subIter.hasNext()) {
    corpusFolder = subIter.next();
  } else {
    throw new Error('Dentro de "' + rootName + '" no se encontró la subcarpeta "corpus_pdf".');
  }

  if (COMPARTIR_SOLO_LECTURA_CON_ENLACE) {
    try {
      corpusFolder.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    } catch (e) {
      Logger.log("Aviso compartiendo corpus_pdf: " + e.message);
    }
  }

  const byRelativePath = {};
  const byFileName = {};
  let total = 0;

  // Recorrer subcarpetas dentro de corpus_pdf (01_Estatal_BOE_Codigos .. 08_Resolucions_Instrucions_Anuncios)
  const catFolders = corpusFolder.getFolders();
  while (catFolders.hasNext()) {
    const cat = catFolders.next();
    const catName = cat.getName();
    const files = cat.getFiles();
    while (files.hasNext()) {
      const file = files.next();
      const name = file.getName();
      if (!name.toLowerCase().endsWith(".pdf")) continue;
      const fid = file.getId();
      const rel = catName + "/" + name;
      byRelativePath[rel] = fid;
      byFileName[name] = fid;
      total++;
    }
  }

  // Por si hubiese PDFs sueltos directamente en corpus_pdf
  const rootFiles = corpusFolder.getFiles();
  while (rootFiles.hasNext()) {
    const file = rootFiles.next();
    const name = file.getName();
    if (!name.toLowerCase().endsWith(".pdf")) continue;
    const fid = file.getId();
    byRelativePath[name] = fid;
    byFileName[name] = fid;
    total++;
  }

  const payload = {
    generated_at: new Date().toISOString(),
    root_folder_id: rootFolder.getId(),
    corpus_folder_id: corpusFolder.getId(),
    total_files: total,
    by_relative_path: byRelativePath,
    by_file_name: byFileName
  };

  const jsonText = JSON.stringify(payload);
  const existing = rootFolder.getFilesByName("drive_map.json");
  let mapFile;
  if (existing.hasNext()) {
    mapFile = existing.next();
    mapFile.setContent(jsonText);
  } else {
    mapFile = rootFolder.createFile("drive_map.json", jsonText, MimeType.PLAIN_TEXT);
  }

  if (COMPARTIR_SOLO_LECTURA_CON_ENLACE) {
    try {
      mapFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    } catch (e) {
      Logger.log("Aviso compartiendo drive_map.json: " + e.message);
    }
  }

  Logger.log("=== MAPA GENERADO CON ÉXITO ===");
  Logger.log("Total de PDFs indexados en Drive: " + total);
  Logger.log("DRIVE_MAP_FILE_ID (pegar en Secret de Render): " + mapFile.getId());
  Logger.log("O descarga drive_map.json desde: " + mapFile.getUrl());
}
