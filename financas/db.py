"""Banco local SQLite: um arquivo por perfil. O real fica em %USERPROFILE%\\financas-dados\\real\\
(fora da pasta do projeto); o de teste, em dados\\teste\\ dentro do projeto.

O que NÃO é gravado, por minimização: números completos de conta e cartão,
CPF completo de contrapartes, CPF do titular, agência e conta de terceiros,
e o JSON bruto da API. Só ficam os últimos dígitos e indicadores (ex.: se a
contraparte é o próprio titular). Textos livres (descrição, nomes) são gravados
já mascarados: qualquer sequência de 5 ou mais dígitos vira '••••' + 4 últimos.
O banco roda com secure_delete, então dados sobrescritos são zerados no arquivo.
"""

import sqlite3
from pathlib import Path

from . import config, mascara

VERSAO = 6

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS itens (
    id                    TEXT PRIMARY KEY,
    conector              TEXT,
    instituicao           TEXT,               -- ex.: 'Nubank (via MeuPluggy)'
    sandbox               INTEGER,
    open_finance          INTEGER,
    status                TEXT,
    status_execucao       TEXT,
    ultima_sincronizacao  TEXT,
    autorizacao_expira    TEXT,
    visto_em              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contas (
    id                          TEXT PRIMARY KEY,
    item_id                     TEXT NOT NULL REFERENCES itens(id),
    tipo                        TEXT NOT NULL,      -- BANK ou CREDIT
    subtipo                     TEXT,
    nome                        TEXT,
    numero_final                TEXT,               -- só os últimos 4 dígitos
    moeda                       TEXT,
    saldo_centavos              INTEGER,
    limite_centavos             INTEGER,
    limite_disponivel_centavos  INTEGER,
    fechamento                  TEXT,
    vencimento                  TEXT,
    convencao_sinal             TEXT,               -- cartões: 'positivo_e_gasto' ou 'negativo_e_gasto'
    substituida_a_partir        TEXT,               -- reconexão com item novo: dia em que a conta nova assume
    visto_em                    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saldos_diarios (
    conta_id        TEXT NOT NULL REFERENCES contas(id),
    dia             TEXT NOT NULL,
    saldo_centavos  INTEGER,
    PRIMARY KEY (conta_id, dia)
);

CREATE TABLE IF NOT EXISTS transacoes (
    id                            TEXT PRIMARY KEY,   -- id da Pluggy
    conta_id                      TEXT NOT NULL REFERENCES contas(id),
    dia                           TEXT NOT NULL,      -- data no horário de Brasília
    data_utc                      TEXT NOT NULL,      -- data como veio da API
    descricao                     TEXT,
    descricao_bruta               TEXT,
    valor_api_centavos            INTEGER NOT NULL,   -- valor da API na moeda da conta (o sinal varia; ver a view)
    moeda                         TEXT,               -- moeda da compra (USD numa compra internacional)
    tipo_api                      TEXT,               -- DEBIT ou CREDIT
    status                        TEXT,               -- PENDING ou POSTED
    tipo_operacao                 TEXT,               -- PIX, TED, BOLETO, PAGAMENTO_FATURA, ESTORNO...
    metodo_pagamento              TEXT,
    categoria_api                 TEXT,
    categoria_api_id              TEXT,
    parcela_numero                INTEGER,
    parcela_total                 INTEGER,
    parcela_valor_total_centavos  INTEGER,
    data_compra                   TEXT,
    fatura_id                     TEXT,
    contraparte_nome              TEXT,
    contraparte_doc               TEXT,               -- CNPJ completo; CPF só com 2 dígitos
    contraparte_mesmo_titular     INTEGER,            -- 1 = conta/CPF do próprio titular
    comerciante                   TEXT,
    comerciante_cnpj              TEXT,
    comerciante_categoria         TEXT,
    saldo_apos_centavos           INTEGER,
    provider_id                   TEXT,               -- não usado: fica sempre vazio, por minimização
    criado_api                    TEXT,
    atualizado_api                TEXT,
    primeira_vez_em               TEXT NOT NULL,
    visto_em                      TEXT NOT NULL,
    removido_em                   TEXT                -- sumiu da API (ex.: recriado com id novo)
);
CREATE INDEX IF NOT EXISTS ix_transacoes_conta_dia ON transacoes(conta_id, dia);

CREATE TABLE IF NOT EXISTS faturas (
    id                   TEXT PRIMARY KEY,
    conta_id             TEXT NOT NULL REFERENCES contas(id),
    vencimento           TEXT,
    fechamento           TEXT,
    total_centavos       INTEGER,
    moeda                TEXT,
    minimo_centavos      INTEGER,
    iof_centavos         INTEGER,
    encargos_centavos    INTEGER,   -- juros, multa e outros, sem IOF
    pagamentos_centavos  INTEGER,
    visto_em             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execucoes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    inicio  TEXT NOT NULL,
    fim     TEXT,
    ok      INTEGER,
    resumo  TEXT
);

-- Única regra de sinal do projeto: valor_centavos negativo = dinheiro saindo (gasto),
-- positivo = dinheiro entrando. Lançamentos removidos ficam de fora, e os de uma conta substituída
-- (reconexão que criou item novo) só entram antes do dia em que a conta nova assume.
CREATE VIEW IF NOT EXISTS lancamentos AS
SELECT
    t.*,
    c.tipo AS conta_tipo,
    CASE
        WHEN c.tipo = 'CREDIT' AND c.convencao_sinal = 'negativo_e_gasto' THEN t.valor_api_centavos
        WHEN c.tipo = 'CREDIT' THEN -t.valor_api_centavos
        WHEN t.tipo_api = 'DEBIT' THEN -abs(t.valor_api_centavos)
        ELSE abs(t.valor_api_centavos)
    END AS valor_centavos
FROM transacoes t
JOIN contas c ON c.id = t.conta_id
WHERE t.removido_em IS NULL AND (c.substituida_a_partir IS NULL OR t.dia < c.substituida_a_partir);

-- Resultado do categorizador: refeito por inteiro a cada 'categorizar'.
CREATE TABLE IF NOT EXISTS classificacao (
    transacao_id  TEXT PRIMARY KEY REFERENCES transacoes(id),
    categoria     TEXT NOT NULL,
    natureza      TEXT NOT NULL,      -- despesa, receita ou neutro
    regra         TEXT NOT NULL,      -- o que decidiu: ajuste manual, nome da regra, categoria da Pluggy
    duvidoso      INTEGER NOT NULL DEFAULT 0
);

CREATE VIEW IF NOT EXISTS lancamentos_classificados AS
SELECT
    l.*,
    k.categoria, k.natureza, k.regra, k.duvidoso,
    c.numero_final, c.subtipo, c.nome AS conta_nome,
    i.instituicao
FROM lancamentos l
JOIN contas c ON c.id = l.conta_id
JOIN itens i ON i.id = c.item_id
LEFT JOIN classificacao k ON k.transacao_id = l.id;
"""


def caminho(perfil: str) -> Path:
    if perfil not in config.PERFIS:
        raise ValueError("perfil inválido")
    base = config.PASTA_DADOS_REAIS / "real" if perfil == "real" else config.RAIZ / "dados" / perfil
    return base / "financas.sqlite3"


def _migrar(con: sqlite3.Connection, versao: int) -> None:
    if versao < 1:
        con.executescript(_ESQUEMA)
    if versao < 3:
        # v2: textos livres dos lançamentos passam a ser gravados mascarados.
        # v3: também nome da conta e do conector, com a máscara ampliada (mais separadores).
        # Reaplica em tudo (mascara.texto é idempotente) e reescreve o arquivo (VACUUM)
        # para não sobrar o texto antigo em páginas livres.
        con.create_function("mascarar_texto", 1, mascara.texto, deterministic=True)
        with con:
            con.execute(
                """UPDATE transacoes SET descricao = mascarar_texto(descricao),
                       descricao_bruta = mascarar_texto(descricao_bruta),
                       contraparte_nome = mascarar_texto(contraparte_nome),
                       comerciante = mascarar_texto(comerciante)"""
            )
            con.execute("UPDATE contas SET nome = mascarar_texto(nome)")
            con.execute("UPDATE itens SET conector = mascarar_texto(conector)")
        con.execute("VACUUM")
    if versao < 4:
        # v4: nome da instituição por item (preenchido na próxima coleta).
        colunas = {linha[1] for linha in con.execute("PRAGMA table_info(itens)")}
        if "instituicao" not in colunas:
            con.execute("ALTER TABLE itens ADD COLUMN instituicao TEXT")
    if versao < 6:
        # v5: tabela de classificação e view para o relatório (o esquema todo é IF NOT EXISTS).
        # v6: conta substituída por reconexão. A coluna entra antes de refazer as views, que a usam.
        colunas = {linha[1] for linha in con.execute("PRAGMA table_info(contas)")}
        if colunas and "substituida_a_partir" not in colunas:  # sem tabela, o esquema abaixo já cria com ela
            con.execute("ALTER TABLE contas ADD COLUMN substituida_a_partir TEXT")
        con.execute("DROP VIEW IF EXISTS lancamentos_classificados")
        con.execute("DROP VIEW IF EXISTS lancamentos")
        con.executescript(_ESQUEMA)
    con.execute(f"PRAGMA user_version = {VERSAO}")
    con.commit()


def conectar_arquivo(arquivo: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(arquivo)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA secure_delete = ON")
    versao = con.execute("PRAGMA user_version").fetchone()[0]
    if versao < VERSAO:
        _migrar(con, versao)
    return con


def _verificar_pasta_teste(arquivo: Path) -> None:
    """O perfil teste não tem as proteções do real (mostra somas, não pede CONFIRMO). Por isso
    nenhuma parte do caminho dele pode ser um link para outro lugar: um banco real ligado em
    dados\\teste não é aberto."""
    pasta = config.RAIZ / "dados" / "teste"
    for parte in (config.RAIZ / "dados", pasta, pasta / "relatorios", arquivo):
        if parte.exists() and parte.resolve() != parte:  # no Windows a comparação ignora maiúsculas
            raise config.LocalInseguro(f"'{parte.name}' do perfil teste aponta para outro lugar (link). "
                                       "O perfil teste só usa a pasta dados\\teste do projeto.")


def conectar(perfil: str) -> sqlite3.Connection:
    arquivo = caminho(perfil)
    if perfil == "teste":
        _verificar_pasta_teste(arquivo)
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    con = conectar_arquivo(arquivo)
    # Só itens do Sandbox no perfil teste: um banco real copiado para dados\teste não é usado.
    if perfil == "teste" and con.execute("SELECT 1 FROM itens WHERE sandbox IS NOT 1 LIMIT 1").fetchone():
        con.close()
        raise config.LocalInseguro("O banco do perfil teste tem itens que não são do Sandbox da Pluggy. "
                                   "O perfil teste só aceita dados do Sandbox: apague dados\\teste e colete de novo.")
    return con
