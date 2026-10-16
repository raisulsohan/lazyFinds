"""The Windows parts of a result's right-click menu: Open with, Send to, Share, Create shortcut, Delete, Properties.

Each one asks Windows' own shell for the same object File Explorer uses, so it behaves as it does there: Delete goes to
the Recycle Bin, Send to a zip makes the zip, Open with lists the apps Explorer lists. The COM calls go through the
objects' method tables with ctypes; the UI thread already has COM (Qt starts OLE for the clipboard).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import HRESULT, POINTER, WINFUNCTYPE, byref, c_void_p, wintypes
from dataclasses import dataclass
from pathlib import Path

ole32 = ctypes.windll.ole32
shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        super().__init__()
        ole32.CLSIDFromString(text, byref(self))


IID_IShellItem = GUID("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")
IID_IContextMenu = GUID("{000214E4-0000-0000-C000-000000000046}")
IID_IDataObject = GUID("{0000010E-0000-0000-C000-000000000046}")
IID_IDropTarget = GUID("{00000122-0000-0000-C000-000000000046}")
BHID_SFUIObject = GUID("{3981E225-F559-11D3-8E3A-00C04F6837D5}")
BHID_DataObject = GUID("{B8C0BD9F-ED24-455C-83E6-D5390C4FE8C4}")
FOLDERID_SendTo = GUID("{8983036C-27C0-404B-8F08-102D10DCFD74}")

shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, c_void_p, POINTER(GUID), POINTER(c_void_p)]
shell32.SHCreateItemFromParsingName.restype = HRESULT
shell32.SHGetKnownFolderPath.argtypes = [POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, POINTER(c_void_p)]
shell32.SHGetKnownFolderPath.restype = HRESULT
shell32.SHAssocEnumHandlers.argtypes = [wintypes.LPCWSTR, ctypes.c_int, POINTER(c_void_p)]
shell32.SHAssocEnumHandlers.restype = HRESULT
shell32.SHObjectProperties.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR]
shell32.SHObjectProperties.restype = wintypes.BOOL
ctypes.windll.shlwapi.SHLoadIndirectString.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, c_void_p]
ctypes.windll.shlwapi.SHLoadIndirectString.restype = HRESULT
ole32.CoTaskMemFree.argtypes = [c_void_p]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.GetMenuItemCount.argtypes = [wintypes.HMENU]
user32.GetMenuItemID.argtypes = [wintypes.HMENU, ctypes.c_int]
user32.GetMenuItemID.restype = ctypes.c_int


def _call(this, index: int, argtypes=(), *args, restype=HRESULT):
    """Calls method `index` of a COM object's table. An HRESULT that failed raises OSError."""
    table = ctypes.cast(this, POINTER(POINTER(c_void_p))).contents
    return WINFUNCTYPE(restype, c_void_p, *argtypes)(table[index])(this, *args)


def _release(this) -> None:
    if this:
        _call(this, 2, restype=wintypes.ULONG)


def _take_string(p: c_void_p) -> str:
    """A string the shell allocated, freed after reading."""
    try:
        return ctypes.wstring_at(p.value) if p.value else ""
    finally:
        ole32.CoTaskMemFree(p)


def _item(path: str) -> c_void_p:
    item = c_void_p()
    shell32.SHCreateItemFromParsingName(path, None, byref(IID_IShellItem), byref(item))
    return item


def _handler(path: str, bhid: GUID, iid: GUID) -> c_void_p:
    """IShellItem::BindToHandler: the object Explorer itself uses for `path` (its menu, its data, its drop target)."""
    item = _item(path)
    try:
        out = c_void_p()
        _call(item, 3, (c_void_p, POINTER(GUID), POINTER(GUID), POINTER(c_void_p)), None, byref(bhid), byref(iid),
              byref(out))
        return out
    finally:
        _release(item)


