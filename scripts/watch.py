#!/usr/bin/env python3
"""One call: page/local file -> media.mp4 -> index -> grids -> transcript.

Thin orchestrator over acquire/index/frames/transcript so a single command gets
an agent everything it needs to answer questions about a video. Every stage is
skippable when its artifact already exists, so a rerun after a fix costs only
what changed.

Usage:
  python watch.py --url PAGE_URL --work WORKDIR [--cells 36] [--lang zh] [--model medium]
  python watch.py --file video.mp4 --work WORKDIR [...]

Prints the manifest, index, transcript and work dir on the last lines as
`ARTIFACTS: {...}` so the caller can parse them.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def run(cmd: list[str], label: str, env: dict | None = None) -> str:
    print(f"[watch] {label} ...", file=sys.stderr, flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        print(out[-2000:], file=sys.stderr)
        raise SystemExit(f"{label} failed (exit {proc.returncode})")
    return out


def tail_path(out: str, tag: str) -> str | None:
    for line in out.splitlines():
        if line.startswith(tag):
            return line.split(":", 1)[1].strip()
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--file")
    ap.add_argument("--work", required=True)
    ap.add_argument("--cells", type=int, default=64)
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--model", default="medium")
    ap.add_argument("--step", type=float, default=9.0)
    ap.add_argument("--skip-acquire", action="store_true")
    ap.add_argument("--skip-transcript", action="store_true")
    args = ap.parse_args()

    work = Path(args.work).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)

    video = Path(args.file).expanduser().resolve() if args.file else work / "media.mp4"
    if args.url and not (args.skip_acquire or video.exists()):
        out = run([PY, str(HERE / "acquire.py"), args.url, "--out-dir", str(work),
                   "--step", str(args.step)], "acquire")
        got = tail_path(out, "ACQUIRED")
        if not got:
            raise SystemExit("acquire produced no file")
        video = Path(got)
    if not video.exists():
        raise SystemExit(f"no video at {video}")

    index_json = work / "index.json"
    if not index_json.exists():
        run([PY, str(HERE / "index.py"), str(video), "--out-dir", str(work)], "index")

    manifest = work / "manifest.json"
    if not manifest.exists():
        run([PY, str(HERE / "frames.py"), str(video), "--out-dir", str(work),
             "--index", str(index_json), "--cells", str(args.cells)], "frames")

    transcript = work / "transcript.json"
    if not args.skip_transcript and not transcript.exists():
        run([PY, str(HERE / "transcript.py"), str(video), "--out", str(transcript),
             "--source", "whisper", "--lang", args.lang], "transcript",
            env={"WHISPER_MODEL": args.model,
                 "WHISPER_DEVICE": os.environ.get("WHISPER_DEVICE", "cpu"),
                 "HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")})

    art = {"video": str(video), "index": str(work / "index.md"),
           "manifest": str(manifest), "transcript": str(transcript) if transcript.exists() else None,
           "work": str(work)}
    if manifest.exists():
        grids = json.loads(manifest.read_text(encoding="utf-8")).get("grids", [])
        art["grids"] = [g["path"] for g in grids]
    print("ARTIFACTS: " + json.dumps(art, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
