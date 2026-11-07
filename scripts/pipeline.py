#!/usr/bin/env python
"""H3 长视频流水线：分镜校验 → 首尾帧 → 分段视频 → 合并。

设计：每个阶段独立可跑、可中断续跑；质检由 h3-video-qa 的 qa_lib 提供。
"""
import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

from PIL import Image
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache

ROOT = r"D:/Comfy-Desktop/ComfyUI-Installs/h3-long-video-repo"
WF_DIR = os.path.join(ROOT, "example_workflows")
COMFY_INPUT = r"D:/Comfy-Desktop/ComfyUI-Shared/input"
COMFY_OUTPUT = r"D:/Comfy-Desktop/ComfyUI-Shared/output"
# 所有产物都落在 Shared 下，不再写进安装目录
SHARED_ROOT = os.path.normpath(os.path.join(COMFY_OUTPUT, "h3-long-video"))
JOBS = os.path.join(SHARED_ROOT, "jobs-single")
H3_OUTPUT = r"D:/Comfy-Desktop/ComfyUI-Installs/ComfyUI/ComfyUI/output/h3_single_clip"
TESTS = os.path.join(SHARED_ROOT, "tests")
API = os.environ.get("H3_COMFY_API", "http://127.0.0.1:8188")
# 最近一次 comfy_run 的端到端耗时（秒）。用于把「这张图跑了多久」写进 state：
# 实测同一 seed/尺寸的耗时会分 17s / 27s / 31s 三档，差别全在显存换页与 TE 重载，
# 跟画质无因果；但要事后复盘就必须有数，不能靠 ComfyUI 控制台目测。
LAST_SEC = None
QA_DIR = r"D:/Comfy-Desktop/.workbuddy/skills/h3-video-qa/scripts"

sys.path.insert(0, QA_DIR)
import qa_lib as Q  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import direction as D  # noqa: E402

PFX = "h3lv"

# 编辑模型改不动腿：实测写了「两腿前后互换」，生成结果腿照抄首帧。
# 能改的是头部、手臂、躯干。腿只能当伴随描述，不能当主动作。
LEG_MAIN = [("换重心", "换重心"), ("换腿", "换腿"), ("换步", "换步"),
            ("膝盖", "屈膝"), ("屈膝", "屈膝"), ("抬脚", "抬脚"), ("踏上", "踏上台阶")]

BAD_ACTS = ["躺下", "躺到", "站起来", "走出", "跑到", "跨过", "换场景", "脱掉", "转身 180"]

# 会让首尾帧产生"人物尺度变化"的写法。H3 会把"人变大"理解成镜头推进，
# 一路推到脸部大特写，同时把脸重绘成另一个人（实测：8s 段第 4 秒整张脸换人并微笑）。
BAD_SCALE = ["走近", "走向镜头", "朝镜头走", "迎面", "逼近", "远处走来", "靠近镜头", "走近镜头"]


# ---------------------------------------------------------------- 基础工具
def die(msg):
    print(f"[错误] {msg}")
    sys.exit(1)


@lru_cache(maxsize=256)
def obj_info(name):
    # 节点类型名可能带空格（例如 "Image Median Blur"），不做 URL 编码会抛 InvalidURL。
    # 缓存：build_prompt 每个节点都查一次，缓存后同一类型只联网一次，也少挨 502。
    with urllib.request.urlopen(
            f"{API}/object_info/{urllib.parse.quote(name, safe='')}", timeout=15) as r:
        return json.load(r)[name]


