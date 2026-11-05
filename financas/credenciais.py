"""Guarda e lê o Client ID e o Client Secret da Pluggy no Gerenciador de
Credenciais do Windows, usando só a biblioteca padrão (ctypes + advapi32.dll).

A credencial é gravada com persistência LOCAL_MACHINE: fica só neste PC,
criptografada pelo Windows com a sua senha de login (DPAPI), e não é
sincronizada com a conta Microsoft nem com outros computadores.
Para conferir ou apagar à mão: Painel de Controle > Gerenciador de
Credenciais > Credenciais do Windows > "financas-pluggy-...".
"""

import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":
    raise RuntimeError("Este módulo só funciona no Windows.")

PREFIXO_ALVO = "financas-pluggy-"

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_CredWriteW = _advapi32.CredWriteW
_CredWriteW.argtypes = [_PCREDENTIALW, wintypes.DWORD]
_CredWriteW.restype = wintypes.BOOL

_CredReadW = _advapi32.CredReadW
_CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_PCREDENTIALW)]
_CredReadW.restype = wintypes.BOOL

_CredDeleteW = _advapi32.CredDeleteW
_CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
_CredDeleteW.restype = wintypes.BOOL

_CredFree = _advapi32.CredFree
_CredFree.argtypes = [ctypes.c_void_p]
_CredFree.restype = None


class CredencialAusente(Exception):
    pass


def alvo(perfil: str) -> str:
    if perfil not in ("teste", "real"):
        raise ValueError("perfil deve ser 'teste' ou 'real'")
    return PREFIXO_ALVO + perfil


def _gravar_alvo(nome_alvo: str, client_id: str, client_secret: str) -> None:
    dados = client_secret.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(dados)).from_buffer_copy(dados)
    credencial = _CREDENTIALW()
    credencial.Type = _CRED_TYPE_GENERIC
    credencial.TargetName = nome_alvo
    credencial.Comment = "Painel de financas: credencial da API Pluggy (somente leitura)"
    credencial.CredentialBlobSize = len(dados)
    credencial.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    credencial.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credencial.UserName = client_id
    if not _CredWriteW(ctypes.byref(credencial), 0):
        raise OSError(f"Falha ao gravar no Gerenciador de Credenciais (erro {ctypes.get_last_error()}).")


def _ler_alvo(nome_alvo: str) -> tuple[str, str]:
    ponteiro = _PCREDENTIALW()
    if not _CredReadW(nome_alvo, _CRED_TYPE_GENERIC, 0, ctypes.byref(ponteiro)):
        erro = ctypes.get_last_error()
        if erro == _ERROR_NOT_FOUND:
            raise CredencialAusente(f"Credencial '{nome_alvo}' não encontrada no Gerenciador de Credenciais.")
        raise OSError(f"Falha ao ler o Gerenciador de Credenciais (erro {erro}).")
    try:
        conteudo = ponteiro.contents
        client_id = conteudo.UserName or ""
        segredo = ctypes.string_at(conteudo.CredentialBlob, conteudo.CredentialBlobSize).decode("utf-8")
    finally:
        _CredFree(ponteiro)
    return client_id, segredo


def _apagar_alvo(nome_alvo: str) -> bool:
    if _CredDeleteW(nome_alvo, _CRED_TYPE_GENERIC, 0):
        return True
    erro = ctypes.get_last_error()
    if erro == _ERROR_NOT_FOUND:
        return False
    raise OSError(f"Falha ao apagar do Gerenciador de Credenciais (erro {erro}).")


def gravar(perfil: str, client_id: str, client_secret: str) -> None:
    _gravar_alvo(alvo(perfil), client_id, client_secret)


def ler(perfil: str) -> tuple[str, str]:
    try:
        return _ler_alvo(alvo(perfil))
    except CredencialAusente:
        raise CredencialAusente(
            f"Credencial do perfil '{perfil}' não encontrada. Rode: .\\financas.cmd configurar --perfil {perfil}"
        ) from None


def apagar(perfil: str) -> bool:
    return _apagar_alvo(alvo(perfil))
