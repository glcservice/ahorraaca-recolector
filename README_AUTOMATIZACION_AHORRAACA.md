# AhorraAcá — recolector automático

Este paquete deja el recolector ejecutándose automáticamente en GitHub Actions.

## Archivos que van al repositorio

Colocá en la raíz del repositorio:

- `recolector_modo_AUTOMATICO.py`
- `requirements_ahorraaca.txt`

Y colocá el workflow en:

- `.github/workflows/recolector_ahorraaca.yml`

## Secrets de GitHub

En GitHub abrí:

`Settings > Secrets and variables > Actions > New repository secret`

Creá exactamente estos dos secretos:

- `SUPABASE_URL` = URL del proyecto Supabase.
- `SUPABASE_SECRET_KEY` = Secret key del proyecto Supabase.

No escribas la Secret Key dentro del código y no la subas al repositorio.

## Qué hace cada ejecución

1. Consulta MODO y procesa las promociones.
2. Guarda/actualiza `promociones_detectadas`.
3. Ejecuta `validar_promociones_detectadas()` por RPC.
4. Ejecuta `publicar_promociones_revisadas()` por RPC.
5. Finaliza con error visible en GitHub si falla la validación o publicación.

## Horarios

Está programado dos veces por día:

- 07:00 Argentina
- 15:00 Argentina

GitHub cron trabaja en UTC, por eso el workflow usa `10:00` y `18:00` UTC.

Además se puede probar inmediatamente desde:

`Actions > AhorraAca - Actualizar promociones > Run workflow`

## Primera prueba

Después de guardar los archivos y los dos Secrets:

1. Entrá en `Actions`.
2. Elegí `AhorraAca - Actualizar promociones`.
3. Tocá `Run workflow`.
4. Abrí la ejecución y mirá el paso `Recolectar, validar y publicar`.
5. Si termina verde, abrí AhorraAcá en el teléfono y repetí una búsqueda.

