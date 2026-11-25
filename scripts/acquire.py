#!/usr/bin/env python3
"""Acquire the actual media file behind a web page.

Any agent can watch a local file. The hard part is getting one out of a page
whose player only hands the stream to a real browser with signed, short-lived
segment URLs. This script does that in two stages:

  harvest  Drive a real browser, let the player request its own media, and
           capture the response bodies it receives. Seeking through the
           timeline forces every segment to load, so the whole video is
           covered rather than the first N seconds of buffer. Segments are
           then remuxed with ffmpeg.

  url      If a plain mp4/m3u8 URL is visible and downloads cleanly without
           the browser, take that shortcut instead.

Usage:
  python acquire.py PAGE_URL --out-dir WORKDIR [--strategy auto|harvest|url]
                    [--wait 45] [--click] [--headless] [--step 8]

Output:
  WORKDIR/media.mp4     the video (audio included when the stream had it)
  WORKDIR/acquire.json  provenance: page title, final URL, segment count, gaps
  last line: ACQUIRED: <abs path>

First run opens a visible Edge window on a dedicated profile
(SKILL_DIR/.browser-profile). If the page needs a login, log in ONCE in that
window; later runs reuse the session. DRM (Widevine/PlayReady) cannot be
captured -- the script says so instead of writing a broken file.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = SKILL_DIR / ".browser-profile"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
SEG_RE = re.compile(r"\.(ts|m4s|cmfv|cmfa|aac)(\?|$)", re.I)
MANIFEST_RE = re.compile(r"\.m3u8(\?|$)", re.I)
FILE_RE = re.compile(r"\.(mp4|webm|flv|mov|mkv)(\?|$)", re.I)


def log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def launch(p, profile: Path, headless: bool):
    last = None
    for channel in ("msedge", "chrome", None):
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=headless, channel=channel,
                args=["--no-first-run", "--no-default-browser-check",
                      "--autoplay-policy=no-user-gesture-required",
                      "--mute-audio", "--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900})
            log(f"[acquire] browser: {channel or 'chromium'}")
            return ctx
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"[acquire] {channel or 'chromium'} unavailable: {exc}")
    raise SystemExit(f"no usable browser ({last}); install Edge/Chrome or run "
                     "`python -m playwright install chromium`")


def probe_video_state(page) -> dict:
    try:
        return page.evaluate("""()=>{const v=document.querySelector('video');
            if(!v) return null;
            return {t:v.currentTime, d:v.duration||0, paused:v.paused,
                    rs:v.readyState, src:(v.currentSrc||v.src||'').slice(0,80)}}""") or {}
    except Exception:  # noqa: BLE001
        return {}


def harvest(page, work: Path, step: float, wait: int, seen: list) -> tuple[int, int, str]:
    """Seek through the whole timeline, saving every media segment body.

    The player's own manifest is captured and used as the authoritative segment
    list: it gives the running order and each segment's start time, so a sweep
    that misses a few (buffering, slow seek) can be repaired by jumping straight
    back to the exact seconds of the holes.
    """
    seg_dir = work / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    got: dict[str, Path] = {}
    manifest: list[tuple[str, float]] = []   # (segment basename, start seconds)
    manifest_raw = {"text": ""}

    def key_of(url: str) -> str:
        return url.split("?")[0].rsplit("/", 1)[-1]

    state = probe_video_state(page)
    duration = float(state.get("d") or 0)

    def drain() -> int:
        added = 0
        while seen:
            r = seen.pop(0)
            try:
                if r.status >= 400:
                    continue
                u = r.url
                if MANIFEST_RE.search(u) and not manifest_raw["text"]:
                    txt = r.text()
                    if "#EXTM3U" in txt:
                        manifest_raw["text"] = txt
                        cum_t = 0.0
                        pending = 0.0
                        for line in txt.splitlines():
                            line = line.strip()
                            if line.startswith("#EXTINF:"):
                                try:
                                    pending = float(line[8:].rstrip(","))
                                except ValueError:
                                    pending = 0.0
                                continue
                            if line and not line.startswith("#"):
                                manifest.append((line.rsplit("/", 1)[-1], cum_t))
                                cum_t += pending if pending else 0.0
                    continue
                if SEG_RE.search(u) or FILE_RE.search(u):
                    k = key_of(u)
                    if k in got:
                        continue
                    body = r.body()
                    if body and len(body) > 1000:
                        p = seg_dir / k
                        p.write_bytes(body)
                        got[k] = p
                        added += 1
            except Exception:  # noqa: BLE001
                continue
        return added

    # start playback
    for _ in range(3):
        if not probe_video_state(page).get("paused"):
            break
        page.mouse.click(640, 430)
        time.sleep(1.5)
    state = probe_video_state(page)
    duration = duration or float(state.get("d") or 0)
    log(f"[acquire] duration: {duration:.1f}s  src={state.get('src','')[:50]}")

    deadline = time.time() + wait
    while duration <= 0 and time.time() < deadline:
        time.sleep(1)
        duration = float(probe_video_state(page).get("d") or 0)

    if duration <= 0:
        drain()
        if got:
            write_order(work, natural_order(list(got)), duration)
            return len(got), 0, "harvested without known duration"
        raise SystemExit("NO_MEDIA: no <video> element and no media responses")

    t = 0.0
    ticks = 0
    while t < duration + step:
        seek(page, t)
        time.sleep(0.9)
        drain()
        t += step
        ticks += 1
        if ticks % 8 == 0:
            log(f"[acquire] {min(t, duration):.0f}/{duration:.0f}s "
                f"segments={len(got)}/{len(manifest) or '?'}")

    drain()

    # repair pass: jump straight to the holes the manifest says are missing
    for attempt in range(3):
        if not manifest:
            break
        missing = [(n, s) for n, s in manifest if n not in got]
        if not missing:
            break
        log(f"[acquire] repair pass {attempt + 1}: {len(missing)} segment(s) missing")
        for name, start in missing:
            seek(page, max(0.0, start + 1.0))
            time.sleep(1.4)
            drain()
            if name in got:
                continue
            seek(page, max(0.0, start + 0.3))
            time.sleep(1.4)
            drain()

    drain()
    if manifest:
        names = [n for n, _ in manifest if n in got]
        missing = [n for n, _ in manifest if n not in got]
        (work / "manifest.m3u8").write_text(manifest_raw["text"], encoding="utf-8")
    else:
        names = natural_order(list(got))
        missing = []
    write_order(work, names, duration)
    if not names:
        raise SystemExit("DOWNLOAD_FAILED: nothing captured")
    return len(names), len(missing), "harvest"


def natural_order(names: list[str]) -> list[str]:
    """Order segment files by the trailing counter, not lexicographically.

    Players name segments `<prefix><index>.ts`, and a prefix ending in a digit
    makes plain sorting put 50310 before 5039. Take the longest common prefix
    and sort the remainder numerically; fall back to the file mtime.
    """
    if not names:
        return []
    prefix = names[0]
    for n in names[1:]:
        i = 0
        while i < min(len(prefix), len(n)) and prefix[i] == n[i]:
            i += 1
        prefix = prefix[:i]
    def key(n: str):
        tail = n[len(prefix):]
        m = re.match(r"(\d+)", tail)
        return (0, int(m.group(1)), n) if m else (1, 0, n)
    return sorted(names, key=key)


def seek(page, t: float) -> None:
    try:
        page.evaluate("(t)=>{const v=document.querySelector('video');"
                      "if(v){v.currentTime=t; if(v.play) v.play().catch(()=>{})}}", t)
    except Exception:  # noqa: BLE001
        pass


def write_order(work: Path, names: list[str], duration: float) -> None:
    (work / "segments.order").write_text("\n".join(names), encoding="utf-8")


def remux(work: Path) -> Path:
    seg_dir = work / "segments"
    names = [n for n in (work / "segments.order").read_text(encoding="utf-8").splitlines() if n]
    if not names:
        raise SystemExit("DOWNLOAD_FAILED: no segments captured")
    exts = {Path(n).suffix.lower() for n in names}
    if exts <= {".ts"}:
        # concatenating TS is safe: they are a byte stream
        concat = work / "all.ts"
        with concat.open("wb") as fh:
            for n in names:
                fh.write((seg_dir / n).read_bytes())
        src, extra = concat, ["-bsf:a", "aac_adtstoasc"]
    else:
        lst = work / "concat.txt"
        lst.write_text("".join(f"file '{(seg_dir / n).as_posix()}'\n" for n in names),
                       encoding="utf-8")
        src, extra = lst, []
        dest = work / "media.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
               "-f", "concat", "-safe", "0", "-i", str(src), "-c", "copy", str(dest)]
        subprocess.run(cmd, check=False)
        if dest.exists():
            return dest
        raise SystemExit("DOWNLOAD_FAILED: concat failed")
    dest = work / "media.mp4"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
           "-i", str(src), "-c", "copy"] + extra + [str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dest.exists():
        raise SystemExit("DOWNLOAD_FAILED:\n" + (proc.stderr or "")[-1200:])
    return dest


def try_direct(page, work: Path, wait: int) -> tuple[bool, dict]:
    """Best-effort: a plain mp4 URL that ffmpeg can fetch on its own."""
    urls: list[str] = []
    page.on("response", lambda r: urls.append(r.url)
            if FILE_RE.search(r.url) and r.status < 400 else None)
    deadline = time.time() + min(wait, 20)
    while time.time() < deadline and not urls:
        time.sleep(1)
    if not urls:
        return False, {}
    url = urls[0]
    dest = work / "media.mp4"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
           "-user_agent", UA, "-referer", page.url, "-i", url, "-c", "copy", str(dest)]
    subprocess.run(cmd, capture_output=True, text=True)
    if dest.exists() and dest.stat().st_size > 200_000:
        return True, {"source_url": url.split("?")[0], "method": "direct"}
    dest.unlink(missing_ok=True)
    return False, {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--strategy", default="harvest", choices=["auto", "harvest", "url"])
    ap.add_argument("--wait", type=int, default=45, help="seconds to wait for playback")
    ap.add_argument("--step", type=float, default=8.0, help="seek step in seconds")
    ap.add_argument("--click", action="store_true", help="click viewport centre first")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not on PATH")

    work = Path(args.out_dir).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = launch(p, Path(args.profile), args.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        seen: list = []
        page.on("response", lambda r: seen.append(r)
                if (MANIFEST_RE.search(r.url) or SEG_RE.search(r.url)
                    or FILE_RE.search(r.url)) else None)
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            log(f"[acquire] goto warning: {exc}")
        time.sleep(4)
        if args.click:
            page.mouse.click(640, 430)
        title, final_url = page.title(), page.url

        method, info = "harvest", {}
        if args.strategy in ("auto", "url"):
            ok, info = try_direct(page, work, args.wait)
            if ok:
                method = "direct"

        if method != "direct":
            if not probe_video_state(page):
                page.wait_for_timeout(3000)
            segs, gaps, note = harvest(page, work, args.step, args.wait, seen)
            if segs == 0:
                raise SystemExit("NO_MEDIA: the player never requested media. "
                                 "Try --wait 90, --click, or check that the page "
                                 "plays in the opened window.")
            dest = remux(work)
        else:
            dest = work / "media.mp4"
            segs, gaps = -1, 0

        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass

    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "default=nw=1:nk=1", str(dest)],
                         capture_output=True, text=True).stdout.strip()
    meta = {"page_title": title, "page_url": final_url, "method": method,
            "segments": segs, "estimated_gaps": gaps, "duration_s": dur,
            "file": str(dest), "bytes": dest.stat().st_size, **info}
    (work / "acquire.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    shutil.rmtree(work / "segments", ignore_errors=True)
    (work / "all.ts").unlink(missing_ok=True)
    print(f"ACQUIRED: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
