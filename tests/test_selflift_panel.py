"""SelfLift 逐段人在回路的静态回归测试（不需要 ComfyUI 服务在运行）。

覆盖：
- 后端 7 个路由能注册、路径白名单能挡穿越
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
        self.assertEqual(len(urls), 7, f"路由数不对：{routes.paths}")
        for suffix in ("status", "set", "run", "merge", "init", "upload", "media"):
            self.assertIn(f"/h3-long-video/selflift/{suffix}", urls)

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


if __name__ == "__main__":
    unittest.main()
