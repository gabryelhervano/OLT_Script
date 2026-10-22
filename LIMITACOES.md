# Limitações e riscos operacionais

Este documento resume o que o script **não** faz, e os riscos que devem ser
considerados antes de qualquer execução real. Não substitui a análise do
operador.

## O que depende da CLI real da OLT

- Os comandos usados são exatamente os observados na MA5800 V100R021
  (`display ont info summary <fsp>`, `display ont wan-info`,
  `display ont ipconfig`, `display service-port all`, `ont delete/add`,
  `ont ipconfig`, `ont port native-vlan`, `ont tr069-server-config`,
  `ont internet-config`, `ont wan-config`, `service-port`,
  `undo service-port`, `display`).
- Outras versões ou modelos podem não suportar algum comando usado, ou
  interpretar a resposta de forma diferente. A camada SSH registra as
  respostas; comandos que falham são tratados como falha, nunca ignorados.
- A listagem de ONTs (`display ont`) é interpretada por heurística: linhas que
  apresentam `ONT ID` iniciam um equipamento. Se a CLI não expuser esses campos,
  a OLT não é localizada e nenhuma alteração é executada nela.
- Os campos ausentes na resposta (por exemplo, `Serial` no `display ont`) são
  apenas ignorados; a identidade de uma ONT é confirmada somente pelos dados
  realmente retornados.

## Prevenção de alteração errada

- Somente ONTs identificados com segurança são submetidos à execução.
- `undo service-port` e `ont delete` só ocorrem depois de o service-port
  original ser identificado; se ele não puder ser identificado, a ONT é
  marcada como `INCONCLUSIVO` e nenhuma alteração é enviada.
- Nenhum identificador calculado, próximo número disponível ou sugestão de
  `gemport` é enviado: a OLT atribui o service-port criado.
- Credenciais vindas da planilha nunca são deduzidas por tentativa; ausência de
  senha, MAC ou serial inválido bloqueia o registro antes de qualquer conexão.
- Ausência de um identificador não é tratada como divergência; divergência
  (serial e usuário apontando para registros diferentes) bloqueia a operação.
- Uma execução real exige confirmação por card: o operador confirma o card inteiro
  antes de qualquer comando ser enviado (o DRY-RUN nunca pede confirmação).
- `undo service-port` e `service-port` são comandos da configuração raiz: o
  `undo` é enviado antes de entrar em `interface gpon` e o `service-port`
  depois de sair dela (`quit`), respeitando o contexto da CLI.

## Sobrecarga e interrupção

- Apenas uma ONT é processada por vez; nenhuma alteração é enviada em
  paralelo.
- Limites configuráveis: máximo de ONTs (`--limit`), intervalo mínimo entre
  operações (`--delay`), pausas periódicas (`--pausa`) e tempo máximo
  (`--tempo-maximo`).
- Três falhas consecutivas suspenden a execução automaticamente.
- A interrupção manual (Ctrl+C) é segura: nenhum comando é reenviado após a
  interrupção, e o estado é recapturado no próximo início.
- A perda de conexão SSH não é interpretada como falha do comando anterior;
  apenas comandos confirmados enquanto a sessão estava ativa são considerados
  executados.
- Consultas de leitura são limitadas em número por ONT e espaçadas; comandos de
  configuração são enviados um a um, aguardando a resposta da CLI.

## Estado, registro e repetição

- O estado original de cada ONT é capturado antes da primeira alteração e os
  comandos executados são registrados em `registro_operacoes.json`.
- Uma ONT concluída em execução anterior não é reprovisionada novamente.
- Falhas intercaladas registram a etapa em que pararam; nenhum rollback é
  executado automaticamente, pois não há procedimento validado para isso.
  O operador deve restaurar manualmente quando necessário.

## Desempenho

