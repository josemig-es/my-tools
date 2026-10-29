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

![How whisper-transcription works](docs/flow.png)

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Usage](#usage)
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

The note for the AI about possible transcription errors is not repeated in each `.md`: it lives
in a single central file, `AI_NOTE.md`, at the root of the destination folder
(see [AI note](#ai-note)).

## Requirements

| Requirement | Details |
|---|---|
| Operating system | Windows |
| Python | 3.12, 3.13 or 3.14, with `tkinter` (official installer from [python.org](https://www.python.org/)) |
| Dependencies | `faster-whisper`, `av<19`, `yt-dlp` (`requirements.txt`); installed automatically by `ejecutar.bat` |

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
| `Enlace .m3u8 o archivo` | Stream link, or a local audio/video file. |
| `Modelo` | `base` (fastest) · `small` (default) · `medium` · `large-v3-turbo` (most accurate, slowest) |

> [!TIP]
> **How to get the `.m3u8` link:** in Chrome, press `F12` › `Network` › filter by `m3u8` ›
> play the video › copy the URL. The link expires; if you get a `403` error, copy a new one.

### Command line

```bash
python transcribe.py "M3U8_URL_OR_FILE" --title "Title" --product planning --event-date 2026-10-15 --model small --page-url "PAGE_URL"
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

### AI note

`AI_NOTE.md` is created at the root of the destination folder the first time you transcribe,
as a copy of `ai_note.example.md` (the general note included in the repo). After that, edit it
directly in the destination; the tool never overwrites it. Share it together with the `.md`
files so the AI takes it into account.

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
├── requirements.txt            # Python dependencies
├── config.example.json         # template for config.json (custom destination)
├── front_matter.example.yaml   # default front matter template
├── ai_note.example.md          # general AI note; initial content of AI_NOTE.md
└── docs/
    └── flow.png                # flow diagram
```

**Local files** (created on your machine, not stored in the repo):

| Path | Content |
|---|---|
| `output\<product>\` | `.md` transcripts (default destination) |
| `config.json` | Custom destination (optional) |
| `front_matter.yaml` | Custom front matter (optional) |
| `<destination>\AI_NOTE.md` | Central AI note |
| `%LOCALAPPDATA%\whisper-transcription\.venv` | Virtual environment |
| `%LOCALAPPDATA%\whisper-transcription\audio` | Downloaded audio (cache) |
| `%LOCALAPPDATA%\whisper-transcription\app.log` | Technical log of the interface |
