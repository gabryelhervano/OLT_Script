# Registro de alterações

Cada versão marcada no git corresponde a um ponto de retorno conhecido. Para
voltar a uma delas: `python3 restaurar_versao.py <versão>`.

## v1.4.1 - 2026-10-21

- A validação final passou a esperar a CPE reconectar: a ONT é apagada e
  recriada, e a CPE leva alguns minutos para voltar a autenticar. Sem a
  espera, toda execução terminava como `PARCIAL`. As tentativas e o
  intervalo são configuráveis (`validacao_tentativas`,
  `validacao_intervalo`).

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
