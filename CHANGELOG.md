# Registro de alterações

Cada versão marcada no git corresponde a um ponto de retorno conhecido. Para
voltar a uma delas: `python restaurar_versao.py <versão>` (no Linux/macOS,
`python3`).

## v1.10.0 - 2026-10-23

- A senha deixa de ser mascarada na saída: o resumo de comandos, o `--roteiro`
  e o modo `--debug` mostram o comando exatamente como sai para a OLT, com o
  valor real — é o que permite conferir o que está sendo configurado. O eco
  que a CLI devolve também aparece cru.
- O relatório CSV não muda: ele continua sem gravar comandos nem senhas.
- Saíram do código `exibir_comandos` e `mascarar_valor`, que não tinham mais
  o que mascarar.
- Atenção: qualquer saída copiada (terminal, transcrição, chat) passa a
  conter as senhas PPPoE em texto puro.

## v1.9.0 - 2026-10-23

- A prioridade do `ont ipconfig` passa a vir do `config.ini`
  (`prioridade = ...`, variável `OLT_PRIORIDADE`), e não mais do estado lido
  na ONT. O valor descoberto não era número em quase nenhuma CPE — o campo
  devolvido por `display ont ipconfig` varia entre elas — e travava a
  operação inteira com `prioridade inválido`.
- Sem `prioridade` na configuração, o script responde com a mensagem do que
  falta **antes de conectar na OLT**: a OLT penaliza login, então um erro de
  configuração não deve custar uma conexão. `--diagnostico` e `--test-ssh`
  dispensam o valor, porque não reconfiguram a ONT.
- `--diagnostico` mostra a prioridade configurada logo abaixo da VLAN da OLT,
  e o resumo do roteiro passa a listar `prioridade (configurável)` junto com
  os perfis.

## v1.8.0 - 2026-10-23

- A VLAN passa a vir de uma leitura única da OLT (`display vlan all`): é a
  VLAN com mais service-ports, ou seja, a que tem mais CPEs conectadas, e é
  aplicada em todos os comandos — `ont ipconfig`, `native-vlan`,
  `service-port` e `user-vlan`. Antes cada ONT usava a VLAN descoberta no
  próprio estado, o que divergia entre CPEs e gerava erros como
  `VLAN inválido` e `VLAN atual não identificada`.
- Quando `display vlan all` não pode ser lido, o aviso sai no log e o fluxo
  continua com a VLAN de cada ONT, como antes; nenhum valor é assumido.
- `--diagnostico` mostra a VLAN da OLT antes do estado de cada ONT.

## v1.7.1 - 2026-10-23

- O `service-port` era recriado com a **manage VLAN da ONT** no lugar da VLAN
  do service-port que já atendia a ONT. Quando os dois números diferem, o
  script trocava o mapeamento de serviço da OLT sem ninguém ter pedido. Agora
  `service-port vlan` usa a VLAN descoberta na listagem
  `display service-port all` (o `user-vlan` já vinha de lá); a manage VLAN
  continua sendo usada no `ont ipconfig` e no `native-vlan`, que configuram a
  própria ONT.
- `--diagnostico` passa a mostrar o campo `service_vlan` junto com
  `user_vlan` e `service_port`.

## v1.7.0 - 2026-10-22

- Uma ONT registrada como concluída deixava de aparecer na saída: ela era
  pulada em silêncio e o script terminava sem nenhuma linha de resultado,
  dando a impressão de nada ter sido feito. Agora aparece como
  `JA_CONCLUIDA`, com a data gravada no `registro_operacoes.json`.
- Novo argumento `--reexecutar` (ou `reexecutar` / `OLT_REEXECUTAR` na
  configuração): reprovisiona também as ONTs que o registro marca como
  concluídas, sem precisar apagar o registro.

## v1.6.0 - 2026-10-22

- Novo modo `--debug`: exibe o terminal da OLT ao vivo, com o comando enviado
  (rotulado como `leitura` ou `ALTERAÇÃO`) e a resposta crua da CLI, tal como
  a OLT devolveu. As senhas continuam mascaradas no comando e na resposta,
  porque a CLI ecoa o que recebeu; os valores literais das senhas do
  `config.ini` também são substituídos. Vale no DRY-RUN, no `--execute` e no
  `--test-ssh`, na OLT real e no `--mock`.
- Corrigido o `--diagnostico`: a rotina que mostra os perfis de TR-069 e WAN
  era chamada com dois argumentos e definida com um, então o modo travava com
  `TypeError` assim que encontrava uma ONT.
- Corrigido o `--mock --execute`: o `Simulador` não tinha o método `alterar`
  que o orquestrador chama e a simulação de execução falhava com
  `AttributeError`. Ele passou a herdar da sessão simulada usada nos testes.
