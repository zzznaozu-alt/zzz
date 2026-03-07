# log.py
# Windows / Python 3.12+
#
# 目的:
#   ゼンレスゾーンゼロ等を遊びながら「入力履歴(スト6風)」を別ウィンドウに表示する。
#
# 要望対応:
# - 入力イベントが来た瞬間に必ずログを出力（DOWN/UPごと）
# - DualShock4(USB) 対応（RawInput HID: VID_054C を検出してDS4レポート解析）
# - 表示は「積み上げ + 継続フレーム数」(最大99F)。無入力は N を積み上げ。
# - 画面に何も出ない問題を避けるため、まず「普通のウィンドウ表示」を優先（透明/クリック透過なし）
#
# 操作:
# - Ctrl + Shift + Q で終了
#
# NOTE:
# - 排他フルスクリーンのゲーム上に重ねる用途ではなく「別ウィンドウで見える」ことを優先しています。
#   （オーバーレイ重ねは別方式(DX)が必要になることが多い）

import ctypes
import threading
import time
from collections import deque
from dataclasses import dataclass
import tkinter as tk
import traceback
import sys
import argparse
import subprocess
import os
import logging
from typing import Dict, Set, Tuple, Optional
import tkinter.font as tkfont  # もし未importなら追加
from PIL import Image, ImageTk

# ========= 設定 =========
APP_FPS = 60
HISTORY_LINES = 16
# ===== Layout knobs (edit freely) =====
WIN_GEOMETRY = "200x620+30+30"   # width x height + x + y
FONT_FAMILY = "Consolas"
FONT_SIZE = 18
PAD_X = 10
PAD_Y = 10
COL_INPUT_CHARS = 8            # input column width in characters
COL_FRAME_CHARS = 4
CHROMA_BG = "#00FF00"
CHROMA_FG = "#000000"
ICON_SIZE = 28
             # frame column width (e.g. '99F')
# ========================================
HISTORY_KEEP = 120

LOG_FILE = "overlay_debug.log"

# DS4 解析
DS4_DEADZONE = 28          # stick deadzone (0..127)
DS4_TRIG_THRESHOLD = 40    # trigger pressed (0..255)

# XInput（Xbox pad 等）も残す
POLL_HZ_XINPUT = 250
# =======================

# -----------------------
# Logging
# -----------------------

def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_as_admin() -> None:
    # Relaunch current script with elevation (UAC prompt).
    params = subprocess.list2cmdline(sys.argv)
    # Use python executable to keep venv/py launcher behavior consistent.
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    if rc <= 32:
        raise RuntimeError(f"ShellExecuteW(runas) failed rc={rc}")


logger = logging.getLogger("overlay")
logger.setLevel(logging.DEBUG)

_fmt = logging.Formatter(
    "%(asctime)s.%(msecs)03d [%(levelname)s] %(threadName)s: %(message)s",
    datefmt="%H:%M:%S"
)

_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.INFO)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

def log_exc(msg: str):
    logger.error(msg)
    logger.error(traceback.format_exc())

logger.info("===== input history start =====")
logger.info(f"cwd={os.getcwd()}")
logger.info(f"python={sys.version.replace(os.linesep,' ')}")
logger.info(f"exe={sys.executable}")
logger.info(f"argv={sys.argv}")

# =========================
# Win32 base types
# =========================
PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)
if PTR_SIZE == 8:
    LONG_PTR  = ctypes.c_int64
    ULONG_PTR = ctypes.c_uint64
else:
    LONG_PTR  = ctypes.c_long
    ULONG_PTR = ctypes.c_ulong

BOOL   = ctypes.c_int
UINT   = ctypes.c_uint
DWORD  = ctypes.c_uint32
WORD   = ctypes.c_uint16
BYTE   = ctypes.c_uint8
LONG   = ctypes.c_long
SHORT  = ctypes.c_int16
LPCWSTR = ctypes.c_wchar_p
HANDLE = ctypes.c_void_p
HWND   = ctypes.c_void_p
HINSTANCE = HANDLE
HMENU = HANDLE
LPVOID = ctypes.c_void_p
WPARAM = ULONG_PTR
LPARAM = LONG_PTR
LRESULT = LONG_PTR
ATOM = WORD

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

def last_error() -> int:
    return ctypes.get_last_error()