- A OLT é a origem da lista de ONTs: a planilha fornece as credenciais. As
  consultas caras (`display service-port all`, `display current-configuration`)
  são lidas uma vez por execução e reutilizadas por todas as ONTs.
- CPEs de equipamentos que a OLT não suporta (prefixo de serial fora do
  configurado em `prefixos_serial`) são descartadas na triagem, com a classe
  `DESCARTA_POR_SERIAL`, e o fluxo segue para a próxima ONT. O filtro é
  aplicado antes de qualquer consulta de leitura.
- Antes de consultar o estado de uma ONT, a triagem descarta as que não têm
  correspondência na planilha, comparando o serial e a descrição (que é o
  usuário PPPoE). Isso evita as consultas de leitura das ONTs fora da
  campanha. A opção `filtrar_por_planilha = nao` consulta todas.
- Cada ONT reprovisionada tem seu estado lido três vezes (descoberta,
  reconferência antes da alteração e validação final), o que protege contra
  mudanças externas entre as etapas.

## Segurança de credenciais

- Senhas PPPoE e senhas SSH nunca são gravadas em logs, relatórios ou mensagens
  de erro. No relatório, comandos com senha aparecem mascarados.
- Valores contendo `"` ou `\` são bloqueados na montagem do comando, pois a CLI
  Huawei não garante transmissão segura deles.
- A conexão SSH valida a chave do servidor (`RejectPolicy`); chaves novas não
  são aceitas automaticamente.

## Limitações conhecidas

- `display service-port` é consultado apenas em modo de leitura, para
  descobrir o identificador do service-port atual (usado no `undo
  service-port`), o gemport e o user-vlan. Se a CLI não expuser esses campos,
  a ONT fica `INCONCLUSIVO` e nenhuma alteração é enviada.
- Os perfis WAN/TR-069 e demais parâmetros são lidos do estado atual da
  ONT (`display ont wan-info` / `display ont ipconfig`). Se qualquer valor não
  puder ser identificado, a ONT fica `INCONCLUSIVO` e nenhuma alteração é
  enviada — nada é assumido.
- Somente os perfis são configuráveis: line-profile (`ont-lineprofile-id`),
  TR-069 (`ont tr069-server-config`) e WAN (`ont wan-config`). Todos os demais
  valores reproduzidos (VLAN, prioridade, gemport, user-vlan e o srv-profile)
  são exatamente os que já estavam na OLT, descobertos a cada execução; não há
  parâmetro para eles no arquivo de configuração.
- A sequência validada reaplica em toda ONT reprovisionada o `native-vlan`,
  o `TR-069`, o `internet-config` e o `wan-config`: como a ONT é apagada e
  recriada, sem esses comandos ela ficaria sem a configuração de WAN. O
  `ip-index` do `internet-config` e do `wan-config` é fixo em 0; os
  identificadores dos perfis TR-069 e WAN vêm da configuração do operador.
- ONTs que já estão com o line-profile configurado são puladas e classificadas
  como `JA_CONFIGURADA`: reprovisioná-las de novo só geraria retrabalho e um
  breve período sem serviço na CPE. A opção `reprovisionar_ja_configuradas`
  força o reprovisionamento.
- O critério de sucesso é a conclusão da sequência: se a OLT recusa algum
  comando, a execução para na hora e a ONT fica `INCONCLUSIVO` com o motivo do
  erro. O script não relê `wan-info` nem `ipconfig` depois dos comandos — essas
  duas leituras são feitas uma única vez, na triagem, e o estado da CPE
  (conexão PPPoE, sinal óptico) é assunto da rede, não do reprovisionamento.
- Execuções reais exigem um operador presente para as confirmações. A
  confirmação pedida é por card: o operador confirma o card inteiro antes de
  qualquer comando ser enviado para os ONTs desse card. O DRY-RUN nunca pede
  confirmação.
- Os testes automatizados usam um transporte SSH simulado; a conferência em OLT
  real é feita pelo operador, uma ONT por vez.
