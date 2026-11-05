#!/usr/bin/env python
"""RunningHub 后端：把 S4「出视频」从本地 ComfyUI 挪到云端。

为什么单独一个文件
------------------
本地那条链路（``comfy_run`` + ``wait_job``）依赖三样云上没有的东西：
LoadImage 吃本地文件名、节点吐 ``jobs/<id>`` 文本、产物落在本地目录。
RunningHub 是另一套语义：图片要先 upload 换 fileName，任务是 taskId，
产物是 https URL。所以这里只做「提交—轮询—取产物」三件事，
质检、重试、状态机、段间链式继续留在 pipeline.py，不重复实现一遍。

为什么不复用我们的工作流 JSON
----------------------------
``H3-Single-Clip-FirstLastRefs.json`` 的核心节点 ``H3SingleClipDirector``
是本地私有节点，且 ``director.py:31`` 把模型名硬编码成读 ``ROOT/config.json``。
云端既没有这个节点，也没有那个配置文件，导入必红。所以云端要跑的平台
自带的 MiniMax H3 首尾帧（fl2v）工作流——节点编号和字段名跟本地完全不同，
必须由 ``node_map`` 显式指过去，不能猜。

node_map 从哪来
---------------
在 RunningHub 里把工作流跑通一次 → 导出 **API 格式** JSON（不是 UI 格式），
里面每个节点就是 ``"101": {"class_type": "...", "inputs": {"image": ..., "text": ...}}``。
照着它填 ``node_map``：

    "node_map": {
      "first":   ["101", "image"],
      "last":    ["102", "image"],
      "ref":     ["111", "image"],
      "prompt":  ["200", "prompt"],
      "seconds": ["200", "seconds"],
      "seed":    ["200", "seed"]
    }

键是「用途」（见 USE_* 常量），值是 ``[节点 id, 字段名]``。
没映射的用途直接跳过——比如云端工作流不吃参考脸，不写 ``ref`` 就行。

配置优先级：环境变量 > 分镜里的 runninghub 块 > 默认值。
"""
import hashlib
import json
import math
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# 用途 -> 值从哪来。IMAGE 类的字段值是 upload 返回的 fileName，
# 其余直接塞原始值。顺序决定 nodeInfoList 的组装顺序，不影响语义。
USE_IMAGE = ("first", "last", "ref")
USE_VALUE = ("prompt", "seconds", "seed", "steps", "width", "height", "megapixels", "frames")
USES = USE_IMAGE + USE_VALUE

# 官方 Duration Planner 的帧网格：17n+5（124=5秒@24fps）。云端 conditioning 节点
# 的 length 不在网格上会被拒或被拉回，所以这里主动取最近的合法值。
FPS = 24
GRID_STEP = 17
GRID_BASE = 5


def frames_on_grid(seconds, fps=FPS):
    """秒数 -> 向上取整到 17n+5 帧网格（至少 1 格）。

    2026-11-04 定：必须和本地 `h3_frame_count` 同一套「向上取整」。
    之前用「最近取整」，6 秒段会落 141 帧 = 5.875s，段段短一点、
    拼接转场一路往前漂（coffee 实测踩过）。5s→124、6s→158、8s→192。
    """
    n = max(1, math.ceil((float(seconds) * float(fps) - GRID_BASE) / GRID_STEP))
    return GRID_BASE + n * GRID_STEP


def size_for_megapixels(megapixels, ref_w, ref_h, multiple=32):
    """按参考宽高比 + 像素预算算渲染宽高（32 对齐）。

    云端工作流的 conditioning 节点硬性要求宽高被 32 整除，所以先按面积算再吸附。
    0.3MP + 9:16 -> 416×736；0.9MP + 9:16 -> 704×1280（和之前手写的 final_size 一致）。
    """
    ratio = float(ref_w) / float(ref_h)
    w = math.sqrt(float(megapixels) * 1_000_000 * ratio)
    h = w / ratio
    return (max(multiple, int(round(w / multiple)) * multiple),
            max(multiple, int(round(h / multiple)) * multiple))

