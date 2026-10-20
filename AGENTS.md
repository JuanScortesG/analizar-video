# AGENTS.md — contexto compartido del proyecto

> Memoria interna. TODOS los agentes leen esto al arrancar. **Solo el orquestador lo escribe** (los workers solo lo leen). El orquestador lo actualiza tras recibir reportes de los workers.

Comunicación: hablá con Juan en español; los encargos a workers pueden ir en inglés.

## Qué es este proyecto
Skill de Claude Code `analizar-video`: dado un enlace de YouTube/TikTok/Instagram extrae transcripción (subtítulos o Whisper), fotogramas clave, y Claude escribe el resumen.

## Stack

| Capa | Tech |
|---|---|
| Descarga / subtítulos | yt-dlp 2026.08.19 (winget) |
| Audio / frames | ffmpeg + ffprobe 8.1.2 |
| Transcripción | faster-whisper 1.2.1, modelo `small`, CPU (CUDA roto: falta cublas64_12.dll) |
| Runtime | Python 3.11, venv `prototypes/analizar-video/.venv` |

## Personas activas (roster)

- `orquestador` — rutea fases + aprueba planes
- `dev-yt`, `dev-hermad`, `dev-1`, `dev-2` — tab **desarrollo**
- `reviewer-1` — tab **qa**

## Fase actual
Hecho (2026-10-18):
- Fase 1 prototipo YouTube → `prototypes/analizar-video/`.
- Fase 2 TikTok (`prototypes/analizar-video/tiktok/`) e Instagram (`.../instagram/`).
- Fase 3 skill final: `.claude/skills/analizar-video/SKILL.md` + `scripts/analizar.py`.
- Fase 4 QA: PASS en las 3 plataformas.
- Ronda de fixes (2026-10-19): los 6 hallazgos de QA + N1-N3 corregidos; reviewer-1 APPROVED.
  Nuevo: errores limpios "metadata failed", `no_speech` en metadata, rutas independientes del cwd,
  fallback de subtítulos sin idioma (un solo track o Whisper; `--language XX` elige), `--model` con choices,
  `--cleanup` borra video/audio.
- Abierto (menor): con GPU visible pero sin cublas64_12.dll se intenta CUDA una vez por corrida (~1 s). Nada commiteado aún.

## Reglas del equipo

- Política de aprobación: auto, salvo auth/dinero/DB/seguridad → escala a Juan. Instalar software → preguntar a Juan.
- Un solo writer al repo; stories paralelas → `git worktree`/branch o archivos separados.
- Peer-to-peer permitido (reviewer→dev, pm→devs); el orquestador coordina el top.
- Solo el orquestador escribe este archivo.

## Pitfalls de hermad (conocidos)
- Nombrar agentes `persona` o `persona-N` (`dev-1`, `reviewer-1`); con otros nombres el daemon no entrega el buzón ni procesa marcadores (bug `personaOf()` en daemon.js del paquete global, sin parchear).
- `spawn --kind` conserva el flag de modelo de la persona original; config del proyecto ya fija dev/reviewer/reader en claude.
- `--skills a,b` en PowerShell: citar la lista (`"a,b"`).
- Nunca poner una línea literal `HERMAD:DONE ...` dentro de un encargo: el daemon la ve en el pane y dispara la ruta (cierra al agente).
- El daemon corre en el pane `wE:p2`.
