---
name: analizar-video
description: Analyze a video from a YouTube, TikTok or Instagram URL — transcript, keyframes, summary. Use when the user shares such a link and wants it watched, summarized, transcribed or explained.
---

`scripts/analizar.py` produces the raw material (transcript, timestamped keyframes, metadata). **You write the summary**, from the transcript and by looking at the frames.

## 1. Preflight

`<skill_dir>` is the directory holding this SKILL.md; every command uses its absolute path, never the cwd.

`<python>` is an interpreter with `faster-whisper` and `yt-dlp`. Resolve it in order: `$ANALIZAR_PYTHON`; the venv the user names; this repo's `prototypes/analizar-video/.venv/Scripts/python.exe`. If none works, ask the user which interpreter to use. Say which one you picked.

Run, and report anything missing:

```
yt-dlp --version ; ffmpeg -version ; ffprobe -version ; <python> -c "import faster_whisper"
```

yt-dlp is found via `ANALIZAR_YTDLP`, then `<python> -m yt_dlp`, then PATH, then next to the interpreter.

Missing tool → **ask the user before installing**, proposing: `winget install yt-dlp.yt-dlp`, `winget install Gyan.FFmpeg`, and a venv with `pip install faster-whisper yt-dlp`. The first Whisper run downloads the model (`small`, ~480 MB).

Done when all four checks pass.

## 2. Run

```
<python> "<skill_dir>/scripts/analizar.py" "<URL>" [--out-dir DIR] [--force-whisper] [--language es]
         [--model tiny|base|small|medium|large-v3] [--start S --end S] [--max-frames 24] [--scene 0.3] [--no-frames]
         [--cleanup] [--cookies-from-browser chrome | --cookies FILE]
```

- Output: `--out-dir`, else `$ANALIZAR_OUTPUT`, else `./analizar-video-output/<id>/` in the cwd (ids: `<yt-id>`, `tiktok-<id>`, `instagram-<id>`). Point it outside `.claude/skills`.
- Final stdout is JSON: `title`, `platform`, `duration`, `description`, `transcript_method`, `no_speech`, `frames`, `output_dir`, `timings_s`.
- Files: `transcript.txt`, `transcript_timed.txt` (`[seconds] text`), `frames/frame_NNN_tTTTTTs.jpg` (name carries the timestamp), `metadata.json`, `video.mp4`, `audio.mp3`.
- `--cleanup` deletes `video.mp4`/`audio.mp3` after processing (frames and transcript stay). Use it when the user only wants the summary.
- A bad, private or unsupported URL exits with `metadata failed: <yt-dlp error>`; report that message to the user.

Done when `metadata.json` exists and either `transcript.txt` is non-empty or `no_speech` is true (silent or music-only video).

## 3. Summarize

1. Read `metadata.json` (title, description) and the transcript.
2. View the frames (Read the jpgs) and tie each to the nearest `transcript_timed.txt` lines by timestamp.
3. Write `summary.md` in the output dir: one-paragraph gist, key points with timestamps, what the frames add (slides, on-screen text, demos).

When `no_speech` is true, say the video has no speech and summarize from frames, title and description.

Done when every claim in the summary traces to the transcript, a frame, or the description.

## Transcript source

- **Subtitles first** (YouTube only): manual subs, else the `*-orig` auto-captions, matched on the base language (`es-US` → `es-orig`/`es`); when the language is unknown, a lone manual track, else a lone `*-orig` track; with several candidates it does not guess, logs the languages and falls back to Whisper (`--language XX` picks that caption track instead). Seconds, not minutes. Auto-captions have no punctuation polish and mangle names/jargon; Whisper is cleaner on both.
- **Whisper** when no subs exist (always for TikTok and Instagram) or with `--force-whisper`. Pass `--language xx` when auto-detect picks wrong (short or music-heavy clips).
- **Model**: `small` is the default. Suggest `--model medium` (~1.5 GB download, roughly 3× slower on CPU) when the video is not English and the `small` transcript has garbled words or wrong-language runs. Ask the user before the first `medium` run, since it downloads the model.
- `transcript_method` in the JSON says which was used; state it in the summary.

## Long videos

- Whisper `small` on CPU runs ~0.54× realtime (~10.5 min for a 20 min video, ~65 min for 2 h). Prefer subtitles; for non-YouTube long videos tell the user the estimate first and offer `--start/--end`.
- `--start/--end` (seconds) restrict transcript and frames to that range; use it for "just the part about X" using the chapters in `description`.
- Summarize long transcripts in chunks (~5 000 characters each, cut on `transcript_timed.txt` line boundaries), summarize each, then merge. A 20 min video is ~27k characters.
- Frames cap at `--max-frames` (24), thinned evenly; scene detection falls back to fixed intervals when it finds fewer than 4 frames.

## Platform quirks

- **YouTube**: subs path available; `video.mp4` is ≤480p.
- **TikTok**: no subtitles, `language` is null. The audio is often background music (a lyric transcript is noise). When the transcript reads as song lyrics or is empty, say so and build the summary from frames, title and description.
- **Instagram**: no subtitles, no `duration` in metadata (script probes it with ffprobe), generic title ("Video by …") so the description carries the text, no 480p format (downloads progressive mp4, frames scaled to 640 px). Signed URLs expire, so the script downloads right after the metadata call. Private reels / login-gated posts need cookies: **ask the user before** using `--cookies-from-browser`.

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Subtitles skipped though the video has captions | Metadata language is `es-US` but tracks are `es-orig`/`es` → fixed by base-language match; check `lang … sub candidates` in the log |
| `cuda failed, cpu fallback: Library cublas64_12.dll …` | A CUDA device is visible but its libs are missing (GTX 1650 box). Logged once per run, then CPU int8 runs; with no CUDA device the attempt is skipped. Installing `nvidia-cublas-cu12`/cudnn would speed it up (untested) |
| `metadata failed: ERROR: …` | Invalid/removed/unsupported URL, or private post: read the yt-dlp error shown |
| Empty transcript, `no speech detected` | Silent or music-only video → summarize from frames/title/description |
| `RuntimeError: mkl_malloc: failed to allocate memory` on CPU | Half-built CUDA model still referenced → script sets `m = None; gc.collect()` before the CPU load |
| `duration` null / crash in frames (Instagram) | No duration in metadata → ffprobe on `audio.mp3` |
| Instagram downloads wrong/audio-only stream | `height<=480` matches nothing → platform selector `b[height<=480][ext=mp4]/…/worst*[vcodec!=none]` |
| Whisper says English, text is garbage (TikTok) | Music, no speech → `--language`, or lean on frames |
| `impersonation … no impersonate target` (TikTok) | Harmless warning |
| `unauthenticated requests to the HF Hub` | Harmless; set `HF_TOKEN` for faster model download |
| `metadata failed` / empty `-J` / HTTP 403 / bot-check | yt-dlp outdated (`pip install -U yt-dlp` / `winget upgrade yt-dlp.yt-dlp`), or private/age-gated → cookies (ask first); YouTube may also need `deno` (install only if yt-dlp asks) |
| Mojibake in channel name on console | Console encoding only; files are UTF-8. Set `PYTHONIOENCODING=utf-8` |
| PowerShell piping JSON into python fails (BOM) | Don't pipe; the script calls yt-dlp itself |
| `audio download failed` | Private/removed video or outdated yt-dlp (see above) |

`video.mp4`/`audio.mp3` stay in the output dir; delete them when done, and keep the output dir out of OneDrive/git sync.
