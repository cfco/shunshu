#!/usr/bin/env bash
# Ubuntu 安装：apt 装系统侧依赖 → 用系统 python3 建 --system-site-packages 的 venv → pip 装 wheel。
#
# Linux 不再发冻结好的二进制：Nuitka 会把编译机那份 gi（PyGObject）连同系统的 typelib
# 快照一起打进产物，运行时还把 GI_TYPELIB_PATH 指到那份快照上。换一台发行版更新的机器，
# 快照里的 typelib 对上本机新的 GTK3 库，就会崩成一句没有文字的 AssertionError
# （v0.16.0 在 Ubuntu 26.10 上就是这个现象）。gi 本来也只能由 apt 提供、跟着系统走，
# 所以托盘这条路只能用系统解释器。
#
# 用法（正常安装，取最新 Release）：
#   bash <(curl -fsSL https://github.com/cfco/shunshu/releases/download/v0.17.0/install-linux.sh)
# CI 自测用本地 wheel，不去下载：
#   SHUNSHU_WHEEL=dist-wheel/shunshu-0.17.0-py3-none-any.whl bash scripts/install-linux.sh
#
# 不用 set -e：自检那步 timeout 正常返回 124，会被 -e 当成失败当场中止。
set -uo pipefail

REPO="${SHUNSHU_REPO:-cfco/shunshu}"
VERSION="${1:-${SHUNSHU_VERSION:-}}"
WHEEL="${SHUNSHU_WHEEL:-}"
VENV="${SHUNSHU_VENV:-$HOME/.local/share/shunshu/venv}"
DATA="${SHUNSHU_DATA_DIR:-$HOME/.local/share/shunshu/data}"
SELFCHK_LOG=/tmp/shunshu-selfcheck.log

# 少了哪样就缺哪块功能：gi/gtk3/appindicator = 右键菜单，tk = 卡片，xlib = 兜底图标，
# xclip/wl-clipboard = 从剪贴板取网址，xdg-utils = 点章节开浏览器，noto-cjk = 卡片上的中文。
# gir1.2-freedesktop 单独列出来是因为 DBus-1.0.typelib 由它带（apt 里挂在 gir1.2-gtk-3.0 的
# 依赖里，不写也装得到）：pystray/_util/gtk.py 在 import 时就无条件 from . import notify_dbus，
# 那个模块 require_version('DBus','1.0')，缺了整条 appindicator/gtk 路当场断。
APT_PKGS="python3-venv python3-tk python3-gi python3-xlib gir1.2-gtk-3.0
          gir1.2-ayatanaappindicator3-0.1 gir1.2-freedesktop
          xclip wl-clipboard xdg-utils fonts-noto-cjk"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
  command -v sudo >/dev/null 2>&1 || { echo "不是 root 又没装 sudo，装不了 apt 依赖"; exit 1; }
fi

command -v apt-get >/dev/null 2>&1 || { echo "本机没有 apt-get（这不是 Ubuntu/Debian？）"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "本机没有 python3"; exit 1; }
echo "解释器：$(python3 --version 2>&1)"

echo "[1/4] apt 装系统侧依赖"
$SUDO apt-get update || echo "apt-get update 失败，先试着按已有的包列表装"
# shellcheck disable=SC2086  # 故意不加引号：这是多个包名
if ! $SUDO apt-get install -y --no-install-recommends $APT_PKGS; then
  # Ubuntu 26.10 把 python3-tk / python3-xlib / 老的 appindicator gir 放进了 universe，
  # 干净镜像里 universe 未必开着，开一次再装
  echo "有包没找到，尝试打开 universe 后重装"
  $SUDO apt-get install -y software-properties-common || true
  $SUDO add-apt-repository -y universe || true
  $SUDO apt-get update || true
  # shellcheck disable=SC2086
  $SUDO apt-get install -y --no-install-recommends $APT_PKGS || {
    echo "系统侧依赖没装齐，托盘和卡片都会有缺。请照上面的包名手动装完再跑这个脚本。"; exit 1; }
