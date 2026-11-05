"""Ponto de entrada. Use sempre o atalho: .\\financas.cmd <comando>
(ele roda o Python isolado, com -I -S, pelo iniciador financas\\_iniciar.py)."""

import argparse
import sqlite3
import sys

from . import config, mascara
from .credenciais import CredencialAusente
from .pluggy import ErroPluggy


def criar_analisador() -> argparse.ArgumentParser:
    # allow_abbrev=False: '--com' não vira '--com-somas', para nenhuma regra de permissão exata
    # ser contornada por abreviação. Quando --perfil se repete, vale o último (comportamento do argparse).
    analisador = argparse.ArgumentParser(prog="financas", description="Painel local de finanças (somente leitura).",
                                         allow_abbrev=False)
    comandos = analisador.add_subparsers(dest="comando", required=True)
    original = comandos.add_parser
    comandos.add_parser = lambda *a, **k: original(*a, allow_abbrev=False, **k)

    c = comandos.add_parser("configurar", help="guarda Client ID e Client Secret de um perfil")
    c.add_argument("--perfil", choices=config.PERFIS, required=True)
    c.add_argument("--apagar", action="store_true", help="apaga a credencial e os itemIds do perfil")

    i = comandos.add_parser("itens", help="cadastra os itemIds de um perfil")
    i.add_argument("--perfil", choices=config.PERFIS, required=True)

    v = comandos.add_parser("verificar", help="mostra o status das conexões de um perfil")
    v.add_argument("--perfil", choices=config.PERFIS, required=True)

    k = comandos.add_parser("coletar", help="lê contas, lançamentos e faturas e grava no banco local")
    k.add_argument("--perfil", choices=config.PERFIS, required=True)
    k.add_argument("--desde", metavar="AAAA-MM-DD", help="relê a partir desta data (padrão: últimos 60 dias)")
    k.add_argument("--saldos", action="store_true",
                   help="mostra o saldo atual de cada conta na tela (no perfil real, só no terminal do dono)")

    g = comandos.add_parser("categorizar", help="classifica os lançamentos e gera a página local de revisão")
    g.add_argument("--perfil", choices=config.PERFIS, required=True)
    g.add_argument("--detalhar", action="store_true", help="contagens por conta, categoria e regra")
    g.add_argument("--so-contagens", action="store_true", help="resumo só com contagens (padrão no perfil real)")
    g.add_argument("--com-somas", action="store_true",
                   help="no perfil real, mostra também as somas por categoria (só no terminal do dono)")

    r = comandos.add_parser("relatorio", help="gera o painel HTML local do último mês fechado")
    r.add_argument("--perfil", choices=config.PERFIS, required=True)
    r.add_argument("--mes", metavar="AAAA-MM", help="mês do painel (padrão: último mês fechado)")
    return analisador


def so_no_terminal_do_dono_ok(args) -> bool:
    """--com-somas e --saldos mostram valores do perfil real: só no terminal do dono, nunca com a
    entrada ou a saída redirecionada (assistente de IA, tarefa agendada)."""
    if args.perfil != "real" or not (getattr(args, "com_somas", False) or getattr(args, "saldos", False)):
        return True
    from .categorizador import console_do_dono
    return console_do_dono()


def _executar(args) -> int:
    try:
        config.verificar_local()
        config.verificar_local(config.PASTA_DADOS_REAIS)  # a pasta de dados reais também: na pasta do usuário e fora da nuvem
        if config.PASTA_DADOS_REAIS.resolve().is_relative_to(config.RAIZ):
            raise config.LocalInseguro("A pasta de dados reais não pode ficar dentro da pasta do projeto.")
        if args.comando == "configurar":
            from .configurar import configurar
            return configurar(args.perfil, args.apagar)
        if args.comando == "itens":
            from .configurar import itens
            return itens(args.perfil)
        if args.comando == "verificar":
            from .verificar import verificar
            return verificar(args.perfil)
        if args.comando == "coletar":
            from .coletor import coletar
            return coletar(args.perfil, args.desde, args.saldos)
        if args.comando == "categorizar":
            from .categorizador import categorizar
            return categorizar(args.perfil, args.detalhar, args.so_contagens, args.com_somas)
        if args.comando == "relatorio":
            from .relatorio import relatorio
            return relatorio(args.perfil, args.mes)
    except (ErroPluggy, CredencialAusente, config.LocalInseguro, ValueError) as erro:
        print(f"Erro: {mascara.texto(str(erro), 300)}", file=sys.stderr)
        return 1
    except sqlite3.Error as erro:
        print(f"Erro no banco local: {mascara.texto(str(erro), 300)}", file=sys.stderr)
        return 1
    except OSError as erro:
        print(f"Erro do sistema: {mascara.texto(str(erro), 300)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        return 130
    except Exception as erro:  # qualquer outro erro: só o tipo, sem detalhes que possam conter dados
        print(f"Erro inesperado ({type(erro).__name__}). Rode de novo; se repetir, veja \"Se der erro\" no README.",
              file=sys.stderr)
        return 1
    return 2


def _avisar_coleta_falhou(perfil: str) -> None:
    try:
        from .relatorio import avisar_coleta_falhou
        avisar_coleta_falhou(perfil)
    except Exception:  # o aviso nunca esconde o erro da coleta
        print("Aviso: não foi possível marcar o painel como desatualizado.", file=sys.stderr)


def main(argv=None) -> int:
    args = criar_analisador().parse_args(argv)
    if not so_no_terminal_do_dono_ok(args):
        print("Erro: no perfil real, --com-somas e --saldos só rodam no terminal do dono "
              "(uma janela do PowerShell aberta por você).", file=sys.stderr)
        return 2
    resultado = _executar(args)
    if args.comando == "coletar" and args.perfil == "real" and resultado != 0:
        _avisar_coleta_falhou(args.perfil)  # o painel antigo ganha o aviso "Painel desatualizado"
    return resultado


if __name__ == "__main__":
    sys.exit(main())
