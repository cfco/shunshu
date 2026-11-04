"""数据目录选址（config.data_dir）：程序装在写不动的地方、或装在 venv 里时的落点。

装到只读位置（exe 在 Program Files 这类地方）没有回退的话，第一次写盘就崩在启动里，
现场只有一张「启动失败」卡；pip/venv 安装态的基准目录必须在 XDG，否则书架跟着
site-packages 走，重装一次虚拟环境就没了。
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
    """安装目录建不出 data/：退到 XDG 数据目录并留下原因。"""
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


def _fake_installed_site(monkeypatch, tmp_path):
    """把 config 假装成装在 site-packages 里：往上找不到 pyproject.toml。"""
    site = tmp_path / "venv" / "lib" / "python3.14" / "site-packages" / "shunshu"
    site.mkdir(parents=True)
    monkeypatch.setattr(config, "__file__", str(site / "config.py"))
    monkeypatch.setattr(config, "is_packaged", lambda: False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    return site


def test_installed_package_lands_in_xdg_not_site_packages(monkeypatch, tmp_path):
    """pip/venv 安装态（scripts/install-linux.sh 那条路）：数据目录跟着用户，不跟着环境。

    site-packages 在重装虚拟环境时会被整个抹掉，书架与代理记忆留在那儿等于随升级丢失。
    """
    _reset_cache(monkeypatch)
    monkeypatch.delenv("SHUNSHU_DATA_DIR", raising=False)
    site = _fake_installed_site(monkeypatch, tmp_path)

    assert config._base_dir() == Path(tmp_path / "xdg") / config.APP_NAME
    assert config.data_dir() == Path(tmp_path / "xdg") / config.APP_NAME / "data"
    assert config.data_dir().is_dir()
    assert not (site / "data").exists()


def test_source_checkout_still_uses_project_root(monkeypatch, tmp_path):
    """仓库检出（有 pyproject.toml）不受安装态那条影响，data/ 还在项目根。"""
    _reset_cache(monkeypatch)
    monkeypatch.delenv("SHUNSHU_DATA_DIR", raising=False)
    root = tmp_path / "checkout"
    (root / "shunshu").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "shunshu"\n', encoding="utf-8")
    monkeypatch.setattr(config, "__file__", str(root / "shunshu" / "config.py"))
    monkeypatch.setattr(config, "is_packaged", lambda: False)

    assert config._base_dir() == root.resolve()