DEFAULT_HOST = "www.runninghub.ai"
DEFAULT_INSTANCE = "default"
POLL_INTERVAL = 8
TIMEOUT = 2400

# 平台返回码：0 成功，其余都是错误；这三个特殊处理，不抛异常
CODE_OK = 0
CODE_RUNNING = 804
CODE_QUEUED = 813
CODE_FAILED = 805
# 常见致命码，命中时给出人话解释，省得对着数字查文档
CODE_HINT = {
    421: "参数缺失或格式不对（nodeInfoList 里的字段名/类型对不上）",
    433: "apiKey 无效",
    434: "workflowId 不存在（或还没在工作台成功跑过一次）",
    435: "余额不足",
    803: "队列已满，稍后重试",
    814: "个人排队任务超过 1000 条上限",
}


class RHError(RuntimeError):
    """云端返回了非预期结果。带 code，方便上层判断要不要重试。"""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- 配置
def conf(sb):
    """把分镜里的 runninghub 块 + 环境变量合成一份实际生效的配置。"""
    c = dict((sb or {}).get("runninghub") or {})
    env = os.environ
    if env.get("RH_HOST"):
        c["host"] = env["RH_HOST"]
    if env.get("RH_API_KEY"):
        c["api_key"] = env["RH_API_KEY"]
    if env.get("RH_WORKFLOW_ID"):
        c["workflow_id"] = env["RH_WORKFLOW_ID"]
    c.setdefault("host", DEFAULT_HOST)
    c.setdefault("instance_type", DEFAULT_INSTANCE)
    c.setdefault("poll_interval", POLL_INTERVAL)
    c.setdefault("timeout", TIMEOUT)
    c.setdefault("retries", 1)          # 云端重跑要花钱，默认只跑一次
    c.setdefault("node_map", {})
    return c


def check_conf(c):
    """缺关键项就一次说全，别让人试一次发现一个。"""
    miss = []
    if not c.get("api_key"):
        miss.append("api_key（或环境变量 RH_API_KEY）")
    if not c.get("workflow_id"):
        miss.append("workflow_id（或环境变量 RH_WORKFLOW_ID）")
    if not c.get("node_map"):
        miss.append("node_map（照导出的 API 格式 JSON 填）")
    if miss:
        raise RHError("RunningHub 配置缺: " + "、".join(miss))


def base(c):
    """host 允许写成 www.runninghub.ai 或 http://127.0.0.1:8000（后者只给本地自测用）。"""
    host = str(c["host"]).strip().rstrip("/")
    scheme = "https"
    if host.startswith("http://"):
        scheme, host = "http", host[len("http://"):]
    elif host.startswith("https://"):
        host = host[len("https://"):]
    return f"{scheme}://{host}/task/openapi"


# ---------------------------------------------------------------- HTTP
def _read(resp):
    raw = resp.read().decode("utf-8", "ignore")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RHError(f"返回的不是 JSON: {raw[:300]}")


def _post_json(url, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    # 文档要求 Host 头必须精确匹配域名；urllib 一般会自动带，
    # 但有些代理会改写，显式写一份更稳。
    host = urllib.parse.urlsplit(url).netloc
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Host": host},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _read(r)
    except urllib.error.HTTPError as e:
        raise RHError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:400]}")
    except urllib.error.URLError as e:
        raise RHError(f"连不上 {url}：{e.reason}（要不要给 urllib 挂代理？见 SKILL.md 网络一节）")


