"""analizar-video: URL (YouTube / TikTok / Instagram) -> transcript + keyframes + metadata.
The script only produces raw material; the summary is written by Claude from its output.

usage: analizar.py URL [--out-dir DIR] [--force-whisper] [--model tiny|base|small|medium|large-v3] [--language es]
                       [--scene 0.3] [--max-frames 24] [--no-frames] [--start S] [--end S]
                       [--cleanup] [--cookies-from-browser BROWSER | --cookies FILE]
env:   ANALIZAR_OUTPUT (default ./analizar-video-output), ANALIZAR_YTDLP (yt-dlp command)
Run it with a python that has faster-whisper (and yt-dlp); nothing depends on the cwd.
Prints one JSON object (metadata + paths + timings) as the last stdout block.
"""
import argparse, gc, glob, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

T = {}
YTDLP = None

# yt-dlp format selector for the keyframe video, per platform
FORMATS = {
    "youtube": "bv*[height<=480][ext=mp4]/bv*[height<=480]/worst",
    # IG DASH video-only starts at 1280p; progressive mp4 reports height=None
    "instagram": "b[height<=480][ext=mp4]/bv*[height<=480]+ba/b[ext=mp4]/worst*[vcodec!=none]",
    "tiktok": "bv*[height<=480]/b[height<=480]/worst*[vcodec!=none]",
}
PLATFORMS = {"youtube": "youtube", "tiktok": "tiktok", "instagram": "instagram"}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


class timed:
    def __init__(s, name): s.name = name
    def __enter__(s): s.t = time.time()
    def __exit__(s, *a):
        T[s.name] = round(time.time() - s.t, 1)
        print(f"[{s.name}] {T[s.name]}s", flush=True)


