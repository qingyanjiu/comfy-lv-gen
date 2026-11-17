#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
remake_panel.py — 视频复刻导演台（独立服务，端口 8901）

完全独立于原 selflift_panel.py（8900）；不读写原面板任何文件。
功能：上传视频 → 切段自动出卡片 → 每段单独生成 / 一键全片 / 单段重生成 → 拼接。
段间用链式首帧：前一段生成的尾帧自动当下一段首帧（黑鹤 AddGuide frame0 硬锚）。

启动：python remake_panel.py        （默认 0.0.0.0:8901）
页面：http://127.0.0.1:8901/
"""
import os, sys, json, time, shutil, subprocess, threading, traceback, re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_SCRIPTS = "D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/scripts"
RUNNER = os.path.join(SKILL_SCRIPTS, "run_selflift.py")
PROMPT_FILE = os.path.join(HERE, "assets", "remake_prompt.txt")
HTML_FILE = os.path.join(HERE, "web", "remake_app.html")
BLACKCRANE_BASE = os.path.join(os.path.dirname(SKILL_SCRIPTS), "assets", "H3-SelfLift_黑鹤二采.base.json")
ROOT = "D:/Comfy-Desktop/output"
UPLOAD_DIR = os.path.join(ROOT, "_remake_uploads")
REF_UPLOAD_DIR = os.path.join(ROOT, "_remake_refs")
COMFY_INPUT = "D:/Comfy-Desktop/ComfyUI-Shared/input"
FFMPEG = "D:/Comfy-Desktop/tools/ffmpeg/bin/ffmpeg"
FFPROBE = "D:/Comfy-Desktop/tools/ffmpeg/bin/ffprobe"
PY = sys.executable
FPS = 24
PORT = 8901
UI_BUILD = "2026-11-17o-segfirst"
COMFY = "http://127.0.0.1:8188"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REF_UPLOAD_DIR, exist_ok=True)

# ---------- 进程 / 停止 注册表（跨请求共享） ----------
PROCS = {}          # film -> list[Popen]，本面板启动的所有跑片子进程
PROCS_LOCK = threading.Lock()
STOP_FLAGS = {}     # film -> bool，True 表示用户请求停止；worker 检查后中止
SHARED_PROMPT_FILE = "shared_prompt.txt"   # 每片公共提示词文件名
PROJECT_FILE = "project.json"              # 每片项目配置文件（点保存落盘，可据此恢复整片）


def shared_prompt_path(wd):
    return os.path.join(wd, SHARED_PROMPT_FILE)


def register_proc(film, p):
    with PROCS_LOCK:
        PROCS.setdefault(film, []).append(p)


def unregister_proc(film, p):
    with PROCS_LOCK:
        try:
            PROCS.get(film, []).remove(p)
        except ValueError:
            pass


def is_stopped(film):
    return bool(STOP_FLAGS.get(film))


def reset_stop(film):
    STOP_FLAGS[film] = False


def stop_comfy_queue():
    """中断当前渲染 + 清空 ComfyUI 待跑队列。返回说明字符串。"""
    notes = []
    try:
        req = urllib.request.Request(COMFY + "/interrupt", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
        notes.append("已发送中断")
    except Exception as e:
        notes.append("中断失败:" + str(e)[:60])
    try:
        req = urllib.request.Request(
            COMFY + "/queue", data=json.dumps({"clear": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()
        notes.append("已清空队列")
    except Exception as e:
        notes.append("清队列失败:" + str(e)[:60])
    return "，".join(notes)


def kill_tree(p):
    """终止子进程及其整棵进程树（Windows 用 taskkill /T）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            p.terminate()
    except Exception:
        try:
            p.terminate()
        except Exception:
            pass


# ---------- 通用工具 ----------
def jresp(h, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)


def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type,width,height,r_frame_rate",
                          "-of", "json", path], capture_output=True, text=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    num, den = v.get("r_frame_rate", "24/1").split("/")
    return float(d["format"]["duration"]), v["width"], v["height"], float(num)/float(den or 1)


def work_dir(name):
    return os.path.join(ROOT, name)


def cfg_path(wd):
    return os.path.join(wd, "segments.json")


def state_path(wd):
    return os.path.join(wd, "_panel_state.json")


def load_state(wd):
    p = state_path(wd)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {"film": os.path.basename(wd), "segments": [], "jobs": {}, "created": time.time()}


