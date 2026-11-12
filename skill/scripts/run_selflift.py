"""H3-SelfLift 单图参考 · 长视频默认链路

用户 2026-11-11 定：以后长视频都用 `H3-SelfLift_单图参考`（ref2va 直出）生成，
比 Director 首尾帧链路快；分辨率默认 **0.5 MP**（16:9 → 960×544）。

用一个工作流逐段出片：同一张人物参考图 + 每段自己的提示词 +
8 秒（192 帧 / 24fps）+ 各自 seed，最后 ffmpeg 按 **0.25 秒短叠化**拼接
（项目原来的接续方式，2026-11-11 用户定；`--join black` 可切回 0.8s 纯黑硬切，
`--join cut` 是无过场硬切）。

用法：
  python run_selflift.py ref                 # 用 Krea2 t2i 造人物参考图
  python run_selflift.py plate               # 造全片唯一无人空镜板
  python run_selflift.py seg 1               # 只跑第 1 段（逐段人在回路）
  python run_selflift.py all                 # 5 段顺序跑
  python run_selflift.py status              # 逐段状态 JSON（Director 面板轮询）
  python run_selflift.py set --seg 2 --seed 123 --body "..." --first-frame x.png
  python run_selflift.py merge               # 0.25s 短叠化 + 拼接
  python run_selflift.py probe               # 只打印每段时长/fps/帧数

逐段人在回路（2026-11-11）：Director 面板用 `status` / `set` / `seg N` 驱动。
段级 `first_frame_override` 可指定任意图片作本段首帧（优先于上段尾帧）；
`last_frame_override` 可把任意图片接到 238 节点的 `last_frame`（**可选、默认关闭**，
接上后 task_type=auto 会走 FL2VA/Hybrid，端点近似复现、运镜自由度下降，需重出本段才生效）。
重出某段或改了该段提示词，本段及之后各段会被标为 stale，必须按序重跑（链路串行）。
"""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

SK = "D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/scripts"
sys.path.insert(0, SK)
import pipeline as P  # noqa: E402

DEFAULT_CFG = "D:/Comfy-Desktop/output/rooftop-dusk/segments.json"
DEFAULT_WORKFLOW = (
    "D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/assets/H3-SelfLift_单图参考.base.json"
)


def set_root(cfg_path):
    """一个片子一个目录：分镜 json 在哪，产物就写在哪。--cfg 即可开新片。"""
    global ROOT, CFG_PATH, SEG_DIR, REF_DIR, FINAL_DIR, QA_DIR
    CFG_PATH = os.path.abspath(cfg_path)
    ROOT = os.path.dirname(CFG_PATH)
    SEG_DIR = os.path.join(ROOT, "segments")
    REF_DIR = os.path.join(ROOT, "refs")
    FINAL_DIR = os.path.join(ROOT, "final")
    QA_DIR = os.path.join(ROOT, "qa")


set_root(DEFAULT_CFG)

N_IMAGE = 150
N_RES = 115
N_DUR = 132
N_PROMPT = 232
N_SAMPLER = 235
N_VIDEO = 180
N_CLIP = 128
N_VIDEO_VAE = 227
N_AUDIO_VAE = 184
N_FIRST = 151   # LoadImage：上一段尾帧 → first_frame
N_PLATE = 152   # LoadImage：全片唯一无人空镜板 → ref_images.ref_image_<人数>
N_LAST = 153    # LoadImage：可选的本段尾帧锁 → last_frame（默认不接）
N_PEOPLE_BASE = 160  # 第 2、3… 个人物参考的 LoadImage 起始节点号（160/161/…）
DEFAULT_MEGAPIXELS = 0.5  # 用户定：长视频默认 0.5MP，别再沿用旧值 0.3/0.98
DEFAULT_ASPECT_RATIO = "16:9 (Widescreen)"
# 与 ComfyUI ResolutionSelector 节点的 options 保持一致，别自己造字符串
ASPECT_RATIO_CHOICES = (
    "1:1 (Square)", "2:3 (Portrait Photo)", "3:2 (Photo)",
    "3:4 (Portrait Standard)", "4:3 (Standard)",
    "9:16 (Portrait Widescreen)", "16:9 (Widescreen)", "21:9 (Ultrawide)",
)
MAX_PEOPLE = 8  # 节点上限 9 个 ref_image 槽，留 1 个给空镜板

REF_PROMPT = (
    "写实电影风格的人物设定参考图。一位 25 岁左右的东亚年轻女性，正面站立，"
    "从头顶到鞋底完整入画，身体正对镜头，双臂自然垂在身侧，站姿端正放松。\n"
    "黑色长直发自然垂落到肩胛下方，发丝柔顺、有细微层次和自然高光。"
    "深灰色简约长款外套敞着，内搭浅灰色针织衫，深色长裤，无配饰。\n"
    "清秀安静的面容，五官清晰对称，皮肤有真实质感与细微微孔，不做磨皮不过度锐化，"
    "眼神平静看向镜头，表情自然不带笑意。\n"
    "柔和均匀的棚拍布光，中性浅灰色无缝背景，画面里没有任何道具、家具、文字、标志或其他人。\n"
    "写实摄影，85mm 镜头，浅景深，真实肤色，细腻胶片颗粒。"
)

PLATE_PROMPT = (
    "写实电影剧照，城市高层建筑天台的**无人空镜**，画面里完全没有人物、人影、"
    "人体局部、剪影或任何生物，没有任何文字、标志、水印。\n"
    "画面左侧立着一扇关闭的斑驳生锈铁门，门框与铰链带锈迹；浅灰色粗糙水泥地面，"
    "地面有几处浅浅的积水；右前方与画面中部横贯一道灰白色金属栏杆，"
    "栏杆外是密集的城市楼群，一层层延伸到地平线。\n"
    "天空占画面上方约三分之一，橙红色夕阳沉在远处建筑之间，"
    "云层被染成柔和的琥珀色与暗玫瑰色，整体是安静温暖的暮色。\n"
    "写实摄影，35mm 电影镜头，自然空间纵深，低饱和电影调色，细腻胶片颗粒，"
    "柔和自然光影，没有过度锐化。"
)


