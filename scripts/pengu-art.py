#!/usr/bin/env python3
"""Regenerate the Ask page's mascot artwork through OpenRouter.

The mascot on /ask is generated, not drawn: a 3D character image model paints
one hero image, the same model edits it into the four other moods so the
character stays consistent, and a video model animates the idle pose from that
hero frame. Everything is then keyed to transparency and assembled into the
files the page serves.

    OPENROUTER_API_KEY=... python3 scripts/pengu-art.py --all
    OPENROUTER_API_KEY=... python3 scripts/pengu-art.py --character   # images only
    python3 scripts/pengu-art.py --assemble                          # re-key local files
    python3 scripts/pengu-art.py --check                             # report what exists

Stages:
  character  hero image + the four mood poses (about US$0.10)
  video      4 s idle loop, image-to-video (about US$0.12)
  assemble   key the video to alpha, build the seamless animated PNG, write the
             static poses and docs/pengu-art.json

Requirements: ffmpeg on PATH. Output goes to elixir/priv/static/images/pengu;
`--assemble` alone needs no API key and no network.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "elixir", "priv", "static", "images", "pengu")
WORK = os.path.join(ROOT, "elixir", "tmp", "pengu-art")  # elixir/tmp is git-ignored
MANIFEST = os.path.join(ROOT, "docs", "pengu-art.json")
API = "https://openrouter.ai/api/v1"

# Models and parameters that were verified against the image/video routers.
# gpt-image-2.5-flare supports background=transparent and image references;
# veo-3.1-lite is the cheapest image-to-video model with a 4 s clip.
IMAGE_MODEL = "openai/gpt-image-2.5-flare"
VIDEO_MODEL = "google/veo-3.1-lite"
IMAGE_ASPECT = "2:3"
VIDEO_ASPECT = "9:16"
VIDEO_RESOLUTION = "720p"
VIDEO_SECONDS = 4

# Keying parameters. The video is generated on flat magenta because the
# character has no magenta in it; the alpha erosion (blur + threshold) removes
# the pink spill that 4:2:0 chroma subsampling leaves on the white edges.
CHROMA = "0xff00ff"
KEY_SIMILARITY = "0.28"
KEY_BLEND = "0.04"
ERODE_SIGMA = "1.2"
ERODE_THRESHOLD = "110"
LOOP_FPS = 10
LOOP_HEIGHT = 200
POSE_HEIGHT = 360
STILL_HEIGHT = 200

CHARACTER_PROMPT = (
    "A friendly 3D-rendered robot mascot, full body, standing, one arm raised in a "
    "friendly wave. Glossy white rounded helmet with a large dark glass visor and two "
    "big glowing cyan pill-shaped eyes; small white headphone-like side pods with teal "
    "rings; a teal collar ring; smooth white capsule-shaped torso with a small teal "
    "diamond emblem; white arms with dark grey mitten hands; short white legs with teal "
    "soles. Soft studio lighting, subtle rim light, high detail, cinematic toy-render, "
    "centred full figure, transparent background, no text, no logos, no watermark, no "
    "extra objects."
)

POSE_PROMPT = (
    "Same character as the reference image - identical design, materials, colours and "
    "lighting. {pose} Full body, centred, transparent background, no text, no logos, "
    "no watermark, cinematic toy-render."
)

POSES = {
    "listening": (
        "The robot leans slightly forward with its head tilted, one mitten hand cupped "
        "beside its head pod as if listening intently; the visor eyes are bright and wide."
    ),
    "thinking": (
        "The robot looks up and to one side, one mitten hand raised to its chin in "
        "thought; the visor eyes are slightly narrowed."
    ),
    "speaking": (
        "The robot gestures with both mitten hands open towards the viewer, mid-sentence, "
        "friendly; the visor eyes are bright, with a soft cyan glow beneath them."
    ),
    "error": (
        "The robot shrugs with both hands palm-up and its head tilted to one side; the "
        "visor eyes are two thin worried lines."
    ),
}

MOTION_PROMPT = (
    "The cute white robot gently breathes and sways, its glowing cyan visor eyes blink "
    "slowly once, it keeps waving with a small friendly motion of its raised mitten "
    "hand. Static camera, no zoom, no crop, plain flat magenta background with no "
    "shadow or gradient, soft studio light, smooth seamless loop."
)

RUNNER = (
    "The page shows the artwork inside a small dark display frame (assets/css/pengu.scss "
    "`.pengu--art`), which also hides the faint keying halo the video's chroma "
    "subsampling leaves on light backgrounds. `idle-still.png` is the reduced-motion "
    "frame. The hand-drawn SVG mascot remains the fallback when the files are absent."
)


# -- OpenRouter -------------------------------------------------------------

def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        sys.exit("OPENROUTER_API_KEY is not set")
    return key


def call(path: str, payload: dict | None, method: str = "POST") -> dict:
    request = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + api_key(), "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"HTTP {error.code} from {path}: {error.read().decode(errors='replace')[:600]}")


def data_url(path: str) -> str:
    with open(path, "rb") as handle:
        return "data:image/png;base64," + base64.b64encode(handle.read()).decode()


def generate_image(prompt: str, out: str, references: list[str] | None = None) -> float:
    payload = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "aspect_ratio": IMAGE_ASPECT,
        "background": "transparent",
        "quality": "medium",
        "n": 1,
    }
    if references:
        payload["input_references"] = [
            {"type": "image_url", "image_url": {"url": data_url(ref)}} for ref in references
        ]
    result = call("/images", payload)
    with open(out, "wb") as handle:
        handle.write(base64.b64decode(result["data"][0]["b64_json"]))
    cost = float((result.get("usage") or {}).get("cost") or 0)
    print(f"  {os.path.basename(out)}  {os.path.getsize(out) // 1024} kB  ${cost:.3f}")
    return cost


def generate_video(first_frame: str, out: str) -> float:
    payload = {
        "model": VIDEO_MODEL,
        "prompt": MOTION_PROMPT,
        "aspect_ratio": VIDEO_ASPECT,
        "resolution": VIDEO_RESOLUTION,
        "duration": VIDEO_SECONDS,
        "generate_audio": False,
        "frame_images": [
            {"type": "image_url", "image_url": {"url": data_url(first_frame)}, "frame_type": "first_frame"}
        ],
    }
    job = call("/videos", payload)
    job_id = job["id"]
    print(f"  video job {job_id} ({job.get('status')})", flush=True)

    deadline = time.time() + 900
    while time.time() < deadline:
        status = call(f"/videos/{job_id}", None, method="GET")
        state = status.get("status")
        if state == "completed":
            request = urllib.request.Request(
                API + f"/videos/{job_id}/content", headers={"Authorization": "Bearer " + api_key()}
            )
            with urllib.request.urlopen(request, timeout=600) as response, open(out, "wb") as handle:
                while chunk := response.read(1 << 20):
                    handle.write(chunk)
            cost = float((status.get("usage") or {}).get("cost") or 0)
            print(f"  {os.path.basename(out)}  {os.path.getsize(out) // 1024} kB  ${cost:.3f}")
            return cost
        if state in ("failed", "cancelled", "expired"):
            raise SystemExit(f"video job {state}: {json.dumps(status)[:400]}")
        time.sleep(5)
    raise SystemExit("video job timed out")


# -- ffmpeg -----------------------------------------------------------------

def ffmpeg(*args: str) -> None:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    subprocess.run(command, check=True)


def character_stage() -> float:
    os.makedirs(WORK, exist_ok=True)
    print("character:")
    cost = generate_image(CHARACTER_PROMPT, os.path.join(WORK, "hero.png"))
    for name, pose in POSES.items():
        cost += generate_image(
            POSE_PROMPT.format(pose=pose), os.path.join(WORK, f"{name}.png"), [os.path.join(WORK, "hero.png")]
        )
    return cost


def video_stage() -> float:
    hero = os.path.join(WORK, "hero.png")
    if not os.path.exists(hero):
        sys.exit("run --character first (needs elixir/tmp/pengu-art/hero.png)")
    frame = os.path.join(WORK, "first-frame.png")
    ffmpeg(
        "-f", "lavfi", "-i", f"color=c={CHROMA}:s=720x1280",
        "-i", hero,
        "-filter_complex",
        "[1:v]scale=-1:1080:flags=lanczos[bot];[0:v][bot]overlay=(W-w)/2:(H-h)/2:format=auto",
        "-frames:v", "1", frame,
    )
    print("video:")
    return generate_video(frame, os.path.join(WORK, "idle-clip.mp4"))


def assemble_stage() -> None:
    if not os.path.isdir(WORK):
        sys.exit("nothing to assemble: run --character and --video first")
    os.makedirs(ART, exist_ok=True)
    hero = os.path.join(WORK, "hero.png")
    clip = os.path.join(WORK, "idle-clip.mp4")

    # The idle loop: 2.4 s forward + the same frames reversed, so the APNG loops
    # with no seam. Keying and a one-pixel alpha erosion happen before the scale.
    ffmpeg(
        "-i", clip, "-t", "2.4",
        "-filter_complex",
        (
            f"[0:v]crop=680:1030:20:130,"
            f"colorkey={CHROMA}:{KEY_SIMILARITY}:{KEY_BLEND},format=rgba,split[fg][a];"
            f"[a]alphaextract,gblur=sigma={ERODE_SIGMA},"
            f"lut=y='if(gt(val,{ERODE_THRESHOLD}),255,0)'[ae];"
            f"[fg][ae]alphamerge,fps={LOOP_FPS},scale=-1:{LOOP_HEIGHT}:flags=lanczos,format=rgba,"
            f"split[x][y];[y]reverse[r];[x][r]concat=n=2:v=1:a=0"
        ),
        "-plays", "0", "-c:v", "apng", "-f", "apng", os.path.join(ART, "idle.png"),
    )

    # The reduced-motion frame, and the four still poses (transparent PNGs).
    ffmpeg("-i", hero, "-vf", f"scale=-1:{STILL_HEIGHT}:flags=lanczos",
           "-c:v", "png", "-compression_level", "9", os.path.join(ART, "idle-still.png"))
    for name in POSES:
        ffmpeg("-i", os.path.join(WORK, f"{name}.png"),
               "-vf", f"scale=-1:{POSE_HEIGHT}:flags=lanczos,format=rgba",
               "-c:v", "png", "-compression_level", "9", os.path.join(ART, f"{name}.png"))

    write_manifest()
    print("assets:", ART)


def write_manifest() -> None:
    assets = {}
    for name in sorted(os.listdir(ART)):
        with open(os.path.join(ART, name), "rb") as handle:
            data = handle.read()
        assets[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16]}
    manifest = {
        "generated": time.strftime("%Y-%m-%d"),
        "provider": "OpenRouter",
        "models": {"character": IMAGE_MODEL, "idle_video": VIDEO_MODEL},
        "assets": assets,
        "pipeline": "scripts/pengu-art.py",
        "cost_note": "character ≈ US$0.01 per image, four pose edits, 4 s 720p video ≈ US$0.12",
        "notes": [
            "Poses are edits of the hero image passed as input_references, so the character stays consistent.",
            f"The idle loop is image-to-video on flat magenta ({CHROMA}), keyed back to alpha with ffmpeg colorkey plus a 1 px alpha erosion, then a boomerang and an animated PNG.",
            RUNNER,
        ],
    }
    with open(MANIFEST, "w") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")


def check() -> None:
    if not os.path.isdir(ART):
        print("no artwork installed at", ART)
        return
    for name in sorted(os.listdir(ART)):
        path = os.path.join(ART, name)
        print(f"{name:16} {os.path.getsize(path) // 1024:5} kB")


def main() -> None:
    args = set(sys.argv[1:])
    if not args or args == {"--check"}:
        check()
        return
    cost = 0.0
    if "--all" in args or "--character" in args:
        cost += character_stage()
    if "--all" in args or "--video" in args:
        cost += video_stage()
    if "--all" in args or "--assemble" in args:
        assemble_stage()
    if cost:
        print(f"spent about ${cost:.2f}")


if __name__ == "__main__":
    main()