def display_name(path: str) -> str:
    """The name Explorer shows: "Compressed (zipped) folder", "USB Drive (E:)", a shortcut without ".lnk"."""
    item = _item(path)
    try:
        name = c_void_p()
        _call(item, 5, (ctypes.c_int, POINTER(c_void_p)), 0, byref(name))   # SIGDN_NORMALDISPLAY
        return _take_string(name)
    finally:
        _release(item)


# -- a verb of Explorer's own right-click menu


class CMINVOKECOMMANDINFOEX(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.DWORD), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCSTR), ("lpParameters", wintypes.LPCSTR), ("lpDirectory", wintypes.LPCSTR),
                ("nShow", ctypes.c_int), ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                ("lpTitle", wintypes.LPCSTR), ("lpVerbW", wintypes.LPCWSTR), ("lpParametersW", wintypes.LPCWSTR),
                ("lpDirectoryW", wintypes.LPCWSTR), ("lpTitleW", wintypes.LPCWSTR), ("ptInvoke", wintypes.POINT)]


CMIC_MASK_UNICODE, CMIC_MASK_PTINVOKE = 0x4000, 0x20000000
IID_IUnknown = GUID("{00000000-0000-0000-C000-000000000046}")
IID_IOleWindow = GUID("{00000114-0000-0000-C000-000000000046}")
IID_IServiceProvider = GUID("{6D5140C1-7436-11CE-8034-00AA006009FA}")
IID_IObjectWithSite = GUID("{FC4801A3-2BA9-11CF-A229-00AA003D7352}")
E_NOINTERFACE = -2147467262

# The menu's "site", as Explorer is for its own menus. Share asks it (IServiceProvider, then IOleWindow) for the window
# to put its window over, and fails with "Invalid window handle" when there's none. One static object: its reference
# counts do nothing.
_QueryInterface = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
_Ref = WINFUNCTYPE(wintypes.ULONG, c_void_p)
_GetWindow = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(wintypes.HWND))
_ContextSensitiveHelp = WINFUNCTYPE(ctypes.c_long, c_void_p, wintypes.BOOL)
_QueryService = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(GUID), POINTER(c_void_p))


class _WindowTable(ctypes.Structure):
    _fields_ = [("QueryInterface", _QueryInterface), ("AddRef", _Ref), ("Release", _Ref),
                ("GetWindow", _GetWindow), ("ContextSensitiveHelp", _ContextSensitiveHelp)]


class _ServiceTable(ctypes.Structure):
    _fields_ = [("QueryInterface", _QueryInterface), ("AddRef", _Ref), ("Release", _Ref),
                ("QueryService", _QueryService)]


class _ComObject(ctypes.Structure):
    _fields_ = [("table", c_void_p)]


_site_window = 0


def _site_query(this, iid, out):
    wanted = bytes(iid.contents)
    if wanted in (bytes(IID_IUnknown), bytes(IID_IOleWindow)):
        out[0] = ctypes.addressof(_window)
    elif wanted == bytes(IID_IServiceProvider):
        out[0] = ctypes.addressof(_services)
    else:
        out[0] = None
        return E_NOINTERFACE
    return 0


def _site_window_of(this, out):
    out[0] = _site_window
    return 0


def _site_service(this, service, iid, out):
    if bytes(iid.contents) == bytes(IID_IOleWindow):
        out[0] = ctypes.addressof(_window)
        return 0
    out[0] = None
    return E_NOINTERFACE


_window_table = _WindowTable(_QueryInterface(_site_query), _Ref(lambda this: 1), _Ref(lambda this: 1),
                             _GetWindow(_site_window_of), _ContextSensitiveHelp(lambda this, on: 0))
_service_table = _ServiceTable(_QueryInterface(_site_query), _Ref(lambda this: 1), _Ref(lambda this: 1),
                               _QueryService(_site_service))
_window = _ComObject(ctypes.addressof(_window_table))
_services = _ComObject(ctypes.addressof(_service_table))


