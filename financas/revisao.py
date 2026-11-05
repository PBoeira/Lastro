"""Página local de revisão: HTML autocontido, sem nenhum recurso externo.

Lista os lançamentos duvidosos ou sem categoria para o dono decidir (opcional). Fica em
%USERPROFILE%\\financas-dados\\relatorios\\revisao.html (perfil real) ou dados\\teste\\relatorios\\revisao.html.
Os textos já vêm mascarados do banco; aqui só são escapados para HTML.
"""

import html
import os
import re
from collections import defaultdict
from pathlib import Path

from . import config, datas, formato

_CABECALHO = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Revisão de categorias</title>
<style>
:root { --fundo:#fbfbfa; --texto:#1d1d1b; --suave:#6b6b66; --linha:#e4e3df; --cartao:#ffffff;
        --saida:#a3312a; --entrada:#2f6b3a; --destaque:#f3efe4; }
@media (prefers-color-scheme: dark) {
  :root { --fundo:#171716; --texto:#ecebe7; --suave:#a3a29c; --linha:#33322f; --cartao:#201f1d;
          --saida:#e58b84; --entrada:#8fcf9a; --destaque:#2a2823; } }
body { margin:0; background:var(--fundo); color:var(--texto);
       font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:18px; margin:32px 0 8px; }
p, li { color:var(--suave); } code, pre { font-family:ui-monospace, Consolas, monospace; font-size:13px; }
pre { background:var(--destaque); padding:8px 10px; border-radius:6px; overflow-x:auto; margin:0; }
.tabela { overflow-x:auto; background:var(--cartao); border:1px solid var(--linha); border-radius:8px; }
table { border-collapse:collapse; width:100%; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--linha); vertical-align:top; }
th { font-size:13px; color:var(--suave); font-weight:600; position:sticky; top:0; background:var(--cartao); }
td.valor { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
.saida { color:var(--saida); } .entrada { color:var(--entrada); }
.etiqueta { font-size:12px; color:var(--suave); white-space:nowrap; }
</style></head><body><main>
"""


def _e(valor) -> str:
    return html.escape("" if valor is None else str(valor))


def assinatura(descricao: str | None) -> str:
    """Agrupa variações do mesmo estabelecimento: sem números, máscaras e pontuação."""
    texto = re.sub(r"[•…]", " ", descricao or "")
    texto = re.sub(r"\d+", " ", texto)
    texto = re.sub(r"[^\w\s*&]", " ", texto)
    return " ".join(texto.lower().split())[:40]


def _conta(linha) -> str:
    instituicao = (linha["instituicao"] or "").replace(" (via MeuPluggy)", "")
    tipo = "cartão" if linha["conta_tipo"] == "CREDIT" else "conta"
    return f"{instituicao} {tipo} ••••{linha['numero_final'] or ''}".strip()


def _valor(centavos: int) -> str:
    classe = "entrada" if centavos > 0 else "saida"
    return f'<td class="valor {classe}">{_e(formato.reais(centavos))}</td>'


def _montar(perfil: str, linhas, categorias: dict) -> str:
    from .categorizador import id_curto  # import tardio: evita ciclo

    duvidosos = sorted((l for l in linhas if l["duvidoso"]), key=lambda l: l["data_utc"], reverse=True)
    partes = [_CABECALHO]
    partes.append(f"<h1>Revisão de categorias</h1><p>Perfil <b>{_e(perfil)}</b>, gerado em "
                  f"{_e(datas.agora().strftime('%d/%m/%Y %H:%M'))}. "
                  f"{len(duvidosos)} {'lançamento duvidoso' if len(duvidosos) == 1 else 'lançamentos duvidosos'} "
                  f"ou sem categoria, de {len(linhas)} no total. Página só deste computador: não compartilhe.</p>")
    confirmar = (" O programa avisa que os ajustes mudaram: digite <code>CONFIRMO</code> e Enter."
                 if perfil == "real" else "")
    partes.append(f"""<h2>Refinar categorias (opcional)</h2><p>O painel funciona sem nada desta página.
Use-a só se quiser refinar as categorias. Os arquivos citados abaixo ficam na pasta do projeto (a mesma do
<code>financas.cmd</code>); crie o arquivo se ele ainda não existir.</p><ul>
<li><b>Um lançamento:</b> copie a linha da última coluna para o arquivo <code>ajustes.local.toml</code>,
abaixo de uma linha <code>[lancamentos]</code>, e troque <code>ESCOLHA UMA CATEGORIA</code> por uma das
categorias do fim desta página.</li>
<li><b>Um padrão que se repete:</b> crie uma regra em <code>regras.local.toml</code> (há um modelo em cada
padrão abaixo). Regras pessoais vão no <code>regras.local.toml</code>, que fica fora do repositório; o
<code>regras.toml</code> é só para regras genéricas.</li>
<li><b>Pix para pessoas:</b> prefira <code>ajustes.local.toml</code>, que só guarda o código do lançamento.
Se você usa um assistente de IA neste projeto, ele pode ler os arquivos de regras: não coloque nomes de pessoas
neles.</li>
<li>Depois, numa janela do PowerShell aberta por você, rode
<code>.\\financas.cmd categorizar --perfil {_e(perfil)}</code>.{confirmar} Em seguida rode
<code>.\\financas.cmd relatorio --perfil {_e(perfil)}</code> para refazer o painel e abra esta página de
novo.</li></ul>""")

    grupos = defaultdict(lambda: {"qtd": 0, "soma": 0, "categorias": defaultdict(int)})
    for linha in duvidosos:
        grupo = grupos[assinatura(linha["descricao"])]
        grupo["qtd"] += 1
        grupo["soma"] += linha["valor_centavos"]
        grupo["categorias"][linha["categoria"]] += 1
    frequentes = sorted(((a, g) for a, g in grupos.items() if a), key=lambda x: (-x[1]["qtd"], x[0]))[:40]
    if frequentes:
        partes.append('<h2>Padrões mais frequentes entre os duvidosos</h2><div class="tabela"><table>'
                      "<tr><th>Padrão</th><th>Lanç.</th><th>Total</th><th>Categoria atual</th>"
                      "<th>Modelo de regra para regras.local.toml</th></tr>")
        for padrao, grupo in frequentes:
            atual = max(grupo["categorias"], key=grupo["categorias"].get)
            # nome genérico: o regras.toml é lido pelo Claude quando ele edita regras, e o nome
            # da regra aparece na saída do categorizar. O padrão em si fica por sua conta.
            modelo = ("[[regra]]\nnome = \"minha regra\"\npessoa = false\n"
                      f"descricao = '{re.escape(padrao).replace(chr(92) + ' ', ' ')}'\n"
                      f"categoria = \"ESCOLHA UMA CATEGORIA\"")
            partes.append(f"<tr><td>{_e(padrao)}</td><td>{grupo['qtd']}</td>{_valor(grupo['soma'])}"
                          f"<td>{_e(atual)}</td><td><pre>{_e(modelo)}</pre></td></tr>")
        partes.append("</table></div>")

    partes.append("<h2>Lançamentos duvidosos</h2>")
    if not duvidosos:
        partes.append("<p>Nada para revisar.</p>")
    partes.append('<div class="tabela"><table>'
                  "<tr><th>Data</th><th>Conta</th><th>Descrição</th><th>Contraparte</th><th>Valor</th>"
                  "<th>Categoria atual</th><th>Linha para ajustes.local.toml</th></tr>")
    for linha in duvidosos:
        contraparte = " ".join(filter(None, (linha["contraparte_nome"], linha["contraparte_doc"])))
        ajuste = f'"{id_curto(linha["id"])}" = "ESCOLHA UMA CATEGORIA"'
        partes.append(
            f"<tr><td>{_e(datas.formatar(linha['dia'], com_hora=False))}</td>"
            f"<td class=\"etiqueta\">{_e(_conta(linha))}</td><td>{_e(linha['descricao'])}</td>"
            f"<td>{_e(contraparte)}</td>{_valor(linha['valor_centavos'])}<td>{_e(linha['categoria'])}"
            f"<div class=\"etiqueta\">{_e(linha['regra'])}</div></td><td><code>{_e(ajuste)}</code></td></tr>")
    partes.append("</table></div>")

    partes.append("<h2>Categorias disponíveis</h2><ul>")
    for natureza in ("despesa", "receita", "neutro"):
        nomes = ", ".join(_e(n) for n, nat in categorias.items() if nat == natureza)
        partes.append(f"<li><b>{natureza}:</b> {nomes}</li>")
    partes.append("</ul></main></body></html>\n")
    return "".join(partes)


def gerar(perfil: str, linhas, categorias: dict) -> Path:
    pasta = config.pasta_relatorios(perfil)
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / "revisao.html"
    temporario = destino.with_name("revisao.tmp")
    temporario.write_text(_montar(perfil, linhas, categorias), encoding="utf-8")
    os.replace(temporario, destino)
    return destino