def wait_service(limit=180):
    """桌面版 ComfyUI 会在大模型换入换出后自动重启，期间 /object_info、/api/prompt
    全部返回 502。等它重新监听再继续，别让整条关键帧链被一次重启打断
    （2026-11-07 实测：Qwen2.1 双图编辑每跑完 1~2 张就轮转一次日志）。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            with urllib.request.urlopen(f"{API}/system_stats", timeout=5) as r:
                if r.status == 200:
                    print(f"    [服务自愈] ComfyUI 已恢复（等待 {int(time.time()-t0)}s）", flush=True)
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def build_prompt(wf):
    prompt = {}
    for n in wf["nodes"]:
        if n["type"] in ("MarkdownNote", "Note"):
            continue
        info = obj_info(n["type"])
        ordered = list(info["input"].get("required", {}).keys()) + list(info["input"].get("optional", {}).keys())
        links = {i["name"]: i for i in n.get("inputs") or []}
        inputs, wv, wi = {}, list(n.get("widgets_values") or []), 0
        for name in ordered:
            if name in links and links[name].get("link") is not None:
                src = next(x for x in wf["links"] if x[0] == links[name]["link"])
                inputs[name] = [str(src[1]), src[2]]
                continue
            spec = info["input"].get("required", {}).get(name) or info["input"].get("optional", {}).get(name)
            typ = spec[0]
            if isinstance(typ, list) or typ in ("COMBO", "INT", "FLOAT", "BOOLEAN", "STRING"):
                if wi < len(wv):
                    inputs[name] = wv[wi]
                    wi += 1
                    if name == "seed" and wi < len(wv) and wv[wi] in ("fixed", "randomize", "increment", "decrement"):
                        wi += 1
        # 自动增长输入（TextEncodeQwenImage21 的 images.image_1/2/3…）不在 object_info 顶层名字里，
        # 只按 object_info 遍历会把这些连线整条丢掉（2.1 的参考图就是这么丢的）。
        for name, inp in links.items():
            if name in inputs or inp.get("link") is None:
                continue
            src = next((x for x in wf["links"] if x[0] == inp["link"]), None)
            if src:
                inputs[name] = [str(src[1]), src[2]]
        prompt[str(n["id"])] = {"class_type": n["type"], "inputs": inputs}
    return prompt


def comfy_run(wf_path, edits, timeout=1200, restarts=4):
    """edits: {node_id: {index: value} | callable(nodes_dict)}

    2026-11-07：ComfyUI 桌面版在 Qwen2.1 双图编辑跑完一两张后会自动重启进程
    （日志每次停在 `Prompt executed in 35s` 后轮转），重启窗口里 /object_info
    和 /api/prompt 直接 502。这里做「等恢复 → 重新提交这张图」自愈；
    每次重提交都用同一套 edits/seed，产物文件名不变，state 仍能正常续跑。
    """
    wf = json.load(open(wf_path, encoding="utf-8"))
    nodes = {n["id"]: n for n in wf["nodes"]}
    for nid, patch in edits.items():
        if callable(patch):
            patch(nodes)
            continue
        if nid not in nodes:
            continue
        for idx, val in patch.items():
            nodes[nid]["widgets_values"][int(idx)] = val

    for attempt in range(restarts + 1):
        try:
            data = json.dumps({"prompt": build_prompt(wf), "client_id": "h3lv"}).encode()
            req = urllib.request.Request(f"{API}/api/prompt", data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                pid = json.load(r)["prompt_id"]
        except urllib.error.HTTPError as e:
            if e.code not in (502, 503, 504) or attempt >= restarts:
                die(f"提交失败 {e.code}: {e.read().decode('utf-8', 'ignore')[:600]}")
            print(f"    [服务自愈] 提交时 ComfyUI 重启（HTTP {e.code}），"
                  f"第 {attempt + 1}/{restarts} 次重试", flush=True)
            wait_service()
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt >= restarts:
                die(f"提交失败: {e}")
            print(f"    [服务自愈] 连接断开（{type(e).__name__}），"
                  f"第 {attempt + 1}/{restarts} 次重试", flush=True)
            wait_service()
            continue

        t0 = time.time()
        lost, broken = 0, False
        while time.time() - t0 < timeout:
            time.sleep(5)
            try:
                with urllib.request.urlopen(f"{API}/history/{pid}", timeout=15) as r:
                    hist = json.load(r)
            except Exception:
                lost += 1
                if lost >= 6:  # 连续约 30s 不可达 → 判定服务重启，这张图重提交
                    if attempt >= restarts:
                        die("渲染中服务反复重启，放弃")
                    print("    [服务自愈] 渲染中服务重启，等恢复后重提交这张图", flush=True)
                    wait_service()
                    broken = True
                    break
                continue
            lost = 0
            if pid in hist:
                st = hist[pid].get("status", {})
                if st.get("status_str") == "error":
                    for m in st.get("messages", []):
                        if m[0] == "execution_error":
                            die(f"执行出错: {m[1].get('exception_message')}")
                    die("执行出错")
                global LAST_SEC
                LAST_SEC = round(time.time() - t0, 1)
                return hist[pid].get("outputs", {})
            if int(time.time() - t0) % 30 < 6:
                print(f"    生成中 {int(time.time()-t0)}s", flush=True)
        else:
            die("生成超时")
        if broken:
            continue
    die("多次服务重启，放弃")


# ---------------------------------------------------------------- 状态
def state_path(sb_path):
    return os.path.splitext(sb_path)[0] + ".state.json"


def load_state(sb_path, sb):
    p = state_path(sb_path)
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return {"title": sb.get("title", "video"), "segments": {}}


def save_state(sb_path, st):
    json.dump(st, open(state_path(sb_path), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def exposure_stats(path):
    """返回 (V 通道均值, 过曝像素占比)。过曝 = V>248 且低饱和（墙面/高光死白）。"""
    import cv2
    img = cv2.imread(path)
    if img is None:
        return None, None
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    v, s = hsv[..., 2], hsv[..., 1]
    return float(v.mean()), float(((v > 248) & (s < 40)).mean())


def align_exposure(anchor, path, tol=6.0):
    """把一帧的亮度对齐到锚图（均值增益，只动 LAB 的 L 通道，色相饱和度不动）。

    2026-11-05：2.1 每帧独立出图，同一提示词也有 ±20 的亮度漂移，而关键帧质检对
    「偏离场景锚 > 8」是**硬 FAIL**。段2 实测 -12.8 / -18.5，换种子重出修不掉——
    因为那是提示词决定的场景明暗（俯身进橱窗阴影），不是随机噪声。
    所以出帧后直接做一次增益归一，比重出靠谱，也省 2~4 次出图。

    增益法（而不是两点重映射）保持明暗比例，不会引入新的高光裁切；
    闭环收敛在 V 均值上（质检量的是 V），L 上执行时乘 1.15 补偿两者差异。
    """
    import cv2
    import numpy as np
    if not (anchor and os.path.exists(anchor) and os.path.exists(path)):
        return False
    if os.path.abspath(anchor) == os.path.abspath(path):
        return False
    av, _ = exposure_stats(anchor)
    ev, _ = exposure_stats(path)
    if av is None or ev is None or abs(ev - av) <= tol:
        return False
    img = cv2.imread(path)
    if img is None:
        return False
    for _ in range(8):
        cur = float(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 2].mean())
        err = av - cur
        if abs(err) <= 0.5:
            break
        gain = 1.0 + (err / max(cur, 1.0)) * 1.15
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        L = lab[..., 0].astype(np.float32)
        if gain > 1.0:
            # 提亮只作用于暗部和中间调：整幅乘增益会把橱窗灯/白墙推到 255，
            # 实测过曝像素 0.06% → 3%，比重出还糟（段2/段3 都中过）。
            L = np.where(L < 200, L * gain, L)
        else:
            L = L * gain
        lab[..., 0] = np.clip(L, 0, 255).astype(np.uint8)
        img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    cv2.imwrite(path, img)
    nv, no = exposure_stats(path)
    print(f"    亮度对齐 {os.path.basename(path)}: {ev:.0f} -> {nv:.0f}（锚 {av:.0f}，"
          f"过曝 {no * 100:.2f}%）", flush=True)
    return True


def normalize_916(path, tol=0.004):
    """把图归一成精确 9:16，只在这里补画幅，2.1 生成阶段不补白。

    2026-11-02 定（用户要求分两步：生成按原图比例，画幅留到最后）：
    - 太宽（4:5/1:1 等）→ 顶部补「背景墙色平滑渐变」，不裁剪、不模糊，
      保住桌上水壶杯子等横向内容；补的是纯色墙面，模型/视频都不会被污染
    - 太高 → 保底裁顶部（脚不会被切）
    """
    import cv2
    import numpy as np
    img = cv2.imread(path)
    if img is None:
        return False
    h, w = img.shape[:2]
    if abs(w / h - 9 / 16) <= tol:
        return False
    th = round(w * 16 / 9)
    if th <= h:
        y0 = h - th                      # 保底：切顶部，脚不会被切
        out = img[y0:y0 + th, :]
    elif th - h <= round(h * 0.06):
        # 只差一点点（752x1392 这类）→ 顶部补薄薄一条墙色，别裁
        out = np.vstack([_wall_grad(img, th - h), img])
    else:
        # 明显偏宽（如 4:5）→ 顶部补墙面，保留全部横向内容
        out = np.vstack([_wall_grad(img, th - h), img])
    cv2.imwrite(path, out)
    print(f"    归一化 9:16 {w}x{h} -> {out.shape[1]}x{out.shape[0]}")
    return True


def _wall_grad(img, pad_h):
    """取左上角背景墙色，生成一条平滑的竖向渐变，用于补画幅（无纹理、无模糊拖影）。"""
    import numpy as np
    h, w = img.shape[:2]
    patch = img[: max(1, int(h * 0.10)), : max(1, int(w * 0.10))].reshape(-1, 3)
    wall = np.median(patch, axis=0).astype(np.float32)
    t = (np.arange(pad_h, dtype=np.float32) / max(1, pad_h - 1))[:, None, None]
    grad = (np.clip(wall * 1.02, 0, 255))[None, None, :] * (1 - t) + wall[None, None, :] * t
    return np.clip(np.repeat(grad, w, axis=1), 0, 255).astype(np.uint8)


OUTPAINT_SCENE_DEFAULT = ("画面下方延续原有的地面、墙面和场景里的物件，"
                          "光线方向、色温和时间感与原图完全一致，不新增或替换任何景物。")
OUTPAINT_PROMPT = (
    "Outpaint the image: replace the solid gray areas with a seamless continuation "
    "of the scene, keeping the existing picture unchanged. "
    "{scene}"
)


def outpaint_916(path, seed=0, scene=None):
    """用 2.1 扩图 LoRA 把关键帧真实扩到 9:16（灰区补成场景延续）。

    2026-11-02 定（用户方案）：关键帧先按原图比例（4:5）干净生成，姿态/构图自由、
    不补白；画幅留到最后用扩图 LoRA 一次补成 9:16。扩出来的是真实场景（地面/墙面），
    不是灰边也不是模糊条，视频阶段拿到的就是正常竖幅照片。
    失败时退回「铺墙色补画幅」（normalize_916），不阻塞流程。

    2026-11-05：原来这段中文写死「原木色桌面和浅灰色墙面、室内自然光」，
    等于给每个项目都补一张咖啡桌——逛街分镜的下半截因此凭空长出桌面。
    现在由调用方按分镜传 scene（分镜写 outpaint_scene 可覆盖），默认只做"延续原场景"。
    """
    import cv2
    from PIL import Image
    img = cv2.imread(path)
    if img is None:
        return False
    h, w = img.shape[:2]
    if abs(w / h - 9 / 16) <= 0.010:
        return False
    try:
        W, H = 768, 1376
        im = Image.open(path).convert("RGB")
        im = im.resize((W, max(1, round(im.height * W / im.width))), Image.LANCZOS)
        if im.height >= H:
            im.crop((0, 0, W, H)).save(path)
            print(f"    裁到 9:16 {w}x{h} -> {W}x{H}")
            return True
        # 上下都留余量（上 22% / 下 78%），人物不会贴顶，头顶留白更自然
        pad_total = H - im.height
        top = round(pad_total * 0.22)
        canvas = Image.new("RGB", (W, H), (127, 127, 127))
        canvas.paste(im, (0, top))
        cnv = os.path.join(COMFY_INPUT, f"{PFX}_op_canvas.png")
        canvas.save(cnv)
        # 输出前缀必须唯一：以前固定用 f"{PFX}_op"，同一分镜里第二次扩图会撞名，
        # comfy_run 有可能捞到上一次（甚至别的画幅）的产物——实测出现过 9:16 帧变 1088×960。
        tag = f"{PFX}_op_{abs(hash((os.path.abspath(path), int(seed)))) % 100000:05d}"
        outs = comfy_run(
            os.path.join(WF_DIR, "Qwen21-Outpaint916.json"),
            {10: {0: os.path.basename(cnv)},
             40: {0: OUTPAINT_PROMPT.format(scene=(scene or OUTPAINT_SCENE_DEFAULT))},
             32: {0: tag},
             30: {0: int(seed)}},
            timeout=1200,
        )
        src = os.path.join(COMFY_OUTPUT, outs["32"]["images"][-1]["filename"])
        shutil.copy(src, path)
        # 扩完必须复核：不是 9:16 就退回铺墙色补画幅，绝不把歪画幅留给视频阶段
        if not _img_ratio_ok(path):
            print(f"    扩图产出画幅不对（{os.path.basename(src)}），退回铺墙色补画幅")
            return normalize_916(path)
        print(f"    扩图 9:16 {w}x{h} -> {W}x{H}")
        return True
    except Exception as e:
        print(f"    扩图失败（{e}），退回铺墙色补画幅")
        return normalize_916(path)


# ---------------------------------------------------------------- 动作时长估算
# 实测结论：H3 单段最短 5s，动作量填不满它会自己补（放慢、晃、来回走）。
# 单段动作量的自然单位是 1-3s，所以两三个动作 ≈ 4-5s —— 最优段长就是 5s。
ACT_SEC = [
    (("换步", "换腿", "重心"), 1.5, "换重心/换腿"),
    (("迈出一步", "迈一步", "迈出", "跨出"), 1.2, "迈一步"),
    (("两步", "三步", "两到三"), 3.0, "连走 2-3 步"),
    (("转头", "转正", "视线转", "转向镜头", "回望"), 2.0, "转头"),
    (("抬手", "抬起手", "拨到耳后", "拨发", "抬起右手"), 2.0, "手臂动作"),
    (("转身", "转回", "侧转"), 3.0, "转身"),
    (("后仰", "前倾", "立直", "侧倾", "肩线打开", "挺直"), 3.0, "躯干前后倾"),
    (("坐下", "站起身", "蹲"), 3.0, "坐/起"),
    (("停住", "站定", "停下", "缓缓停"), 1.5, "停步"),
    (("低头", "抬头", "抬向", "下落"), 1.5, "低头/抬头"),
    (("踏上", "抬脚", "抬腿", "前倾下沉", "膝盖"), 2.0, "抬腿/屈膝"),
    (("风吹", "头发被", "裙摆"), 3.0, "被动物理（风吹/裙摆）"),
]


# 幅度 → 能配的最长时长（超过就是"幅度撑不住时长"）
AMP_MAX = {"none": 0.0, "small": 5.0, "medium": 6.0, "large": 8.0}
# 这些动作不产生"可插值的姿态差"：腿脚编辑模型改不动，风吹只是画面流动
WEAK_BEATS = ("换重心", "换腿", "换步", "迈一步", "迈出", "跨出", "两步", "三步", "两到三",
              "膝盖", "屈膝", "抬脚", "踏上", "裙摆", "头发", "风吹", "停住", "停步", "站定", "缓慢",
              "连走", "走两", "走三", "行走")
AMP_CN = {"none": "无(必废)", "small": "S 小幅", "medium": "M 中等", "large": "L 大幅"}


def effective_amplitude(s, beats=None):
    """真实姿态变化 = 头/手/躯干的动作数量。腿脚、风吹、停步都不算。"""
    if s.get("amplitude") in AMP_MAX:
        return s["amplitude"], []
    if beats is None:
        beats, _ = estimate_action_seconds(s)
    eff = [b for b in beats if b and not any(w in str(b) for w in WEAK_BEATS)]
    n = len(eff)
    if n == 0:
        return "none", eff
    return ("small" if n == 1 else ("medium" if n == 2 else "large")), eff


def estimate_action_seconds(s):
    """估这段动作需要多少秒。显式 beats 优先，否则按键词累加。"""
    beats = s.get("beats")
    if isinstance(beats, list) and beats:
        total = 0.0
        for b in beats:
            best = 0.0
            for kws, sec, _ in ACT_SEC:
                if any(k in str(b) for k in kws):
                    best = max(best, sec)
            total += best or 2.0
        return total, list(beats)
    txt = " ".join(str(s.get(k, "")) for k in ("open_action", "end_action", "motion"))
    hits, total = [], 0.0
    for kws, sec, name in ACT_SEC:
        if any(k in txt for k in kws):
            hits.append(name)
            total += sec
    # 明确写"缓慢/慢慢/逐渐"的动作，放慢本身就是合法的时长填充
    if any(w in txt for w in ("缓慢", "慢慢", "缓缓", "逐渐", "轻轻")):
        total *= 1.3
        hits.append("缓慢(×1.3)")
    return total, hits


# ---------------------------------------------------------------- check
def cmd_check(sb_path, sb):
    segs = sb.get("segments") or []
    errs, warns = [], []
    if not segs:
        errs.append("segments 为空")
    # 两张参考图（2026-11-05 用户定稿）：没写 bg_ref 会静默退回老的照片锚 + 量化链路，
    # 画质和背景一致性都差一档，所以显式提示。
    is_t2i = (sb.get("keyframe_backend") or "").strip() == "krea2_t2i"
    # 文生视频（用户没给参考图）：分镜写了两张参考图的提示词，由 `refs` 造图并回写。
    # 造完之后和「用户直接给了图」完全等价，下面所有规则都按图生视频走。
    needs_refs = bool((sb.get("ref_person_prompt") or "").strip()
                      and (sb.get("ref_bg_prompt") or "").strip())
    if is_t2i:
        # 2026-11-07 晚纠正：t2i 直出全部关键帧是理解反了，只留作实验对照。
        warns.append("[已废弃] keyframe_backend=krea2_t2i（t2i 直出关键帧）不是文生视频正路；"
                     "请用 `refs` 先造人物图 + 无人空镜，再走 `frames` 双图链路")
        if not (sb.get("t2i_base_prompt") or "").strip():
            errs.append("t2i 实验分镜必须写 t2i_base_prompt（所有帧共用的人物/场景/机位锁）")
        warns.append("实验链路：跑 `t2i` 出关键帧；段 N 首帧 byte-for-byte 复制段 N-1 尾帧")
    elif needs_refs and not sb.get("reference_images"):
        warns.append("还没跑 `refs`：分镜已写 ref_person_prompt / ref_bg_prompt，"
                     "先执行 `refs` 生成两张参考图，再回来 check → frames")
    elif not (sb.get("bg_ref") or "").strip():
        warns.append("没写 bg_ref（无人的场景空镜）：将退回旧的「首帧固化锚 + 12 色量化」链路；"
                     "新片子建议写 bg_ref 指向一张纯背景空镜")
    if int(sb.get("keyframe_seed") or 0) == 100000:
        warns.append("keyframe_seed=100000 是已知坏种子；frames 阶段会自动替换成分镜派生的干净种子，"
                     "建议把该字段从分镜里删掉")
    total = 0
    for i, s in enumerate(segs, 1):
        sec = float(s.get("seconds") or sb.get("seconds_per_segment") or 5)
        total += sec
        for k in ("open_action", "end_action", "motion"):
            if not s.get(k):
                errs.append(f"第 {i} 段缺字段 {k}")
        if sec < 5:
            errs.append(f"第 {i} 段 {sec}s < 5s，H3 单段下限 5 秒")
        if sec > 10:
            warns.append(f"第 {i} 段 {sec}s > 10s，超过稳定区间，肢体崩坏概率上升")
        txt = f"{s.get('end_action','')} {s.get('motion','')} {s.get('open_action','')}"
        est, beats = estimate_action_seconds(s)
        # 变化幅度必须跟上时长（H3 只做 A→B 插值，差距不够它就会自己补）
        amp, eff = effective_amplitude(s, beats)
        if amp == "none":
            warns.append(
                f"第 {i} 段没有任何有效姿态变化：动作全落在腿脚/风吹上（{'、'.join(beats) or '未识别'}）。"
                f"编辑模型改不动腿，首尾帧会几乎一样，这段出来必然静止或乱晃。"
                f"把主动作换成头部/手臂/躯干"
            )
        elif sec > AMP_MAX[amp] + 0.01:
            warns.append(
                f"第 {i} 段 {sec:.0f}s 配 {amp} 幅度（{'、'.join(eff)}）：变化量撑不住这个时长，"
                f"{amp} 最多配 {AMP_MAX[amp]:.0f}s。要么加动作升到更高幅度，要么把 seconds 压到 {AMP_MAX[amp]:.0f}"
            )

        gap = sec - est
        if gap > 2.0 and gap / max(sec, 1e-6) > 0.40:
            warns.append(
                f"第 {i} 段动作量约 {est:.1f}s，设定 {sec:.0f}s，空出 {gap:.1f}s（{gap/sec:.0%}）："
                f"H3 会自己补（放慢/晃/来回走）。再加一个动作，或把 seconds 压到 {max(5, round(est+1)):.0f}"
            )
        elif est > 6.0:
            warns.append(
                f"第 {i} 段动作量约 {est:.1f}s（{' + '.join(beats)}）> 6s 稳定上限，建议拆成两段"
            )
        elif sec > est + 2.0:
            warns.append(
                f"第 {i} 段动作量约 {est:.1f}s，却给了 {sec:.0f}s，多出 {sec-est:.1f}s："
                f"H3 会用放慢或走来回填满。建议 seconds 改成 {max(5, round(est)):.0f}"
            )
        for b in BAD_ACTS:
            if b in txt:
                warns.append(f"第 {i} 段出现「{b}」，单段内不可完成，建议拆段")
        for kw, name in LEG_MAIN:
            if kw in f"{s.get('open_action','')} {s.get('end_action','')}":
                warns.append(
                    f"第 {i} 段把「{name}」当主动作：编辑模型改不动腿（实测腿照抄首帧），"
                    f"H3 会拿到两帧一样的腿。把主动作换成头部/手臂/躯干，腿只作伴随描述"
                )
                break
        for b in BAD_SCALE:
            if b in txt:
                warns.append(
                    f"第 {i} 段出现「{b}」：首尾帧会让人物变大，H3 会当成推镜并重绘脸部。"
                    f"改成画面内横向移动（人物大小不变）"
                )
                break
        # 文生视频（refs 造图）与已废弃的 t2i 实验链路，chain 缺省都视为连续动作段：
        # 用户 2026-11-07 明确要求「没给参考图的，后面首尾帧也要连续」。
        _default_chain = is_t2i or needs_refs
        _chained = ((s.get("chain", True) is not False) if _default_chain
                    else bool(s.get("chain")))
        if i > 1 and not _chained and not s.get("transition"):
            warns.append("第 {} 段未设 chain=true 且没写 transition，将按独立镜头处理（需要转场）；"
                         "换机位的切镜请显式写 transition: cut".format(i))

        # 方向契约：脚朝哪、身朝哪、脸朝哪必须自洽，否则首尾帧必然反向构图
        for lv, msg in D.check_segment(s, i):
            (errs if lv == "err" else warns).append(msg)
    miss = [str(i) for i, s in enumerate(segs, 1) if not (s.get("dir") or {}).get("heading")]
    if miss:
        warns.append(f"未写 dir 契约的段：{','.join(miss)}（方向只能从文字猜，出反向构图时无从校验，建议补 heading/body/face）")
    if not sb.get("reference_images") and not is_t2i and not needs_refs:
        errs.append("缺 reference_images（身份锚点）；没给图的话请先写 ref_person_prompt / "
                    "ref_bg_prompt 并跑 `refs`")
    elif sb.get("reference_images"):
        for r in sb["reference_images"]:
            if not os.path.exists(os.path.join(COMFY_INPUT, r)) and not os.path.exists(r):
                errs.append(f"参考图不存在: {r}")
    if total > 120:
        warns.append(f"总时长 {total:.0f}s > 120s，漂移与成本会明显上升")

    print(f"分镜: {sb.get('title','(未命名)')}  共 {len(segs)} 段 / {total:.1f}s")
    print(f"{'段':<3}{'设定':>5}{'动作量':>7}{'幅度':>8}  建议   动作节拍")
    for i, s in enumerate(segs, 1):
        sec = float(s.get("seconds") or sb.get("seconds_per_segment") or 5)
        est, beats = estimate_action_seconds(s)
        amp, eff = effective_amplitude(s, beats)
        if sec - est > 2.0 and (sec - est) / max(sec, 1e-6) > 0.40:
            adv = "加动作/压时长"
        elif est > 6.0:
            adv = "拆两段"
        else:
            adv = f"{max(5, round(est))}s"
        print(f"{i:<3}{sec:>4.0f}s{est:>6.1f}s{AMP_CN.get(amp, amp):>8}  {adv:<6} {' + '.join(beats)}")
    print(f"预计耗时: 约 {total/5*4:.0f} 分钟（每 5 秒约 4 分钟）")

    # ---- 项目一致性 lint（2026-11-05 用户定）---------------------------------
    # 用户原话：每个场景的提示词要符合主参考图或场景要求，不要照抄。
    # 所以这里做两件事：① 缺 wardrobe_items 就提醒补（不然款式细节全凭模型编）；
    # ② 扫一遍渲染出来的提示词，出现"不属于本项目"的景物/服装词就报警——
    #    这类词只可能来自模板残留或别的项目复制过来。
    FOREIGN_TERMS = ("原木色桌面", "浅灰色墙面", "白色背心", "室内自然光",
                     "白昼自然光", "咖啡", "水壶", "沙发", "床", "键盘", "橱窗", "步行街")
    # 语料只算"分镜自己写的东西"（场景/服装/动作字段），不能把渲染出来的提示词
    # 也算进去——否则提示词里抄来的词会自我认证，永远查不出来。
    def _desc_only(obj):
        if isinstance(obj, dict):
            return {k: _desc_only(v) for k, v in obj.items() if not str(k).startswith("prompt_")}
        if isinstance(obj, list):
            return [_desc_only(x) for x in obj]
        return obj
    corpus = json.dumps(_desc_only(sb), ensure_ascii=False)
    if (sb.get("keyframe_backend") or "qwen21") != "2511" and not sb.get("wardrobe_items"):
        warns.append("没写 wardrobe_items：首尾帧的袖长/衣摆/配饰会各编一套，"
                     "建议按主参考图逐项列出服装细节")
    # 2026-11-05：场景正面清单。负向串"不许有桌子/椅子/栏杆"压不住模型加景，
    # 必须正面点名"场景里只有这几样"（外部案例库 §2.8）。
    no_si = [i for i, s in enumerate(segs, 1) if not (s.get("scene_items") or sb.get("scene_items"))]
    if no_si:
        warns.append(f"第 {no_si} 段没写 scene_items：模型会自己在画面下方补家具（我们踩过木台面），"
                     f"建议正面列出\"场景里只有哪几样东西\"，例：脚下的石板路、身后的玻璃橱窗、暖黄橱窗灯")
    foreign_hits = set()
    for i, s in enumerate(segs, 1):
        try:
            pr = render_prompts(sb, s)[0]
        except Exception:
            continue
        for key in ("first", "last", "video"):
            text = pr.get(key) or ""
            for term in FOREIGN_TERMS:
                if term in text and term not in corpus:
                    foreign_hits.add(f"第 {i} 段 {key} 出现「{term}」")
    for h in sorted(foreign_hits):
        warns.append(f"疑似照抄了别的项目：{h}——本项目分镜里根本没有这个词，"
                     f"请把这段提示词改成符合主参考图/场景要求的描述")

    for e in errs:
        print(f"  [错误] {e}")
    for w in warns:
        print(f"  [提醒] {w}")

    if not errs and not warns:
        print("  校验通过")
    # 确认点 ① 要给人看的第二样东西就是提示词本身：顺手把预览页写出来，
    # 用户可以直接在页面上改这段提示词，不用再靠嘴描述。
    try:
        md_path, html_path = write_prompt_files(sb_path, sb)
        print(f"\n提示词已摊开（每段首帧/尾帧/视频）：\n  清单 {md_path}\n  预览 {html_path}")
        print("  改法：预览页里直接编辑 → 导出 JSON 覆盖原分镜；或让 agent 写回 prompt_* 字段")
    except Exception as e:
        print(f"  [提醒] 提示词预览生成失败（{e}），可单独跑 `prompt` 阶段")
    return 0 if not errs else 1


# ---------------------------------------------------------------- frames
FIRST_TMPL = (
    "{shot}：{open_action}，{chars}。{style}。"
    "{dir_lock}"
    "人物身份、脸型、五官、发型与参考图一致（同一个人）。{wardrobe_lock}"
    "{gaze}。"
    "{feet_first}\n"
    "{mouth_img}"
    "真实摄影，自然皮肤纹理，景别为{frame}，构图严格按此景别裁切，{solo_first}"
)
LAST_TMPL = (
    "保持图1中同一个人物、同一场景、同一机位、同一光照方向和同一镜头焦段，改变人物的肢体姿态："
    "{end_action}。"
    "{move}"
    "{dir_lock}"
    "{head}，脸与图2一致，{gaze}。{mouth_img}\n"
    "{feet_last}\n"
    "景别保持为{frame}，构图裁切范围与图1一致。"
    # 2026-11-05：原来写死"白昼自然光、不要黄昏暖调"，傍晚/夜景分镜被它顶成白天。
    "保持真实的皮肤纹理、原有的光线方向和色温，不要改变时间感。"
    "{wardrobe_lock}"
    "服装以图1为唯一标准，逐项照抄：{wardrobe_items}，"
    "衣摆长度、腰头和裙长/裤长与图1完全一致，不许露出腰腹，{solo_last}"
)

# 2.1 双图分工（2026-11-01 定）：图1 = 用户原始参考照，只负责人物身份；
# 图2 = 场景基准帧（首帧用上一段尾帧、尾帧用本段首帧），负责背景/桌面/机位/光照/画幅。
# 这样背景连续，人物每帧都锚回原图，不会像纯链式那样越跑越不像。
# 图2 场景锚句。第 1 段（或链式断掉时）没有场景图可当图2，此时换成文字搭景，
# 否则模型照着"图2=人物头像"出一张白墙空景，桌面全丢（2026-11-01 实测）。
SCENE_FROM_IMG = "场景、背景里的陈设和物品、机位、画幅和光照方向与图2保持一致。"
# 新链路（bg_ref = 无人场景空镜）：场景内容跟图2，但机位 / 人物大小交给 camera_line。
# 旧句会把全片每段锁成同一个正面全身构图，人像贴上去的（2026-11-05 用户指出）。
SCENE_FROM_BG = ("场景内容、背景里的陈设和物品、色温与图2保持一致；"
                 "机位和人物在画面里的大小按下面的镜头描述来，不受图2 约束。")
SCENE_FROM_TXT = "光线方向、色温和时间感与图1一致，曝光正常不过曝，场景本身不加不减任何景物。"

# 锚图卫生（2026-11-04 铁律）：第 1 段首帧会固化成全片图2。2.1 编辑模式会把图2
# 当底版逐像素复刻——锚上的微纹理、畸形解剖（桌下露出的歪脚）、凭空道具（杯口热气）
# 都会被搬进新帧并放大。实测坏帧斑点与锚图高频纹理空间相关 0.950（与人物参考图仅 0.051），
# 触发率约 8/15；换一张干净锚后同 pipeline 六帧零重试。
# 所以生成"即将成为锚"的那一帧时自动追加下面这句，分镜里 anchor_guard 可覆盖/置 false 关闭。
ANCHOR_GUARD = ("画面边缘完整收尾，四肢完整不被裁断，画面中不出现多余的脚、腿或手；"
                "道具表面干净，没有热气、蒸汽、烟雾或漂浮颗粒")

# 教训（2026-11-02 A/B 实测）：「9:16 竖构图中近景，……墙面：图1中人物……」这种
# 冒号标题式写法，2.1 会把冒号前的场景描述当图注忽略，桌子水壶全部不出，
# 只剩一张美颜半身像。场景必须写成普通叙述句（v3 单参考测试验证过的句式）。
# 另外「真实摄影/自然皮肤纹理/坐姿」三句不能删——删了会漂向美颜滤镜人像，
# 链式第二代会放大成满脸花斑。
# 教训（2026-11-02 实测，锐化过度根因）：模板式的「指令句 + 景别为X + 以图2为准」
# 会让 2.1 进入"高清复刻"模式，出图 Laplacian 飙到 225-268（满脸颗粒、硬阴影），
# 再被 H3 带进视频 = 观感上的"锐化过度"。改成段 1 验证过的叙述式（同一 seed
# Laplacian 降到 120-132），皮肤干净柔和。叙述式必须把"照片→同一位女生→动作→
# 场景→光线→构图"按自然语序串成一段话，不要分句下指令。
Q21_FIRST_TMPL = (
    "图1是一位年轻女性的照片。让图1中的同一位女生{open_action}："
    "保持她的身份、脸型、五官、眼神、发型和服装与图1完全一致，是同一个人，不要生成新的人物。"
    # 2026-11-05 用户要求：能描述到的、必须前后一致的东西逐个点出来，
    # 否则袖长、衣摆长度这类细节会一帧一个样。分镜写 wardrobe_items 可覆盖。
    "服装以图1为唯一标准，逐项照抄：{wardrobe_items}，"
    "不许把长袖改短袖或把袖子加长、不许改动衣摆长度和腰头高度、不许增减任何配饰。"
    "头发保持原状：发长、分缝、发丝走向和垂落位置都和图1一样，不要让头发飘动、散开、变形或改变遮挡关系；"
    "9:16 竖构图，{shot}，{frame}。"
    "{camera_line}"
    "{scene_line}"
    "{light_line}"
    "{feet_first}"
    "{mouth_img}"
    # 2026-11-05：全身景别下 2.1 会把皮肤（尤其腿）画成密集颗粒斑点，
    # 全图高频冲到 40~57%（段7 实测），换种子 2 次也压不住，得在提示词里明说。
    "真实摄影，皮肤细腻自然、没有密集毛孔颗粒和噪点斑块，腿部皮肤平滑不粗糙，"
    "画面中只有一个人，无文字水印。"
)
Q21_LAST_TMPL = (
    # 图2 = 本段首帧（同一镜头、同一套服装的唯一标准）；图1 只负责脸/身份。
    # 2026-11-05 重构：旧版整段 90% 篇幅在写"和 图2 完全一致 / 保持原状 / 不许改动"，
    # 只有一句在写"要改成什么样"，模型于是原样复制首帧——实测 10 段里 9 段
    # 「关键点位移 0.02（阈值 0.12）」，手臂该抬的没抬。现在把"必须变"提到最前、
    # "不变"限定到身份/服装/场景三样，并显式排除姿势。
    "图1 是一位年轻女性的照片，图2 是本段首帧，是同一个镜头。"
    "这一张画的是「动作做完之后」的样子：{end_action}。"
    "**姿势必须与图2 明显不同**——手臂位置、手的落点、头部朝向、上身倾角都要按上面的描述"
    "发生可见变化，不要原样复制图2 的站姿，不要只改表情。"
    "必须保持不变的只有三样："
    "① 身份：脸型五官发型与图1 是同一个人，不生成新的人物；"
    "② 服装与配饰：以图2为唯一标准逐项照抄，{wardrobe_items}，"
    "袖长与图2一样（长袖仍是长袖、到手腕的仍到手腕），衣摆和腰头位置不变，"
    "不改成短袖、不卷起或加长袖子、不增减任何配饰；"
    "③ 场景：背景里的店铺门面、招牌、陈列、地面材质和颜色保持图2 的样子，"
    "背景中不新增也不删除物件（动作里明确要求人物手持/端起的道具除外，"
    "道具要按动作描述出现、拿在手里，不能消失也不能悬浮）。"
    "头发保持发长和分缝不变，不飘散变形。"
    "9:16 竖构图，{shot}，{frame}。"
    "人物在画面中的尺度与图2 一致（不靠近也不远离镜头），机位、光照方向和色温不变；"
    "这个尺度一致只约束人物大小，不约束姿势。"
    "{mouth_img}"
    # 2026-11-05：全身景别下 2.1 会把皮肤（尤其腿）画成密集颗粒斑点，
    # 全图高频冲到 40~57%（段7 实测），换种子 2 次也压不住，得在提示词里明说。
    "真实摄影，皮肤细腻自然、没有密集毛孔颗粒和噪点斑块，腿部皮肤平滑不粗糙，"
    "画面中只有一个人，无文字水印。"
)

# 空 latent 生图的先验是"女性正面全身像"，dirlock 写得再细也掰不动这个先验。
# 实测：同一个 end_action 文字，编辑模式（尾帧）能生成正确侧身，生成模式（首帧）出正面。
# 所以首帧生成后要再走一次编辑，专门把身位转过来。
# 教训（2026-10-31）：不锁服装/光线时，编辑会把上衣改成露脐、光线改成黄昏逆光；
# 传 3 张参考图会把参考脸拼进画面（悬浮人脸）。所以这里只传图1，并把服装、色温、防泄漏写死。
POSE_FIX_TMPL = (
    "保持图1的场景、机位、光线方向、色温和时间感完全不变（白昼还是白昼、傍晚还是傍晚），"
    "不要变成黄昏、逆光或橙色暖调。"
    "{wardrobe_lock}"
    "人物身份、五官、发型和景别与图1完全一致。"
    "只改变人物的身体朝向和双脚方向："
    "{dir_lock}"
    "背景不动，人物在画面中的位置、身高和大小都不改变，脚仍踩在原来的地面位置。"
    "画面中只有一个人，不得出现拼贴边框、第二张脸或悬浮的人像，无文字水印。"
)

# 「头部仍然朝向镜头」会把背对镜头的人强行掰回头，制造"脚朝前、脸朝后"的反向构图。
# 默认改成继承首帧，需要回头时必须显式写 head 并在 dir.face 里声明。
DEFAULT_HEAD = "头部朝向与首帧保持一致，下巴角度不变"
DEFAULT_FRAME = "人物全身"
DEFAULT_GAZE = "视线保持与首帧相同的方向，眼神稳定不漂移"


def _end(x):
    """自定义句子补句号，避免拼进模板后和下一句粘在一起。"""
    x = (x or "").strip()
    return x if x.endswith(("。", "；", "！", ".")) else x + "。"


def action_locks(s, sb=None):
    """强动作/特殊表演豁免。

    首帧、尾帧、视频三个模板里都写死了「双脚着地、闭嘴、固定机位、只有一个人物」，
    那是为逛街走路类的稳定镜头服务的。追击、奔跑、喊叫、跟拍这类镜头会被这些句子直接摁死
    （2026-10-31 讨论「被霸王龙追」时发现）。用分段字段逐项放行：

      camera   跟拍/推拉/人物变大变小时用，替换「固定机位…」那一句
      mouth    需要张嘴喘气/喊叫时用，替换「双唇自然闭合…」
      feet     抬起脚/脚跟离地时用，写 "sprint" 走内置冲刺文案，或自己写整句
      pace     "快速、爆发式地" 之类，替换默认的「缓慢」
      extra    画面里允许出现的第二个东西（巨大影子、恐龙局部），替换「只有一个人」

    缺省返回值与原来的硬编码完全一致，老分镜行为不变。
    """
    mouth_img = "双唇自然闭合、嘴角平直，不微笑、不露牙、不张嘴。"
    mouth_video = ("面部稳定，双唇自然闭合，嘴角平直，嘴宽与参考图中的中性表情一致；"
                   "不微笑、不露牙、不张嘴、不咧嘴，嘴巴绝不突然放大。")
    solo_default = "画面中只有一个人，无文字水印。"

    feet = (s.get("feet") or "").strip()
    seated = (s.get("stance") or (sb or {}).get("stance") or "").strip() == "seated"
    if seated and not feet:
        feet_first = "人物坐在桌前，下半身被桌面遮住或在画幅外，画面里不出现站立的腿脚，不站起、不走动。"
        feet_last = "保持与图1相同的坐姿和坐高，不站起、不走动。"
    elif feet.lower() == "sprint":
        feet_first = ("至少有一只脚踩实在地面上（前脚掌或全脚掌均可），另一条腿可以蹬地伸直抬起、"
                      "后脚跟可以离地，两只脚尖都指向同一个方向，不得双脚同时腾空、不得劈叉。")
        feet_last = ("仍然是奔跑中的换步：支撑腿踩实地面、另一条腿蹬地抬起，"
                     "两只脚尖仍指向同一个方向且与图1完全一致，不得双脚同时腾空。")
    elif feet:
        feet_first = feet
        feet_last = feet + "脚尖方向与图1完全一致。"
    else:
        feet_first = "双脚完整着地、脚尖方向清晰可见，不交叉、不内八、不踮脚。"
        feet_last = "双脚完整着地、脚尖方向与图1完全一致，不交叉、不内八、不踮脚。"

    extra = s.get("extra")
    solo = f"画面中只有一个人，{extra}，无文字水印。" if extra else "画面中只有一个人，无文字水印。"
    frame = s.get("frame") or DEFAULT_FRAME

    return {
        "feet_first": feet_first,
        "feet_last": feet_last,
        "mouth_img": _end(s.get("mouth") or mouth_img),
        "mouth_video": _end(s.get("mouth") or mouth_video),
        "camera_lock": _end(
            s.get("camera")
            or ("固定机位，摄影机不推进、不后退、不缩放、不摇晃，"
                f"人物在画面中的身高和位置从头到尾保持不变，{frame}始终完整入镜。")),
        "pace": s.get("pace") or "缓慢",
        "solo_first": solo,
        "solo_last": solo,
    }


def seg_vars(sb, s):
    """合并全局与分段字段，并补上 head / frame / move / gaze 的默认值。"""
    d = dict(sb)
    d.update(s)
    d["head"] = s.get("head") or DEFAULT_HEAD
    d["frame"] = s.get("frame") or DEFAULT_FRAME
    d["move"] = s.get("move") or ""
    d["gaze"] = s.get("gaze") or DEFAULT_GAZE
    merged = dict(sb)
    merged.update(s)
    d["dir_lock"] = D.lock_line(merged)
    # shot 里可能带尾冒号（旧分镜的"标题："写法），叙述句式下要去掉，避免"：。"连标点
    d["shot"] = (d.get("shot") or "").strip().rstrip("：:")
    # 模板里已经写了"9:16 竖构图，{shot}"，分镜的 shot 开头也常带这一句，
    # 不去掉会渲染成"9:16 竖构图，9:16 竖构图，全身…"。
    d["shot"] = re.sub(r"^(?:\s*9:16\s*竖构图\s*[，,、]?\s*)+", "", d["shot"])
    d["scene_line"] = SCENE_FROM_BG if (sb.get("bg_ref") or "").strip() else SCENE_FROM_IMG
    # 每段首帧的镜头设计（2026-11-05 用户定）：机位 / 景别 / 人物比例 / 光影各写各的，
    # 同一镜头内由分镜保证一致，切镜才变。绝不再写「同一机位同一景别」把全片锁死。
    d["camera_line"] = (str(s.get("camera") or sb.get("camera") or "").strip()
                        or "镜头：平视全身景，人物在画面中央。")
    d["light_line"] = (str(s.get("light") or sb.get("light") or "").strip()
                       or "柔和自然光，人物身上的明暗方向与场景一致，脚下有自然的接触阴影。")
    d.update(action_locks(s, sb))
    wr = s.get("wardrobe") or sb.get("wardrobe") or ""
    d["wardrobe_lock"] = (
        f"服装一件都不许改：{wr}，衣长、裙长、颜色、款式与图中完全一致。"
        if wr else
        "服装款式、颜色、衣长与图中完全一致，不许改成露脐装、不许增减衣物。"
    )
    # 2026-11-05：首尾帧之间最容易漂的就是这些"没人点名"的细节。
    # 分镜写 wardrobe_items（字符串或列表）可以按项目自定义，不写就用这套通用清单。
    wi = s.get("wardrobe_items") or sb.get("wardrobe_items")
    if isinstance(wi, (list, tuple)):
        wi = "、".join(str(x) for x in wi)
    d["wardrobe_items"] = (str(wi).strip() if wi else
                           "领口形状、袖型和袖长、袖口结构（系带/褶皱/松紧）、"
                           "上衣长度与下摆位置、下装长度与腰头高度、腰带、项链耳饰、鞋子")
    # 声音（2026-11-05）：H3 自带音轨，不写就由模型随便给。
    # 分镜写 soundscape / music 覆盖；不写只给"与场景一致的稳定环境音"。
    sc = s.get("soundscape") or sb.get("soundscape")
    d["soundscape_line"] = ("环境音：" + str(sc).strip()
                            if sc else
                            "环境音：与画面场景一致的自然环境声，音量稳定，"
                            "不出现不该有的说话声、音乐或音效，无爆音、电流声或失真。")
    mus = s.get("music") or sb.get("music")
    d["music_line"] = ("配乐：" + str(mus).strip() if mus else "配乐：不加任何背景音乐。")
    # 场景正面清单（2026-11-05）：外部案例库 §2.8 的 "The room contains only A, B and C."
    # 负向串说不干净，必须正面点名"只有这几样"。分镜不写就用中性兜底（不含任何具体场景名词，
    # 免得又变成"照抄别的项目"——见 check 的 lint）。
    si = s.get("scene_items") or sb.get("scene_items")
    if isinstance(si, (list, tuple)):
        si = "、".join(str(x) for x in si)
    d["scene_items"] = (str(si).strip().rstrip("。") if si else
                        "原有的地面、人物身后的背景与建筑轮廓、光线和色温")
    d["beats_timeline"] = _beats_timeline(sb, s)
    return d


def _beats_timeline(sb, s):
    """把 beats 摊到时间轴上，生成单镜时间锚（2026-11-05）。

    来源：外部案例库 §2.6 雨夜站台的 `Shot 1 continues — 5.5s:` 写法。
    我们 15 秒单镜实测的问题：三个节拍全挤在前 50% 演完（motion_front 123%），
    后半段定格。根因是提示词只有"先…再…最后…"，一个时间点都没有。

    hold：最后留 0.4~1.2 秒收尾（不新增动作，只保留呼吸/眨眼这类微动），
    对应案例里 `The image ends while these tiny motions are still underway`。
    """
    beats = s.get("beats") or sb.get("beats") or []
    if isinstance(beats, str):
        beats = [x for x in re.split(r"[,，、;；]", beats)]
    bs = [str(b).strip() for b in beats if str(b).strip()]
    try:
        sec = float(s.get("seconds") or sb.get("seconds_per_segment") or 5)
    except (TypeError, ValueError):
        sec = 5.0
    if not bs or sec < 4:
        return ""
    hold = min(1.2, max(0.4, sec * 0.10))
    span = max(1.0, sec - hold)
    n = len(bs)
    pts = [f"{span * i / n:.1f}s {b}完成" for i, b in enumerate(bs, 1)]
    return ("时间锚（严格按这个节奏推进）：0.0s 保持首帧姿态；" + "；".join(pts) +
            f"；{sec:.0f}s 才停在尾帧姿态。每个节拍只在自己那个时间点完成，"
            "不许提前把后面的动作做完，也不许中途停住不动；"
            "最后一个节拍之后不要定格，保留呼吸、眨眼和发丝衣料的细微变化直到结束。")


def ref_name(sb):
    """返回参考脸的绝对路径（工作流里传 basename 即可）。

    2026-11-05 用户定：**参考图统一先 pad 成 9:16，然后全程都用这一张**。

    为什么必须这么做：2.1 的出图尺寸跟 image_1 的宽高比走（工作流注释原话
    「latent 跟它走」）。喂 422×367 的近方形参考图，出图就变成 1088×960 横图，
    于是每一帧都要再跑一次 outpaint_916 才能用——每段白花 2 次出图（约 1 分钟），
    10 段就是 10 分钟，还多两次"扩图撞车/扩出桌面"的机会。
    喂 768×1376 的 9:16 参考图，出图直接就是 768×1376，一张都不用扩。

    实测（2026-11-05，sb.json 段1 首帧）：参考图 pad 成 768×1376 后，
    出图 768×1376，场景仍是步行街橱窗前、下方是石板路、无家具、服装正确。

    和历史方案的区别：2026-11-02 那次"方案 A"是用**扩图 LoRA 重绘**参考图，
    清晰度 335→133 还出彩虹马赛克，被否决；这里只 **pad 画布 + 墙色渐变**，
    人物像素不重绘，所以不掉清晰度。分镜里写 `ref_pad_916: false` 可关掉。
    """
    refs = sb.get("reference_images") or []
    if not refs:
        # 纯文本 t2i（2026-11-07）：没有用户参考图时，拿自己出的段1首帧当身份锚，
        # 否则 push / merge 会因 sb["reference_images"][0] 直接 KeyError，
        # 而且新任务会继承模板 config 里别人的真人图（身份锚老 bug）。
        pfx = sb.get("prefix") or PFX
        p = os.path.join(COMFY_INPUT, f"{pfx}_s01_first.png")
        return p if os.path.exists(p) else ""
    r = refs[0]
    p = os.path.join(COMFY_INPUT, r)
    if not os.path.exists(p):
        p = os.path.abspath(r)
    if not os.path.exists(p) or not sb.get("ref_pad_916", True):
        return p
    base, ext = os.path.splitext(p)
    out = base + "_916" + (ext if ext.lower() in (".png", ".jpg", ".jpeg", ".webp") else ".png")
    try:
        q = to_916(p, out)
        if not _img_ratio_ok(q):
            print(f"  参考图 pad 后比例仍不对（{q}），退回原图")
            return p
        return q
    except Exception as e:
        print(f"  参考图 9:16 化失败（{e}），退回原图")
        return p


def to_916(src, out_path):
    """参考图统一fit到 768x1376（9:16，32 对齐）。

    2026-11-02 定稿配方（v3 实测能出桌子的关键）：等比缩放到宽 768 后，
    高度不足用「末行拉伸+重度模糊」向下补齐——人脸只占画面上半，
    下方留白给模型画桌面/身体。若改成中心裁剪，人脸怼满画面，
    模型只会出贴墙证件照（v8/v9 两次翻车实测）。
    """
    if os.path.exists(out_path) and os.path.getmtime(out_path) >= os.path.getmtime(src):
        return out_path
    im = _fit_916(Image.open(src).convert("RGB"))
    im.save(out_path)
    return out_path


def _fit_916(im):
    """参考图统一 fit 到 768x1376（9:16），人脸占上半，下方留白给桌面/身体。

    2026-11-02 二次修正：底部补白改用「背景墙色 → 略暗」的平滑渐变。
    原先的「末行拉伸 + 高斯模糊」会在底部留下一条糊带，模型把它当成纹理先验，
    导致脸部/皮肤出颗粒脏感（同 prompt 同 seed 实测：模糊补白 Lap=240、
    干净补白 Lap=124，肉眼就是"锐化过度"vs"干净真实"的差别）。
    几何完全不变（仍是等比缩放到宽 768 + 底部补到 1376），只换填充方式。
    """
    import numpy as np
    W, H = 768, 1376
    im = im.resize((W, max(1, round(im.height * W / im.width))), Image.LANCZOS)
    if im.height >= H:  # 太高 → 裁下沿
        return im.crop((0, 0, W, H))
    arr = np.asarray(im)
    patch = arr[: max(1, int(im.height * 0.12)), : max(1, int(W * 0.12))].reshape(-1, 3)
    wall = np.median(patch, axis=0).astype(np.float32)
    pad_h = H - im.height
    t = (np.arange(pad_h, dtype=np.float32) / max(1, pad_h - 1))[:, None, None]
    grad = wall[None, None, :] * (1 - t) + (wall * 0.955)[None, None, :] * t
    grad = np.repeat(grad, W, axis=1).astype(np.uint8)
    canvas = Image.new("RGB", (W, H))
    canvas.paste(im, (0, 0))
    canvas.paste(Image.fromarray(grad), (0, im.height))
    return canvas


def center_crop_916(ref, pfx):
    """2.1 的 latent 跟 image_1 走：参考图中心裁成 9:16 再缩到 752x1337，锁住出图尺寸。"""
    return to_916(ref, os.path.join(COMFY_INPUT, f"{pfx}_ref_916.png"))


def pose_headroom(img_path):
    """无人脸时的头顶留白兜底：人体检测框顶边 / 画面高（框顶≈发顶）。量不出返回 None。"""
    try:
        import numpy as np
        import cv2
        _, box = D.body_keypoints(img_path)
        if box is None:
            return None
        img = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        return max(0.0, float(box[1]) / img.shape[0])
    except Exception:
        return None


def face_sharpness(img_path):
    """脸部区域 Laplacian（统一缩到 256×256 再量）。

    为什么量脸不量全图：全图 Lap 会被内容纹理骗——手、头发、键盘多的帧天然高
    （实测同批 s01_last 全图 413、脸 479；s02_last 全图 125、脸却 196），
    「过锐化」是长在皮肤和五官上的，只有脸区域能把内容和锐度分开。
    实测带（256 尺度）：2.1 干净出图 110-200，真实参考照 278，
    指令式「高清复刻」帧 380-480（2026-11-04 咖啡分镜实测）。
    阈值默认 380，分镜里写 face_sharp_max 可覆盖。
    """
    try:
        import cv2
        img = cv2.imread(img_path)
        if img is None:
            return None
        det = Q.face_info(img)
        if not det or not det.get("bbox"):
            return None
        x1, y1, x2, y2 = [int(v) for v in det["bbox"]]
        pad = int((x2 - x1) * 0.15)
        x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
        y2 = min(img.shape[0], y2 + pad)
        crop = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        crop = cv2.resize(crop, (256, 256))
        return float(cv2.Laplacian(crop, cv2.CV_64F).var())
    except Exception:
        return None


def hf_ratio(img_path, thresh=60):
    """全图强梯度像素占比——「复刻模式」斑驳的全身指标。

    脸部 Lap 只看脸，四肢/身体皮肤的斑驳会漏网（2026-11-04 软锚实验：
    脸 262 达标但手臂大腿仍斑驳）。强梯度像素（Sobel 模长>thresh）占比：
    干净帧 ~8.5%，复刻斑驳帧 ~37.7%，区分度 4 倍以上。阈值默认 0.20。
    """
    try:
        import cv2
        import numpy as np
        img = cv2.imread(img_path)
        if img is None:
            return None
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
        mag = np.hypot(gx, gy)
        return float((mag > thresh).mean())
    except Exception:
        return None


def frame_quality(img_path, thresh=60):
    """分区画质指标，替代「全图 hf > 20%」的单一闸门（2026-11-05 18:16）。

    旧闸门在两类画面上判反：
      · 街景含橱窗框 / 石板缝等**合法硬边**，全图 hf 天然 24~26% → 误判成脏帧，无限重出；
      · 大色块锐化帧 hf 只有 12~16%，但每条边缘对比拉满 → 漏判。
    所以改用三项：
      hf    全图强梯度占比（只记录，不再单独拦截）
      edge  强梯度像素的平均模长（干净帧 120~172；复刻模式 220+）
      body  人物区（中间 44% 宽 × 18%~92% 高）强梯度占比（皮肤斑驳专用，干净 ~18% / 坏 54%）
    """
    try:
        import cv2
        import numpy as np
        im = cv2.imread(img_path)
        if im is None:
            return None
        hh, ww = im.shape[:2]
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
        m = np.hypot(gx, gy)
        hit = m > thresh
        return {
            "hf": float(hit.mean()),
            "edge": float(m[hit].mean()) if hit.any() else 0.0,
            "body": float(hit[int(hh * .18):int(hh * .92),
                             int(ww * .28):int(ww * .72)].mean()),
        }
    except Exception:
        return None


def frame_bad(sharp, q, sharp_max=380, fx=False):
    """首/尾帧画质闸门：脸过锐 / 边缘过硬 / 人物区斑驳，任一命中就换种子重出。

    2026-11-07 卡牌战斗实跑：发光特效题材（符文光带 / 光柱光丝 / 光翼网格 /
    环形光浪）和人物一起落在中央人物区，edge 与 body 会结构性超标（实测
    edge 205 / body 50.4%），但人物脸和衣服干干净净——按项目「静态指标测不出、
    验收只能实跑冒烟」的铁律，段里写 lighting_fx:true 时这两项只存档不重出，
    脸部锐度门仍保留，画面靠拼图人工确认 + 换种子闸门兜底。
    """
    if sharp is not None and sharp > sharp_max:
        return True, f"脸Lap {sharp:.0f}>{sharp_max:.0f}"
    if q is None or fx:
        return False, ""
    if q["edge"] > 200:
        return True, f"强边缘均值 {q['edge']:.0f}>200"
    if q["body"] > 0.35:
        return True, f"人物区高频 {q['body']:.1%}>35%"
    return False, ""


def quant_anchor(path, colors=12, tag="q"):
    """把「场景锚」剥成只剩布局的大色块，再喂给 image_2。

    为什么必须这么做（2026-11-05 八组对照实验，段7 首帧，同 seed/步数/提示词）：
        A 原锚(hf 13.3%)                     → 出图 hf 58.1%，脸 Lap 152
        B image_1 换成真 9:16 全身           → 61.4%（换参考图无效）
        C base20 二十步                      → 50.2%（加步数无效）
        D 提示词写"平整地面/背景虚化"        → 53.7%（改文案无效）
        E1 去掉 frame 里"石板路清晰可见"     → 57.2%（无效）
        F 锚做保边平滑(hf 5.6/3.8%)          → 54.2/51.8%（无效）
        F3 锚整图虚化(hf 1.5%)               → 23.3%（段7 有效，但段9 反被拖脏 13.5%→23.7%）
        I 只处理锚的地面区(hf 7.7%)          → 32.9/35.9%（无效）
        E0 干脆不给 image_2（单图）          → 11.0%（干净，但每段背景各自发挥，拼接会跳）
        J1 **MeanShift + 12 色量化锚**       → 16.0%（干净且保布局）★
        J2 线稿锚                            → 17.3%（略差，出镜像感）
    结论：2.1 的 image_2 不是"参考布局"，而是"照着这张照片重渲染"——锚里的任何高频
    纹理都会被逐像素复刻并按景别放大（全身景放大约 4 倍）。所以不是"锚脏"，而是
    "锚太像照片"。唯一的解法是让 image_2 只剩色块布局、没有可复刻的纹理。
    量化锚全片实跑：段5/7/9 = 17.4% / 16.0% / 16.9%，最差段从 58% 收敛到 17%，
    脸 Lap 从 152 降到 75。干净段会略升 3~5 点，换最差段降 42 点，净收益极大。

    colors 越大越保细节：首帧用 12（去纹理优先）；尾帧的图2 是本段首帧、要保服装
    细节（袖长/蕾丝边），用 24 更稳。
    """
    if not path or not os.path.exists(path):
        return path
    base, _ext = os.path.splitext(path)
    out = f"{base}_{tag}.png"
    try:
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
            return out
        import cv2
        import numpy as np
        im = cv2.imread(path)
        if im is None:
            return path
        sm = cv2.pyrMeanShiftFiltering(im, 24, 32)
        z = sm.reshape(-1, 3).astype(np.float32)
        cv2.setRNGSeed(0)  # kmeans 初始化固定，保证同一张锚多次跑结果一致
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
        _, lbl, cen = cv2.kmeans(z, colors, None, crit, 3, cv2.KMEANS_PP_CENTERS)
        q = cen.astype(np.uint8)[lbl.flatten()].reshape(im.shape)
        cv2.imwrite(out, q)
        print(f"  锚去纹理化 → {os.path.basename(out)}（{colors} 色量化，"
              f"hf {hf_ratio(path):.1%} → {hf_ratio(out):.1%}）", flush=True)
        return out
    except Exception as e:
        print(f"  锚量化失败（{e}），退回原锚——注意这会让出图纹理明显变脏", flush=True)
        return path


def cmd_frames(sb_path, sb, only=None, retry=False, seed_off=0):
    st = load_state(sb_path, sb)
    segs = sb["segments"]
    pfx = sb.get("prefix") or PFX
    w, h = sb.get("final_size", [720, 1280])
    ref = ref_name(sb)
    # 种子：全片统一（2026-11-02 定）。动作差异由提示词驱动，构图/背景一致性由
    # 统一种子+统一画布保证；质检发现首尾帧位移不足时，重试自动 +attempt 换噪声。
    # 2026-11-05：seed=100000 在本配置（int8 ray + viggle6 六步 + 768×1376）下
    # 是已知坏种子（复刻模式，全图高频 61.8%）。默认值改成按分镜路径派生：
    # 不同片子不同、同一片子可复现，且绝不会撞上 100000。
    import hashlib
    _h = hashlib.md5(os.path.abspath(sb_path).encode("utf-8")).hexdigest()
    _default_seed = int(_h[:8], 16) % 2_000_000_000 + 1
    _ks = int(sb.get("keyframe_seed") or 0)
    if _ks == 100000:
        print("[种子] keyframe_seed=100000 是已知坏种子，自动改用分镜派生的干净种子", flush=True)
        _ks = 0
    base_seed = (_ks or _default_seed) + seed_off
    sharp_max = float(sb.get("face_sharp_max") or 380)

    def seed_first(i):
        return base_seed

    def seed_last(i):
        return base_seed
    for i, s in enumerate(segs, 1):
        if only and i != only:
            continue
        key = str(i)
        rec = st["segments"].setdefault(key, {})
        # 提示词统一由 render_prompts 出：分段/全局写了 prompt_* 就用用户的版本
        # 关键帧后端：qwen21（默认，2.1 ray 微调，双图分工：图1=人物 / 图2=场景基准帧）
        # 或 2511（构图服从优先，但脸会向"标准美型"漂）。分镜里写 "keyframe_backend" 切换。
        use21 = (sb.get("keyframe_backend") or "qwen21") != "2511"
        # 2.1 加速档：分镜里写 "keyframe_lora" 选帧生成采样器档位——
        #   省略/null = base20（20 步 / CFG 1.0，质量基准）
        #   "lightning4" = Lightning 4 步 LoRA（合并加载，最快）
        #   "viggle6"    = Viggle turbo v0.3 r128（unmerged 运行时旁路 + 专属 Sigmas，干净度最接近基准）
        # 两者都保持 CFG 1.0（蒸馏模型硬性要求），种子/提示词/参考图编辑点不变。
        lora_v = sb.get("keyframe_lora")
        lora_suf = {"lightning4": "-L4", "viggle6": "-V6"}.get(lora_v, "")

        def wf21(name):
            return os.path.join(WF_DIR, name.replace(".json", f"{lora_suf}.json"))
        pr, _pc = render_prompts(sb, s)
        # 两张参考图（2026-11-05 18:16 用户定稿，见 SKILL.md「两张参考图」）：
        #   image_1 = 人物参考图 ref；image_2 = 场景空镜 bg_ref（全片恒定、必须无人）
        # bg_ref 存在就走新链路：原样喂图、不做 12 色量化、不固化首帧当锚、不链式。
        # 没写 bg_ref 的老分镜仍走 scene_ref / 固化锚链路，保持兼容。
        bg_base = os.path.basename((sb.get("bg_ref") or "").strip()) or None
        bg_path = os.path.join(COMFY_INPUT, bg_base) if bg_base else None
        use_bg = bool(bg_path and os.path.exists(bg_path))
        if bg_base and not use_bg:
            die(f"bg_ref 指向的场景空镜不存在：{bg_path}")
        if use_bg and i == 1:
            print(f"[两张参考图] 图1={os.path.basename(ref)}  图2={bg_base}（原样，不量化）", flush=True)
        # 旧链路：场景锚（恒定）——第 1 段首帧生成后固化，之后所有帧的图2 都指它。
        # 2026-11-02：分镜里写了 scene_ref 就用它当全程恒定的图2。
        scene_ref = sb.get("scene_ref")
        anchor = (os.path.join(COMFY_INPUT, scene_ref) if scene_ref
                  else os.path.join(COMFY_INPUT, f"{pfx}_anchor.png"))
        scene_txt = sb.get("scene_text") or SCENE_FROM_TXT
        # 外部指定的锚（scene_ref）也要先体检：脏锚会让全片每帧都复刻放大（见 ANCHOR_GUARD 注释）
        if i == 1 and not use_bg and scene_ref and os.path.exists(anchor):
            a_hf = hf_ratio(anchor)
            if a_hf is not None and a_hf > 0.20:
                print(f"[段1] ⚠ 指定锚 {scene_ref} 高频 {a_hf:.1%} > 20%（带斑驳纹理）——"
                      f"已自动做量化去纹理兜底，但锚本身脏会连布局一起歪，建议换干净锚", flush=True)
        if use21 and not use_bg and not (os.path.exists(anchor) and (i > 1 or scene_ref)):
            # 场景锚还没建立（第 1 段或锚丢失）：图2 只有人物头像，改由文字搭景
            pr["first"] = pr["first"].replace(SCENE_FROM_IMG, scene_txt)
            pr["last"] = pr["last"].replace(SCENE_FROM_IMG, scene_txt)
        # 锚图卫生：这一帧会固化成全片图2，自动追加"无裁断肢体/无多余人体局部/无蒸汽"约束。
        # 分镜里 anchor_guard 可写自定义句覆盖，写 false 关闭（不推荐）。
        if use21 and not use_bg and i == 1 and not scene_ref and not rec.get("first"):
            guard = sb.get("anchor_guard", ANCHOR_GUARD)
            if guard:
                pr["first"] = pr["first"].rstrip("。") + "。" + str(guard).rstrip("。") + "。"
        if retry:
            for k in ("first", "last", "qa_frames", "pose_fixed", "mirrored", "face_leak", "last_yaw_over"):
                rec.pop(k, None)
        first_name = f"{pfx}_s{i:02d}_first.png"
        last_name = f"{pfx}_s{i:02d}_last.png"

        ref_basename = os.path.basename(ref)

        # 首帧：chain 段一律复制上一段尾帧（byte-for-byte），两个后端都一样。
        # 2026-11-05 修：2.1 分支原来「不复制首帧」，导致段间首尾是两张独立生成的图，
        # 只能靠 0.25s 叠化糊接缝；但文档（工作流关系.md §3.2 / SKILL.md「改尾帧」）里
        # chain 的语义本来就是「段 N 尾帧就是段 N+1 首帧，同一张图」。
        # 2.1 的尾帧编辑模板又明确写「图2 是本段首帧，是同一个镜头」——
        # 首帧不继承，这句话就成了假的，尾帧机位/服装都会漂。
        # 要换机位/光影就写 chain:false 当切镜，别让连续动作段各自生首帧。
        # 文生视频（refs 造图）分镜：chain 缺省视为连续，与 check / push / cmd_t2i 一致。
        # 有参考图的图生视频仍要求显式 chain:true（既有语义，不动）。
        _ref_mode = bool((sb.get("ref_person_prompt") or "").strip()
                         and (sb.get("ref_bg_prompt") or "").strip())
        _chained = ((s.get("chain", True) is not False) if _ref_mode
                    else bool(s.get("chain")))
        if _chained and i > 1:
            prev = st["segments"].get(str(i - 1), {}).get("last")
            if prev and os.path.exists(prev):
                shutil.copy(prev, os.path.join(COMFY_INPUT, first_name))
                rec["first"] = os.path.join(COMFY_INPUT, first_name)
                print(f"[段{i}] 首帧 = 段{i - 1} 尾帧（链式，byte-for-byte）"
                      + ("；图2 场景空镜仍恒定" if use21 else ""), flush=True)
            else:
                print(f"[段{i}] 链式要求上一段尾帧缺失，改为独立生成")
                s["chain"] = False

        if not rec.get("first") or not os.path.exists(rec["first"]):
            print(f"[段{i}] 生成首帧（原生 9:16）...", flush=True)
            # 锐度闸门：同一 seed 在编辑模式下可能系统性出「高清复刻」帧
            # （实测 seed 100000 连续 5 帧脸部 Lap 456-486，+1 后全部干净），
            # 所以生成后量脸，过锐就换种子重出，最多 3 次。
            for attempt in range(3):
                if use21:
                    # 2.1：图1 = 用户原图（原比例，不补白——latent 跟它走，出图就是原比例）
                    # 图2 = 场景锚（第1段首帧）；锚还没建立时走单图 + 文字搭景
                    pad916 = os.path.join(COMFY_INPUT, ref_basename)
                    if use_bg:
                        # 新链路：图2 = 场景空镜，**原样喂，不量化**。
                        # 量化是 seed=100000 时期为照片锚加的历史包袱；空镜 + 随机 seed 不需要。
                        first_i2 = bg_base
                    elif os.path.exists(anchor) and (i > 1 or scene_ref):
                        first_i2 = os.path.basename(quant_anchor(anchor))
                    else:
                        first_i2 = None
                    if first_i2 is not None:
                        outs = comfy_run(
                            wf21("Qwen21-SceneRef916.json"),
                            {
                                10: {0: os.path.basename(pad916)},
                                11: {0: first_i2},
                                40: {0: pr["first"]},
                                32: {0: f"{pfx}_s{i:02d}_first"},
                                30: {0: seed_first(i) + attempt},
                            },
                        )
                    else:
                        outs = comfy_run(
                            wf21("Qwen21-SingleRef916.json"),
                            {
                                10: {0: os.path.basename(pad916)},
                                40: {0: pr["first"]},
                                32: {0: f"{pfx}_s{i:02d}_first"},
                                30: {0: seed_first(i) + attempt},
                            },
                        )
                else:
                    outs = comfy_run(
                        os.path.join(WF_DIR, "Qwen2511-FirstFrame916.json"),
                        {
                            10: {0: ref_basename}, 11: {0: ref_basename}, 12: {0: ref_basename},
                            28: {0: pr["first"]},
                            40: {0: w, 1: h},
                            32: {0: f"{pfx}_s{i:02d}_first"},
                            30: {0: seed_first(i) + attempt},
                        },
                    )
                src = os.path.join(COMFY_OUTPUT, outs["32"]["images"][-1]["filename"])
                shutil.copy(src, os.path.join(COMFY_INPUT, first_name))
                rec.setdefault("first_sec", []).append(LAST_SEC)
                _fp = os.path.join(COMFY_INPUT, first_name)
                sharp = face_sharpness(_fp)
                q = frame_quality(_fp)
                hfr = q["hf"] if q else hf_ratio(_fp)
                bad, why = frame_bad(sharp, q, sharp_max, fx=bool(s.get("lighting_fx")))
                if attempt < 2 and bad:
                    print(f"[段{i}] 首帧画质闸门命中（{why}），第 {attempt + 1}/3 次重出...", flush=True)
                    continue
                rec["first_sharp"] = round(sharp, 1) if sharp is not None else None
                rec["first_hf"] = round(hfr, 4) if hfr is not None else None
                rec["first_edge"] = round(q["edge"], 1) if q else None
                rec["first_body"] = round(q["body"], 4) if q else None
                break
            rec["first"] = os.path.join(COMFY_INPUT, first_name)
            save_state(sb_path, st)

        # 首帧方向闸门（闭环）：生成先验是正面像 → 编辑模式转身 → 测 yaw →
        # 不符就换种子重转，3 次仍不符就水平镜像兜底（方向百分百正确，
        # 代价是挎包/发缝等不对称细节左右互换，记录在 state 的 mirrored 字段）。
        dv, dv_ok = D.get_dir(s)
        target = dv.get("body")
        if dv_ok and target in (D.LEFT, D.RIGHT) and not rec.get("pose_fixed"):
            try:
                nf = D.count_faces(rec["first"])
                if nf and nf > 1:
                    print(f"[段{i}] 警告：首帧检测到 {nf} 张人脸（参考图泄漏或路人入镜）", flush=True)
                    rec["face_leak"] = nf
            except Exception:
                pass
            ok = False
            for attempt in range(3):
                try:
                    y = D.face_yaw_signed(rec["first"])
                except Exception as e:
                    print(f"[段{i}] 姿态检测跳过（{e}）", flush=True)
                    break
                if D.yaw_matches(y, target):
                    ok = True
                    break
                print(f"[段{i}] 姿态修正 第 {attempt + 1}/3 次：目标{D.CN[target]}，当前 yaw {y}...", flush=True)
                if use21:
                    fix916 = to_916(os.path.join(COMFY_INPUT, first_name),
                                    os.path.join(COMFY_INPUT, f"{pfx}_first916_{i}.png"))
                    if use_bg:
                        fix_i2 = bg_base
                    else:
                        _s2 = anchor if os.path.exists(anchor) else fix916
                        fix_i2 = os.path.basename(quant_anchor(_s2))
                    outs = comfy_run(
                        wf21("Qwen21-SceneRef916.json"),
                        {
                            10: {0: ref_basename},
                            11: {0: fix_i2},
                            40: {0: pr["pose_fix"]},
                            32: {0: f"{pfx}_s{i:02d}_fix"},
                            30: {0: seed_first(i) + attempt},
                        },
                    )
                else:
                    outs = comfy_run(
                        os.path.join(WF_DIR, "Qwen2511-Reference-Edit.json"),
                        {
                            10: {0: first_name}, 11: {0: ref_basename}, 12: {0: ref_basename},
                            28: {0: pr["pose_fix"]},
                            32: {0: f"{pfx}_s{i:02d}_fix"},
                            30: {0: seed_first(i) + attempt},
                        },
                    )
                src = os.path.join(COMFY_OUTPUT, outs["32"]["images"][-1]["filename"])
                shutil.copy(src, os.path.join(COMFY_INPUT, first_name))
                rec.setdefault("pose_fix_sec", []).append(LAST_SEC)
                save_state(sb_path, st)
            if not ok:
                try:
                    y = D.face_yaw_signed(rec["first"])
                except Exception:
                    y = None
                if not D.yaw_matches(y, target):
                    fp = os.path.join(COMFY_INPUT, first_name)
                    Image.open(fp).transpose(Image.FLIP_LEFT_RIGHT).save(fp)
                    rec["mirrored"] = True
                    print(f"[段{i}] 3 次修正未达{D.CN[target]}，已水平镜像兜底（挎包/发缝等不对称细节左右互换）", flush=True)
            rec["pose_fixed"] = True
            save_state(sb_path, st)

        # 场景锚固化：第 1 段首帧定稿后复制为全程唯一的图2 场景基准
        # （放到 9:16 归一化之后再固化，锚就是最终画幅，不再二次 _fit_916 补白）
        _need_anchor = use21 and not use_bg and not scene_ref and (i == 1 or not os.path.exists(anchor))

        # 尾帧：以首帧派生，并做转头幅度门控
        # 实测：end_action 写"视线移向远处"，编辑模式会演成转头 55°，H3 会把转头演成转身。
        # 所以尾帧 yaw 必须贴住首帧：|Δyaw| > 0.35 就换种子重出，2 次仍超限只警告不拦截
        # （尾帧不能镜像——会把背景和服装也翻转，破坏与首帧的场景连续性）。
        if not rec.get("last") or not os.path.exists(rec["last"]):
            print(f"[段{i}] 生成尾帧（以首帧派生）...", flush=True)
            ya = yb = None  # 三次都栽在复刻模式下时 for-else 会走到，别让这里 UnboundLocal
            for attempt in range(3):
                if use21:
                    # 图1 = 用户原图（人物身份），图2 = 场景锚（缺锚时退回本段首帧）
                    first916 = to_916(os.path.join(COMFY_INPUT, first_name),
                                      os.path.join(COMFY_INPUT, f"{pfx}_first916_{i}.png"))
                    # 2026-11-05 用户要求：尾帧的图2 必须是**本段首帧**，不能再拿全片场景锚。
                    # 场景锚是第 1 段的首帧，跟本段隔着好几个动作/角度，模型照它画会把袖长、
                    # 衣摆、配饰重新编一遍（实测同段首尾袖子长短不一）。本段首帧本来就是在锚上
                    # 生成的，场景连续性不会丢，但服装细节有了同一镜头的硬基准。
                    # 尾帧图2 = 本段首帧（同一镜头的服装 / 机位基准，装束才不会前后不一）。
                    # 新链路原样喂；旧链路才做 24 色量化。只喂一代，不会代际累积。
                    tail_i2 = (os.path.basename(first916) if use_bg else
                               os.path.basename(quant_anchor(first916, colors=24, tag="q24")))
                    outs = comfy_run(
                        wf21("Qwen21-SceneRef916.json"),
                        {
                            10: {0: ref_basename},
                            11: {0: tail_i2},
                            40: {0: pr["last"]},
                            32: {0: f"{pfx}_s{i:02d}_last"},
                            30: {0: seed_last(i) + attempt},
                        },
                    )
                else:
                    outs = comfy_run(
                        os.path.join(WF_DIR, "Qwen2511-Reference-Edit.json"),
                        {
                            10: {0: first_name}, 11: {0: ref_basename}, 12: {0: ref_basename},
                            28: {0: pr["last"]},
                            32: {0: f"{pfx}_s{i:02d}_last"},
                            30: {0: seed_last(i) + attempt},
                        },
                    )
                src = os.path.join(COMFY_OUTPUT, outs["32"]["images"][-1]["filename"])
                shutil.copy(src, os.path.join(COMFY_INPUT, last_name))
                rec.setdefault("last_sec", []).append(LAST_SEC)
                # 锐度门：脸部 Lap 超阈值 = 2.1 进了「高清复刻」模式，换种子重出。
                # 放在 yaw 检查之前——过锐帧即使构图对了也不能用。
                _lp = os.path.join(COMFY_INPUT, last_name)
                sharp = face_sharpness(_lp)
                q = frame_quality(_lp)
                hfr = q["hf"] if q else hf_ratio(_lp)
                bad, why = frame_bad(sharp, q, sharp_max, fx=bool(s.get("lighting_fx")))
                if bad:
                    print(f"[段{i}] 尾帧画质闸门命中（{why}），第 {attempt + 1}/3 次重出...", flush=True)
                    continue
                rec["last_sharp"] = round(sharp, 1) if sharp is not None else None
                rec["last_hf"] = round(hfr, 4) if hfr is not None else None
                rec["last_edge"] = round(q["edge"], 1) if q else None
                try:
                    ya, yb = D.face_yaw_signed(rec["first"]), D.face_yaw_signed(os.path.join(COMFY_INPUT, last_name))
                except Exception:
                    ya = yb = None
                if ya is None or yb is None or abs(yb - ya) <= 0.35:
                    if ya is not None and yb is not None:
                        print(f"[段{i}] 尾帧转头幅度合格（Δyaw {yb - ya:+.2f}）", flush=True)
                    break
                print(f"[段{i}] 尾帧转头过大（yaw {ya:+.2f} → {yb:+.2f}），第 {attempt + 1}/3 次重出...", flush=True)
            else:
                if ya is not None and yb is not None:
                    print(f"[段{i}] 警告：尾帧转头幅度 3 次均超限（Δyaw {yb - ya:+.2f}），"
                          f"H3 可能把转头演成转身，建议人工确认或把视线变化从 end_action 里去掉", flush=True)
                    rec["last_yaw_over"] = round(abs(yb - ya), 3)
                else:
                    print(f"[段{i}] 警告：尾帧 3 次重出仍不合格（复刻模式/姿态检测失败），"
                          f"当前这张未通过质量门，建议人工确认或 --retry 换种子重跑", flush=True)
                    rec["last_gate_failed"] = True
            rec["last"] = os.path.join(COMFY_INPUT, last_name)
            save_state(sb_path, st)

        # 画幅归一：4:5 关键帧用扩图 LoRA 真实扩成 9:16（失败退回铺墙色）
        # 扩图场景必须来自本项目分镜（outpaint_scene），没写就只做"延续原场景"，
        # 不允许再用任何项目专属的景物描述。
        _op_scene = (sb.get("outpaint_scene") or "").strip() or None
        for k, p in (("first", rec.get("first")), ("last", rec.get("last"))):
            if p and os.path.exists(p):
                outpaint_916(p, seed_first(i) + (700 if k == "last" else 0), scene=_op_scene)
                # 扩图后验：底部要是被补出一大块木色（旧 bug 的桌面/台面），当场说出来，
                # 别等到用户看完片子才骂人。
                wr = warm_bottom_ratio(p)
                if wr is not None:
                    rec[f"{k}_warm_bottom"] = round(wr, 3)
                    if wr > 0.32:
                        print(f"[段{i}] 警告：{k} 帧底部木色占比 {wr:.0%}，"
                              f"疑似扩图补出了桌面/台面，出片前先看一眼这张图", flush=True)

        # 场景锚固化（9:16 定稿后）
        if _need_anchor and rec.get("first") and os.path.exists(rec["first"]):
            if os.path.abspath(rec["first"]) != os.path.abspath(anchor):
                shutil.copy(rec["first"], anchor)
                print(f"[段{i}] 场景锚已固化: {os.path.basename(anchor)}", flush=True)
            else:
                print(f"[段{i}] 场景锚已就位（首帧即锚，跳过自拷贝）", flush=True)
            # 锚图体检：没有可靠的静态指标能预判（实测旧锚中频带 5.86 / 新锚 6.44，
            # 静态看旧锚甚至更"干净"，却让 8/15 帧进复刻模式）。唯一可靠的验收是实跑冒烟：
            # `pipeline.py anchor_probe --storyboard sb.json` 用这张锚连跑 3 个种子，
            # 全部 全图高频<15% 且 脸部 Lap<380 才收。这里只提醒，不假装能判。
            print(f"[段{i}] 锚图已固化——请目视确认：无畸形肢体/多余人体局部、无凭空道具、"
                  f"无蒸汽浮尘；不确定就跑 anchor_probe 做 3 种子冒烟验收", flush=True)

        # 亮度归一到锚图（2026-11-05）：质检对「偏离锚 >8」是硬 FAIL，而这是提示词
        # 决定的场景明暗，换种子重出修不掉（段2 实测 -12.8/-18.5 重出 3 次仍 FAIL）。
        # 放在锚固化之后、质检之前，这样质检量到的是归一后的值。
        # 新链路（bg_ref）已由提示词统一光照，且 align_exposure 曾把橱窗灯推爆过曝，
        # 所以跳过；旧链路才按锚图归一。
        _expo_ref = None if use_bg else (anchor if os.path.exists(anchor) else rec.get("first"))
        if _expo_ref:
            for k in ("first", "last"):
                p = rec.get(k)
                if p and os.path.exists(p):
                    align_exposure(_expo_ref, p)

        # 首尾帧质检
        f1, f2 = Q.image_metrics(rec["first"]), Q.image_metrics(rec["last"])
        e1, e2 = f1.pop("_emb", None), f2.pop("_emb", None)
        issues = []
        # 尺度一致：尾帧人物变大 → H3 会推镜并重绘脸部（实测会直接变成大特写+换脸）
        if f1.get("face_ratio") and f2.get("face_ratio"):
            scale = (f2["face_ratio"] / f1["face_ratio"]) ** 0.5
            if scale > 1.08:
                issues.append(
                    f"首尾帧人物尺度放大 {scale:.2f}×（>1.08），H3 会推镜并重绘脸部；"
                    f"把 end_action 改成画面内横向移动，或让尾帧人物略小"
                )
        for tag, path, m in (("首帧", rec["first"], f1), ("尾帧", rec["last"], f2)):
            if m.get("ratio_dev_916", 0) > Q.TH["ratio_tol"]:
                issues.append(f"{tag} 比例 {m['size']} 不是精确 9:16")
            # 锐度门（质检兜底）：尾帧重试环里已经拦过一次，这里把两帧都量出来存档，
            # 超阈值照样 FAIL——首帧过锐没有自动重试，由 agent 按失败码 --retry。
            sp = face_sharpness(path)
            if sp is not None:
                key = "first_sharp" if tag == "首帧" else "last_sharp"
                rec[key] = round(sp, 1)
                if sp > sharp_max:
                    issues.append(
                        f"{tag} 脸部过锐（Lap {sp:.0f} > {sharp_max:.0f}），"
                        "2.1 进了「高清复刻」模式；确认提示词是叙述式后换种子重跑")
            # 分区画质闸门（2026-11-05）：全图 hf 只存档不拦截——街景的橱窗框 /
            # 石板缝是合法硬边，会把 hf 顶到 24~26%；真正判脏看强边缘均值与人物区。
            qc = frame_quality(path)
            if qc is not None:
                key = "first_hf" if tag == "首帧" else "last_hf"
                rec[key] = round(qc["hf"], 4)
                rec["first_edge" if tag == "首帧" else "last_edge"] = round(qc["edge"], 1)
                if qc["edge"] > 200 or qc["body"] > 0.35:
                    msg = (f"{tag} 画质指标偏高（强边缘均值 {qc['edge']:.0f} > 200 或 "
                           f"人物区高频 {qc['body']:.1%} > 35%）")
                    if s.get("lighting_fx"):
                        # 发光特效本来就是人物区里的高频/强边缘，静态指标结构性失效。
                        # 按「验收只能实跑冒烟」铁律降级存档，靠拼图人工确认。
                        issues.append(f"[提示] {msg}，本段是发光特效，须看拼图确认人物脸部干净")
                    else:
                        issues.append(f"{tag} 画质闸门命中（强边缘均值 {qc['edge']:.0f} > 200 或 "
                                      f"人物区高频 {qc['body']:.1%} > 35%），疑似复刻模式斑驳——换种子重跑")
            # 特写段本来就要切头顶，留白/占比阈值不适用。
            # 无人脸（背影/侧后）时 insightface 不给 headroom，缺省 0 会把所有背影段误判 FAIL，
            # 此时退回人体检测框顶边（≈头顶含头发）来量留白。
            hr = m.get("headroom")
            if hr is None:
                hr = pose_headroom(path)
            if not s.get("closeup") and hr is not None and hr < Q.TH["headroom_min"]:
                issues.append(f"{tag} 头顶留白仅 {hr:.4f}")
            if ((m.get("mouth") or 0) > Q.TH["mouth_open_max"]
                    and (m.get("yaw") or 0) < Q.TH["yaw_max_for_mouth"]
                    and not s.get("mouth")):   # 提示词明写张嘴喘气时不判缺陷
                issues.append(f"{tag} 嘴是张开的（{m.get('mouth', 0):.3f} > {Q.TH['mouth_open_max']}）")
            # 曝光一致性：2.1 照锚图复制场景会系统性提亮（实测段2/3 V+10、过曝 3%），
            # H3 会把关键帧的曝光漂移原样带进视频，且跨段拼接时墙面亮度跳变。
            ev, eo = exposure_stats(path)
            if ev is not None:
                # 曝光基准优先用「本片第 1 段首帧」：锚是扩图后的主参考，本身比出图亮一档，
                # 拿它当基准会把每一帧都判成偏暗（实测 -8 ~ -14）。帧与帧比才是真正要保的。
                base_img = (st["segments"].get("1", {}).get("first")
                            if os.path.exists(st["segments"].get("1", {}).get("first") or "")
                            else anchor)
                av, ao = exposure_stats(base_img) if os.path.exists(base_img) else (None, None)
                # 两张参考图新链路 + 段级 light（2026-11-05 三镜实跑）：
                # 用户明确要求机位/景别/光影逐段变化（远景柔光 → 橱窗暖光 → 侧逆光），
                # 跨段亮度差 20+ 是设计结果，不是缺陷。旧闸门拿段1首帧卡 ±8 会把
                # 段2（-22.6 / -19.8）、段3（-8.0）全判 FAIL，换种子重出也修不掉。
                # 这种段只查「同一镜头内首/尾曝光是否跳」，跨段差异降级为提示存档。
                designed_light = bool(use_bg and str(s.get("light") or "").strip())
                if designed_light:
                    other = rec["last"] if tag == "首帧" else rec["first"]
                    ov = (exposure_stats(other)[0]
                          if other and os.path.exists(other) else None)
                    # 发光特效题材（符文点亮 / 光卡浮出 / 光柱与光浪）本来就会让
                    # 同一镜头内亮度抬高 8~15，这是剧情要求、不是曝光漂移。
                    # 段里写 lighting_fx:true 时把段内阈值放宽到 ±14（默认仍 ±8）。
                    fx = s.get("lighting_fx") is True
                    tol_ev = 14 if fx else 8
                    if ov is not None and abs(ev - ov) > tol_ev:
                        issues.append(f"{tag} 段内曝光跳变 {ev - ov:+.1f}（>±{tol_ev}），"
                                      "同一镜头首尾帧亮度不一致；换种子重出或统一 light")
                    elif av is not None and abs(ev - av) > 8:
                        issues.append(f"[提示] {tag} 亮度偏离段1首帧 {ev - av:+.1f}"
                                      "（本段 light 有意设计了不同光影，不计入 FAIL）")
                elif av is not None and abs(ev - av) > 8:
                    issues.append(f"{tag} 亮度偏离场景锚 {ev - av:+.1f}（阈值 ±8），整段会偏亮/偏暗")
                # 过曝阈值跟着锚走：主参考图本身偏亮时，帧跟着亮是正常的，别误判。
                # 新链路 + 段级 light（2026-11-05 三镜链式实跑）：白天街景的玻璃反光
                # 和天空本来就有 ~2% 高亮像素，而段1首帧这个锚自身几乎 0%，
                # ao+0.015 = 1.7% 会把 2.0% 判成「墙面或高光死白」，肉眼根本没有死白。
                # 分段设计光影时放宽到 3%（仍能抓真过曝），跨段明暗由上面的曝光门管。
                if designed_light:
                    over_th = max(0.030, (ao or 0) + 0.020)
                else:
                    over_th = max(0.015, (ao or 0) + 0.015)
                if eo > over_th:
                    issues.append(f"{tag} 过曝像素 {eo * 100:.1f}%（>{(over_th) * 100:.1f}% 相对锚），墙面或高光死白")
        sim = Q.cos(e1, e2) if (e1 is not None and e2 is not None) else None
        if sim is not None and sim < Q.TH["frame_sim_min"]:
            issues.append(f"首尾帧相似度 {sim:.3f} 偏低（可能不是同一人）")

        # 方向一致性：躯干背对镜头却脸正对镜头 = 回头构图，H3 会顺着脸的方向走
        # 例外：heading=still 的静止段，缓慢转头/移开视线本身就是设计好的动作，
        # Δyaw 超限降级为提示，不算 FAIL（实测等车看远处 3 个种子全部 Δyaw≈0.6，属正常）。
        still_seg = dv.get("heading") == D.STILL
        soft_prefix = "段内脸水平转动幅度偏大"
        try:
            _, dmsgs, dinfo = D.check_frame_pair(s, rec["first"], rec["last"])
            hard = [x for x in dmsgs if not (still_seg and x.startswith(soft_prefix))]
            soft = [f"[提示] {x}" for x in dmsgs if still_seg and x.startswith(soft_prefix)]
            issues.extend(hard + soft)
            print(f"[段{i}] 方向: {dinfo}", flush=True)
        except Exception as e:
            print(f"[段{i}] 方向识别跳过（{e}）", flush=True)

        # 动作变化量：H3 只做首帧到尾帧的插值，两帧同姿态它要么演成静止要么自己乱动
        try:
            bm = D.body_motion(rec["first"], rec["last"])
            if bm is None:
                issues.append("姿态检测失败（画面里没有完整人体）")
            # disp 是 12 个关键点（双臂+双腿+躯干）的平均，绝大多数段是"腿脚站定、只动上半身"，
            # 平均会把手臂的大位移稀释掉：实测段1 右腕动了 0.88 躯干长，disp 才 0.14。
            # 所以"手臂整体有明显变化"（arm≥0.20，约等于整条手臂离开原位）单列一条通过路径，
            # 否则抱臂/叉腰这类贴身动作永远卡在 0.11 附近被误杀（段8 实测 0.11 / arm 0.22）。
            if bm["disp"] < 0.12 and bm["arm"] < 0.20:
                issues.append(
                    "首尾帧几乎同姿态（关键点位移 {:.2f}，手臂 {:.2f}；阈值 disp 0.12 / arm 0.20）："
                    "H3 只做插值，两帧一样必然演成静止或乱编动作；"
                    "把 end_action 改成手臂/躯干上的可见动作（手最可靠，腿改不动）".format(
                        bm["disp"], bm["arm"]))
            else:
                rec["motion"] = round(bm["disp"], 3)
                print("[段{}] 动作量 OK（disp {:.2f}，主要变化：{}）".format(i, bm["disp"], bm["max_kpt"]), flush=True)
                if bm["zoom"] > 1.15:
                    issues.append("尾帧人物放大 {:.2f} 倍，H3 会推镜换脸".format(bm["zoom"]))
        except Exception as e:
            print(f"[段{i}] 动作量检测跳过（{e}）", flush=True)

        # 2026-11-05：带「[提示]」前缀的属于"看着像问题、实测能接受"（主要是转头幅度），
        # 不该把整段判 FAIL——否则每段都要为一条提示重出，纯浪费时间。
        _hard = [x for x in issues if not x.startswith("[提示]")]
        rec["qa_frames"] = {"verdict": "PASS" if not _hard else "FAIL", "issues": issues,
                            "id_sim": round(sim, 4) if sim else None,
                            "face_sharp": {"first": rec.get("first_sharp"),
                                           "last": rec.get("last_sharp")}}
        print(f"[段{i}] 首尾帧质检: {'PASS' if not _hard else 'FAIL'}"
              f"{'（' + str(len(issues) - len(_hard)) + ' 条提示不计入）' if len(issues) > len(_hard) else ''}",
              flush=True)
        for x in issues:
            print(f"        - {x}")
        save_state(sb_path, st)

    print("\n首尾帧完成。给用户看缩略图确认后再跑 video。")


# ---------------------------------------------------------------- adopt
# 用户自己出了首尾帧（别处生成的、PS 改过的、手挑的），想直接用。
# 约定一个目录：<分镜名>.frames/ 里放 s01_first.png / s01_last.png（png/jpg/webp 都认），
# 跑 adopt 就收编：转 png → 复制进 ComfyUI input → 归一化到精确 9:16 → 写进 state → 质检。
# 之后 frames 因为 state 里已有图会跳过生成，video 直接吃这套图。
def _pick_source(d, i, kind):
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG"):
        p = os.path.join(d, f"s{i:02d}_{kind}{ext}")
        if os.path.exists(p):
            return p
    return None


def cmd_adopt(sb_path, sb, only=None, src_dir=None, no_norm=False):
    st = load_state(sb_path, sb)
    pfx = sb.get("prefix") or PFX
    segs = sb.get("segments") or []
    base = os.path.splitext(sb_path)[0]
    d = src_dir or (base + ".frames")
    if not os.path.isdir(d):
        die(f"没有目录 {d}\n  把图按 s01_first.png / s01_last.png 命名放进去再跑")
    got = []
    for i, s in enumerate(segs, 1):
        if only and i != only:
            continue
        rec = st["segments"].setdefault(str(i), {})
        for kind in ("first", "last"):
            src = _pick_source(d, i, kind)
            if not src:
                continue
            dst = os.path.join(COMFY_INPUT, f"{pfx}_s{i:02d}_{kind}.png")
            Image.open(src).convert("RGB").save(dst)
            if not no_norm:
                normalize_916(dst)
            rec[kind] = dst
            got.append((i, kind, src))
            print(f"[段{i}] {kind} ← {os.path.basename(src)}")
        save_state(sb_path, st)
    if not got:
        die(f"{d} 里没找到 s01_first.png 这类文件")

    print(f"\n收编 {len(got)} 张，质检：")
    ref_emb = None
    try:
        if sb.get("reference_images"):
            ref_emb = Q.image_metrics(ref_name(sb)).get("_emb")
    except Exception:
        ref_emb = None
    if ref_emb is None:
        print("  参考脸没解析出人脸，跳过身份证校验（自己看图确认是不是同一个人）")
    print(f"{'段':<4}{'比例':>6}{'尺度':>7}{'参考脸':>8}{'首尾':>7}{'留白':>7}{'动作量':>8}  结论")
    bad = 0
    for i, s in enumerate(segs, 1):
        if only and i != only:
            continue
        rec = st["segments"].get(str(i), {})
        f, l = rec.get("first"), rec.get("last")
        if not (f and os.path.exists(f)):
            continue
        if not (l and os.path.exists(l)):
            print(f"{i:<4}只有首帧，尾帧缺（video 要两张）")
            bad += 1
            continue
        m1, m2 = Q.image_metrics(f), Q.image_metrics(l)
        e1, e2 = m1.pop("_emb", None), m2.pop("_emb", None)
        issues = []
        dev = max(m1.get("ratio_dev_916") or 0, m2.get("ratio_dev_916") or 0)
        if dev > Q.TH["ratio_tol"]:
            issues.append(f"不是精确 9:16（偏差 {dev:.3f}）")
        scale = None
        if m1.get("face_ratio") and m2.get("face_ratio"):
            scale = (m2["face_ratio"] / m1["face_ratio"]) ** 0.5
            if scale > 1.08:
                issues.append(f"尾帧人物放大 {scale:.2f}×，H3 会推镜换脸")
        if m1.get("face_ratio") is None or m2.get("face_ratio") is None:
            issues.append("有一张没检测到人脸（背影/侧后属正常，但要目视确认是同一人）")
        ids = []
        if ref_emb is not None:
            for tag, e in (("首帧", e1), ("尾帧", e2)):
                if e is not None:
                    v = Q.cos(ref_emb, e)
                    ids.append(v)
                    if v < Q.TH["id_sim_min"]:
                        issues.append(f"{tag}与参考脸相似度 {v:.3f}（<{Q.TH['id_sim_min']}），不像同一个人")
        sim = Q.cos(e1, e2) if (e1 is not None and e2 is not None) else None
        if sim is not None and sim < Q.TH["frame_sim_min"]:
            issues.append(f"首尾帧之间相似度 {sim:.3f}，差得太多（可能不是同一人/同一场景）")
        hr = m1.get("headroom")
        if not s.get("closeup") and hr is not None and hr < Q.TH["headroom_min"]:
            issues.append(f"首帧头顶留白 {hr:.4f} 偏小")
        disp = None
        try:
            bm = D.body_motion(f, l)
            if bm:
                disp = bm["disp"]
                if disp < 0.12:
                    issues.append(f"首尾帧几乎同姿态（{disp:.2f} < 0.12），这段出来会静止")
        except Exception as e:
            print(f"    动作量检测跳过（{e}）")
        idcol = f"{min(ids):.3f}" if ids else "—"
        print(f"{i:<4}{(1 - dev):>6.2f}{(scale or 0):>7.2f}"
              f"{idcol:>8}{(sim or 0):>7.3f}"
              f"{(hr if hr is not None else -1):>7.3f}{(disp or 0):>8.2f}  "
              f"{'PASS' if not issues else 'FAIL'}")
        for x in issues:
            print(f"      - {x}")
        if issues:
            bad += 1
    print(f"\n{len(got)} 张已收编，{bad} 段有问题。" if bad else
          f"\n{len(got)} 张已收编，质检通过。接着跑 video。")
    return 0


# ---------------------------------------------------------------- video
VIDEO_TMPL = (
    "<Picture1>、<Picture2> 分别定义本段首帧和尾帧构图。<Picture3> 定义<女主角>的身份、脸型、五官、发型和服装。\n"
    "<女主角>从首帧姿态{pace}过渡到尾帧姿态：{motion}。"
    "动作在本段时长内匀速、连续地完成，最后一帧才达到尾帧姿态，中途不停顿、不静止。\n"
    "行进方向单一：全程朝双脚脚尖所指的方向移动，<Picture1> 里脚尖指向哪边就只朝哪边走，每一步都朝脚尖方向迈出。"
    "头部和视线可以转向别处，但转身不带动脚步：即使回头看，脚步也绝不能朝脸的方向迈，"
    "绝不后退、不倒退、不转身、不原路返回、不朝反方向迈步，人物在画面中的移动方向从头到尾不改变。\n"
    "{shot}，{camera_lock}\n"
    "{gaze}。\n"
    # 2026-11-05：视频阶段也会把袖子变长变短、把衣摆改掉，逐项点名更稳。
    "服装从第一帧到最后一帧不许变：{wardrobe_items}，全程与 <Picture1> 完全一致，"
    "不卷袖子、不加减外套、不换鞋、不增减任何配饰。\n"
    "{mouth_video}\n"
    "镜头全程固定：不推近、不拉远、不切特写，人物脸部在画面中的大小从头到尾保持不变。\n"
    # 2026-11-05：15 秒单镜实测，H3 会在最后一两秒凭空补一张木凳/台面到画面下方。
    # 关键帧本身干净，纯属模型自己加景，必须显式禁止。
    # 2026-11-05 二次：改成"正面清单"（外部案例库 §2.8：The room contains only A, B and C）——
    # 负向串"不许有桌子/椅子/栏杆"模型会漏，正面点名"只有这几样"才压得住。
    "场景里只有 <Picture1> 里已经存在的这些东西：{scene_items}；"
    "除此之外不加入任何家具、长椅、桌子、台面、栏杆或前景遮挡物，"
    "画面下方只有原有的地面，场景里的物件从头到尾不增不减。\n"
    # 2026-11-05：单镜时间锚（外部案例库 §2.6 的 `Shot 1 continues — 5.5s:` 写法）。
    # 此前 15 秒段三个动作全挤在前 50% 演完（motion_front 123%），后半段定格，
    # 根因是提示词只有"先…再…最后…"，没有时间点。
    "{beats_timeline}\n"
    # 2026-11-05 用户给的格式里有 soundscape / music 两段：H3 是带音轨生成的，
    # 我们以前从来没描述过声音，等于把这一半留给模型随便编。
    "{soundscape_line}\n{music_line}\n"
    "不穿模、不跳帧、不切镜，无文字、字幕和水印。"
)

# H3LongVideoDirector 是"一段一段自己往下串"的节点（内部循环 + 续帧），
# 它每段只吃一句 prompt，没有首帧/尾帧之分（关键帧它自己出）。
# 所以这段话要把开场姿态、收尾姿态、运动过程全写进一句里。
DIRECTOR_TMPL = (
    "同一成年女性，身份、脸型、五官、发型严格参考 <Picture1>。{wardrobe_lock}"
    "{shot}。开场姿态：{open_action}；结束姿态：{end_action}。"
    "整个过程：{motion}。动作在本段时长内匀速完成，最后一帧才到结束姿态。"
    "{gaze}。{mouth_img}画面中只有一个人，无文字、字幕和水印。"
)


# ---------------------------------------------------------------- 提示词摊开
# 确认点 ① 最常被打断的一句话是"这段提示词到底是什么，我想改一句"。
# 以前提示词只活在模板里，用户看不到也改不了——于是每次改一句话都得口述给 agent。
# 现在 check 就把每段的首帧/尾帧/视频提示词全部渲染出来，落成两个文件：
#   <name>.prompts.md    人读清单，直接贴进对话
#   <name>.prompts.html  预览页：分镜卡片 + 首尾帧缩略图 + 可直接编辑的提示词框
# 回填字段 prompt_first / prompt_last / prompt_video / prompt_pose_fix 优先于模板，
# 分段写 > 全局写 > 模板。改过之后 frames / video 直接吃自定义提示词，不再套模板。
PROMPT_SPEC = [
    ("first", "prompt_first", "首帧（生成）"),
    ("last", "prompt_last", "尾帧（编辑）"),
    ("pose_fix", "prompt_pose_fix", "首帧转身修正（编辑·只在需要侧身/背面时用）"),
    ("video", "prompt_video", "视频（H3 首尾帧插值）"),
    ("segment", "prompt_segment", "整段（ComfyUI Director 用）"),
]


def _consistency_tail(key, v):
    """首/尾帧的"前后一致"后缀（2026-11-05 用户要求）。

    用户原话：首尾帧的装束有时候不同，生成尾帧时要把首帧给模型参考，
    否则衣服袖子什么的有时候都不一样长；能描述到的、要前后一致的东西都要说清楚。
    所以这里把服装细节逐项点名，并明确"以哪张图为唯一标准"。
    """
    items = v.get("wardrobe_items") or ""
    if key == "last":
        return ("服装和配饰以图2（本段首帧）为唯一标准，逐项照抄：" + items + "，"
                "袖长必须和图2一样长（到手腕的仍到手腕，不许缩成七分或挽起），"
                "抬手臂时袖口跟着手腕一起移动、始终停在手腕处，不许滑落到小臂或手肘；"
                "衣摆长度和腰头位置不变，不许增减任何配饰；"
                "场景、背景里的陈设、机位、画幅和光照方向也与图2一致，"
                "不新增也不删除背景里的物件。")
    return ("服装和配饰以图1为唯一标准，逐项照抄：" + items + "，"
            "袖长、衣摆长度不许改变，袖口停在手腕处，不许增减任何配饰。")


def render_prompts(sb, s):
    """一段的五份提示词。分段或全局写了 prompt_* 就直接用它，不再套模板。"""
    tmpl = {"first": FIRST_TMPL, "last": LAST_TMPL,
            "pose_fix": POSE_FIX_TMPL, "video": VIDEO_TMPL, "segment": DIRECTOR_TMPL}
    if (sb.get("keyframe_backend") or "qwen21") != "2511":
        tmpl["first"], tmpl["last"] = Q21_FIRST_TMPL, Q21_LAST_TMPL
    v = seg_vars(sb, s)
    out, custom = {}, []
    for key, field, _ in PROMPT_SPEC:
        raw = s.get(field) or sb.get(field)
        if isinstance(raw, str) and raw.strip():
            txt = raw.strip()
            # 2026-11-05：手写的首/尾帧提示词也要挂上"逐项照抄"的锁，
            # 否则用户自己写的那段经常漏掉袖长/衣摆/配饰，首尾两帧各编一套。
            if key in ("first", "last") and sb.get("keyframe_backend", "qwen21") != "2511":
                txt = txt.rstrip("。") + "。" + _consistency_tail(key, v)
            out[key] = txt
            custom.append(key)
        else:
            vv = v
            # 2.1 精简模板：动作句里已经写了嘴/笑就别再补一遍表情锁（会读成两条指令打架）
            if tmpl is not None and key in ("first", "last") and sb.get("keyframe_backend", "qwen21") != "2511":
                act = v.get("open_action" if key == "first" else "end_action") or ""
                if any(t in act for t in ("唇", "嘴", "笑")):
                    vv = dict(v, mouth_img="")
            out[key] = tmpl[key].format(**vv)
    return out, custom


def needs_pose_fix(s):
    """侧身/背面段才会走转身修正，预览里也只对它显示 pose_fix 提示词。"""
    dv, ok = D.get_dir(s)
    return ok and dv.get("body") in (D.LEFT, D.RIGHT)


def _seg_rows(sb, sb_path):
    st = {"segments": {}}
    try:
        st = load_state(sb_path, sb)
    except Exception:
        pass
    rows = []
    for i, s in enumerate(sb.get("segments") or [], 1):
        sec = float(s.get("seconds") or sb.get("seconds_per_segment") or 5)
        est, beats = estimate_action_seconds(s)
        amp, _eff = effective_amplitude(s, beats)
        if sec - est > 2.0 and (sec - est) / max(sec, 1e-6) > 0.40:
            adv = "加动作/压时长"
        elif est > 6.0:
            adv = "拆两段"
        else:
            adv = f"{max(5, round(est))}s"
        prompts, custom = render_prompts(sb, s)
        rows.append({
            "i": i, "s": s, "sec": sec, "est": est, "beats": beats,
            "amp": amp, "adv": adv, "prompts": prompts, "custom": custom,
            "rec": (st.get("segments") or {}).get(str(i), {}) or {},
        })
    return rows


def _thumb_b64(path, width=280):
    if not path or not os.path.exists(path):
        return None
    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size
        im = im.resize((width, max(1, round(h * width / w))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=72)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _md_prompts(sb, rows, src):
    title = sb.get("title") or os.path.basename(src)
    total = sum(r["sec"] for r in rows)
    L = [f"# {title} · 分镜与提示词", "",
         f"共 {len(rows)} 段 / {total:.0f}s · 生成于 {time.strftime('%Y-%m-%d %H:%M')}", "",
         "> 这就是真正送进工作流的文字。要改就改这里，两种回填方式：",
         "> ① 把改好的整段贴回 `storyboard.json` 的 `prompt_first` / `prompt_last` / `prompt_video`"
         "（写在某一段里只影响那一段，写在根上是全局默认，一写就不再套模板）；",
         "> ② 或者直接把意思告诉 agent，让它写回分镜再重出这三样文件。", ""]
    for r in rows:
        s = r["s"]
        L.append(f"## 段 {r['i']} · {r['sec']:.0f}s · {AMP_CN.get(r['amp'], r['amp'])} · "
                 f"动作量 {r['est']:.1f}s · 建议 {r['adv']}")
        for k, label in (("shot", "机位"), ("open_action", "开场"), ("end_action", "收尾"),
                         ("motion", "运动"), ("frame", "景别"), ("gaze", "视线"),
                         ("camera", "机位豁免"), ("extra", "画面第二主体")):
            if s.get(k):
                L.append(f"- {label}：{s[k]}")
        if r["beats"]:
            L.append(f"- 节拍：{' + '.join(r['beats'])}")
        L.append("")
        keys = ["first", "last", "video", "segment"] + (["pose_fix"] if needs_pose_fix(s) else [])
        for key in keys:
            label = dict((k, lb) for k, _f, lb in PROMPT_SPEC)[key]
            tag = "（自定义）" if key in r["custom"] else ""
            L += [f"### {label}{tag}", "", "```text", r["prompts"][key], "```", ""]
    return "\n".join(L)


HTML_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6eb;--ink:#1a1d21;--dim:#6b7280;
--acc:#2563eb;--warn:#b45309;--ok:#047857}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:9;background:rgba(255,255,255,.94);
backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:14px 22px}
h1{margin:0 0 4px;font-size:18px}
.meta{margin:0;color:var(--dim);font-size:12px;word-break:break-all}
.toolbar{margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{font:inherit;font-size:12px;padding:6px 12px;border-radius:7px;cursor:pointer;
border:1px solid var(--line);background:#fff;color:var(--ink)}
button:hover{border-color:#c3c9d3}
#export{background:var(--acc);border-color:var(--acc);color:#fff}
#status{font-size:12px;color:var(--dim)}
main{padding:18px 22px 60px;max-width:1080px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
margin-bottom:16px;overflow:hidden}
.card-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
padding:10px 14px;border-bottom:1px solid var(--line);background:#fafbfc}
.idx{font-weight:600;font-size:13px}
.tag{font-size:11px;color:var(--dim);background:#f1f3f6;border-radius:999px;padding:2px 9px}
.tag.amp{color:var(--ok);background:#e9f7f1}
.adv{font-size:11px;color:var(--warn);margin-left:auto}
.card-body{display:grid;grid-template-columns:auto 1fr;gap:16px;padding:14px}
.shots{display:flex;gap:8px}
.shot{width:118px;text-align:center;font-size:11px;color:var(--dim)}
.shot img{width:100%;border-radius:7px;display:block;border:1px solid var(--line)}
.ph{width:118px;height:210px;display:grid;place-items:center;border-radius:7px;
border:1px dashed #cfd4dc;color:#a8aeb8;font-size:11px;background:#fbfcfd}
.info{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
gap:6px 18px;font-size:13px}
.info b{color:var(--dim);font-weight:500;margin-right:6px;font-size:12px}
.prompts{padding:0 14px 14px}
.pblock{border:1px solid var(--line);border-radius:9px;margin-top:10px;overflow:hidden}
.pblock.dirty{border-color:var(--acc);box-shadow:0 0 0 2px rgba(37,99,235,.09)}
.phead{display:flex;gap:8px;align-items:center;padding:7px 10px;background:#fafbfc;
border-bottom:1px solid var(--line);font-size:12px}
.plabel{font-weight:600}
.badge{font-size:10px;color:var(--dim);border:1px solid var(--line);
border-radius:999px;padding:1px 7px}
.badge.custom{color:var(--acc);border-color:#bfd3fb;background:#eef4ff}
.phead button{margin-left:auto;padding:3px 9px;font-size:11px}
textarea{width:100%;border:0;padding:10px 12px;font:12.5px/1.75 ui-monospace,Consolas,
"Microsoft YaHei",monospace;color:var(--ink);background:#fff;resize:vertical;
min-height:96px;display:block;outline:none}
footer{max-width:1080px;margin:0 auto;padding:0 22px 50px;color:var(--dim);font-size:12px}
code{background:#eef1f5;border-radius:4px;padding:1px 5px}
#out{max-width:1080px;margin:0 auto 40px;padding:14px 18px;background:#fff;
border:1px solid var(--acc);border-radius:12px}
#out h2{font-size:14px;margin:0 0 4px}
#out p{margin:0 0 10px;font-size:12px;color:var(--dim)}
#outjson{width:100%;min-height:150px;font:12px/1.6 ui-monospace,Consolas,monospace;
border:1px solid var(--line);border-radius:8px;padding:10px;resize:vertical}
"""