def load_cfg(path=None):
    with open(path or CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def person_node(i):
    """第 i 个人物参考（0-based）的 LoadImage 节点号。0 号是 base.json 自带的 150。"""
    return N_IMAGE if i == 0 else N_PEOPLE_BASE + i - 1


def people_refs(cfg, seg=None):
    """人物参考图列表，顺序 = Picture 编号顺序。

    新写法（多人）：cfg["ref_images"] = [{"path","comfy_name","label"}, …]
    老写法（单人）：cfg["ref_image"] + cfg["comfy_ref_name"]
    两者都支持；有空镜板时它永远排在最后一张，不占人物位。

    段级覆盖（2026-11-12）：seg["ref_images"] 写了就用本段这份列表，可以做到
    「只有看得到脚的段才挂鞋款参考图」。没写的段自动回落到 cfg 级。
    to_api 按 wf 里实际 LoadImage 节点数接线，段间槽位数不同也没问题；
    提示词请用 {pic:} 占位符，只在某些段才有的媒体用 {pic?:} 让整行自动消失。
    """
    arr = (seg or {}).get("ref_images") if seg is not None else None
    if arr is None:
        arr = cfg.get("ref_images")
    out = []
    if isinstance(arr, list):
        for i, it in enumerate(arr):
            if isinstance(it, str):
                p, name, label = it, "", ""
            elif isinstance(it, dict):
                p = it.get("path") or it.get("image") or ""
                name = it.get("comfy_name") or it.get("name") or ""
                label = it.get("label") or ""
            else:
                continue
            if not p:
                continue
            if not name:
                ext = os.path.splitext(p)[1] or ".png"
                name = f"{cfg.get('prefix', 'selflift')}_ref{i}{ext}"
            out.append({"path": p, "comfy_name": name,
                        "label": label or f"人物 {chr(65 + i)}"})
    if out:
        return out[:MAX_PEOPLE]
    p = cfg.get("ref_image")
    if p:
        return [{"path": p,
                 "comfy_name": cfg.get("comfy_ref_name") or os.path.basename(p),
                 "label": "人物 A"}]
    return []


def count_people_in_wf(wf):
    """从 UI wf 里数接了几个 ref_image 槽（150 恒定在，160… 连续递增）。"""
    ids = {n["id"] for n in wf["nodes"]}
    c = 1 if N_IMAGE in ids else 0
    while (N_PEOPLE_BASE + c - 1) in ids:
        c += 1
    return c


def normalize(wf):
    """VHS 节点的 widgets_values 在 UI 里是 dict，build_prompt 只认 list，
    按 object_info 的 required 顺序摊平（只保留 API 层认识的 6 个）。"""
    for n in wf["nodes"]:
        if n["type"] == "VHS_VideoCombine" and isinstance(n.get("widgets_values"), dict):
            d = n["widgets_values"]
            n["widgets_values"] = [
                float(d.get("frame_rate", 24)),
                int(d.get("loop_count", 0)),
                str(d.get("filename_prefix", "video")),
                str(d.get("format", "video/h264-mp4")),
                bool(d.get("pingpong", False)),
                bool(d.get("save_output", True)),
            ]
    return wf


PIC_TOKEN = re.compile(r"\{pic:([A-Za-z]+|\d+)\}")
PIC_OPT_TOKEN = re.compile(r"\{pic\?:([A-Za-z]+|\d+)\}")


def pic_numbers(cfg, seg):
    """本段各媒体实际对应的 Picture 编号（与 conditioning.py 的 push 顺序一致）。

    顺序：first_frame → last_frame → 人物 1..N → 空镜板。
    返回 {"first":1, "last":2, "1":3, "2":4, "plate":5} 这样的映射（只含本段真接了的）。
    """
    m = {}
    n = 0
    if seg.get("first_frame"):
        n += 1
        m["first"] = n
    if seg.get("last_frame_override"):
        n += 1
        m["last"] = n
    for i in range(len(people_refs(cfg, seg))):
        n += 1
        m[str(i + 1)] = n
    plate = seg.get("bg_plate", cfg.get("bg_plate"))
    if plate:
        n += 1
        m["plate"] = n
    return m


def expand_pic_tags(text, mapping, where="提示词"):
    """把 `{pic:1}` / `{pic:plate}` / `{pic:first}` 换成本段真实的 `<Picture N>` 编号。

    这样 head 只写一遍就能全片通用：第 1 段没有首帧 → `{pic:1}`=1；链式段接了首帧
    → `{pic:1}` 自动变成 2。

    带问号的 `{pic?:first}` 是**可选**写法：本段没接这个媒体时，**整行都去掉**。
    全局 head 里写「<Picture {pic?:first}> 是本段首帧…」这种句子时，第 1 段（无首帧）
    就不会留下半句空话，链式段才出现。

    引用了本段没接、又没打问号的媒体会直接报错，而不是留个错编号给 strict_prompt_tags
    去炸（或静默把身份锚换成别的图）。
    """
    if not text or "{pic" not in text:
        return text

    if "{pic?:" in text:
        kept = []
        for line in text.split("\n"):
            keys = PIC_OPT_TOKEN.findall(line)
            if any(k not in mapping for k in keys):
                continue          # 这一行依赖本段没接的媒体 → 整行不要
            kept.append(line)
        text = "\n".join(kept)
    text = PIC_OPT_TOKEN.sub(lambda mo: str(mapping[mo.group(1)]), text)

    missing = sorted({k for k in PIC_TOKEN.findall(text) if k not in mapping})
    if missing:
        have = "、".join(f"{{pic:{k}}}=P{v}" for k, v in sorted(mapping.items())) or "无"
        P.die(
            f"{where} 里引用了本段没有连接的媒体："
            + "、".join(f"{{pic:{k}}}" for k in missing)
            + f"\n    本段实际接线：{have}"
            + "\n    （要接空镜板就在 cfg 里配 bg_plate；要引用某个人物检查 ref_images 数量；"
              "如果是「只在某些段才有」的句子，改用 {pic?:key} 让它整行自动消失）"
        )
    return PIC_TOKEN.sub(lambda mo: str(mapping[mo.group(1)]), text)


def seg_prompt(cfg, seg):
    """拼出本段完整提示词，并把 `{pic:N}` 展开成真实 Picture 编号。

    `<Picture N>` 是**按接线顺序**编号的（first_frame → last_frame → 人物 → 空镜板），
    所以第 1 段和链式段的编号天然不同。head 里用 `{pic:1}` / `{pic:plate}` 写一次，
    这里按本段实际接线自动换算，就**不用逐段重写 head**了。
    段级 `seg["head"]` 仍是整段覆盖（不是追加），个别段要特殊强调时才用。
    """
    m = pic_numbers(cfg, seg)
    head = expand_pic_tags(seg.get("head") or cfg.get("head") or "", m, "head")
    tail = expand_pic_tags(seg.get("tail") or cfg.get("tail") or "", m, "tail")
    body = expand_pic_tags(seg.get("body") or "", m, "segments[].body")
    # {pic?:first} 在首段整行删掉后会留下前导空行，strip 一下别把空行喂给文本编码器
    return "\n\n".join([p.strip() for p in (head, body, tail) if p and p.strip()])


def add_load_image(wf, node_id, filename):
    """复制节点 150（人物参考图）的结构，挂一张新的 LoadImage。"""
    if any(n["id"] == node_id for n in wf["nodes"]):
        for n in wf["nodes"]:
            if n["id"] == node_id:
                n["widgets_values"][0] = filename
        return
    base = next(n for n in wf["nodes"] if n["id"] == N_IMAGE)
    node = copy.deepcopy(base)
    node["id"] = node_id
    node["widgets_values"][0] = filename
    wf["nodes"].append(node)


def build_wf(cfg, seg, out_path):
    with open(cfg["workflow"], encoding="utf-8") as f:
        wf = normalize(json.load(f))
    nodes = {n["id"]: n for n in wf["nodes"]}
    people = people_refs(cfg, seg)
    nodes[N_IMAGE]["widgets_values"][0] = (people[0]["comfy_name"] if people
                                           else cfg.get("comfy_ref_name") or "image.png")
    if cfg.get("clip_name"):
        nodes[N_CLIP]["widgets_values"][0] = cfg["clip_name"]
    nodes[N_PROMPT]["widgets_values"][0] = seg_prompt(cfg, seg)
    nodes[N_DUR]["widgets_values"][0] = float(cfg["seconds"])
    nodes[N_RES]["widgets_values"][0] = cfg.get("aspect_ratio") or DEFAULT_ASPECT_RATIO
    nodes[N_RES]["widgets_values"][1] = float(cfg.get("megapixels", DEFAULT_MEGAPIXELS))
    nodes[N_SAMPLER]["widgets_values"][0] = int(seg["seed"])
    nodes[N_VIDEO]["widgets_values"][0] = float(cfg["fps"])
    nodes[N_VIDEO]["widgets_values"][2] = f"{cfg['prefix']}_{seg['id']}"
    for i, pp in enumerate(people[1:], start=1):
        add_load_image(wf, person_node(i), pp["comfy_name"])
    plate = seg.get("bg_plate", cfg.get("bg_plate"))
    if plate:
        add_load_image(wf, N_PLATE, plate)
    if seg.get("first_frame"):
        add_load_image(wf, N_FIRST, seg["first_frame"])
    if seg.get("last_frame_override"):
        add_load_image(wf, N_LAST, seg["last_frame_override"])
    global REF_VIDEO_PATH, REF_VIDEO_SECONDS
    REF_VIDEO_PATH = seg.get("ref_video") or cfg.get("ref_video") or None
    REF_VIDEO_SECONDS = seg.get("ref_video_seconds") or None
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False)
    return out_path


def check_graph(cfg, seg, verbose=True):
    tmp = os.path.join(QA_DIR, f"_graph_{seg['id']}.json")
    build_wf(cfg, seg, tmp)
    wf = json.load(open(tmp, encoding="utf-8"))
    prompt = to_api(wf)
    a = prompt[str(N_IMAGE)]["inputs"]["image"]
    b = prompt[str(N_SAMPLER)]["inputs"]
    c = prompt[str(N_VIDEO)]["inputs"]
    d = prompt["238"]["inputs"]
    if verbose:
        print(f"    image={a!r} seed={b.get('seed')} cfg={b.get('cfg')} "
              f"trans={b.get('transition_step')} rho={b.get('rho')}", flush=True)
        print(f"    video: fps={c.get('frame_rate')} prefix={c.get('filename_prefix')!r} "
              f"fmt={c.get('format')} audio={c.get('audio')}", flush=True)
        print(f"    238: {d.get('width')}x{d.get('height')} len={d.get('length')} "
              f"task={d.get('task_type')} audio={d.get('audio_mode')}", flush=True)
        print(f"    image={a!r} seed={b.get('seed')} cfg={b.get('cfg')} "
              f"trans={b.get('transition_step')} rho={b.get('rho')}", flush=True)
        refs = sorted((k for k in d if k.startswith("ref_images.")),
                      key=lambda k: int(k.rsplit("_", 1)[-1]))
        print("    ref_images: " + " | ".join(
            f"{k.rsplit('.', 1)[-1]}<-node{v[0]}" for k, v in
            ((k, d[k]) for k in refs)), flush=True)
        print(f"    接续: first_frame={d.get('first_frame')} "
              f"last_frame={d.get('last_frame')}", flush=True)
        res = prompt[str(N_RES)]["inputs"]
        print(f"    resolution: {res.get('aspect_ratio')} / {res.get('megapixels')} MP",
              flush=True)
        print(f"    prompt chars={len(seg_prompt(cfg, seg))}", flush=True)
    return prompt


def extract_last_frame(video, out_png):
    """抽最后一帧，给下一段当 first_frame（黑场两侧画面由此接上）。"""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.042", "-i", video,
                    "-frames:v", "1", "-update", "1", out_png], check=True)
    if not os.path.exists(out_png):
        P.die(f"尾帧抽取失败：{video}")
    return out_png


