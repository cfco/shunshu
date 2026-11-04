"""发版号规划（scripts/plan_release.py）：每次 push 都发一个没发过的号，不覆盖旧 Release。

这里守的是「同一个 Release 不会被下一次推送悄悄换掉内容」：pyproject 那个号已经打过 tag 时
必须往后走一个空闲号，且 wheel 文件名、deb 的 Version、产物里的 VERSION 三处都得跟着同一个号
——所以改写 pyproject 与 uv.lock 的那两个函数也要测住：锁文件里每个依赖都有一行 version，
全局替换会把不相干的包一起改掉。
"""

from __future__ import annotations

import pytest

from scripts.plan_release import (
    apply_lock_version,
    apply_pyproject_version,
    main,
    next_free,
    read_current_version,
    taken_versions,
)

PYPROJECT = '[project]\nname = "shunshu"\nversion = "0.17.0"\n\n[dependency-groups]\ndev = ["pytest"]\n'
LOCK = '''[[package]]
name = "lxml"
version = "6.1.3"

[[package]]
name = "shunshu"
version = "0.17.0"
source = { editable = "." }

[[package]]
name = "pillow"
version = "13.0.0"
'''
LS_REMOTE = (
    "abc123\trefs/tags/v0.17.0\n"
    "def456\trefs/tags/v0.17.0^{}\n"
    "789abc\trefs/tags/v0.16.0\n"
    "000111\trefs/tags/nuitka-test\n"
)


def test_read_current_version():
    assert read_current_version(PYPROJECT) == "0.17.0"


@pytest.mark.parametrize("used,current,expected", [
    (set(), "0.17.0", "0.17.0"),                      # 没发过：就用 pyproject 那个号
    ({"0.17.0"}, "0.17.0", "0.17.1"),                 # 发过了：往后一个号，别覆盖旧包
    ({"0.17.0", "0.17.1"}, "0.17.0", "0.17.2"),       # 连着推三次就是三个新包
    ({"0.17.9"}, "0.17.9", "0.17.10"),                # patch 进位不回到 0.18.0
    ({"1.0.0"}, "0.99.9", "0.99.9"),                  # 别的版本占着不影响
])
def test_next_free_picks_an_untaken_version(used, current, expected):
    assert next_free(current, used) == expected


def test_next_free_rejects_a_non_three_part_version():
    with pytest.raises(ValueError):
        next_free("0.17", set())


def test_taken_versions_reads_ls_remote_and_ignores_non_version_tags():
    assert taken_versions(LS_REMOTE) == {"0.17.0", "0.16.0"}


def test_apply_pyproject_only_touches_the_project_version():
    text = 'version = "0.9.9"\nx = [version = "9.9.9"]\n'
    assert apply_pyproject_version(text, "1.2.3") == 'version = "1.2.3"\nx = [version = "9.9.9"]\n'


def test_apply_lock_only_touches_the_shunshu_block():
    new, done = apply_lock_version(LOCK, "0.17.4")
    assert done
    assert 'name = "shunshu"\nversion = "0.17.4"' in new
    assert 'version = "6.1.3"' in new          # 别的依赖一个字没动
    assert 'version = "13.0.0"' in new


def test_apply_lock_reports_a_missing_package():
    assert apply_lock_version('[[package]]\nname = "other"\nversion = "1"\n', "0.17.4") == (
        '[[package]]\nname = "other"\nversion = "1"\n', False)


def test_main_prints_the_version_and_applies_the_files(tmp_path, capsys):
    pyproject = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"
    tags = tmp_path / "tags.txt"
    pyproject.write_text(PYPROJECT, encoding="utf-8")
    lock.write_text(LOCK, encoding="utf-8")
    tags.write_text(LS_REMOTE, encoding="utf-8")

    code = main(["--tags", str(tags), "--pyproject", str(pyproject), "--lock", str(lock), "--apply"])

    assert code == 0
    assert capsys.readouterr().out.strip() == "0.17.1"      # v0.17.0 已打过 tag
    assert read_current_version(pyproject.read_text(encoding="utf-8")) == "0.17.1"
    assert 'name = "shunshu"\nversion = "0.17.1"' in lock.read_text(encoding="utf-8")


def test_main_without_apply_leaves_the_files_alone(tmp_path, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT, encoding="utf-8")

    assert main(["--pyproject", str(pyproject)]) == 0
    assert capsys.readouterr().out.strip() == "0.17.0"
    assert read_current_version(pyproject.read_text(encoding="utf-8")) == "0.17.0"


def test_main_set_version_forces_the_plan_jobs_choice(tmp_path, capsys):
    """两个 build job 用 plan 的结论改写自己检出里的文件，不能再各算一次。"""
    pyproject = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"
    pyproject.write_text(PYPROJECT, encoding="utf-8")
    lock.write_text(LOCK, encoding="utf-8")

    code = main(["--set-version", "0.17.1", "--pyproject", str(pyproject), "--lock", str(lock), "--apply"])

    assert code == 0
    assert capsys.readouterr().out.strip() == "0.17.1"
    assert read_current_version(pyproject.read_text(encoding="utf-8")) == "0.17.1"


def test_main_fails_loudly_on_an_unparseable_version(tmp_path, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.17\n', encoding="utf-8")

    assert main(["--pyproject", str(pyproject)]) == 2
    assert "找不到" in capsys.readouterr().err