HTML_JS = """
const SB = __SB__;
const FILE = __FILE__;
const FIELDS = {first:'prompt_first',last:'prompt_last',pose_fix:'prompt_pose_fix',video:'prompt_video'};
const tas = Array.from(document.querySelectorAll('.pblock textarea'));
const def = new Map(tas.map(t => [t, t.value]));
const status = document.getElementById('status');
const dirtyList = () => tas.filter(t => t.value !== def.get(t));
function grow(t){ t.style.height='auto'; t.style.height=(t.scrollHeight+4)+'px'; }
function refresh(){
  tas.forEach(t => { grow(t); t.closest('.pblock').classList.toggle('dirty', t.value !== def.get(t)); });
  const n = dirtyList().length;
  status.textContent = n ? n + ' 处提示词已改，记得导出'
    : '提示词可以直接改；改完点导出，用它覆盖原分镜 JSON 就能接着跑';
}
tas.forEach(t => {
  t.addEventListener('input', refresh);
  t.addEventListener('input', () => grow(t));
});
document.querySelectorAll('.copy').forEach(btn => {
  btn.onclick = async () => {
    const ta = btn.closest('.pblock').querySelector('textarea');
    try { await navigator.clipboard.writeText(ta.value); btn.textContent = '已复制'; }
    catch (e) { ta.select(); document.execCommand('copy'); btn.textContent = '已复制'; }
    setTimeout(() => btn.textContent = '复制', 1200);
  };
});
document.getElementById('reset').onclick = () => {
  tas.forEach(t => t.value = def.get(t)); refresh();
};
document.getElementById('export').onclick = () => {
  const out = JSON.parse(JSON.stringify(SB));
  dirtyList().forEach(t => {
    const seg = (out.segments || [])[Number(t.dataset.seg) - 1];
    if (seg) seg[FIELDS[t.dataset.key]] = t.value.trim();
  });
  const text = JSON.stringify(out, null, 2);
  const box = document.getElementById('outjson');
  box.value = text;
  document.getElementById('out').hidden = false;
  try {
    const blob = new Blob([text], {type:'application/json'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = FILE;
    document.body.appendChild(a); a.click(); a.remove();
    status.textContent = '已导出 ' + FILE + ' —— 覆盖原文件后接着跑 frames（下载被拦就用下面的复制）';
  } catch (e) {
    status.textContent = '下载被拦了，用页面底部那段 JSON 手动覆盖原文件';
  }
  document.getElementById('out').scrollIntoView({behavior:'smooth', block:'center'});
};
document.getElementById('copyjson').onclick = async () => {
  const box = document.getElementById('outjson');
  try { await navigator.clipboard.writeText(box.value); }
  catch (e) { box.select(); document.execCommand('copy'); }
  document.getElementById('copyjson').textContent = '已复制';
  setTimeout(() => document.getElementById('copyjson').textContent = '复制 JSON', 1200);
};
refresh();
"""