def extract_first_frame(video, out_png):
    """抽第 0 帧给面板展示（真实首帧，不是参考图）。"""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-vf",
                    "select='eq(n,0)'", "-frames:v", "1", "-update", "1", out_png],
                   check=True)
    if not os.path.exists(out_png):
        P.die(f"首帧抽取失败：{video}")
    return out_png


# ---------------------------------------------------------------------------
# 片名 / 片编号（2026-11-12）
# 文件夹末尾的 MMDD_HHMMSS 就是这部片的稳定编号。以前两部片都叫
# `segments-template`、下拉里只能靠「更新时间」猜是哪部；现在页面统一显示
# 「片名 · #编号 · 文件夹」，编号 = 目录名后缀 = prefix 后缀，三者一一对应。
# ---------------------------------------------------------------------------
FILM_ID_RE = re.compile(r"(\d{4}_\d{6})$")
PLACEHOLDER_TITLES = {"", "film", "segments-template", "segments_template",
                      "未命名长片", "未命名", "untitled"}


def film_id_of(folder, cfg=None):
    """片编号 = 文件夹末尾的时间编号；历史片没编号就用 prefix 里的 / 目录名。"""
    m = FILM_ID_RE.search(str(folder or ""))
    if m:
        return m.group(1)
    m = FILM_ID_RE.search(str((cfg or {}).get("prefix") or ""))
    if m:
        return m.group(1)
    return str((cfg or {}).get("film_id") or folder or "").strip()


def film_display(title, film_id):
    """占位标题（模板默认名）显示成「未命名 #编号」，保证每部片一眼可分。"""
    t = str(title or "").strip()
    if t.lower() in PLACEHOLDER_TITLES:
        return f"未命名 #{film_id}" if film_id else "未命名"
    return t


def film_label(title, film_id, updated_text, done, segments, has_final):
    """下拉选项文字：名字与编号绑在一起显示，查文件夹时能对上。"""
    disp = film_display(title, film_id)
    parts = [disp]
    if (film_id and re.fullmatch(r"\d{4}_\d{6}", film_id)
            and f"#{film_id}" not in disp):
        parts.append(f"#{film_id}")
    parts += [updated_text, f"{done}/{segments}段"]
    if has_final:
        parts.append("✓成片")
    return " · ".join(str(p) for p in parts if p)


