#!/usr/bin/env python3
# 穿行横屏关键帧：远景/近景两档 × 2 场景，各一进一出（左->右 = 横移跟随）。
# 构图 = 人物贴横屏空镜底版(图1) + 场景/本段首帧(图2) -> Qwen21 双图重绘。
import os, sys
import pipeline as P
from PIL import Image

W, H = 1376, 768

def cover(im, w, h):
    im = im.convert("RGB")
    s = max(w / im.width, h / im.height)
    im = im.resize((max(w, round(im.width * s)), max(h, round(im.height * s))), Image.LANCZOS)
    l = (im.width - w) // 2; t = (im.height - h) // 2
    return im.crop((l, t, l + w, t + h))

ID = ("年轻亚裔女性，黑色中分长直发垂至胸前，白色短袖衬衫(领口微敞、内搭黑色蕾丝)、"
      "深蓝色百褶短裙、黑色乐福鞋")

IND = ("工业街区：叠放的集装箱、波纹金属仓库墙、湿混凝土路面、架空管道、阴天灰色天空、冷蓝色调")
NEO = ("霓虹科幻城：发光面板与光带只有纯几何形状(面板是空白平板、光带是不带字的细长灯管)、"
       "湿滑反光地面、科幻高楼、金属桁架、紫青色光")

SHOT_WIDE = "第三人称跟随视角、全身入画、平视机位、背景清晰有纵深"
SHOT_CLOSE = "第三人称跟随视角、更近的景别(人物明显更大、脚在画面之外)、略低机位仰拍、背景虚化"

# (tag, 空镜, x位置, 人物像素高, 场景描述, 动作描述, 景别描述, seed)
KFS = [
    ("tribute_walk_i1", "tribute_industrial_bg.png", 150, 620, IND,
     "在工业街区街道上正面朝镜头迈开步子向前走动，一条腿在前一条在后、双臂自然前后摆动，不是静止站立",
     SHOT_WIDE, 3101),
    ("tribute_walk_i2", "tribute_industrial_bg.png", 780, 620, IND,
     "在工业街区街道上继续向前迈步走动，身体朝前、手臂自然摆动、有行进中的动态",
     SHOT_WIDE, 3102),
    ("tribute_walk_i3", "tribute_industrial_bg.png", 170, 740, IND,
     "在工业街区街道上大步向前走动，一条腿抬起、手臂前后摆动，行进感强",
     SHOT_CLOSE, 3103),
    ("tribute_walk_i4", "tribute_industrial_bg.png", 760, 740, IND,
     "在工业街区街道上继续大步向前走动，手臂前后摆动，行进感强",
     SHOT_CLOSE, 3104),
    ("tribute_walk_n1", "tribute_neon_clean_770377.png", 150, 620, NEO,
     "在霓虹科幻街道上正面朝镜头小跑前进，一条腿抬起、发丝与衣摆有动态，不是静止站立",
     SHOT_WIDE, 3201),
    ("tribute_walk_n2", "tribute_neon_clean_770377.png", 780, 620, NEO,
     "在霓虹科幻街道上继续小跑前进，身体略前倾、衣摆飘动、有冲刺行进感",
     SHOT_WIDE, 3202),
    ("tribute_walk_n3", "tribute_neon_clean_770377.png", 170, 740, NEO,
     "在霓虹科幻街道上小跑前进，手臂摆动幅度更大、发丝被风吹起",
     SHOT_CLOSE, 3203),
    ("tribute_walk_n4", "tribute_neon_clean_770377.png", 760, 740, NEO,
     "在霓虹科幻街道上继续小跑前进，手臂摆动、发丝被风吹起",
     SHOT_CLOSE, 3204),
]

def build_base(bg_name, x, ph, tag):
    bg = Image.open(os.path.join(P.COMFY_INPUT, bg_name)).convert("RGB")
    base = cover(bg, W, H)
    person = Image.open(os.path.join(P.COMFY_INPUT, "tribute_person.png")).convert("RGB")
    pw = round(person.width * ph / person.height)
    per = person.resize((pw, ph), Image.LANCZOS)
    base.paste(per, (x, H - ph + 6))
    bp = f"walker_base_{tag}.png"
    base.save(os.path.join(P.COMFY_INPUT, bp))
    return bp

def gen(tag, bg, x, ph, sdesc, walk, shot, seed):
    base = build_base(bg, x, ph, tag)
    # 尾帧（tag 以 2/4 结尾）的图2 用「本段首帧」继承机位/光影：i2->i1, i4->i3, n2->n1, n4->n3
    i2 = bg
    if tag[-1] in "24":
        first = tag[:-1] + ("3" if tag[-1] == "4" else "1") + ".png"
        if os.path.exists(os.path.join(P.COMFY_INPUT, first)):
            i2 = first
    pos = "画面左侧三分之一" if x < 400 else "画面偏右"
    prompt = (
        f"图1是人物与画面的位置构图；图2是场景参考。重绘成一张 16:9 横构图的电影感游戏画面：{ID}。"
        f"她{walk}，位于{pos}。{shot}。"
        f"场景严格按图2复现（{sdesc}），环境一致、不新增物件，画面里只有她一个人，不生成第二个人。"
        "人物在画面中的大小与离镜头的距离必须与图1完全一致，不要放大、不要拉近、不要缩小。"
        "灯光面板与招牌只允许纯几何光带和空白色块，画面里不出现任何字母、数字、可读文字或标志。"
        "冷调电影感、有运动行进感，皮肤细腻无颗粒噪点，不要任何水印。"
    )
    outs = P.comfy_run(
        os.path.join(P.WF_DIR, "Qwen21-SceneRef916-V6.json"),
        {10: {0: base}, 11: {0: i2}, 40: {0: prompt}, 32: {0: tag}, 30: {0: seed}},
        timeout=1200,
    )
    src = os.path.join(P.COMFY_OUTPUT, outs["32"]["images"][-1]["filename"])
    dst = os.path.join(P.COMFY_INPUT, tag + ".png")
    Image.open(src).convert("RGB").save(dst)
    im = Image.open(dst)
    print(f"  saved {os.path.basename(dst)} {im.size} ratio={im.size[0]/im.size[1]:.3f}", flush=True)

if __name__ == "__main__":
    want = set(sys.argv[1:]) or {"all"}
    for tag, bg, x, ph, sdesc, walk, shot, seed in KFS:
        if "all" not in want and tag not in want:
            continue
        print(f">>> {tag}", flush=True)
        gen(tag, bg, x, ph, sdesc, walk, shot, seed)
    print(">>> done")
