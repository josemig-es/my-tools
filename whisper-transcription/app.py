"""
Interfaz para transcribir vídeos a .md. Se abre con ejecutar.bat.
La lógica de descarga y transcripción está en transcribe.py.
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import traceback
from datetime import date
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

import transcribe as engine

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STAGES = {"download": "Descargando audio", "model": "Cargando modelo", "transcribe": "Transcribiendo"}
MEDIA_TYPES = [("Audio o vídeo", "*.mp4 *.m4a *.mp3 *.wav *.webm *.ts *.mkv *.ogg"), ("Todos", "*.*")]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Transcriptor de vídeos")
        self.minsize(760, 560)

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
        self.model_var = tk.StringVar(value=engine.DEFAULT_MODEL)
        self.status_var = tk.StringVar(value="Rellena los campos y pulsa Transcribir.")

        self._build()
        self.page_url.trace_add("write", self._on_page_url)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._poll)

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
        ttk.Label(f, text="Página de Customer Connect").grid(row=r, column=0, sticky="w", **pad)
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
        ttk.Button(bf, text="Abrir carpeta output", command=self._open_folder).pack(side="left")

        r += 1
        self.bar = ttk.Progressbar(f, mode="determinate", maximum=100)
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
        if not src:
            return self._warn("Pega el enlace .m3u8 o elige un archivo.")
        if not engine.is_url(src) and not Path(src).exists():
            return self._warn("El archivo indicado no existe.")
        if not title:
            return self._warn("Escribe un título (o pega el enlace de la página para rellenarlo).")
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
        self.stage, self.frac = "", 0.0
        self.bar.stop()
        self.bar.configure(mode="determinate", value=0)
        self.t_start = time.time()
        self._log(f"--- {title} ---")
        self.worker = threading.Thread(target=self._work, args=(src, title, dt, self.model_var.get()), daemon=True)
        self.worker.start()

    def _work(self, src: str, title: str, dt: str, model: str) -> None:
        """Se ejecuta en segundo plano para que la ventana no se congele."""
        try:
            path = engine.run(src, title, dt, model,
                              log=lambda m: self.q.put(("log", m)),
                              progress=lambda s, x: self.q.put(("progress", s, x)))
            self.q.put(("done", path))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.q.put(("error", e))

    def _poll(self) -> None:
        """Recoge los mensajes del proceso en segundo plano y actualiza la ventana."""
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self._log(item[1])
                elif item[0] == "progress":
                    self._set_progress(item[1], item[2])
                elif item[0] == "done":
                    self._finish(item[1])
                elif item[0] == "error":
                    self._fail(item[1])
        except queue.Empty:
            pass
        if self.worker and self.worker.is_alive():
            self._update_status()
        self.after(200, self._poll)

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
        self.status_var.set(f"{label}...{pct}  ·  {engine.fmt(time.time() - self.t_start)} transcurrido")

    def _finish(self, path: Path) -> None:
        self.bar.stop()
        self.bar.configure(mode="determinate", value=100)
        self.result = path
        self.status_var.set(f"Terminado en {engine.fmt(time.time() - self.t_start)}  ·  {path.name}")
        self._set_inputs(True)
        self.btn_open["state"] = "normal"
        self.bell()

    def _fail(self, err: Exception) -> None:
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
        engine.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(engine.OUTPUT_DIR)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Transcriptor de vídeos",
                                       "Hay una transcripción en curso. Si cierras, se detiene y el .md "
                                       "quedará incompleto.\n\n¿Cerrar de todos modos?"):
                return
        self.destroy()
        os._exit(0)  # termina también el proceso en segundo plano


def main() -> None:
    try:  # texto nítido en pantallas con escalado
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
