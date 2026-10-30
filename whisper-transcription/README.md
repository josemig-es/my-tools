# whisper-transcription

**Local video and audio transcription to Markdown, ready to discuss with an AI.**

![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-3776AB)
![Runs locally](https://img.shields.io/badge/runs-locally%20on%20CPU-2E7D32)

This tool was born out of the need to study from videos or audio recordings that
have no subtitles or accessible transcript. When I study something, I usually share
the content with an AI to discuss it while I watch the video, and for that I need
the text.

The tool generates a `.md` transcript from a link to the video or stream (depending
on the website, the link will be of one type or another; for example, `.m3u8`) or
from a local audio/video file. Transcription runs on your machine, on CPU, with
faster-whisper: the audio is not sent to any external service.

It also includes a [job queue](#job-queue): scripts, other tools or AI agents can add
videos to a folder, and the interface transcribes them one after another.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/flow-dark.png">
  <img alt="How whisper-transcription works" src="docs/flow.png">
</picture>

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Usage](#usage)
- [Job queue](#job-queue)
- [Output destination](#output-destination)
- [Customization](#customization)
- [Project structure](#project-structure)

## How it works

### 1. Download the audio

Downloads the audio from a `.m3u8` link or video URL with `yt-dlp`, or uses a local file.
Downloaded audio is cached and reused if the same video is transcribed again.

<details>
<summary><b>FAQ</b></summary>
<br>

**What is `.m3u8`?**  
Many videos on websites are not served as a single file (`.mp4`, `.mp3`...). They are
split into many short fragments that the player requests one by one while you watch.
The `.m3u8` link is the index of those fragments. To download the audio, the tool needs
that stream link; the link of the web page where the video is shown is optional and is
only used to suggest the title and fill the `source` field.

**What is `yt-dlp`?**  
An open-source program ([yt-dlp](https://github.com/yt-dlp/yt-dlp)) specialized in
downloading audio and video from websites. Here it is used to download only the audio.

**Is it necessary to download the audio? Can't it just listen to the link and transcribe?**  
In this first version, the audio has to be downloaded. The file is stored locally in
`%LOCALAPPDATA%\whisper-transcription\audio\`. Future versions plan automatic deletion
and/or transcription without downloading the audio.

</details>

### 2. Transcribe

Transcribes the audio with faster-whisper (`int8`, CPU, language `en`, VAD to skip silences).

<details>
<summary><b>FAQ</b></summary>
<br>

**What is `faster-whisper`?**  
An open-source reimplementation ([faster-whisper](https://github.com/SYSTRAN/faster-whisper))
by SYSTRAN of [Whisper](https://github.com/openai/whisper), OpenAI's speech-to-text model.
It uses the same models with an optimized computation engine, so it runs faster and uses
less memory.

**What do the parameters mean?**  
`int8`: the model is loaded in a compressed numeric format (faster, less memory).
CPU: it runs on the processor, no graphics card needed. `en`: the audio is assumed to be
in English. VAD (Voice Activity Detection): silent parts are skipped. These values are
fixed by default for the best performance; future versions plan to make them
configurable when needed.

**What is the initial prompt?**  
A short text passed to Whisper before each audio chunk. Whisper treats it as preceding
context, so product names, acronyms and jargon written there are more likely to be
transcribed with the right spelling (e.g. `Hybrid BSO` instead of `HybridBSO`). It is
optional and built from your own vocabulary files; see [Vocabulary (initial prompt)](#vocabulary-initial-prompt).

</details>

### 3. Generate the `.md`

Writes a `.md` file to the tool's `output\` folder (the destination is configurable; see
[Output destination](#output-destination)) with:

- **YAML front matter**: `title`, `source` (video page link), `product`, `event_date`,
  `language`, `transcribed_on`, `model`, `duration`. Customizable; see [Customization](#customization).
- **The transcript**, in ~60–90 s paragraphs with `[HH:MM:SS]` timestamps.

```markdown
---
title: "Example Webinar Title"
source: "https://example.com/events/example-webinar"
product: planning
event_date: 2026-10-15
language: en
transcribed_on: 2026-10-29
model: small
duration: 00:59:07
---

## Transcript

[00:00:00] Welcome everyone. Today we are going to talk about...
```

## Requirements

| Requirement | Details |
|---|---|
| Operating system | Windows |
| Python | 3.12, 3.13 or 3.14, with `tkinter` (official installer from [python.org](https://www.python.org/)) |
| Dependencies | `faster-whisper`, `av<19`, `yt-dlp`; installed automatically by `ejecutar.bat` |

### Dependency versions

All dependencies, including sub-dependencies, are pinned to exact versions with SHA-256
hashes in `requirements.txt`:

- `ejecutar.bat` installs them only the first time, or when `requirements.txt` changes.
  Nothing is updated silently on each launch.
- `pip` runs with `--require-hashes` and `--only-binary=:all:`: a package whose content does not
  match its hash is rejected, and no source package is built or executed during installation.

To update the dependencies, edit `requirements.in` (top-level packages) and regenerate the lock
file with [uv](https://github.com/astral-sh/uv):

```bash
uv pip compile requirements.in --universal --python-version 3.12 --generate-hashes -o requirements.txt
```

The next launch of `ejecutar.bat` detects the change and installs the new versions.

## Usage

### Graphical interface (recommended)

Double-click `ejecutar.bat`. The first time, it creates the virtual environment and installs
the dependencies (this takes a few minutes).

> [!NOTE]
> The interface is currently in Spanish. Field names are shown below as they appear on screen.

| Field | Description |
|---|---|
| `Página del vídeo` | Link to the page where the video is shown (optional). Suggests the title and fills the `source` field. |
| `Título` | Title of the transcript. Check acronyms and apostrophes. |
| `Fecha del evento` | `YYYY-MM-DD` (optional). Used as the file name prefix. |
| `Producto` | Destination subfolder (`planning`, `fccs`, `arcs`...). Required. |
| `Vocabulario` | Opens the common or product vocabulary file and shows the prompt size. See [Vocabulary (initial prompt)](#vocabulary-initial-prompt). |
| `Enlace .m3u8 o archivo` | Stream link, or a local audio/video file. |
| `Modelo` | `base` (fastest) · `small` (default) · `medium` · `large-v3-turbo` (most accurate, slowest). Also used by queued jobs that do not set their own model. |
| `Cola de trabajos` | Job queue panel. See [Job queue](#job-queue). |

> [!TIP]
> **How to get the `.m3u8` link:** in Chrome, press `F12` › `Network` › filter by `m3u8` ›
> play the video › copy the URL. The link expires; if you get a `403` error, copy a new one.

### Command line

```bash
python transcribe.py "M3U8_URL_OR_FILE" --title "Title" --product planning --event-date 2026-10-15 --model small --page-url "PAGE_URL"
```

## Job queue

Besides manual transcriptions, the interface processes a **job queue**: a folder where each
video to transcribe is a small `.json` file. Anything that can write a file can add jobs: the
command line, a script, or an AI agent with browser and file access (for example, one that finds
recordings on a website, reads their title and date, captures the `.m3u8` link and queues them).

### How it works

1. A job is added to `queue\pending\`.
2. While the window is open, the audio of each pending job is downloaded **right away**, even if
   another job is being transcribed. Stream links (`.m3u8`) usually expire after a while.
3. Jobs whose audio is ready are transcribed **one at a time, in order of arrival**. A manual
   transcription and a queued one never run at the same time: the second one waits.
4. Finished jobs move to `queue\done\` (with the path of the generated `.md`). Jobs with errors
   move to `queue\failed\` (with the error message).

> [!NOTE]
> If the window is closed, jobs simply wait in `pending\` until it is opened again. If it is
> closed during a queued transcription, that job goes back to `pending\` on the next launch and
> its `.md` is rewritten from the start.

### Queue panel

| Element | Description |
|---|---|
| `Estado` column | `En espera` (waiting) · `Descargando audio` (downloading) · `Audio listo` (audio ready) · `Transcribiendo` (transcribing) · `Error` · `Terminado` (done) |
| `Pausar cola` / `Reanudar cola` | Pause / resume. While paused, no new queued transcription starts (the current one finishes); audio downloads continue. |
| `Reintentar` | Moves the selected failed jobs back to pending. |
| `Quitar` | Removes the selected jobs from the queue. Generated `.md` files are kept. |
| `Abrir carpeta cola` | Opens the queue folder. |
| Double-click | On a failed job, shows the error. On a finished job, opens its `.md`. |

> [!TIP]
> A `403` error usually means the stream link expired before the audio was downloaded. Add the
> job again with a new link (see below): the failed job is updated and goes back to pending.

### Adding jobs

With `job_queue.py` (standard library only; it also works outside the virtual environment):

```bash
# One job
python job_queue.py add --media-url "M3U8_URL" --title "Title" --product planning \
    --page-url "PAGE_URL" --event-date 2026-10-15

# From a JSON file ("-" reads it from standard input)
python job_queue.py add --json job.json

# List the queue
python job_queue.py list
python job_queue.py list --state failed

# Already queued or transcribed? (exit code 0 = yes, 1 = no)
python job_queue.py has --page-url "PAGE_URL"
```

Duplicates are detected by `page_url`:

| The same `page_url` is... | `add` does |
|---|---|
| Being transcribed or done | Rejects it (exit code `2`). `--force` adds it anyway. |
| Pending or failed | Updates that job with the new data (e.g. a new link) and leaves it pending. |
| Already in a transcript (`source` field of a `.md`) | Nothing: `add` does not check transcripts. Use `has` first. |

### Job format

```json
{
  "media_url": "https://example.com/stream/index.m3u8",
  "title": "Example Webinar Title",
  "product": "planning",
  "page_url": "https://example.com/events/example-webinar",
  "event_date": "2026-10-15",
  "origin": "example_webinars",
  "model": "small",
  "captured_at": "2026-10-30T10:00:00+01:00"
}
```

| Field | Required | Description |
|---|---|---|
| `media_url` | Yes | `.m3u8` link, video URL supported by `yt-dlp`, or path to a local file |
| `title` | Yes | Title of the transcript |
| `product` | Yes | Destination subfolder (one of `PRODUCTS` in `transcribe.py`) |
| `page_url` | No | Video page link. Fills the `source` field, is used to detect duplicates and is sent as `Referer` when downloading (some players only serve the stream to requests coming from their page). |
| `event_date` | No | `YYYY-MM-DD` |
| `origin` | No | Name of a source in `sources.json` (see below) |
| `model` | No | Whisper model. If empty, the one selected in the interface is used. |
| `captured_at` | No | When the link was captured (informative) |

The queue adds its own fields (`id`, `added_at`, `output_md`, `error`...). Unknown fields are rejected.

> [!IMPORTANT]
> To write job files directly instead of using `job_queue.py`, write them under a name that does
> not end in `.json` and then rename them. This way the interface never reads a half-written file.

### Sources catalog (`sources.json`)

An optional catalog of video sources, meant for whoever searches for videos and fills the queue
(a person, a script or an AI agent): where to search, how to get the stream link and which videos
to skip. The transcription itself does not use it; the queue only checks that the `origin` of a
job exists in it.

1. Copy `sources.example.json` as `sources.json`, in the same folder.
2. Define one entry per source and set `default`.

| Field | Description |
|---|---|
| `default` | Source used when none is specified |
| `label` | Readable name |
| `search_url` | Where to search. `{query}` is replaced by the search terms. |
| `requires_login` | The search must be done in a browser session that is already signed in |
| `capture` | `m3u8`: capture the stream link while the video plays · `page_url`: the page link works directly with `yt-dlp` |
| `skip_if_captions` | Skip videos that already have subtitles |
| `skip_hosts` | Skip videos hosted on these domains |
| `instructions` | Free-text notes for whoever uses the source |

`sources.json` is excluded from the repo, so each user keeps their own sources.

> [!CAUTION]
> Only queue content you are allowed to access, and check each website's terms of use before
> automating access to it or downloading from it.

### Queue location

By default, the queue is the `queue\` folder inside the tool folder (created automatically and
excluded from the repo). To use another folder, set `queue_dir` in `config.json`:

```json
{
  "output_dir": "D:\\Notes\\transcripts",
  "queue_dir": "D:\\Notes\\transcription-queue"
}
```

## Output destination

By default, the `.md` is saved to `output\<product>\`, inside the tool folder. The `output\`
folder is created automatically and is excluded from the repo.

To save it somewhere else:

1. Copy `config.example.json` as `config.json`, in the same folder.
2. Set `output_dir` to the destination path. In JSON, backslashes must be doubled (`\\`).

```json
{
  "output_dir": "D:\\Notes\\transcripts"
}
```

- `config.json` is excluded from the repo (`.gitignore`), so each user keeps their own.
- A relative path is resolved from the tool folder.
- To add a product (subfolder), add it to the `PRODUCTS` list in `transcribe.py`.
- The interface shows the active destination path below the `Producto` field.

## Customization

### Front matter

1. Copy `front_matter.example.yaml` as `front_matter.yaml`, in the same folder.
2. Add, remove or reorder lines. Placeholders in braces are replaced by their value:

| Placeholder | Value |
|---|---|
| `{title}` | Title (quotes are added automatically) |
| `{source}` | Link to the video page (quotes are added automatically) |
| `{product}` | Selected subfolder |
| `{event_date}` | Event date |
| `{language}` | Language (`en`) |
| `{transcribed_on}` | Transcription date |
| `{model}` | Whisper model |
| `{duration}` | Audio duration |

Lines without a placeholder (e.g. `author: Jane Doe`) are copied as they are.
`front_matter.yaml` is excluded from the repo.

### Vocabulary (initial prompt)

The vocabulary is read from plain text files in the `prompts\` folder:

| File | Applied to |
|---|---|
| `prompts\_common.txt` | Every transcription |
| `prompts\<product>.txt` (e.g. `planning.txt`) | Only transcriptions saved to that product subfolder |

Both are joined (common first, then product) into a single `initial_prompt`. To get started:

1. Copy the `prompts.example` folder as `prompts`, in the same folder, and replace the example terms.
2. Or use the `Editar común` / `Editar producto` buttons in the interface: they open the file
   (creating it if it does not exist) for the selected product.

Format:

- Write terms separated by commas, as sentences, or one per line. Lines are joined with `, `
  (or a space, if the previous line ends with punctuation).
- Lines starting with `# ` are comments. `#MISSING` is not a comment (no space after `#`).
- A product without its own file uses only `_common.txt`. Without any file, no prompt is used.

> [!IMPORTANT]
> Whisper accepts up to **223 tokens** of prompt (roughly 600–800 characters of technical
> terms). If common + product exceed it, Whisper discards the **beginning** of the text.
> The interface shows the current size next to the buttons (`N/223 tokens`, once a model has
> been downloaded) and the log reports it at the start of each transcription.

- In batched mode the prompt is applied to every audio chunk, not only to the first 30 seconds.
- Prefer real terms that appear in the videos: unrelated text in the prompt can bias the transcript.
- `prompts\` is excluded from the repo, so each user keeps their own vocabulary. Changes apply
  to new transcriptions only.

### Update existing `.md` files

The `Actualizar .md existentes` button rewrites the front matter of every `.md` in the product
subfolders with the current template. Nothing from `## Transcript` downwards is changed.

- A new field with a placeholder is left empty in files that did not have it.
- Files without a `## Transcript` heading are skipped.

> [!WARNING]
> A field removed from the template is deleted from the files. If you add it back later,
> older files will have it empty.

## Project structure

```
whisper-transcription/
├── ejecutar.bat                # finds Python, creates the venv, installs dependencies, opens the UI
├── app.py                      # tkinter UI; runs the process in the background and shows progress
├── transcribe.py               # core logic: download, transcription and .md writing; also a CLI
├── job_queue.py                # job queue: add, list and check jobs; also a CLI
├── requirements.in             # top-level dependencies
├── requirements.txt            # pinned dependencies with hashes (generated from requirements.in)
├── config.example.json         # template for config.json (custom destination)
├── sources.example.json        # template for sources.json (sources catalog)
├── front_matter.example.yaml   # default front matter template
├── prompts.example/            # example vocabulary for the initial prompt
│   ├── _common.txt             # applied to every product
│   └── planning.txt            # applied only to the "planning" product
└── docs/
    ├── flow.png                # flow diagram (light theme)
    └── flow-dark.png           # flow diagram (dark theme)
```

**Local files** (created on your machine, not stored in the repo):

| Path | Content |
|---|---|
| `output\<product>\` | `.md` transcripts (default destination) |
| `config.json` | Custom destination and queue folder (optional) |
| `front_matter.yaml` | Custom front matter (optional) |
| `prompts\` | Vocabulary for the initial prompt (optional) |
| `sources.json` | Sources catalog (optional) |
| `queue\` | Job queue: `pending\`, `processing\`, `done\`, `failed\` |
| `%LOCALAPPDATA%\whisper-transcription\.venv` | Virtual environment |
| `%LOCALAPPDATA%\whisper-transcription\audio` | Downloaded audio (cache) |
| `%LOCALAPPDATA%\whisper-transcription\app.log` | Technical log of the interface |