def _html_prompts(sb, sb_path, rows, with_images=True):
    title = sb.get("title") or os.path.splitext(os.path.basename(sb_path))[0]
    total = sum(r["sec"] for r in rows)
    labels = dict((k, lb) for k, _f, lb in PROMPT_SPEC)
    cards = []
    for r in rows:
        s = r["s"]
        keys = ["first", "last", "video", "segment"] + (["pose_fix"] if needs_pose_fix(s) else [])
        if with_images:
            shots = []
            for tag, p in (("首帧", r["rec"].get("first")), ("尾帧", r["rec"].get("last"))):
                b = _thumb_b64(p)
                shots.append(f'<div class="shot">{tag}<br>'
                             + (f'<img src="{b}" alt="{tag}">' if b else
                                '<div class="ph">待生成</div>') + '</div>')
            shots = '<div class="shots">' + "".join(shots) + "</div>"
        else:
            shots = ""
        info = []
        for k, lb in (("shot", "机位"), ("open_action", "开场"), ("end_action", "收尾"),
                      ("motion", "运动"), ("frame", "景别"), ("gaze", "视线")):
            if s.get(k):
                info.append(f'<div><b>{lb}</b>{_esc(s[k])}</div>')
        if r["beats"]:
            info.append(f'<div><b>节拍</b>{_esc(" + ".join(map(str, r["beats"])))}</div>')
        blocks = []
        for key in keys:
            custom = key in r["custom"]
            blocks.append(
                f'<div class="pblock" data-key="{key}">'
                f'<div class="phead"><span class="plabel">{labels[key]}</span>'
                f'<span class="badge{" custom" if custom else ""}">'
                f'{"自定义" if custom else "模板渲染"}</span>'
                f'<button class="copy">复制</button></div>'
                f'<textarea spellcheck="false" data-seg="{r["i"]}" data-key="{key}">'
                f'{_esc(r["prompts"][key])}</textarea></div>')
        cards.append(
            f'<section class="card" data-seg="{r["i"]}">'
            f'<div class="card-head"><span class="idx">段 {r["i"]}</span>'
            f'<span class="tag">{r["sec"]:.0f}s</span>'
            f'<span class="tag amp">{AMP_CN.get(r["amp"], r["amp"])}</span>'
            f'<span class="tag">动作量 {r["est"]:.1f}s</span>'
            f'<span class="adv">建议 {r["adv"]}</span></div>'
            f'<div class="card-body">{shots}<div class="info">{"".join(info)}</div></div>'
            f'<div class="prompts">{"".join(blocks)}</div></section>')
    base = os.path.splitext(os.path.basename(sb_path))[0]
    js = (HTML_JS.replace("__SB__", json.dumps(sb, ensure_ascii=False))
                .replace("__FILE__", json.dumps(base + ".json", ensure_ascii=False)))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · 分镜提示词</title>
