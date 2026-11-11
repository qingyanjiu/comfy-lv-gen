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
N_PLATE = 152   # LoadImage：全片唯一无人空镜板 → ref_images.ref_image_1
N_LAST = 153    # LoadImage：可选的本段尾帧锁 → last_frame（默认不接）
DEFAULT_MEGAPIXELS = 0.5  # 用户定：长视频默认 0.5MP，别再沿用旧值 0.3/0.98

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


def seg_prompt(cfg, seg):
    return "\n\n".join([cfg["head"], seg["body"], cfg["tail"]])


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
    nodes[N_IMAGE]["widgets_values"][0] = cfg["comfy_ref_name"]
    if cfg.get("clip_name"):
        nodes[N_CLIP]["widgets_values"][0] = cfg["clip_name"]
    nodes[N_PROMPT]["widgets_values"][0] = seg_prompt(cfg, seg)
    nodes[N_DUR]["widgets_values"][0] = float(cfg["seconds"])
    nodes[N_RES]["widgets_values"][1] = float(cfg.get("megapixels", DEFAULT_MEGAPIXELS))
    nodes[N_SAMPLER]["widgets_values"][0] = int(seg["seed"])
    nodes[N_VIDEO]["widgets_values"][0] = float(cfg["fps"])
    nodes[N_VIDEO]["widgets_values"][2] = f"{cfg['prefix']}_{seg['id']}"
    if cfg.get("bg_plate"):
        add_load_image(wf, N_PLATE, cfg["bg_plate"])
    if seg.get("first_frame"):
        add_load_image(wf, N_FIRST, seg["first_frame"])
    if seg.get("last_frame_override"):
        add_load_image(wf, N_LAST, seg["last_frame_override"])
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
              f"task={d.get('task_type')} audio={d.get('audio_mode')} "
              f"ref0={d.get('ref_images.ref_image_0')}", flush=True)
        print(f"    接续: plate={d.get('ref_images.ref_image_1')} "
              f"first_frame={d.get('first_frame')}", flush=True)
        res = prompt[str(N_RES)]["inputs"]
        print(f"    resolution: {res.get('aspect_ratio')} / {res.get('megapixels')} MP "
              f"(0.5MP@16:9 = 960x544)", flush=True)
        print(f"    prompt chars={len(str(cfg['head'])+cfg['segments'][0]['body'])}", flush=True)
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
            "mtime": os.path.getmtime(vid) if done else 0,
        })
    stale_from = None
    for i, item in enumerate(segs):
        if not item["done"]:
            continue
        edited = cfg["segments"][i].get("updated_at", 0) > item["mtime"] + 1
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
    return {
        "title": cfg.get("title", ""),
        "cfg": CFG_PATH,
        "prefix": cfg.get("prefix", ""),
        "join_mode": cfg.get("join_mode", "dissolve"),
        "chain_first_frame": bool(cfg.get("chain_first_frame")),
        "segments": segs,
        "finals": finals,
    }


def cmd_set(cfg, args):
    """面板改 seed / 提示词 / 首帧覆盖：直接写回分镜 json，并打 updated_at 让状态判 stale。"""
    idx = args.seg - 1
    if not (0 <= idx < len(cfg["segments"])):
        P.die(f"段号越界：{args.seg}（共 {len(cfg['segments'])} 段）")
    sg = cfg["segments"][idx]
    changed = []
    if args.seed is not None:
        sg["seed"] = int(args.seed)
        changed.append(f"seed={sg['seed']}")
    if args.body is not None:
        sg["body"] = args.body
        changed.append("body")
    if args.first_frame is not None:
        if args.first_frame.strip().lower() in ("", "none", "auto", "clear"):
            sg.pop("first_frame_override", None)
            changed.append("first_frame=自动承接上段尾帧")
        else:
            sg["first_frame_override"] = args.first_frame.strip()
            changed.append(f"first_frame={sg['first_frame_override']}")
    if args.last_frame is not None:
        if args.last_frame.strip().lower() in ("", "none", "auto", "clear"):
            sg.pop("last_frame_override", None)
            changed.append("last_frame=解除锁定")
        else:
            sg["last_frame_override"] = args.last_frame.strip()
            changed.append(f"last_frame={sg['last_frame_override']}（需重出本段）")
    if args.link_mode is not None:
        if args.link_mode not in ("chain", "transition"):
            P.die(f"未知 link_mode：{args.link_mode}（应为 chain / transition）")
        sg.setdefault("link", {})
        sg["link"]["mode"] = args.link_mode
        changed.append(f"link.mode={args.link_mode}")
    if args.link_transition is not None:
        if args.link_transition not in ("dissolve", "black", "cut"):
            P.die(f"未知 link.transition：{args.link_transition}（应为 dissolve / black / cut）")
        sg.setdefault("link", {})
        sg["link"]["transition"] = args.link_transition
        changed.append(f"link.transition={args.link_transition}")
    if changed:
        sg["updated_at"] = time.time()
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"段 {args.seg} 已更新：{', '.join(changed) or '无改动'}", flush=True)


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
    print(f"\n===== 段 {idx+1}/5  {seg['id']} {seg['title']}  seed={seg['seed']} =====", flush=True)
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
    # 把尾帧备给下一段：仅当下一段是「链式承接」时才需要（转场模式下游独立，无需共享）
    if idx + 1 < len(cfg["segments"]):
        nlink = cfg["segments"][idx + 1].get("link") or {}
        if nlink.get("mode", "chain") != "transition":
            last_name = f"{cfg['prefix']}_s{idx+1:02d}_last.png"
            last_local = extract_last_frame(dst, os.path.join(REF_DIR, last_name))
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


