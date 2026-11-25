---
name: watch-video
description: Watch a video and answer questions about it — summarize, explain, recap, find a moment, or transcribe. Works on a local file AND on web pages that gate their video behind a login and signed, short-lived segment URLs (课程平台、知识付费站、企业培训站、微信登录页). Fetches the media by driving a real browser, indexes every shot, packs one frame per shot into timestamp-labelled grids, and pairs them with a local timestamped transcript. Trigger words — 看视频、读视频、这个视频讲了什么、帮我总结这段视频、视频转写、把讲的内容整理出来、视频里第几分钟讲了什么、watch this video、summarize this clip, or a video URL/path pasted with any question about its contents. Do NOT use for editing, cutting, rendering, or generating video.
---

# watch-video

An agent cannot "watch" video directly: a 13-minute lecture is ~24,000 frames.
This skill turns a video into three things a text model reads well — a shot
index, timestamp-stamped frame grids, and a timestamped transcript — and then
you do the understanding.

Scripts live in `SKILL_DIR/scripts/`, where `SKILL_DIR` is the directory holding
this file. The Python that has the dependencies is the venv inside it — use it,
not bare `python`:

| OS | interpreter |
| --- | --- |
| Windows | `"$SKILL_DIR/.venv/Scripts/python.exe"` |
| macOS / Linux | `"$SKILL_DIR/.venv/bin/python"` |

Examples below use the Windows path; swap it on other platforms. Set
`SKILL_DIR=<this directory>` once and the commands below work verbatim.

| script | job |
| --- | --- |
| `acquire.py` | page URL → `media.mp4` (browser harvest, survives signed URLs) |
| `index.py` | shot boundaries, motion/light, sound hits, silences |
| `frames.py` | one frame per shot, packed into labelled grids |
| `transcript.py` | timestamped speech (local faster-whisper by default) |
| `watch.py` | all of the above in one call |

## Step 0 — get the file

Local file → skip this step.

