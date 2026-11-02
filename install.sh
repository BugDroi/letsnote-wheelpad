#!/usr/bin/env bash
# Let's Note ホイールパッド デーモンのインストーラ
#   sudo ./install.sh
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "sudo で実行してください: sudo $0" >&2
  exit 1
fi

SRC=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

echo "==> 依存パッケージ (python-evdev)"
if python3 -c 'import evdev' 2>/dev/null; then
  echo "    すでにインストール済み"
elif command -v pacman >/dev/null; then
  pacman -S --needed --noconfirm python-evdev
else
  cat >&2 <<'EOM'
python-evdev が必要です。お使いのディストリビューションで導入してください:
  Debian/Ubuntu : sudo apt install python3-evdev
  Fedora        : sudo dnf install python3-evdev
  pip           : sudo python3 -m pip install evdev
EOM
  exit 1
fi

echo "==> 本体を配置"
install -Dm755 "${SRC}/wheelpad.py" /usr/local/bin/wheelpad.py
install -Dm644 "${SRC}/wheelpad.service" /etc/systemd/system/wheelpad.service
install -Dm644 "${SRC}/99-wheelpad.rules" /etc/udev/rules.d/99-wheelpad.rules
udevadm control --reload-rules

echo "==> 設定ファイル"
install -d -m755 /etc/wheelpad
if [[ -e /etc/wheelpad/config.toml ]]; then
  echo "    既存の /etc/wheelpad/config.toml はそのまま残します"
else
  install -Dm644 "${SRC}/config.toml" /etc/wheelpad/config.toml
  echo "    /etc/wheelpad/config.toml を作成しました"
fi

echo "==> デバイス検出"
/usr/bin/python3 /usr/local/bin/wheelpad.py --config /etc/wheelpad/config.toml --probe || true

echo "==> systemd サービスを有効化"
systemctl daemon-reload
systemctl enable wheelpad.service >/dev/null
systemctl restart wheelpad.service

sleep 1
systemctl --no-pager --full status wheelpad.service || true

cat <<'EOS'

完了しました。タッチパッドの外周を円を描くようになぞるとスクロールします。
うまく動かないときは:
  sudo wheelpad.py --debug            # 前面で実行してログを見る
  journalctl -u wheelpad -f           # サービス版のログ
EOS
