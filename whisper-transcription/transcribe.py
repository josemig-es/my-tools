"""
Motor de transcripción: descarga el audio de un vídeo (.m3u8 o URL) o usa un
archivo local, lo transcribe en local con faster-whisper (CPU, int8) y genera
un .md con front matter YAML, una nota para la IA y el texto en párrafos.

Lo usa la interfaz (app.py). También funciona desde la línea de comandos:
    python transcribe.py "URL_M3U8_O_ARCHIVO" --title "Título" [--event-date 2026-10-15]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

# --- Configuración fija ---------------------------------------------------------
WORK_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "whisper-transcription"
AUDIO_DIR = WORK_DIR / "audio"                      # fuera de OneDrive
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
SOURCE = "Oracle Cloud Customer Connect"
LANGUAGE = "en"                                     # todas las transcripciones son en inglés
MODELS = ["base", "small", "medium", "large-v3-turbo"]
DEFAULT_MODEL = "small"
PARAGRAPH_SECONDS = 60      # cierra párrafo al pasar de 60 s y terminar una frase
PARAGRAPH_MAX_SECONDS = 90  # o, como máximo, a los 90 s

AI_NOTE = (
    "> **Note for the AI analyzing this file:** This is an automatic speech-to-text\n"
    "> transcript (Whisper `{model}`, English) of a recorded Oracle webinar. Expect\n"
    "> errors: misheard words, wrong or split acronyms and product names (e.g. FCCS,\n"
    "> PBCS, EPM Planning, Smart View, Data Integration), misspelled speaker names,\n"
    "> and passages degraded by accents or audio quality. Infer the intended meaning\n"
    "> from the Oracle EPM context, flag ambiguous passages instead of guessing\n"
    "> silently, and do not treat any sentence as a verbatim quote. Timestamps are\n"
    "> approximate."
)

# Palabras que deben ir en mayúsculas o minúsculas al generar el título desde la URL
ACRONYMS = {
    "epm", "fccs", "pbcs", "epbcs", "arcs", "trcs", "edm", "edmcs", "pcmcs", "erp",
    "hcm", "scm", "ai", "api", "rest", "kpi", "ifrs", "gaap", "sql", "ui", "ux",
    "faq", "ii", "iii", "iv",
}
SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with", "vs"}

Log = Callable[[str], None]
Progress = Callable[[str, float], None]   # (fase, 0..1); fracción < 0 = indeterminado


def _noop_log(msg: str) -> None:
    print(msg)


def _noop_progress(stage: str, frac: float) -> None:
    pass


# --- Utilidades -------------------------------------------------------------------
def fmt(seconds: float) -> str:
    """Segundos -> HH:MM:SS."""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def title_from_page_url(url: str) -> str:
    """Propone un título a partir del enlace de la página de Customer Connect.
    Ej.: .../events/569551-epm-application-design-for-epm-planning-part-i
         -> 'EPM Application Design for EPM Planning Part I'"""
    path = urlparse(url.strip()).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = re.sub(r"^\d+-", "", slug)               # quita el id numérico inicial
    words = [w for w in re.split(r"[-_]+", slug) if w]
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw == "i" or lw in ACRONYMS:
            out.append(lw.upper())
        elif i > 0 and lw in SMALL_WORDS:
            out.append(lw)
        else:
            out.append(lw.capitalize())
    return " ".join(out)


def is_url(source: str) -> bool:
    return source.strip().lower().startswith(("http://", "https://"))


def audio_key(url: str) -> str:
    """Identificador estable del vídeo para reutilizar el audio ya descargado.
    En los enlaces de Kaltura/Akamai es el entryId (no cambia aunque caduque el token)."""
    m = re.search(r"/entryId/([^/?]+)", url)
    if m:
        return re.sub(r"[^\w-]", "_", m.group(1))
    base = url.split("?", 1)[0]
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def find_cached_audio(key: str) -> Path | None:
    for p in sorted(AUDIO_DIR.glob(f"{key}.*")):
        if p.suffix.lower() not in {".part", ".ytdl", ".tmp"} and p.stat().st_size > 0:
            return p
    return None


def safe_filename(title: str, max_len: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:max_len] or "transcript"


def output_path(title: str, event_date: str, out_dir: Path = OUTPUT_DIR) -> Path:
    stem = safe_filename(title)
    if event_date:
        stem = f"{event_date}_{stem}"
    path = out_dir / f"{stem}.md"
    n = 2
    while path.exists():
        path = out_dir / f"{stem} ({n}).md"
        n += 1
    return path


def _yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def front_matter(title: str, event_date: str, model: str, duration: float) -> str:
    return (
        "---\n"
        f"title: {_yaml_str(title)}\n"
        f"source: {SOURCE}\n"
        f"event_date: {event_date}\n"
        f"language: {LANGUAGE}\n"
        f"transcribed_on: {date.today().isoformat()}\n"
        f"model: {model}\n"
        f"duration: {fmt(duration)}\n"
        "---\n"
    )


# --- Fases ------------------------------------------------------------------------
class _YdlLogger:
    """Envía a la interfaz solo avisos y errores de yt-dlp."""
    def __init__(self, log: Log):
        self.log = log

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass  # avisos de formato HLS/MP4 inofensivos

    def error(self, msg: str) -> None:
        self.log(msg)


def download_audio(url: str, log: Log = _noop_log, progress: Progress = _noop_progress) -> Path:
    """Descarga el audio (o reutiliza el ya descargado) en AUDIO_DIR."""
    import yt_dlp

    key = audio_key(url)
    cached = find_cached_audio(key)
    if cached:
        log(f"Audio ya descargado, se reutiliza: {cached.name}")
        return cached

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        if d.get("fragment_count"):
            progress("download", (d.get("fragment_index") or 0) / d["fragment_count"])
        elif d.get("total_bytes") or d.get("total_bytes_estimate"):
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            progress("download", (d.get("downloaded_bytes") or 0) / total)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(AUDIO_DIR / f"{key}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YdlLogger(log),
        "progress_hooks": [hook],
    }
    log("Descargando audio...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloads = info.get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") \
            else Path(ydl.prepare_filename(info))
    log(f"Audio descargado: {path.name} ({path.stat().st_size / 1_048_576:.0f} MB)")
    return path


def write_markdown(segments: Iterable, duration: float, out_md: Path, title: str,
                   event_date: str, model: str, progress: Progress = _noop_progress) -> int:
    """Escribe el .md: front matter + nota para la IA + párrafos con marca de tiempo.
    Escribe cada párrafo según se genera, así que si se interrumpe queda lo hecho."""
    out_md.parent.mkdir(parents=True, exist_ok=True)
    paragraphs = 0
    with open(out_md, "w", encoding="utf-8", newline="\n") as f:
        f.write(front_matter(title, event_date, model, duration))
        f.write("\n" + AI_NOTE.format(model=model) + "\n\n## Transcript\n\n")
        f.flush()
        buf: list[str] = []
        start: float | None = None
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            if start is None:
                start = seg.start
            buf.append(text)
            length = seg.end - start
            if (length >= PARAGRAPH_SECONDS and text[-1] in ".?!") or length >= PARAGRAPH_MAX_SECONDS:
                f.write(f"[{fmt(start)}] {' '.join(buf)}\n\n")
                f.flush()
                paragraphs += 1
                buf, start = [], None
            if duration:
                progress("transcribe", min(seg.end / duration, 1.0))
        if buf:
            f.write(f"[{fmt(start)}] {' '.join(buf)}\n\n")
            paragraphs += 1
    return paragraphs


def transcribe_to_md(audio_path: Path, out_md: Path, title: str, event_date: str, model_name: str,
                     log: Log = _noop_log, progress: Progress = _noop_progress) -> Path:
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    threads = os.cpu_count() or 4
    log(f"Cargando modelo {model_name} ({threads} threads)... la primera vez se descarga.")
    progress("model", -1)
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    batched = BatchedInferencePipeline(model=model)

    log("Transcribiendo...")
    progress("transcribe", 0.0)
    segments, info = batched.transcribe(
        str(audio_path),
        language=LANGUAGE,
        beam_size=1,        # greedy decoding: más rápido
        batch_size=8,       # 8 fragmentos de audio en paralelo
        vad_filter=True,    # salta silencios
    )
    log(f"Duración del audio: {fmt(info.duration)}")
    t0 = time.time()
    n = write_markdown(segments, info.duration, out_md, title, event_date, model_name, progress)
    log(f"Transcripción terminada en {fmt(time.time() - t0)} ({n} párrafos).")
    return out_md


def run(source: str, title: str, event_date: str = "", model_name: str = DEFAULT_MODEL,
        log: Log = _noop_log, progress: Progress = _noop_progress) -> Path:
    """Proceso completo. 'source' = enlace .m3u8/URL o ruta a un archivo local."""
    source = source.strip().strip('"')
    if is_url(source):
        audio = download_audio(source, log, progress)
    else:
        audio = Path(source)
        if not audio.exists():
            raise FileNotFoundError(f"No existe el archivo: {audio}")
        log(f"Usando archivo local: {audio.name}")
    out_md = output_path(title, event_date)
    transcribe_to_md(audio, out_md, title, event_date, model_name, log, progress)
    log(f"Guardado en: {out_md}")
    return out_md


def main() -> None:
    p = argparse.ArgumentParser(description="Transcripción local a .md con faster-whisper")
    p.add_argument("source", help="Enlace .m3u8/URL o ruta a un archivo de audio/vídeo")
    p.add_argument("--title", required=True)
    p.add_argument("--event-date", default="", help="AAAA-MM-DD (opcional)")
    p.add_argument("--model", default=DEFAULT_MODEL, choices=MODELS)
    a = p.parse_args()
    run(a.source, a.title, a.event_date, a.model)


if __name__ == "__main__":
    main()
