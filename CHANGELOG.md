# Registro de alterações

Cada versão marcada no git corresponde a um ponto de retorno conhecido. Para
voltar a uma delas: `python3 restaurar_versao.py <versão>`.

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
