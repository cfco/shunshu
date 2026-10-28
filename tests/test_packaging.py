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