def _set_site(menu, hwnd: int) -> None:
    global _site_window
    _site_window = hwnd
    with_site = c_void_p()
    try:
        _call(menu, 0, (POINTER(GUID), POINTER(c_void_p)), byref(IID_IObjectWithSite), byref(with_site))
    except OSError:
        return
    try:
        _call(with_site, 3, (c_void_p,), ctypes.addressof(_window))   # SetSite
    finally:
        _release(with_site)


def invoke(path: str, verb: str, hwnd: int = 0, where: tuple[int, int] | None = None) -> None:
    """Runs `verb` of the file's Explorer menu: "delete" (to the Recycle Bin), "link" (a shortcut beside it),
    "Windows.ModernShare" (the Share window)."""
    menu = _handler(path, BHID_SFUIObject, IID_IContextMenu)
    hmenu = user32.CreatePopupMenu()
    try:
        _set_site(menu, hwnd)
        # The menu is built, never shown. The item is run by its number, as Explorer runs it when clicked: some
        # handlers (Share) take no verb by name.
        _call(menu, 3, (wintypes.HMENU, wintypes.UINT, wintypes.UINT, wintypes.UINT, wintypes.UINT), hmenu, 0, 1,
              0x7FFF, 0)
        offset = _find_verb(menu, hmenu, verb)
        info = CMINVOKECOMMANDINFOEX(cbSize=ctypes.sizeof(CMINVOKECOMMANDINFOEX), fMask=CMIC_MASK_UNICODE,
                                     hwnd=hwnd or None, nShow=1)
        if offset is None:
            info.lpVerb, info.lpVerbW = verb.encode("ascii"), verb
        else:   # MAKEINTRESOURCE(offset)
            info.lpVerb = ctypes.cast(c_void_p(offset), wintypes.LPCSTR)
            info.lpVerbW = ctypes.cast(c_void_p(offset), wintypes.LPCWSTR)
        if where:
            info.fMask |= CMIC_MASK_PTINVOKE
            info.ptInvoke = wintypes.POINT(*where)
        _call(menu, 4, (POINTER(CMINVOKECOMMANDINFOEX),), byref(info))
    finally:
        user32.DestroyMenu(hmenu)
        _release(menu)


def _find_verb(menu, hmenu, verb: str) -> int | None:
    """The number of the menu item whose verb is `verb`, counted from the first number QueryContextMenu used (1)."""
    for i in range(user32.GetMenuItemCount(hmenu)):
        command = user32.GetMenuItemID(hmenu, i)
        if command <= 0:   # a separator (0) or a submenu (-1)
            continue
        name = ctypes.create_unicode_buffer(256)
        try:
            _call(menu, 5, (ctypes.c_size_t, wintypes.UINT, c_void_p, c_void_p, wintypes.UINT), command - 1, 4, None,
                  name, 256)   # GetCommandString, GCS_VERBW
        except OSError:
            continue
        if name.value.lower() == verb.lower():
            return command - 1
    return None


def properties(path: str) -> None:
    shell32.SHObjectProperties(None, 0x2, path, None)   # SHOP_FILEPATH


# -- Open with


class OPENASINFO(ctypes.Structure):
    _fields_ = [("pcszFile", wintypes.LPCWSTR), ("pcszClass", wintypes.LPCWSTR), ("oaifInFlags", ctypes.c_int)]


def choose_app(path: str, hwnd: int = 0) -> None:
    """Windows' "Choose another app" window; it opens the file in the app picked."""
    info = OPENASINFO(path, None, 0x1 | 0x2 | 0x4)   # OAIF_ALLOW_REGISTRATION | OAIF_REGISTER_EXT | OAIF_EXEC
    shell32.SHOpenWithDialog(wintypes.HWND(hwnd or 0), byref(info))


@dataclass
class App:
    """An app Explorer's "Open with" lists for a file type. Call close() when the menu is gone."""
    name: str
    icon: str          # a file the icon is in (an .exe or a .png), or ""
    icon_index: int
    handler: c_void_p

    def open(self, path: str) -> None:
        data = _handler(path, BHID_DataObject, IID_IDataObject)
        try:
            _call(self.handler, 8, (c_void_p,), data)   # IAssocHandler::Invoke
        finally:
            _release(data)

    def close(self) -> None:
        _release(self.handler)
        self.handler = c_void_p()


