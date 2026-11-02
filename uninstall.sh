#!/usr/bin/env bash
# アンインストーラ
#   sudo ./uninstall.sh
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "sudo で実行してください: sudo $0" >&2
  exit 1
fi

systemctl disable --now wheelpad.service 2>/dev/null || true
rm -f /etc/systemd/system/wheelpad.service
systemctl daemon-reload
rm -f /usr/local/bin/wheelpad.py
rm -f /etc/udev/rules.d/99-wheelpad.rules
udevadm control --reload-rules

echo "削除しました。設定を消す場合は: sudo rm -rf /etc/wheelpad"
echo "（python-evdev は他の用途で使うかもしれないので残しています）"
