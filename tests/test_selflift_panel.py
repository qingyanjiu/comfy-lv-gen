"""SelfLift 逐段人在回路的静态回归测试（不需要 ComfyUI 服务在运行）。

覆盖：
- 后端 15 个路由能注册、路径白名单能挡穿越
- 下载的模板是合法 JSON、字段说明齐全、能被 import 流程直接消化
- director.py 有注册胶水、director.js 有第 5 个 tab 且默认打开
- **控件顺序没变**：这次改造没加 widget，22 项 widgets_values 不受影响
- 唯一驱动 run_selflift.py 提供 status / set 命令
- 逐段动作参考视频：插入时截取前 n 秒 + 提示词最前面注入，删除时干净剥除
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
        self.assertEqual(len(urls), 20, f"路由数不对：{routes.paths}")
        for suffix in ("status", "set", "run", "merge", "init", "upload", "frame",
                       "media", "insert", "refimg", "import", "preview", "template",
                       "refvideo", "rename", "films", "delete", "runall"):
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
        self.assertTrue(data["segments"][0].get("seed"), "段级 seed 没被保留")
        # 模板改用 {pic:} 占位符后，head 只写一遍，不再要求每段都有段级 head
        self.assertIn("{pic:", tpl["head"], "模板 head 应该用 {pic:N} 占位符")
        self.assertIn("picture", tpl["_规则说明"])

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

    def test_multi_person_reference_slots(self):
        """多人参考图接线：人物占 ref_image_0..N-1，空镜板永远排最后一位。

        这条是 2026-11-11 加多图支持时的口径，错了会静默降级（身份锚被换成场景板），
        strict_prompt_tags 也查不出来，所以必须钉死节点号。
        """
        import json as _json
        import tempfile

        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        R.set_root(str(SP.TEMPLATE))
        base = R.load_cfg()
        seg = dict(base["segments"][0])

        def graph_refs(cfg):
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "wf.json"
                R.build_wf(cfg, cfg["segments"][0], str(p))
                prompt = R.to_api(_json.loads(p.read_text(encoding="utf-8")))
            d = prompt["238"]["inputs"]
            return {k: v[0] for k, v in d.items() if k.startswith("ref_images.")}

        # 单人旧字段：人物 150，空镜板 152
        solo = dict(base, ref_image="D:/x/girl.png", comfy_ref_name="girl.png",
                    bg_plate="plate.png", segments=[seg])
        solo.pop("ref_images", None)
        self.assertEqual(graph_refs(solo), {
            "ref_images.ref_image_0": "150",
            "ref_images.ref_image_1": "152",
        })
        self.assertEqual([p["label"] for p in R.people_refs(solo)], ["人物 A"])

        # 三人 + 空镜板：人物 150/160/161，板排到 ref_image_3
        many = dict(solo)
        many.pop("ref_image")
        many.pop("comfy_ref_name")
        many["ref_images"] = [{"path": f"D:/x/p{i}.png", "comfy_name": f"p{i}.png"}
                              for i in range(3)]
        self.assertEqual(graph_refs(many), {
            "ref_images.ref_image_0": "150",
            "ref_images.ref_image_1": "160",
            "ref_images.ref_image_2": "161",
            "ref_images.ref_image_3": "152",
        })

        # 三人不带空镜板：只到 ref_image_2，不能残留 152
        many.pop("bg_plate")
        refs = graph_refs(many)
        self.assertNotIn("ref_images.ref_image_3", refs)
        self.assertNotIn("152", refs.values())

    def test_pic_token_expands_per_segment(self):
        """head 里的 {pic:N} 按本段实际接线换算成 <Picture N>，所以只写一遍就够。"""
        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        R.set_root(str(SP.TEMPLATE))
        cfg = R.load_cfg()
        cfg["ref_image"] = "D:/x/a.png"
        cfg["comfy_ref_name"] = "a.png"
        cfg["bg_plate"] = "p.png"
        cfg["head"] = "<Picture {pic:1}> 人物\n\n<Picture {pic:plate}> 空镜板"
        cfg["tail"] = ""
        s1 = {"body": "走到窗边", "seed": 1}                       # 第 1 段：无首帧
        s2 = {"body": "坐下", "seed": 2, "first_frame": "上段尾帧.png"}  # 链式段

        p1, p2 = R.seg_prompt(cfg, s1), R.seg_prompt(cfg, s2)
        self.assertIn("<Picture 1> 人物", p1)
        self.assertIn("<Picture 2> 空镜板", p1)
        self.assertIn("<Picture 2> 人物", p2)      # 首帧占了 P1，人物整体后移
        self.assertIn("<Picture 3> 空镜板", p2)
        self.assertNotIn("{pic:", p1 + p2)         # 占位符绝不能漏进 prompt

        # 老片子写死数字的 head 原样保留（向后兼容）
        cfg["head"] = "<Picture 1> 人物\n\n<Picture 2> 空镜板"
        self.assertIn("<Picture 1> 人物", R.seg_prompt(cfg, s1))

        # 可选占位符 {pic?:first}：本段没接首帧时整行消失，链式段才出现
        cfg["bg_plate"] = "p.png"
        cfg["head"] = ("<Picture {pic?:first}> 是本段首帧，必须无缝延续。\n\n"
                       "<Picture {pic:1}> 是人物参考")
        no_first = R.seg_prompt(cfg, {"body": "b", "seed": 1})
        chained = R.seg_prompt(cfg, {"body": "b", "seed": 2, "first_frame": "x.png"})
        self.assertNotIn("本段首帧", no_first)
        self.assertIn("<Picture 1> 是人物参考", no_first)
        self.assertIn("<Picture 1> 是本段首帧", chained)
        self.assertIn("<Picture 2> 是人物参考", chained)

        # 引用本段没接的媒体要直接报错，而不是留个错编号
        cfg["head"] = "<Picture {pic:plate}> 空镜板"
        cfg.pop("bg_plate")
        with self.assertRaises(SystemExit):
            R.seg_prompt(cfg, s1)

    def test_set_global_head_and_tail(self):
        """网页「全片提示词」保存：不传段号也能写，并打 head_updated_at 让全片变 stale。"""
        import argparse
        import json as _json
        import tempfile

        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "segments.json"
            cfg_path.write_text(_json.dumps({
                "title": "t", "prefix": "p", "head": "旧", "tail": "旧",
                "segments": [{"id": "s01", "seed": 7, "body": "b"}],
            }, ensure_ascii=False), encoding="utf-8")
            R.set_root(str(cfg_path))
            ns = argparse.Namespace(seg=0, join=None, seed=None, body=None, head="新 head {pic:1}",
                                    tail="新 tail", seg_head=None, first_frame=None,
                                    last_frame=None, link_mode=None, link_transition=None,
                                    ref_video=None, seg_tail=None,
                                    megapixels=None, aspect_ratio=None)
            R.cmd_set(R.load_cfg(), ns)
            out = _json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(out["head"], "新 head {pic:1}")
        self.assertEqual(out["tail"], "新 tail")
        self.assertTrue(out.get("head_updated_at"), "改 head 没打 head_updated_at，全片不会标需重出")
        self.assertEqual(out["segments"][0]["seed"], 7, "全局改动不该动到段级字段")

    def test_set_megapixels_marks_all_segments_stale(self):
        """全局改百万像素/画幅：写回 cfg、输出新值，并让所有已完成段标需重出。"""
        import argparse
        import json as _json
        import os
        import tempfile
        import time

        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "segments.json"
            cfg_path.write_text(_json.dumps({
                "title": "t", "prefix": "p", "head": "", "tail": "",
                "segments": [{"id": "s01", "seed": 7, "body": "b"},
                             {"id": "s02", "seed": 8, "body": "b"}],
            }, ensure_ascii=False), encoding="utf-8")
            segdir = Path(tmp) / "segments"
            segdir.mkdir()
            old = time.time() - 100
            for sid in ("s01", "s02"):
                vid = segdir / f"p_{sid}.mp4"
                vid.write_bytes(b"x")
                os.utime(vid, (old, old))

            R.set_root(str(cfg_path))
            before = R.seg_status(R.load_cfg())
            self.assertEqual(before["megapixels"], 0.5)
            self.assertFalse(any(s["stale"] for s in before["segments"]))

            ns = argparse.Namespace(seg=0, join=None, seed=None, body=None, head=None, tail=None,
                                    seg_head=None, first_frame=None, last_frame=None,
                                    link_mode=None, link_transition=None, ref_video=None,
                                    seg_tail=None, megapixels=0.8,
                                    aspect_ratio="9:16 (Portrait Widescreen)")
            R.cmd_set(R.load_cfg(), ns)
            out = _json.loads(cfg_path.read_text(encoding="utf-8"))
            after = R.seg_status(out)
        self.assertEqual(out["megapixels"], 0.8)
        self.assertEqual(out["aspect_ratio"], "9:16 (Portrait Widescreen)")
        self.assertTrue(out.get("res_updated_at"), "改分辨率没打时间戳，全片不会标需重出")
        self.assertEqual(after["megapixels"], 0.8)
        self.assertTrue(all(s["stale"] for s in after["segments"]),
                        "分辨率变了，所有已完成段都必须标需重出")

    def test_set_join_writes_join_mode_to_cfg(self):
        """网页「全篇硬切」保存：/set 转发 --join，driver 把 join_mode 落盘到 segments.json。"""
        import argparse
        import json as _json
        import tempfile

        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "segments.json"
            cfg_path.write_text(_json.dumps({
                "title": "t", "prefix": "p", "head": "", "tail": "",
                "join_mode": "auto",
                "segments": [{"id": "s01", "seed": 7, "body": "b"}],
            }, ensure_ascii=False), encoding="utf-8")
            R.set_root(str(cfg_path))
            ns = argparse.Namespace(seg=0, join="cut", seed=None, body=None, head=None,
                                    tail=None, seg_head=None, first_frame=None,
                                    last_frame=None, link_mode=None, link_transition=None,
                                    ref_video=None, seg_tail=None, megapixels=None,
                                    aspect_ratio=None)
            R.cmd_set(R.load_cfg(), ns)
            out = _json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(out["join_mode"], "cut", "全篇硬切没写回 join_mode")
        self.assertEqual(out["segments"][0]["seed"], 7, "全局 join 不该动到段级字段")

    def test_video_ref_prompt_injection_and_removal(self):
        """插入动作参考视频：提示词最前面加带哨兵的说明；删除时整段干净剥除。

        重复插入不能叠加，用户手改中间内容也能被剥掉（哨兵正则非贪婪）。
        """
        S = SP.VIDEO_REF_SENTINEL
        body = "两人松开手后同步旋转。"

        injected = SP._inject_video_ref(body)
        self.assertTrue(injected.startswith(S), "动作参考说明必须插在提示词最前面")
        self.assertIn("参考视频仅用于动作指导", injected)
        self.assertIn(body, injected, "原提示词必须保留在说明之后")

        # 重复注入不得叠加哨兵
        again = SP._inject_video_ref(injected)
        self.assertEqual(again.count(S), 2, "重复注入不应翻倍")

        # 手改中间内容也能被剥除
        messy = injected[:25] + "用户乱改" + injected[25:]
        self.assertEqual(SP._strip_video_ref(messy), body)

        # 删除操作应回到原样
        self.assertEqual(SP._strip_video_ref(injected), body)
        self.assertNotIn(S, SP._strip_video_ref(injected))

    def test_refvideo_route_registered(self):
        """selflift/refvideo 路由存在且接受 upload/delete 两种 op。"""
        routes = FakeRoutes()
        SP.register_routes(routes, FakeWeb())
        self.assertIn(("POST", "/h3-long-video/selflift/refvideo"), routes.paths)

    def test_cancel_route_registered(self):
        """取消全片必须走服务端路由，只清浏览器内存里的队列不算取消。"""
        routes = FakeRoutes()
        SP.register_routes(routes, FakeWeb())
        self.assertIn(("POST", "/h3-long-video/selflift/cancel"), routes.paths)
        self.assertTrue(hasattr(SP, "CANCELLED"), "服务端要有跨标签页的取消状态")

    # ---------------- 片名 / 片编号（2026-11-12） ----------------
    def test_film_id_display_label(self):
        """编号来自文件夹末尾，占位标题显示成「未命名 #编号」，label 带编号。"""
        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        self.assertEqual(R.film_id_of("女生的午后_1112_135157"), "1112_135157")
        self.assertEqual(R.film_id_of("任意目录", {"prefix": "sl_1111_215408"}),
                         "1111_215408")
        self.assertEqual(R.film_id_of("rooftop-dusk-v2", {"prefix": "rooftop2"}),
                         "rooftop-dusk-v2")
        self.assertEqual(R.film_display("segments-template", "1112_135157"),
                         "未命名 #1112_135157")
        self.assertEqual(R.film_display("女生的午后", "1112_135157"), "女生的午后")
        label = R.film_label("女生的午后", "1112_135157", "11-12 14:15", 6, 6, True)
        self.assertIn("女生的午后", label)
        self.assertIn("#1112_135157", label)
        self.assertIn("✓成片", label)
        # 占位名里已经有编号，别再重复拼一次
        self.assertEqual(
            R.film_label("segments-template", "1112_135157", "", 0, 1, False)
            .count("#1112_135157"), 1)

    def test_list_films_exposes_name_and_id(self):
        """status 的片列表要给页面提供编号 / 显示名 / label / 搜索串。"""
        import os
        import tempfile
        from unittest import mock
        sys.path.insert(0, str(Path(SP.DRIVER).parent))
        import run_selflift as R
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder, title in (("segments-template_1112_135157", "segments-template"),
                                  ("女生的午后_1112_1210", "女生的午后")):
                d = root / folder
                d.mkdir()
                (d / "segments.json").write_text(json.dumps({
                    "title": title, "prefix": "sl_" + folder.rsplit("_", 2)[-2],
                    "segments": [{"id": "s01"}],
                }, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"H3_FILMS_ROOT": str(root)}):
                films = R.list_films()
            self.assertEqual(len(films), 2)
            by_dir = {f["dir"]: f for f in films}
            a = by_dir["segments-template_1112_135157"]
            self.assertEqual(a["film_id"], "1112_135157")
            self.assertEqual(a["display"], "未命名 #1112_135157")
            self.assertIn("#1112_135157", a["label"])
            self.assertIn("1112_135157", a["search"])
            b = by_dir["女生的午后_1112_1210"]
            self.assertEqual(b["display"], "女生的午后")
            self.assertIn("女生的午后", b["search"])

    def test_rename_film_syncs_folder_paths_and_final(self):
        """改名 = 片名/文件夹/cfg 绝对路径/成片名一起带同一个编号。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "segments-template_1112_135157"
            (old / "refs").mkdir(parents=True)
            (old / "segments").mkdir()
            (old / "final").mkdir()
            cfg = {
                "title": "segments-template", "prefix": "sl_1112_135157",
                "final_name": "segments-template_成片.mp4",
                "ref_image": (old / "refs" / "girl.png").as_posix(),
                "bg_plate_path": str(old / "refs" / "bg.png"),
                "ref_images": [{"path": (old / "refs" / "a.png").as_posix()}],
                "segments": [{"id": "s01",
                              "first_frame_override": str(old / "refs" / "f.png")}],
            }
            (old / "segments.json").write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            (old / "final" / "segments-template_成片.mp4").write_bytes(b"x")
            (old / "final" / "segments-template_成片_静音.mp4").write_bytes(b"y")

            r = SP._rename_film(old / "segments.json", "女生的午后")
            self.assertEqual(r["film_id"], "1112_135157")
            self.assertEqual(r["dir"], "女生的午后_1112_135157")
            self.assertTrue(r["final_renamed"])
            new = root / r["dir"]
            self.assertTrue(new.is_dir())
            self.assertFalse(old.exists())
            c2 = json.loads((new / "segments.json").read_text(encoding="utf-8"))
            self.assertEqual(c2["title"], "女生的午后")
            self.assertEqual(c2["film_id"], "1112_135157")
            self.assertEqual(c2["final_name"], "女生的午后_1112_135157_成片.mp4")
            self.assertIn(new.as_posix(), c2["ref_image"])
            self.assertIn(str(new), c2["bg_plate_path"])
            self.assertIn(str(new), c2["segments"][0]["first_frame_override"])
            self.assertNotIn(old.name, json.dumps(c2, ensure_ascii=False))
            self.assertTrue((new / "final" / "女生的午后_1112_135157_成片.mp4").is_file())
            self.assertTrue((new / "final" / "女生的午后_1112_135157_成片_静音.mp4").is_file())
            with self.assertRaises(ValueError):
                SP._rename_film(r["cfg"], "带/斜杠的名字")

    def test_rename_route_and_ui_present(self):
        """改名路由、搜索框与手册说明都要在，避免后端加了前端没落地。"""
        routes = FakeRoutes()
        SP.register_routes(routes, FakeWeb())
        self.assertIn(("POST", "/h3-long-video/selflift/rename"), routes.paths)
        page = (ROOT / "web" / "selflift_app.html").read_text(encoding="utf-8")
        for token in ("filmFilter", "renameBtn", "renderFilmOptions",
                      "/h3-long-video/selflift/rename", "film_id"):
            self.assertIn(token, page, f"页面缺 {token}")
        manual = (ROOT / "web" / "manual.html").read_text(encoding="utf-8")
        self.assertIn("片名 · 编号 · 文件夹", manual)

    def test_films_route_and_loadfilms_present(self):
        """首屏下拉必须从文件夹读片清单，不能只靠 localStorage。"""
        routes = FakeRoutes()
        SP.register_routes(routes, FakeWeb())
        self.assertIn(("GET", "/h3-long-video/selflift/films"), routes.paths)
        self.assertTrue(hasattr(SP, "list_films_local"),
                        "面板要有不依赖 driver 的片扫描函数")
        page = (ROOT / "web" / "selflift_app.html").read_text(encoding="utf-8")
        for token in ("loadFilms", "selectFilm", "/h3-long-video/selflift/films"):
            self.assertIn(token, page, f"页面缺 {token}")
        # list_films_local 不依赖某个具体 cfg，扫的是输出根目录
        films = SP.list_films_local()
        self.assertIsInstance(films, list)


if __name__ == "__main__":
    unittest.main()