def list_films(limit=80):
    """扫描输出根下所有片（含 segments.json 的子目录），按「最近动过」倒序。

    更新时间取 cfg / segments/*.mp4 / final/*.mp4 里最新的 mtime —— 只改提示词
    和跑完一段都会反映出来，比单看 segments.json 更符合「我刚动过哪部片」。
    status 每 3 秒被轮询一次，这里只读 json + stat，不做任何写操作。
    """
    root = os.environ.get("H3_FILMS_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(DEFAULT_CFG)))
    out = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return out
    for ent in entries:
        try:
            if not ent.is_dir():
                continue
            cfg_path = os.path.join(ent.path, "segments.json")
            if not os.path.isfile(cfg_path):
                continue
            with open(cfg_path, encoding="utf-8") as f:
                c = json.load(f)
            segs = c.get("segments") or []
            prefix = str(c.get("prefix") or "")
            ts = os.path.getmtime(cfg_path)
            done = 0
            sd = os.path.join(ent.path, "segments")
            for sg in segs:
                f = os.path.join(sd, f"{prefix}_{sg.get('id', '')}.mp4")
                if os.path.isfile(f):
                    done += 1
                    ts = max(ts, os.path.getmtime(f))
            has_final = False
            fd = os.path.join(ent.path, "final")
            if os.path.isdir(fd):
                for f in os.scandir(fd):
                    try:
                        if f.is_file():
                            ts = max(ts, f.stat().st_mtime)
                            if f.name.lower().endswith(".mp4"):
                                has_final = True
                    except OSError:
                        pass
            fid = film_id_of(ent.name, c)
            title = str(c.get("title") or ent.name)
            updated_text = time.strftime("%m-%d %H:%M", time.localtime(ts))
            out.append({
                "path": os.path.abspath(cfg_path),
                "dir": ent.name,
                "title": title,
                "film_id": fid,
                "display": film_display(title, fid),
                "label": film_label(title, fid, updated_text, done, len(segs),
                                    has_final),
                "search": " ".join((title, fid, ent.name, prefix)).lower(),
                "prefix": prefix,
                "updated": ts,
                "updated_text": updated_text,
                "segments": len(segs),
                "done": done,
                "has_final": has_final,
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out[:limit]


def seg_status(cfg):
    """逐段状态 JSON：Director 面板轮询用，不改动任何文件。

    stale = 本段提示词/seed 被改过，或上段尾帧比本段视频新（first_frame 已失效）。
    stale 会自动向后传染，因为 first_frame 是串行依赖。
    """
    segs = []
    for i, sg in enumerate(cfg["segments"]):
        vid = os.path.join(SEG_DIR, f"{cfg['prefix']}_{sg['id']}.mp4")
        first = os.path.join(REF_DIR, f"{cfg['prefix']}_s{i+1:02d}_first.png")
        last = os.path.join(REF_DIR, f"{cfg['prefix']}_s{i+1:02d}_last.png")
        done = os.path.exists(vid)
        segs.append({
            "index": i + 1,
            "id": sg["id"],
            "title": sg.get("title", ""),
            "seed": sg.get("seed"),
            "body": sg.get("body", ""),
            "first_frame_override": sg.get("first_frame_override"),
            "last_frame_override": sg.get("last_frame_override"),
            "done": done,
            "stale": False,
            "video": vid if done else None,
            "first_frame": first if os.path.exists(first) else None,
            "last_frame": last if os.path.exists(last) else None,
            "link": sg.get("link"),
            "head_override": sg.get("head") or "",
            "tail_override": sg.get("tail") or "",
            "ref_images": [{"path": p["path"], "comfy_name": p["comfy_name"],
                            "label": p["label"], "exists": os.path.exists(p["path"])}
                           for p in people_refs(cfg, sg)],
            "refs_override": "ref_images" in sg,
            "bg_plate": sg.get("bg_plate", cfg.get("bg_plate")) or "",
            "bg_plate_path": sg.get("bg_plate_path", cfg.get("bg_plate_path")) or "",
            "plate_override": "bg_plate" in sg,
            "mtime": os.path.getmtime(vid) if done else 0,
        })
    stale_from = None
    for i, item in enumerate(segs):
        if not item["done"]:
            continue
        edited = max(cfg["segments"][i].get("updated_at", 0),
                     cfg.get("head_updated_at", 0),
                     cfg.get("res_updated_at", 0)) > item["mtime"] + 1
        prev = segs[i-1]["last_frame"] if i > 0 else None
        # 转场模式(transition)的段首帧独立生成，上游尾帧变化不会让它失效
        lmode = (cfg["segments"][i].get("link") or {}).get("mode", "chain")
        superseded = bool(prev and lmode != "transition"
                         and os.path.getmtime(prev) > item["mtime"] + 1)
        if edited or superseded:
            stale_from = i
            break
    if stale_from is not None:
        for j in range(stale_from, len(segs)):
            segs[j]["stale"] = True
    finals = []
    if os.path.isdir(FINAL_DIR):
        finals = sorted(f for f in os.listdir(FINAL_DIR) if f.lower().endswith(".mp4"))
    folder = os.path.basename(ROOT)
    fid = film_id_of(folder, cfg)
    return {
        "title": cfg.get("title", ""),
        "film_id": fid,
        "film_display": film_display(cfg.get("title"), fid),
        "folder": folder,
        "cfg": CFG_PATH,
        "prefix": cfg.get("prefix", ""),
        "join_mode": cfg.get("join_mode", "auto"),
        "join_updated_at": cfg.get("join_updated_at", 0),
        "films": list_films(),
        "megapixels": float(cfg.get("megapixels", DEFAULT_MEGAPIXELS)),
        "aspect_ratio": cfg.get("aspect_ratio") or DEFAULT_ASPECT_RATIO,
        "chain_first_frame": bool(cfg.get("chain_first_frame")),
        "head": cfg.get("head", ""),
        "tail": cfg.get("tail", ""),
        "ref_images": [{"path": p["path"], "comfy_name": p["comfy_name"],
                        "label": p["label"], "exists": os.path.exists(p["path"])}
                       for p in people_refs(cfg)],
        "bg_plate_path": cfg.get("bg_plate_path") or "",
        "bg_plate_exists": bool(cfg.get("bg_plate")
                                and os.path.exists(cfg.get("bg_plate_path")
                                                   or os.path.join(REF_DIR, "bg_plate.png"))),
        "segments": segs,
        "finals": finals,
    }


def cmd_set(cfg, args):
    """面板改 seed / 提示词 / 首尾帧 / 承接 / 全局 head+tail：直接写回分镜 json。

    全局字段（`--head` / `--tail` / `--megapixels` / `--aspect-ratio` / `--join`）不要求 `--seg`。
    `--join` 只写 cfg["join_mode"]，不打 updated_at —— 拼接方式变了不需要重出画面。
    head/tail 改动打 `head_updated_at`，分辨率改动打 `res_updated_at`；两者都会让
    所有已完成段被判 stale —— 全局参数一变，全片画面都得重出，不能只标某一段。
    """
    changed = []

    def _global_text(value):
        return "" if str(value).strip().lower() in ("", "clear", "none") else value

    if args.head is not None:
        cfg["head"] = _global_text(args.head)
        changed.append("全局 head")
    if args.tail is not None:
        cfg["tail"] = _global_text(args.tail)
        changed.append("全局 tail")
    if args.head is not None or args.tail is not None:
        cfg["head_updated_at"] = time.time()

    if args.megapixels is not None:
        mp = float(args.megapixels)
        if not 0.1 <= mp <= 2.0:
            P.die(f"megapixels 必须在 0.1~2.0 之间（8GB 显存建议 0.5 左右）：{mp}")
        cfg["megapixels"] = round(mp, 3)
        changed.append(f"megapixels={cfg['megapixels']}")
    if args.aspect_ratio is not None:
        ar = str(args.aspect_ratio).strip()
        if ar not in ASPECT_RATIO_CHOICES:
            P.die(f"未知画幅比例：{ar}\n    可选：" + " / ".join(ASPECT_RATIO_CHOICES))
        cfg["aspect_ratio"] = ar
        changed.append(f"aspect_ratio={ar}")
    if args.megapixels is not None or args.aspect_ratio is not None:
        cfg["res_updated_at"] = time.time()

    join = getattr(args, "join", None)   # 旧调用方/单测手工构造的 Namespace 可能没这个字段
    if join is not None:
        if join not in ("black", "dissolve", "cut", "auto"):
            P.die(f"未知 join：{join}（应为 black / dissolve / cut / auto）")
        cfg["join_mode"] = join
        changed.append(f"join_mode={join}")

    if args.ref_video is not None:
        rv = str(args.ref_video).strip()
        if rv.lower() in ("", "none", "clear"):
            cfg.pop("ref_video", None)
            changed.append("ref_video=移除")
        else:
            if not os.path.isfile(rv):
                P.die(f"ref_video 文件不存在：{rv}")
            dst_name = f"{cfg['prefix']}_refvideo.mp4"
            shutil.copy(rv, os.path.join(P.COMFY_INPUT, dst_name))
            cfg["ref_video"] = dst_name
            changed.append(f"ref_video={dst_name}（已复制进 ComfyUI input）")

    idx = args.seg - 1
    seg_args = (args.seed, args.body, args.first_frame, args.last_frame,
                args.link_mode, args.link_transition, args.seg_head, args.seg_tail)
    if any(v is not None for v in seg_args):
        if not (0 <= idx < len(cfg["segments"])):
            P.die(f"段号越界：{args.seg}（共 {len(cfg['segments'])} 段）")
        sg = cfg["segments"][idx]
        seg_changed = []
        if args.seed is not None:
            sg["seed"] = int(args.seed)
            seg_changed.append(f"seed={sg['seed']}")
        if args.body is not None:
            sg["body"] = args.body
            seg_changed.append("body")
        if args.seg_head is not None:
            head_text = _global_text(args.seg_head)
            if head_text:
                sg["head"] = head_text
                seg_changed.append("段级 head（整段覆盖全局）")
            else:
                sg.pop("head", None)
                seg_changed.append("段级 head=回落到全局")
        if args.seg_tail is not None:
            tail_text = _global_text(args.seg_tail)
            if tail_text:
                sg["tail"] = tail_text
                seg_changed.append("段级 tail（覆盖全局画风）")
            else:
                sg.pop("tail", None)
                seg_changed.append("段级 tail=回落到全局")
        if args.first_frame is not None:
            if args.first_frame.strip().lower() in ("", "none", "auto", "clear"):
                sg.pop("first_frame_override", None)
                seg_changed.append("first_frame=自动承接上段尾帧")
            else:
                sg["first_frame_override"] = args.first_frame.strip()
                seg_changed.append(f"first_frame={sg['first_frame_override']}")
        if args.last_frame is not None:
            if args.last_frame.strip().lower() in ("", "none", "auto", "clear"):
                sg.pop("last_frame_override", None)
                seg_changed.append("last_frame=解除锁定")
            else:
                sg["last_frame_override"] = args.last_frame.strip()
                seg_changed.append(f"last_frame={sg['last_frame_override']}（需重出本段）")
        if args.link_mode is not None:
            if args.link_mode not in ("chain", "transition"):
                P.die(f"未知 link_mode：{args.link_mode}（应为 chain / transition）")
            sg.setdefault("link", {})
            sg["link"]["mode"] = args.link_mode
            seg_changed.append(f"link.mode={args.link_mode}")
        if args.link_transition is not None:
            if args.link_transition not in ("dissolve", "black", "cut"):
                P.die(f"未知 link.transition：{args.link_transition}（应为 dissolve / black / cut）")
            sg.setdefault("link", {})
            sg["link"]["transition"] = args.link_transition
            seg_changed.append(f"link.transition={args.link_transition}")
        if seg_changed:
            sg["updated_at"] = time.time()
            changed += [f"段{args.seg} {c}" for c in seg_changed]

    if changed:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"已更新：{', '.join(changed) or '无改动'}", flush=True)


def run_seg(cfg, idx):
    os.makedirs(SEG_DIR, exist_ok=True)
    os.makedirs(REF_DIR, exist_ok=True)
    seg = dict(cfg["segments"][idx])
    link = cfg["segments"][idx].get("link") or {}
    lmode = link.get("mode", "chain")
    if seg.get("first_frame_override"):
        seg["first_frame"] = seg["first_frame_override"]
        print(f"    首帧覆盖（面板指定）：{seg['first_frame']}", flush=True)
    elif idx > 0 and lmode != "transition":
        prev_name = f"{cfg['prefix']}_s{idx:02d}_last.png"
        if os.path.exists(os.path.join(P.COMFY_INPUT, prev_name)):
            seg["first_frame"] = prev_name
            print(f"    首帧承接上段尾帧：{prev_name}", flush=True)
        else:
            print(f"    [警告] 缺上一段尾帧 {prev_name}，本段不接 first_frame", flush=True)
    elif idx > 0 and lmode == "transition":
        print(f"    转场模式：本段独立生成（不承接上段尾帧）", flush=True)
    print(f"\n===== 段 {idx+1}/{len(cfg['segments'])}  {seg['id']} {seg['title']}  seed={seg['seed']} =====", flush=True)
    api = check_graph(cfg, seg)
    if seg.get("last_frame_override"):
        print(f"    [实验] 尾帧锁定：{seg['last_frame_override']} "
              f"（task_type=auto 会切 FL2VA/Hybrid，运镜自由度下降）", flush=True)
    print(f"    prompt 全文 {len(seg_prompt(cfg, seg))} 字", flush=True)
    P.comfy_free()
    t0 = time.time()
    outs = submit(api)
    dt = time.time() - t0
    item = None
    for nid, o in outs.items():
        for key in ("gifs", "videos", "images"):
            for it in o.get(key) or []:
                if str(it.get("filename", "")).lower().endswith((".mp4", ".webm")):
                    item = it
                    break
            if item:
                break
        if item:
            break
    if not item:
        P.die(f"段 {seg['id']} 没找到视频产物: {json.dumps(outs, ensure_ascii=False)[:600]}")
    src = os.path.join(P.COMFY_OUTPUT, item.get("subfolder", ""), item["filename"])
    dst = os.path.join(SEG_DIR, f"{cfg['prefix']}_{seg['id']}.mp4")
    shutil.copy(src, dst)
    print(f"  -> {dst}  ({dt/60:.1f} 分钟)", flush=True)
    probe(dst)
    shots(dst, seg["id"])
    extract_first_frame(dst, os.path.join(REF_DIR, f"{cfg['prefix']}_s{idx+1:02d}_first.png"))
    # 尾帧图始终重抽：页面缩略图与验收都靠它，重跑本段必须覆盖。
    # 以前只在「下一段是链式承接」时才抽，导致最后一段 / 后接转场的段重跑后
    # refs 里留着上一版的尾帧，看起来像「尾帧没更新」。
    last_name = f"{cfg['prefix']}_s{idx+1:02d}_last.png"
    last_local = extract_last_frame(dst, os.path.join(REF_DIR, last_name))
    # 只有下一段确实是链式承接时，才额外拷一份进 ComfyUI input 供 first_frame 使用
    if idx + 1 < len(cfg["segments"]):
        nlink = cfg["segments"][idx + 1].get("link") or {}
        if nlink.get("mode", "chain") != "transition":
            shutil.copy(last_local, os.path.join(P.COMFY_INPUT, last_name))
            print(f"    尾帧已备给下一段（链式）：{last_name}", flush=True)
    return dst


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,nb_frames,duration", "-of", "json", path],
        capture_output=True, text=True).stdout
    try:
        s = json.loads(out)["streams"][0]
    except Exception:
        return {}
    num, den = (s.get("r_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den or 1)
    return {"w": s.get("width"), "h": s.get("height"), "fps": round(fps, 3),
            "frames": s.get("nb_frames"), "dur": float(s.get("duration") or 0)}


def probe(path):
    i = ffprobe(path)
    aud = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,duration", "-of", "json", path],
        capture_output=True, text=True).stdout
    has_a = '"codec_name"' in aud
    print(f"    {os.path.basename(path)}: {i.get('w')}x{i.get('h')} "
          f"{i.get('fps')}fps {i.get('frames')}帧 {i.get('dur'):.2f}s audio={has_a}", flush=True)
    return i


def shots(path, tag):
    """抽 6 帧做 2x3 拼图，供人工/视觉检查。"""
    sheet = os.path.join(QA_DIR, f"{tag}_sheet.png")
    vf = ("select='not(mod(n\\,32))',scale=440:-1,tile=3x2")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-vf", vf,
                    "-frames:v", "1", sheet], capture_output=True)
    if os.path.exists(sheet):
        print(f"    抽帧: {sheet}", flush=True)


