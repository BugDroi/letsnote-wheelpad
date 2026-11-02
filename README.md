# letsnote-wheelpad

Panasonic Let's Note のタッチパッド外周（ホイールパッド）を、Wayland / libinput 環境で
スクロールとして使えるようにする小さなデーモンです。

Let's Note のタッチパッドは縁を円を描くようになぞるとスクロールできますが、
これは Xorg 時代の libinput/synaptics ドライバの機能で、**Wayland（Hyprland / Sway など）
では動きません**。このデーモンは evdev でタッチパッドを直接読み、外周の円運動を
角度として解釈してスクロールイベントに変換します。

> A small daemon that brings back the "wheelpad" (scroll-by-drawing-circles-on-the-edge)
> feature of Panasonic Let's Note touchpads on Wayland / libinput, where it normally
> doesn't work. Pure Python + evdev, no compositor patches required.

## 対応環境

- Linux + evdev + `/dev/uinput`
- Wayland コンポジタ（libinput 経由）: Hyprland / Sway など
- Python 3.11+（`tomllib` を使うため）
- 動作確認: **Let's Note CF-SV1 (CF-SV1RDLAS) / Synaptics TM3562-003 / Omarchy 4 / Hyprland**

他の Let's Note でも、`config.toml` の中心座標とデッドゾーンを調整すれば動くはずです。

## 仕組み

```
物理タッチパッド (Synaptics TM3562-003)
   │  evdev で grab（他のアプリからは見えなくなる）
   ▼
wheelpad.py
   ├─ 中央のタッチ ──► LetsNote-Virtual-Touchpad へそのまま転送
   └─ 外周のタッチ ──► 角度を計算 ──► LetsNote-Virtual-Wheel へスクロールとして出力
```

- タッチ開始位置が中心から `deadzone` より外なら、指を離すまでそのジェスチャは
  スクロールモード。中央のタッチは今まで通り仮想タッチパッドへ流すので、
  タップ・2 本指スクロール・3 本指ジェスチャはそのまま使えます。
- 時計回り＝下、反時計回り＝上。2 本指でなぞると水平スクロール。
- 角速度 (rad/s) に応じてスクロール量の倍率が変わります。
- `REL_WHEEL_HI_RES` によるスムーズスクロール。120 単位で通常のノッチも合成します。
- 指を離した後の慣性スクロール（任意）。

## インストール

```bash
git clone https://github.com/BugDroi/letsnote-wheelpad.git
cd letsnote-wheelpad
sudo ./install.sh
```

`install.sh` は以下を行います。

1. `python-evdev` を導入（Arch 系は pacman、それ以外は導入済みか確認）
2. `/usr/local/bin/wheelpad.py`、`/etc/wheelpad/config.toml`、
   `/etc/systemd/system/wheelpad.service` を配置
3. `/etc/udev/rules.d/99-wheelpad.rules` を配置
   （仮想タッチパッドを「内蔵」と認識させ、パーム検出と
   「入力中はタッチパッドを無効化」を効かせる）
4. `wheelpad.service` を `enable --now`

インストール時に `--probe` が走り、検出したデバイスと ABS 範囲・中心・デッドゾーンを
表示します。

### 手動で入れる場合

```bash
sudo cp wheelpad.py /usr/local/bin/
sudo install -Dm644 wheelpad.service /etc/systemd/system/wheelpad.service
sudo install -Dm644 99-wheelpad.rules /etc/udev/rules.d/99-wheelpad.rules
sudo install -Dm644 config.toml /etc/wheelpad/config.toml
sudo udevadm control --reload-rules
sudo systemctl daemon-reload
sudo systemctl enable --now wheelpad
```

## 設定

`/etc/wheelpad/config.toml`。主なもの:

| キー | 意味 |
| --- | --- |
| `device.name` | タッチパッド名（部分一致） |
| `wheelpad.deadzone_ratio` | 中心から半径の何割より外を「外周」とみなすか（既定 0.74） |
| `wheelpad.deadzone` | 絶対値で指定したい場合（0 なら上を使う） |
| `wheelpad.sensitivity` | スクロール 1 ステップに必要な累積角度 (rad) |
| `wheelpad.hires_step` | 1 ステップの HI_RES 値（120 = 1 ノッチ） |
| `wheelpad.natural_scroll` | 方向反転 |
| `speed.thresholds` | 角速度ごとの倍率 |
| `inertia.enabled` | 指を離した後の慣性スクロール |

変更したら `sudo systemctl restart wheelpad`。

`center_x` / `center_y` は `-1` で ABS 範囲の中央を自動計算します。通常は触る必要は
ありませんが、実機で中央がずれる場合だけ数値で指定してください。

## 調整

```bash
# デバイスの ABS 範囲・中心・デッドゾーンを表示
sudo wheelpad.py --probe

# 前面で実行してログを見る（サービスは止めておく）
sudo systemctl stop wheelpad
sudo wheelpad.py --debug

# どんな入力デバイスがあるか
sudo wheelpad.py --list-devices
```

`--debug` はタッチ位置・モード・角速度・倍率を出力します。

| 症状 | 調整 |
| --- | --- |
| 中央を触ったのにスクロールしてしまう | `deadzone_ratio` を上げる |
| 外周をなぞってもスクロールしない | `deadzone_ratio` を下げる / `sensitivity` を下げる |
| スクロールが速すぎる | `hires_step` を下げる（60 → 40 など） |
| 方向が逆 | `natural_scroll = true` |
| 反応が渋い | `sensitivity` を下げる（0.30 → 0.20） |

ログ: `journalctl -u wheelpad -f`

## 動作確認

```bash
systemctl status wheelpad
hyprctl devices | grep -A2 LetsNote
```

`LetsNote-Virtual-Touchpad` と `LetsNote-Virtual-Wheel` が見えていれば OK です。

- スクロール用の仮想デバイスはホイールしか持たないため、libinput からは
  「キーボード兼ポインタ」として見えます（`hyprctl devices` の keyboards 側に出ます）。
  キーイベントは一切出さないので実害はありません。
- 仮想タッチパッド名に `Touchpad` を含めているので、Omarchy の
  `omarchy-toggle-touchpad` もこのデバイスを対象にできます。

## アンインストール

```bash
sudo ./uninstall.sh
```

## 注意

- 物理タッチパッドはデーモンが `grab()` するため、デーモンが落ちている間は
  タッチパッドが反応しません。`Restart=always` で自動復帰します。
- Hyprland 側で `input.touchpad.natural_scroll = true` にしている場合は、
  `config.toml` の `natural_scroll` も `true` にしないと方向が揃いません
  （スクロールは別デバイスから出るため、Hyprland の設定は適用されません）。
- root で動く systemd サービスです（`/dev/input/*` と `/dev/uinput` の読み書きが必要）。

## 技術メモ

タッチ位置の判定:

```python
dist = math.hypot(x - center_x, y - center_y)
if dist > deadzone:
    scroll_mode = True
```

円運動の角度差:

```python
current_angle = math.atan2(y - center_y, x - center_x)
diff = normalize(current_angle - last_angle)   # -π ~ π
```

`diff` の符号で方向、`|diff| / dt` で角速度を求め、しきい値ごとに倍率を切り替えます。
スクロール量は角度の累積が `sensitivity` を超えるたびに 1 ステップ出力します。

## 開発

```bash
# 合成イベントによるロジックの自己テスト（実機・uinput 不要）
python3 wheelpad.py --self-test
```

`WheelpadProcessor` は evdev のデバイスに依存しないので、イベント列を直接
流し込んでテストできます。

## 謝辞

アイデアと設計は kuroiko さんの記事
「[Waylandでも Let's Note のホイールパッドをスクロールに使う](https://zenn.dev/kuroikotech/articles/wheelpad-daemon)」
によるものです。記事で紹介されていた実装は現在参照できないため、
このリポジトリは記事の設計を元に一から実装し直したものです。

## ライセンス

MIT