<style>{HTML_CSS}</style></head>
<body>
<header>
  <h1>{_esc(title)} · 分镜与提示词</h1>
  <p class="meta">共 {len(rows)} 段 / {total:.0f}s · 源文件 {_esc(os.path.abspath(sb_path))}</p>
  <div class="toolbar">
    <button id="export">导出改后的 JSON</button>
    <button id="reset">撤销全部修改</button>
    <span id="status"></span>
  </div>
</header>
<main>{"".join(cards)}</main>
<section id="out" hidden>
  <h2>改后的分镜 JSON</h2>
  <p>下载被拦的话，直接复制这段，覆盖原分镜文件或贴回对话都行。</p>
  <textarea id="outjson" readonly spellcheck="false"></textarea>
  <button id="copyjson" style="margin-top:8px">复制 JSON</button>
</section>
<footer>
  下面两段文字是每段真正送进工作流的提示词，可以直接改：改完点「导出改后的 JSON」，
  用下载到的 <code>{_esc(base)}.json</code> 覆盖原分镜文件，再跑 <code>frames</code> / <code>video</code>
  就会用你改过的版本（写回的是 <code>prompt_first</code> / <code>prompt_last</code> / <code>prompt_video</code>）。
  懒得分文件的话，把改好的段落贴回对话，让 agent 写回分镜也一样。
  <br><br>
  想自己换首帧或尾帧：把图命名成 <code>s01_first.png</code> / <code>s01_last.png</code>，
  放进同目录的 <code>{_esc(base)}.frames/</code>，然后跑
  <code>pipeline.py adopt --storyboard {_esc(base)}.json</code> —— 会自动裁成 9:16、写进状态并质检。
