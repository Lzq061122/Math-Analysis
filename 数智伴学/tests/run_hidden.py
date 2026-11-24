"""在单独的 Windows 桌面上运行命令，窗口不出现在当前桌面。

用法：python tools/run_hidden.py -- <命令> [参数...]
退出码与子进程一致。只用于自动化测试，不参与软件功能。
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


user32.CreateDesktopW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
user32.CreateDesktopW.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION),
]
kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def main(argv):
    if "--" not in argv:
        print("用法：python tools/run_hidden.py -- <命令> [参数...]")
        return 2
    command = argv[argv.index("--") + 1:]
    if not command:
        return 2
    name = "dsh-test-%d" % int(time.time())
    desk = user32.CreateDesktopW(name, None, None, 0, 0x10000000, None)  # GENERIC_ALL
    if not desk:
        print("创建桌面失败：%d" % ctypes.get_last_error())
        return 3
    info = STARTUPINFO()
    info.cb = ctypes.sizeof(info)
    info.lpDesktop = name
    proc_info = PROCESS_INFORMATION()
    cmdline = subprocess.list2cmdline(command)
    ok = kernel32.CreateProcessW(
        None, ctypes.create_unicode_buffer(cmdline), None, None, True,
        0, None, None, ctypes.byref(info), ctypes.byref(proc_info),
    )
    if not ok:
        print("启动失败：%s" % ctypes.get_last_error())
        user32.CloseDesktop(desk)
        return 3
    kernel32.WaitForSingleObject(proc_info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(proc_info.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(proc_info.hThread)
    kernel32.CloseHandle(proc_info.hProcess)
    user32.CloseDesktop(desk)
    return code.value


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