def has_audio(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", path], capture_output=True, text=True).stdout
    return bool(out.strip())


# 当前段的参考视频（喂 ref_videos 软参考）：build_wf 写入，to_api 读取
REF_VIDEO_PATH = None
REF_VIDEO_SECONDS = None


def to_api(wf):
    """pipeline.build_prompt 对 MiniMaxH3AudioConditioningT8 会错位（UI 里转成输入的
    宽/高/时长仍在 widgets_values 里占位），这里单独重写该节点，其余节点按原样用。

    ref_images 槽位顺序 = 人物 0..N-1，空镜板排最后的第 N 位（见 people_refs）。
    """
    p = P.build_prompt(wf)
    if "238" in p:
        inp = {
            "clip": [str(N_CLIP), 0],
            "video_vae": [str(N_VIDEO_VAE), 0],
            "audio_vae": [str(N_AUDIO_VAE), 0],
            "prompt": [str(N_PROMPT), 0],
            "width": [str(N_RES), 0],
            "height": [str(N_RES), 1],
            "length": ["131", 1],
            "task_type": "auto",
            "audio_mode": "native",
            "audio_denoise_strength": 1.0,
            "add_source_as_reference": False,
            "prompt_primary_audio_ordinal": 0,
            "strict_prompt_tags": True,
            "ref_image_size": "match",
            "reference_video_policy": "official_2_to_15s",
            "allow_above_reference_area": True,
            "ref_images.ref_image_0": [str(N_IMAGE), 0],
        }
        n_ppl = count_people_in_wf(wf)
        for i in range(1, n_ppl):
            node = person_node(i)
            if str(node) in p:
                inp[f"ref_images.ref_image_{i}"] = [str(node), 0]
        if str(N_PLATE) in p:
            inp[f"ref_images.ref_image_{n_ppl}"] = [str(N_PLATE), 0]
        if str(N_FIRST) in p:
            inp["first_frame"] = [str(N_FIRST), 0]
        if str(N_LAST) in p:
            inp["last_frame"] = [str(N_LAST), 0]
        p["238"]["inputs"] = inp
        if REF_VIDEO_PATH:
            vname = os.path.basename(REF_VIDEO_PATH)
            # 帧数上限跟着截取秒数走：int(秒*24)+10，至少 200，避免把参考视频截短
            cap = 200
            if REF_VIDEO_SECONDS:
                cap = max(200, int(REF_VIDEO_SECONDS * 24) + 10)
            p["900"] = {
                "class_type": "VHS_LoadVideo",
                "inputs": {
                    "video": vname,
                    "force_rate": 24,
                    "custom_width": 0,
                    "custom_height": 0,
                    "frame_load_cap": cap,
                    "skip_first_frames": 0,
                    "select_every_nth": 1,
                    "format": "None",
                },
            }
            p["238"]["inputs"]["ref_videos.ref_video_0"] = ["900", 0]
    return p


def submit(api, timeout=5400):
    body = json.dumps({"prompt": api, "client_id": "h3selflift"}).encode()
    pid = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(P.API + "/prompt", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                pid = json.load(r)["prompt_id"]
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")
            if e.code in (502, 503, 504) and attempt < 4:
                print("    [服务自愈] 提交时服务重启，等待恢复", flush=True)
                P.wait_service()
                continue
            P.die(f"提交失败 {e.code}: {msg[:1500]}")
        except Exception as e:
            if attempt < 4:
                print(f"    [服务自愈] {type(e).__name__}，等待恢复", flush=True)
                P.wait_service()
                continue
            P.die(f"提交失败: {e}")
    if not pid:
        P.die("提交失败：未拿到 prompt_id")
    print(f"    prompt_id={pid}", flush=True)

    t0 = time.time()
    last = -1
    empty_q = 0
    while time.time() - t0 < timeout:
        time.sleep(5)
        try:
            with urllib.request.urlopen(f"{P.API}/history/{pid}", timeout=20) as r:
                hist = json.load(r)
        except Exception:
            continue
        if pid in hist:
            st = hist[pid].get("status", {})
            if st.get("status_str") == "error":
                err = ""
                for m in st.get("messages", []):
                    if m[0] == "execution_error":
                        err = str(m[1].get("exception_message", ""))
                        print(json.dumps(m[1], ensure_ascii=False)[:1500], flush=True)
                if "media tag validation failed" in err or "is not connected" in err:
                    P.die(
                        "执行出错：提示词里的 <Picture N> 编号与本段实际接上的媒体数对不上。\n"
                        f"    ComfyUI 原话：{err[:300]}\n"
                        "    这不是并行 / 显存问题，原样重跑这一段还会挂。修法二选一：\n"
                        "      1) 在导演台补上传缺失的参考图（最常见是空镜板没传），让编号数量对上；\n"
                        "      2) 把 head/body 里写死的 <Picture N> 改成 {pic:1} / {pic:plate} /\n"
                        "         {pic?:first} 占位符，由 seg_prompt 按本段真实接线自动换算。")
                P.die("执行出错" + (f"：{err[:300]}" if err else ""))
            return hist[pid].get("outputs", {})
        # 队列状态：既给进度条用，也用来识别「任务已从队列消失」的僵尸等待
        nr = np = -1
        try:
            with urllib.request.urlopen(P.API + "/queue", timeout=10) as r:
                qd = json.load(r)
            nr, np = len(qd["queue_running"]), len(qd["queue_pending"])
        except Exception:
            pass
        if nr == 0 and np == 0:
            empty_q += 1
            if empty_q >= 6:
                P.die(
                    f"任务 {pid} 既不在执行队列、也不在 history 里（连续 {empty_q * 5} 秒）。\n"
                    "    多半是 ComfyUI 中途重启清掉了 history，再等只会空转到 5400s 超时。\n"
                    "    请确认桌面版 ComfyUI 在线，然后重跑这一段。")
        else:
            empty_q = 0
        el = int(time.time() - t0)
        if el // 30 > last:
            last = el // 30
            q = f" 队列 running={nr} pending={np}" if nr >= 0 else ""
            print(f"    ...渲染中 {el//60}分{el%60:02d}秒{q}", flush=True)
    P.die(f"超时 {timeout}s")


def cmd_ref(cfg):
    """Krea2 造「第 1 个人物」参考图。启用 ref_images 多人数组后不再适用。"""
    if not cfg.get("ref_image") or not cfg.get("comfy_ref_name"):
        P.die("当前 cfg 用 ref_images 数组（多人），没有 ref_image 单图字段；"
              "请自备人物图放进 refs/ 并写进 ref_images，不要用 ref 命令。")
    os.makedirs(REF_DIR, exist_ok=True)
    out = cfg["ref_image"]
    if os.path.exists(out) and "--force" not in sys.argv:
        print(f"参考图已存在，跳过（--force 重出）：{out}")
        return out
    P.comfy_free()
    print("Krea2 t2i 生成人物参考图 ...", flush=True)
    P.gen_krea2_t2i(REF_PROMPT, 913741, 832, 1472, "rooftop_girl_ref", out, steps=8)
    shutil.copy(out, os.path.join(P.COMFY_INPUT, cfg["comfy_ref_name"]))
    print(f"已拷进 ComfyUI input: {cfg['comfy_ref_name']}", flush=True)
    return out


def cmd_plate(cfg):
    """造全片唯一一张无人天台空镜板（必须完全无人、从零生成、不回喂）。"""
    os.makedirs(REF_DIR, exist_ok=True)
    out = cfg.get("bg_plate_path") or os.path.join(REF_DIR, "bg_plate.png")
    if os.path.exists(out) and "--force" not in sys.argv:
        print(f"空镜板已存在，跳过（--force 重出）：{out}")
    else:
        P.comfy_free()
        print("Krea2 t2i 生成无人场景空镜板 ...", flush=True)
        P.gen_krea2_t2i(PLATE_PROMPT, int(cfg.get("bg_plate_seed", 913755)),
                        1344, 768, f"{cfg['prefix']}_bg_plate", out, steps=8)
    shutil.copy(out, os.path.join(P.COMFY_INPUT, cfg["bg_plate"]))
    print(f"已拷进 ComfyUI input: {cfg['bg_plate']}", flush=True)
    return out


def ensure_ref_in_input(cfg):
    """把人物参考图 + 空镜板按 comfy_name 拷进 ComfyUI input。

    源文件不见了要在这里就喊出来。以前是静默跳过，等 ComfyUI 校验时抛
    `Invalid image file: xxx.png`（400 prompt_outputs_failed_validation），
    从那条报错完全看不出是「cfg 里有一条指向不存在路径的人物参考」。
    段级 seg["ref_images"] 覆盖的图也一并检查/拷贝（按 comfy_name 去重）。
    """
    seen = {}
    for refs in [people_refs(cfg)] + [people_refs(cfg, s)
                                      for s in cfg.get("segments", [])]:
        for pp in refs:
            seen[pp["comfy_name"]] = pp
    ppl = list(seen.values())
    if not ppl:
        P.die("cfg 里没有任何人物参考图。网页上「+ 添加人物」传一张，"
              "或改 json 的 ref_images / ref_image。")
    for pp in ppl:
        src, dst = pp["path"], os.path.join(P.COMFY_INPUT, pp["comfy_name"])
        if not os.path.isfile(src):
            P.die(f"参考图不存在：{pp['label']} → {src}\n"
                  f"    ref_images 里有这一条但文件没了。网页上重新传一张，"
                  f"或把它从 json 的 ref_images 里删掉。")
        if not pp.get("comfy_name"):
            P.die(f"参考图缺 comfy_name：{src}（ref_images 每项都要有 path/comfy_name）")
        if not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
            shutil.copy(src, dst)
    # 段级 bg_plate 覆盖：每段可以有自己的空镜板（通常仍指向同一块，避免场景漂）
    plates = [(cfg.get("bg_plate"), cfg.get("bg_plate_path"))]
    plates += [(s.get("bg_plate", cfg.get("bg_plate")),
                s.get("bg_plate_path", cfg.get("bg_plate_path")))
               for s in cfg.get("segments", [])]
    for name, src in plates:
        if not name:
            continue
        src = src or os.path.join(REF_DIR, "bg_plate.png")
        dst2 = os.path.join(P.COMFY_INPUT, name)
        if not os.path.isfile(src):
            P.die(f"空镜板不存在：{src}\n"
                  f"    写了 bg_plate={name} 但源图没了。"
                  f"网页上重新传空镜板，或把 bg_plate / bg_plate_path 删掉（不带空镜板也能跑）。")
        if not os.path.exists(dst2) or os.path.getmtime(src) > os.path.getmtime(dst2):
            shutil.copy(src, dst2)


def _gap_transition(cfg, seg, override=None):
    """段 seg 之前的接缝转场类型（它如何与上一段相连）。

    override 非 None（面板强制全局过场）时统一用 override；否则按本段 link：
    - 无 link / link.mode=chain  → dissolve（首帧已承接，叠化只柔化接缝，项目原方式）
    - link.mode=transition        → link.transition（dissolve / black / cut）
    """
    if override and override != "auto":
        return override
    link = seg.get("link")
    if not link:
        return "dissolve"
    if link.get("mode") == "transition":
        return link.get("transition") or "dissolve"
    return "dissolve"  # chain 模式


def _normalize_clip(path, w, h, fps, out_path):
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", path]
    if not has_audio(path):
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    # -af apad 把音轨补成无限长，-shortest 才会以「视频流结束」为准。
    # 否则 H3 出的 mp4 音轨常比视频短一帧，-shortest 会截掉视频最后一帧：
    # 链式段丢的正好是承接下一段的尾帧，硬切时接缝会凭空多一次跳变。
    cmd += ["-vf", f"scale={w}:{h}:flags=lanczos,fps={fps},setsar=1,format=yuv420p",
            "-c:v", "libx264", "-crf", "14", "-preset", "slow", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-af", "apad", "-shortest", out_path]
    subprocess.run(cmd, check=True)
    return out_path


def cmd_merge(cfg, join=None):
    """逐段拼成片，支持 per-link 接缝转场（链式承接 / 叠化 / 黑屏 / 硬切 可逐段混用）。

    join 非 None 时强制全片统一为该类型；否则按每个分段的 link 决定本段之前的接缝。
    xfade(acrossfade) 与 concat 必须分开构建，否则同一 filtergraph 会互相拖死。
    """
    if join is None:                       # 命令行没给 --join 时按片配置走
        join = cfg.get("join_mode") or "auto"
    elif join != cfg.get("join_mode"):
        # 面板显式选择写回片配置：换浏览器 / 换地址访问都能恢复，
        # 命令行 merge 不带 --join 时也沿用。不打 updated_at（不影响已生成画面）。
        cfg["join_mode"] = join
        cfg["join_updated_at"] = time.time()
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"    拼接方式已写入片配置：join_mode={join}", flush=True)
    os.makedirs(FINAL_DIR, exist_ok=True)
    segs = [os.path.join(SEG_DIR, f"{cfg['prefix']}_{s['id']}.mp4") for s in cfg["segments"]]
    for s in segs:
        if not os.path.exists(s):
            P.die(f"缺段文件：{s}")
    probes = [ffprobe(s) for s in segs]
    w, h, fps = probes[0]["w"], probes[0]["h"], probes[0]["fps"]
    # 改过分辨率却只重出部分段时，新段尺寸和旧段不同；_normalize_clip 会把每段
    # 硬 scale 到第一段的 w:h（非等比）—— 3:2 内容被拉成 16:9 就是无声的变形。
    # 宁可报错让用户整片重出，也不要悄悄出一版歪的成片。
    sizes = {(p["w"], p["h"]) for p in probes if p.get("w") and p.get("h")}
    if len(sizes) > 1:
        detail = " / ".join(f"{cfg['segments'][i]['id']}={p['w']}x{p['h']}"
                            for i, p in enumerate(probes))
        P.die("各段分辨率不一致，直接拼会被拉伸变形：" + detail +
              "\n    改过分辨率后要把整片重出（面板逐段重出 / 跑全片），别只重出其中几段。")

    d = float(cfg.get("join_dissolve_seconds", 0.25))
    gap = float(cfg.get("join_black_seconds", 0.8))
    gap_frames = max(1, round(gap * fps))
    gap = gap_frames / fps  # 量化到整帧，避免 concat 时间基错位

    # 每个接缝(段i之前, i>=1)的转场类型
    T = [None] + [_gap_transition(cfg, cfg["segments"][i], join) for i in range(1, len(segs))]

    work = os.path.join(FINAL_DIR, "_work_join")
    os.makedirs(work, exist_ok=True)
    black = os.path.join(work, "black.mp4")
    if any(t == "black" for t in T if t):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", f"color=c=black:s={w}x{h}:r={fps}:d={gap:.6f}",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                        "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                        "-shortest", "-t", f"{gap:.6f}", black], check=True)

    # 构建展平片段列表：黑屏作为独立片段插在对应接缝处
    flat = []  # ("seg", path, orig_idx) 或 ("black", path, None)
    for i, s in enumerate(segs):
        flat.append(("seg", s, i))
        if i < len(segs) - 1 and T[i + 1] == "black":
            flat.append(("black", black, None))

    norm = []
    for k, (kind, path, oi) in enumerate(flat):
        n = os.path.join(work, f"j{k}.mp4")
        _normalize_clip(path, w, h, fps, n)
        norm.append((kind, n, oi))

    base = os.path.join(FINAL_DIR, cfg.get("final_name") or f"{cfg['title']}_成片")
    out = base if base.lower().endswith(".mp4") else base + ".mp4"
    out_silent = os.path.splitext(out)[0] + "_静音.mp4"

    # 相邻 flat 片段之间的转场：仅当两者都是 seg 且后者接缝为 dissolve 才 xfade，否则 concat
    def is_xfade(k):
        if norm[k][0] != "seg" or norm[k + 1][0] != "seg":
            return False
        return T[norm[k + 1][2]] == "dissolve"

    inputs = []
    for kind, n, oi in norm:
        inputs += ["-i", n]

    # 视频 filtergraph
    vlines = [f"[{i}:v]settb=AVTB,fps={fps},format=yuv420p,setsar=1[v{i}]"
              for i in range(len(norm))]
    cur = float(probes[norm[0][2]]["dur"]) if norm[0][0] == "seg" else gap
    prev = "v0"
    for i in range(1, len(norm)):
        dur_i = float(probes[norm[i][2]]["dur"]) if norm[i][0] == "seg" else gap
        if is_xfade(i - 1):
            off = cur - d
            nx = f"x{i}"
            vlines.append(f"[{prev}][v{i}]xfade=transition=fade:duration={d:.3f}:offset={off:.3f}[{nx}]")
            prev = nx
            cur = off + dur_i
        else:
            nx = f"c{i}"
            vlines.append(f"[{prev}][v{i}]concat=n=2:v=1:a=0[{nx}]")
            prev = nx
            cur = cur + dur_i

    # 音频 filtergraph（与视频对应：xfade→acrossfade，否则 concat）
    alines = [f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
              for i in range(len(norm))]
    aprev = "a0"
    for i in range(1, len(norm)):
        if is_xfade(i - 1):
            na = f"ax{i}"
            alines.append(f"[{aprev}][a{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{na}]")
        else:
            na = f"ac{i}"
            alines.append(f"[{aprev}][a{i}]concat=n=2:v=0:a=1[{na}]")
        aprev = na

    silent = os.path.join(work, "xfade_silent.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs,
                    "-filter_complex", ";".join(vlines), "-map", f"[{prev}]",
                    "-an", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", silent], check=True)
    wav = os.path.join(work, "xfade_audio.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs,
                    "-filter_complex", ";".join(alines), "-map", f"[{aprev}]",
                    "-c:a", "pcm_s16le", wav], check=True)
    mux = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", silent, "-i", wav,
                          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                          "-movflags", "+faststart", out], capture_output=True, text=True)
    if mux.returncode != 0:
        P.die(f"最终 mux 失败（{mux.returncode}）：{(mux.stderr or '')[-900:]}")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", out, "-an", "-c:v", "copy",
                    "-movflags", "+faststart", out_silent], check=True)
    types = [t for t in T if t]
    print(f"成片（逐段转场）：{' / '.join(types)}  总时长约 {cur:.2f}s", flush=True)
    print(f"成片：{out}", flush=True)
    print(f"静音版：{out_silent}", flush=True)
    probe(out)
    shots(out, "_final_mixed")
    return out


