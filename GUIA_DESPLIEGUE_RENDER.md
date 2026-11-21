# Guía de Despliegue Gratuito en Render.com + Google Sites

Esta solución es **100% gratuita**, no requiere pagar nada ni suscripciones especiales, y te proporciona una URL HTTPS oficial permanente para incrustar en tu Google Sites.

---

## Paso 1: Crear el repositorio en GitHub (1 minuto)

1. Abre tu navegador y entra en [github.com/new](https://github.com/new).
2. En **Repository name**, escribe: `legislacion-galicia`
3. Puedes dejarlo en **Public** o **Private**.
4. **IMPORTANTE:** No marques ninguna casilla ("Add a README", .gitignore, license). Déjalo vacío.
5. Pulsa el botón verde **Create repository**.

---

## Paso 2: Subir el código desde tu PC

Abre una terminal PowerShell o copia y ejecuta estos comandos (ya dejamos la carpeta preparada y con los fragmentos de la base de datos de <75 MB listos):

```powershell
cd "C:\Users\oscar\Desktop\Antigravity ID Legislación\deploy_hf"
git remote add origin https://github.com/opbpo55/legislacion-galicia.git
git push -u origin main
```

---

## Paso 3: Desplegar en Render.com (2 minutos)

1. Entra en [dashboard.render.com](https://dashboard.render.com) e inicia sesión con el botón **GitHub** (para que conecte directamente con tu cuenta).
2. Haz clic en el botón superior **New +** y selecciona **Web Service**.
3. Elige la opción **Build and deploy from a Git repository** y pulsa **Next**.
4. En la lista de repositorios verás `legislacion-galicia`. Pulsa en **Connect**.
5. Configura estos campos:
   - **Name:** `legislacion-galicia` (o el nombre que prefieras).
   - **Region:** Frankfurt (EU) o la que prefieras.
   - **Environment:** Render detectará automáticamente `Docker`.
   - **Instance Type:** Selecciona **Free** ($0/month).
6. Despliega la sección **Environment Variables** y añade las siguientes variables:
   - `PUBLIC_MODE` = `1`
   - `GEMINI_API_KEY` = *(Pega aquí la clave de Gemini proporcionada)*
   - `DRIVE_MAP_FILE_ID` = *(el ID que obtuviste al ejecutar el script en Google Drive)*
7. Haz clic en **Deploy Web Service** (botón al final).

En unos 2 o 3 minutos, el panel mostrará el estado **Live** en verde y te dará tu enlace permanente:
`https://legislacion-galicia.onrender.com`

---

## Paso 4: Incrustar en Google Sites (1 minuto)

1. Abre tu web en [sites.google.com](https://sites.google.com).
2. En el menú de la derecha, pulsa en **Insertar** > **Mediante URL** (o *Insertar código*).
3. Pega la URL de tu servicio de Render (ejemplo: `https://legislacion-galicia.onrender.com`).
4. Selecciona **Página completa** e insértala.
5. Ajusta el tamaño de la caja al ancho y alto que desees y pulsa **Publicar**.
