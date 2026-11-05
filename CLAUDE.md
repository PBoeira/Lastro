# Regras para agentes de IA neste projeto

O Claude Code lê este arquivo sozinho quando abre a pasta. Se você usa outro agente de IA, peça
para ele ler este arquivo antes de começar. As regras valem para qualquer sessão aberta aqui.

> Se existirem, leia antes de continuar o `CLAUDE.local.md` (contexto pessoal do dono) e o
> `PROGRESSO.local.md` (andamento e decisões). Atualize o `PROGRESSO.local.md` ao fim de cada etapa.

## API da Pluggy: somente leitura

- O código só faz `POST /auth` e GETs de leitura da API Pluggy. Nada de pagamentos, Pix, criação, alteração ou exclusão de itens.
- O código nunca chama endpoints de identidade, investimentos ou empréstimos (`/identity`, `/investments`, `/loans`), mesmo que a autorização permita. Um teste trava isso.
- Sem MCP da Pluggy neste projeto: documentação só pelo site docs.pluggy.ai.

## Dados reais: o agente só vê totais e dados mascarados

- Nunca ler, imprimir ou consultar lançamentos individuais reais (descrição, valor, contraparte), nem abrir diretamente o banco ou os relatórios do perfil `real`. Eles ficam em `%USERPROFILE%\financas-dados\`, fora do projeto, de propósito; os caminhos `dados/real/` e `relatorios/` dentro do projeto também ficam bloqueados. Os detalhes o dono confere no painel local. Para desenvolver e depurar, usar o perfil `teste` (Sandbox) ou dados sintéticos.
- Análise de dados reais só por comandos do projeto que imprimem agregados (contagens e totais). Nunca consultar o SQLite real por script avulso.
- Agregado de verdade: grupo com menos de 3 lançamentos nunca mostra soma, e nenhum total pode permitir deduzir um grupo pequeno por subtração. Diagnóstico detalhado (`--detalhar`) só com contagens; `--so-contagens` não mostra soma nenhuma.
- No perfil real, o `categorizar` mostra só contagens por padrão, porque comparar as somas de duas execuções revelaria o valor de um lançamento. O agente nunca usa `--com-somas` nem `--saldos` no real; essas opções são para o dono, no próprio terminal.
- No perfil real, regras e ajustes novos só valem depois que o dono confirma, digitando CONFIRMO no próprio terminal. O agente nunca tenta confirmar por ele nem roda comandos num terminal de verdade em nome dele.
- Antes de rodar no perfil real qualquer comando novo ou alterado que imprima dados, mostrar ao dono o trecho do código que gera a saída e rodar primeiro no perfil teste.
- Números de conta e cartão sempre mascarados em tela, logs e relatórios.

## Segredos

- Nunca pedir o Client Secret no chat. Ele fica no Gerenciador de Credenciais do Windows (`financas-pluggy-teste`, `financas-pluggy-real`) e é cadastrado pelo dono com `.\financas.cmd configurar`.

## Permissões e proteções

- Trabalhar no modo padrão (pedir aprovação), não no "Aceitar edições": nesse modo, README, progresso e regras mudam sem pedido, e nem todo jeito de gravar arquivo pelo PowerShell tem regra de pedido.
- Pedir confirmação antes de instalar algo, mudar configuração ou mexer fora desta pasta.
- Nenhuma mudança que reduza proteções (regras de bloqueio do `.claude/settings.json` e do `.claude/settings.local.json`, máscaras, filtros de saída, testes de segurança), nem mesmo mover uma regra de lugar, sem antes mostrar ao dono o antes e o depois, a justificativa e como será verificado, e receber a aprovação dele.
- Não publicar artifacts nem usar conectores da conta do assistente (Drive, Notion, Docs etc.) com dados deste projeto. Cada conta tem os próprios identificadores de conectores, por isso o bloqueio deles fica no `.claude/settings.local.json` de cada pessoa (veja o README).
- Revisores (subagentes) não fazem testes que dependam de o dono aprovar ou negar um pedido de permissão. Um teste assim é descrito antes ao dono e só roda se ele combinar.

## Genérico e pessoal

- Tudo que é pessoal fica em arquivos `*.local.*`, fora do repositório: `regras.local.toml`, `ajustes.local.toml`, `config.local.json`, `CLAUDE.local.md`, `PROGRESSO.local.md`, `.claude/settings.local.json`.
- Nunca colocar dado pessoal (nomes, estabelecimentos frequentados, empregador, cidade, CNPJs ou CPFs de alguém, padrões de pagamento) em `regras.toml`, `README.md`, `CLAUDE.md`, código ou testes.
- Testes só com dados sintéticos, sem nada tirado de dados reais.
- Nada é publicado sem a revisão de publicação (revisor independente procurando dados pessoais e segredos em todo arquivo que iria para o repositório) e sem decisão explícita do dono.
