import fixtures
import pytest
from conftest import FakeFetcher

from shunshu.config import Config
from shunshu.reader import load_toc

from shunshu.toc_detect import (
    TocDetectError,
    build_chapters,
    clean_title,
    container_by_selector,
    find_page_urls,
    parse_page,
    pick_container,
)


def _container(html, url=fixtures.BASE_URL, rules=None, prefer=None):
    from shunshu.config import Config

    rules = rules or Config().compiled()
    page = parse_page(html, url, rules)
    return page, pick_container(page, rules, prefer_prefix=prefer)


def test_normal_toc_is_detected_with_latest_chapter(rules):
    page, container = _container(fixtures.normal_html(), rules=rules)
    chapters = build_chapters(container.links)
    assert len(chapters) == 60
    assert chapters[-1].number == 60
    assert chapters[-1].title.startswith("第60章")


def test_book_title_prefers_h1_over_title_tag(rules):
    page, _ = _container(fixtures.normal_html(), rules=rules)
    assert page.book_title == "测试这本书"


def test_latest_uses_chapter_number_not_page_position(rules):
    """倒序目录：最后一章仍要认成第60章。"""
    page, container = _container(fixtures.reverse_html(), rules=rules)
    chapters = build_chapters(container.links)
    assert chapters[0].number == 1
    assert chapters[-1].number == 60


def test_plain_numbered_titles_are_accepted(rules):
    page, container = _container(fixtures.plain_numbered_html(), rules=rules)
    chapters = build_chapters(container.links)
    assert len(chapters) == 25
    assert chapters[-1].number == 1025


def test_ads_inside_chapter_container_are_filtered(rules):
    page, container = _container(fixtures.ad_heavy_html(), rules=rules)
    chapters = build_chapters(container.links)
    titles = [c.title for c in chapters]
    assert len(chapters) == 31  # 30 真章节 + 第三十一章
    assert any(t.startswith("第三十一章") for t in titles)
    reasons = {info.drop_reason for info in container.dropped}
    assert {"external_host", "functional_text", "bad_href", "ad_container"} <= reasons
    assert not any("赌博" in (info.text or "") for info in container.kept)
    assert not any("txt下载" in info.text for info in container.kept)


def test_ad_container_node_hides_fake_chapter_links(rules):
    page, container = _container(fixtures.ad_heavy_html(), rules=rules)
    dropped = {info.text: info.drop_reason for info in container.dropped}
    fake = next(text for text in dropped if text.startswith("第200章"))
    assert dropped[fake] == "ad_container"


def test_title_site_suffix_and_zero_width_cleaned(rules):
    page, container = _container(fixtures.renamed_title_suffix_html(), rules=rules)
    chapters = build_chapters(container.links)
    assert all("笔趣阁" not in c.title for c in chapters)
    assert all("​" not in c.title for c in chapters)
    assert [c.number for c in chapters] == list(range(1, 21))


def test_unresolvable_page_raises_with_candidates(rules):
    with pytest.raises(TocDetectError) as exc:
        _container(fixtures.no_container_html(), rules=rules)
    assert exc.value.candidates, "识别失败时必须给出候选容器"


def test_ad_class_on_anchor_itself_is_filtered():
    html = ('<html><head><title>测试这本书</title></head><body><div class="chapterlist"><dl>'
            + fixtures.toc_links([f"第{i}章 正章" for i in range(1, 21)])
            + '<dd><a class="ad" href="/99.html">第99章 假章</a></dd>'
            + '</dl></div></body></html>')
    result = load_toc(FakeFetcher({fixtures.BASE_URL: html}), fixtures.BASE_URL, Config().compiled())
    assert len(result.toc.chapters) == 20
    assert result.toc.latest.title == "第20章 正章"
    assert any(item.reason == "ad_container" and "假章" in item.text for item in result.dropped)


def test_plain_numbered_chapters_are_not_followed_as_pages():
    html = ('<html><head><title>无题书</title></head><body><div class="chapterlist"><dl>'
            + ''.join(f'<dd><a href="{i}.html">{i}</a></dd>' for i in range(1, 21))
            + '</dl></div></body></html>')
    fetcher = FakeFetcher({fixtures.BASE_URL: html})
    result = load_toc(fetcher, fixtures.BASE_URL, Config().compiled())
    assert fetcher.calls == [fixtures.BASE_URL]
    assert len(result.toc.chapters) == 20
    assert result.toc.pages_followed == 1


def test_numbered_chapters_with_page_like_urls_inside_container_are_kept():
    html = ('<html><head><title>无题书</title></head><body><div class="chapterlist"><dl>'
            + ''.join(f'<dd><a href="_{i}.html">{i}</a></dd>' for i in range(1, 21))
            + '</dl></div></body></html>')
    fetcher = FakeFetcher({fixtures.BASE_URL: html})
    result = load_toc(fetcher, fixtures.BASE_URL, Config().compiled())
    assert fetcher.calls == [fixtures.BASE_URL]
    assert len(result.toc.chapters) == 20
    assert result.toc.latest.number == 20


