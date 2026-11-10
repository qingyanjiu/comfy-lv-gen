#!/usr/bin/env python3
# 直接生成 16:9 横屏关键帧：拼横屏底版(图1) + Qwen21 双图重绘。绕开管线 9:16 锁。
import os, sys, json
import pipeline as P
from PIL import Image

W, H = 1280, 720

def cover(im, w, h):
    im = im.convert("RGB")
    s = max(w / im.width, h / im.height)
    im = im.resize((max(w, round(im.width * s)), max(h, round(im.height * s))), Image.LANCZOS)
    l = (im.width - w) // 2; t = (im.height - h) // 2
    return im.crop((l, t, l + w, t + h))

def build_base():
    bg = Image.open(os.path.join(P.COMFY_INPUT, "tribute_bg.png"))
    person = Image.open(os.path.join(P.COMFY_INPUT, "tribute_person.png")).convert("RGB")
    base = cover(bg, W, H)
    ph = 690
    pw = round(person.width * ph / person.height)
    per = person.resize((pw, ph), Image.LANCZOS)
    # 左三分之一，脚接近底边
    x = 90
    y = H - ph + 8
    base.paste(per, (x, y))
    p = os.path.join(P.COMFY_INPUT, "tribute_169_base.png")
    base.save(p)
    return base

SCENE = "霓虹光带、悬浮全息面板、暗色反光地面、金属桁架"
ID = ("年轻亚裔女性，黑色中分长直发垂至胸前，白色短袖衬衫(领口微敞、内搭黑色蕾丝)、"
      "深蓝色百褶短裙、黑色乐福鞋")

FIRST_PROMPT = (
    "图1是人物与画面的位置构图：这位女性站在画面左侧。图2是赛博街景参考。"
    f"请重绘成一张 16:9 横构图的电影感剧照：{ID}。"
    "她位于画面左侧三分之一、全身中景、正面微侧朝向镜头、双手自然垂在身侧、表情平静双唇闭合；"
    f"画面右侧与背景是{SCENE}。身份、长相、发型、服装与图1逐项一致，只有她一个人，不生成第二个人；"
    "侧前方主光(画面右上方、自上而下)，冷调青蓝、暗部明显、低饱和电影感、背景略虚，"
    "皮肤细腻无颗粒噪点，画面里不要任何文字水印。"
)
LAST_PROMPT = (
    "图1是人物身份与构图参考，图2是本段首帧，是同一个镜头。"
    f"重绘成 16:9 横构图：{ID}。"
    "这一张画的是动作做完之后：右手从身侧明显抬起到胸口高度、手肘向外撑开、掌心转向镜头，"
    "同时上半身微向右转、头微微向右偏、重心前移一点，目光看向镜头(右手位置必须与首帧明显不同)。"
    "身份、发型、服装与图1逐项一致，只有她一个人；场景与图2的霓虹光带、悬浮全息面板、暗色反光地面、"
    "金属桁架保持一致；冷调青蓝、暗部明显、低饱和电影感，皮肤细腻无颗粒噪点，不要任何文字水印。"
)

def gen(idx, side):
    base = build_base()
    p1 = os.path.join(P.COMFY_INPUT, "tribute_169_base.png")
    pr = FIRST_PROMPT if side == "first" else LAST_PROMPT
    if side == "first":
        i2 = "tribute_bg.png"
    else:
        i2 = f"tribute169_s{idx:02d}_first.png"
    tag = f"tribute169_s{idx:02d}_{side}"
    seed = 20261109 + (0 if side == "first" else 7) + idx * 13
    outs = P.comfy_run(
        os.path.join(P.WF_DIR, "Qwen21-SceneRef916-V6.json"),
        {10: {0: os.path.basename(p1)}, 11: {0: i2}, 40: {0: pr}, 32: {0: tag}, 30: {0: seed}},
        timeout=1200,
    )
    src = os.path.join(P.COMFY_OUTPUT, outs["32"]["images"][-1]["filename"])
    dst = os.path.join(P.COMFY_INPUT, f"{tag}.png")
    Image.open(src).convert("RGB").save(dst)
    im = Image.open(dst)
    print(f"  saved {os.path.basename(dst)} {im.size} ratio={im.size[0]/im.size[1]:.3f}")
    return dst

if __name__ == "__main__":
    idx = int(sys.argv[1]); side = sys.argv[2]
    print(f">>> gen scene {idx} {side}")
    gen(idx, side)
    print(">>> done")
