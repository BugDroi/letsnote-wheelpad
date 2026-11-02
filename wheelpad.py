#!/usr/bin/env python3
"""Let's Note ホイールパッド（外周スクロール）を Wayland / libinput で使うデーモン.

物理タッチパッドを evdev で grab し、

  * 中央付近のタッチ … そのまま仮想タッチパッドへ転送
  * 外周（デッドゾーンより外）を円を描くようになぞる … スクロールに変換

という処理を行う。スクロールは REL_WHEEL_HI_RES を使った高解像度イベントとして
別の仮想デバイスに書き出すので、Hyprland / Sway などの libinput 系
コンポジタでそのまま動く。

設定は TOML。/etc/wheelpad/config.toml を参照。
"""

from __future__ import annotations

import argparse
import copy
import errno
import logging
import math
import os
import select
import signal
import sys
import threading
import time
import tomllib
from typing import Callable

import evdev
from evdev import InputDevice, UInput, ecodes

LOG = logging.getLogger("wheelpad")

DEFAULT_CONFIG: dict = {
    "device": {
        # 部分一致で探す。空文字なら最初に見つかったタッチパッドを使う。
        "name": "Synaptics TM3562-003",
        # /dev/input/eventX を直接指定したい場合に設定（name より優先）
        "path": "",
        # 仮想デバイスの名前。Hyprland の device[...] 指定にも使われる。
        "pad_name": "LetsNote-Virtual-Touchpad",
        "scroll_name": "LetsNote-Virtual-Wheel",
    },
    "wheelpad": {
        # -1 なら ABS 範囲の中央を自動で使う
        "center_x": -1,
        "center_y": -1,
        # 中心からこの距離より外に触れたらスクロールモード。
        # 0 なら deadzone_ratio を使う。
        "deadzone": 0,
        # パッド半径（短い方）に対するデッドゾーンの割合
        "deadzone_ratio": 0.74,
        # スクロール 1 ステップに必要な累積角度 (rad)
        "sensitivity": 0.30,
        # HI_RES 1 ステップの値。120 = 1 ノッチ
        "hires_step": 60,
        # スクロール方向を反転（ナチュラルスクロール）
        "natural_scroll": False,
        # この本数以上の指でなぞったら水平スクロール
        "horizontal_fingers": 2,
    },
    "speed": {
        # 角速度 (rad/s) が velocity 以上なら multiplier 倍
        "thresholds": [
            {"velocity": 8.0, "multiplier": 4.0},
            {"velocity": 4.0, "multiplier": 3.0},
            {"velocity": 2.0, "multiplier": 2.0},
        ],
    },
    "inertia": {
        "enabled": False,
        "friction": 0.92,
        "min_velocity": 0.5,
        "interval": 0.016,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        with open(path, "rb") as fh:
            cfg = _merge(cfg, tomllib.load(fh))
    cfg["speed"]["thresholds"] = sorted(
        cfg["speed"]["thresholds"], key=lambda t: -float(t["velocity"])
    )
    return cfg


def _normalize_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


class Gesture:
    """1 回のタッチ（指が触れてから離すまで）の状態。"""

    __slots__ = (
        "mode",
        "primary",
        "last_angle",
        "last_time",
        "accum",
        "velocity",
        "direction",
        "horizontal",
    )

    def __init__(self) -> None:
        self.mode: str | None = None  # None = 判定中, "pad", "scroll"
        self.primary = -1
        self.last_angle = 0.0
        self.last_time = 0.0
        self.accum = 0.0
        self.velocity = 0.0
        self.direction = 0
        self.horizontal = False


class WheelpadProcessor:
    """evdev イベント列を解釈して、転送 or スクロールに振り分ける.

    evdev のデバイスに直接依存しないので、単体テスト（--self-test）でも使える。
    """

    def __init__(
        self,
        cfg: dict,
        center: tuple[float, float],
        deadzone: float,
        forward: Callable[[list], None],
        scroll: Callable[[int, float, bool], None],
        on_touch_start: Callable[[], None] | None = None,
        on_gesture_end: Callable[[Gesture], None] | None = None,
    ) -> None:
        wp = cfg["wheelpad"]
        self.center = center
        self.deadzone = deadzone
        self.sensitivity = float(wp["sensitivity"])
        self.natural_scroll = bool(wp["natural_scroll"])
        self.horizontal_fingers = int(wp["horizontal_fingers"])
        self.thresholds = cfg["speed"]["thresholds"]

        self._forward = forward
        self._scroll = scroll
        self._on_touch_start = on_touch_start
        self._on_gesture_end = on_gesture_end

        # MT 状態
        self.slot = 0
        self.track: dict[int, int] = {}
        self.mt_x: dict[int, int] = {}
        self.mt_y: dict[int, int] = {}
        self.single_x: int | None = None
        self.single_y: int | None = None
        self.btn_touch = False

        self.gesture: Gesture | None = None
        self.pending: list[list] = []
        self.frame: list = []
        self.has_mt = False

    # ------------------------------------------------------------------ 入力
    def feed(self, event) -> None:
        if event.type == ecodes.EV_SYN:
            if event.code == ecodes.SYN_DROPPED:
                # 取りこぼし。状態を捨てて再同期する
                self.frame.clear()
                self.track.clear()
                self.mt_x.clear()
                self.mt_y.clear()
                self.btn_touch = False
                self.gesture = None
                self.pending.clear()
                return
            if event.code != ecodes.SYN_REPORT:
                return
            frame, self.frame = self.frame, []
            if frame:
                self._handle_frame(frame)
            return
        self.frame.append(event)

    def _apply(self, event) -> None:
        if event.type == ecodes.EV_ABS:
            code = event.code
            if code == ecodes.ABS_MT_SLOT:
                self.slot = event.value
            elif code == ecodes.ABS_MT_TRACKING_ID:
                if event.value < 0:
                    self.track.pop(self.slot, None)
                    self.mt_x.pop(self.slot, None)
                    self.mt_y.pop(self.slot, None)
                else:
                    self.has_mt = True
                    self.track[self.slot] = event.value
                    self.mt_x.pop(self.slot, None)
                    self.mt_y.pop(self.slot, None)
            elif code == ecodes.ABS_MT_POSITION_X:
                self.mt_x[self.slot] = event.value
            elif code == ecodes.ABS_MT_POSITION_Y:
                self.mt_y[self.slot] = event.value
            elif code == ecodes.ABS_X:
                self.single_x = event.value
            elif code == ecodes.ABS_Y:
                self.single_y = event.value
        elif event.type == ecodes.EV_KEY:
            if event.code == ecodes.BTN_TOUCH:
                self.btn_touch = bool(event.value)

    # ------------------------------------------------------------ フレーム処理
    def _handle_frame(self, frame: list) -> None:
        for event in frame:
            self._apply(event)

        contacts = len(self.track) if self.has_mt else (1 if self.btn_touch else 0)
        position = self.position()
        primary = min(self.track) if self.track else -1

        if self.gesture is None and contacts > 0:
            self.gesture = Gesture()
            self.pending = []
            if self._on_touch_start:
                self._on_touch_start()

        gesture = self.gesture
        if gesture is None:
            self._forward(frame)
            return

        if gesture.mode is None:
            self.pending.append(frame)
            if position is not None:
                self._decide_mode(gesture, position, primary)
            return

        if gesture.mode == "scroll":
            if position is not None and contacts > 0:
                self._scroll_step(gesture, position, contacts, primary)
        else:
            self._forward(frame)

        if contacts == 0:
            self._finish_gesture(gesture)

    def _decide_mode(
        self, gesture: Gesture, position: tuple[int, int], primary: int
    ) -> None:
        distance = math.hypot(position[0] - self.center[0], position[1] - self.center[1])
        gesture.mode = "scroll" if distance > self.deadzone else "pad"
        if gesture.mode == "pad":
            for buffered in self.pending:
                self._forward(buffered)
        self.pending = []
        gesture.primary = primary
        gesture.last_angle = self._angle(position)
        gesture.last_time = time.monotonic()
        LOG.debug(
            "touch at (%.0f, %.0f) dist=%.0f -> %s", position[0], position[1], distance, gesture.mode
        )

    def _finish_gesture(self, gesture: Gesture) -> None:
        if gesture.mode is None and self.pending:
            # 座標が来ないまま指が離れた
            for buffered in self.pending:
                self._forward(buffered)
        if gesture.mode == "scroll" and self._on_gesture_end:
            self._on_gesture_end(gesture)
        self.gesture = None
        self.pending = []

    # ------------------------------------------------------------------ 角度
    def _angle(self, position: tuple[int, int]) -> float:
        return math.atan2(position[1] - self.center[1], position[0] - self.center[0])

    def _scroll_step(
        self, gesture: Gesture, position: tuple[int, int], contacts: int, primary: int
    ) -> None:
        angle = self._angle(position)
        now = time.monotonic()

        if primary != gesture.primary:
            # 最初に触れていた指が離れて別の指が主 contact になった。
            # 角度が飛ぶので差分は取らずに基準だけ更新する。
            gesture.primary = primary
            gesture.last_angle = angle
            gesture.last_time = now
            gesture.accum = 0.0
            return

        delta = _normalize_angle(angle - gesture.last_angle)
        dt = now - gesture.last_time
        gesture.last_angle = angle
        gesture.last_time = now

        if dt <= 0.0 or delta == 0.0:
            return

        gesture.accum += abs(delta)
        if gesture.accum < self.sensitivity:
            return
        gesture.accum = 0.0

        velocity = abs(delta) / dt
        multiplier = 1.0
        for threshold in self.thresholds:
            if velocity >= float(threshold["velocity"]):
                multiplier = float(threshold["multiplier"])
                break

        direction = 1 if delta > 0 else -1
        if self.natural_scroll:
            direction = -direction
        horizontal = contacts >= self.horizontal_fingers

        gesture.velocity = velocity
        gesture.direction = direction
        gesture.horizontal = horizontal

        LOG.debug(
            "scroll dir=%+d vel=%.2f rad/s mult=%.1f horizontal=%s",
            direction,
            velocity,
            multiplier,
            horizontal,
        )
        self._scroll(direction, multiplier, horizontal)

    # ---------------------------------------------------------------- 位置取得
    def position(self) -> tuple[int, int] | None:
        if self.track:
            slot = min(self.track)
            x, y = self.mt_x.get(slot), self.mt_y.get(slot)
            if x is not None and y is not None:
                return x, y
        if self.single_x is not None and self.single_y is not None:
            return self.single_x, self.single_y
        return None


class InertiaEngine:
    """指を離したあとの慣性スクロール。"""

    def __init__(self, cfg: dict, emit: Callable[[int, float, bool], None]) -> None:
        self.enabled = bool(cfg["enabled"])
        self.friction = float(cfg["friction"])
        self.min_velocity = float(cfg["min_velocity"])
        self.interval = float(cfg["interval"])
        self._emit = emit
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self._stop.set()

    def start(self, gesture: Gesture) -> None:
        if not self.enabled or gesture.direction == 0:
            return
        if gesture.velocity < self.min_velocity:
            return
        with self._lock:
            self._stop.set()
            stop = threading.Event()
            self._stop = stop
            thread = threading.Thread(
                target=self._run,
                args=(gesture.velocity, gesture.direction, gesture.horizontal, stop),
                daemon=True,
                name="wheelpad-inertia",
            )
            thread.start()

    def _run(self, velocity: float, direction: int, horizontal: bool, stop: threading.Event) -> None:
        initial = velocity
        while velocity > self.min_velocity and not stop.is_set():
            scale = velocity / initial
            self._emit(direction, max(1.0, scale * 2.0), horizontal)
            velocity *= self.friction
            stop.wait(self.interval)


class WheelpadDaemon:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.device = find_device(cfg)
        self.stop = threading.Event()
        self._acc = {False: 0, True: 0}

        self.absinfo = {
            code: info for code, info in self.device.capabilities(absinfo=True).get(ecodes.EV_ABS, [])
        }
        center, deadzone = resolve_geometry(cfg, self.absinfo)

        self.pad = create_pad_device(self.device, cfg["device"]["pad_name"])
        self.scroll_dev = create_scroll_device(self.device, cfg["device"]["scroll_name"])

        self.inertia = InertiaEngine(cfg["inertia"], self.emit_scroll)
        self.processor = WheelpadProcessor(
            cfg,
            center=center,
            deadzone=deadzone,
            forward=self.forward,
            scroll=self.emit_scroll,
            on_touch_start=self.inertia.cancel,
            on_gesture_end=self.inertia.start,
        )

        LOG.info("device: %s (%s)", self.device.name, self.device.path)
        LOG.info("center=(%.0f, %.0f) deadzone=%.0f", center[0], center[1], deadzone)

    # ------------------------------------------------------------------ 出力
    def forward(self, frame: list) -> None:
        for event in frame:
            self.pad.write(event.type, event.code, event.value)
        self.pad.syn()

    def emit_scroll(self, direction: int, multiplier: float, horizontal: bool) -> None:
        hires_step = int(self.cfg["wheelpad"]["hires_step"])
        delta = direction * max(1, int(round(hires_step * multiplier)))
        if horizontal:
            code_hi, code_lo = ecodes.REL_HWHEEL_HI_RES, ecodes.REL_HWHEEL
        else:
            code_hi, code_lo = ecodes.REL_WHEEL_HI_RES, ecodes.REL_WHEEL

        self.scroll_dev.write(ecodes.EV_REL, code_hi, delta)

        # hi-res 非対応のアプリ/環境向けに、120 単位で 1 ノッチを合成する
        acc = self._acc[horizontal] + delta
        while acc >= 120:
            self.scroll_dev.write(ecodes.EV_REL, code_lo, 1)
            acc -= 120
        while acc <= -120:
            self.scroll_dev.write(ecodes.EV_REL, code_lo, -1)
            acc += 120
        self._acc[horizontal] = acc
        self.scroll_dev.syn()

    # ------------------------------------------------------------------- 実行
    def run(self) -> None:
        try:
            self.device.grab()
            LOG.info("grabbed. ready.")
            while not self.stop.is_set():
                try:
                    ready, _, _ = select.select([self.device.fd], [], [], 0.5)
                except InterruptedError:
                    continue
                if not ready:
                    continue
                try:
                    events = self.device.read()
                except OSError as exc:
                    if exc.errno in (errno.ENODEV, errno.EBADF):
                        LOG.warning("device removed, exiting")
                        break
                    raise
                for event in events:
                    self.processor.feed(event)
        finally:
            self.close()

    def close(self) -> None:
        self.inertia.cancel()
        for obj, name in ((self.device, "device"), (self.scroll_dev, "scroll"), (self.pad, "pad")):
            try:
                if name == "device":
                    self.device.ungrab()
                obj.close()
            except Exception:  # noqa: BLE001 - 終了処理なので握りつぶす
                pass


# ---------------------------------------------------------------- デバイス探索
def _abs_codes(device: InputDevice) -> set[int]:
    return {code for code, _ in device.capabilities().get(ecodes.EV_ABS, [])}


def find_device(cfg: dict) -> InputDevice:
    explicit = (cfg["device"].get("path") or "").strip()
    if explicit:
        return InputDevice(explicit)

    wanted = (cfg["device"].get("name") or "").strip().lower()
    candidates: list[InputDevice] = []
    for path in evdev.list_devices():
        try:
            device = InputDevice(path)
        except OSError:
            continue
        codes = _abs_codes(device)
        is_touchpad = ecodes.ABS_MT_POSITION_X in codes or ecodes.ABS_X in codes
        if not is_touchpad or (wanted and wanted not in device.name.lower()):
            device.close()
            continue
        candidates.append(device)

    if not candidates:
        raise SystemExit(
            f"タッチパッドが見つかりません (name={wanted!r})。"
            "`wheelpad.py --list-devices` で確認してください。"
        )

    candidates.sort(key=lambda d: ecodes.ABS_MT_POSITION_X not in _abs_codes(d))
    chosen = candidates[0]
    for other in candidates[1:]:
        other.close()
    return chosen


def resolve_geometry(cfg: dict, absinfo: dict) -> tuple[tuple[float, float], float]:
    wp = cfg["wheelpad"]
    xinfo = absinfo.get(ecodes.ABS_MT_POSITION_X) or absinfo.get(ecodes.ABS_X)
    yinfo = absinfo.get(ecodes.ABS_MT_POSITION_Y) or absinfo.get(ecodes.ABS_Y)
    if xinfo is None or yinfo is None:
        raise SystemExit("パッドの ABS 範囲が取得できません")

    center_x = float(xinfo.min + xinfo.max) / 2.0
    center_y = float(yinfo.min + yinfo.max) / 2.0
    if float(wp["center_x"]) >= 0:
        center_x = float(wp["center_x"])
    if float(wp["center_y"]) >= 0:
        center_y = float(wp["center_y"])

    radius = min((xinfo.max - xinfo.min) / 2.0, (yinfo.max - yinfo.min) / 2.0)
    deadzone = float(wp["deadzone"])
    if deadzone <= 0:
        deadzone = radius * float(wp["deadzone_ratio"])
    return (center_x, center_y), deadzone


def _input_props():
    return [getattr(ecodes, "INPUT_PROP_POINTER", 0x00)]


def create_pad_device(source: InputDevice, name: str) -> UInput:
    caps = source.capabilities(absinfo=True)
    events = {
        ev_type: caps[ev_type]
        for ev_type in (ecodes.EV_KEY, ecodes.EV_ABS)
        if caps.get(ev_type)
    }
    kwargs = dict(
        name=name,
        vendor=source.info.vendor,
        product=source.info.product,
        version=source.info.version,
        bustype=source.info.bustype,
        phys=f"{source.phys or 'wheelpad'}/wheelpad",
    )
    try:
        return UInput(events, input_props=_input_props(), **kwargs)
    except TypeError:  # 古い python-evdev
        return UInput(events, **kwargs)


def create_scroll_device(source: InputDevice, name: str) -> UInput:
    caps = {
        ecodes.EV_REL: [
            ecodes.REL_WHEEL,
            ecodes.REL_WHEEL_HI_RES,
            ecodes.REL_HWHEEL,
            ecodes.REL_HWHEEL_HI_RES,
        ]
    }
    return UInput(
        caps,
        name=name,
        vendor=source.info.vendor,
        product=source.info.product,
        version=source.info.version,
        bustype=source.info.bustype,
        phys=f"{source.phys or 'wheelpad'}/wheelpad-scroll",
    )


# ------------------------------------------------------------------- 自己診断
def list_devices() -> None:
    for path in evdev.list_devices():
        try:
            device = InputDevice(path)
        except OSError as exc:
            print(f"{path}: (open error: {exc})")
            continue
        abs_codes = _abs_codes(device)
        marker = "touchpad" if ecodes.ABS_MT_POSITION_X in abs_codes else ""
        print(f"{path}: {device.name} {marker}".rstrip())
        device.close()


def probe(cfg: dict) -> None:
    device = find_device(cfg)
    print(f"device : {device.name} ({device.path})")
    caps = device.capabilities(absinfo=True)
    for code, info in caps.get(ecodes.EV_ABS, []):
        try:
            label = ecodes.ABS[code]
        except (KeyError, IndexError):
            label = str(code)
        print(
            f"  ABS {label:22s} min={info.min:<6d} max={info.max:<6d} "
            f"fuzz={info.fuzz:<4d} flat={info.flat:<4d} res={info.resolution}"
        )
    center, deadzone = resolve_geometry(cfg, {c: i for c, i in caps.get(ecodes.EV_ABS, [])})
    print(f"center : ({center[0]:.0f}, {center[1]:.0f})")
    print(f"deadzone: {deadzone:.0f}")
    device.close()


def self_test() -> int:
    """合成イベントでロジックだけを検証する。"""

    class Ev:
        __slots__ = ("type", "code", "value")

        def __init__(self, type_, code, value):
            self.type, self.code, self.value = type_, code, value

    cfg = load_config(None)
    cfg["wheelpad"]["deadzone"] = 0
    cfg["wheelpad"]["deadzone_ratio"] = 0.75
    cfg["wheelpad"]["sensitivity"] = 0.3
    center, deadzone = (264.0, 264.0), 198.0

    forwarded: list = []
    scrolled: list = []
    proc = WheelpadProcessor(
        cfg, center, deadzone, forward=lambda f: forwarded.append(f), scroll=lambda d, m, h: scrolled.append((d, m, h))
    )

    def touch(x, y, contacts=1):
        proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, 0))
        proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, 100))
        proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, int(x)))
        proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, int(y)))
        proc.feed(Ev(ecodes.EV_KEY, ecodes.BTN_TOUCH, 1))
        proc.feed(Ev(ecodes.EV_SYN, ecodes.SYN_REPORT, 0))

    def move(x, y, slot=0):
        proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot))
        proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, int(x)))
        proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, int(y)))
        proc.feed(Ev(ecodes.EV_SYN, ecodes.SYN_REPORT, 0))

    def lift(*slots):
        for slot in slots or (0,):
            proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot))
            proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, -1))
        proc.feed(Ev(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0))
        proc.feed(Ev(ecodes.EV_SYN, ecodes.SYN_REPORT, 0))

    # 1) 中央タッチはそのまま転送される
    touch(264, 264)
    move(300, 260)
    lift()
    assert forwarded, "central touch should be forwarded"
    assert not scrolled, "central touch must not scroll"

    # 2) 外周を時計回り → 下方向スクロール
    forwarded.clear()
    scrolled.clear()
    touch(480, 264)  # 右端
    for degree in range(0, 180, 6):  # 角度を進める（y 下向き = 時計回り）
        rad = math.radians(degree)
        move(264 + 216 * math.cos(rad), 264 + 216 * math.sin(rad))
        time.sleep(0.002)
    lift()
    assert not forwarded, "edge touch must not be forwarded to the pad"
    assert scrolled, "edge drag should scroll"
    assert all(d == 1 for d, _, _ in scrolled), f"clockwise should scroll down: {scrolled}"

    # 3) 反時計回り → 上方向
    scrolled.clear()
    touch(48, 264)  # 左端（180 度）から反時計回り
    for degree in range(180, 0, -6):
        rad = math.radians(degree)
        move(264 + 216 * math.cos(rad), 264 + 216 * math.sin(rad))
        time.sleep(0.002)
    lift()
    assert scrolled and all(d == -1 for d, _, _ in scrolled), f"counter-clockwise should scroll up: {scrolled}"

    # 4) 2 本指なら水平スクロール
    scrolled.clear()
    touch(480, 264)
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, 1))
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, 101))
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, 470))
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, 264))
    proc.feed(Ev(ecodes.EV_SYN, ecodes.SYN_REPORT, 0))
    for degree in range(0, 180, 6):
        rad = math.radians(degree)
        move(264 + 216 * math.cos(rad), 264 + 216 * math.sin(rad))
        time.sleep(0.002)
    lift(1, 0)
    assert scrolled and all(h for _, _, h in scrolled), f"two fingers should scroll horizontally: {scrolled}"

    # 5) 主 contact が切り替わっても余計なスクロールが出ない
    scrolled.clear()
    touch(480, 264)
    for degree in range(0, 60, 6):
        rad = math.radians(degree)
        move(264 + 216 * math.cos(rad), 264 + 216 * math.sin(rad))
        time.sleep(0.002)
    # 2 本目（正反対の位置）を追加
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, 1))
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, 101))
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, 48))
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, 264))
    proc.feed(Ev(ecodes.EV_SYN, ecodes.SYN_REPORT, 0))
    count_before = len(scrolled)
    # 1 本目だけ離す → 主 contact が 0 から 1 へ飛ぶ
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, 0))
    proc.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, -1))
    proc.feed(Ev(ecodes.EV_SYN, ecodes.SYN_REPORT, 0))
    assert len(scrolled) == count_before, f"primary switch must not scroll: {scrolled[count_before:]}"
    lift(1)

    print("self-test: OK")
    return 0


# ----------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Let's Note ホイールパッド スクロールデーモン")
    parser.add_argument("-c", "--config", default="/etc/wheelpad/config.toml")
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--probe", action="store_true", help="デバイスの ABS 範囲と中心/デッドゾーンを表示")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.self_test:
        return self_test()
    if args.list_devices:
        list_devices()
        return 0

    config_path = args.config
    if config_path and not os.path.exists(config_path):
        LOG.warning("config not found: %s (using defaults)", config_path)
        config_path = None
    cfg = load_config(config_path)

    if args.probe:
        probe(cfg)
        return 0

    daemon = WheelpadDaemon(cfg)

    def _shutdown(signum, _frame):
        LOG.info("signal %s received, shutting down", signum)
        daemon.stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