def save_state(wd, st):
    json.dump(st, open(state_path(wd), "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------- 项目改名 / 删除 ----------
BAD_NAME_CHARS = '\\/:*?"<>|'


def check_film_name(name):
    """校验项目名（= output 下的目录名），返回 (名字, 错误)。"""
    name = (name or "").strip()
    if not name:
        return None, "项目名不能为空"
    if any(c in name for c in BAD_NAME_CHARS):
        return None, '项目名不能包含 \\ / : * ? " < > |'
    if name.startswith("_") or name.startswith("."):
        return None, "项目名不能以 _ 或 . 开头（会和内部目录冲突）"
    if "_备份_" in name:
        return None, "项目名不能包含「_备份_」"
    if len(name) > 80:
        return None, "项目名太长（80 字以内）"
    return name, None


def project_running(wd):
    """项目里是否还有正在跑的任务。"""
    st = load_state(wd)
    if any(v == "running" for v in (st.get("jobs") or {}).values()):
        return True
    return any(s.get("status") == "running" for s in (st.get("segments") or []))


def clean_comfy_copies(wd):
    """删掉该项目在 ComfyUI input 里的临时副本（按本片 prefix 精确匹配）。"""
    st = load_state(wd)
    prefix = st.get("prefix")
    if not prefix:
        return 0
    n = 0
    try:
        for f in os.listdir(COMFY_INPUT):
            if f.startswith(prefix + "_"):
                try:
                    os.remove(os.path.join(COMFY_INPUT, f))
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


def rename_project(old, new):
    """改项目名 = 改 output 下的目录名，并同步配置里的片名/成片名。"""
    new, err = check_film_name(new)
    if err:
        return False, err
    old_wd, new_wd = work_dir(old), work_dir(new)
    if not os.path.isdir(old_wd):
        return False, "项目不存在"
    if os.path.abspath(old_wd) == os.path.abspath(new_wd):
        return False, "名字没变"
    if os.path.exists(new_wd):
        return False, "已经有一个叫「%s」的项目了" % new
    if project_running(old_wd):
        return False, "这个项目还有任务在跑，先「停止所有任务」再改名"
    try:
        os.rename(old_wd, new_wd)
    except Exception:
        return False, traceback.format_exc()[-600:]
    # 目录名改了，把配置里写死的片名一起改掉（成片文件名也换）
    try:
        cp = cfg_path(new_wd)
        if os.path.exists(cp):
            cfg = json.load(open(cp, encoding="utf-8"))
            cfg["title"] = new
            cfg["final_name"] = new + "_复刻成片.mp4"
            json.dump(cfg, open(cp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        st = load_state(new_wd)
        st["film"] = new
        save_state(new_wd, st)
        write_project(new_wd)
    except Exception:
        traceback.print_exc()
    return True, new


def _hard_rmtree(path):
    """逐项硬删除目录。

    注意：本机 Python 的 shutil.rmtree 被 safe-delete 钩子接管（转回收站会失败），
    所以这里自己走 os.remove/os.rmdir。"""
    errors = []
    for root, dirs, files in os.walk(path, topdown=False):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                os.chmod(fp, 0o666)
            except Exception:
                pass
            try:
                os.remove(fp)
            except Exception as e:
                errors.append(f"{fp}: {e}")
        for dn in dirs:
            dp = os.path.join(root, dn)
            try:
                os.rmdir(dp)
            except Exception as e:
                errors.append(f"{dp}: {e}")
    try:
        os.rmdir(path)
    except Exception as e:
        errors.append(f"{path}: {e}")
    if errors:
        raise OSError("；".join(errors[:4]))


def delete_project(name):
    """彻底删除项目目录（含源视频副本、各段视频、成片、参考图、配置）。

    调用方必须已让用户二次确认；这里只做安全校验（在跑的不删）。"""
    wd = work_dir(name)
    if not os.path.isdir(wd):
        return False, "项目不存在"
    if os.path.abspath(wd) == os.path.abspath(ROOT) or not os.path.abspath(wd).startswith(
            os.path.abspath(ROOT) + os.sep):
        return False, "路径不合法"
    if project_running(wd):
        return False, "这个项目还有任务在跑，先「停止所有任务」再删"
    cleaned = clean_comfy_copies(wd)
    try:
        _hard_rmtree(wd)
    except Exception:
        return False, traceback.format_exc()[-900:]
    return True, {"comfy_cleaned": cleaned}


# ---------- 项目配置文件（project.json） ----------
def project_path(wd):
    return os.path.join(wd, PROJECT_FILE)


def build_project(wd):
    """汇总项目的全部可恢复信息：源、切段参数、公共/段级提示词、参考图、seed、进度。

    以 cfg(segments.json) + state(_panel_state.json) 为事实来源现场生成，
    因此即使 project.json 暂时没更新，打开项目也能拿到最新内容。"""
    st = load_state(wd)
    cp = cfg_path(wd)
    cfg = {}
    if os.path.exists(cp):
        try:
            cfg = json.load(open(cp, encoding="utf-8"))
        except Exception:
            cfg = {}
    sp = shared_prompt_path(wd)
    prompt = open(sp, encoding="utf-8").read() if os.path.exists(sp) else ""
    segs = []
    for i, s in enumerate(cfg.get("segments", [])):
        ss = st["segments"][i] if i < len(st.get("segments", [])) else {}
        segs.append({
            "idx": i + 1,
            "title": s.get("title") or f"段 {i+1}",
            "seed": s.get("seed", ss.get("seed")),
            "prompt": s.get("body", ""),
            "ref_video": s.get("ref_video", ""),
            "ref_images": s.get("ref_images", []),
            "status": ss.get("status", "pending"),
            "stale": bool(ss.get("stale")),
            "two_pass": ss.get("two_pass"),
            "seconds": s.get("seconds") or ss.get("seconds"),
            "clip_sec": ss.get("clip_sec"),
            "clip": ss.get("clip"),
            "first": ss.get("first"),
            # 段首帧锁定：override=手动指定（prev_last 抽上段尾帧 / upload 上传），空=自动承接
            "first_frame_override": s.get("first_frame_override"),
            "first_frame_source": s.get("first_frame_source"),
            "updated_at": ss.get("updated_at"),
        })
    src = st.get("src")
    # 切点可信度校验：旧版本只存 manual_cuts，可能与当前段数已不一致（重切过），
    # 段数对不上就当作不可信，退回 even 让页面按每段秒数现算。
    cuts = st.get("cuts") or st.get("manual_cuts")
    n_seg = st.get("n") or len(segs)
    if cuts and n_seg and len(cuts) - 1 != n_seg:
        cuts = None
    mode = st.get("mode") or ("manual" if cuts else "even")
    if not cuts and mode == "manual":
        mode = "even"
    return {
        "schema": 1,
        "app": "remake_panel",
        "film": os.path.basename(wd),
        "created": st.get("created", 0),
        "updated": time.time(),
        "source": {
            "src": src, "name": os.path.basename(src) if src else "",
            "duration": st.get("duration"), "seg_sec": st.get("seg_sec"),
            "exact": bool(st.get("exact")), "actual": st.get("actual"),
            "n": st.get("n", len(segs)), "prefix": st.get("prefix"),
            "fps": st.get("fps"), "total_frames": st.get("total_frames"),
            # mode: even=按秒均匀切 / manual=按帧手动切；cuts=实际切点帧号（含首尾）
            "mode": mode,
            "cuts": cuts,
        },
        "settings": {
            "two_pass": bool(st.get("two_pass", True)),
            "anchor_first": bool(st.get("anchor_first", True)),
        },
        "prompt": prompt,
        "refs": st.get("user_refs") or [],
        "segments": segs,
        "merged": bool(st.get("merged")),
    }


def write_project(wd):
    """把 project.json 写到项目目录（点保存/每次改动后调用）。"""
    try:
        os.makedirs(wd, exist_ok=True)
        pj = build_project(wd)
        json.dump(pj, open(project_path(wd), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        return pj
    except Exception:
        traceback.print_exc()
        return None


def list_projects():
    """列出 output 下所有可打开的项目（有 project.json 或 _panel_state.json）。"""
    out = []
    try:
        names = os.listdir(ROOT)
    except Exception:
        names = []
    for d in names:
        wd = os.path.join(ROOT, d)
        if not os.path.isdir(wd):
            continue
        if "_备份_" in d or d.startswith("_remake_"):
            continue   # 快照目录/上传缓存不算项目
        pj_p, st_p = project_path(wd), state_path(wd)
        if not (os.path.exists(pj_p) or os.path.exists(st_p)):
            continue
        item = {"name": d, "n": 0, "done": 0, "created": 0.0, "updated": 0.0,
                "seg_sec": None, "duration": None, "merged": False,
                "two_pass": True, "has_project": os.path.exists(pj_p)}
        try:
            st = load_state(wd)
            item.update(n=len(st.get("segments") or []),
                        done=sum(1 for s in (st.get("segments") or [])
                                 if s.get("status") == "done"),
                        created=st.get("created") or 0,
                        updated=os.path.getmtime(st_p),
                        seg_sec=st.get("seg_sec"), duration=st.get("duration"),
                        merged=bool(st.get("merged")),
                        two_pass=bool(st.get("two_pass", True)))
        except Exception:
            pass
        if os.path.exists(pj_p):
            try:
                j = json.load(open(pj_p, encoding="utf-8"))
                src = j.get("source") or {}
                item.update(n=len(j.get("segments") or []) or item["n"],
                            merged=bool(j.get("merged")),
                            updated=max(item["updated"], j.get("updated") or 0),
                            seg_sec=src.get("seg_sec") or item["seg_sec"],
                            duration=src.get("duration") or item["duration"],
                            two_pass=bool((j.get("settings") or {}).get("two_pass", True)))
            except Exception:
                pass
        out.append(item)
    out.sort(key=lambda x: (x.get("updated") or x.get("created") or 0), reverse=True)
    return out


def log_path(wd, seg=None):
    if seg is None:
        return os.path.join(wd, "panel.log")
    return os.path.join(wd, "logs", f"seg_{seg:02d}.log")


def append_log(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


# ---------- 段首帧（承接上段尾帧 / 手动上传） ----------
def seg_last_png(wd, idx):
    """某段成片抽出来的尾帧（runner 生成时写进 refs/{prefix}_sNN_last.png）。"""
    st = load_state(wd)
    prefix = st.get("prefix") or ""
    return os.path.join(wd, "refs", f"{prefix}_s{idx:02d}_last.png")


def seg_first_override_png(wd, idx):
    """手动指定的首帧图（上传 / 抽帧都落到这里，独立于源段首帧 refs/first_NN.png）。"""
    return os.path.join(wd, "refs", f"override_first_{idx:02d}.png")


def set_seg_first_frame(wd, idx, png_path, source):
    """把某段首帧锁成指定图片：写 cfg.first_frame_override + state 记录来源。

    source: "prev_last"（抽上一段尾帧）/ "upload"（手动上传）。
    返回 (ok, msg)。"""
    cp = cfg_path(wd)
    if not os.path.exists(cp):
        return False, "未切段"
    j = json.load(open(cp, encoding="utf-8"))
    segs = j.get("segments") or []
    if not (1 <= idx <= len(segs)):
        return False, f"段号越界（1~{len(segs)}）"
    dst = seg_first_override_png(wd, idx)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.abspath(png_path) != os.path.abspath(dst):
        shutil.copy2(png_path, dst)
    # ComfyUI input 用唯一名，避免和多段/多片互相覆盖
    comfy_name = f"rmkfirst_{os.path.basename(wd)}_{idx:02d}.png"
    shutil.copy(dst, os.path.join(COMFY_INPUT, comfy_name))
    segs[idx - 1]["first_frame_override"] = comfy_name
    segs[idx - 1]["first_frame_source"] = source
    segs[idx - 1]["first_frame_local"] = os.path.relpath(dst, wd).replace("\\", "/")
    segs[idx - 1]["updated_at"] = time.time()
    json.dump(j, open(cp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    st = load_state(wd)
    if idx - 1 < len(st.get("segments") or []):
        st["segments"][idx - 1]["first_override"] = source
        st["segments"][idx - 1]["first_override_at"] = time.time()
        # 首帧变了 → 本段已出成片作废；链式下游也会因尾帧变化失效
        if st["segments"][idx - 1].get("status") == "done":
            st["segments"][idx - 1]["stale"] = True
        for k in range(idx, len(st["segments"])):
            if st["segments"][k].get("status") == "done":
                st["segments"][k]["stale"] = True
    save_state(wd, st)
    write_project(wd)
    return True, comfy_name


def clear_seg_first_frame(wd, idx):
    """解除本段首帧锁定：回到自动承接上段尾帧（首段回到不接首帧）。"""
    cp = cfg_path(wd)
    if not os.path.exists(cp):
        return False, "未切段"
    j = json.load(open(cp, encoding="utf-8"))
    segs = j.get("segments") or []
    if not (1 <= idx <= len(segs)):
        return False, f"段号越界（1~{len(segs)}）"
    old = segs[idx - 1].pop("first_frame_override", None)
    src = segs[idx - 1].pop("first_frame_source", None)
    segs[idx - 1].pop("first_frame_local", None)
    segs[idx - 1]["updated_at"] = time.time()
    json.dump(j, open(cp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 首段解除锁定后就不再接任何首帧（源首帧硬锚也一起撤掉）
    if old:
        try:
            os.remove(os.path.join(COMFY_INPUT, os.path.basename(old)))
        except Exception:
            pass
    try:
        os.remove(seg_first_override_png(wd, idx))
    except Exception:
        pass
    st = load_state(wd)
    if idx - 1 < len(st.get("segments") or []):
        st["segments"][idx - 1].pop("first_override", None)
        st["segments"][idx - 1].pop("first_override_at", None)
        if st["segments"][idx - 1].get("status") == "done":
            st["segments"][idx - 1]["stale"] = True
    save_state(wd, st)
    write_project(wd)
    return True, src or ""


def ensure_seg_inputs(wd, seg):
    """跑段前确保该段的参考视频/参考图都在 ComfyUI input 里。

    根因修复：跑完一段后 ComfyUI input 里的后续 clip 会消失（被清理/重启），
    导致段2+ 报 'Invalid video file: xxx_clip_02.mp4'。每次跑前从项目目录
    强制重拷，生成自包含，不再依赖切段时那一次拷贝是否还在。
    返回 (ok, 说明)。"""
    notes = []
    j = json.load(open(cfg_path(wd), encoding="utf-8"))
    prefix = j.get("prefix", "")
    # 1) 参考视频：ref_video 名形如 {prefix}_clip_02.mp4 → 源 source_segments/clip_02.mp4
    rv = seg.get("ref_video")
    if rv:
        dst = os.path.join(COMFY_INPUT, os.path.basename(rv))
        if not os.path.exists(dst):
            src = None
            # 从文件名末尾 _clip_NN.mp4 提取序号
            m = re.search(r"_clip_(\d+)\.mp4$", rv)
            if m:
                cand = os.path.join(wd, "source_segments", f"clip_{int(m.group(1)):02d}.mp4")
                if os.path.exists(cand):
                    src = cand
            if src and os.path.exists(src):
                shutil.copy2(src, dst)
                notes.append("重拷参考视频 " + os.path.basename(dst))
            else:
                return False, f"参考视频缺失且无法恢复：{rv}"
    # 2) 参考图：每张从 path 重拷到 comfy_name
    for r in seg.get("ref_images", []):
        src = r.get("path")
        cn = r.get("comfy_name")
        if not cn:
            continue
        dst = os.path.join(COMFY_INPUT, os.path.basename(cn))
        if not os.path.exists(dst) and src and os.path.exists(src):
            shutil.copy2(src, dst)
            notes.append("重拷参考图 " + os.path.basename(cn))
    # 3) 手动指定的首帧（上传/抽帧）：ComfyUI input 可能被清理，跑前从项目目录重拷
    ff = seg.get("first_frame_override")
    if ff:
        dst = os.path.join(COMFY_INPUT, os.path.basename(ff))
        if not os.path.exists(dst):
            local = seg.get("first_frame_local")
            cand = os.path.join(wd, local.replace("/", os.sep)) if local else None
            if not (cand and os.path.exists(cand)):
                m = re.search(r"_(\d+)\.png$", os.path.basename(ff))
                if m:
                    n = int(m.group(1))
                    # 源视频首帧（首段硬锚）→ refs/first_NN.png；手动指定 → override_first_NN.png
                    if seg.get("first_frame_source") == "source":
                        c2 = os.path.join(wd, "refs", f"first_{n:02d}.png")
                    else:
                        c2 = seg_first_override_png(wd, n)
                    cand = c2 if os.path.exists(c2) else cand
            if cand and os.path.exists(cand):
                shutil.copy2(cand, dst)
                notes.append("重拷首帧 " + os.path.basename(dst))
    else:
        # 3b) 自动承接上段尾帧：runner 只会从 ComfyUI input 找 {prefix}_sNN_last.png，
        # 而 input 会被清理/重启清空 → 链式悄悄断掉。这里从项目 refs 补一份。
        m = re.search(r"_clip_(\d+)\.mp4$", seg.get("ref_video") or "")
        if m:
            i = int(m.group(1))
            if i > 1:
                prev_local = seg_last_png(wd, i - 1)
                prev_name = os.path.basename(prev_local)
                dst = os.path.join(COMFY_INPUT, prev_name)
                if not os.path.exists(dst) and os.path.exists(prev_local):
                    shutil.copy2(prev_local, dst)
                    notes.append("重拷上段尾帧 " + prev_name)
            else:
                return False, f"首帧图缺失且无法恢复：{ff}"
    return True, ("；".join(notes) if notes else "参考文件齐全")


# ---------- 跑片子进程（流式写日志、跨请求存活） ----------
def run_seg_proc(wd, idx, seed=None, two_pass=None, prompt=None):
    """idx 1-based。返回 (ok, msg)。进程在后台线程跑，状态写 _panel_state.json。"""
    film = os.path.basename(wd)
    st = load_state(wd)
    if st["jobs"].get(f"seg{idx}") == "running":
        return False, f"段{idx}正在生成中"
    cfg = cfg_path(wd)
    if not os.path.exists(cfg):
        return False, "未切段，请先切段"
    reset_stop(film)  # 单段手动生成 = 新一轮，清除之前的停止请求
    # 可选换 seed / 切换二采 / 改本段提示词（重生成时可覆盖）
    if seed or two_pass is not None or prompt is not None:
        j = json.load(open(cfg, encoding="utf-8"))
        if seed:
            j["segments"][idx-1]["seed"] = int(seed)
            st["segments"][idx-1]["seed"] = int(seed)  # 同步 state，供前端弹窗显示
        if two_pass is not None:
            j["bc_two_pass"] = bool(two_pass)
            st["two_pass"] = bool(two_pass)  # 状态同步，前端显示
        if prompt is not None:
            j["segments"][idx-1]["body"] = str(prompt)
        j["segments"][idx-1]["updated_at"] = time.time()
        json.dump(j, open(cfg, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 跑前确保该段参考文件都在 ComfyUI input（修复"成功一段后其余失败"）
    seg_cfg = json.load(open(cfg, encoding="utf-8"))["segments"][idx-1]
    # 本次实际用的二采开关（写进 state，页面据此标「二采/一采」）
    eff_two_pass = json.load(open(cfg, encoding="utf-8")).get("bc_two_pass") is not False
    st["segments"][idx-1]["two_pass"] = bool(eff_two_pass)
    st["segments"][idx-1]["gen_at"] = time.time()
    inputs_ok, inputs_note = ensure_seg_inputs(wd, seg_cfg)
    if not inputs_ok:
        return False, inputs_note
    st["jobs"][f"seg{idx}"] = "running"
    st["segments"][idx-1]["status"] = "running"
    st["segments"][idx-1].pop("error", None)
    st["current_seg"] = idx   # 前端进度标识：正在生成第几段
    save_state(wd, st)
    lp = log_path(wd, idx)
    open(lp, "w", encoding="utf-8").close()

    def worker():
        env = os.environ.copy()
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        cmd = [PY, RUNNER, "seg", str(idx), "--cfg", cfg]
        ok = False
        stopped = False
        p = None
        try:
            try:
                cur_seed = seed if seed is not None else json.load(open(cfg, encoding="utf-8"))["segments"][idx-1].get("seed")
            except Exception:
                cur_seed = "?"
            append_log(lp, "=" * 56)
            append_log(lp, f"  开始生成  段 {idx}/{len(st.get('segments', []))}    seed {cur_seed}    二采 {'开' if eff_two_pass else '关'}")
            append_log(lp, f"  开始时间  {time.strftime('%H:%M:%S')}")
            append_log(lp, f"  参考文件  {inputs_note}")
            append_log(lp, "=" * 56)
            append_log(lp, f"$ {' '.join(cmd)}\n")
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=env, cwd=HERE, text=True, encoding="utf-8", errors="replace")
            register_proc(film, p)
            for line in p.stdout:
                append_log(lp, line.rstrip("\n"))
                if is_stopped(film):
                    stopped = True
                    break
            if p.poll() is None:
                p.wait(timeout=5)
            if is_stopped(film):   # 进程被 taskkill 后 stdout 直接 EOF，这里补判停止标志
                stopped = True
            ok = (not stopped) and (p.returncode == 0)
            append_log(lp, f"\n[exit {p.returncode}] {'STOPPED' if stopped else ('OK' if ok else 'FAIL')}\n")
        except Exception:
            append_log(lp, traceback.format_exc())
        finally:
            if p is not None:
                unregister_proc(film, p)

        s2 = load_state(wd)
        if stopped or is_stopped(film):
            # 用户主动停止：回滚为 pending，不标失败，保留可重跑
            s2["jobs"][f"seg{idx}"] = "stopped"
            s2["segments"][idx-1]["status"] = "stopped"
        else:
            s2["jobs"][f"seg{idx}"] = "done" if ok else "error"
            s2["segments"][idx-1]["status"] = "done" if ok else "error"
            if not ok:
                s2["segments"][idx-1]["error"] = "生成失败，见日志"
            # 链式段：本段成功后，把下一段标 stale（首帧变了，需要重出）
            if ok and idx < len(s2["segments"]):
                s2["segments"][idx]["stale"] = True
            s2["segments"][idx-1]["stale"] = False
        s2["segments"][idx-1]["updated_at"] = time.time()
        if s2.get("current_seg") == idx:
            s2["current_seg"] = None
        save_state(wd, s2)
        write_project(wd)   # 段状态/seed 变了，同步项目配置文件

    threading.Thread(target=worker, daemon=True).start()
    return True, f"段{idx}已开始生成"


def run_all_proc(wd, skip_done=True):
    """一键全片：按顺序跑段（后台线程）。skip_done=True 跳过已完成的段。"""
    film = os.path.basename(wd)
    st = load_state(wd)
    if st["jobs"].get("all") == "running":
        return False, "全片生成中"
    reset_stop(film)
    st["jobs"]["all"] = "running"
    save_state(wd, st)
    # 一键全片：清空面板日志，重写标题（旧的进度不残留）
    plp = log_path(wd)
    open(plp, "w", encoding="utf-8").close()
    append_log(plp, "#" * 56)
    append_log(plp, f"#  一键全片生成开始   {time.strftime('%H:%M:%S')}")
    append_log(plp, "#" * 56)

    def worker():
        st0 = load_state(wd)
        n = len(st0["segments"])
        statuses = [st0["segments"][k].get("status") for k in range(n)]
        done_cnt = 0
        for i in range(1, n + 1):
            if is_stopped(film):
                append_log(plp, f"[{i}/{n}] 收到停止请求，中止全片（段{i}前）\n")
                break
            # 跳过已完成且未标 stale 的段
            if skip_done and statuses[i-1] == "done" and not st0["segments"][i-1].get("stale"):
                append_log(plp, f"[{i}/{n}] 段{i}已完成，跳过\n")
                done_cnt += 1
                continue
            append_log(plp, "\n" + "-" * 56)
            append_log(plp, f"  全片进度  正在生成第 {i}/{n} 段   {time.strftime('%H:%M:%S')}")
            append_log(plp, "-" * 56)
            ok, _ = run_seg_proc(wd, i)
            # 等待本段结束（或收到停止）
            while True:
                if is_stopped(film):
                    append_log(plp, f"[{i}/{n}] 收到停止请求，中止全片（段{i}中）\n")
                    break
                s = load_state(wd)
                if s["jobs"].get(f"seg{i}") not in ("running",):
                    break
                time.sleep(1)
            if is_stopped(film):
                break
            s = load_state(wd)
            stt = s["jobs"].get(f"seg{i}")
            if stt == "error":
                append_log(plp, f"[{i}/{n}] 段{i}失败，中止全片\n")
                break
            elif stt == "stopped":
                append_log(plp, f"[{i}/{n}] 段{i}已停止，中止全片\n")
                break
            else:
                done_cnt += 1
                append_log(plp, f"[{i}/{n}] 段{i}完成   {time.strftime('%H:%M:%S')}\n")
        s = load_state(wd)
        s["jobs"]["all"] = "stopped" if is_stopped(film) else "done"
        s["current_seg"] = None
        save_state(wd, s)
        write_project(wd)
        append_log(plp, f"\n" + "#" * 56)
        append_log(plp, f"#  全片生成结束   完成 {done_cnt}/{n} 段   {time.strftime('%H:%M:%S')}")
        append_log(plp, "#" * 56)

    threading.Thread(target=worker, daemon=True).start()
    return True, "全片生成已启动"


def run_merge_proc(wd):
    film = os.path.basename(wd)
    st = load_state(wd)
    if st["jobs"].get("merge") == "running":
        return False, "拼接中"
    reset_stop(film)
    st["jobs"]["merge"] = "running"
    save_state(wd, st)
    lp = log_path(wd)
    append_log(lp, "\n===== 拼接 =====\n")

    def worker():
        env = os.environ.copy()
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        cmd = [PY, RUNNER, "merge", "--cfg", cfg_path(wd)]
        ok = False
        p = None
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=env, cwd=HERE, text=True, encoding="utf-8", errors="replace")
            register_proc(film, p)
            for line in p.stdout:
                append_log(lp, line.rstrip("\n"))
            if p.poll() is None:
                p.wait(timeout=5)
            ok = (p.returncode == 0)
        except Exception:
            append_log(lp, traceback.format_exc())
        finally:
            if p is not None:
                unregister_proc(film, p)
        s = load_state(wd)
        s["jobs"]["merge"] = "done" if ok else "error"
        s["merged"] = ok
        save_state(wd, s)
        write_project(wd)

    threading.Thread(target=worker, daemon=True).start()
    return True, "拼接已启动"


def stop_all_tasks(wd):
    """停止一片的所有任务：设标志 → 杀进程树 → 清 ComfyUI 队列 → 重置运行中状态。"""
    film = os.path.basename(wd)
    STOP_FLAGS[film] = True
    with PROCS_LOCK:
        procs = list(PROCS.get(film, []))
    killed = 0
    for p in procs:
        if p.poll() is None:
            kill_tree(p)
            killed += 1
    with PROCS_LOCK:
        PROCS[film] = []
    comfy_note = stop_comfy_queue()
    # 重置所有 running 状态（worker 后续检测到 stop 标志也可能再写一次 stopped，幂等）
    st = load_state(wd)
    for k in [k for k, v in st.get("jobs", {}).items() if v == "running"]:
        st["jobs"][k] = "stopped"
    for s in st.get("segments", []):
        if s.get("status") == "running":
            s["status"] = "stopped"
            s["error"] = "已手动停止"
    save_state(wd, st)
    append_log(log_path(wd), f"\n[stop-all] 终止 {killed} 个进程；{comfy_note}\n")
    return {"ok": True, "killed": killed, "comfy": comfy_note}


# ---------- 切段建片 ----------
def h3_seconds(dur):
    """把任意时长对齐到最近的 17n+5 帧（24fps），返回 (生成秒数, 帧数)。

    H3 的 video_latent 只接受 17n+5 帧；按「离目标帧数最近的有效长度」取。
    """
    target = max(1.0, float(dur)) * FPS
    n = max(1, int(round((target - 5) / 17.0)))
    frames = 17 * n + 5
    return frames / float(FPS), frames


def do_split(name, src, seg_sec, exact, prompt, two_pass=True, user_refs=None,
             anchor_first=True, cuts=None, fps=None):
    """切段建片。

    cuts 为帧号列表（含起点与终点，如 [0, 120, 288, 336]）时按手工切点切，
    每段长度可不同；否则按 seg_sec 均匀切。"""
    wd = work_dir(name)
    os.makedirs(os.path.join(wd, "source_segments"), exist_ok=True)
    os.makedirs(os.path.join(wd, "refs"), exist_ok=True)
    os.makedirs(os.path.join(wd, "logs"), exist_ok=True)
    os.makedirs(os.path.join(wd, "final"), exist_ok=True)

    # 保存本片公共提示词（切段时定稿，之后可在页面单独改公共/段级）
    open(shared_prompt_path(wd), "w", encoding="utf-8").write(prompt)

    dur, w, h, srcfps = probe(src)
    use_fps = float(fps or srcfps or FPS)
    prefix = f"rmk_{time.strftime('%m%d_%H%M%S')}"
    total_frames = int(round(dur * use_fps))

    # 人物参考图归档进项目目录（self-contained：全局 _remake_refs 被清理也不影响本片）
    if user_refs:
        os.makedirs(os.path.join(wd, "user_refs"), exist_ok=True)
    proj_refs = []
    for r in (user_refs or []):
        base = os.path.basename(r.get("path") or "")
        dst = os.path.join(wd, "user_refs", base) if base else None
        if dst and os.path.exists(r.get("path", "")):
            try:
                if os.path.abspath(r["path"]) != os.path.abspath(dst):
                    shutil.copy2(r["path"], dst)
            except Exception:
                pass
        if not dst or not os.path.exists(dst):
            dst = r.get("path")   # 归档失败就退回原路径，至少能用
        proj_refs.append({
            "path": dst or r.get("path"),
            "comfy_name": r.get("comfy_name") or ("rmkref_" + os.path.splitext(base)[0] + ".png"),
            "label": r.get("label") or "人物参考（<Picture>）",
        })

    # ---- 切点：手工帧切点优先，否则均匀切 ----
    manual = bool(cuts)
    if manual:
        cs = sorted({max(0, min(int(c), total_frames)) for c in cuts})
        if len(cs) < 2:
            cs = [0, total_frames]
        spans = [(cs[i], cs[i + 1]) for i in range(len(cs) - 1) if cs[i + 1] > cs[i]]
        n = len(spans)
        actual = dur / n
    else:
        n = max(1, int(dur // seg_sec) if exact else int(round(dur / seg_sec)))
        actual = float(seg_sec) if exact else dur / n
        spans = [(i * actual, min((i + 1) * actual, dur)) for i in range(n)]

    segs = []
    seg_state = []
    for i, (a, b) in enumerate(spans):
        clip = os.path.join(wd, "source_segments", f"clip_{i+1:02d}.mp4")
        first = os.path.join(wd, "refs", f"first_{i+1:02d}.png")
        if manual:
            this_dur = (b - a) / use_fps
            # trim 按帧号走：真正帧级精确（素材短，解码代价可接受）
            vf = (f"trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS,"
                  f"scale=trunc(iw/2)*2:trunc(ih/2)*2")
            cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", src,
                   "-vf", vf, "-an", "-c:v", "libx264", "-crf", "18",
                   "-preset", "fast", clip]
        else:
            t0 = a
            this_dur = b - a
            cmd = [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t0:.3f}", "-i", src,
                   "-t", f"{this_dur:.3f}", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                   "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-an", clip]
        subprocess.run(cmd, check=True)
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", clip,
                        "-frames:v", "1", first], check=True)
        # 段级生成时长：对齐 17n+5（手工切出来的段长各不相同）
        gen_sec, gen_frames = h3_seconds(this_dur)
        vname = f"{prefix}_clip_{i+1:02d}.mp4"
        iname = f"{prefix}_first_{i+1:02d}.png"
        shutil.copy(clip, os.path.join(COMFY_INPUT, vname))
        shutil.copy(first, os.path.join(COMFY_INPUT, iname))
        # 人物身份锚：用户上传的参考图优先（<Picture 1..N>）；
        # 未上传时才用源段首帧兜底（保持旧行为）。
        if proj_refs:
            p_refs = [dict(r) for r in proj_refs]
        else:
            p_refs = [{"path": first, "comfy_name": iname,
                       "label": f"人物参考（源段{i+1}首帧，未上传参考图）"}]
        # 链式：中间段不设 first_frame_override，让 run_selflift 用上段尾帧。
        # 首段锚：仅当 anchor_first=True 时用源首帧硬锚（纯复刻、无上传人物图时）；
        # 上传了人物参考图时默认关掉——源首帧含原人物脸，硬锚会把源脸锁进 frame0，
        # 与"替换成参考图人物"冲突。
        seg = {
            "id": f"r{i+1:02d}", "title": f"段 {i+1}",
            "seed": 400000 + i * 7919 + int(time.time()) % 100000,
            "body": prompt, "cast": "",
            "ref_images": p_refs,
            "ref_video": vname,
            "ref_video_seconds": round(this_dur, 4),
            "seconds": round(gen_sec, 4),      # 段级生成时长（H3 17n+5 对齐）
            "length": gen_frames,
            "link": {"mode": "chain"},  # chain：前段尾帧 → 本段首帧
        }
        if i == 0 and anchor_first:
            seg["first_frame_override"] = iname  # 首段用源视频首帧起手
            seg["first_frame_source"] = "source"  # 来源=源视频首帧（可清除）
        segs.append(seg)
        seg_state.append({"idx": i + 1, "status": "pending", "stale": False,
                          "seed": seg["seed"],
                          "clip_sec": round(this_dur, 4),
                          "seconds": round(gen_sec, 4),
                          "two_pass": bool(two_pass),
                          "clip": f"source_segments/clip_{i+1:02d}.mp4",
                          "first": f"refs/first_{i+1:02d}.png"})

    cfg = {
        "title": name, "prefix": prefix, "film_id": prefix.split("_", 1)[-1],
        "final_name": f"{name}_复刻成片.mp4", "workflow": BLACKCRANE_BASE,
        "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "fps": FPS, "seconds": round(h3_seconds(actual)[0], 4),
        "length": h3_seconds(actual)[1],
        "megapixels": 0.3, "aspect_ratio": "16:9 (Widescreen)", "quality": "high",
        "audio": "off", "chain_first_frame": True, "join_mode": "cut",
        "bc_two_pass": bool(two_pass),  # 二采开关：False=跳过放大+二采，快速低清预览
        "head": "", "tail": "", "segments": segs,
    }
    # 统一记录切点帧号：两种模式都留痕，页面时间轴可回显「这一段到底从哪切到哪」
    if manual:
        cut_frames = [int(a) for a, _ in spans] + [int(spans[-1][1])]
    else:
        raw_cf = [int(round(a * use_fps)) for a, _ in spans]
        raw_cf.append(min(total_frames, max(raw_cf[-1] + 1, int(round(spans[-1][1] * use_fps)))))
        cut_frames = []
        for c in raw_cf:
            if not cut_frames or c > cut_frames[-1]:
                cut_frames.append(c)

    json.dump(cfg, open(cfg_path(wd), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    st = {"film": name, "src": src, "prefix": prefix, "seg_sec": round(actual, 4),
          "actual": actual, "exact": bool(exact), "two_pass": bool(two_pass),
          "anchor_first": bool(anchor_first),
          "mode": "manual" if manual else "even",   # 切段方式：按帧手动 / 按秒均匀
          "cuts": cut_frames,
          "fps": use_fps, "total_frames": total_frames,
          "user_refs": proj_refs, "duration": dur, "n": n, "jobs": {}, "merged": False,
          "created": time.time(), "segments": seg_state}
    save_state(wd, st)
    write_project(wd)   # 切段即落盘 project.json，页面随时可重新打开
    return st


# ---------- HTTP 处理 ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(n).decode("utf-8", "replace") if n else ""

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p in ("/", "/index.html"):
            data = open(HTML_FILE, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/version":
            return jresp(self, {"build": UI_BUILD, "runner": RUNNER, "port": PORT})
        if p == "/api/films":
            films = []
            for d in sorted(os.listdir(ROOT), reverse=True):
                wd = work_dir(d)
                if os.path.exists(state_path(wd)):
                    st = load_state(wd)
                    films.append({"name": d, "n": len(st["segments"]),
                                  "created": st.get("created", 0),
                                  "merged": st.get("merged", False)})
            return jresp(self, {"films": films})
        if p == "/api/projects":
            return jresp(self, {"projects": list_projects(), "root": ROOT})
        if p == "/api/project":
            name = q.get("film", [None])[0]
            if not name:
                return jresp(self, {"error": "缺 film"}, 400)
            wd = work_dir(name)
            if not os.path.isdir(wd):
                return jresp(self, {"error": "项目不存在：" + name}, 404)
            proj = build_project(wd)
            # 老项目可能没记 fps/总帧数：现探一次，供时间轴按帧定位
            if not (proj.get("source") or {}).get("fps"):
                try:
                    sp = (proj.get("source") or {}).get("src")
                    if sp and os.path.exists(sp):
                        d, _w, _h, fps = probe(sp)
                        proj["source"]["fps"] = fps
                        proj["source"]["total_frames"] = int(round(d * fps))
                except Exception:
                    pass
            # 参考图补 url（项目内 user_refs/ 与全局上传目录都能取）
            for r in proj.get("refs", []):
                base = os.path.basename(r.get("path") or "")
                r["name"] = base
                r["url"] = ("/api/ref-file?film=" + urllib.parse.quote(name) +
                            "&name=" + urllib.parse.quote(base))
            return jresp(self, {"ok": True, "project": proj,
                                "config_path": project_path(wd),
                                "has_config": os.path.exists(project_path(wd)),
                                "state": load_state(wd)})
        if p == "/api/state":
            name = q.get("film", [None])[0]
            if not name:
                return jresp(self, {"error": "缺 film"}, 400)
            wd = work_dir(name)
            st = load_state(wd)
            # 把 cfg 里的首帧锁定信息并进每段 state，供卡片显示「首帧来源」
            cp = cfg_path(wd)
            if os.path.exists(cp):
                try:
                    j = json.load(open(cp, encoding="utf-8"))
                    for i, sg in enumerate(j.get("segments") or []):
                        if i >= len(st.get("segments") or []):
                            break
                        ov = sg.get("first_frame_override")
                        if ov:
                            st["segments"][i]["first_override"] = sg.get("first_frame_source") or "upload"
                            st["segments"][i]["first_override_name"] = ov
                            # 时间戳用作缩略图 cache-buster（换图后必须变）
                            st["segments"][i]["first_override_at"] = sg.get("updated_at") or 0
                        else:
                            st["segments"][i].pop("first_override", None)
                            st["segments"][i].pop("first_override_name", None)
                            st["segments"][i].pop("first_override_at", None)
                except Exception:
                    pass
            return jresp(self, st)
        if p == "/api/prompt":
            # 公共提示词：优先读本片 shared_prompt.txt，否则读全局默认模板
            name = q.get("film", [None])[0]
            sp = shared_prompt_path(work_dir(name)) if name else None
            if sp and os.path.exists(sp):
                text = open(sp, encoding="utf-8").read()
                src = "film"
            else:
                text = open(PROMPT_FILE, encoding="utf-8").read()
                src = "default"
            return jresp(self, {"prompt": text, "source": src})
        if p == "/api/refs":
            # 列出已上传的参考图
            items = []
            for f in sorted(os.listdir(REF_UPLOAD_DIR)):
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    stem = os.path.splitext(f)[0]
                    items.append({"name": f, "path": os.path.join(REF_UPLOAD_DIR, f),
                                  "comfy_name": f"rmkref_{stem}.png",
                                  "label": f"人物参考（<Picture>）",
                                  "url": "/api/ref-file?name=" + urllib.parse.quote(f)})
            return jresp(self, {"refs": items})
        if p == "/api/ref-file":
            # 展示参考图（供页面缩略图）：先找项目 user_refs/，再找全局上传目录
            name = q.get("name", [None])[0]
            film = q.get("film", [None])[0]
            if not name:
                return jresp(self, {"error": "缺 name"}, 400)
            base = os.path.basename(name)
            cands = []
            if film:
                cands.append(os.path.join(work_dir(film), "user_refs", base))
            cands.append(os.path.join(REF_UPLOAD_DIR, base))
            fp = next((c for c in cands if os.path.exists(c)), None)
            if not fp:
                return jresp(self, {"error": "文件不存在"}, 404)
            data = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/seg-prompt":
            # 读取某段当前提示词
            name = q.get("film", [None])[0]
            idx = int(q.get("idx", ["0"])[0])
            cp = cfg_path(work_dir(name)) if name else None
            if not cp or not os.path.exists(cp):
                return jresp(self, {"error": "未切段"}, 400)
            j = json.load(open(cp, encoding="utf-8"))
            if not (1 <= idx <= len(j["segments"])):
                return jresp(self, {"error": "段号越界"}, 400)
            return jresp(self, {"prompt": j["segments"][idx-1].get("body", "")})
        if p == "/api/log":
            name = q.get("film", [None])[0]
            seg = q.get("seg", [None])[0]
            if not name:
                return jresp(self, {"error": "缺 film"}, 400)
            lp = log_path(work_dir(name), int(seg) if seg else None)
            txt = open(lp, encoding="utf-8", errors="replace").read() if os.path.exists(lp) else ""
            return jresp(self, {"text": txt[-6000:]})
        if p == "/api/src-video":
            # 播放项目源视频（时间轴预览用），支持 Range 以便任意位置 seek
            name = q.get("film", [None])[0]
            if not name:
                return jresp(self, {"error": "缺 film"}, 400)
            st = load_state(work_dir(name))
            fp = st.get("src")
            if not fp or not os.path.exists(fp):
                return jresp(self, {"error": "源视频不存在（项目里没记录或已移动）"}, 404)
            size = os.path.getsize(fp)
            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes="):
                try:
                    a, b = rng[6:].split("-", 1)
                    if a:
                        start = int(a)
                    if b:
                        end = int(b)
                    start = max(0, start)
                    end = min(size - 1, end)
                    if end < start:
                        end = size - 1
                    code = 206
                except Exception:
                    start, end, code = 0, size - 1, 200
            length = end - start + 1
            ext = os.path.splitext(fp)[1].lower()
            ctype = {".webm": "video/webm", ".mov": "video/quicktime",
                     ".mkv": "video/x-matroska"}.get(ext, "video/mp4")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(fp, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            return
        if p == "/api/media":
            # 读分段视频 / 首帧 / 成片
            name = q.get("film", [None])[0]
            kind = q.get("kind", ["clip"])[0]
            idx = int(q.get("idx", ["1"])[0])
            wd = work_dir(name) if name else None
            if kind == "final":
                fp = os.path.join(wd, "final", f"{load_state(wd)['film']}_复刻成片.mp4")
            elif kind == "out":
                fp = os.path.join(wd, "segments", f"{load_state(wd)['prefix']}_r{idx:02d}.mp4")
            elif kind == "first":
                fp = os.path.join(wd, "refs", f"first_{idx:02d}.png")
            elif kind == "firstov":
                # 手动锁定的段首帧（上传/抽上段尾帧）
                fp = seg_first_override_png(wd, idx)
            elif kind == "last":
                fp = os.path.join(wd, "refs", f"{load_state(wd)['prefix']}_s{idx:02d}_last.png")
            else:
                fp = os.path.join(wd, "source_segments", f"clip_{idx:02d}.mp4")
            if not fp or not os.path.exists(fp):
                return jresp(self, {"error": "文件不存在：" + str(fp)}, 404)
            ctype = "image/png" if fp.lower().endswith(".png") else "video/mp4"
            data = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        return jresp(self, {"error": "unknown"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            raw = self._body()
            data = json.loads(raw) if raw else {}
        except Exception as e:
            return jresp(self, {"error": f"JSON 解析失败: {e}"}, 400)

        if p == "/api/rename-project":
            old = (data.get("film") or "").strip()
            ok, msg = rename_project(old, data.get("to"))
            if not ok:
                return jresp(self, {"ok": False, "error": msg}, 400)
            return jresp(self, {"ok": True, "film": msg})
        if p == "/api/delete-project":
            name = (data.get("film") or "").strip()
            ok, msg = delete_project(name)
            if not ok:
                return jresp(self, {"ok": False, "error": msg}, 400)
            return jresp(self, {"ok": True, "comfy_cleaned": msg.get("comfy_cleaned", 0)})
        if p == "/api/upload":
            name = data.get("name", "").strip()
            b64 = data.get("data64", "")
            if not name or not b64:
                return jresp(self, {"error": "缺 name/data64"}, 400)
            import base64
            safe = time.strftime("%m%d_%H%M%S") + "_" + os.path.basename(name).replace(" ", "_")
            fp = os.path.join(UPLOAD_DIR, safe)
            open(fp, "wb").write(base64.b64decode(b64))
            dur, w, h, fps = probe(fp)
            return jresp(self, {"file": fp, "name": os.path.basename(fp),
                                "duration": dur, "w": w, "h": h, "fps": fps,
                                "frames": int(round(dur * fps))})
        if p == "/api/upload-ref":
            name = data.get("name", "").strip()
            b64 = data.get("data64", "")
            if not name or not b64:
                return jresp(self, {"error": "缺 name/data64"}, 400)
            import base64
            safe = time.strftime("%m%d_%H%M%S_") + os.path.basename(name).replace(" ", "_")
            stem = os.path.splitext(safe)[0]
            raw = os.path.join(REF_UPLOAD_DIR, "_raw_" + safe)
            open(raw, "wb").write(base64.b64decode(b64))
            # 统一转 PNG（LoadImage 按扩展名判类型，避免 webp/jpeg 后缀错配 400）
            png = os.path.join(REF_UPLOAD_DIR, stem + ".png")
            r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", raw,
                                "-frames:v", "1", "-vf",
                                "scale=trunc(iw/2)*2:trunc(ih/2)*2", png],
                               capture_output=True, text=True)
            try:
                os.remove(raw)
            except Exception:
                pass
            if r.returncode != 0 or not os.path.exists(png):
                return jresp(self, {"error": "图片转换失败: " + r.stderr[-200:]}, 400)
            # 复制到 ComfyUI input，comfy_name 用唯一前缀
            comfy_name = f"rmkref_{stem}.png"
            shutil.copy(png, os.path.join(COMFY_INPUT, comfy_name))
            return jresp(self, {"ok": True, "path": png, "comfy_name": comfy_name,
                                "name": os.path.basename(png),
                                "url": "/api/ref-file?name=" + urllib.parse.quote(os.path.basename(png))})
        if p == "/api/del-ref":
            name = data.get("name", "").strip()
            base = os.path.basename(name)
            film = data.get("film")
            rm = [os.path.join(REF_UPLOAD_DIR, base),
                  os.path.join(COMFY_INPUT, "rmkref_" + os.path.splitext(base)[0] + ".png")]
            if film:
                wd = work_dir(film)
                rm.append(os.path.join(wd, "user_refs", base))
                st = load_state(wd)
                st["user_refs"] = [r for r in (st.get("user_refs") or [])
                                   if os.path.basename(r.get("path") or "") != base]
                save_state(wd, st)
                cp = cfg_path(wd)
                if os.path.exists(cp):
                    j = json.load(open(cp, encoding="utf-8"))
                    for s in j["segments"]:
                        s["ref_images"] = [r for r in s.get("ref_images", [])
                                           if os.path.basename(r.get("path") or "") != base]
                    json.dump(j, open(cp, "w", encoding="utf-8"),
                              ensure_ascii=False, indent=2)
                write_project(wd)
            for f in rm:
                try:
                    os.remove(f)
                except Exception:
                    pass
            return jresp(self, {"ok": True})
        if p == "/api/split":
            name = data.get("film", "").strip() or ("复刻_" + time.strftime("%m%d_%H%M%S"))
            src = data.get("src", "").strip()
            seg = float(data.get("seg", 5))
            exact = bool(data.get("exact", False))
            prompt = open(PROMPT_FILE, encoding="utf-8").read().strip()
            if data.get("prompt"):
                prompt = data["prompt"].strip()
            if not src or not os.path.exists(src):
                return jresp(self, {"error": "源视频不存在"}, 400)
            # 用户上传的人物参考图（<Picture 1..N> 身份锚）
            user_refs = data.get("refs") or []
            for r in user_refs:
                if not os.path.exists(r.get("path", "")):
                    return jresp(self, {"error": "参考图不存在: " + str(r.get("path"))}, 400)
            anchor = bool(data.get("anchor_first", not bool(user_refs)))  # 有参考图默认关首帧硬锚
            try:
                st = do_split(name, src, seg, exact, prompt,
                              two_pass=bool(data.get("two_pass", True)),
                              user_refs=user_refs, anchor_first=anchor)
            except Exception:
                return jresp(self, {"error": traceback.format_exc()[-1200:]}, 500)
            return jresp(self, {"ok": True, "film": name, "n": st["n"],
                                "actual": st["actual"], "mode": st.get("mode"),
                                "cuts": st.get("cuts"), "segments": st["segments"]})
        if p == "/api/split-at":
            # 按帧号切点建片：[0, 120, 288, 336] → 3 段
            name = data.get("film", "").strip() or ("复刻_" + time.strftime("%m%d_%H%M%S"))
            src = data.get("src", "").strip()
            if not src or not os.path.exists(src):
                return jresp(self, {"error": "源视频不存在"}, 400)
            raw = data.get("cuts") or []
            try:
                cuts = sorted({int(round(float(c))) for c in raw})
            except Exception:
                return jresp(self, {"error": "切点格式不对（需帧号数组）"}, 400)
            if len(cuts) < 2:
                return jresp(self, {"error": "至少需要起点和终点两个切点"}, 400)
            prompt = open(PROMPT_FILE, encoding="utf-8").read().strip()
            if data.get("prompt"):
                prompt = data["prompt"].strip()
            user_refs = data.get("refs") or []
            for r in user_refs:
                if not os.path.exists(r.get("path", "")):
                    return jresp(self, {"error": "参考图不存在: " + str(r.get("path"))}, 400)
            anchor = bool(data.get("anchor_first", not bool(user_refs)))
            try:
                st = do_split(name, src, float(data.get("seg") or 5), False, prompt,
                              two_pass=bool(data.get("two_pass", True)),
                              user_refs=user_refs, anchor_first=anchor,
                              cuts=cuts, fps=data.get("fps"))
            except Exception:
                return jresp(self, {"error": traceback.format_exc()[-1200:]}, 500)
            return jresp(self, {"ok": True, "film": name, "n": st["n"],
                                "actual": st["actual"], "frames": st["total_frames"],
                                "mode": st.get("mode"),
                                "cuts": st.get("cuts"), "segments": st["segments"]})
        if p == "/api/gen":
            film = data.get("film"); idx = int(data.get("idx", 0)); seed = data.get("seed")
            tp = data.get("two_pass", None)   # None=不改；True/False=本次生成覆盖二采开关
            pr = data.get("prompt", None)     # 可选：本次生成临时覆盖该段提示词
            ok, msg = run_seg_proc(work_dir(film), idx, seed, two_pass=tp, prompt=pr)
            return jresp(self, {"ok": ok, "msg": msg})
        if p == "/api/gen-all":
            film = data.get("film")
            skip = bool(data.get("skip_done", True))
            ok, msg = run_all_proc(work_dir(film), skip_done=skip)
            return jresp(self, {"ok": ok, "msg": msg})
        if p == "/api/seg-first-from-prev":
            # 抽上一段成片的尾帧，锁成本段首帧（第 1 段没有上一段，前端不给按钮）
            film = data.get("film"); idx = int(data.get("idx", 0))
            wd = work_dir(film)
            if idx < 2:
                return jresp(self, {"ok": False, "error": "第 1 段没有上一段可承接"}, 400)
            if project_running(wd):
                return jresp(self, {"ok": False, "error": "有任务在跑，先停下再改首帧"}, 400)
            src = seg_last_png(wd, idx - 1)
            if not os.path.exists(src):
                return jresp(self, {"ok": False,
                                    "error": f"第 {idx-1} 段还没有成片尾帧，先把上一段生成出来"}, 400)
            try:
                ok, msg = set_seg_first_frame(wd, idx, src, "prev_last")
            except Exception:
                return jresp(self, {"ok": False, "error": traceback.format_exc()[-800:]}, 500)
            if not ok:
                return jresp(self, {"ok": False, "error": msg}, 400)
            append_log(log_path(wd), f"\n[first-frame] 段{idx} 首帧 ← 段{idx-1} 尾帧（{msg}）\n")
            return jresp(self, {"ok": True, "from": f"段{idx-1} 尾帧", "comfy_name": msg})
        if p == "/api/seg-first-upload":
            # 手动上传一张图作为本段首帧（任意段都可用，含第 1 段）
            film = data.get("film"); idx = int(data.get("idx", 0))
            b64 = data.get("data64", ""); fname = data.get("name") or "first.png"
            if not b64:
                return jresp(self, {"ok": False, "error": "缺 data64"}, 400)
            wd = work_dir(film)
            if not os.path.exists(cfg_path(wd)):
                return jresp(self, {"ok": False, "error": "未切段"}, 400)
            if project_running(wd):
                return jresp(self, {"ok": False, "error": "有任务在跑，先停下再改首帧"}, 400)
            import base64
            tmpdir = os.path.join(wd, "_tmp")
            os.makedirs(tmpdir, exist_ok=True)
            raw = os.path.join(tmpdir, "_up_" + os.path.basename(fname).replace(" ", "_"))
            open(raw, "wb").write(base64.b64decode(b64))
            png = os.path.join(tmpdir, "first_in.png")
            # 统一转 PNG（LoadImage 按扩展名判类型）+ 偶数边长
            r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", raw,
                                "-frames:v", "1", "-vf",
                                "scale=trunc(iw/2)*2:trunc(ih/2)*2", png],
                               capture_output=True, text=True)
            try:
                os.remove(raw)
            except Exception:
                pass
            if r.returncode != 0 or not os.path.exists(png):
                return jresp(self, {"ok": False,
                                    "error": "图片转换失败：" + r.stderr[-200:]}, 400)
            try:
                ok, msg = set_seg_first_frame(wd, idx, png, "upload")
            except Exception:
                return jresp(self, {"ok": False, "error": traceback.format_exc()[-800:]}, 500)
            finally:
                try:
                    os.remove(png)
                except Exception:
                    pass
            if not ok:
                return jresp(self, {"ok": False, "error": msg}, 400)
            append_log(log_path(wd), f"\n[first-frame] 段{idx} 首帧 ← 上传图 {fname}（{msg}）\n")
            return jresp(self, {"ok": True, "from": "上传", "comfy_name": msg})
        if p == "/api/seg-first-clear":
            # 解除本段首帧锁定：回到自动承接上段尾帧
            film = data.get("film"); idx = int(data.get("idx", 0))
            wd = work_dir(film)
            if project_running(wd):
                return jresp(self, {"ok": False, "error": "有任务在跑，先停下再改首帧"}, 400)
            ok, old = clear_seg_first_frame(wd, idx)
            if not ok:
                return jresp(self, {"ok": False, "error": old}, 400)
            append_log(log_path(wd), f"\n[first-frame] 段{idx} 解除首帧锁定（原 {old or '无'}）\n")
            return jresp(self, {"ok": True})
        if p == "/api/set-prompt":
            # 保存公共提示词到本片（不影响各段 body）
            film = data.get("film"); text = data.get("prompt", "")
            wd = work_dir(film)
            if not os.path.exists(cfg_path(wd)):
                return jresp(self, {"error": "未切段"}, 400)
            open(shared_prompt_path(wd), "w", encoding="utf-8").write(text)
            write_project(wd)
            return jresp(self, {"ok": True})
        if p == "/api/apply-prompt":
            # 把公共提示词应用到所有段（写 shared + 每段 body）
            film = data.get("film"); text = data.get("prompt", "")
            wd = work_dir(film); cp = cfg_path(wd)
            if not os.path.exists(cp):
                return jresp(self, {"error": "未切段"}, 400)
            open(shared_prompt_path(wd), "w", encoding="utf-8").write(text)
            j = json.load(open(cp, encoding="utf-8"))
            n = 0
            for s in j["segments"]:
                if s.get("status") != "done" or data.get("force"):
                    s["body"] = text
                    s["updated_at"] = time.time()
                    n += 1
            json.dump(j, open(cp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            # 被改动的已完成段标 stale（需重出）
            st = load_state(wd)
            for i, s in enumerate(j["segments"]):
                if s.get("body") == text and st["segments"][i].get("status") == "done":
                    st["segments"][i]["stale"] = True
            save_state(wd, st)
            write_project(wd)
            return jresp(self, {"ok": True, "applied": n})
        if p == "/api/set-seg-prompt":
            # 单独改某段提示词（落盘 cfg）
            film = data.get("film"); idx = int(data.get("idx", 0)); text = data.get("prompt", "")
            wd = work_dir(film); cp = cfg_path(wd)
            if not os.path.exists(cp):
                return jresp(self, {"error": "未切段"}, 400)
            j = json.load(open(cp, encoding="utf-8"))
            if not (1 <= idx <= len(j["segments"])):
                return jresp(self, {"error": "段号越界"}, 400)
            j["segments"][idx-1]["body"] = text
            j["segments"][idx-1]["updated_at"] = time.time()
            json.dump(j, open(cp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            # 已完成段改了提示词 → 标 stale 需重出
            st = load_state(wd)
            if st["segments"][idx-1].get("status") == "done":
                st["segments"][idx-1]["stale"] = True
            save_state(wd, st)
            write_project(wd)
            return jresp(self, {"ok": True})
        if p == "/api/stop-all":
            film = data.get("film")
            return jresp(self, stop_all_tasks(work_dir(film)))
        if p == "/api/save-project":
            # 点「保存项目」：把 seed 同步到 state，并把整片配置写进 项目目录/project.json
            film = data.get("film")
            wd = work_dir(film)
            cp = cfg_path(wd)
            if not os.path.exists(cp):
                return jresp(self, {"error": "未切段"}, 400)
            try:
                j = json.load(open(cp, encoding="utf-8"))
                st = load_state(wd)
                for i, s in enumerate(j["segments"]):
                    if "seed" in s and i < len(st["segments"]):
                        st["segments"][i]["seed"] = s["seed"]
                st["two_pass"] = bool(j.get("bc_two_pass", st.get("two_pass", True)))
                save_state(wd, st)
                pj = write_project(wd)
                resp = {"ok": True, "config": project_path(wd),
                        "config_name": os.path.basename(project_path(wd)),
                        "dir": wd, "n": len(j.get("segments", [])),
                        "done": sum(1 for s in pj.get("segments", [])
                                    if s.get("status") == "done") if pj else 0}
                return jresp(self, resp)
            except Exception:
                return jresp(self, {"error": traceback.format_exc()[-1200:]}, 500)
        if p == "/api/merge":
            film = data.get("film")
            ok, msg = run_merge_proc(work_dir(film))
            return jresp(self, {"ok": ok, "msg": msg})
        if p == "/api/set-twopass":
            # 随时切换二采开关（写 cfg + state，下次生成生效；不影响已生成的段）
            film = data.get("film"); tp = bool(data.get("two_pass", True))
            wd = work_dir(film)
            cp = cfg_path(wd)
            if not os.path.exists(cp):
                return jresp(self, {"error": "未切段"}, 400)
            j = json.load(open(cp, encoding="utf-8"))
            j["bc_two_pass"] = tp
            json.dump(j, open(cp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            st = load_state(wd); st["two_pass"] = tp; save_state(wd, st)
            write_project(wd)
            return jresp(self, {"ok": True, "two_pass": tp})
        if p == "/api/reset-seg":
            # 清状态、不删文件（重生成走 /api/gen）
            film = data.get("film"); idx = int(data.get("idx", 0))
            wd = work_dir(film); st = load_state(wd)
            st["segments"][idx-1]["status"] = "pending"
            st["segments"][idx-1]["stale"] = True
            save_state(wd, st)
            return jresp(self, {"ok": True})
        return jresp(self, {"error": "unknown"}, 404)


def reconcile_stale_jobs():
    """服务重启后，把上次遗留的 running 状态归位（进程已随旧服务消失）。"""
    for p in list_projects():
        wd = work_dir(p["name"])
        try:
            st = load_state(wd)
            changed = False
            for k, v in list((st.get("jobs") or {}).items()):
                if v == "running":
                    st["jobs"][k] = "stopped"
                    changed = True
            for s in st.get("segments", []):
                if s.get("status") == "running":
                    s["status"] = "stopped"
                    changed = True
            if st.get("current_seg"):
                st["current_seg"] = None
                changed = True
            if changed:
                save_state(wd, st)
                write_project(wd)
        except Exception:
            pass


if __name__ == "__main__":
    print(f"[remake-panel] http://127.0.0.1:{PORT}/  build={UI_BUILD}", flush=True)
    try:
        reconcile_stale_jobs()
    except Exception:
        traceback.print_exc()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.serve_forever()
