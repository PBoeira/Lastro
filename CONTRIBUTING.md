# Como contribuir

Obrigado pelo interesse no Lastro. Sugestões, relatos de problemas e correções são bem-vindos.
Antes de mandar uma mudança, leia as regras abaixo: elas existem porque o programa lida com dados
bancários de verdade.

## Antes de começar

- Para mudanças grandes, abra uma issue primeiro para combinarmos o caminho.
- O projeto roda no Windows, com Python 3.14, e usa só a biblioteca padrão. Não acrescente
  dependências externas.
- Se encontrar uma falha de segurança, não abra uma issue pública: use **Report a vulnerability**,
  na aba **Security** do repositório.

## Regras que valem para toda mudança

- **Somente leitura.** O código só faz `POST /auth` e os GETs listados em `financas/pluggy.py`.
  Não acrescente chamadas que criem, alterem ou apaguem nada, nem os endpoints de identidade,
  investimentos ou empréstimos: um teste automático trava isso.
- **Nada de dados reais.** Testes, exemplos e issues usam só dados inventados. Nunca cole
  extratos, descrições de lançamentos, nomes, CPFs, números de conta ou itemIds de verdade, nem
  Client ID, Client Secret, apiKey ou trechos do seu banco de dados, do seu painel ou do
  `mensal.log`.
- **Nada pessoal nos arquivos públicos.** Regras para as lojas que você frequenta, o seu
  empregador ou a sua cidade vão no seu `regras.local.toml`, que fica fora do repositório. O
  `regras.toml` é só para regras genéricas.
- **Segredo fora do código.** O Client Secret fica no Gerenciador de Credenciais do Windows. Não
  troque isso por variáveis de ambiente nem por arquivo `.env`: o `.env` fica em texto puro no
  disco e pode acabar no repositório por engano, e as variáveis de ambiente passam para todo
  programa aberto a partir daquele terminal.
- **Proteções não diminuem sem motivo.** Mudanças que afrouxem máscaras, bloqueios do
  `.claude/settings.json`, filtros de saída ou testes de segurança precisam de uma justificativa
  clara no pull request.
- **Use os atalhos.** Rode o programa por `financas.cmd` e os testes por `testes.cmd`, nunca por
  `python -m`. Os atalhos isolam o Python (`-I -S`), para que um arquivo com nome de módulo
  deixado na pasta não seja executado no lugar do original.

## Como mandar uma mudança

1. Faça a mudança num fork, num branch próprio.
2. Rode `.\testes.cmd` e confira que tudo passa. Se a mudança corrige um erro, acrescente um teste
   que falharia sem ela.
3. Para conferir o painel, use o perfil teste (banco de testes da Pluggy) ou dados sintéticos.
4. Abra o pull request explicando o que mudou e por quê.

## Se você usa um assistente de IA

Siga o [`CLAUDE.md`](CLAUDE.md). Em resumo: o assistente não deve ver lançamentos reais, só totais
e dados mascarados (os bloqueios do `.claude/settings.json` reforçam essa regra), e trabalha no
modo que pede aprovação.
