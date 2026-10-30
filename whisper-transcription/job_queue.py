"""
Cola de trabajos de transcripción basada en carpetas.

Cada trabajo es un archivo .json dentro de una de estas subcarpetas de la cola:
    pending/     en espera; la interfaz descarga el audio en cuanto aparece
    processing/  transcribiéndose
    done/        terminado; incluye la ruta del .md generado
    failed/      con error; incluye el mensaje

Cualquier programa (un script, otra herramienta, un agente) puede añadir trabajos con
este módulo o escribiendo un .json en pending/. La interfaz (app.py) los procesa en orden
mientras está abierta. Los movimientos entre carpetas son renombrados (os.replace): un
trabajo nunca se lee a medio escribir.

Solo usa la biblioteca estándar, así que funciona también fuera del entorno virtual.

Línea de comandos:
    python job_queue.py add --media-url URL --title "Título" --product planning
                            [--page-url URL] [--event-date AAAA-MM-DD] [--origin NOMBRE] [--model small]
    python job_queue.py add --json trabajo.json          ('-' lee el JSON de la entrada estándar)
    python job_queue.py list [--state pending]
    python job_queue.py has --page-url URL [--output-dir RUTA]
Opción común: --queue-dir RUTA (por defecto, 'queue_dir' de config.json o queue/).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import transcribe as engine

STATES = ("pending", "processing", "done", "failed")
DEFAULT_QUEUE_DIR = engine.TOOL_DIR / "queue"
SOURCES_FILE = engine.TOOL_DIR / "sources.json"     # catálogo local de orígenes (no se versiona)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Campos que acepta un trabajo nuevo. Los demás (id, added_at, output_md, error...) los añade la cola.
JOB_FIELDS = ("media_url", "title", "product", "page_url", "event_date", "origin", "model", "captured_at")
REQUIRED = ("media_url", "title", "product")


class DuplicateJob(ValueError):
    """El vídeo ya está en la cola (pendiente, en curso o terminado)."""


# --- Rutas ------------------------------------------------------------------------
def queue_root(override: str | os.PathLike | None = None) -> Path:
    """Carpeta de la cola: el argumento, 'queue_dir' de config.json o queue/ en la herramienta."""
    custom = str(override or engine.load_config().get("queue_dir", "")).strip()
    if not custom:
        return DEFAULT_QUEUE_DIR
    path = Path(os.path.expandvars(custom)).expanduser()
    return path if path.is_absolute() else engine.TOOL_DIR / path


def ensure_dirs(root: Path) -> None:
    for state in STATES:
        (root / state).mkdir(parents=True, exist_ok=True)


def normalize_url(url: str) -> str:
    """Forma comparable de un enlace: sin espacios, sin '/' final y en minúsculas."""
    return url.strip().rstrip("/").lower()


# --- Lectura y escritura ----------------------------------------------------------
def _write_atomic(path: Path, data: dict) -> None:
    """Escribe en un temporal oculto y lo renombra: quien lea nunca ve un JSON incompleto."""
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def read_job(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def list_jobs(state: str, root: Path | None = None) -> list[tuple[Path, dict]]:
    """Trabajos de un estado, del más antiguo al más reciente (el nombre empieza por la fecha).
    Los archivos ilegibles se ignoran (p. ej., uno que se está moviendo en ese instante)."""
    folder = (root or queue_root()) / state
    jobs = []
    for path in sorted(folder.glob("*.json")):
        try:
            jobs.append((path, read_job(path)))
        except (OSError, ValueError):
            continue
    return jobs


def move(path: Path, state: str, updates: dict | None = None, root: Path | None = None) -> Path:
    """Mueve un trabajo a otro estado (renombrado) y, si se indica, actualiza sus campos.
    Los campos con valor None se eliminan."""
    dest = (root or path.parent.parent) / state / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, dest)
    if updates:
        job = read_job(dest)
        for key, value in updates.items():
            if value is None:
                job.pop(key, None)
            else:
                job[key] = value
        _write_atomic(dest, job)
    return dest


# --- Validación -------------------------------------------------------------------
def validate(job: dict) -> dict:
    """Comprueba un trabajo nuevo y devuelve una copia limpia (sin campos vacíos)."""
    unknown = sorted(set(job) - set(JOB_FIELDS))
    if unknown:
        raise ValueError(f"Campos desconocidos: {', '.join(unknown)}. Válidos: {', '.join(JOB_FIELDS)}")
    clean = {k: str(job.get(k) or "").strip() for k in JOB_FIELDS}
    for key in REQUIRED:
        if not clean[key]:
            raise ValueError(f"Falta el campo obligatorio '{key}'.")
    if clean["product"] not in engine.PRODUCTS:
        raise ValueError(f"Producto no válido: {clean['product']}. Opciones: {', '.join(engine.PRODUCTS)}")
    if clean["model"] and clean["model"] not in engine.MODELS:
        raise ValueError(f"Modelo no válido: {clean['model']}. Opciones: {', '.join(engine.MODELS)}")
    if clean["event_date"]:
        try:
            if not DATE_RE.match(clean["event_date"]):
                raise ValueError
            date.fromisoformat(clean["event_date"])
        except ValueError:
            raise ValueError("event_date debe tener el formato AAAA-MM-DD.") from None
    if clean["page_url"] and not engine.is_url(clean["page_url"]):
        raise ValueError("page_url debe empezar por http:// o https://.")
    sources = load_sources()
    if clean["origin"] and sources and clean["origin"] not in sources.get("sources", {}):
        raise ValueError(f"Origen no definido en sources.json: {clean['origin']}. "
                         f"Opciones: {', '.join(sources.get('sources', {}))}")
    return {k: v for k, v in clean.items() if v}


def load_sources() -> dict:
    """Catálogo de orígenes (sources.json). Lo usan quienes buscan vídeos y llenan la cola;
    aquí solo sirve para comprobar el campo 'origin'. Sin archivo, devuelve {}."""
    if not SOURCES_FILE.is_file():
        return {}
    try:
        return json.loads(SOURCES_FILE.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ValueError(f"sources.json no es un JSON válido: {e}") from e


# --- Duplicados -------------------------------------------------------------------
def find_in_queue(page_url: str, root: Path | None = None,
                  states: tuple[str, ...] = STATES) -> tuple[str, Path] | None:
    """Busca un trabajo con el mismo page_url. Devuelve (estado, ruta) o None."""
    target = normalize_url(page_url)
    for state in states:
        for path, job in list_jobs(state, root):
            if normalize_url(job.get("page_url", "")) == target:
                return state, path
    return None


def find_transcript(page_url: str, output_dir: Path | None = None) -> Path | None:
    """Busca un .md ya generado cuyo campo 'source' sea ese enlace."""
    root = output_dir or engine.output_root()
    target = normalize_url(page_url)
    for md in sorted(root.glob("*/*.md")):
        try:
            head = md.read_text(encoding="utf-8")[:4000].replace("\r\n", "\n")
        except OSError:
            continue
        if normalize_url(engine.parse_front_matter(head).get("source", "")) == target:
            return md
    return None


# --- Alta de trabajos -------------------------------------------------------------
def add(job: dict, root: Path | None = None, force: bool = False) -> Path:
    """Añade un trabajo a pending/ y devuelve su ruta.
    - Si el mismo page_url ya está en curso o terminado, lanza DuplicateJob (con force, lo añade igualmente).
    - Si está pendiente o con error, actualiza ese trabajo (p. ej., con un enlace .m3u8 nuevo porque el
      anterior caducó) y lo deja en pending/, en lugar de crear otro."""
    root = root or queue_root()
    job = validate(job)
    ensure_dirs(root)
    if job.get("page_url"):
        found = find_in_queue(job["page_url"], root, ("processing", "done"))
        if found and not force:
            raise DuplicateJob(f"Ya está en la cola ({found[0]}): {found[1].name}")
        existing = find_in_queue(job["page_url"], root, ("pending", "failed"))
        if existing and not found:
            updates = {**job, "error": None, "failed_at": None,
                       "updated_at": datetime.now().isoformat(timespec="seconds")}
            return move(existing[1], "pending", updates, root)
    now = datetime.now()
    digest = hashlib.sha1((job.get("page_url") or job["media_url"]).encode("utf-8")).hexdigest()[:6]
    job_id = f"{now:%Y%m%d-%H%M%S}-{digest}"
    path = root / "pending" / f"{job_id}.json"
    n = 2
    while any((root / s / path.name).exists() for s in STATES):
        path = root / "pending" / f"{job_id}-{n}.json"
        n += 1
    _write_atomic(path, {"id": path.stem, "added_at": now.isoformat(timespec="seconds"), **job})
    return path


def recover(root: Path | None = None) -> list[Path]:
    """Devuelve a pending/ los trabajos que quedaron en processing/ (la ventana se cerró a medias).
    Conservan 'output_md', así que al repetirse se sobrescribe el .md incompleto."""
    return [move(path, "pending", root=root) for path, _ in list_jobs("processing", root)]


# --- Línea de comandos ------------------------------------------------------------
def _cli() -> int:
    p = argparse.ArgumentParser(description="Cola de trabajos de whisper-transcription")
    p.add_argument("--queue-dir", help="Carpeta de la cola (por defecto, config.json o queue/)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Añade un trabajo a la cola")
    a.add_argument("--json", help="Archivo JSON con el trabajo ('-' = entrada estándar)")
    for field in JOB_FIELDS:
        a.add_argument("--" + field.replace("_", "-"), dest=field)
    a.add_argument("--force", action="store_true", help="Añadir aunque ya esté en la cola")

    ls = sub.add_parser("list", help="Lista los trabajos")
    ls.add_argument("--state", choices=STATES)

    h = sub.add_parser("has", help="¿Ya está en la cola o transcrito? (código de salida 0 = sí, 1 = no)")
    h.add_argument("--page-url", required=True)
    h.add_argument("--output-dir", help="Carpeta raíz de los .md (por defecto, la de config.json)")

    args = p.parse_args()
    root = queue_root(args.queue_dir)

    if args.cmd == "add":
        if args.json:
            raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text(encoding="utf-8-sig")
            job = json.loads(raw)
        else:
            job = {}
        job.update({f: getattr(args, f) for f in JOB_FIELDS if getattr(args, f)})
        try:
            path = add(job, root, args.force)
        except ValueError as e:   # incluye DuplicateJob
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(path)
        return 0

    if args.cmd == "list":
        for state in ([args.state] if args.state else STATES):
            for path, job in list_jobs(state, root):
                print(f"{state:<10} {job.get('event_date', ''):<10} {job.get('product', ''):<12} "
                      f"{job.get('title', '')}" + (f"  [{job['error']}]" if job.get("error") else ""))
        return 0

    if args.cmd == "has":
        found = find_in_queue(args.page_url, root)
        if found:
            print(f"queue:{found[0]} {found[1]}")
            return 0
        out = Path(args.output_dir) if args.output_dir else engine.output_root()
        if not out.is_dir():
            print(f"AVISO: no existe la carpeta de transcripciones {out}; solo se ha revisado la cola.",
                  file=sys.stderr)
        md = find_transcript(args.page_url, out)
        if md:
            print(f"transcript {md}")
            return 0
        print("no")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
