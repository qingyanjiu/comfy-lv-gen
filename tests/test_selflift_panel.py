"""SelfLift 逐段人在回路的静态回归测试（不需要 ComfyUI 服务在运行）。

覆盖：
- 后端 14 个路由能注册、路径白名单能挡穿越
- 下载的模板是合法 JSON、字段说明齐全、能被 import 流程直接消化
- director.py 有注册胶水、director.js 有第 5 个 tab 且默认打开
- **控件顺序没变**：这次改造没加 widget，22 项 widgets_values 不受影响
- 唯一驱动 run_selflift.py 提供 status / set 命令
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import selflift_panel as SP  # noqa: E402


class FakeRoutes:
    def __init__(self):
        self.paths = []

    def get(self, path):
        self.paths.append(("GET", path))
        return lambda fn: fn

    def post(self, path):
        self.paths.append(("POST", path))
        return lambda fn: fn


class FakeWeb:
    def json_response(self, *args, **kwargs):
        return ("json", args, kwargs)

    def FileResponse(self, *args, **kwargs):
        return ("file", args, kwargs)

    def Response(self, *args, **kwargs):
        return ("resp", args, kwargs)


class TestSelfLiftPanel(unittest.TestCase):
    def test_routes_registered(self):
        routes = FakeRoutes()
        SP.register_routes(routes, FakeWeb())
        urls = {path for _, path in routes.paths}
        self.assertEqual(len(urls), 14, f"路由数不对：{routes.paths}")
        for suffix in ("status", "set", "run", "merge", "init", "upload", "frame",
                       "media", "insert", "refimg", "import", "preview", "template"):
            self.assertIn(f"/h3-long-video/selflift/{suffix}", urls)
        self.assertIn("/h3-long-video/app", urls)

    def test_download_template_is_importable(self):
        """下载的模板必须是能直接导入的合法 JSON，且字段说明齐全。"""
        import selflift_schema as SCHEMA
        tpl = SCHEMA.build_template()
        json.dumps(tpl, ensure_ascii=False)          # 必须可序列化
        self.assertTrue(tpl["_字段说明"], "模板缺少 _字段说明")
        for key in ("seconds", "megapixels", "head", "tail", "segments[].body",
                    "segments[].link.mode", "segments[].link.transition"):
            self.assertIn(key, tpl["_字段说明"], f"字段说明缺 {key}")
        data = SCHEMA.normalize(tpl)                 # 必须能被导入流程消化
        self.assertEqual(len(data["segments"]), len(tpl["segments"]))
        self.assertTrue(data["segments"][1].get("link"), "模板里的 link 没被保留")
        self.assertTrue(data["segments"][1].get("head"), "段级 head 没被保留")
        self.assertTrue(data["segments"][0].get("seed"), "段级 seed 没被保留")

    def test_path_guard_rejects_traversal(self):
        for bad in ("C:/Windows/system32/config/SAM", "../../../etc/passwd", ""):
            with self.assertRaises(Exception):
                SP._safe(bad)

    def test_media_url_is_percent_encoded(self):
        url = SP._media_url("D:/Comfy-Desktop/output/片名/refs/首帧.png")
        self.assertTrue(url.startswith("/h3-long-video/selflift/media?path="))
        self.assertNotIn("片名", url)

    def test_director_glue_present(self):
        text = (ROOT / "director.py").read_text(encoding="utf-8")
        self.assertIn("selflift_panel.register_routes", text)
        self.assertIn("注册失败也不能拖垮节点加载", text)

    def test_js_has_default_selflift_tab(self):
        text = (ROOT / "web" / "director.js").read_text(encoding="utf-8")
        self.assertIn('label: "SelfLift 逐段"', text)
        self.assertIn('node.properties?.h3_tab || "selflift"', text)
        self.assertIn("this.h3DirectorUI?.selflift?.stop?.()", text)
        self.assertIn("/h3-long-video/selflift/run", text)
        self.assertIn("/h3-long-video/selflift/upload", text)
        self.assertIn("/h3-long-video/selflift/frame", text)
        self.assertIn("替换尾帧→下段首帧", text)

    def test_widget_count_unchanged(self):
        """这次改造没加 widget：工作流里 Director 节点必须仍是 22 项。"""
        wf = json.loads((ROOT / "example_workflows" / "H3-Long-Video-Director.json")
                        .read_text(encoding="utf-8"))
        node = next(n for n in wf["nodes"] if n["type"] == "H3LongVideoDirector")
        self.assertEqual(len(node["widgets_values"]), 22)

    def test_driver_has_status_and_set(self):
        text = Path(SP.DRIVER).read_text(encoding="utf-8")
        self.assertIn('"status"', text)
        self.assertIn('"set"', text)
        self.assertIn("def seg_status(cfg)", text)
        self.assertIn("first_frame_override", text)
        self.assertIn("last_frame_override", text)
        self.assertIn("N_LAST = 153", text)
        # per-link 链接模式 + 插入分段
        self.assertIn("def cmd_insert(cfg, after)", text)
        self.assertIn("--link-mode", text)
        self.assertIn("--link-transition", text)
        self.assertIn("def _gap_transition(cfg, seg, override=None)", text)

    def _mk_project(self, tmp, segments):
        import json as _json
        t = Path(tmp)
        (t / "segments").mkdir()
        (t / "refs").mkdir()
        (t / "qa").mkdir()
        cfg = {"title": "t", "prefix": "px", "final_name": "t_成片", "segments": segments}
        (t / "segments.json").write_text(_json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return t, cfg

    def test_insert_renumbers_and_renames(self):
        """在两段中间插入新段：重编号 s01/s02/s03，旧产物文件同步改名。"""
        import json as _json
        import tempfile

        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        with tempfile.TemporaryDirectory() as tmp:
            t, _ = self._mk_project(tmp, [
                {"id": "s01", "index": 0, "seed": 1, "body": "A"},
                {"id": "s02", "index": 1, "seed": 2, "body": "B"},
            ])
            (t / "segments" / "px_s01.mp4").write_bytes(b"1")
            (t / "segments" / "px_s02.mp4").write_bytes(b"2")
            (t / "refs" / "px_s01_last.png").write_bytes(b"L1")
            (t / "refs" / "px_s02_last.png").write_bytes(b"L2")
            R.set_root(str(t / "segments.json"))
            c = R.load_cfg()
            R.cmd_insert(c, 1)  # 在第 1 段后插入
            c2 = _json.loads((t / "segments.json").read_text(encoding="utf-8"))
            self.assertEqual([s["id"] for s in c2["segments"]], ["s01", "s02", "s03"])
            self.assertEqual(c2["segments"][1]["title"], "插入分段")
            self.assertEqual((c2["segments"][1]["link"] or {}).get("mode"), "chain")
            # 旧 s02 → s03（视频与尾帧都改名），新 s02 没有产物
            self.assertTrue((t / "segments" / "px_s03.mp4").exists())
            self.assertFalse((t / "segments" / "px_s02.mp4").exists())
            self.assertTrue((t / "refs" / "px_s03_last.png").exists())
            self.assertFalse((t / "refs" / "px_s02_last.png").exists())

    def test_transition_link_not_superseded(self):
        """转场模式的段，上游尾帧变新不应被判 stale（链式模式才向后传染）。"""
        import json as _json
        import os
        import tempfile
        import time

        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        with tempfile.TemporaryDirectory() as tmp:
            t, cfg = self._mk_project(tmp, [
                {"id": "s01", "index": 0, "seed": 1},
                {"id": "s02", "index": 1, "seed": 2,
                 "link": {"mode": "transition", "transition": "dissolve"}},
            ])
            v1 = t / "segments" / "px_s01.mp4"
            v2 = t / "segments" / "px_s02.mp4"
            v1.write_bytes(b"1")
            v2.write_bytes(b"2")
            (t / "refs" / "px_s01_first.png").write_bytes(b"F1")
            (t / "refs" / "px_s02_first.png").write_bytes(b"F2")
            l1 = t / "refs" / "px_s01_last.png"
            l1.write_bytes(b"L1")
            now = time.time()
            os.utime(v1, (now - 200, now - 200))
            os.utime(v2, (now - 100, now - 100))
            os.utime(l1, (now, now))  # seg1 尾帧比 seg2 视频新
            R.set_root(str(t / "segments.json"))
            st = R.seg_status(R.load_cfg())
            self.assertFalse(st["segments"][1]["stale"], "转场模式不应被上游尾帧判 stale")
            # 同一份数据改成链式，则应判 stale
            cfg["segments"][1]["link"] = {"mode": "chain", "transition": "dissolve"}
            (t / "segments.json").write_text(_json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            st2 = R.seg_status(R.load_cfg())
            self.assertTrue(st2["segments"][1]["stale"], "链式模式应被上游尾帧判 stale")

    def test_last_frame_reaches_api_graph(self):
        """段级 last_frame_override 必须真的接到 238 节点的 last_frame 输入。"""
        import json as _json
        import tempfile

        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        R.set_root(str(SP.TEMPLATE))
        cfg = R.load_cfg()
        cfg["segments"][0]["last_frame_override"] = "my_tail.png"
        with tempfile.TemporaryDirectory() as tmp:
            wf_path = Path(tmp) / "wf.json"
            R.build_wf(cfg, cfg["segments"][0], str(wf_path))
            prompt = R.to_api(_json.loads(wf_path.read_text(encoding="utf-8")))
        self.assertEqual(prompt["238"]["inputs"]["last_frame"], ["153", 0])
        self.assertIn("153", prompt)
        # 默认不接：去掉 override 后不能残留 last_frame
        del cfg["segments"][0]["last_frame_override"]
        with tempfile.TemporaryDirectory() as tmp:
            wf_path = Path(tmp) / "wf.json"
            R.build_wf(cfg, cfg["segments"][0], str(wf_path))
            prompt = R.to_api(_json.loads(wf_path.read_text(encoding="utf-8")))
        self.assertNotIn("last_frame", prompt["238"]["inputs"])


if __name__ == "__main__":
    unittest.main()