def _multipart(fields, file_field, file_path):
    """手搓 multipart/form-data：不引入 requests，跟 pipeline.py 保持同一套依赖。"""
    boundary = "----h3lv" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    name = os.path.basename(file_path)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
             f"filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    with open(file_path, "rb") as f:
        body += f.read()
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def upload(c, path):
    """把本地图传上去，换回云端能认的 fileName。已传过的同名同内容会命中缓存。"""
    body, ctype = _multipart({"apiKey": c["api_key"], "fileType": "input"}, "file", path)
    req = urllib.request.Request(f"{base(c)}/upload", data=body, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            j = _read(r)
    except urllib.error.HTTPError as e:
        raise RHError(f"上传失败 HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
    if j.get("code") != CODE_OK:
        raise RHError(f"上传失败 code={j.get('code')} {j.get('msg')}", j.get("code"))
    d = j.get("data") or {}
    fn = d.get("fileName") if isinstance(d, dict) else d
    if not fn:
        raise RHError(f"上传返回里没有 fileName: {j}")
    return fn


def build_node_list(c, images, values):
    """images: {用途: 本地路径}，values: {用途: 值}。按 node_map 组装 nodeInfoList。"""
    nm = c.get("node_map") or {}
    out = []
    for use in USES:
        spec = nm.get(use)
        if not spec:
            continue
        node_id, field, kind = _spec(spec, use)
        if use in USE_IMAGE:
            p = images.get(use)
            if not p:
                continue
            out.append({"nodeId": str(node_id), "fieldName": field,
                        "fieldValue": upload(c, p)})
        else:
            if use not in values or values[use] is None:
                continue
            out.append({"nodeId": str(node_id), "fieldName": field,
                        "fieldValue": _coerce(values[use], kind)})
    return out


def _spec(spec, use):
    """接受三种写法：

        ["200", "seconds"]                 原样传
        ["200", "seconds", "int"]          按类型转（平台要求 fieldValue 类型跟原字段一致）
        {"node": "200", "field": "seconds", "type": "int"}
    """
    kind = None
    if isinstance(spec, dict):
        node_id, field, kind = spec.get("node"), spec.get("field"), spec.get("type")
    elif isinstance(spec, (list, tuple)) and len(spec) in (2, 3):
        node_id, field = spec[0], spec[1]
        kind = spec[2] if len(spec) == 3 else None
    else:
        raise RHError(f"node_map[{use}] 格式不对，要 [节点id, 字段名(, 类型)]: {spec!r}")
    if node_id in (None, "") or not field:
        raise RHError(f"node_map[{use}] 缺 node 或 field: {spec!r}")
    return str(node_id), str(field), (str(kind).lower() if kind else None)


def _coerce(v, kind):
    if not kind or v is None:
        return v
    try:
        if kind == "int":
            return int(round(float(v)))
        if kind == "float":
            return float(v)
        if kind == "str":
            return str(v)
        if kind == "bool":
            return bool(v)
    except (TypeError, ValueError):
        raise RHError(f"值 {v!r} 不能转成 {kind}")
    raise RHError(f"未知类型 {kind!r}（只认 int / float / str / bool）")


def submit(c, node_list):
    payload = {
        "apiKey": c["api_key"],
        "workflowId": str(c["workflow_id"]),
        "nodeInfoList": node_list,
        "instanceType": c["instance_type"],
    }
    if c.get("add_metadata") is not None:
        payload["addMetadata"] = bool(c["add_metadata"])
    if c.get("workflow"):
        # 平台支持直接塞整份 API 格式工作流，此时忽略 workflowId。
        # 好处是不用先在控制台存一份；坏处是每次都要传全量，出问题不好查。
        payload["workflow"] = c["workflow"]
    j = _post_json(f"{base(c)}/create", payload)
    code = j.get("code")
    if code != CODE_OK:
        raise RHError(f"提交失败 code={code} {j.get('msg')}"
                      + (f"（{CODE_HINT[code]}）" if code in CODE_HINT else ""), code)
    d = j.get("data")
    tid = d.get("taskId") if isinstance(d, dict) else d
    if not tid:
        raise RHError(f"提交成功但没拿到 taskId: {j}")
    return str(tid)


def outputs(c, task_id):
    j = _post_json(f"{base(c)}/outputs", {"apiKey": c["api_key"], "taskId": str(task_id)})
    return j


def pick_video(j):
    """从 outputs 里挑出 mp4。各应用返回结构不太一致，能容的都容一下。"""
    d = j.get("data")
    items = d if isinstance(d, list) else (d.get("outputs") if isinstance(d, dict) else [])
    if isinstance(items, dict):
        items = [items]
    for it in items or []:
        if not isinstance(it, dict):
            continue
        url = it.get("fileUrl") or it.get("url")
        if not url:
            continue
        ft = str(it.get("fileType") or "").lower()
        if ft in ("mp4", "video") or url.lower().split("?")[0].endswith((".mp4", ".webm", ".mov")):
            return url
    return None


def poll(c, task_id):
    """轮询到出结果。返回 mp4 的 https 地址。"""
    t0 = time.time()
    interval = float(c.get("poll_interval") or POLL_INTERVAL)
    timeout = float(c.get("timeout") or TIMEOUT)
    last = ""
    while time.time() - t0 < timeout:
        j = outputs(c, task_id)
        code = j.get("code")
        if code == CODE_OK:
            url = pick_video(j)
            if url:
                return url
            raise RHError(f"任务成功但没有视频产物: {json.dumps(j, ensure_ascii=False)[:400]}")
        if code == CODE_FAILED:
            raise RHError(f"任务失败（taskId {task_id}）: {j.get('msg')}", code)
        if code not in (CODE_RUNNING, CODE_QUEUED):
            raise RHError(f"轮询异常 code={code} {j.get('msg')}"
                          + (f"（{CODE_HINT[code]}）" if code in CODE_HINT else ""), code)
        state = "排队中" if code == CODE_QUEUED else "生成中"
        if state != last:
            print(f"    [云] {state} {int(time.time() - t0)}s", flush=True)
            last = state
        elif int(time.time() - t0) % 60 < interval:
            print(f"    [云] {state} {int(time.time() - t0)}s", flush=True)
        time.sleep(interval)
    raise RHError(f"云端超时（{int(timeout)}s），taskId {task_id} 可能还在跑，别急着重提")


def download(url, dest):
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(url, timeout=600) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, dest)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    if not os.path.exists(dest) or os.path.getsize(dest) < 1024:
        raise RHError(f"下载下来的文件不像视频: {dest}")
    return dest


# ---------------------------------------------------------------- 对外主入口
def gen(sb, s, rec, seed, dest_dir, dry=False):
    """跑一段云端视频。返回 (本地 mp4 路径, taskId)。dry=True 只打印不提交。"""
    c = conf(sb)
    check_conf(c)
    seconds = float(s.get("seconds") or sb.get("seconds_per_segment") or 5)
    w, h = sb.get("final_size", [720, 1280])
    # ``runninghub.megapixels``：只压云端的渲染分辨率（省时间/省额度），
    # 本地 S4 仍按 final_size 跑。宽高比跟 final_size 走，宽高吸附到 32 的倍数。
    cloud_mp = (sb.get("runninghub") or {}).get("megapixels")
    if cloud_mp:
        w, h = size_for_megapixels(float(cloud_mp), w, h)
    # 云端 conditioning 节点硬性要求宽高被 32 整除（实测 720 报
    # ValueError），不在网格上就吸附到最近的合法值。
    w = max(32, int(round(w / 32)) * 32)
    h = max(32, int(round(h / 32)) * 32)
    if cloud_mp:
        print(f"    [云] runninghub.megapixels={cloud_mp} → 渲染 {w}×{h}"
              f"（{w * h / 1_000_000:.2f}MP，成片仍按 final_size 输出）", flush=True)
    values = {
        "prompt": _video_prompt(sb, s),
        "seconds": seconds,
        "seed": seed,
        "steps": int((sb.get("runninghub") or {}).get("steps") or sb.get("steps") or 6),
        "width": w,
        "height": h,
        "megapixels": float(sb.get("megapixels", 0.3)),
        "frames": frames_on_grid(seconds, (sb.get("runninghub") or {}).get("fps") or sb.get("fps") or FPS),
    }
    # 云端工作流通常只连首尾帧两张图。我们的提示词模板引用 <Picture3>（参考图
    # 身份锁），没有第三张图时 strict_prompt_tags 会直接拒单（实测 805）。
    # 改写成身份锚定到首帧 <Picture1>，语义不变。
    if "ref" not in (c.get("node_map") or {}):
        values["prompt"] = re.sub(
            r"<Picture\s*3>\s*定义[^。]*。",
            "角色身份、脸型、五官、发型和服装以<Picture1>为首帧基准。",
            values["prompt"])
    images = {"first": rec.get("first"), "last": rec.get("last")}
    ref = sb.get("_ref_path") or _ref(sb)
    if ref:
        images["ref"] = ref

    if dry:
        nm = c.get("node_map") or {}
        print("[dry-run] 将要覆盖的字段：")
        for use in USES:
            if use not in nm:
                continue
            node_id, field, _k = _spec(nm[use], use)
            if use in USE_IMAGE:
                p = images.get(use)
                if p and os.path.exists(p):
                    v = f"{p} → 上传后换成 fileName（{os.path.getsize(p) // 1024} KB）"
                elif p:
                    v = f"{p} → ⚠ 文件不存在"
                else:
                    v = "(缺图，跳过)"
            else:
                v = _coerce(values.get(use), _k)
                if _k:
                    v = f"{v!r} ({_k})"
            shown = str(v).replace("\n", " ⏎ ")
            if len(shown) > 160:
                shown = shown[:160] + f"…（共 {len(str(v))} 字）"
            print(f"  {use:<11} → 节点 {node_id} 的 {field} = {shown}")
        unmapped = [u for u in ("first", "last", "prompt") if u not in nm]
        if unmapped:
            print("  ⚠ 这些用途没配 node_map，云端会用工作流里的默认值: " + "、".join(unmapped))
        return None, None

    node_list = build_node_list(c, images, values)
    have = {n["nodeId"] + "." + n["fieldName"] for n in node_list}
    for need in ("first", "prompt"):
        spec = (c.get("node_map") or {}).get(need)
        if spec:
            node_id, field, _k = _spec(spec, need)
            if f"{node_id}.{field}" not in have:
                raise RHError(f"{need} 映射到了 {node_id}.{field}，但没拿到值（图或提示词缺失）")

    print(f"    [云] 提交 {len(node_list)} 个字段覆盖 → workflow {c['workflow_id']}", flush=True)
    task_id = submit(c, node_list)
    print(f"    [云] taskId {task_id}", flush=True)
    url = poll(c, task_id)
    os.makedirs(dest_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(rec.get("first") or "seg"))[0]
    dest = os.path.join(dest_dir, f"{stem}.mp4")
    download(url, dest)
    return dest, task_id


def _video_prompt(sb, s):
    """复用 pipeline 的模板渲染，避免云端和本地提示词两套写法漂移。"""
    try:
        import pipeline as P
        return P.render_prompts(sb, s)[0]["video"]
    except Exception:
        return s.get("prompt_video") or sb.get("prompt_video") or ""


def _ref(sb):
    try:
        import pipeline as P
        return P.ref_name(sb)
    except Exception:
        for k in ("reference_images",):
            v = sb.get(k)
            if isinstance(v, list) and v:
                return v[0]
        return None


# ---------------------------------------------------------------- 自检
def _cli():
    ap = __import__("argparse").ArgumentParser(description="RunningHub 后端自检，不提交任务")
    ap.add_argument("--storyboard", required=True)
    ap.add_argument("--seg", type=int, default=1)
    a = ap.parse_args()
    sb = json.load(open(a.storyboard, encoding="utf-8"))
    c = conf(sb)
    print(f"host       {c['host']}")
    print(f"workflow   {c.get('workflow_id') or '(未配置)'}")
    print(f"apiKey     {'已配置 ' + hashlib.sha256(str(c['api_key']).encode()).hexdigest()[:8] if c.get('api_key') else '(未配置)'}")
    print(f"instance   {c['instance_type']}   重试 {c['retries']} 次   轮询 {c['poll_interval']}s")
    nm = c.get("node_map") or {}
    print(f"node_map   {len(nm)} 条映射")
    try:
        check_conf(c)
    except RHError as e:
        print(f"\n[未就绪] {e}")
        return 1
    st = json.load(open(os.path.splitext(a.storyboard)[0] + ".state.json", encoding="utf-8")) \
        if os.path.exists(os.path.splitext(a.storyboard)[0] + ".state.json") else {}
    rec = ((st.get("segments") or {}).get(str(a.seg)) or {})
    seg = (sb.get("segments") or [{}])[a.seg - 1]
    gen(sb, seg, rec, 500000 + a.seg * 100 + 1, "", dry=True)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
