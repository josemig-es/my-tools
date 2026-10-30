"""
Interfaz para transcribir vídeos a .md. Se abre con ejecutar.bat.
La lógica de descarga y transcripción está en transcribe.py.

Mientras está abierta, procesa también la cola de trabajos (job_queue.py):
- un hilo descarga el audio de cada trabajo pendiente en cuanto aparece (los enlaces .m3u8 caducan);
- otro hilo transcribe los trabajos con el audio listo, de uno en uno y por orden de llegada.
Una transcripción manual y una de la cola nunca se ejecutan a la vez (engine_lock).
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import traceback
from datetime import date, datetime
from pathlib import Path

WORK_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "whisper-transcription"
WORK_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = WORK_DIR / "app.log"

# pythonw no tiene consola: la salida técnica (errores, avisos) va a app.log
if sys.stdout is None or sys.stderr is None:
    _logf = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _logf
    sys.stderr = sys.stderr or _logf
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import job_queue as jq
import transcribe as engine

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DONE_SHOWN = 20             # trabajos terminados que se muestran en la lista
QUEUE_REFRESH_MS = 3000     # cada cuánto se relee la carpeta de la cola
STAGES = {"download": "Descargando audio", "model": "Cargando modelo", "transcribe": "Transcribiendo"}
MEDIA_TYPES = [("Audio o vídeo", "*.mp4 *.m4a *.mp3 *.wav *.webm *.ts *.mkv *.ogg"), ("Todos", "*.*")]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Transcriptor de vídeos")
        self.minsize(760, 760)

        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.result: Path | None = None
        self.t_start = 0.0
        self.stage = ""
        self.frac = 0.0
        self._autotitle = ""   # último título propuesto automáticamente

        self.page_url = tk.StringVar()
        self.title_var = tk.StringVar()
        self.date_var = tk.StringVar()
        self.source_var = tk.StringVar()
        self.product_var = tk.StringVar()
        self.model_var = tk.StringVar(value=engine.DEFAULT_MODEL)
        self.status_var = tk.StringVar(value="Rellena los campos y pulsa Transcribir.")
        self.prompt_var = tk.StringVar()

        # --- Cola de trabajos ---
        self.engine_lock = threading.Lock()      # una sola transcripción a la vez (manual o de la cola)
        self.queue_lock = threading.Lock()       # movimientos de archivos de la cola
        self.queue_paused = threading.Event()    # activado = no se empiezan transcripciones de la cola
        self.downloading: set[str] = set()       # audios que se están descargando (audio_key)
        self.active = ""                         # qué usa la barra de progreso: "", "manual" o "cola"
        self.active_title = ""
        self._model = engine.DEFAULT_MODEL       # copia del modelo elegido, legible desde los hilos
        self._queue_rows: list = []
        self.queue_btn_var = tk.StringVar(value="Pausar cola")
        self.queue_info_var = tk.StringVar()
        try:
            self.qroot: Path | None = jq.queue_root()
            jq.ensure_dirs(self.qroot)
            self.queue_error = ""
        except (ValueError, OSError) as e:   # config.json mal escrito o carpeta inaccesible
            self.qroot = None
            self.queue_error = str(e)

        self._build()
        self.model_var.trace_add("write", lambda *_: setattr(self, "_model", self.model_var.get()))
        self.page_url.trace_add("write", self._on_page_url)
        self.product_var.trace_add("write", lambda *_: self._refresh_prompt())
        # Al volver a la ventana (p. ej. tras editar el vocabulario en el Bloc de notas) se recalcula
        self.bind("<FocusIn>", lambda ev: self._refresh_prompt())
        self._refresh_prompt()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._poll)
        self._start_queue()

    # --- Construcción de la ventana ------------------------------------------------
    def _entry(self, parent: tk.Misc, var: tk.StringVar, width: int = 60) -> ttk.Entry:
        """Campo de texto con menú de clic derecho (Pegar, Copiar...)."""
        e = ttk.Entry(parent, textvariable=var, width=width)
        menu = tk.Menu(e, tearoff=0)
        menu.add_command(label="Pegar", command=lambda: e.event_generate("<<Paste>>"))
        menu.add_command(label="Copiar", command=lambda: e.event_generate("<<Copy>>"))
        menu.add_command(label="Cortar", command=lambda: e.event_generate("<<Cut>>"))
        menu.add_separator()
        menu.add_command(label="Borrar todo", command=lambda: var.set(""))
        e.bind("<Button-3>", lambda ev: menu.tk_popup(ev.x_root, ev.y_root))
        return e

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 5}
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        self.inputs: list[tk.Widget] = []

        r = 0
        ttk.Label(f, text="Página del vídeo").grid(row=r, column=0, sticky="w", **pad)
        e = self._entry(f, self.page_url)
        e.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        self.inputs.append(e)

        r += 1
        ttk.Label(f, text="Título").grid(row=r, column=0, sticky="w", **pad)
        e = self._entry(f, self.title_var)
        e.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        self.inputs.append(e)
        r += 1
        ttk.Label(f, text="Se rellena desde el enlace de la página; revísalo (acrónimos, apóstrofos).",
                  foreground="gray").grid(row=r, column=1, columnspan=2, sticky="w", padx=8)

        r += 1
        ttk.Label(f, text="Fecha del evento").grid(row=r, column=0, sticky="w", **pad)
        df = ttk.Frame(f)
        df.grid(row=r, column=1, columnspan=2, sticky="w", **pad)
        e = self._entry(df, self.date_var, width=14)
        e.pack(side="left")
        self.inputs.append(e)
        ttk.Label(df, text="AAAA-MM-DD (opcional)", foreground="gray").pack(side="left", padx=8)

        r += 1
        ttk.Label(f, text="Producto").grid(row=r, column=0, sticky="w", **pad)
        pf = ttk.Frame(f)
        pf.grid(row=r, column=1, columnspan=2, sticky="w", **pad)
        cb = ttk.Combobox(pf, textvariable=self.product_var, values=engine.PRODUCTS,
                          state="readonly", width=20)
        cb.pack(side="left")
        self.inputs.append(cb)
        ttk.Label(pf, text="Subcarpeta dentro de la carpeta de destino",
                  foreground="gray").pack(side="left", padx=8)
        r += 1
        try:
            dest = f"Destino: {engine.output_root()}"
        except ValueError as e:   # config.json mal escrito
            dest = f"Error en config.json: {e}"
        ttk.Label(f, text=dest,
                  foreground="gray").grid(row=r, column=1, columnspan=2, sticky="w", padx=8)

        r += 1
        ttk.Label(f, text="Vocabulario").grid(row=r, column=0, sticky="w", **pad)
        vf = ttk.Frame(f)
        vf.grid(row=r, column=1, columnspan=2, sticky="w", **pad)
        b = ttk.Button(vf, text="Editar común", command=lambda: self._edit_prompt(engine.COMMON_PROMPT))
        b.pack(side="left")
        self.inputs.append(b)
        b = ttk.Button(vf, text="Editar producto", command=lambda: self._edit_prompt(self.product_var.get()))
        b.pack(side="left", padx=8)
        self.inputs.append(b)
        ttk.Label(vf, textvariable=self.prompt_var, foreground="gray").pack(side="left")

        r += 1
        ttk.Label(f, text="Enlace .m3u8 o archivo").grid(row=r, column=0, sticky="w", **pad)
        e = self._entry(f, self.source_var)
        e.grid(row=r, column=1, sticky="ew", **pad)
        self.inputs.append(e)
        b = ttk.Button(f, text="Archivo...", command=self._pick_file)
        b.grid(row=r, column=2, sticky="e", **pad)
        self.inputs.append(b)

        r += 1
        ttk.Label(f, text="Modelo").grid(row=r, column=0, sticky="w", **pad)
        mf = ttk.Frame(f)
        mf.grid(row=r, column=1, columnspan=2, sticky="w", **pad)
        cb = ttk.Combobox(mf, textvariable=self.model_var, values=engine.MODELS, state="readonly", width=16)
        cb.pack(side="left")
        self.inputs.append(cb)
        ttk.Label(mf, text="base: más rápido · medium: más preciso y lento",
                  foreground="gray").pack(side="left", padx=8)

        r += 1
        bf = ttk.Frame(f)
        bf.grid(row=r, column=0, columnspan=3, sticky="w", padx=8, pady=(12, 6))
        self.btn_run = ttk.Button(bf, text="Transcribir", command=self._start)
        self.btn_run.pack(side="left")
        self.btn_open = ttk.Button(bf, text="Abrir resultado", command=self._open_result, state="disabled")
        self.btn_open.pack(side="left", padx=8)
        ttk.Button(bf, text="Abrir carpeta destino", command=self._open_folder).pack(side="left")
        b = ttk.Button(bf, text="Actualizar .md existentes", command=self._update_existing)
        b.pack(side="left", padx=8)
        self.inputs.append(b)

        r += 1
        qf = ttk.LabelFrame(f, text="Cola de trabajos", padding=8)
        qf.grid(row=r, column=0, columnspan=3, sticky="ew", padx=8, pady=(8, 4))
        qf.columnconfigure(0, weight=1)
        cols = (("estado", "Estado", 140, False), ("fecha", "Fecha", 90, False),
                ("producto", "Producto", 110, False), ("titulo", "Título", 320, True))
        self.tree = ttk.Treeview(qf, columns=[c[0] for c in cols], show="headings", height=6,
                                 selectmode="extended")
        for key, text, width, stretch in cols:
            self.tree.heading(key, text=text, anchor="w")
            self.tree.column(key, width=width, stretch=stretch, anchor="w")
        self.tree.grid(row=0, column=0, sticky="ew")
        tsb = ttk.Scrollbar(qf, command=self.tree.yview)
        self.tree["yscrollcommand"] = tsb.set
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._on_tree_double)
        qb = ttk.Frame(qf)
        qb.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(qb, textvariable=self.queue_btn_var, command=self._toggle_queue).pack(side="left")
        ttk.Button(qb, text="Reintentar", command=self._retry_selected).pack(side="left", padx=8)
        ttk.Button(qb, text="Quitar", command=self._remove_selected).pack(side="left")
        ttk.Button(qb, text="Abrir carpeta cola", command=self._open_queue_folder).pack(side="left", padx=8)
        ttk.Label(qb, textvariable=self.queue_info_var, foreground="gray").pack(side="left")

        r += 1
        self.bar =ttk.Progressbar(f, mode="determinate", maximum=100)
        self.bar.grid(row=r, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 2))
        r += 1
        ttk.Label(f, textvariable=self.status_var).grid(row=r, column=0, columnspan=3, sticky="w", padx=8)

        r += 1
        lf = ttk.Frame(f)
        lf.grid(row=r, column=0, columnspan=3, sticky="nsew", padx=8, pady=(8, 0))
        f.rowconfigure(r, weight=1)
        self.log_text = tk.Text(lf, height=12, wrap="word", state="disabled")
        sb = ttk.Scrollbar(lf, command=self.log_text.yview)
        self.log_text["yscrollcommand"] = sb.set
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    # --- Acciones -----------------------------------------------------------------
    def _on_page_url(self, *_args) -> None:
        url = self.page_url.get()
        proposal = engine.title_from_page_url(url) if engine.is_url(url) else ""
        current = self.title_var.get().strip()
        # Solo sobrescribe si el título está vacío o no lo has editado tú
        if proposal and (not current or current == self._autotitle):
            self.title_var.set(proposal)
            self._autotitle = proposal

    def _refresh_prompt(self) -> None:
        """Muestra qué archivos de vocabulario se usarán con el producto elegido y su tamaño."""
        try:
            text, files = engine.build_initial_prompt(self.product_var.get())
            n = engine.count_prompt_tokens(text) if text else 0
        except Exception as e:  # noqa: BLE001
            return self.prompt_var.set(f"Error al leer prompts/: {e}")
        if not files:
            return self.prompt_var.set("Sin vocabulario (initial prompt)")
        names = " + ".join(p.name for p in files)
        if n is None:   # aún no hay ningún modelo descargado para contar tokens
            size = f"{len(text)} caracteres"
        elif n > engine.PROMPT_MAX_TOKENS:
            size = f"{n}/{engine.PROMPT_MAX_TOKENS} tokens · SUPERA EL LÍMITE: se perderá el inicio"
        else:
            size = f"{n}/{engine.PROMPT_MAX_TOKENS} tokens"
        self.prompt_var.set(f"{names} · {size}")

    def _edit_prompt(self, name: str) -> None:
        """Abre (y crea si no existe) el archivo de vocabulario común o el del producto."""
        if not name:
            return self._warn("Elige primero el producto.")
        os.startfile(engine.ensure_prompt_file(name))  # abre con el programa asociado a .txt

    def _pick_file(self) -> None:
        path = filedialog.askopenfilename(title="Elige un archivo de audio o vídeo", filetypes=MEDIA_TYPES)
        if path:
            self.source_var.set(path)

    def _log(self, msg: str) -> None:
        print(msg)  # también a app.log
        self.log_text["state"] = "normal"
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text["state"] = "disabled"

    def _set_inputs(self, enabled: bool) -> None:
        for w in self.inputs:
            if isinstance(w, ttk.Combobox):
                w["state"] = "readonly" if enabled else "disabled"
            else:
                w["state"] = "normal" if enabled else "disabled"
        self.btn_run["state"] = "normal" if enabled else "disabled"

    def _start(self) -> None:
        src = self.source_var.get().strip().strip('"')
        title = self.title_var.get().strip()
        dt = self.date_var.get().strip()
        product = self.product_var.get()
        page = self.page_url.get().strip()
        if page and not engine.is_url(page):
            return self._warn("El enlace de la página debe empezar por http:// o https://.")
        if not src:
            return self._warn("Pega el enlace .m3u8 o elige un archivo.")
        if not engine.is_url(src) and not Path(src).exists():
            return self._warn("El archivo indicado no existe.")
        if not title:
            return self._warn("Escribe un título (o pega el enlace de la página para rellenarlo).")
        if not product:
            return self._warn("Elige el producto (subcarpeta de destino).")
        if dt:
            try:
                if not DATE_RE.match(dt):
                    raise ValueError
                date.fromisoformat(dt)
            except ValueError:
                return self._warn("La fecha debe tener el formato AAAA-MM-DD, por ejemplo 2026-10-15.")

        self._set_inputs(False)
        self.btn_open["state"] = "disabled"
        self.result = None
        self.t_start = time.time()
        self._log(f"--- {title} ---")
        if self.engine_lock.locked():
            self.status_var.set("Esperando a que termine la transcripción de la cola...")
        self.worker = threading.Thread(target=self._work,
                                       args=(src, title, product, dt, self.model_var.get(), page),
                                       daemon=True)
        self.worker.start()

    def _work(self, src: str, title: str, product: str, dt: str, model: str, page: str) -> None:
        """Se ejecuta en segundo plano para que la ventana no se congele."""
        if not self.engine_lock.acquire(blocking=False):
            self.q.put(("log", "Esperando a que termine la transcripción de la cola..."))
            self.engine_lock.acquire()
        try:
            self.q.put(("active", "manual", title))
            path = engine.run(src, title, product, dt, model, page,
                              log=lambda m: self.q.put(("log", m)),
                              progress=lambda s, x: self.q.put(("progress", s, x)))
            self.q.put(("done", path))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.q.put(("error", e))
        finally:
            self.engine_lock.release()

    def _poll(self) -> None:
        """Recoge los mensajes del proceso en segundo plano y actualiza la ventana."""
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self._log(item[1])
                elif item[0] == "progress":
                    self._set_progress(item[1], item[2])
                elif item[0] == "active":
                    self._set_active(item[1], item[2])
                elif item[0] == "done":
                    self._finish(item[1])
                elif item[0] == "error":
                    self._fail(item[1])
                elif item[0] == "qdone":
                    self._queue_finished(item[1], item[2])
                elif item[0] == "qerror":
                    self._queue_failed(item[1])
                elif item[0] == "qrefresh":
                    self._refresh_queue()
        except queue.Empty:
            pass
        if self.active:
            self._update_status()
        self.after(200, self._poll)

    def _set_active(self, kind: str, title: str) -> None:
        """Empieza una transcripción (manual o de la cola): reinicia la barra y el cronómetro."""
        self.active, self.active_title = kind, title
        self.t_start = time.time()
        self.stage, self.frac = "", 0.0
        self.bar.stop()
        self.bar.configure(mode="determinate", value=0)
        if kind == "cola":
            self._log(f"--- [Cola] {title} ---")
            self._refresh_queue()

    def _set_progress(self, stage: str, frac: float) -> None:
        self.stage = stage
        self.frac = frac
        if frac < 0:
            if str(self.bar["mode"]) != "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(15)
        else:
            if str(self.bar["mode"]) != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar["value"] = frac * 100

    def _update_status(self) -> None:
        label = STAGES.get(self.stage, "Preparando")
        pct = f" {self.frac * 100:.0f} %" if self.frac >= 0 and self.stage != "model" else ""
        prefix = ""
        if self.active == "cola":
            t = self.active_title
            prefix = f"[Cola] {t[:57] + '...' if len(t) > 60 else t}  ·  "
        self.status_var.set(f"{prefix}{label}...{pct}  ·  {engine.fmt(time.time() - self.t_start)} transcurrido")

    def _finish(self, path: Path) -> None:
        self.active = ""
        self.bar.stop()
        self.bar.configure(mode="determinate", value=100)
        self.result = path
        self.status_var.set(f"Terminado en {engine.fmt(time.time() - self.t_start)}  ·  {path.name}")
        self._set_inputs(True)
        self.btn_open["state"] = "normal"
        self.bell()

    def _fail(self, err: Exception) -> None:
        self.active = ""
        self.bar.stop()
        self.bar.configure(mode="determinate", value=0)
        self._set_inputs(True)
        text = str(err)
        if "403" in text or "Forbidden" in text:
            msg = ("El enlace .m3u8 ha caducado o no es válido.\n\n"
                   "Copia uno nuevo en Chrome: F12 › Network › filtra por m3u8 › reproduce el vídeo.")
        elif any(s in text for s in ("getaddrinfo", "Failed to resolve", "ConnectionError", "timed out")):
            msg = "No se ha podido conectar. Revisa tu conexión a internet."
        else:
            msg = f"Ha ocurrido un error:\n\n{text[:500]}\n\nDetalle técnico en:\n{LOG_FILE}"
        self.status_var.set("Error. Revisa el mensaje.")
        self._log(text if text.startswith("ERROR") else f"ERROR: {text}")
        messagebox.showerror("Transcriptor de vídeos", msg)

    def _warn(self, msg: str) -> None:
        messagebox.showwarning("Transcriptor de vídeos", msg)

    def _open_result(self) -> None:
        if self.result and self.result.exists():
            os.startfile(self.result)  # abre con el programa asociado a .md

    def _open_folder(self) -> None:
        folder = engine.output_dir(self.product_var.get()) if self.product_var.get() else engine.output_root()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def _update_existing(self) -> None:
        """Aplica la plantilla actual de front matter a todos los .md ya generados."""
        try:
            root = engine.output_root()
        except ValueError as e:
            return self._warn(str(e))
        if not messagebox.askyesno(
                "Transcriptor de vídeos",
                f"Se reescribirá el front matter de todos los .md de:\n{root}\n\n"
                "con la plantilla actual. El texto de la transcripción no se modifica.\n\n¿Continuar?"):
            return
        try:
            updated, unchanged, skipped = engine.update_existing(log=self._log)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return messagebox.showerror("Transcriptor de vídeos", f"Ha ocurrido un error:\n\n{e}")
        msg = f"Actualizados: {updated}  ·  sin cambios: {unchanged}  ·  omitidos: {skipped}"
        self._log(msg)
        self.status_var.set(msg)

    def _on_close(self) -> None:
        if self.engine_lock.locked() or (self.worker and self.worker.is_alive()):
            if not messagebox.askyesno("Transcriptor de vídeos",
                                       "Hay una transcripción en curso. Si cierras, se detiene y el .md "
                                       "quedará incompleto.\n\nSi es de la cola, se repetirá desde el "
                                       "principio la próxima vez que abras la ventana.\n\n"
                                       "¿Cerrar de todos modos?"):
                return
        self.destroy()
        os._exit(0)  # termina también el proceso en segundo plano

    # --- Cola de trabajos -----------------------------------------------------------
    def _qlog(self, msg: str) -> None:
        """Log de los hilos de la cola (se muestra desde el hilo de la ventana)."""
        self.q.put(("log", f"[Cola] {msg}"))

    @staticmethod
    def _job_key(job: dict) -> str:
        src = job.get("media_url", "").strip().strip('"')
        return engine.audio_key(src) if engine.is_url(src) else ""

    def _audio_ready(self, job: dict) -> bool:
        """True si el audio del trabajo ya está en disco (en la caché o como archivo local)."""
        src = job.get("media_url", "").strip().strip('"')
        if not engine.is_url(src):
            return Path(src).exists()
        key = engine.audio_key(src)
        return key not in self.downloading and engine.find_cached_audio(key) is not None

    def _start_queue(self) -> None:
        if self.qroot is None:
            self.queue_info_var.set(f"Cola desactivada: {self.queue_error}")
            return
        with self.queue_lock:
            recovered = jq.recover(self.qroot)
        if recovered:
            self._log(f"[Cola] {len(recovered)} trabajo(s) interrumpido(s) vuelven a pendientes.")
        threading.Thread(target=self._download_loop, daemon=True).start()
        threading.Thread(target=self._transcribe_loop, daemon=True).start()
        self._refresh_queue()
        self.after(QUEUE_REFRESH_MS, self._auto_refresh)

    def _auto_refresh(self) -> None:
        self._refresh_queue()
        self.after(QUEUE_REFRESH_MS, self._auto_refresh)

    def _queue_fail(self, path: Path, err: object) -> None:
        """Pasa un trabajo a failed/ con el mensaje de error. Se llama desde los hilos."""
        text = re.sub(r"^(ERROR:\s*)+", "", str(err))
        if "403" in text or "Forbidden" in text:
            text = "Enlace caducado o no válido (403): hace falta un enlace nuevo. " + text
        with self.queue_lock:
            if path.exists():
                jq.move(path, "failed", {"error": text[:1000],
                                         "failed_at": datetime.now().isoformat(timespec="seconds")},
                        self.qroot)
        self._qlog(f"ERROR: {text[:300]}")
        self.q.put(("qrefresh",))

    def _download_loop(self) -> None:
        """Descarga el audio de cada trabajo pendiente en cuanto aparece, porque los enlaces .m3u8
        caducan. Sigue funcionando con la cola en pausa."""
        while True:
            try:
                for path, job in jq.list_jobs("pending", self.qroot):
                    src = job.get("media_url", "").strip().strip('"')
                    if not engine.is_url(src) or self._audio_ready(job):
                        continue
                    key = engine.audio_key(src)
                    self.downloading.add(key)
                    self.q.put(("qrefresh",))
                    self._qlog(f"Audio de: {job.get('title', path.name)}")
                    try:
                        engine.download_audio(src, log=self._qlog, referer=job.get("page_url", ""))
                    except Exception as e:  # noqa: BLE001
                        traceback.print_exc()
                        self._queue_fail(path, e)
                    finally:
                        self.downloading.discard(key)
                        self.q.put(("qrefresh",))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(5)

    def _claim_next(self) -> tuple[Path, dict] | None:
        """Pasa a processing/ el primer trabajo pendiente con el audio listo y fija la ruta de su .md."""
        with self.queue_lock:
            for path, job in jq.list_jobs("pending", self.qroot):
                src = job.get("media_url", "").strip().strip('"')
                if not engine.is_url(src) and not Path(src).exists():
                    self._queue_fail_locked(path, f"No existe el archivo: {src}")
                    continue
                if not self._audio_ready(job):
                    continue
                try:
                    out_md = job.get("output_md") or str(engine.output_path(
                        job["title"], job.get("event_date", ""), engine.output_dir(job["product"])))
                except (KeyError, ValueError) as e:   # trabajo mal formado escrito a mano
                    self._queue_fail_locked(path, f"Trabajo no válido: {e}")
                    continue
                new = jq.move(path, "processing", {
                    "output_md": out_md, "started_at": datetime.now().isoformat(timespec="seconds")},
                    self.qroot)
                return new, jq.read_job(new)
        return None

    def _queue_fail_locked(self, path: Path, msg: str) -> None:
        """Como _queue_fail, pero con queue_lock ya adquirido."""
        jq.move(path, "failed", {"error": msg, "failed_at": datetime.now().isoformat(timespec="seconds")},
                self.qroot)
        self._qlog(f"ERROR: {msg}")
        self.q.put(("qrefresh",))

    def _transcribe_loop(self) -> None:
        """Transcribe los trabajos con el audio listo, de uno en uno y por orden de llegada."""
        while True:
            time.sleep(3)
            if self.queue_paused.is_set():
                continue
            if not self.engine_lock.acquire(blocking=False):   # hay una transcripción manual en curso
                continue
            claimed = None
            try:
                claimed = self._claim_next()
                if not claimed:
                    continue
                path, job = claimed
                title = job.get("title", path.stem)
                self.q.put(("active", "cola", title))
                md = engine.run(job["media_url"], title, job["product"], job.get("event_date", ""),
                                job.get("model") or self._model, job.get("page_url", ""),
                                log=self._qlog, progress=lambda s, x: self.q.put(("progress", s, x)),
                                out_md=Path(job["output_md"]))
                with self.queue_lock:
                    jq.move(path, "done", {"output_md": str(md),
                                           "finished_at": datetime.now().isoformat(timespec="seconds")},
                            self.qroot)
                self.q.put(("qdone", title, md))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                if claimed:
                    self._queue_fail(claimed[0], e)
                    self.q.put(("qerror", claimed[1].get("title", claimed[0].stem)))
            finally:
                self.engine_lock.release()

    def _queue_finished(self, title: str, path: Path) -> None:
        self.active = ""
        self.bar.stop()
        self.bar.configure(mode="determinate", value=100)
        self.result = path
        if str(self.btn_run["state"]) == "normal":   # no hay una manual esperando
            self.btn_open["state"] = "normal"
        self.status_var.set(f"[Cola] Terminado en {engine.fmt(time.time() - self.t_start)}  ·  {path.name}")
        self._refresh_queue()

    def _queue_failed(self, title: str) -> None:
        self.active = ""
        self.bar.stop()
        self.bar.configure(mode="determinate", value=0)
        self.status_var.set(f"[Cola] Error en: {title}. Doble clic en el trabajo para ver el detalle.")
        self._refresh_queue()

    def _refresh_queue(self) -> None:
        """Relee la carpeta de la cola y actualiza la lista (solo si algo ha cambiado)."""
        if self.qroot is None:
            return
        rows, counts = [], {}
        names = {"processing": "Transcribiendo", "failed": "Error", "done": "Terminado"}
        for state in ("processing", "pending", "failed", "done"):
            jobs = jq.list_jobs(state, self.qroot)
            counts[state] = len(jobs)
            if state == "done":
                jobs = jobs[-DONE_SHOWN:][::-1]   # los últimos, el más reciente arriba
            for path, job in jobs:
                if state == "pending":
                    if self._job_key(job) in self.downloading:
                        label = "Descargando audio"
                    elif self._audio_ready(job):
                        label = "Audio listo"
                    else:
                        label = "En espera"
                else:
                    label = names[state]
                rows.append((f"{state}/{path.name}",
                             (label, job.get("event_date", ""), job.get("product", ""),
                              job.get("title", path.stem))))
        paused = "  ·  EN PAUSA (las descargas continúan)" if self.queue_paused.is_set() else ""
        self.queue_info_var.set(f"Pendientes: {counts['pending']}  ·  errores: {counts['failed']}  ·  "
                                f"terminados: {counts['done']}{paused}")
        if rows == self._queue_rows:
            return
        self._queue_rows = rows
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for iid, values in rows:
            self.tree.insert("", "end", iid=iid, values=values)
        keep = [i for i in selected if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)

    def _selected_jobs(self, allowed: tuple[str, ...]) -> list[Path]:
        paths = []
        for iid in self.tree.selection():
            state, _, name = iid.partition("/")
            if state in allowed:
                paths.append(self.qroot / state / name)
        return paths

    def _toggle_queue(self) -> None:
        if self.queue_paused.is_set():
            self.queue_paused.clear()
            self.queue_btn_var.set("Pausar cola")
            self._log("[Cola] Reanudada.")
        else:
            self.queue_paused.set()
            self.queue_btn_var.set("Reanudar cola")
            self._log("[Cola] En pausa: la transcripción en curso termina; no empieza ninguna nueva. "
                      "Las descargas de audio continúan.")
        self._refresh_queue()

    def _retry_selected(self) -> None:
        if self.qroot is None:
            return
        paths = self._selected_jobs(("failed",))
        if not paths:
            return self._warn("Selecciona uno o más trabajos con estado Error.")
        with self.queue_lock:
            for p in paths:
                if p.exists():
                    jq.move(p, "pending", {"error": None, "failed_at": None,
                                           "requeued_at": datetime.now().isoformat(timespec="seconds")},
                            self.qroot)
        self._log(f"[Cola] {len(paths)} trabajo(s) vuelven a pendientes. Si el error era de enlace "
                  "caducado (403) y el audio no llegó a descargarse, hace falta un enlace nuevo.")
        self._refresh_queue()

    def _remove_selected(self) -> None:
        if self.qroot is None:
            return
        paths = self._selected_jobs(("pending", "failed", "done"))
        if not paths:
            return self._warn("Selecciona uno o más trabajos pendientes, con error o terminados.\n"
                              "El trabajo que se está transcribiendo no se puede quitar.")
        if not messagebox.askyesno("Transcriptor de vídeos",
                                   f"Se quitarán {len(paths)} trabajo(s) de la cola.\n\n"
                                   "Los .md ya generados no se borran.\n\n¿Continuar?"):
            return
        with self.queue_lock:
            for p in paths:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
        self._refresh_queue()

    def _open_queue_folder(self) -> None:
        if self.qroot is None:
            return self._warn(f"Cola desactivada: {self.queue_error}")
        os.startfile(self.qroot)

    def _on_tree_double(self, event: tk.Event) -> None:
        """Doble clic: un trabajo con error muestra el mensaje; uno terminado abre su .md."""
        iid = self.tree.identify_row(event.y)
        if not iid or self.qroot is None:
            return
        state, _, name = iid.partition("/")
        try:
            job = jq.read_job(self.qroot / state / name)
        except (OSError, ValueError):
            return
        if state == "failed":
            messagebox.showerror("Transcriptor de vídeos", f"{job.get('title', name)}\n\n{job.get('error', '')}")
        elif state == "done" and job.get("output_md") and Path(job["output_md"]).exists():
            os.startfile(job["output_md"])


def main() -> None:
    try:  # texto nítido en pantallas con escalado
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