def check_nonzero(v, name=""):
    if not v:
        err = last_error()
        logger.error(f"{name} failed, GetLastError={err}")
        raise ctypes.WinError(err)
    return v

# =========================
# QPC
# =========================
class LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]

kernel32.QueryPerformanceFrequency.argtypes = [ctypes.POINTER(LARGE_INTEGER)]
kernel32.QueryPerformanceFrequency.restype = BOOL
kernel32.QueryPerformanceCounter.argtypes = [ctypes.POINTER(LARGE_INTEGER)]
kernel32.QueryPerformanceCounter.restype = BOOL

_qpc_freq = LARGE_INTEGER()
check_nonzero(kernel32.QueryPerformanceFrequency(ctypes.byref(_qpc_freq)), "QueryPerformanceFrequency")

def qpc_seconds() -> float:
    c = LARGE_INTEGER()
    kernel32.QueryPerformanceCounter(ctypes.byref(c))
    return c.QuadPart / _qpc_freq.QuadPart

logger.info(f"QPC freq={_qpc_freq.QuadPart}")

# =========================
# Win32 message-only window for RawInput
# =========================
WM_INPUT   = 0x00FF
WM_DESTROY = 0x0002
WM_CLOSE   = 0x0010
HWND_MESSAGE = ctypes.c_void_p(-3)

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HANDLE),
        ("hCursor", HANDLE),
        ("hbrBackground", HANDLE),
        ("lpszMenuName", LPCWSTR),
        ("lpszClassName", LPCWSTR),
    ]

class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", HWND),
        ("message", UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt", POINT),
    ]

kernel32.GetModuleHandleW.argtypes = [LPCWSTR]
kernel32.GetModuleHandleW.restype = HINSTANCE

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = ATOM

user32.CreateWindowExW.argtypes = [
    DWORD, LPCWSTR, LPCWSTR, DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HWND, HMENU, HINSTANCE, LPVOID
]
user32.CreateWindowExW.restype = HWND

user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT

user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), HWND, UINT, UINT]
user32.GetMessageW.restype = ctypes.c_int

user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.TranslateMessage.restype = BOOL

user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT

user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None

user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = BOOL

user32.DestroyWindow.argtypes = [HWND]
user32.DestroyWindow.restype = BOOL

# =========================
# RawInput structures
# =========================
RID_INPUT = 0x10000003
RIM_TYPEKEYBOARD = 1
RIM_TYPEHID = 2

RIDEV_INPUTSINK = 0x00000100

RI_KEY_BREAK = 0x0001
RI_KEY_E0    = 0x0002

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", WORD),
        ("usUsage", WORD),
        ("dwFlags", DWORD),
        ("hwndTarget", HWND),
    ]

class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", DWORD),
        ("dwSize", DWORD),
        ("hDevice", HANDLE),
        ("wParam", WPARAM),
    ]

class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", WORD),
        ("Flags", WORD),
        ("Reserved", WORD),
        ("VKey", WORD),
        ("Message", UINT),
        ("ExtraInformation", DWORD),
    ]

class RAWHID_HDR(ctypes.Structure):
    _fields_ = [
        ("dwSizeHid", DWORD),
        ("dwCount", DWORD),
    ]

user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), UINT, UINT]
user32.RegisterRawInputDevices.restype = BOOL

user32.GetRawInputData.argtypes = [HANDLE, UINT, ctypes.c_void_p, ctypes.POINTER(UINT), UINT]
user32.GetRawInputData.restype = UINT

user32.GetKeyNameTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetKeyNameTextW.restype = ctypes.c_int

user32.MapVirtualKeyW.argtypes = [UINT, UINT]
user32.MapVirtualKeyW.restype = UINT

MAPVK_VK_TO_VSC = 0

RIDI_DEVICENAME = 0x20000007
user32.GetRawInputDeviceInfoW.argtypes = [HANDLE, UINT, ctypes.c_void_p, ctypes.POINTER(UINT)]
user32.GetRawInputDeviceInfoW.restype = UINT

def rawinput_dev_name(hdev: HANDLE) -> str:
    sz = UINT(0)
    user32.GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, None, ctypes.byref(sz))
    if sz.value == 0:
        return ""
    buf = ctypes.create_unicode_buffer(sz.value)
    r = user32.GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, buf, ctypes.byref(sz))
    if r == 0xFFFFFFFF:
        return ""
    return buf.value

