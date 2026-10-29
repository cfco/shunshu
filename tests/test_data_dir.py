"""数据目录选址（config.data_dir）：deb 装在 /opt 写不动时的退路。

这条是 deb 的硬前提——没有回退，装完第一次写盘就崩在启动里，现场只有一张「启动失败」卡。
"""

from __future__ import annotations

from pathlib import Path

from shunshu import config


def _reset_cache(monkeypatch):
    monkeypatch.setattr(config, "_data_dir_cache", None, raising=False)


def test_override_wins(monkeypatch, tmp_path):
    _reset_cache(monkeypatch)
    monkeypatch.delenv("SHUNSHU_DATA_HOME", raising=False)
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "given"))
    assert config.data_dir() == tmp_path / "given"


def test_writable_install_dir_stays_beside_the_program(monkeypatch, tmp_path):
    """exe/源码态：安装目录能写就还在程序旁边，不能因为加了回退就悄悄搬走。"""
    _reset_cache(monkeypatch)
    monkeypatch.delenv("SHUNSHU_DATA_DIR", raising=False)
    install = tmp_path / "shunshu-0.16.0"
    install.mkdir()
    monkeypatch.setattr(config, "_base_dir", lambda: install)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert config.data_dir() == install / "data"
    assert not (tmp_path / "xdg").exists()


def test_readonly_install_dir_falls_back_to_xdg(monkeypatch, tmp_path):
    """deb 形态：/opt/shunshu 建不出 data/，退到 XDG 数据目录并留下原因。"""
    _reset_cache(monkeypatch)
    monkeypatch.delenv("SHUNSHU_DATA_DIR", raising=False)
    blocked = tmp_path / "opt-shunshu"
    blocked.write_text("")                      # 「安装目录」其实是个文件：mkdir 必失败
    monkeypatch.setattr(config, "_base_dir", lambda: blocked)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    picked = config.data_dir()
    assert picked == Path(tmp_path / "xdg") / config.APP_NAME
    assert picked.exists()
    assert "不可写" in (picked / "shunshu.log").read_text(encoding="utf-8")


def test_choice_is_cached(monkeypatch, tmp_path):
    """选址只做一次：data_dir() 每轮都会被碰到，不该每次都去摸盘。"""
    _reset_cache(monkeypatch)
    monkeypatch.delenv("SHUNSHU_DATA_DIR", raising=False)
    install = tmp_path / "app"
    install.mkdir()
    monkeypatch.setattr(config, "_base_dir", lambda: install)

    first = config.data_dir()
    monkeypatch.setattr(config, "_base_dir", lambda: tmp_path / "elsewhere")
    assert config.data_dir() == first