def find_ytdlp():
    env = os.environ.get("ANALIZAR_YTDLP")
    if env:
        return env.split()
    if run([sys.executable, "-m", "yt_dlp", "--version"]).returncode == 0:
        return [sys.executable, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    for d in (Path(sys.executable).parent, Path(__file__).resolve().parent.parent / ".venv" / "Scripts"):
        for n in ("yt-dlp.exe", "yt-dlp"):
            if (d / n).exists():
                return [str(d / n)]
    sys.exit("yt-dlp not found: pip install yt-dlp in this python, put yt-dlp on PATH, or set ANALIZAR_YTDLP")


def platform_of(d):
    k = (d.get("extractor_key") or d.get("extractor") or "").lower()
    for p in PLATFORMS:
        if p in k:
            return p
    return "other"


def hms(s):
    h, m, sec = s.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def parse_vtt(path):
    """-> [(start_seconds, text)] with auto-caption rolling repeats collapsed."""
    cues, t, last = [], 0.0, ""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "-->" in line:
            t = hms(line.split("-->")[0].strip().split()[0]); continue
        if line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or not line.strip():
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and line != last:
            cues.append((t, line)); last = line
    return cues


def get_subs(url, d, out, cookie_args, language=None):
    lang = d.get("language") or language  # --language picks the track when metadata has none
    manual = d.get("subtitles") or {}
    auto = d.get("automatic_captions") or {}
    if lang:
        base = lang.split("-")[0]  # es-US -> es ; YouTube keys are es-orig / es
        cand = [l for l in manual if l.split("-")[0] == base] or \
               [l for l in auto if l.startswith(base) and l.endswith("-orig")] or \
               [l for l in auto if l.split("-")[0] == base]
    else:  # language unknown: use a track only if it is unambiguous, never guess
        orig = [l for l in auto if l.endswith("-orig")]
        cand = list(manual) if len(manual) == 1 else [] if manual else orig if len(orig) == 1 else []
        if not cand and (len(manual) > 1 or len(orig) > 1):
            print("language unknown and multiple caption languages", sorted(manual) or sorted(orig),
                  "-> falling back to Whisper (use --language XX to pick captions)")
    print("lang", repr(lang), "-> sub candidates", cand)
    if not cand:
        return None, None
    l = cand[0]
    src = "manual-subs" if l in manual else "auto-captions"
    for f in glob.glob(str(out / "subs*")): os.remove(f)
    run(YTDLP + cookie_args + ["--skip-download", "--write-subs" if l in manual else "--write-auto-subs",
                               "--sub-langs", l, "--sub-format", "vtt", "-o", str(out / "subs"), url])
    files = glob.glob(str(out / "subs*.vtt"))
    return (parse_vtt(files[0]), f"{src}:{l}") if files else (None, None)


def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try: return round(float(r.stdout.strip()), 1)
    except ValueError: return None


def cuda_usable():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def transcribe(url, out, model_name, language, start, end, cookie_args):
    for f in glob.glob(str(out / "audio.*")): os.remove(f)
    with timed("audio_download"):
        run(YTDLP + cookie_args + ["-f", "bestaudio", "-x", "--audio-format", "mp3", "--audio-quality", "5",
                                   "-o", str(out / "audio.%(ext)s"), url])
    audio = out / "audio.mp3"
    if not audio.exists():
        sys.exit("audio download failed (see Troubleshooting: private/login-gated or yt-dlp outdated)")
    if start or end:  # transcribe only the requested range
        cut = out / "audio_range.mp3"
        cmd = ["ffmpeg", "-y", "-ss", str(start or 0)] + (["-to", str(end)] if end else []) + ["-i", str(audio), str(cut)]
        run(cmd); audio = cut
    from faster_whisper import WhisperModel
    m = segs = info = None
    dev = "cpu"
    if cuda_usable():  # skip the CUDA attempt entirely when no device is visible
        try:
            m = WhisperModel(model_name, device="cuda", compute_type="int8_float16"); dev = "cuda"
            segs, info = m.transcribe(str(audio), vad_filter=True, language=language)
            segs = list(segs)
        except Exception as e:  # e.g. cublas64_12.dll missing -> CPU
            print("cuda failed, cpu fallback:", str(e)[:120])
            m = None; gc.collect()  # free the half-built CUDA model, else CPU load dies with mkl_malloc
            dev = "cpu"
    if dev == "cpu":
        m = WhisperModel(model_name, device="cpu", compute_type="int8")
        segs, info = m.transcribe(str(audio), vad_filter=True, language=language)
        segs = list(segs)
    off = start or 0
    cues = [(s.start + off, s.text.strip()) for s in segs]
    return cues, f"whisper:{model_name}:{dev}:lang={info.language}"


def frames(url, out, scene, max_frames, duration, start, end, platform, cookie_args):
    fdir = out / "frames"
    shutil.rmtree(fdir, ignore_errors=True); fdir.mkdir()
    vid = out / "video.mp4"
    for f in glob.glob(str(out / "video.*")): os.remove(f)
    with timed("video_download"):
        run(YTDLP + cookie_args + ["-f", FORMATS.get(platform, FORMATS["youtube"]), "--merge-output-format", "mp4",
                                   "-o", str(out / "video.%(ext)s"), url])
    c = glob.glob(str(out / "video.*"))
    if not c: return []
    vid = Path(c[0])
    rng = (["-ss", str(start)] if start else []) + (["-to", str(end)] if end else [])
    off = start or 0
    times = []
    with timed("frames_scene"):
        r = run(["ffmpeg", "-y"] + rng + ["-i", str(vid), "-vf", f"select='gt(scene,{scene})',showinfo,scale=640:-2",
                "-vsync", "vfr", "-q:v", "4", str(fdir / "scene_%03d.jpg")])
        times = [float(x) + off for x in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    files = sorted(fdir.glob("scene_*.jpg"))
    if len(files) < 4:  # scene detection too sparse -> fixed interval
        span = (end or duration) - off
        iv = max(5, int(span / max_frames))
        for f in files: f.unlink()
        with timed("frames_interval"):
            run(["ffmpeg", "-y"] + rng + ["-i", str(vid), "-vf", f"fps=1/{iv},scale=640:-2", "-q:v", "4",
                                         str(fdir / "iv_%03d.jpg")])
        files = sorted(fdir.glob("iv_*.jpg")); times = [off + i * iv for i in range(len(files))]
    pairs = list(zip(files, times + [0] * (len(files) - len(times))))
    if len(pairs) > max_frames:  # thin evenly
        keep = {pairs[int(i * len(pairs) / max_frames)][0] for i in range(max_frames)}
        for f, _ in pairs:
            if f not in keep: f.unlink()
        pairs = [p for p in pairs if p[0] in keep]
    res = []
    for i, (f, t) in enumerate(pairs):  # timestamped names: frame_001_t0123s.jpg
        new = f.with_name(f"frame_{i + 1:03d}_t{int(t):05d}s.jpg"); f.rename(new); res.append(new.name)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out-dir", default=os.environ.get("ANALIZAR_OUTPUT", "./analizar-video-output"))
    ap.add_argument("--force-whisper", action="store_true")
    ap.add_argument("--model", default="small", choices=["tiny", "base", "small", "medium", "large-v3"])
    ap.add_argument("--language", default=None, help="Whisper language and, when metadata has none, the caption track (default: auto-detect)")
    ap.add_argument("--scene", type=float, default=0.3)
    ap.add_argument("--max-frames", type=int, default=24)
    ap.add_argument("--no-frames", action="store_true")
    ap.add_argument("--start", type=float, default=None, help="range start, seconds")
    ap.add_argument("--end", type=float, default=None, help="range end, seconds")
    ap.add_argument("--cleanup", action="store_true", help="delete video.mp4/audio.mp3 after processing")
    ap.add_argument("--cookies-from-browser", default=None)
    ap.add_argument("--cookies", default=None)
    a = ap.parse_args()
    global YTDLP
    YTDLP = find_ytdlp()
    cookie_args = (["--cookies-from-browser", a.cookies_from_browser] if a.cookies_from_browser else []) + \
                  (["--cookies", a.cookies] if a.cookies else [])

    with timed("metadata"):
        r = run(YTDLP + cookie_args + ["--skip-download", "-J", "--no-playlist", a.url])
        try:
            d = json.loads(r.stdout) if r.stdout.strip() else None
        except ValueError:
            d = None
        if r.returncode != 0 or not isinstance(d, dict) or "id" not in d:
            errs = [l for l in r.stderr.splitlines() if l.startswith("ERROR")]  # skip WARNING noise
            sys.exit("metadata failed: " + ("\n".join(errs)[-800:] or r.stderr.strip()[-800:]
                                            or "yt-dlp returned no video info for this URL"))
    platform = platform_of(d)
    vid_id = d["id"] if platform in ("youtube", "other") else f"{platform}-{d['id']}"
    out = Path(a.out_dir).resolve() / vid_id
    out.mkdir(parents=True, exist_ok=True)

    cues, method = None, None
    if not a.force_whisper:
        with timed("subs"): cues, method = get_subs(a.url, d, out, cookie_args, a.language)
    if not cues:
        with timed("transcribe"): cues, method = transcribe(a.url, out, a.model, a.language, a.start, a.end, cookie_args)
    elif a.start or a.end:
        cues = [c for c in cues if (a.start or 0) <= c[0] <= (a.end or 1e12)]
    text = " ".join(t for _, t in cues)
    no_speech = not text.strip()
    if no_speech:
        print("no speech detected: transcript is empty (silent or music-only video); rely on frames, title, description")
    (out / "transcript.txt").write_text(text, encoding="utf-8")
    (out / "transcript_timed.txt").write_text("\n".join(f"[{t:7.1f}] {x}" for t, x in cues), encoding="utf-8")

    duration = d.get("duration") or probe_duration(out / "audio.mp3") or 60  # IG: no duration in metadata
    names = [] if a.no_frames else frames(a.url, out, a.scene, a.max_frames, duration, a.start, a.end, platform, cookie_args)

    meta = {k: d.get(k) for k in ("id", "title", "channel", "language", "upload_date", "webpage_url", "view_count")}
    meta.update(platform=platform, duration=duration, description=(d.get("description") or "")[:2000],
                transcript_method=method, transcript_chars=len(text), no_speech=no_speech, range=[a.start, a.end],
                frames=names, output_dir=str(out), timings_s=T)
    (out / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    if a.cleanup:
        for pat in ("video.*", "audio.*", "audio_range.*"):
            for f in out.glob(pat): f.unlink()
    print(json.dumps(meta, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