def key_name_from_raw(make_code: int, flags: int, vkey: int) -> str:
    sc = make_code
    if sc == 0 and vkey != 0:
        sc = user32.MapVirtualKeyW(vkey, MAPVK_VK_TO_VSC)

    lparam = (sc & 0xFF) << 16
    if flags & RI_KEY_E0:
        lparam |= 1 << 24

    buf = ctypes.create_unicode_buffer(64)
    n = user32.GetKeyNameTextW(ctypes.c_void_p(int(lparam)), buf, 64)
    if n > 0:
        return buf.value

    if 0x30 <= vkey <= 0x5A:
        return chr(vkey)
    return f"VK{vkey:02X}"

# =========================
# Unified input state
# =========================
DIR_UP = "UP"
DIR_DOWN = "DOWN"
DIR_LEFT = "LEFT"
DIR_RIGHT = "RIGHT"

@dataclass
class InputEvent:
    t: float
    kind: str
    name: str
    down: bool
    vk: int = 0

class InputState:
    def __init__(self):
        self.dirs: Set[str] = set()
        self.btns: Set[str] = set()

    def set_dir(self, d: str, down: bool):
        if down:
            self.dirs.add(d)
        else:
            self.dirs.discard(d)

    def set_btn(self, b: str, down: bool):
        if down:
            self.btns.add(b)
        else:
            self.btns.discard(b)

    def snapshot_token(self) -> str:
        u = DIR_UP in self.dirs
        d = DIR_DOWN in self.dirs
        l = DIR_LEFT in self.dirs
        r = DIR_RIGHT in self.dirs

        dir_sym = "N"
        if u and l:
            dir_sym = "↖"
        elif u and r:
            dir_sym = "↗"
        elif d and l:
            dir_sym = "↙"
        elif d and r:
            dir_sym = "↘"
        elif u:
            dir_sym = "↑"
        elif d:
            dir_sym = "↓"
        elif l:
            dir_sym = "←"
        elif r:
            dir_sym = "→"

        order = ["□", "×", "○", "△", "L1", "R1", "L2", "R2", "L3", "R3", "Share", "Options", "PS", "Touch"]
        btns = [b for b in order if b in self.btns]
        extra = sorted([b for b in self.btns if b not in order])
        btns.extend(extra)

        # ボタンは区切り無しで連結（"+" や "/" を使わない）
        btn_str = "".join(btns)

        if dir_sym == "N":
            return btn_str if btn_str else "N"
        return f"{dir_sym}{btn_str}"

# =========================
# History 
# =========================
class History:
    def __init__(self, keep: int):
        self.items: deque[Tuple[str, int]] = deque(maxlen=keep)  # newest first: (token, frames)

    @staticmethod
    def cap(fr: int) -> int:
        return 99 if fr > 99 else fr

    def push_frames(self, token: str, frames: int):
        if frames <= 0:
            return
        if self.items and self.items[0][0] == token:
            tok, fr = self.items[0]
            self.items[0] = (tok, fr + frames)
        else:
            self.items.appendleft((token, frames))

    def render_lines(self, max_lines: int) -> str:
        # 文字幅の補正（半角/全角）はしない。トークン文字列は固定幅で切ってフレーム列を固定する。
        TOKEN_COL = 18  # 近づけたいので短め。長い入力は右が切れる。
        lines = []
        for (tok, fr) in list(self.items)[:max_lines]:
            fr_disp = f"{self.cap(fr)}F"
            tok_disp = tok
            if len(tok_disp) > TOKEN_COL:
                tok_disp = tok_disp[:TOKEN_COL]
            lines.append(f"{tok_disp:<{TOKEN_COL}} {fr_disp:>3}")
        while len(lines) < max_lines:
            lines.append("")
        return "\n".join(lines)

    def render_columns(self, max_lines: int, input_w: int, frame_w: int) -> tuple[str, str]:
        """2列表示用。左列(入力)は右寄せ、右列(フレーム)は左寄せ。"""
        left_lines = []
        right_lines = []
        rows = list(self.items)[:max_lines]
        for tok, fr in rows:
            fr_disp = f"{self.cap(fr)}F"   # 0埋めしない（6F）
            tok_disp = tok
            # 長いトークンは右端が見えるように末尾側を優先（右寄せの都合）
            if len(tok_disp) > input_w:
                tok_disp = tok_disp[-input_w:]
            left_lines.append(f"{tok_disp:>{input_w}}")
            right_lines.append(f"{fr_disp:<{frame_w}}")
        # pad to max_lines
        while len(left_lines) < max_lines:
            left_lines.append(" " * input_w)
            right_lines.append(" " * frame_w)
        return "\n".join(left_lines), "\n".join(right_lines)