def to_api(wf):
    """pipeline.build_prompt 对 MiniMaxH3AudioConditioningT8 会错位（UI 里转成输入的
    宽/高/时长仍在 widgets_values 里占位），这里单独重写该节点，其余节点按原样用。"""
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
        if str(N_PLATE) in p:
            inp["ref_images.ref_image_1"] = [str(N_PLATE), 0]
        if str(N_FIRST) in p:
            inp["first_frame"] = [str(N_FIRST), 0]
        if str(N_LAST) in p:
            inp["last_frame"] = [str(N_LAST), 0]
        p["238"]["inputs"] = inp
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
                for m in st.get("messages", []):
                    if m[0] == "execution_error":
                        print(json.dumps(m[1], ensure_ascii=False)[:1500], flush=True)
                P.die("执行出错")
            return hist[pid].get("outputs", {})
        el = int(time.time() - t0)
        if el // 30 > last:
            last = el // 30
            q = ""
            try:
                with urllib.request.urlopen(P.API + "/queue", timeout=10) as r:
                    qd = json.load(r)
                q = f" 队列 running={len(qd['queue_running'])} pending={len(qd['queue_pending'])}"
            except Exception:
                pass
            print(f"    ...渲染中 {el//60}分{el%60:02d}秒{q}", flush=True)
    P.die(f"超时 {timeout}s")


def cmd_ref(cfg):
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
    dst = os.path.join(P.COMFY_INPUT, cfg["comfy_ref_name"])
    if not os.path.exists(dst) or os.path.getmtime(cfg["ref_image"]) > os.path.getmtime(dst):
        shutil.copy(cfg["ref_image"], dst)
    if cfg.get("bg_plate"):
        src = cfg.get("bg_plate_path") or os.path.join(REF_DIR, "bg_plate.png")
        dst2 = os.path.join(P.COMFY_INPUT, cfg["bg_plate"])
        if os.path.exists(src) and (not os.path.exists(dst2)
                                    or os.path.getmtime(src) > os.path.getmtime(dst2)):
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
    cmd += ["-vf", f"scale={w}:{h}:flags=lanczos,fps={fps},setsar=1,format=yuv420p",
            "-c:v", "libx264", "-crf", "14", "-preset", "slow", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", out_path]
    subprocess.run(cmd, check=True)
    return out_path


def cmd_merge(cfg, join=None):
    """逐段拼成片，支持 per-link 接缝转场（链式承接 / 叠化 / 黑屏 / 硬切 可逐段混用）。

    join 非 None 时强制全片统一为该类型；否则按每个分段的 link 决定本段之前的接缝。
    xfade(acrossfade) 与 concat 必须分开构建，否则同一 filtergraph 会互相拖死。
    """
    os.makedirs(FINAL_DIR, exist_ok=True)
    segs = [os.path.join(SEG_DIR, f"{cfg['prefix']}_{s['id']}.mp4") for s in cfg["segments"]]
    for s in segs:
        if not os.path.exists(s):
            P.die(f"缺段文件：{s}")
    probes = [ffprobe(s) for s in segs]
    w, h, fps = probes[0]["w"], probes[0]["h"], probes[0]["fps"]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ref", "plate", "seg", "all", "merge", "probe",
                                    "graph", "status", "set", "insert"])
    ap.add_argument("idx", nargs="?", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--seg", type=int, default=0, help="set 命令的目标段号（1 起）")
    ap.add_argument("--seed", type=int, default=None, help="set：换 seed 重出用")
    ap.add_argument("--body", default=None, help="set：替换该段提示词正文")
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
    a = ap.parse_args()
    set_root(a.cfg)
    os.makedirs(QA_DIR, exist_ok=True)
    cfg = load_cfg()
    if a.cmd == "ref":
        cmd_ref(cfg)
    elif a.cmd == "plate":
        cmd_plate(cfg)
    elif a.cmd == "graph":
        for s in cfg["segments"]:
            check_graph(cfg, s)
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
                    if not os.path.exists(dst_in):
                        extract_last_frame(dst, os.path.join(REF_DIR, ln))
                        shutil.copy(os.path.join(REF_DIR, ln), dst_in)
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