# cmd_merge 统一实现见上方（per-link 转场已原生支持 dissolve/black/cut 混用）。


def cmd_insert(cfg, after):
    """在 after 段(1-based)后插入一段新段，重编号并重命名产物文件。

    新段默认「链式承接」(link.mode=chain)：首帧=前段尾帧，后段首帧自动改为=新段尾帧；
    body 复制前段作起点，seed 随机。插入后由面板 refresh 重建卡片。
    """
    segs = cfg["segments"]
    if not (1 <= after < len(segs)):
        P.die(f"插入位置非法：after={after}（共 {len(segs)} 段，需在 1..{len(segs) - 1} 之间）")
    prefix = cfg["prefix"]
    old_id = {id(s): s["id"] for s in segs}
    old_pos = {id(s): i for i, s in enumerate(segs)}
    new_seg = {
        "id": "_new",
        "title": "插入分段",
        "seed": int(time.time() * 1000) % 10 ** 8,
        "body": segs[after - 1].get("body", ""),
        "link": {"mode": "chain", "transition": "dissolve"},
        "updated_at": 0,
    }
    segs.insert(after, new_seg)  # 插在 1-based after 段之后（0-based 位置 after）
    rename, pos_map = {}, {}
    for i, s in enumerate(segs):
        oid = old_id.get(id(s))
        s["index"] = i
        s["id"] = f"s{i + 1:02d}"
        if oid is not None and oid != s["id"]:
            rename[oid] = s["id"]
        op = old_pos.get(id(s))
        if op is not None and op != i:
            pos_map[op] = i
    # 视频文件改名（先临时名避免同目录碰撞）
    for oid, nid in rename.items():
        src = os.path.join(SEG_DIR, f"{prefix}_{oid}.mp4")
        if os.path.exists(src):
            os.rename(src, os.path.join(SEG_DIR, f"__tmp_{nid}.mp4"))
    for oid, nid in rename.items():
        tmp = os.path.join(SEG_DIR, f"__tmp_{nid}.mp4")
        if os.path.exists(tmp):
            os.rename(tmp, os.path.join(SEG_DIR, f"{prefix}_{nid}.mp4"))
    # 首尾帧 refs + ComfyUI input 里的尾帧副本，按位置改名
    for op, np_ in pos_map.items():
        for suf in ("first", "last"):
            src = os.path.join(REF_DIR, f"{prefix}_s{op + 1:02d}_{suf}.png")
            if os.path.exists(src):
                os.rename(src, os.path.join(REF_DIR, f"__tmp_s{np_ + 1:02d}_{suf}.png"))
        inp = os.path.join(P.COMFY_INPUT, f"{prefix}_s{op + 1:02d}_last.png")
        if os.path.exists(inp):
            os.rename(inp, os.path.join(P.COMFY_INPUT, f"__tmp_s{np_ + 1:02d}_last.png"))
    for op, np_ in pos_map.items():
        for suf in ("first", "last"):
            tmp = os.path.join(REF_DIR, f"__tmp_s{np_ + 1:02d}_{suf}.png")
            if os.path.exists(tmp):
                os.rename(tmp, os.path.join(REF_DIR, f"{prefix}_s{np_ + 1:02d}_{suf}.png"))
        tmp = os.path.join(P.COMFY_INPUT, f"__tmp_s{np_ + 1:02d}_last.png")
        if os.path.exists(tmp):
            os.rename(tmp, os.path.join(P.COMFY_INPUT, f"{prefix}_s{np_ + 1:02d}_last.png"))
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"已在第 {after} 段后插入新段（默认链式承接），现共 {len(segs)} 段，已重编号并重命名产物。",
          flush=True)


