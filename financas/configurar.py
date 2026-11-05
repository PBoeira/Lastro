"""Comandos 'configurar' e 'itens': guardam a credencial e os itemIds de um perfil.

O Client Secret é digitado (ou colado) sem aparecer na tela, testado na API e,
só se for válido, gravado no Gerenciador de Credenciais do Windows.
"""

import getpass
import re

from . import config, credenciais, mascara, pluggy
from .verificar import cliente_do_perfil, mostrar_item

_UUID_RE = re.compile(pluggy.UUID)


def _exigir_terminal() -> None:
    # console_do_dono e não sys.stdin.isatty(): no Windows o isatty também é verdadeiro para o NUL,
    # que é a entrada dos comandos de um assistente de IA e da tarefa agendada.
    from .categorizador import console_do_dono
    if not console_do_dono():
        raise SystemExit("Rode este comando direto numa janela do PowerShell ou do Terminal do Windows.")


def configurar(perfil: str, apagar: bool = False) -> int:
    if apagar:
        if perfil == "real":
            _exigir_terminal()
            print("Isto apaga a credencial do perfil real e a lista de itemIds. Os dados já coletados ficam.")
            if input("Digite APAGAR para confirmar (ou só Enter para cancelar): ").strip().upper() != "APAGAR":
                print("Nada foi apagado.")
                return 0
        removida = credenciais.apagar(perfil)
        config.remover_perfil(perfil)
        estado = "apagada" if removida else "não existia"
        print(f"Credencial '{credenciais.alvo(perfil)}' {estado}; itemIds do perfil removidos.")
        return 0

    _exigir_terminal()
    print(f"Configurando o perfil '{perfil}'.")
    print("Nenhum dos dois campos aparece enquanto você cola. Cole cada um uma vez e aperte Enter.")
    client_id = getpass.getpass("Client ID: ").strip()
    if not _UUID_RE.fullmatch(client_id):
        print("Isso não parece um Client ID (formato xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx). Nada foi salvo.")
        return 2
    print(f"Client ID recebido: {mascara.final(client_id)}")
    segredo = getpass.getpass("Client Secret: ").strip()
    if not segredo:
        print("Client Secret vazio. Nada foi salvo.")
        return 2

    print("Testando a credencial na API da Pluggy...")
    cliente = pluggy.Cliente(client_id, segredo)
    cliente.autenticar()  # se falhar, levanta ErroPluggy e nada é salvo
    credenciais.gravar(perfil, client_id, segredo)
    del segredo
    print(f"Credencial válida e salva como '{credenciais.alvo(perfil)}' (Client ID {mascara.final(client_id)}).")
    print("Dica: limpe a área de transferência (Win+V, depois 'Limpar tudo') para o secret não ficar lá.")

    resposta = input("Quer cadastrar os itemIds agora? [s/N] ").strip().lower()
    if resposta in ("s", "sim"):
        return itens(perfil, cliente)
    print(f"Quando tiver os itemIds, rode: .\\financas.cmd itens --perfil {perfil}")
    return 0


def itens(perfil: str, cliente: pluggy.Cliente | None = None) -> int:
    _exigir_terminal()
    cliente = cliente or cliente_do_perfil(perfil)
    atuais = config.itens(perfil)
    if atuais:
        print(f"Itens atuais: {', '.join(mascara.final(i) for i in atuais)}")
        print("A lista que você colar SUBSTITUI a atual.")
    entrada = input("Cole os itemIds separados por vírgula (Enter para cancelar): ").strip()
    if not entrada:
        print("Nada mudou.")
        return 0

    ids: list[str] = []
    for parte in re.split(r"[,;\s]+", entrada):
        if not parte:
            continue
        if not _UUID_RE.fullmatch(parte):
            print(f"'{mascara.final(parte)}' não parece um itemId. Nada foi salvo.")
            return 2
        if parte.lower() not in ids:
            ids.append(parte.lower())

    print("Conferindo cada item na API...")
    for item_id in ids:
        mostrar_item(cliente.item(item_id), cliente.contas(item_id))  # itemId errado: ErroPluggy, nada é salvo
    config.definir_itens(perfil, ids)
    print(f"{len(ids)} item(ns) salvo(s) em config.local.json para o perfil '{perfil}'.")
    return 0
