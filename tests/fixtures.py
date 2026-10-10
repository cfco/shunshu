"""各种盗版站目录页形态的测试数据。"""

from __future__ import annotations

BASE_URL = "https://book.example.com/12_3456/"
CHAPTERS = [f"第{i}章 测试章节{i:04d}" for i in range(1, 61)]


def toc_links(titles: list[str], start: int = 1) -> str:
    """盗版站最常见的目录结构：dl 里逐条 dd + a。"""
    return "\n".join(
        f'<dd><A href="{start + offset}.html">{title}</A></dd>'
        for offset, title in enumerate(titles)
    )


def normal_html(chapters: list[str] | None = None) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>测试这本书_笔趣阁_最新章节</title></head>
<body>
<div class="header"><a href="/">首页</a><a href="/user/bookcase">我的书架</a></div>
<div class="info"><h1>测试这本书</h1><a href="/author/张三">作者的其他作品</a></div>
<div class="listmain">
  <dl>
{toc_links(chapters or CHAPTERS)}
    <DD><A href="/module.php?go=next">下一页</A></DD>
  </dl>
</div>
<div class="footer"><a href="/about">关于我们</a></div>
</body></html>"""


def gbk_bytes(chapters: list[str] | None = None) -> bytes:
    html = f"""<!DOCTYPE html>
<html><head><meta http-equiv="Content-Type" content="text/html; charset=gbk">
<title>古风测试书_顶点小说</title></head>
<body><div class="listmain"><dl>
{toc_links(chapters or [f"第{i}章 风雨如晦" for i in range(1, 41)])}
</dl></div></body></html>"""
    return html.encode("gb18030")


def reverse_html() -> str:
    """最新章节排在最前面的站点：判断最新不能按页面位置。"""
    return f"""<!DOCTYPE html><html><head><title>倒序这本书目录</title></head>
<body><div class="listmain"><dl>
{toc_links(list(reversed(CHAPTERS)))}
</dl></div></body></html>"""


def paged_first_html() -> str:
    pages = "".join(f'<a href="_{n}.html">{n}</a>' for n in range(1, 21))
    return f"""<!DOCTYPE html><html><head><title>分页这本书</title></head>
<body><div class="listmain"><dl>
{toc_links(CHAPTERS)}
</dl></div>
<div class="pagetype"><a href="_1.html">上一页</a> {pages} <a href="_2.html">下一页</a>
<a href="_20.html">末页</a></div>
</body></html>"""


def paged_last_html(latest: str = "第六百章 大结局", number: int = 600) -> str:
    return f"""<!DOCTYPE html><html><head><title>分页这本书</title></head>
<body><div class="listmain"><dl>
{toc_links([latest], start=number)}
</dl></div>
<div class="pagetype"><a href="_1.html">首页</a> <a href="_19.html">上一页</a></div>
</body></html>"""


def ad_heavy_html() -> str:
    """盗版站常见形态：广告链接就混在章节目录容器里。"""
    return f"""<!DOCTYPE html><html><head><title>广告很多这本书</title></head>
<body>
<div class="listmain"><dl>
{toc_links(CHAPTERS[:30])}
  <DD class="ad"><A href="/xiaoshuo/9999/200.html">第200章 广告容器里的假章节</A></DD>
  <DD><A href="https://www.赌博外站.com/agent-register.html">澳门真人娱乐注册送88</A></DD>
  <DD><A href="https://download-elsewhere.net/book.txt">本书txt下载</A></DD>
  <DD><A href="/module.php?mod=bookcase">加入书签</A></DD>
  <DD><A href="/user/feedback.php?aid=12">章节报错</A></DD>
  <DD><A href="javascript:goTop()">返回顶部</A></DD>
  <DD><A href="#top">目录</A></DD>
  <DD><A href="/xiaoshuo/12_3456/31.html">第三十一章 中文数字章</A></DD>
</dl></div>
</body></html>"""


def no_container_html() -> str:
    return """<!DOCTYPE html><html><head><title>识别失败这本书</title></head>
<body><div class="nav"><a href="/">首页</a><a href="/rank">排行</a><a href="/finish">完本</a></div>
<p>本书章节已隐藏，请登录后查看。</p></body></html>"""


def skip_link_html() -> str:
    """章节链接指向站内跳板页：保留，跳转交给浏览器完成。"""
    links = "\n".join(
        f'<A href="/b.php?url=/read/{i}.html">第{i}章 跳板章节</A>' for i in range(1, 31)
    )
    return f"""<!DOCTYPE html><html><head><title>跳板这本书</title></head>
<body><div class="chapterlist"><ul>
{links}
</ul></div></body></html>"""


def plain_numbered_html() -> str:
    """没有「第X章」字样、用 1001. 形式编号的目录。"""
    links = "\n".join(f'<A href="{1000 + i}.html">{1000 + i}. 无题章{i}</A>' for i in range(1, 26))
    return f"""<!DOCTYPE html><html><head><title>编号这本书</title></head>
<body><div class="chapterlist"><ul>
{links}
</ul></div></body></html>"""


def renamed_title_suffix_html() -> str:
    """标题带站名尾缀与零宽字符：清洗后才能稳定比对。"""
    titles = [f"第{i}章​ 含零宽与后缀 _笔趣阁" for i in range(1, 21)]
    return normal_html(titles)


def next_only_html(chapters: list[str] | None = None,
                   nav: str = '<dd><a href="p-2.html">下一页</a></dd>') -> str:
    """只有「下一页」、没有「末页」的两页目录。"""
    return f"""<!DOCTYPE html><html><head><title>只有下一页这本书目录</title></head>
<body><div class="listmain"><dl>
{toc_links(chapters or CHAPTERS)}
{nav}
</dl></div></body></html>"""