def _pick_body(seg):
    """从各种分镜格式里取段落提示词：prompt_segment / prompt_video / body / prompt，
    都没有就用镜头/动作字段拼一段。"""
    for key in ("prompt_segment", "prompt_video", "body", "prompt"):
        value = seg.get(key)
        if value and str(value).strip():
            return str(value).strip()
    parts = []
    for key in ("shot", "camera", "light", "open_action", "motion", "end_action"):
        value = seg.get(key)
        if value and str(value).strip():
            parts.append(str(value).strip())
    return "\n\n".join(parts)


def _auto_head(src):
    """没有现成 head 时拼一个通用身份/场景锚，用户可在面板里改。"""
    head = ("<Picture 1> 是人物参考，完整还原其中人物的脸型、五官、发型、服装与身材比例，"
            "全程不换人、不换装。\n\n"
            "<Picture 2> 是全片唯一的场景空镜参考，环境结构、陈设与光源方向以它为准，保持不变。")
    wardrobe = str(src.get("wardrobe_items") or "").strip()
    if wardrobe:
        head += f"\n\n服装锁定：{wardrobe}"
    return head


def _find_asset(name, *dirs):
    if not name:
        return None
    if os.path.isfile(name):
        return os.path.abspath(name)
    base = os.path.basename(str(name))
    for folder in dirs:
        for candidate in (os.path.join(folder, str(name)), os.path.join(folder, base)):
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    return None


