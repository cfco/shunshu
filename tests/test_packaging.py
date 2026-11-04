"""运行形态判定（packaging）：Nuitka 伪造 sys.executable 的坑必须测住。

自启命令、data 目录定位、卡片子进程都靠 is_packaged / self_executable 的同一结论，
判错时现象是「自启写了个不存在的 python.exe」「升级后书架消失」，现场很难查。
"""

from __future__ import annotations

import os
import sys


def test_pyinstaller_frozen_flag_is_recognized(monkeypatch, tmp_path):
    """PyInstaller 态：有 sys.frozen 就直接认打包，不看 argv。"""
    from shunshu import packaging

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "whatever.py")])
    assert packaging.is_packaged() is True


def test_nuitka_layout_is_recognized_by_argv_and_missing_interpreter(monkeypatch, tmp_path):
    """Nuitka 态：argv[0] 是真实 exe，而 sys.executable 指向产物目录里不存在的 python.exe。"""
    from shunshu import packaging

    exe = tmp_path / "shunshu.exe"
    exe.write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "argv", [str(exe)])
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))

    assert packaging.is_packaged() is True
    assert os.path.normcase(packaging.self_executable()) == os.path.normcase(str(exe))


def test_source_layout_with_a_real_interpreter_is_not_packaged(monkeypatch, tmp_path):
    """源码态：解释器真实存在，argv[0] 是不存在的 __main__.py，就不算打包。"""
    from shunshu import packaging

    interpreter = tmp_path / "pythonw.exe"
    interpreter.write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "__main__.py")])

    assert packaging.is_packaged() is False
    assert os.path.normcase(packaging.self_executable()) == os.path.normcase(str(interpreter))


def test_nuitka_on_linux_standalone_is_packaged(monkeypatch, tmp_path):
    """Linux 上的 Nuitka standalone：argv[0] 与 sys.executable 都指向真实存在的产物文件。

    只看「解释器存不存在」会判成源码态，于是卡片子进程被拼成 `./shunshu -m shunshu popup`
    （产物不认 -m）、data/ 从 __file__ 往上找 pyproject 找不到——现场表现是「弹不出卡、
    书架像空了」，所以入口模块的 __compiled__ 必须认出来。
    """
    from shunshu import packaging

    binary = tmp_path / "shunshu"
    binary.write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "argv", [str(binary)])
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys.modules["__main__"], "__compiled__", (3, 0), raising=False)

    assert packaging.is_packaged() is True
    assert os.path.normcase(packaging.self_executable()) == os.path.normcase(str(binary))


def test_packaged_popup_command_reuses_the_binary(monkeypatch, tmp_path):
    """打包态弹卡片要用产物自己（同一个 exe/bin 靠子命令分流），不能再走 -m。"""
    from shunshu import packaging
    from shunshu.notify import popup_command

    binary = tmp_path / "shunshu"
    binary.write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "argv", [str(binary)])
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys.modules["__main__"], "__compiled__", (3, 0), raising=False)

    command = popup_command({"title": "t", "lines": [], "chapters": []}, 0)
    assert command[0] == str(binary.resolve())
    assert command[1] == "popup"
    assert "-m" not in command


def test_source_mode_under_a_real_interpreter_stays_source(monkeypatch, tmp_path):
    """源码态（解释器与脚本都在、入口没 __compiled__）不能因为「argv[0] 是真文件」就被判成打包。"""
    from shunshu import packaging

    interpreter = tmp_path / "python3"
    script = tmp_path / "shunshu.py"
    interpreter.write_bytes(b"")
    script.write_text("")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.delattr(sys.modules["__main__"], "__compiled__", raising=False)

    assert packaging.is_packaged() is False


def test_self_executable_prefers_argv_zero_when_both_exist(monkeypatch, tmp_path):
    """两边都真实存在时（少见）以 argv[0] 为准：它才是真正被启动的那个文件。"""
    from shunshu import packaging

    exe = tmp_path / "shunshu.exe"
    interpreter = tmp_path / "python.exe"
    exe.write_bytes(b"")
    interpreter.write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "argv", [str(exe)])
    monkeypatch.setattr(sys, "executable", str(interpreter))

    # 两个文件都在 → 按定义不算 Nuitka 打包态，但 self_executable 仍先信 argv[0]
    assert packaging.is_packaged() is False
    assert os.path.normcase(packaging.self_executable()) == os.path.normcase(str(exe))


# ---------- 产物里的版本号 ----------

def test_packaged_version_reads_the_file_beside_the_exe(monkeypatch, tmp_path):
    """Nuitka 产物没有 dist-info，importlib 读不到版本：崩溃日志的版本靠产物里那个 VERSION。

    v0.16.0 那次 Ubuntu 26.10 的崩溃日志写的是 v0.0.0，看日志根本认不出是哪一版。
    """
    import shunshu
    from shunshu import packaging

    exe = tmp_path / "shunshu"
    exe.write_bytes(b"")
    (tmp_path / "VERSION").write_text("0.17.0", encoding="utf-8")
    monkeypatch.setattr(packaging, "is_packaged", lambda: True)
    monkeypatch.setattr(packaging, "self_executable", lambda: str(exe))

    assert shunshu._packaged_version() == "0.17.0"


def test_packaged_version_is_empty_in_source_mode(monkeypatch):
    """源码态不猜目录：返回空串，由调用方退回 "0.0.0"。"""
    import shunshu
    from shunshu import packaging

    monkeypatch.setattr(packaging, "is_packaged", lambda: False)
    assert shunshu._packaged_version() == ""


# ---------- Linux 打包态的 gi 从系统取 ----------

def test_linux_packaged_mode_appends_the_apt_gi_dir(monkeypatch):
    """产物里没有 gi：把 apt 的 dist-packages 挂到 sys.path 末尾，gi 与 _gi.so 才是同一套。

    26.10 上冻结进产物的 gi 起不来（PyGObject 3.57 + glib 把 unix_* 拆到 GLibUnix-2.0，
    现场是 `unix_signal_add_full was set deprecated but wasn't added to __all__`）。
    """
    from shunshu import packaging

    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != packaging.SYSTEM_GI_DIR])
    monkeypatch.setattr(packaging, "IS_WINDOWS", False)
    monkeypatch.setattr(packaging, "is_packaged", lambda: True)

    assert packaging.expose_system_gi() is True
    # 挂末尾而不是开头：产物里冻结的那些依赖（PIL、requests…）仍要优先于系统 dist-packages
    assert sys.path[-1] == packaging.SYSTEM_GI_DIR


def test_source_mode_and_windows_do_not_touch_sys_path(monkeypatch):
    """源码态 gi 本来就 import 得到，Windows 没有这个目录，都不该动 sys.path。"""
    from shunshu import packaging

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(packaging, "IS_WINDOWS", False)
    monkeypatch.setattr(packaging, "is_packaged", lambda: False)
    assert packaging.expose_system_gi() is False
    assert packaging.SYSTEM_GI_DIR not in sys.path

    monkeypatch.setattr(packaging, "is_packaged", lambda: True)
    monkeypatch.setattr(packaging, "IS_WINDOWS", True)
    assert packaging.expose_system_gi() is False
    assert packaging.SYSTEM_GI_DIR not in sys.path
