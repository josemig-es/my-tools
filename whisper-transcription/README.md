# whisper-transcription

Transcribe en local vídeos de Oracle Cloud Customer Connect (u otro audio/vídeo) a un `.md` listo para analizar con IA. Funciona en CPU con faster-whisper; no envía el audio a ningún servicio externo.

## Qué hace

1. **Descarga el audio** de un enlace `.m3u8`/URL con `yt-dlp`, o usa un archivo local. El audio descargado se reutiliza si se vuelve a transcribir el mismo vídeo.
2. **Transcribe** con faster-whisper (`int8`, CPU, idioma `en`, VAD para saltar silencios).
3. **Genera un `.md`** en `output/` con:
   - front matter YAML (`title`, `source`, `event_date`, `language`, `transcribed_on`, `model`, `duration`)
   - una nota para la IA sobre los posibles errores de la transcripción
   - el texto en párrafos de ~60–90 s con marca de tiempo `[HH:MM:SS]`

## Requisitos

- Windows
- Python 3.12, 3.13 o 3.14 con `tkinter` (instalador oficial de python.org)
- Dependencias (`requirements.txt`): `faster-whisper`, `av<19`, `yt-dlp`

## Uso

### Interfaz (recomendado)

Doble clic en `ejecutar.bat`. La primera vez crea el entorno virtual e instala las dependencias (tarda unos minutos).

| Campo | Contenido |
|---|---|
| Página de Customer Connect | Enlace de la página del evento; propone el título automáticamente |
| Título | Revisar acrónimos y apóstrofos |
| Fecha del evento | `AAAA-MM-DD` (opcional); se usa como prefijo del nombre del archivo |
| Enlace .m3u8 o archivo | Enlace del stream o archivo local de audio/vídeo |
| Modelo | `base` (rápido) · `small` (por defecto) · `medium` · `large-v3-turbo` |

**Cómo obtener el enlace `.m3u8`:** en Chrome, `F12` › `Network` › filtrar por `m3u8` › reproducir el vídeo › copiar la URL. El enlace caduca; si da error `403`, copiar uno nuevo.

### Línea de comandos

```bash
python transcribe.py "URL_M3U8_O_ARCHIVO" --title "Título" --event-date 2026-10-15 --model small
```

## Archivos

| Archivo | Función |
|---|---|
| `ejecutar.bat` | Localiza Python, crea el venv, instala dependencias y abre la interfaz |
| `app.py` | Interfaz `tkinter`; ejecuta el proceso en segundo plano y muestra el progreso |
| `transcribe.py` | Lógica: descarga, transcripción y escritura del `.md`; también usable por CLI |
| `requirements.txt` | Dependencias de Python |

## Rutas

| Ruta | Contenido |
|---|---|
| `output/` | Transcripciones `.md` (excluido del repo) |
| `%LOCALAPPDATA%\whisper-transcription\.venv` | Entorno virtual |
| `%LOCALAPPDATA%\whisper-transcription\audio` | Audio descargado (caché) |
| `%LOCALAPPDATA%\whisper-transcription\app.log` | Log técnico de la interfaz |
