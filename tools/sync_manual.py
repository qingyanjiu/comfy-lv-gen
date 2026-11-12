"""把 web/manual.html 同步进 selflift_app.html 的内嵌文档块。

为什么内嵌：出片台的两个入口（独立 :8900 / 内嵌 8188）都只注册了有限的路由，
加一个静态页面路由就必须重启常驻进程（8188 还得重启桌面版）。把手册正文放进
`<script type="text/plain">` 后，按钮用 Blob URL 打开就能得到真正的独立手册页，
刷新页面即生效，零重启、两个入口一致。

manual.html 是唯一内容源；改完手册跑一次本脚本即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "web" / "manual.html"
DST = ROOT / "web" / "selflift_app.html"
BEGIN = "<!-- MANUAL_DOC_BEGIN -->"
END = "<!-- MANUAL_DOC_END -->"


def main() -> int:
    doc = SRC.read_text(encoding="utf-8")
    if "</script" in doc.lower():
        print("手册里出现 </script，会截断内嵌块，请改用实体转义", file=sys.stderr)
        return 1
    html = DST.read_text(encoding="utf-8")
    block = (
        f"{BEGIN}\n"
        f'<script id="manualDoc" type="text/plain">\n{doc}\n</script>\n'
        f"{END}"
    )
    if BEGIN in html and END in html:
        i, j = html.index(BEGIN), html.index(END) + len(END)
        html = html[:i] + block + html[j:]
        action = "已更新"
    else:
        marker = "<script>\n"
        pos = html.index(marker)
        html = html[:pos] + block + "\n" + html[pos:]
        action = "已插入"
    DST.write_text(html, encoding="utf-8")
    print(f"{action}内嵌手册：{len(doc)} 字符 -> {DST.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
