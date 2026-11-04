"""RunningHub 云端后端：Director 的「出视频」也可以跑在云端。

为什么单独一个文件
------------------
本地那条链路依赖三样云上没有的东西：LoadImage 吃本地 input 目录里的文件名、
节点吐 ``jobs/<id>`` 文本、产物直接落在本地 output 目录。RunningHub 是另一套
语义：图片要先 upload 换 fileName，任务是 taskId，产物是 https URL。所以这里
只做「上传—提交—轮询—下载」四件事；缓存、重跑、拼接、质检仍留在 runner 里。

配置从哪来
----------
``config.json`` 的 ``runninghub`` 块（Director 每次开任务会把整份 config 落到
任务目录，所以任务目录里那份就是当时实际用的值）。**仓库里那份不带 key**——
这个 repo 是公开的，``api_key`` 留空，真 key 放同目录的 ``runninghub.local.json``
（已 gitignore，只写 ``{"api_key": "..."}``）。优先级从低到高：

    config.json → runninghub.local.json → 环境变量 RH_API_KEY / RH_WORKFLOW_ID / RH_HOST

``node_map`` 照平台导出的 **API 格式** JSON 填（不是 UI 格式），键是用途：

    "first" / "last" / "ref"                图
    "prompt" / "seconds" / "seed" / "steps"
    "width" / "height" / "frames"           值

值是 ``[节点id, 字段名]``，要转类型就写 ``[节点id, 字段名, "int"]``。
没映射的用途直接跳过（比如云端工作流不吃身份参考图，就不写 ``ref``）。

和本地不一样的地方
------------------
* 云端只有单采。段里写了 ``two_pass`` 也没用，会打一行提示后忽略。
* 分辨率按 ``runninghub.megapixels`` 换算（留空则用 final_width/height）；
  成片尺寸仍由拼接阶段统一缩放到 final_width×final_height。
* 只回 mp4，末帧由 runner 用 ffmpeg 抽（链式继承要用）。
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_HOST = "www.runninghub.ai"
DEFAULT_INSTANCE = "plus"
POLL_INTERVAL = 5.0
TIMEOUT = 3600.0

CODE_OK = 0
CODE_RUNNING = 804
CODE_QUEUED = 813
CODE_FAILED = 805
CODE_HINT = {
    803: "队列已满，稍后重试",
    814: "个人排队任务超过 1000 条上限",
}

# 用途 -> 值从哪来。IMAGE 类的字段值是 upload 返回的 fileName，
# 其余直接塞原始值。顺序决定 nodeInfoList 的组装顺序，不影响语义。
USE_IMAGE = ("first", "last", "ref")
USE_VALUE = ("prompt", "seconds", "seed", "steps", "width", "height", "megapixels", "frames")
USES = USE_IMAGE + USE_VALUE

# 官方 Duration Planner 的帧网格：17n+5（124=5 秒@24fps）。云端 conditioning
# 节点的 length 不在网格上会被拒或被拉回，所以主动取最近的合法值。
FPS = 24
GRID_STEP = 17
GRID_BASE = 5


def frames_on_grid(seconds, fps=FPS):
    """秒数 -> 17n+5 网格上不小于请求时长的帧数。

    和本地 ``h3_frame_count`` 一样**向上取整**（不是取最近）：取最近会让 6 秒段
    变成 141 帧 = 5.875 秒，段段短一点点，拼接时按声明秒数算的转场/音频淡出
    会一路往前漂。宁可多几帧，交由拼接的 ``-t`` 截。
    """
    n = max(1, math.ceil((float(seconds) * float(fps) - GRID_BASE) / GRID_STEP))
    return GRID_BASE + n * GRID_STEP


def size_for_megapixels(megapixels, ref_w, ref_h, multiple=32):
    """按参考宽高比 + 像素预算算渲染宽高（32 对齐）。

    云端 conditioning 节点硬性要求宽高被 32 整除，所以先按面积算再吸附。
    0.3MP + 9:16 -> 416x736；0.9MP + 9:16 -> 704x1280。
    """
    ratio = float(ref_w) / float(ref_h)
    w = math.sqrt(float(megapixels) * 1_000_000 * ratio)
    h = w / ratio
    return (
        max(multiple, int(round(w / multiple)) * multiple),
        max(multiple, int(round(h / multiple)) * multiple),
    )


class CloudError(RuntimeError):
    """云端返回了非预期结果。带 code，方便上层判断要不要重试。"""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- 配置
# 密钥不能进仓库（这个 repo 是公开的）：config.json 里 api_key 留空，
# 真 key 放同目录的 runninghub.local.json（已 gitignore），或者走环境变量。
LOCAL_SECRET_PATH = Path(__file__).resolve().parent / "runninghub.local.json"


def _local_secret():
    """读本机私密覆盖文件；不存在/坏 JSON 都当没配，不阻塞启动。"""
    try:
        if not LOCAL_SECRET_PATH.is_file():
            return {}
        data = json.loads(LOCAL_SECRET_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def conf(config):
    """把 config.json 的 runninghub 块 + 本地私密文件 + 环境变量合成一份实际生效的配置。

    优先级（后者盖前者）：config.json → runninghub.local.json → 环境变量。
    """
    c = dict((config or {}).get("runninghub") or {})
    c.update({k: v for k, v in _local_secret().items() if v not in (None, "")})
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
    c.setdefault("retries", 1)  # 云端重跑要花钱，默认只跑一次
    c.setdefault("node_map", {})
    return c


def check_conf(c):
    """缺关键项就一次说全，别让人试一次发现一个。"""
    missing = []
    if not c.get("api_key"):
        missing.append("api_key（或 runninghub.local.json / 环境变量 RH_API_KEY）")
    if not c.get("workflow_id"):
        missing.append("workflow_id（或环境变量 RH_WORKFLOW_ID）")
    if not c.get("node_map"):
        missing.append("node_map（照平台导出的 API 格式 JSON 填）")
    if missing:
        raise CloudError("RunningHub 配置缺: " + "、".join(missing))


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
        raise CloudError(f"返回的不是 JSON: {raw[:300]}")


def _post_json(url, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    # 文档要求 Host 头必须精确匹配域名；urllib 一般会自动带，
    # 但有些代理会改写，显式写一份更稳。
    host = urllib.parse.urlsplit(url).netloc
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Host": host},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _read(r)
    except urllib.error.HTTPError as e:
        raise CloudError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:400]}")
    except urllib.error.URLError as e:
        raise CloudError(f"连不上 {url}：{e.reason}（要给 urllib 挂代理？）")


def _multipart(fields, file_field, file_path):
    """手搓 multipart/form-data：不引入 requests，跟 repo 其它脚本同一套依赖。"""
    boundary = "----h3lv" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    name = os.path.basename(file_path)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    body += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n"
    ).encode()
    with open(file_path, "rb") as f:
        body += f.read()
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def upload(c, path):
    """把本地图传上去，换回云端能认的 fileName。"""
    body, ctype = _multipart({"apiKey": c["api_key"], "fileType": "input"}, "file", path)
    req = urllib.request.Request(f"{base(c)}/upload", data=body, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            j = _read(r)
    except urllib.error.HTTPError as e:
        raise CloudError(f"上传失败 HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
    if j.get("code") != CODE_OK:
        raise CloudError(f"上传失败 code={j.get('code')} {j.get('msg')}", j.get("code"))
    d = j.get("data") or {}
    fn = d.get("fileName") if isinstance(d, dict) else d
    if not fn:
        raise CloudError(f"上传返回里没有 fileName: {j}")
    return fn


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
        raise CloudError(f"node_map[{use}] 格式不对，要 [节点id, 字段名(, 类型)]: {spec!r}")
    if node_id in (None, "") or not field:
        raise CloudError(f"node_map[{use}] 缺 node 或 field: {spec!r}")
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
        raise CloudError(f"值 {v!r} 不能转成 {kind}")
    raise CloudError(f"未知类型 {kind!r}（只认 int / float / str / bool）")


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
            out.append(
                {
                    "nodeId": str(node_id),
                    "fieldName": field,
                    "fieldValue": upload(c, p),
                }
            )
        else:
            if use not in values or values[use] is None:
                continue
            out.append(
                {
                    "nodeId": str(node_id),
                    "fieldName": field,
                    "fieldValue": _coerce(values[use], kind),
                }
            )
    return out


def submit(c, node_list):
    payload = {
        "apiKey": c["api_key"],
        "workflowId": str(c["workflow_id"]),
        "nodeInfoList": node_list,
        "instanceType": c["instance_type"],
    }
    if c.get("workflow"):
        # 平台支持直接塞整份 API 格式工作流，此时忽略 workflowId。
        payload["workflow"] = c["workflow"]
    j = _post_json(f"{base(c)}/create", payload)
    code = j.get("code")
    if code != CODE_OK:
        raise CloudError(
            f"提交失败 code={code} {j.get('msg')}"
            + (f"（{CODE_HINT[code]}）" if code in CODE_HINT else ""),
            code,
        )
    d = j.get("data")
    tid = d.get("taskId") if isinstance(d, dict) else d
    if not tid:
        raise CloudError(f"提交成功但没拿到 taskId: {j}")
    return str(tid)


def outputs(c, task_id):
    return _post_json(f"{base(c)}/outputs", {"apiKey": c["api_key"], "taskId": str(task_id)})


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
            raise CloudError(f"任务成功但没有视频产物: {json.dumps(j, ensure_ascii=False)[:400]}")
        if code == CODE_FAILED:
            raise CloudError(f"任务失败（taskId {task_id}）: {j.get('msg')}", code)
        if code not in (CODE_RUNNING, CODE_QUEUED):
            raise CloudError(
                f"轮询异常 code={code} {j.get('msg')}"
                + (f"（{CODE_HINT[code]}）" if code in CODE_HINT else ""),
                code,
            )
        state = "排队中" if code == CODE_QUEUED else "生成中"
        if state != last:
            print(f"    [云] {state} {int(time.time() - t0)}s", flush=True)
            last = state
        elif int(time.time() - t0) % 60 < interval:
            print(f"    [云] {state} {int(time.time() - t0)}s", flush=True)
        time.sleep(interval)
    raise CloudError(f"云端超时（{int(timeout)}s），taskId {task_id} 可能还在跑，别急着重提")


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
        raise CloudError(f"下载下来的文件不像视频: {dest}")
    return dest


# ---------------------------------------------------------------- 对外主入口
def cloud_size(config, c=None):
    """云端渲染尺寸：默认跟成片一致，配了 runninghub.megapixels 就按预算压。"""
    c = c or conf(config)
    w = int(config.get("final_width") or 832)
    h = int(config.get("final_height") or 1472)
    mp = c.get("megapixels")
    if mp:
        w, h = size_for_megapixels(float(mp), w, h)
    else:
        # 云端 conditioning 节点硬性要求宽高被 32 整除（实测 720 报 ValueError）。
        w = max(32, int(round(w / 32)) * 32)
        h = max(32, int(round(h / 32)) * 32)
    return w, h


def render(config, segment, seed, first_frame, last_frame, dest_dir, label="Segment"):
    """跑一段云端视频。返回 (本地 mp4 路径, taskId)。"""
    c = conf(config)
    check_conf(c)
    seconds = float(segment.get("seconds") or 5)
    if segment.get("two_pass"):
        print(f"    [云] {label}: 云端只跑单采，忽略 two_pass", flush=True)
    width, height = cloud_size(config, c)
    mp = c.get("megapixels")
    if mp:
        print(
            f"    [云] runninghub.megapixels={mp} → 渲染 {width}x{height}"
            f"（{width * height / 1_000_000:.2f}MP，成片仍按 "
            f"{config['final_width']}x{config['final_height']} 输出）",
            flush=True,
        )
    values = {
        "prompt": str(segment.get("prompt") or ""),
        "seconds": seconds,
        "seed": int(seed),
        "steps": int(c.get("steps") or config.get("steps") or 6),
        "width": width,
        "height": height,
        "megapixels": float(mp or config.get("megapixels") or 0.3),
        "frames": frames_on_grid(seconds, c.get("fps") or config.get("fps") or FPS),
    }
    # 云端工作流通常只连首尾帧两张图。提示词里如果引用了第三张图
    # （<Picture 3> 身份锁），没有第三张时 strict_prompt_tags 会直接拒单（实测 805）。
    # 改写成身份锚定到首帧 <Picture 1>，语义不变。
    nm = c.get("node_map") or {}
    if "ref" not in nm:
        values["prompt"] = re.sub(
            r"<Picture\s*3>\s*定义[^。]*。",
            "角色身份、脸型、五官、发型和服装以<Picture1>为首帧基准。",
            values["prompt"],
        )
    images = {"first": first_frame, "last": last_frame}
    refs = config.get("reference_images") or []
    if "ref" in nm and refs:
        images["ref"] = input_path(config, refs[0])

    node_list = build_node_list(c, images, values)
    have = {n["nodeId"] + "." + n["fieldName"] for n in node_list}
    for need in ("first", "prompt"):
        spec = nm.get(need)
        if spec:
            node_id, field, _k = _spec(spec, need)
            if f"{node_id}.{field}" not in have:
                raise CloudError(f"{need} 映射到了 {node_id}.{field}，但没拿到值（图或提示词缺失）")

    print(f"    [云] {label}: 提交 {len(node_list)} 个字段覆盖 → workflow {c['workflow_id']}", flush=True)
    task_id = submit(c, node_list)
    print(f"    [云] {label}: taskId {task_id}", flush=True)
    url = poll(c, task_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{label.replace(' ', '_').lower()}_{seed}_{task_id}.mp4")
    download(url, dest)
    print(f"    [云] {label}: {dest}", flush=True)
    return dest, task_id


def input_path(config, image):
    """参考图可能是 input 目录里的文件名，也可能是任务目录里的绝对路径。"""
    p = os.path.expanduser(str(image))
    if not os.path.isabs(p):
        p = os.path.join(str(config.get("comfy_input_dir") or ""), p)
    return p