URL → run acquire. It opens a visible browser window (Edge, else Chrome, else
Playwright's own chromium) on a dedicated profile (`SKILL_DIR/.browser-profile`),
lets the player request its own segments, and saves the response bodies; the
manifest is captured first and used to verify coverage, with a repair pass for
holes.

```bash
SKILL_DIR=~/.workbuddy/skills/watch-video
"$SKILL_DIR/.venv/Scripts/python.exe" "$SKILL_DIR/scripts/acquire.py" "<PAGE_URL>" \
    --out-dir /tmp/wv_<slug> --step 9
```

Prints `ACQUIRED: <path>` and writes `acquire.json` with `segments`,
`estimated_gaps` and `duration_s`. Then check the file is complete before
trusting it:

```bash
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 <file>
```

If `estimated_gaps > 0` or the duration is short of what the player showed,
rerun with `--step 5`. Do not analyse a file with holes and then claim it is the
whole video.

Rules that matter here:

- **Login-walled pages**: the first run opens a real window. If the page asks for
  a login or a QR scan, tell the user to complete it in that window once; the
  profile keeps the session for later runs.
- **DRM is a hard stop.** Widevine/PlayReady streams cannot be captured. Say so
  instead of producing a broken file.
- Never `rm` the browser profile — it holds the user's session.
- The harvested file is a temp working copy. Leave the user's originals alone.

## Step 1 — index (cheap, do it first)

```bash
"$SKILL_DIR/.venv/Scripts/python.exe" "$SKILL_DIR/scripts/index.py" "<video>" --out-dir <work>
```

Prints `INDEX: <path>`. Read `index.md` whole: one line per shot with motion,
light and sound hits, plus silence spans. On a lecture or screencast this tells
you where the slide changed, where the demo started, and where the speaker
paused for questions.

## Step 2 — grids

Start the transcript FIRST in the background (Step 3), then build grids while it
runs; they share nothing and the transcript dominates wall clock.

```bash
"$SKILL_DIR/.venv/Scripts/python.exe" "$SKILL_DIR/scripts/frames.py" "<video>" \
    --out-dir <work> --index <work>/index.json
```

Prints `MANIFEST: <path>`. Pick `--cells` by the question, not by length:

| `--cells` | use for |
| --- | --- |
| 16 | small on-screen text: code, UI labels, slide footnotes |
| 36 | facial expression, fine motion, demo cursors |
| 64 (default) | plot, scene inventory, lecture structure |

A screencast with dense code on screen is a **--cells 16** job on the spans that
matter. For a specific moment use `--start`/`--end` instead of re-reading
everything.

## Step 3 — transcript

```bash
HF_ENDPOINT=https://hf-mirror.com WHISPER_MODEL=medium WHISPER_DEVICE=cpu \
"$SKILL_DIR/.venv/Scripts/python.exe" "$SKILL_DIR/scripts/transcript.py" "<video>" \
    --out <work>/transcript.json --source whisper --lang zh
```

`--source auto` also tries native captions (needs `yt-dlp`, URLs only) and
ElevenLabs (needs `ELEVENLABS_API_KEY`); local whisper is the default fallback
and the only one that always works offline.

`WHISPER_DEVICE=cpu` is the safe default: a GPU without the matching CUDA/cuBLAS
runtime makes `auto` pick CUDA and die on `cublas64_12.dll`. Set
`WHISPER_DEVICE=cuda` only when `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`
has actually been done.

Model choice on CPU, `WHISPER_MODEL`:

| model | speed (13 min audio, CPU) | quality for Chinese |
| --- | --- | --- |
| `small` | ~1-2 min | usable, drops technical terms |
| **`medium`** | ~4-8 min | good default: 安卓/控件/属性 terms mostly land |
| `large-v3` | 15 min+ | best, only if the wording matters line by line |

One whole-file call is the fast path — do not split the audio into chunks and
run them in parallel, it is slower.

Trust the transcript for *what was said*, never for *what was on screen*: it
mangles proper nouns and English technical terms. When a term matters, check it
against a frame.

## Step 4 — read everything, then answer

Read `index.md` whole, then every `grids[].path` from the manifest in one
message (parallel reads so they arrive together), then `transcript.json` whole.
No sampling, no "interesting windows" filter — that choice is exactly the guess
these files exist to prevent.

Every cell carries its timestamp burned into the top-left corner (`MM:SS.d`).
Cells are not evenly spaced, so read the stamp; never infer time from position.

Then answer from all three streams: frames say what is shown, transcript says
what is said, index says where the cuts and pauses are. Cite timestamps. Never
paste the raw transcript back — synthesize it.

**The failure mode.** Frames let you build a story that is coherent and wrong.
When the visuals suggest a causal claim, treat it as a hypothesis and find the
transcript line that confirms it. If nothing confirms it, report what you saw
and say the reason was not stated.

## Deliverable shape

For a lecture or course video, hand back:

1. one-paragraph gist
2. outline with `[mm:ss]` per section — the actual steps, settings, values said
3. key on-screen elements worth naming (menus, dialogs, code) with timestamps
4. anything uncertain, marked as uncertain

If the user asked a specific question, answer it directly and cite the timestamp.

## Recommended one-shot

```bash
"$SKILL_DIR/.venv/Scripts/python.exe" "$SKILL_DIR/scripts/watch.py" \
    --url "<PAGE_URL>" --work /tmp/wv_job --cells 36 --lang zh --model medium
```

Runs acquire → index → frames → transcript, prints every artifact path, and
leaves a `REPORT.md` skeleton at the end of the work dir for you to fill in.

## Limits — say these plainly

- **DRM** (Widevine/PlayReady): cannot capture.
- **Live streams** and anything requiring interactive auth per segment: out of
  scope.
- **Continuous motion**, easing and exact pacing: grids give positions, not
  curves.
- **Off-screen and inaudible content** is not recoverable.
- Long videos on CPU: transcript time scales roughly linearly; a 2-hour video
  with `medium` is 40+ minutes. Say the cost before starting.
