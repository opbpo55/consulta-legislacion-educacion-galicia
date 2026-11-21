/**
 * Google Apps Script para generar automáticamente `drive_map.json`
 * conectando tu carpeta "Antigravity ID Legislación" de Google Drive
 * con el buscador en Hugging Face Spaces.
 *
 * CÓMO USARLO (1 minuto):
 * 1. Sube la carpeta "Antigravity ID Legislación" (al menos `corpus_pdf`) a tu Google Drive.
 * 2. Abre https://script.google.com -> "Nuevo proyecto".
 * 3. Borra el código de ejemplo, pega TODO este archivo y pulsa Guardar.
 * 4. Selecciona la función `generarMapaDrive` arriba y pulsa "Ejecutar".
 * 5. Acepta los permisos de Google Drive la primera vez.
 * 6. Al finalizar, mira el Registro de ejecución: te dará el `DRIVE_MAP_FILE_ID`
 *    y guardará `drive_map.json` dentro de tu carpeta en Google Drive.
 */

// Cambia a `true` si quieres que el script haga compartible con enlace de solo lectura
// la carpeta `corpus_pdf` y `drive_map.json` automáticamente para que el visor web
// pueda abrir los PDFs desde Hugging Face sin pedir inicio de sesión.
const COMPARTIR_SOLO_LECTURA_CON_ENLACE = true;

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
  Logger.log("DRIVE_MAP_FILE_ID (pegar en Secret de Hugging Face): " + mapFile.getId());
  Logger.log("O descarga drive_map.json desde: " + mapFile.getUrl());
}