# =========================
# RawInput thread (Keyboard + DS4 via HID)
# =========================
ERROR_CLASS_ALREADY_EXISTS = 1410

class RawInputThread(threading.Thread):
    def __init__(self, event_queue: deque, queue_lock: threading.Lock, quit_flag: threading.Event):
        super().__init__(daemon=True, name="RawInputThread")
        self.event_queue = event_queue
        self.queue_lock = queue_lock
        self.quit_flag = quit_flag
        self.hwnd = None
        self._wndproc_cb = None

        self.devname_cache: Dict[int, str] = {}
        self.ds4_prev_btns: Dict[int, Set[str]] = {}
        self.ds4_prev_dirs: Dict[int, Set[str]] = {}

        self.ds4_reports = 0
        self._ds4_last = time.time()

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def run(self):
        hinst = kernel32.GetModuleHandleW(None)

        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                try:
                    self.handle_wm_input(ctypes.c_void_p(lparam))
                except Exception:
                    log_exc("handle_wm_input exception")
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_cb = WNDPROC(_wndproc)

        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(self._wndproc_cb, ctypes.c_void_p)
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = "RawInputHiddenWindow"

        atom = user32.RegisterClassW(ctypes.byref(wc))
        if atom == 0:
            err = last_error()
            if err != ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError(err)

        self.hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "ri", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, hinst, None
        )
        check_nonzero(self.hwnd, "CreateWindowExW")

        # Register: Keyboard + GamePad + Joystick
        rids = (RAWINPUTDEVICE * 3)()

        rids[0].usUsagePage = 0x01
        rids[0].usUsage = 0x06
        rids[0].dwFlags = RIDEV_INPUTSINK
        rids[0].hwndTarget = self.hwnd

        rids[1].usUsagePage = 0x01
        rids[1].usUsage = 0x05
        rids[1].dwFlags = RIDEV_INPUTSINK
        rids[1].hwndTarget = self.hwnd

        rids[2].usUsagePage = 0x01
        rids[2].usUsage = 0x04
        rids[2].dwFlags = RIDEV_INPUTSINK
        rids[2].hwndTarget = self.hwnd

        ok = user32.RegisterRawInputDevices(rids, 3, ctypes.sizeof(RAWINPUTDEVICE))
        check_nonzero(ok, "RegisterRawInputDevices")
        logger.info("RegisterRawInputDevices ok (keyboard+hid)")

        msg = MSG()
        while not self.quit_flag.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0:
                break
            if r == -1:
                logger.error("GetMessageW -> -1")
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _devname(self, hdev: HANDLE) -> str:
        key = int(hdev) if hdev else 0
        if key in self.devname_cache:
            return self.devname_cache[key]
        nm = rawinput_dev_name(hdev)
        self.devname_cache[key] = nm
        if nm:
            logger.info(f"HID device: {nm}")
        return nm

    @staticmethod
    def _is_ds4(devname: str) -> bool:
        d = devname.upper()
        return ("VID_054C" in d)

    def _emit(self, ev: InputEvent):
        logger.info(f"INPUT {ev.kind}: {ev.name} {'DOWN' if ev.down else 'UP'}")
        with self.queue_lock:
            self.event_queue.append(ev)

    def handle_wm_input(self, hrawinput: ctypes.c_void_p):
        size = UINT(0)
        user32.GetRawInputData(hrawinput, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if size.value == 0:
            return

        buf = ctypes.create_string_buffer(size.value)
        user32.GetRawInputData(hrawinput, RID_INPUT, buf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))

        hdr = RAWINPUTHEADER.from_buffer_copy(buf.raw[:ctypes.sizeof(RAWINPUTHEADER)])

        if hdr.dwType == RIM_TYPEKEYBOARD:
            off = ctypes.sizeof(RAWINPUTHEADER)
            kb = RAWKEYBOARD.from_buffer_copy(buf.raw[off:off+ctypes.sizeof(RAWKEYBOARD)])
            down = (kb.Flags & RI_KEY_BREAK) == 0
            vkey = int(kb.VKey)
            name = key_name_from_raw(int(kb.MakeCode), int(kb.Flags), vkey)
            self._emit(InputEvent(t=qpc_seconds(), kind="kb", name=name, down=down, vk=vkey))
            return

        if hdr.dwType == RIM_TYPEHID:
            dev = hdr.hDevice
            devname = self._devname(dev)

            off = ctypes.sizeof(RAWINPUTHEADER)
            hid_hdr = RAWHID_HDR.from_buffer_copy(buf.raw[off:off+ctypes.sizeof(RAWHID_HDR)])
            data_off = off + ctypes.sizeof(RAWHID_HDR)
            total = int(hid_hdr.dwSizeHid) * int(hid_hdr.dwCount)
            raw = buf.raw[data_off:data_off+total]
            if not raw:
                return

            if devname and self._is_ds4(devname):
                self.ds4_reports += 1
                self._handle_ds4(int(dev), raw)

                now = time.time()
                if now - self._ds4_last >= 2.0:
                    rps = self.ds4_reports / (now - self._ds4_last)
                    logger.info(f"DS4 reports/sec ~ {rps:.1f}")
                    self.ds4_reports = 0
                    self._ds4_last = now
            return

    def _handle_ds4(self, dev_key: int, raw: bytes):
        if len(raw) < 10:
            return
        idx0 = raw[0]
        start = 1 if idx0 in (0x01, 0x11) else 0
        if len(raw) < start + 10:
            return

        lx = raw[start + 0]
        ly = raw[start + 1]
        b5 = raw[start + 4]
        b6 = raw[start + 5]
        b7 = raw[start + 6]
        l2 = raw[start + 7]
        r2 = raw[start + 8]

        dx = int(lx) - 128
        dy = int(ly) - 128

        dirs: Set[str] = set()
        if dx <= -DS4_DEADZONE:
            dirs.add(DIR_LEFT)
        elif dx >= DS4_DEADZONE:
            dirs.add(DIR_RIGHT)
        if dy <= -DS4_DEADZONE:
            dirs.add(DIR_UP)
        elif dy >= DS4_DEADZONE:
            dirs.add(DIR_DOWN)

        hat = b5 & 0x0F
        if hat == 0:   dirs |= {DIR_UP}
        elif hat == 1: dirs |= {DIR_UP, DIR_RIGHT}
        elif hat == 2: dirs |= {DIR_RIGHT}
        elif hat == 3: dirs |= {DIR_DOWN, DIR_RIGHT}
        elif hat == 4: dirs |= {DIR_DOWN}
        elif hat == 5: dirs |= {DIR_DOWN, DIR_LEFT}
        elif hat == 6: dirs |= {DIR_LEFT}
        elif hat == 7: dirs |= {DIR_UP, DIR_LEFT}

        btns: Set[str] = set()
        if b5 & 0x10: btns.add("□")
        if b5 & 0x20: btns.add("×")
        if b5 & 0x40: btns.add("○")
        if b5 & 0x80: btns.add("△")

        if b6 & 0x01: btns.add("L1")
        if b6 & 0x02: btns.add("R1")
        if b6 & 0x04: btns.add("L2")
        if b6 & 0x08: btns.add("R2")
        if b6 & 0x10: btns.add("Share")
        if b6 & 0x20: btns.add("Options")
        if b6 & 0x40: btns.add("L3")
        if b6 & 0x80: btns.add("R3")

        if b7 & 0x01: btns.add("PS")
        if b7 & 0x02: btns.add("Touch")

        if l2 >= DS4_TRIG_THRESHOLD: btns.add("L2")
        if r2 >= DS4_TRIG_THRESHOLD: btns.add("R2")

        prev_btns = self.ds4_prev_btns.get(dev_key, set())
        prev_dirs = self.ds4_prev_dirs.get(dev_key, set())
        t = qpc_seconds()

        for d in (DIR_UP, DIR_DOWN, DIR_LEFT, DIR_RIGHT):
            now_down = d in dirs
            prev_down = d in prev_dirs
            if now_down != prev_down:
                self._emit(InputEvent(t=t, kind="ds4", name=d, down=now_down))

        for b in (prev_btns | btns):
            now_down = b in btns
            prev_down = b in prev_btns
            if now_down != prev_down:
                self._emit(InputEvent(t=t, kind="ds4", name=b, down=now_down))

        self.ds4_prev_btns[dev_key] = btns
        self.ds4_prev_dirs[dev_key] = dirs

