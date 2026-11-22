/**
 * Google Apps Script para conectar tu carpeta "Antigravity ID Legislación" de Google Drive
 * con el buscador en Render (lectura) y con el actualizador diario de GitHub Actions (escritura y archivo histórico).
 *
 * CÓMO USARLO:
 * A) Indexar mapa inicial / regenerar mapa manual:
 *    1. Sube la carpeta "Antigravity ID Legislación" (al menos `corpus_pdf`) a tu Google Drive.
 *    2. Abre https://script.google.com -> "Nuevo proyecto" (o abre el existente).
 *    3. Pega TODO este archivo, guarda y ejecuta la función `generarMapaDrive`.
 *    4. Acepta los permisos de Google Drive la primera vez.
 *
 * B) Activar subida y archivo histórico automático desde GitHub Actions (Webhook):
 *    1. Cambia WEBHOOK_SECRET_TOKEN abajo por una clave secreta inventada por ti
 *       (o guárdala en Configuración del proyecto -> Propiedades del script -> WEBHOOK_TOKEN).
 *    2. Guarda y pulsa "Implementar" -> "Nueva implementación" (o "Gestionar implementaciones" -> "Nueva versión").
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
const HISTORICO_SUBFOLDER = "99_Historico_Derogadas";

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

function _sanitizeName(rawName, forcePdfSuffix) {
  let clean = String(rawName || "")
    .replace(/\.\./g, "")
    .replace(/[\/\\:*?"<>|\x00-\x1f]/g, "_")
    .trim();
  if (!clean) {
    return "";
  }
  if (forcePdfSuffix && !clean.toLowerCase().endsWith(".pdf")) {
    clean += ".pdf";
  }
  return clean;
}

function _sanitizeSubfolder(rawSub, fallbackSub) {
  const clean = String(rawSub || "")
    .replace(/\.\./g, "")
    .replace(/[\/\\:*?"<>|\x00-\x1f]/g, "")
    .trim();
  return clean || fallbackSub;
}

function _getOrCreateSubfolder(corpusFolder, subfolderName) {
  const iter = corpusFolder.getFoldersByName(subfolderName);
  return iter.hasNext() ? iter.next() : corpusFolder.createFolder(subfolderName);
}

function _loadOrInitDriveMap(rootFolder, corpusFolder) {
  let mapPayload = {
    generated_at: new Date().toISOString(),
    root_folder_id: rootFolder.getId(),
    corpus_folder_id: corpusFolder.getId(),
    total_files: 0,
    by_relative_path: {},
    by_file_name: {}
  };
  let mapFile = null;
  const existingMap = rootFolder.getFilesByName("drive_map.json");
  if (existingMap.hasNext()) {
    mapFile = existingMap.next();
    try {
      const parsed = JSON.parse(mapFile.getBlob().getDataAsString("UTF-8"));
      if (parsed && typeof parsed === "object") {
        mapPayload = parsed;
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
  return { mapFile: mapFile, mapPayload: mapPayload };
}

function _saveDriveMap(rootFolder, mapState) {
  const jsonText = JSON.stringify(mapState.mapPayload);
  if (mapState.mapFile) {
    mapState.mapFile.setContent(jsonText);
    return mapState.mapFile;
  }
  const newFile = rootFolder.createFile("drive_map.json", jsonText, MimeType.PLAIN_TEXT);
  if (COMPARTIR_SOLO_LECTURA_CON_ENLACE) {
    try {
      newFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    } catch (err) {
      Logger.log("Aviso compartiendo drive_map.json: " + err.message);
    }
  }
  return newFile;
}

function _findPdfInCorpus(corpusFolder, preferredFolderName, fileName, fileId) {
  if (fileId) {
    try {
      const byId = DriveApp.getFileById(String(fileId).trim());
      if (byId && !byId.isTrashed()) {
        return byId;
      }
    } catch (err) {
      Logger.log("Aviso buscando por file_id (" + fileId + "): " + err.message);
    }
  }
  if (preferredFolderName) {
    const pIter = corpusFolder.getFoldersByName(preferredFolderName);
    if (pIter.hasNext()) {
      const pf = pIter.next().getFilesByName(fileName);
      if (pf.hasNext()) {
        return pf.next();
      }
    }
  }
  const catIter = corpusFolder.getFolders();
  while (catIter.hasNext()) {
    const cat = catIter.next();
    if (cat.getName() === HISTORICO_SUBFOLDER) {
      continue;
    }
    const fIter = cat.getFilesByName(fileName);
    if (fIter.hasNext()) {
      return fIter.next();
    }
  }
  const rootIter = corpusFolder.getFilesByName(fileName);
  if (rootIter.hasNext()) {
    return rootIter.next();
  }
  return null;
}

function doGet(e) {
  return _jsonResponse({
    ok: true,
    service: "Antigravity Drive Uploader",
    actions: ["upload", "archive"],
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

    const rootName = "Antigravity ID Legislación";
    const rootIter = DriveApp.getFoldersByName(rootName);
    if (!rootIter.hasNext()) {
      return _jsonResponse({ ok: false, error: "root_folder_not_found: " + rootName });
    }
    const rootFolder = rootIter.next();

    const subIter = rootFolder.getFoldersByName("corpus_pdf");
    if (!subIter.hasNext()) {
      return _jsonResponse({ ok: false, error: "corpus_pdf_folder_not_found" });
    }
    const corpusFolder = subIter.next();

    const action = payload.action ? String(payload.action).trim().toLowerCase() : "upload";

    if (action === "archive") {
      const oldRel = payload.relative_path ? String(payload.relative_path).replace(/\\/g, "/").trim() : "";
      const relParts = oldRel ? oldRel.split("/") : [];
      const oldFolderHint = relParts.length > 1 ? _sanitizeSubfolder(relParts[0], "") : "";
      const rawName = payload.file_name || (relParts.length ? relParts[relParts.length - 1] : "");
      const safeFileName = _sanitizeName(rawName, false);
      if (!safeFileName && !payload.file_id) {
        return _jsonResponse({ ok: false, error: "missing_file_identifier" });
      }

      const targetSub = _sanitizeSubfolder(payload.target_subfolder, HISTORICO_SUBFOLDER);
      const targetFolder = _getOrCreateSubfolder(corpusFolder, targetSub);
      const file = _findPdfInCorpus(corpusFolder, oldFolderHint, safeFileName, payload.file_id);
      if (!file) {
        return _jsonResponse({ ok: false, error: "file_not_found" });
      }

      const fid = file.getId();
      const actualName = file.getName();
      file.moveTo(targetFolder);

      const mapState = _loadOrInitDriveMap(rootFolder, corpusFolder);
      const newRel = targetSub + "/" + actualName;
      if (oldRel && oldRel !== newRel) {
        delete mapState.mapPayload.by_relative_path[oldRel];
      }
      const existingKeys = Object.keys(mapState.mapPayload.by_relative_path);
      for (let i = 0; i < existingKeys.length; i++) {
        const k = existingKeys[i];
        if (k !== newRel && mapState.mapPayload.by_relative_path[k] === fid) {
          delete mapState.mapPayload.by_relative_path[k];
        }
      }
      mapState.mapPayload.by_relative_path[newRel] = fid;
      mapState.mapPayload.by_file_name[actualName] = fid;
      mapState.mapPayload.total_files = Object.keys(mapState.mapPayload.by_relative_path).length;
      mapState.mapPayload.generated_at = new Date().toISOString();
      _saveDriveMap(rootFolder, mapState);

      return _jsonResponse({
        ok: true,
        action: "archive",
        moved: true,
        file_id: fid,
        new_relative_path: newRel
      });
    }

    const safeFileName = _sanitizeName(payload.file_name, true);
    if (!safeFileName) {
      return _jsonResponse({ ok: false, error: "missing_file_name" });
    }

    const subfolder = _sanitizeSubfolder(payload.subfolder, DEFAULT_UPLOAD_SUBFOLDER);
    const pdfBase64 = payload.pdf_base64 ? String(payload.pdf_base64).trim() : "";
    if (!pdfBase64) {
      return _jsonResponse({ ok: false, error: "missing_pdf_base64" });
    }

    const targetFolder = _getOrCreateSubfolder(corpusFolder, subfolder);
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

    const mapState = _loadOrInitDriveMap(rootFolder, corpusFolder);
    mapState.mapPayload.by_relative_path[rel] = fid;
    mapState.mapPayload.by_file_name[safeFileName] = fid;
    mapState.mapPayload.total_files = Object.keys(mapState.mapPayload.by_relative_path).length;
    mapState.mapPayload.generated_at = new Date().toISOString();
    const savedMapFile = _saveDriveMap(rootFolder, mapState);

    return _jsonResponse({
      ok: true,
      action: "upload",
      file_id: fid,
      view_url: "https://drive.google.com/file/d/" + fid + "/view",
      relative_path: rel,
      drive_map_file_id: savedMapFile.getId()
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

  // Recorrer subcarpetas dentro de corpus_pdf (01_Estatal_BOE_Codigos .. 99_Historico_Derogadas)
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