def cmd_import(a):
    """把旧 Director storyboard（或任意带 segments 的分镜）转成 SelfLift 的 segments.json。"""
    import random

    if not a.src:
        P.die("import 需要 --src（源分镜 json）")
    src = os.path.abspath(a.src)
    if not os.path.isfile(src):
        P.die(f"找不到源文件：{src}")
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    segs_in = data.get("segments") or []
    if not segs_in:
        P.die("源文件里没有 segments[]")
    if "body" in segs_in[0] and data.get("workflow"):
        print("这已经是 SelfLift 的 segments.json 格式，直接把它填进面板路径即可。", flush=True)
        return

    out_dir = os.path.abspath(a.out or os.path.dirname(src))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "segments.json")
    if os.path.exists(out):
        if not a.force:
            P.die(f"{out} 已存在；加 --force 覆盖（会先留一份 .bak）")
        shutil.copyfile(out, out + ".bak")

    prefix = str(data.get("prefix") or os.path.splitext(os.path.basename(src))[0] or "selflift")
    title = str(data.get("title") or prefix)
    fps = int(data.get("fps") or 24)
    secs = [float(s["seconds"]) for s in segs_in if s.get("seconds")]
    seconds = float(a.seconds or (secs[0] if secs else data.get("seconds_per_segment") or 5.0))
    if len(set(secs)) > 1:
        print(f"[警告] 源分镜每段时长不同 {sorted(set(secs))}；SelfLift 全片统一，已取 {seconds}s",
              flush=True)

    refs_dirs = [out_dir, os.path.join(out_dir, "refs"), P.COMFY_INPUT]
    ref_name = a.ref or (data.get("reference_images") or [None])[0]
    ref_path = _find_asset(ref_name, *refs_dirs) if ref_name else None
    plate_name = a.plate or data.get("bg_ref")
    plate_path = _find_asset(plate_name, *refs_dirs) if plate_name else None
    if not ref_path:
        print(f"[警告] 没找到人物参考图（{ref_name!r}）。把图放进 {out_dir}/refs/ 或 ComfyUI input 后，"
              f"改 cfg 的 ref_image / comfy_ref_name 两项即可。", flush=True)
    if not plate_path:
        print(f"[警告] 没找到空镜板（{plate_name!r}），cfg 里 bg_plate 会留空（=不接第二张参考图）。",
              flush=True)

    segments = []
    for i, src_seg in enumerate(segs_in, 1):
        body = _pick_body(src_seg)
        if not body:
            print(f"[警告] 第 {i} 段取不到提示词，body 留空，请在面板里补。", flush=True)
        item = {
            "id": f"s{i:02d}",
            "title": str(src_seg.get("name") or src_seg.get("title") or f"第 {i} 段"),
            "seed": (int(a.seed_base) + i * 37) if a.seed_base else random.randint(300000, 900000),
            "body": body,
        }
        if i > 1:
            chained = src_seg.get("chain")
            mode = "chain" if (chained is None or bool(chained)) else "transition"
            item["link"] = {"mode": mode, "transition": "dissolve"}
        segments.append(item)

    fid = film_id_of(os.path.basename(out_dir), {"prefix": prefix})
    final_name = (f"{title}_{fid}_成片.mp4" if re.fullmatch(r"\d{4}_\d{6}", fid)
                  else f"{title}_成片.mp4")
    cfg = {
        "title": title,
        "film_id": fid,
        "prefix": prefix,
        "final_name": final_name,
        "workflow": a.workflow or DEFAULT_WORKFLOW,
        "ref_image": ref_path or "",
        "comfy_ref_name": (f"{prefix}_{os.path.basename(ref_path)}" if ref_path else ""),
        "bg_plate": (f"{prefix}_{os.path.basename(plate_path)}" if plate_path else ""),
        "bg_plate_path": plate_path or "",
        "megapixels": float(data.get("megapixels") or DEFAULT_MEGAPIXELS),
        "seconds": seconds,
        "length": int(17 * round(seconds * fps / 17) + 5),
        "fps": fps,
        "join_mode": "auto",
        "join_dissolve_seconds": 0.25,
        "chain_first_frame": True,
        "head": a.head or _auto_head(data),
        "tail": a.tail or ("写实电影摄影，画面中只有剧情里的人物，不出现文字、水印、字幕。\n\n"
                           "[衔接] 段首约 0.5 秒保持动作与镜头稳定，段尾约 0.5 秒动作收住。"),
        "segments": segments,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"已转换 {len(segments)} 段 → {out}", flush=True)
    print(f"  时长 {seconds}s × {len(segments)} 段 @{fps}fps；人物参考 "
          f"{'有' if ref_path else '缺'}；空镜板 {'有' if plate_path else '缺'}", flush=True)
    if len(segments) > 1:
        modes = [s.get("link", {}).get("mode", "chain") for s in segments[1:]]
        print(f"  接缝：{' / '.join(modes)}（沿用源分镜的 chain 字段；可在面板逐段改）", flush=True)
    print(f"  下一步：面板里填这个路径 → 打开/刷新 → 一键跑全片", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ref", "plate", "seg", "all", "merge", "probe",
                                    "graph", "status", "set", "insert", "import"])
    ap.add_argument("idx", nargs="?", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--seg", type=int, default=0, help="set 命令的目标段号（1 起）")
    ap.add_argument("--seed", type=int, default=None, help="set：换 seed 重出用")
    ap.add_argument("--body", default=None, help="set：替换该段提示词正文")
    ap.add_argument("--seg-head", dest="seg_head", default=None,
                    help="set：本段 head（整段覆盖全局，不是追加）；clear=删掉、回落到全局")
    ap.add_argument("--seg-tail", dest="seg_tail", default=None,
                    help="set：本段 tail（覆盖全局 tail，用于单段单独调画风）；clear=删掉、回落到全局")
    ap.add_argument("--first-frame", dest="first_frame", default=None,
                    help="set：指定本段首帧图文件名；auto/clear 恢复自动承接")
    ap.add_argument("--last-frame", dest="last_frame", default=None,
                    help="set：锁定本段尾帧图（接 238.last_frame，需重出本段）；auto 解除")
    ap.add_argument("--cfg", default=DEFAULT_CFG, help="分镜 json 路径；产物写到同目录")
    ap.add_argument("--join", choices=["black", "dissolve", "cut", "auto"], default=None,
                    help="段间接续：auto=按每段 link 决定；black=0.8s 纯黑；"
                         "dissolve=0.25s 短叠化（项目原来的方式）；cut=首尾帧直连硬切")
    ap.add_argument("--link-mode", dest="link_mode", default=None,
                    choices=["chain", "transition"],
                    help="set：本段承接上一段的方式 chain=像素承接 / transition=独立+转场")
    ap.add_argument("--link-transition", dest="link_transition", default=None,
                    choices=["dissolve", "black", "cut"],
                    help="set：transition 模式下的接缝转场类型")
    ap.add_argument("--after", type=int, default=0, help="insert：在第 after 段(1起)后插入新段")
    ap.add_argument("--src", default=None, help="import：源分镜 json（旧 Director storyboard 等）")
    ap.add_argument("--out", default=None, help="import：输出片目录（默认源文件同目录）")
    ap.add_argument("--ref", default=None, help="import：人物参考图（路径或文件名）")
    ap.add_argument("--plate", default=None, help="import：场景空镜板（路径或文件名）")
    ap.add_argument("--head", default=None, help="import：覆盖全局 head（身份/场景锚）")
    ap.add_argument("--tail", default=None, help="import：覆盖全局 tail（画风/衔接锚）")
    ap.add_argument("--seed-base", dest="seed_base", type=int, default=None,
                    help="import：起始 seed（第 i 段用 base+i*37）")
    ap.add_argument("--seconds", type=float, default=None, help="import：全片每段时长（秒）")
    ap.add_argument("--workflow", default=None, help="import：base workflow 路径")
    ap.add_argument("--ref-video", dest="ref_video", default=None,
                    help="set：把某视频作为 ref_videos 软参考（动作/机位），自动复制到 ComfyUI input；clear/none 移除")
    ap.add_argument("--megapixels", type=float, default=None,
                    help="set：全局输出分辨率（百万像素，8GB 显存建议 0.5 左右）")
    ap.add_argument("--aspect-ratio", dest="aspect_ratio", default=None,
                    help="set：全局画幅比例（如 '16:9 (Widescreen)'）")
    a = ap.parse_args()
    if a.cmd == "import":
        cmd_import(a)
        return
    set_root(a.cfg)
    os.makedirs(QA_DIR, exist_ok=True)
    cfg = load_cfg()
    if a.cmd == "ref":
        cmd_ref(cfg)
    elif a.cmd == "plate":
        cmd_plate(cfg)
    elif a.cmd == "graph":
        # 链式段运行时才拿到上段尾帧（seg["first_frame"]），不模拟的话 Picture 编号
        # 会按「没接首帧」算，和真跑差一位——校验就没意义了。这里补一个虚拟首帧。
        for i, s in enumerate(cfg["segments"]):
            probe = dict(s)
            mode = (s.get("link") or {}).get("mode", "chain")
            if (i > 0 and cfg.get("chain_first_frame") and mode == "chain"
                    and not probe.get("first_frame")):
                probe["first_frame"] = f"(虚拟)_{cfg['prefix']}_s{i:02d}_last.png"
            if i > 0 and mode == "transition":
                probe.pop("first_frame", None)
            check_graph(cfg, probe)
    elif a.cmd == "seg":
        ensure_ref_in_input(cfg)
        run_seg(cfg, a.idx - 1)
    elif a.cmd == "all":
        ensure_ref_in_input(cfg)
        for i in range(len(cfg["segments"])):
            dst = os.path.join(SEG_DIR, f"{cfg['prefix']}_{cfg['segments'][i]['id']}.mp4")
            if os.path.exists(dst) and not a.force:
                print(f"跳过已存在：{dst}")
                if cfg.get("chain_first_frame"):
                    ln = f"{cfg['prefix']}_s{i+1:02d}_last.png"
                    dst_in = os.path.join(P.COMFY_INPUT, ln)
                    dst_ref = os.path.join(REF_DIR, ln)
                    # refs 里的那份也要在：页面缩略图读它，缺了会显示旧图或空白
                    if not os.path.exists(dst_in) or not os.path.isfile(dst_ref):
                        extract_last_frame(dst, dst_ref)
                        shutil.copy(dst_ref, dst_in)
                        print(f"    补抽尾帧：{ln}", flush=True)
                continue
            run_seg(cfg, i)
    elif a.cmd == "status":
        print(json.dumps(seg_status(cfg), ensure_ascii=False), flush=True)
    elif a.cmd == "insert":
        cmd_insert(cfg, a.after)
    elif a.cmd == "set":
        cmd_set(cfg, a)
    elif a.cmd == "probe":
        for s in cfg["segments"]:
            p = os.path.join(SEG_DIR, f"{cfg['prefix']}_{s['id']}.mp4")
            if os.path.exists(p):
                probe(p)
    elif a.cmd == "merge":
        cmd_merge(cfg, a.join)


if __name__ == "__main__":
    main()