# =========================
# XInput (optional)
# =========================
def load_xinput():
    for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            dll = ctypes.WinDLL(name)
            logger.info(f"XInput loaded: {name}")
            return dll
        except OSError:
            continue
    logger.info("XInput not found (ok)")
    return None

xinput = load_xinput()

class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", SHORT),
        ("sThumbLY", SHORT),
        ("sThumbRX", SHORT),
        ("sThumbRY", SHORT),
    ]

class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", DWORD), ("Gamepad", XINPUT_GAMEPAD)]

XINPUT_GAMEPAD_A = 0x1000
XINPUT_GAMEPAD_B = 0x2000
XINPUT_GAMEPAD_X = 0x4000
XINPUT_GAMEPAD_Y = 0x8000
XINPUT_GAMEPAD_DPAD_UP = 0x0001
XINPUT_GAMEPAD_DPAD_DOWN = 0x0002
XINPUT_GAMEPAD_DPAD_LEFT = 0x0004
XINPUT_GAMEPAD_DPAD_RIGHT = 0x0008

class XInputThread(threading.Thread):
    def __init__(self, event_queue: deque, queue_lock: threading.Lock, quit_flag: threading.Event):
        super().__init__(daemon=True, name="XInputThread")
        self.event_queue = event_queue
        self.queue_lock = queue_lock
        self.quit_flag = quit_flag
        self.poll_dt = 1.0 / POLL_HZ_XINPUT
        self.last_buttons = [0, 0, 0, 0]

    def run(self):
        if not xinput:
            return
        xinput.XInputGetState.argtypes = [DWORD, ctypes.POINTER(XINPUT_STATE)]
        xinput.XInputGetState.restype = DWORD

        while not self.quit_flag.is_set():
            t = qpc_seconds()
            for i in range(4):
                st = XINPUT_STATE()
                res = xinput.XInputGetState(DWORD(i), ctypes.byref(st))
                if res != 0:
                    continue
                buttons = int(st.Gamepad.wButtons)
                changed = buttons ^ self.last_buttons[i]
                if changed:
                    def emit(name: str, down: bool):
                        logger.info(f"INPUT pad: {name} {'DOWN' if down else 'UP'}")
                        with self.queue_lock:
                            self.event_queue.append(InputEvent(t=t, kind="pad", name=name, down=down))
                    if changed & XINPUT_GAMEPAD_DPAD_UP: emit(DIR_UP, bool(buttons & XINPUT_GAMEPAD_DPAD_UP))
                    if changed & XINPUT_GAMEPAD_DPAD_DOWN: emit(DIR_DOWN, bool(buttons & XINPUT_GAMEPAD_DPAD_DOWN))
                    if changed & XINPUT_GAMEPAD_DPAD_LEFT: emit(DIR_LEFT, bool(buttons & XINPUT_GAMEPAD_DPAD_LEFT))
                    if changed & XINPUT_GAMEPAD_DPAD_RIGHT: emit(DIR_RIGHT, bool(buttons & XINPUT_GAMEPAD_DPAD_RIGHT))
                    if changed & XINPUT_GAMEPAD_A: emit("A", bool(buttons & XINPUT_GAMEPAD_A))
                    if changed & XINPUT_GAMEPAD_B: emit("B", bool(buttons & XINPUT_GAMEPAD_B))
                    if changed & XINPUT_GAMEPAD_X: emit("X", bool(buttons & XINPUT_GAMEPAD_X))
                    if changed & XINPUT_GAMEPAD_Y: emit("Y", bool(buttons & XINPUT_GAMEPAD_Y))
                    self.last_buttons[i] = buttons
            time.sleep(self.poll_dt)

