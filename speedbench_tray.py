# -*- coding: utf-8 -*-
"""Windows 系统托盘图标（纯 ctypes / Win32，零第三方依赖）。

设计取舍：托盘收进面板进程内部，而不是另起 PowerShell + WinForms——
后者会多挂一个常驻 powershell 进程，且 Windows Terminal 作为默认终端时
-WindowStyle Hidden 藏不住窗口（真机实测）。进程内方案生命周期与面板
天然一致：面板退、图标即摘，也不需要额外的看门狗轮询。

实现要点：
- 独立守护线程跑 Win32 消息循环；窗口用 HWND_MESSAGE 消息专用窗口，
  不在任务栏/屏幕上出现任何东西
- 图标用 LoadImageW 从面板同目录的 speedbench.ico 按系统小图标尺寸加载，
  失败退回系统默认图标 IDI_APPLICATION，保证托盘一定有东西可点
- 左键 = 打开面板；右键弹菜单（打开面板 / 退出 SpeedBench）
- stop_tray 时给线程投递 WM_QUIT，消息循环退出后在线程内 NIM_DELETE
  摘图标并销毁窗口，避免僵尸图标
- 非 win32 全部 no-op（start_tray 返回 None），mac/Linux 行为不变
"""
from __future__ import annotations

import sys
import threading

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # ---------------- Win32 常量 ----------------
    WM_APP = 0x8000
    WM_TRAY_CALLBACK = WM_APP + 1     # 托盘图标事件回投给窗口的消息号
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205
    WM_NULL = 0x0000
    WM_QUIT = 0x0012
    NIM_ADD, NIM_DELETE = 0, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x10
    SM_CXSMICON, SM_CYSMICON = 49, 50
    MF_STRING, MF_SEPARATOR = 0, 0x800
    TPM_RETURNCMD = 0x100             # TrackPopupMenu 返回选中项 ID 而非直接派发
    ID_OPEN, ID_QUIT = 1001, 1002
    IDI_APPLICATION = 32512
    HWND_MESSAGE = wintypes.HWND(-3 & 0xFFFFFFFFFFFFFFFF)  # (HWND)-3：消息专用窗口父句柄

    # Win64 只有一套调用约定，WINFUNCTYPE/stdcall 直接可用
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HANDLE),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):
        # 经典布局（szTip 128 字符）：现代 Windows 全部兼容，无需 GUID/V4 字段
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE),
                    ("szTip", wintypes.WCHAR * 128),
                    ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", POINT)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HANDLE, wintypes.HINSTANCE,
                                       wintypes.LPVOID)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetMessageW.argtypes = (ctypes.POINTER(MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT)
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = (ctypes.POINTER(MSG),)
    user32.DispatchMessageW.argtypes = (ctypes.POINTER(MSG),)
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)
    user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM)
    user32.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT)
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadIconW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR)
    user32.LoadIconW.restype = wintypes.HANDLE
    user32.DestroyIcon.argtypes = (wintypes.HANDLE,)
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.CreatePopupMenu.restype = wintypes.HANDLE
    user32.AppendMenuW.argtypes = (wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t,
                                   wintypes.LPCWSTR)
    user32.TrackPopupMenu.argtypes = (wintypes.HANDLE, wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.LPVOID)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
    user32.DestroyMenu.argtypes = (wintypes.HANDLE,)
    user32.DestroyWindow.argtypes = (wintypes.HWND,)
    user32.UnregisterClassW.argtypes = (wintypes.LPCWSTR, wintypes.HINSTANCE)
    shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    class TrayIcon:
        """托盘图标本体：run() 在专属线程里跑消息循环直到收到 WM_QUIT。"""

        def __init__(self, icon_path: str, tip: str, on_open, on_quit) -> None:
            self.icon_path = icon_path
            self.tip = tip
            self.on_open = on_open
            self.on_quit = on_quit
            self.thread_id = None   # 消息循环线程的 Win32 线程 id（stop 时投递用）
            self._hwnd = None
            self._nid = None
            self._hicon = None

        def run(self) -> None:
            # WNDPROC 回调对象必须全程持有引用，否则被 GC 后窗口过程变野指针
            self._wndproc_ref = WNDPROC(self._wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.lpszClassName = "SpeedBenchTrayWnd"
            wc.hInstance = kernel32.GetModuleHandleW(None)
            user32.RegisterClassW(ctypes.byref(wc))
            self._hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, "SpeedBenchTray", 0, 0, 0, 0, 0,
                HWND_MESSAGE, None, wc.hInstance, None)
            if not self._hwnd:
                print("托盘：创建消息窗口失败，托盘不可用（不影响面板本身）")
                return
            self.thread_id = kernel32.GetCurrentThreadId()

            hicon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON,
                                      user32.GetSystemMetrics(SM_CXSMICON),
                                      user32.GetSystemMetrics(SM_CYSMICON),
                                      LR_LOADFROMFILE)
            if not hicon:  # 自定义图标缺失/损坏时退回系统默认应用图标
                hicon = user32.LoadIconW(None, wintypes.LPCWSTR(IDI_APPLICATION))
            self._hicon = hicon

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY_CALLBACK
            nid.hIcon = hicon
            nid.szTip = self.tip
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            self._nid = nid

            msg = MSG()
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:  # 0 = WM_QUIT；-1 = 出错，同样退出避免死循环
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            # 消息循环结束 = 面板在关闭：摘图标、销毁资源
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            if self._hicon:
                user32.DestroyIcon(self._hicon)
            user32.DestroyWindow(self._hwnd)
            user32.UnregisterClassW("SpeedBenchTrayWnd", wc.hInstance)

        def _wndproc(self, hwnd, msg, wparam, lparam):
            # 未声明 NOTIFYICON_VERSION 时：wparam = 图标 uID，lparam = 鼠标消息
            if msg == WM_TRAY_CALLBACK:
                if lparam == WM_LBUTTONUP:
                    self.on_open()
                elif lparam == WM_RBUTTONUP:
                    self._popup(hwnd)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _popup(self, hwnd) -> None:
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "打开面板")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出 SpeedBench")
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # 必须先 SetForegroundWindow 再弹菜单，否则点击别处菜单不收起；
            # 菜单关闭后再补一个 WM_NULL 让窗口退出前台态（Win32 惯例）
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            user32.DestroyMenu(menu)
            if cmd == ID_OPEN:
                self.on_open()
            elif cmd == ID_QUIT:
                self.on_quit()


def start_tray(icon_path, on_open, on_quit, tip: str = "Clash SpeedBench"):
    """win32 上启动托盘图标线程并返回 TrayIcon；其他平台 no-op 返回 None。

    on_open/on_quit 在托盘线程里被调用，回调实现自己保证线程安全。
    """
    if sys.platform != "win32":
        return None
    tray = TrayIcon(str(icon_path), tip, on_open, on_quit)
    t = threading.Thread(target=tray.run, daemon=True, name="SpeedBenchTray")
    t.start()
    return tray


def stop_tray(tray) -> None:
    """给托盘线程投递 WM_QUIT；线程内完成 NIM_DELETE 与窗口销毁。"""
    if tray is None or sys.platform != "win32":
        return
    tid = tray.thread_id
    if tid:  # 线程尚未跑起来（极端时序）就让 daemon 线程随进程退即可
        user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