def test_plain_numeric_pagination_outside_chapter_container_is_followed(rules):
    html = ('<html><head><title>测试这本书</title></head><body>'
            '<div class="chapterlist"><dl>'
            + fixtures.toc_links([f"第{i}章 正章" for i in range(1, 21)])
            + '</dl></div><div class="nav"><a href="21.html">21</a><a href="22.html">22</a></div>'
            '</body></html>')
    last_page = ('<html><head><title>测试这本书</title></head><body>'
                 '<div class="chapterlist"><dl><dd><a href="23.html">第23章 最新章节</a></dd></dl></div>'
                 '</body></html>')
    fetcher = FakeFetcher({fixtures.BASE_URL: html, fixtures.BASE_URL + "22.html": last_page})

    result = load_toc(fetcher, fixtures.BASE_URL, rules)
    assert fetcher.calls == [fixtures.BASE_URL, fixtures.BASE_URL + "22.html"]
    assert result.toc.pages_followed == 2
    assert result.toc.latest.title == "第23章 最新章节"


def test_mixed_numeric_page_url_styles_outside_container_are_followed(rules):
    html = ('<html><head><title>测试这本书</title></head><body>'
            '<div class="chapterlist"><dl>'
            + fixtures.toc_links([f"第{i}章 正章" for i in range(1, 21)])
            + '</dl></div><div class="nav"><a href="21.html">21</a>'
            '<a href="_22.html">22</a><a href="23.html">23</a></div>'
            '</body></html>')
    last_page = ('<html><head><title>测试这本书</title></head><body>'
                 '<div class="chapterlist"><dl><dd><a href="24.html">第24章 最新章节</a></dd></dl></div>'
                 '</body></html>')
    fetcher = FakeFetcher({fixtures.BASE_URL: html, fixtures.BASE_URL + "23.html": last_page})

    result = load_toc(fetcher, fixtures.BASE_URL, rules)
    assert fetcher.calls == [fixtures.BASE_URL, fixtures.BASE_URL + "23.html"]
    assert result.toc.latest.title == "第24章 最新章节"


def test_numeric_pagination_uses_the_last_continuous_run(rules):
    html = ('<html><head><title>测试这本书</title></head><body>'
            '<div class="chapterlist"><dl>'
            + fixtures.toc_links([f"第{i}章 正章" for i in range(1, 21)])
            + '</dl></div><div class="nav"><a href="1.html">1</a>'
            '<a href="21.html">21</a><a href="_22.html">22</a><a href="23.html">23</a></div>'
            '</body></html>')
    last_page = ('<html><head><title>测试这本书</title></head><body>'
                 '<div class="chapterlist"><dl><dd><a href="24.html">第24章 最新章节</a></dd></dl></div>'
                 '</body></html>')
    fetcher = FakeFetcher({fixtures.BASE_URL: html, fixtures.BASE_URL + "23.html": last_page})

    result = load_toc(fetcher, fixtures.BASE_URL, rules)
    assert fetcher.calls == [fixtures.BASE_URL, fixtures.BASE_URL + "23.html"]
    assert result.toc.latest.title == "第24章 最新章节"


def test_manual_selector_can_be_used(rules):
    html = fixtures.no_container_html().replace(
        "</body>",
        '<dl class="real">' + fixtures.toc_links([f"第{i}章 手写命中" for i in range(1, 15)]) + "</dl></body>",
    )
    page = parse_page(html, fixtures.BASE_URL, rules)
    container = container_by_selector(page, "dl.real")
    chapters = build_chapters(container.links)
    assert len(chapters) == 14
    assert chapters[-1].number == 14


def test_pagination_last_page_is_found(rules):
    page, _ = _container(fixtures.paged_first_html(), rules=rules)
    last, numeric = find_page_urls(page, rules)
    assert last == fixtures.BASE_URL + "_20.html"
    assert len(numeric) >= 2


def test_pagination_pages_excluded_from_chapters(rules):
    page, container = _container(fixtures.paged_first_html(), rules=rules)
    chapters = build_chapters(container.links)
    assert len(chapters) == 60
    assert not any("末页" in c.title for c in chapters)


def test_skip_links_stay_on_same_site(rules):
    page, container = _container(fixtures.skip_link_html(), rules=rules)
    chapters = build_chapters(container.links)
    assert len(chapters) == 30
    assert all("/b.php" in c.url for c in chapters)


def test_clean_title_keeps_real_chapter_with_promo_words(rules):
    """「第3章（求订阅）」是真章节：只清括号，不改判成广告。"""
    title = clean_title("第3章（求订阅） 初入江湖 _笔趣阁", rules)
    assert title.startswith("第3章")
    assert "求订阅" not in title and "笔趣阁" not in title
    from shunshu.models import parse_chapter_number

    assert parse_chapter_number(title) == 3