def open_with_apps(extension: str) -> list[App]:
    """The recommended apps for `extension` (".jpg"), as Explorer's Open with submenu has them."""
    found = c_void_p()
    try:
        shell32.SHAssocEnumHandlers(extension, 0x1, byref(found))   # ASSOC_FILTER_RECOMMENDED
    except OSError:
        return []
    apps = []
    try:
        while True:
            handler, fetched = c_void_p(), wintypes.ULONG()
            _call(found, 3, (wintypes.ULONG, POINTER(c_void_p), POINTER(wintypes.ULONG)), 1, byref(handler),
                  byref(fetched))
            if not fetched.value:
                break
            try:
                name = c_void_p()
                _call(handler, 4, (POINTER(c_void_p),), byref(name))   # GetUIName
                icon, index = c_void_p(), ctypes.c_int()
                try:
                    _call(handler, 5, (POINTER(c_void_p), POINTER(ctypes.c_int)), byref(icon), byref(index))
                    icon_text = _take_string(icon)
                except OSError:
                    icon_text = ""
                apps.append(App(_take_string(name), _icon_file(icon_text), index.value, handler))
            except OSError:
                _release(handler)
    finally:
        _release(found)
    return apps


def _icon_file(location: str) -> str:
    """A Store app's icon is "@{Package?ms-resource://…}": Windows turns that into the .png it means."""
    if location.startswith("@"):
        buffer = ctypes.create_unicode_buffer(1024)
        try:
            ctypes.windll.shlwapi.SHLoadIndirectString(location, buffer, 1024, None)
            location = buffer.value
        except OSError:
            return ""
    location = os.path.expandvars(location.strip('"'))
    return location if location and os.path.isfile(location) else ""


# -- Send to


def send_to_targets() -> list[str]:
    """What Explorer's Send to lists: the SendTo folder's items (zip, desktop shortcut, apps…), then removable drives."""
    folder = c_void_p()
    try:
        shell32.SHGetKnownFolderPath(byref(FOLDERID_SendTo), 0, None, byref(folder))
        sendto = Path(_take_string(folder))
    except OSError:
        sendto = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "SendTo"
    targets = []
    try:
        for entry in sorted(os.scandir(sendto), key=lambda e: e.name.lower()):
            hidden = entry.stat().st_file_attributes & 0x6   # FILE_ATTRIBUTE_HIDDEN | SYSTEM: desktop.ini
            if not hidden:
                targets.append(entry.path)
    except OSError:
        pass
    drives = kernel32.GetLogicalDrives()
    for i in range(26):
        root = f"{chr(65 + i)}:\\"
        if drives >> i & 1 and kernel32.GetDriveTypeW(root) == 2:   # DRIVE_REMOVABLE
            targets.append(root)
    return targets


class POINTL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def send_to(target: str, path: str) -> None:
    """Drops the file on a Send to item, as Explorer does: a copy, never a move."""
    data = _handler(path, BHID_DataObject, IID_IDataObject)
    try:
        drop = _handler(target, BHID_SFUIObject, IID_IDropTarget)
        try:
            argtypes = (c_void_p, wintypes.DWORD, POINTL, POINTER(wintypes.DWORD))
            effect = wintypes.DWORD(0x1 | 0x4)   # DROPEFFECT_COPY | DROPEFFECT_LINK
            _call(drop, 3, argtypes, data, 0x1, POINTL(0, 0), byref(effect))   # DragEnter, MK_LBUTTON
            if effect.value:
                _call(drop, 6, argtypes, data, 0x1, POINTL(0, 0), byref(effect))   # Drop
            else:
                _call(drop, 5)   # DragLeave
        finally:
            _release(drop)
    finally:
        _release(data)
