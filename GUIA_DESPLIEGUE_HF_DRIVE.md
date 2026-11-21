# Guía de Despliegue Gratuito: Google Drive + Hugging Face Spaces (+ Google Sites)

Esta carpeta (`deploy_hf`) contiene todo lo necesario para publicar gratis la web en **Hugging Face Spaces** (0 €/mes, 16 GB RAM, sin tarjeta bancaria), conectada a tus **3.734 PDFs en Google Drive** e incrustable opcionalmente en **Google Sites**.

---

## Paso 1: Subir los PDFs a Google Drive y generar el mapa (`drive_map.json`)
1. Sube tu carpeta local **`Antigravity ID Legislación`** (especialmente la subcarpeta **`corpus_pdf`** con los 3.734 PDFs) a la raíz de tu **Google Drive**.
2. Abre [Google Apps Script](https://script.google.com) → **Nuevo proyecto**.
3. Copia todo el contenido de **`generar_drive_map.gs`**, pégalo en el editor y pulsa **Guardar**.
4. Arriba, selecciona la función **`generarMapaDrive`** y pulsa **Ejecutar** (acepta los permisos la primera vez).
5. En el registro verás dos cosas importantes:
   - El total de PDFs mapeados (`3734`).
   - El identificador **`DRIVE_MAP_FILE_ID`** (y se habrá creado el archivo `drive_map.json` dentro de tu carpeta en Google Drive).
   - *(Opcional)* También puedes descargar `drive_map.json` de Drive y copiarlo dentro de esta carpeta `deploy_hf/` antes de subirla a Hugging Face, o simplemente usar el Secret `DRIVE_MAP_FILE_ID`.

> **Nota:** Mientras terminas de subir los PDFs a Drive o si un PDF concreto no estuviese mapeado aún, la web descarga automáticamente el PDF desde la URL oficial de la Xunta/DOG/BOE como respaldo para que el visor lateral funcione siempre.

---

## Paso 2: Crear el Space gratuito en Hugging Face
1. Entra en [huggingface.co/new-space](https://huggingface.co/new-space) (crea cuenta gratuita si no tienes).
2. Configura:
   - **Space name:** por ejemplo `lexislacion-galicia-educacion`
   - **License:** `mit` (o la que prefieras)
   - **Select the Space SDK:** **`Docker`** → plantilla **`Blank`**
   - **Space hardware:** **`CPU basic · 2 vCPU · 16 GB · FREE`**
   - **Visibility:** **Public** o **Private** (si lo dejas Public y quieres restringir quién pregunta, añade el Secret `ACCESS_PIN` en el Paso 3).
3. Sube todo el contenido de la carpeta **`deploy_hf/`** al repositorio del Space:
   - **Opción A (por navegador, sin comandos):** en la pestaña **Files** de tu Space → **Add file → Upload files**, arrastra todos los archivos de `deploy_hf/` (incluyendo la carpeta `base_datos/legislacion_galicia_web.db.gz`).
   - **Opción B (por Git con Git LFS si subes `legislacion_galicia_web.db.gz`):**
     ```powershell
     git clone https://huggingface.co/spaces/TU_USUARIO/lexislacion-galicia-educacion
     cd lexislacion-galicia-educacion
     git lfs install
     git lfs track "*.gz"
     # Copia aquí todo el contenido de deploy_hf\
     git add .
     git commit -m "Despliegue inicial corpus Galicia + Gemini + Drive"
     git push
     ```

---

## Paso 3: Configurar los Secrets (tu clave nunca queda expuesta)
En tu Space de Hugging Face, ve a **Settings → Variables and secrets → New secret**:

1. **`GEMINI_API_KEY`** *(Obligatorio)*: pega tu API key de Gemini (`AQ.Ab8...`).
2. **`DRIVE_MAP_FILE_ID`** *(Recomendado)*: pega el ID de `drive_map.json` que imprimió Apps Script en el Paso 1 (si no lo copiastes dentro de `deploy_hf/`).
3. **`ACCESS_PIN`** *(Opcional)*: si quieres proteger el chat con un código numérico/contraseña para que solo tú o tu centro gastéis tu cuota de Gemini.

Tras guardar los Secrets, Hugging Face reconstruirá el contenedor Docker automáticamente (~1–2 minutos) y tu web quedará activa en `https://TU_USUARIO-lexislacion-galicia-educacion.hf.space`.

---

## Paso 4 (Opcional): Incrustar la web dentro de Google Sites
1. Abre [sites.google.com](https://sites.google.com) y crea o edita tu sitio.
2. En el menú derecho pulsa **Insertar → Insertar** (`Embed`) → **Por URL**.
3. Pega la URL directa de tu Space: `https://TU_USUARIO-lexislacion-galicia-educacion.hf.space` (o si usas la URL estándar del hub, usa la URL directa `.hf.space` que aparece en el menú `Embed this Space` del Space).
4. Ajusta el tamaño al ancho completo de la página y pulsa **Publicar**.
