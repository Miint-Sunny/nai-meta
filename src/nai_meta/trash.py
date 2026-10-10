# -*- coding: utf-8 -*-
"""将文件移到废纸篓。目前仅支持 macOS。

macOS 上通过 Objective-C 运行时调用 NSFileManager 的 ``trashItemAtURL:resultingItemURL:error:``，
不依赖第三方库。其他系统暂不支持，调用时抛出 OSError；需要支持 Windows 回收站或 Linux 回收站时，
在本模块中补充对应实现即可。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from functools import lru_cache
from pathlib import Path

UNSUPPORTED = '当前系统不支持移到废纸篓（目前仅支持 macOS）'


def supported() -> bool:
    """返回当前系统是否支持移到废纸篓。"""
    return sys.platform == 'darwin'


def move_to_trash(path: str | os.PathLike) -> str | None:
    """将文件移到废纸篓。

    Returns:
        文件在废纸篓中的路径；系统未返回时为 None。

    Raises:
        OSError: 当前系统不支持，或移动失败。失败时文件保持原位。
    """
    if not supported():
        raise OSError(UNSUPPORTED)
    p = Path(path).absolute()
    result = _objc().trash(p)
    if os.path.lexists(p):
        raise OSError(f'移到废纸篓后文件仍在原位置：{p}')
    return result


@lru_cache(maxsize=1)
def _objc() -> '_ObjC':
    return _ObjC()


class _ObjC:
    """通过 ctypes 调用 Objective-C 运行时的最小封装，仅实现移到废纸篓所需的消息。"""

    def __init__(self):
        try:
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('objc'))
            ctypes.cdll.LoadLibrary(ctypes.util.find_library('Foundation'))   # 加载后才能取到 NSFileManager 等类
        except (OSError, TypeError) as e:
            raise OSError(f'无法加载 Objective-C 运行时：{e}') from e
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self._lib = objc
        # objc_msgSend 必须按每条消息的实际签名调用，arm64 上不能作为可变参数函数调用
        addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        vp = ctypes.c_void_p
        self._send = ctypes.CFUNCTYPE(vp, vp, vp)(addr)                              # id (id, SEL)
        self._send_obj = ctypes.CFUNCTYPE(vp, vp, vp, vp)(addr)                      # id (id, SEL, id)
        self._send_cstr = ctypes.CFUNCTYPE(vp, vp, vp, ctypes.c_char_p)(addr)        # id (id, SEL, const char *)
        self._utf8 = ctypes.CFUNCTYPE(ctypes.c_char_p, vp, vp)(addr)                 # const char * (id, SEL)
        self._trash = ctypes.CFUNCTYPE(ctypes.c_bool, vp, vp, vp,                    # BOOL (id, SEL, NSURL *,
                                       ctypes.POINTER(vp), ctypes.POINTER(vp))(addr)  # NSURL **, NSError **)

    def _cls(self, name: str) -> int:
        c = self._lib.objc_getClass(name.encode())
        if not c:
            raise OSError(f'无法加载 Objective-C 类 {name}')
        return c

    def _sel(self, name: str) -> int:
        return self._lib.sel_registerName(name.encode())

    def _string(self, ns) -> str:
        if not ns:
            return ''
        raw = self._utf8(ns, self._sel('UTF8String'))
        return raw.decode('utf-8', 'replace') if raw else ''

    def trash(self, path: Path) -> str | None:
        """调用 NSFileManager 将 path 移到废纸篓，返回其在废纸篓中的路径。"""
        pool = self._send(self._send(self._cls('NSAutoreleasePool'), self._sel('alloc')), self._sel('init'))
        try:
            ns_path = self._send_cstr(self._cls('NSString'), self._sel('stringWithUTF8String:'), os.fsencode(path))
            if not ns_path:                      # 传入 nil 会令 fileURLWithPath: 抛出 Objective-C 异常，必须先排除
                raise OSError(f'无法转换路径：{path}')
            url = self._send_obj(self._cls('NSURL'), self._sel('fileURLWithPath:'), ns_path)
            manager = self._send(self._cls('NSFileManager'), self._sel('defaultManager'))
            result, err = ctypes.c_void_p(), ctypes.c_void_p()
            ok = self._trash(manager, self._sel('trashItemAtURL:resultingItemURL:error:'), url,
                             ctypes.byref(result), ctypes.byref(err))
            if not ok:
                reason = self._string(self._send(err.value, self._sel('localizedDescription'))) if err.value else ''
                raise OSError(reason or '移到废纸篓失败')
            if not result.value:
                return None
            return self._string(self._send(result.value, self._sel('path'))) or None
        finally:
            if pool:
                self._send(pool, self._sel('drain'))
