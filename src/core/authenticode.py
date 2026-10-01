"""Inspect embedded Authenticode signatures through the Windows trust API.

This is a runtime diagnostic, not an authorization or malware check. It uses
the local certificate cache and never launches a shell or downloads a file.
Catalog-only signatures are outside the scope of this embedded-signature probe.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.POINTER(_GUID))]


class _WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
        ("dwStateAction", wintypes.DWORD), ("hWVTStateData", wintypes.HANDLE),
        ("pwszURLReference", wintypes.LPWSTR), ("dwProvFlags", wintypes.DWORD),
        ("dwUIContext", wintypes.DWORD), ("pSignatureSettings", ctypes.c_void_p),
    ]


def authenticode_status(path: Path) -> str:
    if os.name != "nt":
        return "Unavailable"
    try:
        path = Path(path).resolve()
        if not path.is_file():
            return "Missing"
        trust = ctypes.WinDLL("wintrust.dll", use_last_error=True).WinVerifyTrust
        trust.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(_WINTRUST_DATA)]
        trust.restype = wintypes.LONG  # Only zero means success; this is not a BOOL.
        action = _GUID(0x00AAC56B, 0xCD44, 0x11D0,
                       (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
        file_info = _WINTRUST_FILE_INFO()
        file_info.cbStruct = ctypes.sizeof(file_info)
        file_info.pcwszFilePath = str(path)
        data = _WINTRUST_DATA()
        data.cbStruct = ctypes.sizeof(data)
        data.dwUIChoice = 2  # WTD_UI_NONE
        data.dwUnionChoice = 1  # WTD_CHOICE_FILE
        data.pFile = ctypes.pointer(file_info)
        data.dwStateAction = 1  # WTD_STATEACTION_VERIFY
        data.dwProvFlags = 0x1000 | 0x2000  # Cache-only retrieval; reject MD2/MD4.
        try:
            result = int(trust(wintypes.HWND(-1), ctypes.byref(action), ctypes.byref(data))) & 0xFFFFFFFF
        finally:
            data.dwStateAction = 2  # WTD_STATEACTION_CLOSE, also after verification failures.
            trust(wintypes.HWND(-1), ctypes.byref(action), ctypes.byref(data))
        if result == 0:
            return "Valid"
        return {
            0x800B0100: "NotSigned",  # TRUST_E_NOSIGNATURE
            0x80096010: "HashMismatch",  # TRUST_E_BAD_DIGEST
            0x800B0004: "NotTrusted",  # TRUST_E_SUBJECT_NOT_TRUSTED
            0x800B0109: "NotTrusted",  # CERT_E_UNTRUSTEDROOT
            0x800B010A: "NotTrusted",  # CERT_E_CHAINING
            0x800B010C: "NotTrusted",  # CERT_E_REVOKED
            0x800B0101: "NotTrusted",  # CERT_E_EXPIRED
            0x800B010E: "Unavailable",  # CERT_E_REVOCATION_FAILURE
            0x80092013: "Unavailable",  # CRYPT_E_REVOCATION_OFFLINE
        }.get(result, "UnknownError")
    except (AttributeError, OSError, ValueError):
        return "Unavailable"
