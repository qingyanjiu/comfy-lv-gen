#!/usr/bin/env python3
# 生成两张 16:9 无人场景空镜（工业街区 / 霓虹科幻城），Krea2 t2i 1376x768。
import os, sys
import pipeline as P

W, H = 1376, 768

SCENES = {
    "industrial": (
        "Wide cinematic establishing shot of an empty industrial street: stacked shipping "
        "containers, corrugated metal warehouse walls, bare concrete ground, overhead pipes, "
        "overcast desaturated gray sky, cold steel-blue tones, faint haze, deep depth of field, "
        "third-person game environment, no people, no characters, no text, no UI, realistic.",
        "tribute_industrial_bg",
    ),
    "neon": (
        "Wide cinematic shot of an empty futuristic neon city street at night: glowing purple and "
        "cyan neon signage, holographic billboards, wet reflective pavement, sleek sci-fi towers "
        "and metal trusses, moody dark atmosphere, volumetric light, third-person game environment, "
        "no people, no characters, no text, no UI, realistic.",
        "tribute_neon_bg_169",
    ),
}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    for key, (prompt, tag) in SCENES.items():
        if which not in ("all", key):
            continue
        out = os.path.join(P.COMFY_INPUT, tag + ".png")
        print(f">>> {key} -> {tag} {W}x{H}")
        P.gen_krea2_t2i(prompt, 20261109 + (7 if key == "neon" else 0), W, H, tag, out, steps=8)
        print(f"    saved {out}")
    print(">>> done")
