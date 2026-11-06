"""Make a subject matte (white subject, black background) for a range of a video with SAM 2.1.

  python matte.py VIDEO --start 0 --end 55 --box 300,500,800,1900 [--point 540,900] [--neg 50,50] --out DIR

Frames are source-frame numbers, the same ones Resolve reports as source_start/source_end.
Prompts are in source pixels and apply to the first frame of the range (--prompt-frame to change).
Writes DIR/matte_00000.png ... (one per frame, for a Fusion Loader) and DIR/preview.jpg.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import cv2
import numpy as np
import torch

HERE = Path(__file__).resolve().parent
MODELS = {
    "tiny": ("sam2.1_hiera_tiny.pt", "configs/sam2.1/sam2.1_hiera_t.yaml"),
    "small": ("sam2.1_hiera_small.pt", "configs/sam2.1/sam2.1_hiera_s.yaml"),
    "base_plus": ("sam2.1_hiera_base_plus.pt", "configs/sam2.1/sam2.1_hiera_b+.yaml"),
    "large": ("sam2.1_hiera_large.pt", "configs/sam2.1/sam2.1_hiera_l.yaml"),
}


def xy(s):
    return [float(v) for v in s.split(",")]


def extract(video, start, end, dest):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", video,
         "-vf", f"select=between(n\\,{start}\\,{end})", "-fps_mode", "passthrough",
         "-q:v", "2", "-start_number", "0", str(dest / "%05d.jpg")],
        check=True,
    )
    n = len(list(dest.glob("*.jpg")))
    if n != end - start + 1:
        sys.exit(f"expected {end - start + 1} frames, ffmpeg gave {n}")
    return n


def preview(frames_dir, mattes, out_path, count=6):
    idx = np.linspace(0, len(mattes) - 1, min(count, len(mattes))).astype(int)
    tiles = []
    for i in idx:
        img = cv2.imread(str(frames_dir / f"{i:05d}.jpg"))
        m = mattes[i][..., None] / 255.0
        tint = (img * (1 - m) * 0.35 + (img * 0.5 + np.array([0, 255, 0]) * 0.5) * m).astype(np.uint8)
        h = 480
        tiles.append(cv2.resize(tint, (int(img.shape[1] * h / img.shape[0]), h)))
    cv2.imwrite(str(out_path), np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 85])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--box", type=xy, help="x0,y0,x1,y1 around the subject")
    ap.add_argument("--point", type=xy, action="append", default=[], help="x,y on the subject")
    ap.add_argument("--neg", type=xy, action="append", default=[], help="x,y NOT on the subject")
    ap.add_argument("--prompt-frame", type=int, default=0, help="offset into the range")
    ap.add_argument("--grow", type=int, default=2, help="dilate matte by N px")
    ap.add_argument("--feather", type=int, default=3, help="blur matte edge by N px")
    ap.add_argument("--model", default="small", choices=MODELS)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.box is None and not a.point:
        sys.exit("give --box and/or --point")

    from sam2.build_sam import build_sam2_video_predictor

    ckpt, cfg = MODELS[a.model]
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    predictor = build_sam2_video_predictor(cfg, str(HERE / "models" / ckpt), device=device)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("matte_*.png"):
        old.unlink()
    frames_dir = Path(tempfile.mkdtemp(prefix="matte_frames_"))
    try:
        n = extract(a.video, a.start, a.end, frames_dir)
        pts = a.point + a.neg
        kw = {}
        if pts:
            kw["points"] = np.array(pts, dtype=np.float32)
            kw["labels"] = np.array([1] * len(a.point) + [0] * len(a.neg), dtype=np.int32)
        if a.box is not None:
            kw["box"] = np.array(a.box, dtype=np.float32)

        mattes = [None] * n
        with torch.inference_mode():
            state = predictor.init_state(video_path=str(frames_dir), offload_video_to_cpu=True)
            predictor.add_new_points_or_box(state, frame_idx=a.prompt_frame, obj_id=1, **kw)
            runs = [False, True] if a.prompt_frame > 0 else [False]
            for reverse in runs:
                for fi, _, logits in predictor.propagate_in_video(state, reverse=reverse):
                    mattes[fi] = ((logits[0, 0] > 0).cpu().numpy() * 255).astype(np.uint8)

        k = np.ones((3, 3), np.uint8)
        for i, m in enumerate(mattes):
            if a.grow:
                m = cv2.dilate(m, k, iterations=a.grow)
            if a.feather:
                m = cv2.GaussianBlur(m, (0, 0), a.feather)
            mattes[i] = m
            cv2.imwrite(str(out / f"matte_{i:05d}.png"), m)
        preview(frames_dir, mattes, out / "preview.jpg")
        cover = [float((m > 127).mean()) for m in mattes]
        print(f"wrote {n} mattes to {out} | subject covers {min(cover):.0%}-{max(cover):.0%} of frame")
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
