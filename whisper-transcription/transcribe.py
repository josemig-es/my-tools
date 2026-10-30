"""
Motor de transcripción: descarga el audio de un vídeo (.m3u8 o URL) o usa un
archivo local, lo transcribe en local con faster-whisper (CPU, int8) y genera
un .md con front matter YAML (plantilla configurable) y el texto en párrafos.

Vocabulario opcional (initial_prompt): prompts/_common.txt se aplica a todos los
productos y prompts/<producto>.txt solo a ese producto. La carpeta no se versiona.

El .md se guarda en output/<producto>/ o, si existe config.json con 'output_dir',
en <output_dir>/<producto>/.

Lo usa la interfaz (app.py), también para procesar la cola de trabajos (job_queue.py).
También funciona desde la línea de comandos:
    python transcribe.py "URL_M3U8_O_ARCHIVO" --title "Título" --product planning [--event-date 2026-10-15]
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
TOOL_DIR = Path(__file__).resolve().parent          # carpeta de la herramienta
DEFAULT_OUTPUT_DIR = TOOL_DIR / "output"            # destino por defecto
CONFIG_FILE = TOOL_DIR / "config.json"              # configuración local opcional (no se versiona)
FRONT_MATTER_FILE = TOOL_DIR / "front_matter.yaml"  # plantilla local opcional (no se versiona)
PROMPTS_DIR = TOOL_DIR / "prompts"                  # vocabulario para el initial_prompt (no se versiona)
COMMON_PROMPT = "_common"                           # prompts/_common.txt se aplica a todos los productos
PROMPT_MAX_TOKENS = 223                             # límite de Whisper; si se supera, se descarta el principio
TRANSCRIPT_HEADING = "## Transcript"                # todo lo que va debajo es el texto transcrito
# Subcarpetas de destino. Para añadir un producto, añádelo a esta lista.
PRODUCTS = ["planning", "fccs", "arcs", "tax-reporting", "narrative-reporting",
            "edm", "pcm", "data-integration", "general"]
LANGUAGE = "en"                                     # todas las transcripciones son en inglés
MODELS = ["base", "small", "medium", "large-v3-turbo"]
DEFAULT_MODEL = "small"
PARAGRAPH_SECONDS = 60      # cierra párrafo al pasar de 60 s y terminar una frase
PARAGRAPH_MAX_SECONDS = 90  # o, como máximo, a los 90 s

# Plantilla por defecto del front matter. Cada {marcador} se sustituye por su valor.
# Se puede sustituir por un front_matter.yaml propio (ver front_matter.example.yaml).
PLACEHOLDERS = ("title", "source", "product", "event_date", "language", "transcribed_on", "model", "duration")
DEFAULT_FRONT_MATTER = """---
title: {title}
source: {source}
product: {product}
event_date: {event_date}
language: {language}
transcribed_on: {transcribed_on}
model: {model}
duration: {duration}
---
"""


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
        name = p.name.lower()   # descarga a medias: .part, .part-Frag3, .ytdl, .temp.mp4...
        if not any(t in name for t in (".part", ".ytdl", ".tmp", ".temp")) and p.stat().st_size > 0:
            return p
    return None


def safe_filename(title: str, max_len: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:max_len] or "transcript"


def load_config() -> dict:
    """Lee config.json si existe; si no, devuelve {}."""
    if not CONFIG_FILE.is_file():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))  # -sig: tolera el BOM del Bloc de notas
    except json.JSONDecodeError as e:
        raise ValueError(f"config.json no es un JSON válido: {e}") from e


def output_root() -> Path:
    """Carpeta raíz de destino: 'output_dir' de config.json si está definido; si no, output/."""
    custom = str(load_config().get("output_dir", "")).strip()
    if not custom:
        return DEFAULT_OUTPUT_DIR
    path = Path(os.path.expandvars(custom)).expanduser()
    return path if path.is_absolute() else TOOL_DIR / path   # relativa = desde la carpeta de la herramienta


def output_dir(product: str) -> Path:
    """Carpeta de destino de un producto, p. ej. <destino>\\planning."""
    if product not in PRODUCTS:
        raise ValueError(f"Producto no válido: {product}. Opciones: {', '.join(PRODUCTS)}")
    return output_root() / product


def output_path(title: str, event_date: str, out_dir: Path) -> Path:
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


def front_matter_template() -> str:
    """Plantilla del front matter: front_matter.yaml si existe; si no, la de por defecto."""
    if FRONT_MATTER_FILE.is_file():
        return FRONT_MATTER_FILE.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    return DEFAULT_FRONT_MATTER


def render_front_matter(values: dict[str, str]) -> str:
    """Rellena la plantilla. Un marcador sin valor queda vacío; título y enlace van entre comillas."""
    text = front_matter_template()
    for key in PLACEHOLDERS:
        value = values.get(key, "")
        if key in ("title", "source"):
            value = _yaml_str(value) if value else ""
        text = text.replace("{" + key + "}", value)
    return text.rstrip("\n") + "\n"


def parse_front_matter(text: str) -> dict[str, str]:
    """Lee los campos 'clave: valor' del front matter de un .md existente."""
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 3)
    if end == -1:
        return {}
    values = {}
    for line in text[4:end].split("\n"):
        key, sep, value = line.partition(":")
        if not sep or line[:1] in (" ", "\t", "#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':   # quita las comillas del título
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        values[key.strip()] = value
    return values


def update_existing(log: Log = _noop_log) -> tuple[int, int, int]:
    """Reescribe el front matter de todos los .md de las subcarpetas de producto con la
    plantilla actual. No toca nada desde '## Transcript' hacia abajo.
    Devuelve (actualizados, sin cambios, omitidos)."""
    root = output_root()
    updated = unchanged = skipped = 0
    for product in PRODUCTS:
        for md in sorted((root / product).glob("*.md")):
            text = md.read_text(encoding="utf-8").replace("\r\n", "\n")
            i = text.find("\n" + TRANSCRIPT_HEADING + "\n")
            if i == -1:
                log(f"Omitido (no contiene '{TRANSCRIPT_HEADING}'): {product}/{md.name}")
                skipped += 1
                continue
            new = render_front_matter(parse_front_matter(text)) + text[i:]
            if new == text:
                unchanged += 1
            else:
                md.write_text(new, encoding="utf-8", newline="\n")
                updated += 1
                log(f"Actualizado: {product}/{md.name}")
    return updated, unchanged, skipped


# --- Initial prompt (vocabulario) -------------------------------------------------
PROMPT_TEMPLATE = """# Vocabulary for the Whisper initial_prompt ({scope}).
# Lines starting with "# " are comments. "#MISSING" is not a comment (no space after #).
# Write terms separated by commas, or one per line. Common + product must stay under {limit} tokens.
"""


def prompt_file(name: str) -> Path:
    """Ruta del archivo de vocabulario: prompts/_common.txt o prompts/<producto>.txt."""
    return PROMPTS_DIR / f"{name}.txt"


def ensure_prompt_file(name: str) -> Path:
    """Crea el archivo de vocabulario con una cabecera de ayuda si no existe; devuelve su ruta."""
    path = prompt_file(name)
    if not path.exists():
        scope = "all products" if name == COMMON_PROMPT else f"product: {name}"
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(PROMPT_TEMPLATE.format(scope=scope, limit=PROMPT_MAX_TOKENS),
                        encoding="utf-8", newline="\n")
    return path


def read_prompt_file(path: Path) -> list[str]:
    """Líneas útiles de un archivo de vocabulario: sin vacías ni comentarios ('# ' al inicio)."""
    lines = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():   # -sig: tolera el BOM del Bloc de notas
        s = line.strip()
        if s and s != "#" and not s.startswith("# "):
            lines.append(s)
    return lines


def build_initial_prompt(product: str) -> tuple[str, list[Path]]:
    """Une el vocabulario común y el del producto (en ese orden) en un único texto.
    Cada línea se une a la anterior con ', ', o con un espacio si la anterior termina en puntuación.
    Devuelve (texto, archivos usados). Sin archivos, el texto es ''."""
    names = [COMMON_PROMPT] + ([product] if product else [])
    files = [prompt_file(n) for n in names if prompt_file(n).is_file()]
    text = ""
    for path in files:
        for line in read_prompt_file(path):
            if text:
                text += " " if text[-1] in ".,;:!?" else ", "
            text += line
    return text, files


_tokenizer = None


def count_prompt_tokens(text: str) -> int | None:
    """Cuenta los tokens con el tokenizador de Whisper (el mismo en todos los modelos de MODELS).
    Lo lee de un modelo ya descargado; si aún no hay ninguno, devuelve None."""
    global _tokenizer
    if _tokenizer is None:
        try:
            from huggingface_hub import try_to_load_from_cache
            from tokenizers import Tokenizer
        except ImportError:
            return None
        for repo in ("Systran/faster-whisper-small", "Systran/faster-whisper-base",
                     "Systran/faster-whisper-medium", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"):
            path = try_to_load_from_cache(repo, "tokenizer.json")
            if isinstance(path, str):
                _tokenizer = Tokenizer.from_file(path)
                break
        else:
            return None
    return len(_tokenizer.encode(text, add_special_tokens=False).ids)


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


def download_audio(url: str, log: Log = _noop_log, progress: Progress = _noop_progress,
                   referer: str = "") -> Path:
    """Descarga el audio (o reutiliza el ya descargado) en AUDIO_DIR.
    'referer' = página del vídeo. Algunos reproductores solo sirven el vídeo a peticiones que
    vienen de su página (cabeceras Referer/Origin); sin ella responden 404 o 403."""
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
    if referer:
        page = urlparse(referer)
        opts["http_headers"] = {"Referer": referer, "Origin": f"{page.scheme}://{page.netloc}"}
    log("Descargando audio...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloads = info.get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") \
            else Path(ydl.prepare_filename(info))
    log(f"Audio descargado: {path.name} ({path.stat().st_size / 1_048_576:.0f} MB)")
    return path


def write_markdown(segments: Iterable, duration: float, out_md: Path, title: str,
                   event_date: str, product: str, model: str, page_url: str = "",
                   progress: Progress = _noop_progress) -> int:
    """Escribe el .md: front matter + párrafos con marca de tiempo.
    Escribe cada párrafo según se genera, así que si se interrumpe queda lo hecho."""
    out_md.parent.mkdir(parents=True, exist_ok=True)
    paragraphs = 0
    with open(out_md, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_front_matter({
            "title": title, "source": page_url, "product": product, "event_date": event_date,
            "language": LANGUAGE, "transcribed_on": date.today().isoformat(),
            "model": model, "duration": fmt(duration),
        }))
        f.write("\n" + TRANSCRIPT_HEADING + "\n\n")
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


def transcribe_to_md(audio_path: Path, out_md: Path, title: str, event_date: str, product: str,
                     model_name: str, page_url: str = "", log: Log = _noop_log,
                     progress: Progress = _noop_progress, initial_prompt: str = "") -> Path:
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    threads = os.cpu_count() or 4
    log(f"Cargando modelo {model_name} ({threads} threads)... la primera vez se descarga.")
    progress("model", -1)
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    batched = BatchedInferencePipeline(model=model)

    if initial_prompt:
        n_tokens = len(model.hf_tokenizer.encode(initial_prompt, add_special_tokens=False).ids)
        log(f"Initial prompt: {n_tokens}/{PROMPT_MAX_TOKENS} tokens.")
        if n_tokens > PROMPT_MAX_TOKENS:
            log(f"AVISO: el initial prompt supera el límite; Whisper descarta sus "
                f"{n_tokens - PROMPT_MAX_TOKENS} primeros tokens (el inicio del vocabulario común).")

    log("Transcribiendo...")
    progress("transcribe", 0.0)
    segments, info = batched.transcribe(
        str(audio_path),
        language=LANGUAGE,
        beam_size=1,        # greedy decoding: más rápido
        batch_size=8,       # 8 fragmentos de audio en paralelo
        vad_filter=True,    # salta silencios
        initial_prompt=initial_prompt or None,   # vocabulario; en modo batched se aplica a cada fragmento
    )
    log(f"Duración del audio: {fmt(info.duration)}")
    t0 = time.time()
    n = write_markdown(segments, info.duration, out_md, title, event_date, product, model_name,
                       page_url, progress)
    log(f"Transcripción terminada en {fmt(time.time() - t0)} ({n} párrafos).")
    return out_md


def run(source: str, title: str, product: str, event_date: str = "",
        model_name: str = DEFAULT_MODEL, page_url: str = "", log: Log = _noop_log,
        progress: Progress = _noop_progress, out_md: Path | None = None) -> Path:
    """Proceso completo. 'source' = enlace .m3u8/URL o ruta a un archivo local.
    'page_url' = enlace de la página del vídeo; se guarda en el campo 'source' del front matter.
    'out_md' = ruta fija del .md (la usa la cola para repetir un trabajo interrumpido sobre el
    mismo archivo); si no se indica, se calcula a partir de la fecha y el título."""
    out_dir = output_dir(product)   # valida el producto antes de descargar nada
    prompt, prompt_used = build_initial_prompt(product)
    source = source.strip().strip('"')
    if is_url(source):
        audio = download_audio(source, log, progress, referer=page_url.strip())
    else:
        audio = Path(source)
        if not audio.exists():
            raise FileNotFoundError(f"No existe el archivo: {audio}")
        log(f"Usando archivo local: {audio.name}")
    if prompt_used:
        log("Vocabulario (initial prompt): " + " + ".join(p.name for p in prompt_used))
    else:
        log("Sin vocabulario (initial prompt): no hay archivos en prompts/.")
    out_md = Path(out_md) if out_md else output_path(title, event_date, out_dir)
    transcribe_to_md(audio, out_md, title, event_date, product, model_name, page_url.strip(), log, progress,
                     initial_prompt=prompt)
    log(f"Guardado en: {out_md}")
    return out_md


def main() -> None:
    p = argparse.ArgumentParser(description="Transcripción local a .md con faster-whisper")
    p.add_argument("source", help="Enlace .m3u8/URL o ruta a un archivo de audio/vídeo")
    p.add_argument("--title", required=True)
    p.add_argument("--product", required=True, choices=PRODUCTS, help="Subcarpeta de destino")
    p.add_argument("--event-date", default="", help="AAAA-MM-DD (opcional)")
    p.add_argument("--model", default=DEFAULT_MODEL, choices=MODELS)
    p.add_argument("--page-url", default="", help="Enlace de la página del vídeo (campo 'source')")
    a = p.parse_args()
    run(a.source, a.title, a.product, a.event_date, a.model, a.page_url)


if __name__ == "__main__":
    main()
