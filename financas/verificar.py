"""Comando 'verificar': mostra o status de cada conexão (item) de um perfil."""

from . import config, credenciais, datas, instituicoes, mascara, pluggy


def cliente_do_perfil(perfil: str) -> pluggy.Cliente:
    client_id, segredo = credenciais.ler(perfil)
    return pluggy.Cliente(client_id, segredo)


def mostrar_item(item: dict, contas: list[dict] | None = None) -> None:
    # Todo texto vindo da API passa por mascara.texto (sem controles, números longos mascarados).
    print(f"Item {mascara.final(item.get('id'))}  {instituicoes.nome(item, contas or [])}")
    print(f"  status: {mascara.texto(item.get('status'), 40)} ({mascara.texto(item.get('executionStatus'), 40)})")
    if item.get("lastUpdatedAt"):
        print(f"  última sincronização: {datas.formatar(item['lastUpdatedAt'])}")
    expira = item.get("consentExpiresAt")
    if expira:
        dias = (datas.ler_iso(expira) - datas.agora()).days
        aviso = "  ATENÇÃO: renove a autorização" if dias < 30 else ""
        print(f"  autorização expira em: {datas.formatar(expira, com_hora=False)} ({dias} dias){aviso}")
    erro = item.get("error")
    if isinstance(erro, dict) and (erro.get("code") or erro.get("message")):
        codigo = mascara.texto(erro.get("code"), 50)
        mensagem = mascara.texto(pluggy.mascarar_ids(str(erro.get("message", ""))), 200)
        print(f"  erro: {codigo}: {mensagem}")
    detalhe = item.get("statusDetail")
    if isinstance(detalhe, dict):
        faltando = [nome for nome, valor in detalhe.items() if isinstance(valor, dict) and valor.get("isUpdated") is False]
        if faltando:
            print(f"  produtos não atualizados: {mascara.texto(', '.join(faltando), 200)}")


def verificar(perfil: str) -> int:
    ids = config.itens(perfil)
    if not ids:
        print(f"Nenhum item cadastrado no perfil '{perfil}'. Rode: .\\financas.cmd itens --perfil {perfil}")
        return 1
    cliente = cliente_do_perfil(perfil)
    for item_id in ids:
        mostrar_item(cliente.item(item_id), cliente.contas(item_id))
    return 0
