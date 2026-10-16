# Reprovisionamento de ONTs em OLTs Huawei

Script único em Python para automatizar o reprovisionamento de ONTs em OLTs
Huawei (modelos MA5800, entre outros), utilizando a CLI do equipamento por
SSH. A sequência de comandos reproduz é a mesma validada na MA5800 com
versão V100R021; nenhum comando novo foi inventado.

## Estrutura do projeto

| Arquivo | Descrição |
|---|---|
| `reprovisionar_olt.py` | Script completo: leitura da planilha, consulta SSH, classificação, fluxo e relatório. |
| `exemplo_simulacao.py` | Executa a simulação com três ONTs ilustrativos (um de cada padrão de CPE). |
| `testes_reprovisionar.py` | Testes automatizados (sem conexão real). |
| `config_exemplo.ini` | Exemplo de configuração, sem credenciais reais. |
| `requirements.txt` | Dependências. |

## Instalação

```bash
pip3 install -r requirements.txt
```

O script usa apenas `openpyxl` e `paramiko` além da biblioteca padrão.

## Execução

A execução **sempre começa em DRY-RUN**. Sem `--execute`, nenhuma alteração
é enviada; apenas consultas de leitura são executadas, com a sessão SSH aberta
quando a configuração permite.

```bash
# DRY-RUN: lê a planilha, classifica as ONTs e mostra o que seria feito.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx

# Limita a execução a uma ONT (padrão conservador).
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --limit 1

# Gera também um relatório CSV do que foi simulado.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --limit 1 --report simulacao.csv

# Valida a conexão SSH sem executar nenhuma alteração.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --test-ssh

# Simulação com ONTs fictícios, para apresentar os três padrões de CPE.
python3 exemplo_simulacao.py
```

Para testar uma única CPE dentro da OLT, utilize os selecionadores opcionais:

```bash
# Processa somente a ONT com ID 42 (a listagem é filtrada antes de qualquer alteração).
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --ont-id 42

# Ou somente a ONT cujo serial corresponde ao valor informado.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --serial HWTC0D3BDDA7
```

Sem `--ont-id` ou `--serial`, todas as ONTs listadas na OLT são avaliadas.

Para executar alterações reais são exigidos todos os seguintes:

1. `--execute`;
2. confirmação interativa por card (uma vez por card que terá ONTs para
   reprovisionar);
3. ausência de divergências (a classificação bloqueia divergências);
4. serviço disponível e sessão SSH ativa.

```bash
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --execute --limit 1
```

## Configuração

A conexão e o line-profile usam variáveis de ambiente ou de um arquivo INI
(`--config`). Veja `config_exemplo.ini` para a lista completa; nenhum valor
real é necessário para o modo DRY-RUN. Somente o line-profile é configurável:
todos os demais valores usados na reprovisão (VLAN, prioridade, gemport,
user-vlan e perfis WAN/TR-069) são descobertos no estado atual da ONT a cada
execução. Se algum deles não puder ser identificado, o ONT fica
`INCONCLUSIVO` e nenhuma alteração é enviada.

## Relatório

O relatório CSV registra, por ONT: identificador, padrão de CPE, método de
associação, etapa, consultas, validações, motivo e comandos executados. Senhas
nunca aparecem; comandos com senha são gravados mascarados.