# =========================
# UI / Window
# =========================
VK_Q = 0x51
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3

VK_LEFT = 0x25
VK_UP   = 0x26
VK_RIGHT= 0x27
VK_DOWN = 0x28

def kb_vk_to_dir(vk: int) -> Optional[str]:
    if vk in (VK_UP, ord("W")): return DIR_UP
    if vk in (VK_DOWN, ord("S")): return DIR_DOWN
    if vk in (VK_LEFT, ord("A")): return DIR_LEFT
    if vk in (VK_RIGHT, ord("D")): return DIR_RIGHT
    return None

class OverlayApp:
    def __init__(self):
        self.quit_flag = threading.Event()
        self.queue_lock = threading.Lock()
        self.event_queue = deque()

        self.state = InputState()
        self.history = History(keep=HISTORY_KEEP)

        self._t0 = qpc_seconds()
        self._frame = 0
        self._dt = 1.0 / APP_FPS
        self._last_render_log = time.time()

        self.kb_vk_down: Set[int] = set()

        self.root = tk.Tk()
        self.root.title("Input History Overlay")
        self.root.attributes("-topmost", True)
        self.root.configure(bg=CHROMA_BG)
        self.root.geometry(WIN_GEOMETRY)

        header = tk.Label(
            self.root,
            text="INPUT",
            fg=CHROMA_FG, bg=CHROMA_BG,
            font=("Segoe UI", 11, "bold"),
            anchor="e", padx=10, pady=6
        )
        header.pack(fill="x")

        # 2列レイアウト：左=入力（右寄せ）、右=フレーム（左寄せ）
        body = tk.Frame(self.root, bg=CHROMA_BG)
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)

        self.text_input = tk.Text(
            body,
            wrap="none",
            font=(FONT_FAMILY, FONT_SIZE),
            bg=CHROMA_BG, fg=CHROMA_FG,
            insertbackground="white",
            borderwidth=0, highlightthickness=0,
            width=COL_INPUT_CHARS,
        )
        self.text_input.grid(row=0, column=0, sticky="nsew")

        self.text_frames = tk.Text(
            body,
            wrap="none",
            font=(FONT_FAMILY, FONT_SIZE),
            bg=CHROMA_BG, fg=CHROMA_FG,
            insertbackground="white",
            borderwidth=0, highlightthickness=0,
            width=COL_FRAME_CHARS,
        )
        self.text_frames.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)

        self.text_input.configure(state="disabled")
        self.text_frames.configure(state="disabled")

        footer = tk.Label(
            self.root,
            text="Ctrl+Shift+Q: Quit   |   See overlay_debug.log for details",
            fg="#CCCCCC", bg=CHROMA_BG,
            font=("Segoe UI", 9),
            anchor="w", padx=10, pady=6
        )
        footer.pack(fill="x")
        self.icon_images = {}
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            icon_dir = os.path.join(base_dir, "icons")

            def load_icon(filename: str):
                path = os.path.join(icon_dir, filename)
                img = Image.open(path).convert("RGBA")
                img = img.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
                return ImageTk.PhotoImage(img)

            self.icon_images["□"] = load_icon("basic.png")
            self.icon_images["×"] = load_icon("dodge.png")
            self.icon_images["△"] = load_icon("sp.png")
            self.icon_images["L1"] = load_icon("assist.png")
            self.icon_images["R1"] = self.icon_images["L1"]
            self.icon_images["R2"] = load_icon("ult.png")
        except Exception:
            logger.warning("icon load failed; fallback to text\n" + traceback.format_exc())

        self.root.protocol("WM_DELETE_WINDOW", self.stop)

        # 初期表示：これが見えないならTk側の描画/表示問題
        self.history.push_frames("N", 1)
        self._render()

        self.raw_thread = RawInputThread(self.event_queue, self.queue_lock, self.quit_flag)
        self.raw_thread.start()
        self.pad_thread = XInputThread(self.event_queue, self.queue_lock, self.quit_flag)
        self.pad_thread.start()

        self.root.after(1, self._tick)

    def stop(self):
        if self.quit_flag.is_set():
            return
        self.quit_flag.set()
        try:
            self.raw_thread.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _process_event(self, ev: InputEvent):
        if ev.kind == "kb":
            if ev.down:
                self.kb_vk_down.add(ev.vk)
            else:
                self.kb_vk_down.discard(ev.vk)

            d = kb_vk_to_dir(ev.vk)
            if d:
                self.state.set_dir(d, ev.down)
            else:
                # IME切替（半角/全角）などは履歴に出さない
                if ("半角" in ev.name and "全角" in ev.name):
                    pass
                elif ev.name not in ("Shift", "Left Shift", "Right Shift",
                                     "Ctrl", "Left Ctrl", "Right Ctrl",
                                     "Alt", "Left Alt", "Right Alt"):
                    self.state.set_btn(ev.name, ev.down)

            if ev.down and ev.vk == VK_Q:
                shift = any(v in self.kb_vk_down for v in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT))
                ctrl  = any(v in self.kb_vk_down for v in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL))
                if shift and ctrl:
                    logger.info("Ctrl+Shift+Q -> quit")
                    self.stop()
        else:
            if ev.name in (DIR_UP, DIR_DOWN, DIR_LEFT, DIR_RIGHT):
                self.state.set_dir(ev.name, ev.down)
            else:
                self.state.set_btn(ev.name, ev.down)

    def _render_input_with_icons(self):
        self.text_input.configure(state="normal")
        self.text_input.delete("1.0", "end")

        rows = list(self.history.items)[:HISTORY_LINES]

        for row_idx, (tok, fr) in enumerate(rows):
            units = self.split_token_units(tok)

            if row_idx > 0:
                self.text_input.insert("end", "\n")

            for u in units:
                if u in self.icon_images:
                    self.text_input.image_create("end", image=self.icon_images[u])
                else:
                    self.text_input.insert("end", u)

        while len(rows) < HISTORY_LINES:
            self.text_input.insert("end", "\n")
            rows.append(("", 0))

        self.text_input.configure(state="disabled")

    def _render(self):
        rows = list(self.history.items)[:HISTORY_LINES]

        # 左列：文字 + 画像
        self._render_input_with_icons()

        # 右列：フレーム数
        right_lines = []
        for tok, fr in rows:
            fr_disp = f"{self.history.cap(fr)}F"
            right_lines.append(f"{fr_disp:<{COL_FRAME_CHARS}}")

        while len(right_lines) < HISTORY_LINES:
            right_lines.append(" " * COL_FRAME_CHARS)

        self.text_frames.configure(state="normal")
        self.text_frames.delete("1.0", "end")
        self.text_frames.insert("1.0", "\n".join(right_lines))
        self.text_frames.configure(state="disabled")

        self.root.update_idletasks()

    def _tick(self):
        try:
            now = qpc_seconds()

            with self.queue_lock:
                while self.event_queue:
                    ev = self.event_queue.popleft()
                    self._process_event(ev)
                    if self.quit_flag.is_set():
                        return

            target_frame = int((now - self._t0) / self._dt)
            delta = target_frame - self._frame
            if delta > 0:
                token = self.state.snapshot_token()
                self.history.push_frames(token, delta)
                self._frame = target_frame
                self._render()

                tnow = time.time()
                if tnow - self._last_render_log >= 1.0:
                    logger.info(f"RENDER frame={self._frame} token={token}")
                    self._last_render_log = tnow

        except Exception:
            log_exc("_tick exception")

        if not self.quit_flag.is_set():
            self.root.after(1, self._tick)

    def run(self):
        logger.info("mainloop start")
        self.root.mainloop()
        self.stop()

    def split_token_units(self, token: str) -> list[str]:
        units = [
            "Options", "Share", "Touch",
            "L1", "R1", "L2", "R2", "L3", "R3", "PS",
            "↖", "↗", "↙", "↘", "↑", "↓", "←", "→",
            "□", "×", "○", "△",
            "N",
        ]

        result = []
        i = 0
        while i < len(token):
            matched = False
            for u in units:
                if token.startswith(u, i):
                    result.append(u)
                    i += len(u)
                    matched = True
                    break
            if not matched:
                result.append(token[i])
                i += 1
        return result

if __name__ == "__main__":
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--no-elevate", action="store_true",
                    help="Do not auto-relaunch as Administrator (no UAC prompt).")
    args = ap.parse_args()

    if not args.no_elevate and not _is_admin():
        # Many full-screen games or elevated apps may block low-privilege global input hooks.
        # Elevation is the simplest fix when inputs stop while a game is running as admin.
        try:
            logger.warning("Not running as Administrator -> relaunch with UAC (disable with --no-elevate)")
            _relaunch_as_admin()
            sys.exit(0)
        except Exception:
            logger.warning("Auto-elevate failed; continue without admin.\n" + traceback.format_exc())

    try:
        OverlayApp().run()
    except Exception:
        logger.critical("FATAL:\n" + traceback.format_exc())
        raise