</footer>
<script>{js}</script>
</body></html>
"""


def _esc(x):
    return (str(x).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def write_prompt_files(sb_path, sb, with_images=True):
    """把每段提示词摊开成 .prompts.md / .prompts.html，返回两个路径。"""
    rows = _seg_rows(sb, sb_path)
    base = os.path.splitext(sb_path)[0]
    md_path, html_path = base + ".prompts.md", base + ".prompts.html"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_md_prompts(sb, rows, sb_path))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(_html_prompts(sb, sb_path, rows, with_images))
    return md_path, html_path


def cmd_prompt(sb_path, sb, only=None, quiet=False):
    rows = _seg_rows(sb, sb_path)
    md_path, html_path = write_prompt_files(sb_path, sb)
    if not quiet:
        total = sum(r["sec"] for r in rows)
        print(f"分镜: {sb.get('title', '(未命名)')}  共 {len(rows)} 段 / {total:.1f}s")
        print(f"清单: {md_path}")
        print(f"预览: {html_path}")
        for r in rows:
            if only and r["i"] != only:
                continue
            s = r["s"]
            print(f"\n═══ 段 {r['i']} · {r['sec']:.0f}s · {AMP_CN.get(r['amp'], r['amp'])} · "
                  f"动作量 {r['est']:.1f}s · 建议 {r['adv']} ═══")
            if s.get("shot"):
                print(f"机位：{s['shot']}")
            if r["beats"]:
                print(f"节拍：{' + '.join(map(str, r['beats']))}")
            keys = ["first", "last", "video", "segment"] + (["pose_fix"] if needs_pose_fix(s) else [])
            for key in keys:
                label = dict((k, lb) for k, _f, lb in PROMPT_SPEC)[key]
                tag = "（自定义）" if key in r["custom"] else ""
                print(f"\n[{label}]{tag}\n{r['prompts'][key]}")
    return 0


def cmd_direct(sb_path, sb, ref_arg=None, bg_arg=None, prompt_arg=None,
               out=None, seg=1, seed_off=0):
    """两张参考图直接出一张帧图，不写分镜也能跑（2026-11-05 用户定稿）。

        image_1 = 人物参考图   管身份 / 长相 / 服装
        image_2 = 场景空镜     管场景 / 构图 / 光影，必须没有人

    V6 原版双图工作流直出，随机 seed（禁 100000），不量化、不抠图、不合成、不加节点。
    用途：试构图 / 试机位 / 试光影。正式出片仍走 `frames` 的首尾帧链路。

    参数来源有两条：命令行 --ref/--bg/--prompt，或 --storyboard 里的
    reference_images[0] / bg_ref / 第 N 段的 first 提示词。命令行优先。
    """
    import random
    sb = sb or {}

    def _stage(p):
        """绝对路径的图片先拷进 COMFY_INPUT，工作流只能读 input 下的文件名。"""
        if not p:
            return None
        ap_ = os.path.abspath(p)
        if os.path.isfile(ap_) and os.path.dirname(ap_) != os.path.normpath(COMFY_INPUT):
            dst = os.path.join(COMFY_INPUT, os.path.basename(ap_))
            shutil.copy(ap_, dst)
        return os.path.basename(ap_)

    ref_base = (_stage(ref_arg) if ref_arg else
                (os.path.basename(ref_name(sb)) if sb.get("reference_images") else None))
    bg_base = (_stage(bg_arg) if bg_arg else
               (str(sb.get("bg_ref") or "").strip() or None))
    if not ref_base:
        die("direct 需要 --ref（人物参考图），或在 --storyboard 里写 reference_images")
    if not bg_base:
        die("direct 需要 --bg（无人的场景空镜），或在 --storyboard 里写 bg_ref")
    for f in (ref_base, bg_base):
        if not os.path.exists(os.path.join(COMFY_INPUT, f)):
            die(f"参考图不存在：{os.path.join(COMFY_INPUT, f)}")

    if prompt_arg:
        prompt = prompt_arg
    elif sb.get("segments"):
        k = max(1, min(int(seg or 1), len(sb["segments"])))
        prompt = render_prompts(sb, sb["segments"][k - 1])[0]["first"]
    else:
        die("direct 需要 --prompt，或用 --storyboard 提供分镜提示词")

    lora_suf = {"lightning4": "-L4", "viggle6": "-V6"}.get(sb.get("keyframe_lora"), "-V6")
    wf = os.path.join(WF_DIR, f"Qwen21-SceneRef916{lora_suf}.json")
    seed = int(sb.get("keyframe_seed") or 0)
    if seed == 100000:
        print("[种子] keyframe_seed=100000 是已知坏种子，direct 改用随机种子", flush=True)
        seed = 0
    seed = (seed or random.randint(1, 2 ** 31)) + int(seed_off or 0)

    outs = comfy_run(wf, {
        10: {0: ref_base}, 11: {0: bg_base},
        40: {0: prompt},
        32: {0: "direct_out"}, 30: {0: seed},
    })
    src = os.path.join(COMFY_OUTPUT, outs["32"]["images"][-1]["filename"])
    dst = os.path.abspath(out or "direct_out.png")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy(src, dst)

    q = frame_quality(dst) or {"hf": 0, "edge": 0, "body": 0}
    bad, why = frame_bad(face_sharpness(dst), q)
    print(f"[direct] 图1={ref_base}  图2={bg_base}  seed={seed}")
    print(f"[direct] 输出 {dst}")
    print(f"[direct] hf={q['hf']:.1%} 强边缘={q['edge']:.0f} 人物区={q['body']:.1%} "
          f"{'⚠ ' + why if bad else '画质闸门 PASS'}", flush=True)
    return 0


def cmd_director(sb_path, sb, out=None):
    """把分镜转成 H3LongVideoDirector 节点吃的 segments 数组。

    和流水线的区别：Director 是"一段一段自己往下串"的节点，关键帧由它内部生成，
    所以每段只要一句 prompt + 秒数。改词仍然在 `.prompts.html` 里改（每段一张卡），
    导出后重跑这个命令刷新 .director.json，再粘回 ComfyUI。
    """
    rows = _seg_rows(sb, sb_path)
    segs = []
    for r in rows:
        s = r["s"]
        segs.append({
            "seconds": float(s.get("seconds") or sb.get("seconds_per_segment") or 5),
            "two_pass": bool(s.get("two_pass", False)),
            "prompt": r["prompts"]["segment"],
        })
    out = out or (os.path.splitext(sb_path)[0] + ".director.json")
    json.dump({"segments": segs}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    total = sum(x["seconds"] for x in segs)
    print(f"已生成 {out}：{len(segs)} 段 / {total:.0f}s")
    print("用法：ComfyUI 打开 H3-Long-Video-Director.json，把该文件的整段内容粘进 #4 节点的"
          " storyboard 框，Queue 一次，它自己一段段往下跑。")
    print(f"参考图 LoadImage #1/#2/#3：{sb.get('reference_images')}")
    for i, x in enumerate(segs, 1):
        print(f"  段{i:<2} {x['seconds']:.0f}s  {x['prompt'][:34]}…")
    return 0


DIRECTOR_DIR = (r"D:/Comfy-Desktop/ComfyUI-Installs/ComfyUI/ComfyUI/custom_nodes/"
                r"ComfyUI-H3-LongVideo")


def _director_jobs_root():
    """Director 的任务目录由它自己 config.json 的 jobs_dir 决定（默认包内 jobs/）。"""
    try:
        cfg = json.load(open(os.path.join(DIRECTOR_DIR, "config.json"), encoding="utf-8"))
    except Exception:
        cfg = {}
    raw = str(cfg.get("jobs_dir") or "").strip()
    if raw:
        return raw.replace("/", os.sep)
    return os.path.join(DIRECTOR_DIR, "jobs")


DIRECTOR_JOBS = _director_jobs_root()


def _latest_director_job(root):
    if not os.path.isdir(root):
        return None
    cands = [d for d in os.listdir(root)
             if os.path.isfile(os.path.join(root, d, "config.json"))]
    if not cands:
        return None
    return max(cands, key=lambda d: os.path.getmtime(os.path.join(root, d, "config.json")))


def cmd_director_push(sb_path, sb, job=None, dry=False):
    """把我们的首尾帧和提示词灌进一个 Director 任务，让它别自己用 H3 出关键帧。

    Director 的关键帧是"跑 5 秒草稿取最后一帧"（keyframe_draft_seconds=5），
    一个关键帧 ≈ 一段视频的成本，画质和身份还不如 Qwen-Image-Edit 2511 + 参考脸。
    它的关键帧阶段其实支持人工指定：state 里 `keyframe_overrides` 命中的帧会被直接采用、
    不再生成。所以这里把我们的图写成 manual 关键帧，同时把提示词和秒数同步进 config.json。
    """
    root = DIRECTOR_JOBS
    os.makedirs(root, exist_ok=True)
    job = job or _latest_director_job(root)
    if not job:
        job = _new_director_job(sb)
    jd = os.path.join(root, job)
    cfg_path = os.path.join(jd, "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    dst_segs = cfg.get("segments") or []
    src_segs = sb.get("segments") or []
    if len(dst_segs) != len(src_segs):
        print(f"任务 {job} 是 {len(dst_segs)} 段、分镜是 {len(src_segs)} 段，另建一个任务")
        job = _new_director_job(sb)
        jd = os.path.join(root, job)
        # 2026-11-07 修：cfg_path 是函数开头按旧 job 算的，另建任务后必须同步更新，
        # 否则下面 json.dump(cfg, cfg_path) 会把新任务的配置写回旧任务，
        # 把历史任务的 segments / 秒数 / 提示词全部覆盖（3段5s 冒烟时实测踩到）。
        cfg_path = os.path.join(jd, "config.json")
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        dst_segs = cfg.get("segments") or []

    st = load_state(sb_path, sb)
    kfdir = os.path.join(jd, "keyframes")
    if not dry:
        os.makedirs(kfdir, exist_ok=True)

    made, skipped = [], []
    for i, s in enumerate(src_segs, 1):
        rec = (st.get("segments") or {}).get(str(i), {})
        chained = bool(s.get("chain", True)) and i > 1
        first_src = rec.get("first")
        if chained:
            prev_end = (st.get("segments") or {}).get(str(i - 1), {}).get("last")
            if prev_end and os.path.exists(prev_end):
                # 2026-11-05 修：chain 段的首帧必须是上一段尾帧（同一张图，
                # byte-for-byte），不能再用「本段独立生成的首帧」。
                # 文档 工作流关系.md §3.2：首帧优先级 manual start > 链式继承；
                # SKILL.md「改尾帧 = 整条链会往下重出」也写明同一张图。
                # 之前这里给每段都塞一张独立 start_manual.png，把 Director 的
                # ensure_keyframe_chain() 继承顶掉了 —— 段间首尾帧因此断开。
                first_src = prev_end
                print(f"  段{i} 首帧 ← 段{i - 1} 尾帧（链式，byte-for-byte）")
            else:
                print(f"  ⚠ 段{i} 链式但段{i - 1} 尾帧缺失，退回本段首帧")
        for side, src in (("start", first_src), ("end", rec.get("last"))):
            if not src or not os.path.exists(src):
                skipped.append(f"{i}/{side}")
                continue
            dst = os.path.join(kfdir, f"seg{i:02d}_{side}_manual.png")
            if not dry:
                shutil.copy(src, dst)
            made.append((i, side, dst))

    state_path = os.path.join(jd, ".state.json")
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            state = {}
    overrides = state.setdefault("keyframe_overrides", {})
    items = {int(x["segment"]): x for x in state.get("keyframes", []) if x.get("segment")}
    # 链式/切镜按分镜写，别再写死 True：chain:false 的段本来就是独立镜头
    for i, s in enumerate(src_segs, 1):
        it = items.setdefault(i, {"segment": i})
        it["continue_from_previous"] = bool(s.get("chain", True))
        it["free_end"] = bool(s.get("free_end", False))
    for i, side, dst in made:
        posix = dst.replace("\\", "/")
        overrides.setdefault(str(i), {})[side] = posix
        it = items.setdefault(i, {"segment": i})
        it[f"{side}_frame"] = posix
        it[f"{side}_sha1"] = f"manual:{posix}"
        it[f"{side}_source"] = "manual"
    state["keyframes"] = [items[k] for k in sorted(items)]

    for i, s in enumerate(src_segs):
        sec = float(s.get("seconds") or sb.get("seconds_per_segment") or 5)
        dst_segs[i]["seconds"] = sec
        dst_segs[i]["prompt"] = render_prompts(sb, src_segs[i])[0]["segment"]
    cfg["segments"] = dst_segs

    if not dry:
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"任务 {job}：写入 {len(made)} 张关键帧、{len(dst_segs)} 段提示词"
          + ("（dry-run，没落盘）" if dry else ""))
    for i, side, dst in made:
        print(f"  段{i:<2} {side:<5} ← {os.path.basename(dst)}")
    if skipped:
        print(f"  缺图：{'、'.join(skipped)} —— 这几张 Director 仍会自己用 H3 生成")
    wf_out = write_director_workflow(sb_path, sb, job)
    if wf_out:
        print(f"\n已生成指向该任务的工作流：{wf_out}")
        print("  在 ComfyUI 里打开它，节点面板点『刷新预览』就能看到这套关键帧。")
    print("\n下一步：")
    print("  ① ComfyUI 里点这个节点的『关键帧』（或直接『继续生成视频』）——现在它会直接用上面的图，"
          "不再跑 H3 草稿")
    print(f"  ② 也可以命令行继续：curl -X POST http://127.0.0.1:8188/h3-long-video/resume/{job}")
    return 0


def _new_director_job(sb, template_job=None):
    """新建一个 Director 任务目录（不烧 GPU）：抄一份模板 config，换掉 segments 和参考图。

    Director 节点自己是"Queue 一次才建任务"，而 Queue 会立刻开跑 H3 关键帧。
    我们不需要它的关键帧，所以自己把任务目录建出来，再 push 我们的图。
    """
    root = DIRECTOR_JOBS
    tmpl = template_job or _latest_director_job(root)
    if tmpl:
        cfg = json.load(open(os.path.join(root, tmpl, "config.json"), encoding="utf-8"))
    else:
        # 任务目录是空的（刚清过盘）就拿仓库根目录的 config.json 当模板，
        # 不然"先 push 再让 Director 跑"这条顺序走不通。
        fallback = os.path.join(DIRECTOR_DIR, "config.json")
        if not os.path.isfile(fallback):
            die(f"{root} 里没有任务模板，{fallback} 也不在")
        cfg = json.load(open(fallback, encoding="utf-8"))
    def explicit_seg_prompt(field):
        """只取用户手写的那句（分段优先，其次全局）。没写就返回空。"""
        raw = s.get(field) or sb.get(field)
        return raw.strip() if isinstance(raw, str) else ""

    segs = []
    for s in sb.get("segments") or []:
        pr = render_prompts(sb, s)[0]
        seg = {
            "seconds": float(s.get("seconds") or sb.get("seconds_per_segment") or 5),
            "two_pass": bool(s.get("two_pass", False)),
            "prompt": pr["segment"],
            # 首/尾帧提示词只在用户手写过 prompt_first / prompt_last 时才写进任务，
            # 这样面板里那个「尾帧提示词」框显示的就是他自己那句话，可以直接改；
            # 没写过就留空 = 跟主提示词一样（runner 会回落）。
            "start_frame_prompt": explicit_seg_prompt("prompt_first"),
            "end_frame_prompt": explicit_seg_prompt("prompt_last"),
        }
        segs.append(seg)
    cfg["segments"] = segs
    jid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    jd = os.path.join(root, jid)
    os.makedirs(jd)
    json.dump(cfg, open(os.path.join(jd, "config.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    if sb.get("reference_images") or (sb.get("keyframe_backend") or "").strip() == "krea2_t2i":
        # 纯文本 t2i 也要写 reference_images，否则新任务继承模板里的旧真人图。
        # 两张参考图（2026-11-05 用户定稿，Director 版）：
        #   reference_1 = 人物参考图（身份锚，<Picture1>）
        #   reference_2 = 无人的场景空镜 bg_ref（场景 / 构图 / 光影）
        # Director 节点本来就有 reference_1..9 可选输入、config.json 默认也是两张，
        # 所以只要把这两张写对即可，不用改 director.py、不用重启 ComfyUI。
        # 注意：第二张是**无人空镜**，不是第二张人物照——后者会被 Qwen 画成两个人
        # （repo run_h3_long_video.py build_qwen_keyframe_graph 的源码注释）。
        try:
            refs = []
            d1 = os.path.join(jd, "reference_1.png")
            shutil.copy(ref_name(sb), d1)
            refs.append(d1.replace("\\", "/"))
            bg = str(sb.get("bg_ref") or "").strip()
            if bg:
                p2 = os.path.join(COMFY_INPUT, os.path.basename(bg))
                if not os.path.exists(p2):
                    p2 = os.path.abspath(bg)
                if os.path.exists(p2):
                    d2 = os.path.join(jd, "reference_2.png")
                    shutil.copy(p2, d2)
                    refs.append(d2.replace("\\", "/"))
                    cfg["bg_ref"] = d2.replace("\\", "/")
                else:
                    print(f"  ⚠ bg_ref 不存在（{bg}），Director 只写一张人物参考图")
            # 只复制文件、不改 config 会让新任务继承模板里的别人的脸（2026-11-05 老 bug）
            cfg["reference_images"] = refs
            print(f"  参考图 {len(refs)} 张：" + "、".join(os.path.basename(x) for x in refs))
        except Exception as e:
            print(f"  参考图没复制成功（{e}），Director 会用模板里的")
            cfg["reference_images"] = [ref_name(sb).replace("\\", "/")]
    # cfg 的 reference_images / bg_ref 是上面才写进内存的，必须再落一次盘。
    # 否则新任务会继承模板 config 里的旧真人图（2026-11-05 身份锚 bug 的残留），
    # 且 bg_ref 永远不生效。
    json.dump(cfg, open(os.path.join(jd, "config.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump({"status": "awaiting_review"},
              open(os.path.join(jd, "status.json"), "w", encoding="utf-8"))
    print(f"新建任务 {jid}（模板 {tmpl}，{len(segs)} 段）")
    return jid


def cmd_resume(job):
    """让 Director 接着把视频跑完（关键帧用我们灌进去的那套）。"""
    if not job:
        job = _latest_director_job(DIRECTOR_JOBS)
    if not job:
        die("没有可继续的任务")
    req = urllib.request.Request(
        f"{API}/h3-long-video/resume/{job}",
        data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        die(f"继续失败 HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")
    print(f"任务 {job}: {json.dumps(d, ensure_ascii=False)}")
    if d.get("ok"):
        print(f"已在跑。看进度：{API}/h3-long-video/status/{job}")
    return 0


def write_director_workflow(sb_path, sb, job):
    """生成一个"已经指向该任务"的 Director 工作流，打开就能点『刷新预览』看关键帧。

    面板的任务 id 来自 node.properties.h3_job_id，而它只在节点自己跑过一次时才被写入；
    我们脚本建的任务不会被记住，所以直接把这个属性烤进工作流文件里。
    """
    src = os.path.join(WF_DIR, "H3-Long-Video-Director.json")
    wf = json.load(open(src, encoding="utf-8"))
    node = next((n for n in wf["nodes"] if n["type"] == "H3LongVideoDirector"), None)
    if node is None:
        return None
    node.setdefault("properties", {})["h3_job_id"] = job
    wv = node.get("widgets_values") or []
    segs = [{"seconds": float(s.get("seconds") or sb.get("seconds_per_segment") or 5),
             "two_pass": bool(s.get("two_pass", False)),
             "prompt": render_prompts(sb, s)[0]["segment"]}
            for s in (sb.get("segments") or [])]
    if len(wv) > 16:
        wv[0] = json.dumps({"segments": segs}, ensure_ascii=False, indent=1)
        wv[16] = job          # resume_job_id，「继续生成视频」直接可用
        node["widgets_values"] = wv
    # 参考图/首帧也一起烤进去：模板里的默认值已经改成 placeholder.png，
    # 不写的话打开工作流看到的是占位图（甚至点到 Queue 会拿占位图当身份锚）。
    try:
        ref_base = os.path.basename(ref_name(sb))
        # 两张参考图：node 1 = 人物（reference_1），node 2 = 场景空镜（reference_2）。
        # 老代码把 1 和 2 都写成同一张人物图，等于第二张参考图是摆设。
        _bg = str(sb.get("bg_ref") or "").strip()
        bg_base = os.path.basename(_bg) if _bg else None
        need = os.path.join(COMFY_INPUT, ref_base)
        if not os.path.exists(need):
            shutil.copy(ref_name(sb), need)
        if bg_base and not os.path.exists(os.path.join(COMFY_INPUT, bg_base)) and os.path.isabs(_bg):
            shutil.copy(_bg, os.path.join(COMFY_INPUT, bg_base))
        st = load_state(sb_path, sb)
        first = (st.get("segments", {}).get("1", {}) or {}).get("first")
        first_base = os.path.basename(first) if first and os.path.exists(first) else None
        for n in wf["nodes"]:
            if n.get("type") not in ("LoadImage", "VHS_LoadImagePath"):
                continue
            w = n.get("widgets_values") or []
            if not w:
                continue
            if n.get("id") == 1:
                w[0] = ref_base
            elif n.get("id") == 2:
                # 有 bg_ref 就放场景空镜；老分镜没写就仍放人物图（兼容旧行为）
                w[0] = bg_base or ref_base
            elif n.get("id") == 5 and first_base:
                w[0] = first_base
    except Exception as e:
        print(f"  参考图没写进工作流（{e}）")
    out = os.path.splitext(sb_path)[0] + ".director.workflow.json"
    json.dump(wf, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out

def find_job_video(job_id):
    d = os.path.join(JOBS, job_id)
    if not os.path.isdir(d):
        alt = os.path.join(SHARED_ROOT, "jobs", job_id)
        if os.path.isdir(alt):
            d = alt
    log = os.path.join(d, "run.log")
    if not os.path.exists(log):
        return None
    for line in reversed(open(log, encoding="utf-8", errors="ignore").read().splitlines()):
        if ".mp4" in line:
            for tok in line.replace('"', " ").replace("'", " ").split():
                if tok.lower().endswith(".mp4") and os.path.exists(tok):
                    return tok
    return None


def wait_job(job_id, timeout=2400):
    d = os.path.join(JOBS, job_id)
    if not os.path.isdir(d):
        # 扩展实际把任务写在 h3-long-video/jobs/ 下，jobs-single 只是历史约定
        alt = os.path.join(SHARED_ROOT, "jobs", job_id)
        if os.path.isdir(alt):
            d = alt
    t0 = time.time()
    while time.time() - t0 < timeout:
        sp = os.path.join(d, "status.json")
        st = None
        try:
            st = json.load(open(sp, encoding="utf-8")).get("status")
        except Exception:
            pass
        if st == "completed":
            return find_job_video(job_id)
        if st in ("failed", "error", "cancelled"):
            return None
        time.sleep(10)
    return None


def video_backend(sb, override=None):
    """S4 出视频跑在哪：local（本地 ComfyUI）还是 runninghub（云端）。

    优先级：命令行 --backend > 环境变量 H3_VIDEO_BACKEND > 分镜里的 video_backend > local。
    写错值不报错而是回落 local 很危险（以为花了云端的钱其实跑的本地），所以只认白名单。
    """
    raw = override or os.environ.get("H3_VIDEO_BACKEND") or sb.get("video_backend") or "local"
    b = str(raw).strip().lower()
    if b in ("local", "comfy", "comfyui"):
        return "local"
    if b in ("runninghub", "runninghub.ai", "rh", "cloud"):
        return "runninghub"
    die(f"未知的 video_backend: {raw!r}（只认 local / runninghub）")


def _job_from_outs(outs):
    for o in outs.values():
        for t in (o.get("text") or []):
            if isinstance(t, str) and "jobs" in t.replace("\\", "/"):
                return t.replace("\\", "/").split("jobs/")[1].split("/")[0]
    return None


def gen_local(sb, s, rec, seed, w, h):
    """本地那条老路：提交工作流 → 节点吐 jobs/<id> → 轮询本地目录取 mp4。

    二采默认开（用户 2026-11-05 定）：一采 4 步 + 二采 2 步。
    段级 `steps` / `two_pass_steps` / `two_pass` 可覆盖，分镜顶层同名字段兜底。
    """
    steps = int(s.get("steps", sb.get("steps", 4)))
    tp_steps = int(s.get("two_pass_steps", sb.get("two_pass_steps", 2)))
    two_pass = bool(s.get("two_pass", sb.get("two_pass", True)))
    # 110 和 111 必须一起覆写（2026-11-05 bug）：
    # 工作流里 110→reference_1、111→reference_2，而提示词把 <Picture3> 当身份锚，
    # Picture3 就是 reference_1。以前只改 111，110 一直留着工作流默认的 image.png
    # （旧任务的校服女生），于是模型的身份锚是别人、我们的参考图退到第二顺位——
    # 这就是本地 identity 怎么调都上不去的真正原因。
    ref_base = os.path.basename(ref_name(sb))
    outs = comfy_run(
        os.path.join(WF_DIR, "H3-Single-Clip-FirstLastRefs.json"),
        {
            101: {0: os.path.basename(rec["first"])},
            102: {0: os.path.basename(rec["last"])},
            110: {0: ref_base},
            111: {0: ref_base},
            200: {0: render_prompts(sb, s)[0]["video"], 1: float(s.get("seconds", 5)),
                  2: steps, 3: tp_steps,
                  6: float(sb.get("megapixels", 0.3)), 7: w, 8: h,
                  11: seed, 12: two_pass},
        },
    )
    job = _job_from_outs(outs)
    if not job:
        print("    未拿到 job id")
        return None, None
    return wait_job(job), job


def gen_cloud(sb, s, rec, seed, sb_path):
    """云端那条路：上传首尾帧 → workflowId 提交 → 轮询 → 下载回本地。

    产物落在 Shared/output/h3-long-video/cloud/<分镜名>/，跟本地段视频一样
    直接喂给 merge，下游不用关心这段是谁生成的。
    """
    import rh_backend as RH
    dest = os.path.join(SHARED_ROOT, "cloud",
                        os.path.splitext(os.path.basename(sb_path))[0])
    return RH.gen(sb, s, rec, seed, dest)


def warm_bottom_ratio(path):
    """底部 12% 里"暖棕高饱和"像素占比——扩图补出桌面/台面的探测器。

    2026-11-05 出事那天：旧扩图提示词写死"原木色桌面"，于是每个项目扩 9:16 时
    底部都被画上一块木台面。实测干净帧 0.15~0.19，带桌面的帧 0.48~0.94，区分很干净，
    阈值取 0.32。只报警不拦截：有些场景（木地板店铺）本来就偏暖。
    """
    try:
        import cv2
        img = cv2.imread(path)
        if img is None:
            return None
        h, w = img.shape[:2]
        bot = img[int(h * 0.88):h, :]
        hsv = cv2.cvtColor(bot, cv2.COLOR_BGR2HSV)
        m = ((hsv[:, :, 0] >= 8) & (hsv[:, :, 0] <= 32) &
             (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 45) & (hsv[:, :, 2] < 205))
        return float(m.mean())
    except Exception:
        return None


def _img_ratio_ok(path, tol=0.010):
    try:
        from PIL import Image as _I
        with _I.open(path) as im:
            w, h = im.size
        return abs(w / h - 9 / 16) <= tol
    except Exception:
        return True


def _ensure_ratio_916(rec, sb):
    """把非 9:16 的关键帧扩成 9:16（返回是否可用）。

    为什么必须有这一步：H3 的 first_frame 不做等比适配，直接按目标画幅采样，
    3:4 的帧塞进 832×1472 会把人物纵向拉长（用户 2026-11-05 一眼看出来）。
    扩图用的是 neutal 的"延续原场景"，不再补桌面。
    """
    ok = True
    scene = (sb.get("outpaint_scene") or "").strip() or None
    for key in ("first", "last"):
        p = rec.get(key)
        if not p or not os.path.exists(p):
            ok = False
            continue
        if _img_ratio_ok(p):
            continue
        print(f"    {key} 帧不是 9:16，自动扩图...", flush=True)
        try:
            outpaint_916(p, seed=int(sb.get("keyframe_seed") or 20261105), scene=scene)
        except Exception as e:
            print(f"    扩图失败：{e}")
        if not _img_ratio_ok(p):
            ok = False
    return ok


def cmd_video(sb_path, sb, only=None, max_try=3, backend=None):
    st = load_state(sb_path, sb)
    ref = ref_name(sb)
    ref_emb = Q.image_metrics(ref).get("_emb")
    w, h = sb.get("final_size", [720, 1280])
    be = video_backend(sb, backend)
    if be == "runninghub":
        # 云端每跑一次都真扣钱 + 排队，默认只跑一次；要重试由 runninghub.retries 显式放开
        cloud_try = int((sb.get("runninghub") or {}).get("retries", 1) or 1)
        max_try = max(1, min(max_try, cloud_try))
        print(f"出视频走 RunningHub 云端（每段最多 {max_try} 次）")
    for i, s in enumerate(sb["segments"], 1):
        if only and i != only:
            continue
        rec = st["segments"].get(str(i), {})
        if not (rec.get("first") and os.path.exists(rec["first"])):
            print(f"[段{i}] 首帧缺失，先跑 frames")
            continue
        if not (rec.get("last") and os.path.exists(rec["last"])):
            print(f"[段{i}] 尾帧缺失，先跑 frames")
            continue
        # 关键帧比例守卫（2026-11-05 用户发现「首帧人被拉长」）：
        # 非 9:16 的帧喂给 H3 会被直接拉伸（实测 896×1184 的 3:4 帧里人被拉长约 35%）。
        # 先尝试自动扩图，扩完仍不合格就停下来说清楚，绝不允许带着变形去出片。
        if not _ensure_ratio_916(rec, sb):
            print(f"[段{i}] 关键帧不是 9:16（扩图后仍不合格），先修帧再出片")
            continue
        if rec.get("video") and os.path.exists(rec["video"]) and rec.get("qa", {}).get("verdict") != "FAIL":
            print(f"[段{i}] 已有合格视频，跳过")
            continue

        for attempt in range(1, max_try + 1):
            seed = 500000 + i * 100 + attempt
            print(f"[段{i}] 出视频 第 {attempt}/{max_try} 次 (seed {seed})...", flush=True)
            if be == "runninghub":
                mp4, job = gen_cloud(sb, s, rec, seed, sb_path)
            else:
                mp4, job = gen_local(sb, s, rec, seed, w, h)
            if not mp4:
                print(f"[段{i}] 视频失败（{be}）")
                continue
            m = Q.video_metrics(mp4, ref_emb, first_img=rec.get("first"), last_img=rec.get("last"))
            issues = Q_and_judge(m)
            # 跨场景豁免：背景整体改变时，人脸框摆动会被计成"往返行程"、
            # 背景切换本身会被计成闪烁（实测段 1 假阳性 backtrack 1.78/3.71）。
            # 这类镜头改查身份、尺度漂移与运动量，往返/闪烁降级为提示。
            # 表演豁免：提示词明写「张嘴喘气/喊叫」的段落，张嘴是设计动作不是缺陷。
            soft = set()
            tagname = ""
            if s.get("scene_change"):
                soft, tagname = {"backtrack", "flicker"}, "跨场景镜头豁免"
            elif s.get("mouth"):
                soft, tagname = {"mouth_open"}, "表演豁免（提示词要求张嘴）"
            if soft:
                kept = [x for x in issues if x["code"] not in soft]
                dropped = [x for x in issues if x["code"] in soft]
                for x in dropped:
                    x["level"] = "WARN" if x["level"] == "FAIL" else x["level"]
                    kept.append(x)
                if dropped:
                    print(f"[段{i}] {tagname}（降级不拦截）："
                          + "、".join(x["code"] for x in dropped), flush=True)
                issues = kept
            rec.update({"video": mp4, "job": job, "qa": {"verdict": Q_verdict(issues),
                                                         "issues": [x["code"] + ": " + x["msg"] for x in issues],
                                                         "metrics": {k: m.get(k) for k in
                                                                     ("headroom_min", "id_sim_min",
                                                                      "anchor_first_sim", "anchor_last_sim",
                                                                      "motion_early", "motion_total")}}})
            save_state(sb_path, st)
            print(f"[段{i}] 质检: {rec['qa']['verdict']}", flush=True)
            a1, a2 = m.get("anchor_first_sim"), m.get("anchor_last_sim")
            if a1 is not None or a2 is not None:
                print(f"        首帧到位 {a1 if a1 is not None else '-'}"
                      f" / 尾帧到位 {a2 if a2 is not None else '-'}（SSIM，越高越贴）")
            for x in issues:
                print(f"        [{x['level']}] {x['code']} {x['msg']}")
            if rec["qa"]["verdict"] != "FAIL":
                break
        save_state(sb_path, st)

    print("\n分段视频完成。确认后跑 merge。")


def Q_and_judge(m):
    sys.path.insert(0, os.path.join(QA_DIR))
    import qa as QA
    return QA.judge_video(m, Q.TH)


def Q_verdict(issues):
    import qa as QA
    return QA.verdict(issues)


# ---------------------------------------------------------------- merge
def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def has_audio(path):
    """用 ffmpeg -i 判断成片是否带音轨。"""
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path],
                       capture_output=True, text=True, encoding="utf-8", errors="ignore")
    return "Audio:" in (r.stderr or "")


def probe_duration(path):
    """用 ffmpeg -i 的 Duration 行取时长（不依赖 ffprobe）。"""
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path],
                       capture_output=True, text=True, encoding="utf-8", errors="ignore")
    import re
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr or "")
    if not m:
        return None
    h, mi, se = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(se)


def _xfade_name(t):
    """转场名映射：中文/别名 → ffmpeg xfade 的 transition 名。"""
    t = (t or "cut").lower()
    table = {"dissolve": "fade", "fade": "fade", "叠化": "fade",
             "black": "fadeblack", "黑场": "fadeblack",
             "white": "fadewhite", "白闪": "fadewhite",
             "wipeleft": "wipeleft", "wiperight": "wiperight",
             "slideleft": "slideleft", "slideright": "slideright"}
    return table.get(t, "fade")


def cmd_merge(sb_path, sb, transition=None, out=None, join_dissolve=None):
    """合并分段视频。

    ``join_dissolve`` / 分镜里的 ``join_dissolve_seconds``：给连续动作的接缝
    自动加一段**短叠化**（2026-11-04 coffee 实测 0.25s 最优）。2.1 后端
    chain 不复制首帧，相邻段首尾帧天然差一点点，硬切会读成「顿一下」；
    短叠化能抹掉这一点点差，又不会像 0.4s dissolve 那样出双影。
    """
    st = load_state(sb_path, sb)
    w0, h0 = sb.get("final_size", [720, 1280])
    files = []
    for i, _ in enumerate(sb["segments"], 1):
        v = st["segments"].get(str(i), {}).get("video")
        if not v or not os.path.exists(v):
            die(f"第 {i} 段没有视频，先跑 video")
        files.append(v)

    # 统一输出到 Shared/output/h3-long-video/，不再撒在分镜旁边
    if not out:
        out = os.path.join(SHARED_ROOT, f"{sb.get('title', 'video')}.mp4")
    out_dir = os.path.dirname(os.path.abspath(out))
    os.makedirs(out_dir, exist_ok=True)
    ff = ffmpeg_exe()

    # 「微调叠化」：CLI --join-dissolve 优先，其次分镜 join_dissolve_seconds。
    # 显式 transition / scene_change 的接缝不自动改，保留人工判断。
    jd_raw = join_dissolve if join_dissolve is not None else sb.get("join_dissolve_seconds", 0)
    try:
        jd = float(jd_raw or 0)
    except (TypeError, ValueError):
        jd = 0.0
    trans = [None] + [(s.get("transition") or "cut") for s in sb["segments"][1:]]
    if transition:
        trans = [None] + [transition] * (len(files) - 1)
    elif jd > 0:
        auto = []
        for k, seg in enumerate(sb["segments"][1:], 1):
            if seg.get("transition") or seg.get("scene_change"):
                continue  # 人工指定 / 跨场景，不碰
            trans[k] = "dissolve"
            auto.append(f"{k}→{k+1}")
        if auto:
            print(f"接缝微调叠化 {jd:.2f}s：{', '.join(auto)}（动作帧零裁剪）", flush=True)
    xf = jd if jd > 0 else float(sb.get("xfade_seconds", 0.4))

    def is_cut(t):
        return (t or "cut") in ("cut", "none", "硬切")

    def cut_len(t):
        return 0.0 if is_cut(t) else xf

    if all(is_cut(t) for t in trans[1:]):
        lst = os.path.join(out_dir, "_concat.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for q in files:
                f.write("file '" + os.path.abspath(q).replace("'", "'\\''").replace("\\", "/") + "'\n")
        r = subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out],
                           capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if r.returncode != 0:
            die(f"ffmpeg 合并失败:\n{(r.stderr or '')[-800:]}")
        os.remove(lst)
        print("合并（全硬切，流拷贝）")
    else:
        # 踩过的三个坑（2026-10-31）：
        # 1) H3 输出没有 fps 元数据（rate 1/0），xfade 直接拒绝 → 先逐段归一化并写 -r 24；
        # 2) xfade 与 acrossfade 放同一 filtergraph 会互相拖死（成片被截成一段） → 音视频分开；
        # 3) concat + xfade 混在一个 graph 里时基不一致（1/1000000 vs 1/12288）且硬切用 0.04s
        #    的 xfade 短于一帧会让滤镜提前收尾 → 改成逐段串行：硬切 concat、叠化单独 xfade。
        tmpdir = tempfile.mkdtemp(prefix="h3xf_")
        norm = []
        for k, f in enumerate(files):
            o = os.path.join(tmpdir, f"n{k}.mp4")
            r = subprocess.run([ff, "-y", "-i", f,
                                "-vf", "scale=%d:%d,setsar=1,format=yuv420p" % (w0, h0), "-r", "24",
                                "-c:v", "libx264", "-crf", "18",
                                "-c:a", "aac", "-ar", "48000", "-ac", "2", o],
                               capture_output=True, text=True, encoding="utf-8", errors="ignore")
            if r.returncode != 0:
                die(f"归一化第 {k + 1} 段失败:\n{(r.stderr or '')[-600:]}")
            norm.append(o)

        cur = norm[0]
        cur_dur = probe_duration(cur) or 0.0
        for k in range(1, len(norm)):
            t = trans[k] or "cut"
            nxt = os.path.join(tmpdir, f"step{k}.mp4")
            if is_cut(t):
                lst = os.path.join(tmpdir, f"l{k}.txt")
                with open(lst, "w", encoding="utf-8") as f:
                    for q in (cur, norm[k]):
                        f.write("file '" + os.path.abspath(q).replace("'", "'\\''").replace("\\", "/") + "'\n")
                r = subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", nxt],
                                   capture_output=True, text=True, encoding="utf-8", errors="ignore")
                if r.returncode != 0:
                    die(f"第 {k + 1} 段硬切拼接失败:\n{(r.stderr or '')[-600:]}")
            else:
                off = max(0.0, cur_dur - xf)
                # acrossfade 要求两段都有音轨；H3 偶尔出不带音轨的片，
                # 这时只叠画面、音轨直接丢掉，否则整个 filtergraph 会被 ffmpeg 拒绝。
                both_audio = has_audio(cur) and has_audio(norm[k])
                fc = f"[0:v][1:v]xfade=transition={_xfade_name(t)}:duration={xf:.3f}:offset={off:.3f}[v]"
                maps = ["-map", "[v]"]
                if both_audio:
                    fc += (f";[0:a][1:a]acrossfade=d={xf:.3f}:c1=tri:c2=tri[a]")
                    maps += ["-map", "[a]"]
                r = subprocess.run([ff, "-y", "-i", cur, "-i", norm[k],
                                    "-filter_complex", fc] + maps +
                                   ["-r", "24", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                                    "-c:a", "aac", "-ar", "48000", nxt],
                                   capture_output=True, text=True, encoding="utf-8", errors="ignore")
                if r.returncode != 0:
                    die(f"第 {k + 1} 段转场失败（{t}）:\n{(r.stderr or '')[-700:]}")
            cur = nxt
            cur_dur = probe_duration(cur) or cur_dur
        shutil.copy(cur, out)
        span = "，".join(("%s(硬切)" % t) if is_cut(t) else ("%s(%.2fs)" % (t, xf)) for t in trans[1:])
        print(f"转场：{span}")
        shutil.rmtree(tmpdir, ignore_errors=True)

    size = os.path.getsize(out) / 1e6
    print(f"输出: {out}  ({size:.1f} MB)")

    ref_emb = Q.image_metrics(ref_name(sb)).get("_emb")
    m = Q.video_metrics(out, ref_emb)
    total_sec = sum(float(s.get("seconds", sb.get("seconds_per_segment", 5))) for s in sb["segments"])
    total_sec -= sum(cut_len(t) for t in trans[1:])
    dur = float(m.get("duration") or 0)

    issues = []
    if abs(dur - total_sec) > 0.8:
        issues.append({"level": "FAIL", "code": "duration",
                       "msg": f"时长 {dur:.2f}s 与分镜 {total_sec:.1f}s 不符"})
    if m.get("size") != f"{w0}x{h0}":
        issues.append({"level": "FAIL", "code": "resolution",
                       "msg": f"分辨率 {m.get('size')}，不是 {w0}x{h0}"})
    if not has_audio(out):
        issues.append({"level": "FAIL", "code": "no_audio", "msg": "成片没有音轨"})
    seg_fail = [k for k, v in st["segments"].items() if (v.get("qa") or {}).get("verdict") == "FAIL"]
    if seg_fail:
        issues.append({"level": "FAIL", "code": "segment_fail",
                       "msg": f"以下段落质检为 FAIL：{seg_fail}，整片含废段"})
    seg_warn = [k for k, v in st["segments"].items() if (v.get("qa") or {}).get("verdict") == "WARN"]
    if seg_warn:
        issues.append({"level": "WARN", "code": "segment_warn",
                       "msg": f"以下段落为 WARN（可用）：{seg_warn}"})

    print(f"整片抽检: {Q_verdict(issues)}  ({dur:.2f}s, {m.get('sampled')} 帧抽样)")
    for x in issues:
        print(f"  [{x['level']}] {x['code']} {x['msg']}")
    return 0


# ---------------------------------------------------------------- 纯文本 t2i 关键帧
# 2026-11-07 用户定：没给参考图（纯文本）的片子，关键帧用
# `image_krea2_turbo_t2i_int8` 那套出图更好，且后面各段首尾帧必须连续。
# 该工作流是纯 t2i（EmptyLatentImage，无任何图像输入），所以：
#   - 身份/场景一致性靠「所有帧共用同一段 t2i_base_prompt + 同一 seed」；
#     实测同 seed 换动作描述，双人长相/服装/场地都稳（k2_probe_1/2.png）。
#   - 段间连续靠 byte-for-byte 复制：段 N 首帧 = 段 N-1 尾帧，不重新生成。
#   - 原生 9:16 出图，不需要 16:9→9:16 的模糊铺底适配。
KREA2_UNET = "krea2_aingmix_kroma_v10_int8_convrot.safetensors"
KREA2_CLIP = "qwen3vl_4b_fp8_scaled.safetensors"
KREA2_VAE = "qwen_image_vae.safetensors"


def krea2_t2i_graph(prompt, seed, w, h, prefix, steps=8, unet=None):
    """等价复刻 image_krea2_turbo_t2i_int8.json 的真实档位（用户实际设置）：
    unet = Aingmix Kroma v1.0 int8 / clip = qwen3vl_4b_fp8_scaled (type krea2)
    vae = qwen_image_vae / 8 步 / CFG 1.0 / euler+simple
    prompt_enhance=false、enable_lora=false（子图里就是关的，别打开，否则测的是扩写器）。
    """
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": unet or KREA2_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": KREA2_CLIP, "type": "krea2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": KREA2_VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": int(w), "height": int(h), "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "seed": int(seed), "steps": int(steps), "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple",
                         "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["6", 0], "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }


def gen_krea2_t2i(prompt, seed, w, h, prefix, out_path, steps=8, unet=None, timeout=600):
    """提交一次 Krea2 t2i，等完成并下载到 out_path。带进度心跳（见 SKILL.md 长任务节）。"""
    body = json.dumps({"prompt": krea2_t2i_graph(prompt, seed, w, h, prefix, steps, unet),
                       "client_id": "h3krea2"}).encode()
    req = urllib.request.Request(API + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        die("Krea2 t2i 提交失败: " + e.read().decode("utf-8", "ignore")[:900])
    pid = res.get("prompt_id")
    if not pid:
        die("Krea2 t2i 提交失败: " + json.dumps(res, ensure_ascii=False)[:400])
    print(f"  prompt_id {pid}", flush=True)
    t0 = time.time()
    last_tick = -1
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(API + "/history/" + pid, timeout=15) as r:
                hst = json.load(r)
            if pid in hst:
                stt = hst[pid].get("status", {})
                if stt.get("status_str") == "error":
                    for m in stt.get("messages", []):
                        if m[0] == "execution_error":
                            die("Krea2 t2i 执行失败: " + str(m[1].get("exception_message"))[:900])
                    die("Krea2 t2i 执行失败")
                items = (hst[pid].get("outputs", {}).get("9") or {}).get("images") or []
                if items:
                    im = items[0]
                    url = (API + "/view?filename=" + urllib.parse.quote(im["filename"]) +
                           "&subfolder=" + urllib.parse.quote(im.get("subfolder", "")) +
                           "&type=" + im.get("type", "output"))
                    with urllib.request.urlopen(url, timeout=120) as r:
                        open(out_path, "wb").write(r.read())
                    print(f"  -> {os.path.basename(out_path)}  {time.time()-t0:.0f}s", flush=True)
                    return out_path
        except Exception:
            pass
        el = int(time.time() - t0)
        if el // 15 > last_tick:
            last_tick = el // 15
            print(f"    ...{prefix} 渲染中 {el}s", flush=True)
        time.sleep(3)
    die(f"Krea2 t2i 超时（{timeout}s）：{prefix}")


def cmd_refs(sb_path, sb, retry=False):
    """文生视频（用户没给参考图）第一步：用 Krea2 t2i 造两张参考图
    —— 人物设定图 + 无人场景空镜，回写分镜，之后**完全回到双图 frames 链路**。

    2026-11-07 用户纠正：「不是要先生成人物参考图吗」。
    纯文本 ≠ 让 t2i 直接把全部关键帧出了；t2i 只负责造参考图。
    造完之后身份锚、链式首尾帧、9:16 硬锁、push/resume 全是图生视频的既定规则。
    """
    import hashlib
    person_prompt = (sb.get("ref_person_prompt") or "").strip()
    bg_prompt = (sb.get("ref_bg_prompt") or "").strip()
    if not person_prompt or not bg_prompt:
        die("文生视频分镜必须写 ref_person_prompt（人物参考图）与 ref_bg_prompt（无人场景空镜）")

    pfx = sb.get("prefix") or PFX
    w, h = sb.get("final_size", [832, 1472])
    _h = hashlib.md5(os.path.abspath(sb_path).encode("utf-8")).hexdigest()
    base_seed = int(sb.get("ref_seed") or 0) or (int(_h[:8], 16) % 2_000_000_000 + 1)
    p_seed = int(sb.get("ref_person_seed") or 0) or base_seed
    b_seed = int(sb.get("ref_bg_seed") or 0) or base_seed + 1
    for label, sd in (("人物参考图", p_seed), ("无人场景空镜", b_seed)):
        if sd == 100000:
            die(f"refs 的 {label} seed=100000 是坏种子，换一个")

    # 尊重分镜里已写好的文件名，便于复跑、也便于人工替换成自己挑的图
    person = (sb.get("ref_person_name") or (sb.get("reference_images") or [None])[0]
              or f"{pfx}_ref_person.png")
    bg = sb.get("ref_bg_name") or sb.get("bg_ref") or f"{pfx}_ref_bg.png"
    person_path = os.path.join(COMFY_INPUT, os.path.basename(person))
    bg_path = os.path.join(COMFY_INPUT, os.path.basename(bg))
    steps = int(sb.get("t2i_steps") or 8)
    unet = sb.get("t2i_unet") or None

    print(f"[refs] 文生视频第一步：Krea2 造两张参考图 / {w}x{h} / {steps} 步 CFG1", flush=True)
    for label, prompt, seed, path, tag in (
            ("人物参考图 (image_1)", person_prompt, p_seed, person_path, "PERSON"),
            ("无人场景空镜 (image_2 / bg_ref)", bg_prompt, b_seed, bg_path, "BG")):
        if os.path.exists(path) and not retry:
            print(f"  {label} 已存在，跳过：{os.path.basename(path)}", flush=True)
            continue
        print(f"  生成 {label} / seed={seed}", flush=True)
        gen_krea2_t2i(prompt, seed, w, h, f"{pfx}_REF{tag}", path, steps, unet)

    # 回写分镜：造完图之后就等价于「用户给了两张参考图」的图生视频分镜
    sb["reference_images"] = [os.path.basename(person)]
    sb["bg_ref"] = os.path.basename(bg)
    sb["ref_pad_916"] = False        # 原生 9:16，别再 pad
    sb.pop("keyframe_backend", None)  # 别留 krea2_t2i，否则 check 分流到已废弃链路
    json.dump(sb, open(sb_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[refs] 分镜已回写：reference_images={sb['reference_images']}  bg_ref={sb['bg_ref']}")
    print("下一步：check → frames → push → resume（段 2+ 写 chain:true 或留空）", flush=True)
    return 0


def cmd_t2i(sb_path, sb, only=None, retry=False, seed_off=0):
    """【已废弃】纯文本分镜用 Krea2 t2i 直接出全部首尾帧 + 链式复制。

    2026-11-07 晚用户纠正：没给参考图时应当先用 `refs` 造人物图 + 空镜，
    再走双图 `frames` 链路；t2i 直出关键帧是理解反了，仅保留作实验对照。
    """
    print("[已废弃] t2i 直出全部关键帧不是文生视频的正路；"
          "请改用 `refs` 先造两张参考图，再跑 `frames` 双图链路", flush=True)
    import hashlib
    base = (sb.get("t2i_base_prompt") or "").strip()
    if not base:
        die("纯文本分镜必须写 t2i_base_prompt（所有帧共用的人物/场景/机位锁）")
    st = load_state(sb_path, sb)
    segs = sb["segments"]
    pfx = sb.get("prefix") or PFX
    w, h = sb.get("final_size", [832, 1472])
    steps = int(sb.get("t2i_steps") or 8)
    unet = sb.get("t2i_unet") or None
    _h = hashlib.md5(os.path.abspath(sb_path).encode("utf-8")).hexdigest()
    default_seed = int(_h[:8], 16) % 2_000_000_000 + 1
    seed = int(sb.get("keyframe_seed") or 0) or default_seed
    if seed == 100000:
        print("[种子] 100000 是坏种子，改回分镜派生值", flush=True)
        seed = default_seed
    seed += seed_off
    print(f"[纯文本 t2i] Krea2 / {w}x{h} / {steps} 步 CFG1 / seed={seed} / {len(segs)} 段", flush=True)
    for i, s in enumerate(segs, 1):
        if only and i != only:
            continue
        rec = st["segments"].setdefault(str(i), {})
        try:
            pr, _ = render_prompts(sb, s)
        except Exception:
            # 纯文本分镜没写全 prompt_* 时模板会缺变量；t2i 只需要动作句，
            # 静态锁全在 t2i_base_prompt 里，不需要套 Qwen 双图模板。
            pr = {"first": s.get("open_action") or "", "last": s.get("end_action") or ""}
        first_path = os.path.join(COMFY_INPUT, f"{pfx}_s{i:02d}_first.png")
        last_path = os.path.join(COMFY_INPUT, f"{pfx}_s{i:02d}_last.png")
        if retry:
            for k in ("first", "last"):
                rec.pop(k, None)
                try:
                    os.remove(first_path if k == "first" else last_path)
                except OSError:
                    pass
        # 纯文本默认链式（用户 2026-11-07 定）：不写 chain 就当连续动作段。
        chained = (s.get("chain", True) is not False) and i > 1
        if chained:
            prev = st["segments"].get(str(i - 1), {}).get("last")
            if not prev or not os.path.exists(prev):
                die(f"段{i} 链式要求段{i-1} 尾帧存在，先跑 t2i 不带 --seg")
            shutil.copy(prev, first_path)
            rec["first"] = first_path
            print(f"[段{i}] 首帧 = 段{i-1} 尾帧（链式，byte-for-byte）", flush=True)
        if not rec.get("first") or not os.path.exists(rec["first"]):
            print(f"[段{i}] t2i 生成首帧", flush=True)
            act = s.get("prompt_first") or s.get("open_action") or pr["first"]
            gen_krea2_t2i(base + "\n" + act, seed, w, h, f"{pfx}_F{i:02d}", first_path,
                          steps, unet)
            rec["first"] = first_path
        if not rec.get("last") or not os.path.exists(rec["last"]):
            print(f"[段{i}] t2i 生成尾帧", flush=True)
            act = s.get("prompt_last") or s.get("end_action") or pr["last"]
            gen_krea2_t2i(base + "\n" + act, seed, w, h, f"{pfx}_L{i:02d}", last_path,
                          steps, unet)
            rec["last"] = last_path
    save_state(sb_path, st)
    print(f"\n完成 {len(segs)} 段关键帧（state 已登记）。下一步：push --storyboard <分镜> → resume", flush=True)
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["check", "prompt", "adopt", "director", "direct", "refs", "t2i", "push", "resume", "frames", "video", "merge", "anchor_probe"])
    ap.add_argument("--storyboard")
    ap.add_argument("--seg", type=int)
    ap.add_argument("--frames-dir", help="adopt 用：外部首尾帧目录，默认 <分镜名>.frames/")
    ap.add_argument("--no-normalize", action="store_true", help="adopt 用：不要裁成精确 9:16")
    ap.add_argument("--out", help="输出路径：director 默认 <分镜名>.director.json，merge 默认 Shared/output/h3-long-video/<标题>.mp4")
    ap.add_argument("--job", help="push 用：Director 任务 id，默认取最新的一个")
    ap.add_argument("--dry-run", action="store_true", help="push 用：只打印不落盘")
    ap.add_argument("--retry", action="store_true", help="清掉该段已有首尾帧，换 seed 重做")
    ap.add_argument("--seed-off", type=int, default=0, help="seed 偏移，用于换一版重试")
    ap.add_argument("--transition", help="整体覆盖转场：cut / dissolve / white / black / wipeleft ...")
    ap.add_argument("--join-dissolve", type=float,
                    help="merge 用：给连续动作的接缝自动加短叠化（秒），如 0.25；0 = 关闭")
    ap.add_argument("--backend", choices=["local", "runninghub"],
                    help="video 用：本次出视频跑哪里，覆盖分镜里的 video_backend")
    ap.add_argument("--max-try", type=int, default=3,
                    help="video 用：每段最多尝试几次（默认 3；做对照测试时设 1，避免 FAIL 后自动重跑）")
    ap.add_argument("--anchor", help="anchor_probe 用：候选锚图（绝对路径或 input 下的文件名）")
    ap.add_argument("--ref", help="direct 用：image_1 人物参考图（input 下文件名或绝对路径）")
    ap.add_argument("--bg", help="direct 用：image_2 无人的场景空镜（input 下文件名或绝对路径）")
    ap.add_argument("--prompt", help="direct 用：直接给的提示词；不写就从 --storyboard 取该段首帧提示词")
    a = ap.parse_args()

    if a.stage == "resume":
        return cmd_resume(a.job)
    if not a.storyboard and a.stage != "direct":
        ap.error("--storyboard 必填（只有 resume / direct 不需要）")
    sb = json.load(open(a.storyboard, encoding="utf-8")) if a.storyboard else {}
    if a.stage == "direct":
        return cmd_direct(a.storyboard, sb, a.ref, a.bg, a.prompt, a.out, a.seg, a.seed_off)
    if a.stage == "check":
        return cmd_check(a.storyboard, sb)
    if a.stage == "prompt":
        return cmd_prompt(a.storyboard, sb, a.seg)
    if a.stage == "adopt":
        return cmd_adopt(a.storyboard, sb, a.seg, a.frames_dir, a.no_normalize)
    if a.stage == "director":
        return cmd_director(a.storyboard, sb, a.out)
    if a.stage == "push":
        return cmd_director_push(a.storyboard, sb, a.job, a.dry_run)

    if a.stage == "refs":
        return cmd_refs(a.storyboard, sb, a.retry)
    if a.stage == "t2i":
        return cmd_t2i(a.storyboard, sb, a.seg, a.retry, a.seed_off)
    if a.stage == "frames":
        return cmd_frames(a.storyboard, sb, a.seg, a.retry, a.seed_off) or 0
    if a.stage == "anchor_probe":
        import anchor_probe
        return anchor_probe.probe(a.storyboard, a.anchor)
    if a.stage == "video":
        return cmd_video(a.storyboard, sb, a.seg, max_try=a.max_try, backend=a.backend) or 0
    return cmd_merge(a.storyboard, sb, a.transition, a.out, a.join_dissolve)


if __name__ == "__main__":
    sys.exit(main())
