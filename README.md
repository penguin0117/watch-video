# watch-video

**An Agent Skill that lets a text-only agent actually watch a video** — including
videos locked behind a login on pages that hand out signed, expiring segment
URLs (course platforms, paid knowledge sites, corporate training portals,
WeChat-login players).

Drop this folder into your agent's skills directory and it can summarise a
lecture, find the moment something happened, transcribe what was said, or read
code off a screencast.

[中文说明](#中文说明) · [Limits](#limits) · [Credits](#credits)

---

## The problem

An agent cannot "watch" a 13-minute lecture: that is ~24,000 frames. Feeding
frames to a model one by one is both slow and expensive, and feeding audio
alone loses everything that was shown on screen.

There is a second, harder problem that most "video understanding" skills skip.
Many video pages do not expose a downloadable URL. The player asks an API for a
manifest, and **that response — and every single media segment after it — is
signed by JavaScript, tied to a session, and valid for a few minutes**. The
practical consequence:

```console
$ curl -I "https://…/stream.m3u8?KEY1=…&KEY2=…"      # the URL copied from devtools
HTTP/2 403
$ yt-dlp "https://…/video-page.htm"                  # no generic extractor match
ERROR: Unsupported URL
```

Neither `ffmpeg`, `curl`, nor `yt-dlp` can fetch it, because the signature is
only valid for the request the page itself is about to make.

## What this skill does about it

`acquire.py` stops trying to forge the request and **lets the browser make it**.
It drives a real browser on a persistent profile, seeks through the timeline so
the player is forced to request every segment, and captures the response bodies
as they arrive. Then it remuxes them with ffmpeg.

```
page URL ──▶ [acquire]  real browser ──▶ captures segments ──▶ media.mp4
                                                               │
local file ────────────────────────────────────────────────────┤
                                                               ▼
                                            [index]  shot boundaries, motion,
                                                     light, sound hits, silences
                                                               │
                                          ┌────────────────────┴────────────┐
                                          ▼                                 ▼
                              [frames] one frame per shot      [transcript] local
                              packed into timestamp-            faster-whisper,
                              labelled grids                    timestamped
                                          └────────────────────┬────────────┘
                                                               ▼
                                                    the agent reads all three
```

The three outputs are exactly what a text model is good at reading: a compact
structural outline, a few timestamp-stamped images instead of thousands of
frames, and a timestamped transcript. The agent does the understanding.

## Install

```bash
git clone https://github.com/penguin0117/watch-video.git ~/.workbuddy/skills/watch-video
cd ~/.workbuddy/skills/watch-video

python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt          # Windows: .venv/Scripts/python.exe
.venv/bin/python -m playwright install chromium              # only if you have no Edge/Chrome

ffmpeg -version                                              # must be on PATH
```

Any agent that reads `SKILL.md` frontmatter works — WorkBuddy
(`~/.workbuddy/skills/`), Claude Code (`~/.claude/skills/`), or anything else
following the same convention. Copy the folder to wherever yours lives.

On a slow or restricted network, point pip and Hugging Face at a mirror:

```bash
.venv/bin/python -m pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple
export HF_ENDPOINT=https://hf-mirror.com
```

## Use

One call does everything:

```bash
.venv/bin/python scripts/watch.py --url "<PAGE_URL>" --work ./out/job --cells 36 --lang zh
.venv/bin/python scripts/watch.py --file ./lecture.mp4 --work ./out/job
```

It prints `ARTIFACTS: {...}` on the last line with the paths of the index, the
frame grids and the transcript. Stages are skipped when their output already
exists, so re-running after a fix costs only what changed.

Or run a stage alone:

| command | what it gives you |
| --- | --- |
| `scripts/acquire.py "<URL>" --out-dir work --step 9` | `media.mp4` + `acquire.json` (segment count, estimated gaps) |
| `scripts/index.py media.mp4 --out-dir work` | `index.md` — one line per shot, with cuts and pauses |
| `scripts/frames.py media.mp4 --out-dir work --index work/index.json --cells 16` | timestamp-labelled frame grids |
| `scripts/transcript.py media.mp4 --out work/transcript.json --lang zh` | timestamped transcript |

### Picking `--cells`

`--cells` is the grid width, and it is the one knob that decides whether
on-screen text is readable. Pick by the question, not by video length:

| `--cells` | use for |
| --- | --- |
| 16 | code, UI labels, slide footnotes — small text |
| 36 | facial expression, demo cursors, fine motion |
| 64 (default) | plots, scene inventory, overall structure |

A code screencast is a `--cells 16` job on the spans that matter. For one
specific moment, use `--start` / `--end` instead of re-reading the whole video.

### Login-walled pages

The first run opens a visible browser window on a dedicated profile
(`.browser-profile/`). If the page asks you to log in or scan a QR code, do it
once in that window — later runs reuse the session. **Never commit or delete
that folder**; it holds your cookies and it is already in `.gitignore`.

## Measured on a real case

A 13-minute Android course video on a WeChat-login player with per-segment
signatures (`mediaprotx.*`, `.m3u8?KEY1=…&KEY2=…`, all request-signed):

| stage | result |
| --- | --- |
| acquire | 83/83 segments, **792.59 s**, matching the player's reported 792.5 s — zero gaps |
| index | 32 shots, 143 sound events |
| frames | 2 grids, 92 timestamped frames |
| transcript | 220 Chinese segments, 7 min 17 s on CPU (`medium`) |

The transcript alone was not enough: it mangled attribute names and method
names. Reading a `--cells 16` grid plus a cropped raw frame recovered the exact
XML and Java. That is the failure mode this design exists to prevent — **trust
the transcript for what was said, never for what was on screen.**

## Limits

- **DRM (Widevine / PlayReady) cannot be captured.** Netflix, Disney+, iQIYI
  premium and similar will fail, and the script says so instead of writing a
  broken file. This is a hard stop, not a bug.
- **Live streams** and anything that re-requires interactive auth per segment
  are out of scope.
- **Continuous motion** — grids give positions, not easing curves.
- **Off-screen and inaudible content** is not recoverable.
- Transcription time scales roughly linearly: a 2-hour video with `medium` on
  CPU is 40+ minutes. Say the cost before starting.
- Respect the terms of service and copyright of whatever you point this at. It
  is a reading aid for content you already have access to.

## Files

```
SKILL.md              the skill contract: when to use it, in what order, and what to hand back
scripts/acquire.py    page URL -> media.mp4, by harvesting what the browser itself requested
scripts/index.py      shot boundaries, motion, light, sound hits, silences
scripts/frames.py     one frame per shot, packed into timestamp-labelled grids
scripts/transcript.py timestamped speech: local faster-whisper, native captions, or ElevenLabs
scripts/watch.py      thin orchestrator over all of the above
scripts/text_check.py OCR helper used by index.py --ocr (macOS Vision, or tesseract)
```

## Credits

`scripts/index.py`, `scripts/frames.py`, `scripts/cull.py`, `scripts/text_check.py`
and `scripts/ocr.swift` are adapted from the **video-understand** skill in
[andrewii23/ii23-skills](https://github.com/andrewii23/ii23-skills) by warodom,
MIT licensed. The shot-indexing design — and packing one frame per shot into a
labelled grid instead of sampling frames uniformly — is theirs, and it is the
reason this stays cheap.

Added in this repo: `acquire.py` (browser harvest of signed segment streams),
`watch.py`, and fixes for three portability bugs in the upstream scripts — a
Windows path escaping bug in ffmpeg filter strings, a PyAV/faster-whisper
version conflict, and a CUDA-without-cuBLAS crash.

## License

MIT. See [LICENSE](LICENSE).

---

## 中文说明

**让只能读文字的 agent 真正"看"视频**，包括那种必须登录、而且视频分段地址由
页面 JS 现场签名、几分钟就过期的页面（课程平台、知识付费站、企业培训站、
微信登录播放器）。

直接 `curl` / `ffmpeg` / `yt-dlp` 抓这类视频必定 403——签名只对页面自己即将发出的
那个请求有效。这里的做法是**不在外面伪造请求，而是让浏览器自己去请求**：用真实
浏览器打开页面，沿时间轴拖动迫使播放器逐个拉取分片，把响应体截获下来，再用
ffmpeg 合并。

产出三样东西给 agent 读：镜头索引、带时间戳的拼帧网格图、带时间戳的本地转写稿。
agent 负责理解。

**安装**：把仓库克隆到你的 skills 目录，建 venv，装 `requirements.txt`，确认
`ffmpeg` 在 PATH 里。需要登录的站点第一次会弹出浏览器窗口，扫码登一次就好，
`.browser-profile/` 会记住会话，别删也别提交。

**用法**：

```bash
.venv/bin/python scripts/watch.py --url "<页面地址>" --work ./out/job --cells 36 --lang zh
```

看代码/小字用 `--cells 16`，看整体结构用默认 64。

**关键提醒**：转写稿只能信"说了什么"，不能信"屏幕上是什么"——属性名、方法名
一定会被听错，需要精确时要对着 `--cells 16` 的帧核对。

**做不到的**：DRM 加密（Widevine/PlayReady）抓不了，会直接告诉你；直播流不支持。
请遵守目标站点的服务条款与版权，这东西的定位是"帮你读你本来就有权看的内容"。