fi

echo "[2/4] 用系统 python3 建 venv（--system-site-packages 才看得见 apt 那份 gi）"
python3 -m venv --system-site-packages --clear "$VENV" || { echo "venv 建不起来"; exit 1; }

echo "[3/4] 装 shunshu"
if [ -z "$WHEEL" ]; then
  if [ -z "$VERSION" ]; then
    # 用 releases/latest 的重定向拿版本号，不打 GitHub API（匿名调用很容易被限流）
    VERSION=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" \
              | sed -n 's|.*/tag/v||p')
  fi
  [ -n "$VERSION" ] || { echo "拿不到最新版本号；可以写死：SHUNSHU_VERSION=0.17.0 bash install-linux.sh"; exit 1; }
  url="https://github.com/$REPO/releases/download/v$VERSION/shunshu-$VERSION-py3-none-any.whl"
  echo "下载 $url"
  WHEEL="/tmp/shunshu-$VERSION-py3-none-any.whl"
  curl -fsSL -o "$WHEEL" "$url" || { echo "wheel 下载失败"; exit 1; }
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip || echo "pip 自身没升级，继续用原来那个"
"$VENV/bin/python" -m pip install --quiet "$WHEEL" || { echo "shunshu 或它的依赖装失败"; exit 1; }
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/shunshu" "$HOME/.local/bin/shunshu"
echo "装好：$("$VENV/bin/python" -c 'import shunshu; print("v" + shunshu.__version__)' 2>/dev/null || echo "版本未知")"

echo "[4/4] 自检：真跑 15 秒，看托盘后端到底是哪一个"
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] && ! command -v xvfb-run >/dev/null 2>&1; then
  echo "既没有图形会话也没有 xvfb，跳过自检（装是装好了，直接运行 shunshu 试试）"
  exit 0
fi
cmd=("$HOME/.local/bin/shunshu" run --boot-delay=1)
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  cmd=(xvfb-run -a "${cmd[@]}")
fi
# appindicator 是走会话总线注册的，纯 xvfb 环境（CI 容器）里没有那条总线
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && command -v dbus-run-session >/dev/null 2>&1; then
  cmd=(dbus-run-session -- "${cmd[@]}")
fi
code=0
timeout 15 "${cmd[@]}" >"$SELFCHK_LOG" 2>&1 || code=$?
backend=$(sed -n 's/.*托盘后端: \([^（(]*\).*/\1/p' "$DATA/shunshu.log" 2>/dev/null | tail -1)
if [ "$code" -ne 124 ]; then
  echo "启动没撑到 15 秒（exit=$code），日志尾部如下："
  tail -n 15 "$SELFCHK_LOG"
  tail -n 15 "$DATA/shunshu.log" 2>/dev/null
  exit 1
fi
case "$backend" in
  _appindicator|_gtk)
    echo "托盘后端：$backend（有右键菜单）—— 可以用了" ;;
  _xorg|"")
    echo "托盘后端：${backend:-没写出来}——图标能出但右键菜单没有，添加小说就没有入口"
    echo "多半是 appindicator 的 typelib 或 DBus-1.0 typelib 没装；先确认 apt 那几步没报错，再重跑这个脚本"
    exit 1 ;;
  *)
    echo "托盘后端：$backend（不认识的后端）"; exit 1 ;;
esac

echo
echo "数据目录：$DATA（书架 novels.json、日志都在这儿）"
echo "启动：终端里敲 shunshu（~/.local/bin 不在 PATH 就用 ~/.local/bin/shunshu）"
echo "开机自启：托盘菜单里勾「开机自启」"
if command -v gnome-extensions >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  gnome-extensions list --enabled 2>/dev/null | grep -q appindicator || \
    echo "提示：GNOME 不装 AppIndicator 扩展的话，托盘里连图标都不显示（sudo apt install gnome-shell-extension-appindicator，然后在「扩展」里启用）"
fi
