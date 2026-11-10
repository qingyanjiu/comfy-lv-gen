#!/usr/bin/env python3
# 重出霓虹科幻城空镜：招牌只允许抽象几何光带，禁止任何字母/数字/字形。
import os, sys
import pipeline as P

W, H = 1376, 768

TAG = "tribute_neon_clean"
PROMPT = (
    "Wide cinematic shot of an empty futuristic city street at night. Architecture: sleek "
    "sci-fi towers, metal trusses, glass facades, wet reflective pavement with long light "
    "streaks, atmospheric haze, volumetric fog, deep depth of field, third-person game "
    "environment, photorealistic. Lighting: purple and cyan glow, but the light sources are "
    "PURE ABSTRACT GEOMETRY - unlit blank panels, bare glowing tubes, plain color-block "
    "bars, soft bokeh orbs, thin light strips running along walls and trusses. "
    "Absolutely no letters, no alphabet, no words, no numbers, no glyphs, no signage text, "
    "no logos, no symbols on any surface; every glowing panel is a plain empty shape with no "
    "markings. No people, no characters, no UI, no watermarks."
)

SEEDS = [770311, 770377, 770433]

if __name__ == "__main__":
    picks = sys.argv[1:] or [str(s) for s in SEEDS]
    for s in picks:
        tag = f"{TAG}_{s}"
        out = os.path.join(P.COMFY_INPUT, tag + ".png")
        print(f">>> seed {s} -> {tag} {W}x{H}", flush=True)
        P.gen_krea2_t2i(PROMPT, int(s), W, H, tag, out, steps=8)
        print(f"    saved {out}", flush=True)
    print(">>> done")
