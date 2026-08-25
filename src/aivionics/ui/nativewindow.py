"""Give a frameless window its native behaviour back (BACKLOG item 3).

`Qt.FramelessWindowHint` strips `WS_CAPTION` and `WS_THICKFRAME` off the
native window, and with them go the things the desktop provides rather than
the application: the minimise and restore animation, Aero Snap, and the
window controls on the taskbar thumbnail. Nothing errors — the window simply
vanishes and reappears, which reads as unfinished.

The fix is the one every frameless-window implementation on Windows arrives
at: put the frame styles back so the desktop treats the window as an ordinary
one, then answer `WM_NCCALCSIZE` with a client area that covers the whole
window, so the frame that was just re-enabled is never actually drawn.

Two details are not optional:

* **Maximised windows are deliberately larger than the work area** by the
  resize border, on the assumption the frame will eat it. With the frame gone
  the client rect has to be pulled back in by hand or the edges of the app are
  off-screen.
* **An auto-hiding taskbar needs a pixel to live in.** A maximised window that
  covers its edge completely stops it re-appearing, so one pixel is given back
  on whichever edge it is docked to.

Everything here is best-effort. On a non-Windows platform, or if any call
fails, the window is left exactly as Qt made it and the caller carries on.
"""
from __future__ import annotations

import ctypes
import sys

IS_WINDOWS = sys.platform == "win32"

# Window styles the desktop looks for before it animates or snaps anything.
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
NATIVE_STYLE = (WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX
                | WS_MAXIMIZEBOX | WS_SYSMENU)

WM_NCCALCSIZE = 0x0083
SM_CXSIZEFRAME = 32
SM_CYSIZEFRAME = 33
SM_CXPADDEDBORDER = 92

ABM_GETSTATE = 0x00000004
ABM_GETTASKBARPOS = 0x00000005
ABS_AUTOHIDE = 0x0000001
ABE_LEFT, ABE_TOP, ABE_RIGHT, ABE_BOTTOM = 0, 1, 2, 3


if IS_WINDOWS:
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [("rgrc", RECT * 3), ("lppos", ctypes.c_void_p)]

    class APPBARDATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uCallbackMessage", wintypes.UINT),
                    ("uEdge", wintypes.UINT), ("rc", RECT),
                    ("lParam", ctypes.c_void_p)]


def set_taskbar_identity(app_id: str = "AIvionics.Workstation") -> bool:
    """Claim our own taskbar identity, so Windows stops filing us under Python.

    A taskbar button is grouped by AppUserModelID, and a process started by
    `python.exe` inherits the interpreter's. Setting the window icon alone does
    not move it - the button keeps Python's icon and Python's group until the
    process says who it is.
    """
    if not IS_WINDOWS:
        return False
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        return False


def restore_native_frame(window) -> bool:
    """Put the frame styles back on `window`'s native handle.

    Returns True if the styles are in place, False on any platform or call
    where they are not — the caller should treat that as "no change" and not
    as an error.
    """
    if not IS_WINDOWS:
        return False
    try:
        hwnd = int(window.winId())
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        if style == 0:
            return False
        user32.SetWindowLongW(hwnd, GWL_STYLE, style | NATIVE_STYLE)
        return True
    except Exception:
        return False


def _resize_border(hwnd: int) -> tuple[int, int]:
    """The frame thickness Windows assumed when it sized a maximised window."""
    user32 = ctypes.windll.user32
    try:
        dpi = user32.GetDpiForWindow(hwnd)
        metric = user32.GetSystemMetricsForDpi
        padded = metric(SM_CXPADDEDBORDER, dpi)
        return (metric(SM_CXSIZEFRAME, dpi) + padded,
                metric(SM_CYSIZEFRAME, dpi) + padded)
    except Exception:
        padded = user32.GetSystemMetrics(SM_CXPADDEDBORDER)
        return (user32.GetSystemMetrics(SM_CXSIZEFRAME) + padded,
                user32.GetSystemMetrics(SM_CYSIZEFRAME) + padded)


def _autohide_taskbar_edge() -> int | None:
    """The edge an auto-hiding taskbar is docked to, or None if it is pinned."""
    try:
        shell32 = ctypes.windll.shell32
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        if not (shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(data))
                & ABS_AUTOHIDE):
            return None
        shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(data))
        return int(data.uEdge)
    except Exception:
        return None


def handle_native_event(window, event_type, message) -> tuple[bool, int] | None:
    """Answer `WM_NCCALCSIZE` so the restored frame is never painted.

    Returns the `(handled, result)` pair Qt expects, or None to let Qt deal
    with the message as it normally would.
    """
    if not IS_WINDOWS:
        return None
    if event_type not in (b"windows_generic_MSG", "windows_generic_MSG"):
        return None
    try:
        msg = wintypes.MSG.from_address(int(message))
    except Exception:
        return None
    if msg.message != WM_NCCALCSIZE or not msg.hWnd:
        return None

    try:
        if msg.wParam:
            rect = ctypes.cast(
                msg.lParam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents.rgrc[0]
        else:
            rect = ctypes.cast(msg.lParam, ctypes.POINTER(RECT)).contents

        if ctypes.windll.user32.IsZoomed(msg.hWnd) and not window.isFullScreen():
            bx, by = _resize_border(msg.hWnd)
            rect.left += bx
            rect.right -= bx
            rect.top += by
            rect.bottom -= by
            edge = _autohide_taskbar_edge()
            if edge == ABE_BOTTOM:
                rect.bottom -= 1
            elif edge == ABE_TOP:
                rect.top += 1
            elif edge == ABE_LEFT:
                rect.left += 1
            elif edge == ABE_RIGHT:
                rect.right -= 1
    except Exception:
        return None
    # Zero means "keep the client area I just described" — no frame is drawn.
    return True, 0
