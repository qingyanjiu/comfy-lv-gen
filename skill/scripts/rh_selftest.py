"""rh_backend 的本地假服务器自测：不碰真 RunningHub，验证 multipart / 组装 / 轮询 / 下载。"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SK = r"D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/scripts"
sys.path.insert(0, SK)
import rh_backend as RH  # noqa: E402

from PIL import Image  # noqa: E402

TMP = os.path.join(os.environ["TEMP"], "rh-selftest")
os.makedirs(TMP, exist_ok=True)
MP4 = os.path.join(TMP, "fake.mp4")
with open(MP4, "wb") as f:
    f.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4096)

seen = {"create": None, "uploads": [], "poll": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path == "/task/openapi/upload":
            assert b'name="apiKey"' in raw, "multipart 缺 apiKey"
            assert b'name="fileType"' in raw, "multipart 缺 fileType"
            assert b'name="file"; filename=' in raw, "multipart 缺 file 段"
            assert raw.endswith(b"--\r\n"), "multipart 结尾不完整"
            seen["uploads"].append(len(raw))
            return self._json({"code": 0, "msg": "success",
                               "data": {"fileName": "api/up.png", "fileType": "IMAGE"}})
        if self.path == "/task/openapi/create":
            seen["create"] = json.loads(raw)
            return self._json({"code": 0, "msg": "success",
                               "data": {"taskId": "T-123"}})
        if self.path == "/task/openapi/outputs":
            seen["poll"] += 1
            if seen["poll"] == 1:
                return self._json({"code": 813, "msg": "queued"})
            if seen["poll"] == 2:
                return self._json({"code": 804, "msg": "running"})
            return self._json({"code": 0, "msg": "success", "data": [
                {"fileUrl": f"http://127.0.0.1:{PORT}/f.mp4", "fileType": "mp4", "nodeId": "9"}]})
        self._json({"code": 404, "msg": "no route"}, 404)

    def do_GET(self):
        if self.path == "/f.mp4":
            with open(MP4, "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        else:
            self.send_response(404)
            self.end_headers()


srv = HTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

first = os.path.join(TMP, "s01_first.png")
last = os.path.join(TMP, "s01_last.png")
ref = os.path.join(TMP, "ref.png")
for p in (first, last, ref):
    Image.new("RGB", (32, 56), (200, 180, 160)).save(p)

sb = {
    "title": "自测",
    "final_size": [720, 1280],
    "megapixels": 0.3,
    "reference_images": [ref],
    "video_backend": "runninghub",
    "segments": [{"id": 1, "seconds": 5, "open_action": "站", "end_action": "坐",
                  "motion": "站起坐下", "shot": "全身", "prompt_video": "自测提示词"}],
    "runninghub": {
        "host": f"http://127.0.0.1:{PORT}",
        "api_key": "sk-test",
        "workflow_id": "1904136902449209346",
        "instance_type": "plus",
        "poll_interval": 0.2,
        "timeout": 30,
        "node_map": {
            "first": ["101", "image"],
            "last": ["102", "image"],
            "ref": ["111", "image"],
            "prompt": {"node": "200", "field": "prompt"},
            "seconds": ["200", "seconds", "int"],
            "seed": ["200", "seed", "int"],
        },
    },
}
rec = {"first": first, "last": last}

out, task = RH.gen(sb, sb["segments"][0], rec, 500101, os.path.join(TMP, "out"))
assert task == "T-123", task
assert os.path.exists(out) and os.path.getsize(out) > 1024, out

req = seen["create"]
assert req["workflowId"] == "1904136902449209346", req
assert req["instanceType"] == "plus"
assert len(seen["uploads"]) == 3, f"应该传 3 张图，实际 {len(seen['uploads'])}"
nl = {f"{i['nodeId']}.{i['fieldName']}": i["fieldValue"] for i in req["nodeInfoList"]}
assert nl["101.image"] == "api/up.png", nl
assert nl["200.prompt"] == "自测提示词", nl
assert nl["200.seconds"] == 5 and isinstance(nl["200.seconds"], int), nl
assert nl["200.seed"] == 500101, nl
print("覆盖字段:", json.dumps(req["nodeInfoList"], ensure_ascii=False))

print(f"\n✓ 提交/轮询/下载 全通：{len(seen['uploads'])} 张图 → taskId {task} → {os.path.basename(out)} "
      f"({os.path.getsize(out)} B)，轮询 {seen['poll']} 次")

# 缺配置要一次说全
for k, expect in (("api_key", "api_key"), ("workflow_id", "workflow_id"), ("node_map", "node_map")):
    bad = json.loads(json.dumps(sb))
    bad["runninghub"].pop(k)
    try:
        RH.check_conf(RH.conf(bad))
    except RH.RHError as e:
        assert expect in str(e), (k, str(e))
    else:
        raise AssertionError(f"缺 {k} 竟然没报错")
print("✓ 缺配置能一次报全")

# 错误码要带人话
try:
    RH.check_conf({})
except RH.RHError as e:
    print(f"✓ 空配置提示: {e}")
srv.shutdown()
