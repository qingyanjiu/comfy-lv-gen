#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方向契约：把"人物朝哪走 / 身体朝哪 / 脸朝哪 / 脚尖朝哪"变成可校验的结构化字段。

为什么需要它
------------
H3 只做首尾帧插值，它不会推理"这个人在往前走"。你喂给它一组自相矛盾的姿态，
它只能挑一个方向去插，结果就是倒着走、原地扭、或者走出画面再走回来。

实测事故（2026-10-31 段 1）：
  end_action 写"身体侧向画面左方"，但整段行进方向是画面右方；
  head 默认值"头部仍然朝向镜头"，把背对镜头的人强行掰回头。
  生成的首尾帧：脚尖朝画面右下、脸朝画面右、身体背对镜头 —— 三者互相差 90 度。
  H3 拿到这种帧，走的不是"向右"，而是"朝脸的方向倒退"。

坐标系（画面 2D，0 度 = 画面右方，逆时针为正）
    right = 0      left = 180
    away  = 90     朝向画面纵深（走远、背影）
    toward= 270    朝镜头走来
"""
import math
import os
import sys

# ---------------------------------------------------------------- 枚举
LEFT, RIGHT, AWAY, TOWARD, STILL = "left", "right", "away", "toward", "still"
DIRS = (LEFT, RIGHT, AWAY, TOWARD)

ANG = {RIGHT: 0.0, AWAY: 90.0, LEFT: 180.0, TOWARD: 270.0}
CN = {LEFT: "画面左方", RIGHT: "画面右方", AWAY: "画面纵深(走远)", TOWARD: "朝镜头"}

# 中文描述 → 方向。只收人物朝向语义，不收"马路在画面左方"这类环境描述。
KW = {
    LEFT: ["画面左方", "画面左侧", "朝左", "向左", "左侧面", "身体朝左", "脸朝左", "看向左", "左前方", "左后方"],
    RIGHT: ["画面右方", "画面右侧", "朝右", "向右", "右侧面", "身体朝右", "脸朝右", "看向右", "右前方", "右后方"],
    AWAY: ["纵深", "远处", "背影", "背对镜头", "向里", "走远", "背朝镜头", "身后的小路"],
    TOWARD: ["朝镜头", "面对镜头", "正对镜头", "走向镜头", "迎面", "镜头走来", "看镜头", "看向镜头"],
}
# 这些词说明画面里"人"是动作主体，用来筛掉环境句
SUBJ = ["人物", "身体", "上身", "脸", "头", "视线", "脚尖", "双脚", "脚掌", "侧身", "背部", "肩"]


# ---------------------------------------------------------------- 角度
def angle_between(a, b):
    """两个方向的最小夹角（0-180）。"""
    if a not in ANG or b not in ANG:
        return None
    d = abs(ANG[a] - ANG[b]) % 360
    return d if d <= 180 else 360 - d


def describe_gap(a, b):
    g = angle_between(a, b)
    if g is None:
        return "?"
    return f"{g:.0f}°"


# ---------------------------------------------------------------- 文字抽取
def _sentences(text):
    out = []
    for seg in str(text).replace("；", "。").replace("，", "。").split("。"):
        seg = seg.strip()
        if seg:
            out.append(seg)
    return out


def parse_dir(text):
    """从一句中文里抽出方向词。只在含人物主语的短句上生效，避免抓到背景。"""
    hits = {}
    for sent in _sentences(text):
        if not any(s in sent for s in SUBJ):
            continue
        for d, kws in KW.items():
            if any(k in sent for k in kws):
                hits.setdefault(d, []).append(sent)
    return hits


def infer_field_dirs(s):
    """把一段分镜里各字段的方向抽出来，按 body / face / toe 归类。"""
    body, face, toe = {}, {}, {}
    for key in ("open_action", "end_action", "motion"):
        v = s.get(key)
        if not v:
            continue
        for sent in _sentences(v):
            hits = parse_dir(sent)
            for d in hits:
                if any(k in sent for k in ("脚尖", "双脚", "脚掌", "迈", "步")):
                    toe.setdefault(d, []).append(sent)
                elif any(k in sent for k in ("脸", "头", "视线", "看")):
                    face.setdefault(d, []).append(sent)
                elif any(k in sent for k in ("身体", "上身", "侧身", "背部", "肩", "躯干")):
                    body.setdefault(d, []).append(sent)
    return body, face, toe


# ---------------------------------------------------------------- 契约
def get_dir(s):
    """取该段的方向契约。优先结构化字段，缺省则从文字推断（置信度低）。"""
    d = s.get("dir") or {}
    got = {k: d.get(k) for k in ("heading", "body", "face", "toe")}
    explicit = all(got.get(k) for k in ("heading", "body", "face"))
    if explicit:
        return got, True
    body, face, toe = infer_field_dirs(s)
    # 文字推断：一类方向只有一个候选才算数，多个说明自相矛盾，交给 check 报错
    def pick(m, fallback=None):
        return list(m)[0] if len(m) == 1 else fallback
    got["heading"] = got.get("heading") or pick(body) or pick(toe)
    got["body"] = got.get("body") or pick(body)
    got["face"] = got.get("face") or pick(face)
    got["toe"] = got.get("toe") or pick(toe) or got["heading"]
    return got, False


def check_segment(s, idx, walking=None):
    """返回 [(级别, 消息)]；级别 err / warn。"""
    out = []
    d, explicit = get_dir(s)
    head, body, face, toe = d.get("heading"), d.get("body"), d.get("face"), d.get("toe")
    txt = " ".join(str(s.get(k, "")) for k in ("open_action", "end_action", "motion"))
    walk = walking if walking is not None else ("走" in txt or "迈" in txt)

    tag = "错误" if explicit else "提醒"
    if not explicit:
        # 不缺字段的段不用每段都报，交给调用方汇总
        pass

    if head and body:
        g = angle_between(head, body)
        if g is not None and g > 90:
            out.append(("err" if explicit else "warn",
                        f"第 {idx} 段身体朝向 {CN.get(body, body)} 与行进方向 {CN.get(head, head)} 夹角 "
                        f"{g:.0f}°（>90°）：人是在倒着走。要么改 body，要么把 heading 改成 still"))
        elif g is not None and g > 45:
            out.append(("warn", f"第 {idx} 段身体朝向与行进方向差 {g:.0f}°，接近侧行，H3 容易走偏"))

    if head and toe and head != STILL:
        g = angle_between(head, toe)
        if g is not None and g > 45:
            out.append(("err" if explicit else "warn",
                        f"第 {idx} 段脚尖朝 {CN.get(toe, toe)}，但人要走 {CN.get(head, head)}（差 {g:.0f}°）："
                        f"脚尖必须踩在行进方向上，否则 H3 会顺着脚尖走"))

    if walk and head and face:
        g = angle_between(head, face)
        if g is not None and g > 90:
            out.append(("err" if explicit else "warn",
                        f"第 {idx} 段脸朝 {CN.get(face, face)}，与行进方向 {CN.get(head, head)} 差 {g:.0f}°（>90°）："
                        f"边走边回头，H3 会拆成两个方向，实测就是倒着走或原地扭。"
                        f"此段改成 heading=still 只转身不位移，或把 face 改回一致"))
        elif g is not None and g > 45:
            out.append(("warn", f"第 {idx} 段脸与行进方向差 {g:.0f}°，属半侧脸前进，风险中等"))

    # 同一字段里左右互斥
    bmap, fmap, tmap = infer_field_dirs(s)
    for name, m in (("身体", bmap), ("脸", fmap), ("脚尖", tmap)):
        sides = [x for x in m if x in (LEFT, RIGHT)]
        if len(sides) == 2:
            out.append(("warn", f"第 {idx} 段文字里{name}同时出现「画面左方」和「画面右方」，自相矛盾："
                                + "；".join(m[LEFT] + m[RIGHT])))

    if not explicit:
        # 只在推断出了方向、且存在明显分歧时才输出，避免每段刷屏
        set_d = {v for v in (head, body, face) if v}
        if len(set_d) > 1:
            out.append(("warn", f"第 {idx} 段文字推断出多个方向：行进={CN.get(head, head)} "
                                f"身体={CN.get(body, body)} 脸={CN.get(face, face)} 脚尖={CN.get(toe, toe)}"
                                f"（缺 dir 契约，靠文字猜，置信度低）"))
    return out


# ---------------------------------------------------------------- 提示词锁定句
# 生图模型不认「躯干朝向画面右方」这种元语言，只认可见的解剖特征。
# 下面把方向翻译成"哪只肩朝哪、哪只脚在前、鞋尖指哪"。
# 不要写「镜头看到她的左侧面」——模型会把它反转成「人朝左」（实测段 1 因此朝反）。
# 只用「多个部件同时指向同一方向」这种不带视角翻转的描述。
BODY_POSE = {
    RIGHT: "人物面朝画面右方侧身站立，鼻尖、下巴、胸口、肚脐和两只脚尖全部指向画面右边缘，"
           "身体与画面右边缘平行",
    LEFT: "人物面朝画面左方侧身站立，鼻尖、下巴、胸口、肚脐和两只脚尖全部指向画面左边缘，"
          "身体与画面左边缘平行",
    AWAY: "人物完全背对镜头站立，后背和后脑勺朝向观众，看不到胸口和五官",
    TOWARD: "人物正面朝向镜头站立，双肩平齐对镜头，胸口正对观众",
}
TOE_POSE = {
    RIGHT: "双脚前后错开，前脚在画面右方、后脚在画面左方，两只黑色皮鞋的鞋尖都指向画面右边缘",
    LEFT: "双脚前后错开，前脚在画面左方、后脚在画面右方，两只黑色皮鞋的鞋尖都指向画面左边缘",
    AWAY: "双脚朝向画面纵深处，只能看到鞋跟，鞋尖指向远离镜头的方向",
    TOWARD: "双脚朝镜头方向，两只鞋尖正对观众",
}
FACE_POSE = {
    RIGHT: "脸朝画面右方，鼻尖和下巴指向画面右边缘，呈四分之三侧脸",
    LEFT: "脸朝画面左方，鼻尖和下巴指向画面左边缘，呈四分之三侧脸",
    AWAY: "头部与身体同向，后脑勺对着镜头，看不到五官",
    TOWARD: "脸正对镜头，鼻尖和下巴正对观众",
}


def lock_line(s):
    """生成"方向锁定"提示词，直接拼进首帧/尾帧模板。

    以 heading 为准：body/toe 与 heading 冲突时，提示词一律按 heading 写，
    宁可锁成"能走的那一版"，也不要把矛盾原样喂给模型。
    """
    d, _ = get_dir(s)
    head, body, face, toe = d.get("heading"), d.get("body"), d.get("face"), d.get("toe")
    L = []

    # 兜底：与行进方向冲突的 body / toe 一律改成 heading
    if head and head != STILL:
        if body and angle_between(head, body) and angle_between(head, body) > 90:
            body = head
        if toe and angle_between(head, toe) and angle_between(head, toe) > 45:
            toe = head

    seated = (s.get("stance") or "").strip() == "seated"
    if body and body in BODY_POSE:
        body_line = BODY_POSE[body]
        if seated and body == "toward":
            body_line = "上身正对镜头，双肩平齐对观众"
        L.append(body_line + "，本段全程保持这个身位，不转正、不侧转")
    # 坐姿镜头：腿脚被桌子/画幅挡住，写脚尖反而是噪声
    if toe and toe in TOE_POSE and not s.get("closeup") and not seated:
        if head == STILL:
            L.append(f"双脚并拢站定、不挪位，两只鞋尖都朝向{CN.get(toe, toe)}")
        else:
            L.append(TOE_POSE[toe])
    if face and face in FACE_POSE:
        if face == body:
            L.append(FACE_POSE[face] + "，头与身体同向，不回头、不看镜头")
        elif body and angle_between(face, body) and angle_between(face, body) > 90:
            L.append(FACE_POSE[face] + "，只有脖子转动，肩线、胸腔和骨盆一律不跟着转")
        else:
            L.append(FACE_POSE[face] + "，头在颈根处转动，躯干不跟着转")
    if head and head != STILL:
        L.append(f"人物在画面中只朝{CN.get(head, head)}移动，不朝反方向")
    elif seated:
        L.append("本段人物保持坐姿，不站起、不走动，只在原位改变上半身和手臂姿态")
    else:
        L.append("本段人物不走动，双脚原地站定，只在原位改变上半身姿态")
    return "".join(x + "。" for x in L)


# ---------------------------------------------------------------- 画面侧识别
def face_yaw_signed(img_path):
    """有符号偏航：负=脸朝画面左，正=脸朝画面右，绝对值>0.8 视为接近侧脸/背面。

    依赖 h3-video-qa 的 qa_lib（同一套 insightface 环境）。
    """
    import numpy as np
    import cv2

    qa_dir = r"D:/Comfy-Desktop/.workbuddy/skills/h3-video-qa/scripts"
    if qa_dir not in sys.path:
        sys.path.insert(0, qa_dir)
    import qa_lib as Q

    img = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    app = Q.get_app()
    faces = app.get(img)
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    k = np.array(f.kps, dtype=float)
    eye_d = float(np.linalg.norm(k[0] - k[1])) + 1e-6
    yaw = float((k[2][0] - (k[0][0] + k[1][0]) / 2.0) / eye_d)
    return yaw


_POSE_SESSION = None


def _pose_session():
    """YOLOv8n-pose ONNX 会话（onnxruntime CPU，13MB，单图约 0.3s）。"""
    global _POSE_SESSION
    if _POSE_SESSION is None:
        import onnxruntime as ort
        _POSE_SESSION = ort.InferenceSession(
            r"D:/Comfy-Desktop/ComfyUI-Shared/models/detection/yolov8n-pose.onnx",
            providers=["CPUExecutionProvider"])
    return _POSE_SESSION


def body_keypoints(img_path):
    """单图人体关键点。返回 (kps[17,3]=x,y,conf 原图像素坐标, box[4]=x1y1x2y2)；无人体返回 (None, None)。"""
    import numpy as np
    import cv2

    img = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, None
    H, W = img.shape[:2]
    sq = cv2.resize(img, (640, 640))
    x = np.ascontiguousarray(sq[..., ::-1].transpose(2, 0, 1), dtype=np.float32) / 255.0
    out = _pose_session().run(None, {"input": x[None]})[0][0].T  # [8400, 56]

    def sigmoid(v):
        return 1.0 / (1.0 + np.exp(-v))

    score = sigmoid(out[:, 4])
    keep = score > 0.35
    if not keep.any():
        return None, None
    out, score = out[keep], score[keep]
    xywh, kpts = out[:, :4], out[:, 5:].reshape(-1, 17, 3)

    boxes = np.stack([xywh[:, 0] - xywh[:, 2] / 2, xywh[:, 1] - xywh[:, 3] / 2,
                      xywh[:, 0] + xywh[:, 2] / 2, xywh[:, 1] + xywh[:, 3] / 2], 1)
    # 简易 NMS：检测数通常 <20，逐个贪心即可
    order = score.argsort()[::-1]
    sel = []
    while order.size:
        i = order[0]
        sel.append(i)
        if order.size == 1:
            break
        a, b = boxes[i], boxes[order[1:]]
        ix1, iy1 = np.maximum(a[0], b[:, 0]), np.maximum(a[1], b[:, 1])
        ix2, iy2 = np.minimum(a[2], b[:, 2]), np.minimum(a[3], b[:, 3])
        inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
        areaA = (a[2] - a[0]) * (a[3] - a[1])
        areaB = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
        iou = inter / (areaA + areaB - inter + 1e-9)
        order = order[1:][iou < 0.5]
    best = sel[0]

    kps = kpts[best].copy()
    kps[:, 0] = sigmoid(kps[:, 0]) * 640 if kps[:, 0].max() <= 1.0 else kps[:, 0]
    kps[:, 2] = sigmoid(kps[:, 2])
    sx, sy = W / 640.0, H / 640.0
    kps[:, 0] *= sx
    kps[:, 1] *= sy
    box = boxes[best] * [sx, sy, sx, sy]
    return kps, box


def body_motion(first, last):
    """首尾帧的动作变化量——用 17 个身体关键点直接量姿态位移。

    H3 只做首帧到尾帧的插值：两帧几乎一样时它要么演成静止，要么自己乱编。
    转头门控只管头不许乱转，这个指标管身体必须有变化，两者必须同时成立。

    位移用躯干长度（肩中点到髋中点）归一化，与人物远近无关。
    返回 dict：
      disp    加权平均关键点位移（躯干长为单位）。<0.15 约等于两帧同姿态
      arm/leg 胳膊 / 腿部分别的平均位移（判断变化落在哪）
      max_kpt 位移最大的部位
      x_drift 人物水平漂移（按 720 宽归一）
      zoom    人物高度变化
    """
    ka, ba = body_keypoints(first)
    kb, bb = body_keypoints(last)
    if ka is None or kb is None:
        return None
    import numpy as np

    def torso(k):
        sh = (k[5][:2] + k[6][:2]) / 2
        hp = (k[11][:2] + k[12][:2]) / 2
        return float(np.linalg.norm(sh - hp)) + 1e-6

    scale = (torso(ka) + torso(kb)) / 2
    # COCO 索引：5/6 肩 7/8 肘 9/10 腕 11/12 髋 13/14 膝 15/16 踝
    groups = {"arm": list(range(7, 11)), "leg": list(range(13, 17)),
              "torso": [5, 6, 11, 12]}
    disp, all_d = {}, []
    w = np.minimum(ka[:, 2], kb[:, 2])
    for g, idx in groups.items():
        ds = [float(np.linalg.norm(ka[j][:2] - kb[j][:2]) / scale) for j in idx if w[j] > 0.3]
        disp[g] = float(np.mean(ds)) if ds else 0.0
        all_d += ds
    names = {5: "左肩", 6: "右肩", 7: "左肘", 8: "右肘", 9: "左腕", 10: "右腕",
             11: "左髋", 12: "右髋", 13: "左膝", 14: "右膝", 15: "左踝", 16: "右踝"}
    per_kpt = {j: float(np.linalg.norm(ka[j][:2] - kb[j][:2]) / scale)
               for j in range(5, 17) if w[j] > 0.3}
    top = max(per_kpt, key=per_kpt.get) if per_kpt else None
    return {
        "disp": float(np.mean(all_d)) if all_d else 0.0,
        "arm": disp["arm"], "leg": disp["leg"], "torso_shift": disp["torso"],
        "max_kpt": "{} {:.2f}".format(names.get(top, top), per_kpt.get(top, 0.0)),
        "x_drift": float(abs((ba[0] + ba[2]) - (bb[0] + bb[2])) / 2 / 720),
        "zoom": float((bb[3] - bb[1]) / max(1e-6, ba[3] - ba[1])),
    }


def face_pitch_proxy(img_path):
    """低头/抬头的粗代理：(鼻y - 双眼中点y) / 双眼间距。抬头时下巴上移、鼻眼距变短，值变小。

    返回 float；无人脸返回 None。
    """
    import numpy as np
    import cv2

    qa_dir = r"D:/Comfy-Desktop/.workbuddy/skills/h3-video-qa/scripts"
    if qa_dir not in sys.path:
        sys.path.insert(0, qa_dir)
    import qa_lib as Q

    img = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    faces = Q.get_app().get(img)
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    k = np.array(f.kps, dtype=float)
    eye_d = float(np.linalg.norm(k[0] - k[1])) + 1e-6
    return float((k[2][1] - (k[0][1] + k[1][1]) / 2) / eye_d)


def count_faces(img_path):
    """画面里的人脸数量。>1 意味着参考图泄漏（悬浮脸）或路人入镜。"""
    import numpy as np
    import cv2

    qa_dir = r"D:/Comfy-Desktop/.workbuddy/skills/h3-video-qa/scripts"
    if qa_dir not in sys.path:
        sys.path.insert(0, qa_dir)
    import qa_lib as Q

    img = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    return len(Q.get_app().get(img))


def yaw_matches(yaw, body):
    """实测 yaw 是否符合目标身位。away / toward 无法用 yaw 判，返回 None（跳过检查）。"""
    if yaw is None:
        return False
    if body == LEFT:
        return yaw < -0.25
    if body == RIGHT:
        return yaw > 0.25
    return None


def yaw_to_dir(yaw):
    if yaw is None:
        return None
    if abs(yaw) < 0.18:
        return TOWARD      # 正脸对着镜头
    return RIGHT if yaw > 0 else LEFT


def check_frame_pair(s, first, last):
    """首尾帧方向一致性（能自动判的部分）。返回 (level, msgs)。"""
    d, explicit = get_dir(s)
    msgs = []
    y1, y2 = face_yaw_signed(first), face_yaw_signed(last)
    b, f = d.get("body"), d.get("face")

    # 1) 解剖不可能：躯干与脸差 >100°，脖子转不了这么多，模型只能靠扭腰/扭脚实现
    if b and f:
        g = angle_between(b, f)
        if g is not None and g > 100:
            msgs.append(f"躯干朝 {CN.get(b, b)}、脸朝 {CN.get(f, f)}，相差 {g:.0f}°："
                        f"真实人体颈部上限约 90-100°，这个姿态只能靠扭腰扭脚凑，"
                        f"结果就是脚尖和脸各朝一边。把 face 改成与 body 相差 ≤90° 的方向")

    # 2) 首尾帧脸朝向左右翻转
    if y1 is not None and y2 is not None and y1 * y2 < 0 and min(abs(y1), abs(y2)) > 0.25:
        msgs.append(f"首尾帧脸朝向左右翻转（yaw {y1:+.2f} → {y2:+.2f}）：H3 会跟着扭头，产生转身或倒走")

    # 3) 段内头部大幅转动（>0.35 ≈ 30°），H3 容易把转头当成转身
    if y1 is not None and y2 is not None and abs(y2 - y1) > 0.35:
        msgs.append(f"段内脸水平转动幅度偏大（yaw {y1:+.2f} → {y2:+.2f}，约 {abs(y2-y1)*90:.0f}°）："
                    f"H3 可能把转头演成转身，建议缩小或改由手臂/躯干承担变化")

    # 4) 躯干背对镜头 + 脸接近正对镜头（|yaw|<0.2）
    if b == AWAY:
        for tag, y in (("首帧", y1), ("尾帧", y2)):
            if y is not None and abs(y) < 0.20:
                msgs.append(f"{tag}躯干背对镜头，脸却几乎正对镜头（yaw {y:+.2f}）："
                            f"俯拍正面脸+背面身体，H3 会按脸的方向把人翻过来。"
                            f"改成 3/4 侧回头（yaw 0.35-0.6）")

    info = (f"脸朝向 yaw 首/尾 = {y1:+.2f} / {y2:+.2f}（>0 朝画面右，<0 朝画面左）；脚尖朝向需目视确认"
            if (y1 is not None and y2 is not None) else "未识别到人脸，方向需目视确认")
    return ("err" if msgs else "ok"), msgs, info


if __name__ == "__main__":
    import json
    sb = json.load(open(sys.argv[1], encoding="utf-8"))
    for i, s in enumerate(sb["segments"], 1):
        for lv, m in check_segment(s, i):
            print(f"[{lv}] {m}")
        print(f"      锁定句: {lock_line(s)}")
