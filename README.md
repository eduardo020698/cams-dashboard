# Dashboard de Calidad del Aire · Perú y Pacífico Oriental (CAMS)

Dashboard interactivo con concentraciones en superficie de **PM2.5, PM10, O₃, SO₂ y NO₂** del
**CAMS Global Atmospheric Composition Forecasts** (Copernicus), actualizado automáticamente cada día.
Solo se almacenan los **últimos 3 días** de data.

> Nota: se usa el dataset de *forecasts* (casi tiempo real) y no el *reanalysis* (EAC4),
> porque el reanalysis tiene ~2 años de retraso y no permite actualización diaria.

## Cómo funciona

1. **GitHub Actions** corre cada día a las 10:30 UTC (05:30 hora Perú).
2. `scripts/fetch_cams.py` descarga del ADS los últimos 3 días (run 00Z, cada 3 h) para el área
   2°N–20°S, 92°O–66°O, convierte todo a µg/m³ y genera JSONs compactos.
3. El sitio (`site/`) se publica en **GitHub Pages**. La data nunca se guarda en el repositorio:
   cada despliegue contiene exactamente los últimos 3 días. Gratis y sin servidores propios.

## Despliegue paso a paso (no necesitas saber programar)

### 1. Crear cuenta en GitHub (5 min)
1. Entra a https://github.com/signup
2. Regístrate con tu correo y verifica tu email.

### 2. Crear el repositorio
1. Arriba a la derecha: **+** → **New repository**.
2. Nombre: `cams-dashboard` · visibilidad: **Public** (necesario para Pages gratis) → **Create repository**.

### 3. Subir los archivos
1. En el repositorio: **uploading an existing file** (o Add file → Upload files).
2. Arrastra TODO el contenido de la carpeta `cams-dashboard` (incluyendo las carpetas
   `.github`, `scripts`, `site`). Si el navegador no sube la carpeta `.github`,
   créala a mano: **Add file → Create new file**, escribe como nombre
   `.github/workflows/update.yml` y pega el contenido de ese archivo.
3. **Commit changes**.

### 4. Guardar tu API key como secreto (¡nunca la subas al código!)
1. En el repositorio: **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `ADS_API_KEY`
3. Secret: tu key del archivo `.cdsapirc` (solo la parte después de `key:`, ej. `bf65f010-...`).
4. **Add secret**.

> Requisito único: entra una vez a https://ads.atmosphere.copernicus.eu, inicia sesión y acepta
> la licencia del dataset "CAMS global atmospheric composition forecasts" (pestaña Download →
> aceptar términos al final). Sin esto el API rechaza las descargas.

### 5. Activar GitHub Pages
1. **Settings → Pages** → en "Build and deployment", Source: **GitHub Actions**.

### 6. Primera ejecución
1. Pestaña **Actions** → habilita los workflows si lo pide → selecciona
   "Actualizar data CAMS y publicar dashboard" → **Run workflow**.
2. Tarda ~10–30 min (las colas del ADS varían). Al terminar, tu dashboard queda en:
   `https://TU_USUARIO.github.io/cams-dashboard/`
3. A partir de ahí se actualiza solo, todos los días.

## Ver el dashboard localmente (opcional)

```
cd cams-dashboard/site
python -m http.server 8000
```
Abre http://localhost:8000 — ahora mismo contiene **data de demostración** (verás un banner naranja);
la data real aparece tras la primera ejecución del workflow.

Para descargar data real desde tu PC (opcional, requiere Python):
```
pip install -r requirements.txt
set ADS_API_KEY=TU_KEY        (Windows)
python scripts/fetch_cams.py
```

## Uso del dashboard

- **Chips superiores**: cambia el contaminante mostrado en el mapa.
- **Slider inferior / ▶**: navega o anima las últimas 72 h (pasos de 3 h, hora de Perú).
- **Clic en el mapa**: panel con los valores de los 5 contaminantes en ese punto y el
  gráfico de **tendencia de las últimas 48 h** del contaminante seleccionado.

## Detalles técnicos

| Ítem | Valor |
|---|---|
| Dataset | cams-global-atmospheric-composition-forecasts (ADS) |
| Área | 2°N–20°S, 92°O–66°O (Perú + Pacífico oriental) |
| Resolución | 0.4° × 0.4°, cada 3 h (run 00Z) |
| Conversión gases | mmr (kg/kg) × ρ, con ρ = P_sup / (R·T) — válido también en los Andes |
| PM | kg/m³ × 10⁹ → µg/m³ |
| Retención | 3 días (cada corrida re-descarga y re-publica solo esos días) |