- A listagem de perfis tenta `display ont tr069-server-profile all` antes da
  forma simples, no mesmo formato do `wan-profile all` que a MA5800 exige.

## v1.5.0 - 2026-10-21

- A validação final foi removida. Agora a ONT é `SUCESSO` quando a sequência
  de comandos termina sem erro e `INCONCLUSIVO` quando a OLT recusa algum
  comando, com o motivo do erro na saída e no relatório. Um comando recusado
  já interrompia a execução; o que mudou foi o critério de sucesso.
- `display ont wan-info` e `display ont ipconfig` deixaram de ser relidos
  depois dos comandos e passaram a ser lidos uma única vez, na triagem.
- Saíram do código a classe `PARCIAL`, a etapa `validacao` e a coluna `estado`
  do relatório CSV, que só existiam para essa validação.

## v1.4.2 - 2026-10-21

- Revertida a espera de reconexão antes da validação (v1.4.1). O atraso da CPE
  vinha do sinal óptico, que está fora do escopo do script.

## v1.4.1 - 2026-10-21

- A validação final passou a esperar a CPE reconectar antes de ler o estado.
  Alteração revertida na v1.4.2.

## v1.4.0 - 2026-10-21

- Os comandos de alteração agora são verificados: quando a OLT recusa um
  comando, a execução para ali e a ONT fica `INCONCLUSIVO` com o motivo do
  erro. Antes, a recusa passava despercebida.
- O MAC deixou de fazer parte do diagnóstico e do resumo do estado: o
  fluxo usa apenas o serial (e o usuário) para associar a CPE.

## v1.3.4 - 2026-10-21

- O diagnóstico passa a listar os perfis de TR-069 e WAN da OLT, com o nome de
  cada um, junto do perfil que será aplicado.

## v1.3.3 - 2026-10-21

- O diagnóstico passa a mostrar os perfis de TR-069 e WAN disponíveis na OLT,
  para conferir qual identificador é de cada um (eles mudam de OLT para OLT).

## v1.3.2 - 2026-10-21

- Corrigido o erro na exibição dos resultados, que interrompia a saída no
  fim da execução (os comandos já tinham sido enviados).

## v1.3.1 - 2026-10-21

- O registro persistente passou a gravar o resultado de cada ONT (antes o
  arquivo ficava vazio e a execução repetia o que já estava concluído).
- O relatório em CSV ganhou as colunas `fsp`, `ont_id` e `estado`, com o
  estado final lido na OLT, e a saída do terminal mostra esse estado quando
  o resultado não é `SUCESSO`.
- O MAC da planilha passou a ser aceito no formato da OLT (XXXX-XXXX-XXXX).

## v1.3.0 - 2026-10-21

- Filtro de CPEs suportadas: ONTs cujo serial não começa com um dos
  prefixos de `prefixos_serial` (padrão `HW,4857,57`) são descartadas na
  triagem, com a classe `DESCARTA_POR_SERIAL`, e o fluxo segue para a
  próxima ONT. O filtro roda antes de qualquer consulta de leitura.

## v1.2.0 - 2026-10-20

- Versionamento: marcas (`v1.0.0`, `v1.1.0`, `v1.2.0`) e o script
  `restaurar_versao.py`, que restaura uma versão, guarda o estado anterior
  e roda a suíte de testes na versão restaurada.
- `CHANGELOG.md` com o histórico.

## v1.1.0 - 2026-10-20

- Código reorganizado no pacote `olt/`; `reprovisionar_olt.py` ficou só com a
  linha de comando.
- Perfis de TR-069 e WAN passaram a ser configurados (`tr069profile` e
  `wanprofile` no `config.ini`); as linhas `ont tr069-server-config`,
  `ont internet-config` e `ont wan-config` são sempre executadas.
- ONTs que já estão com o line-profile configurado são puladas
  (`JA_CONFIGURADA`), evitando retrabalho e indisponibilidade da CPE.
- ONTs sem correspondência com a planilha não são reprovisionadas.
- Descoberta do line-profile em uso pelo nome informado pela OLT.
- Menos consultas de leitura: listagem de service-ports lida uma vez por
  execução, `display ont profile` só para as ONTs que serão reprovisionadas e
  triagem antecipada das ONTs fora da campanha.
- Ajustes na sessão com a CLI da MA5800: `enable`, `scroll`, visão de
  configuração, envio caractere a caractere e Enter adicional.

## v1.0.0 - 2026-10-20

- Script único, validado contra a MA5800: descoberta do estado da ONT
  (VLAN, prioridade, gemport, user-vlan, perfis e service-port), sequência
  de reprovisionamento e confirmação por card antes de qualquer alteração.
