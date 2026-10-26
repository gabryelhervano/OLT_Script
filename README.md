# Reprovisionamento de ONTs em OLTs Huawei

Ferramenta em Python para automatizar o reprovisionamento de ONTs em OLTs
Huawei (modelos MA5800, entre outros), utilizando a CLI do equipamento por
SSH. A sequência de comandos reproduzida é a mesma validada na MA5800 com
versão V100R021.

## Estrutura do projeto

| Arquivo | Descrição |
|---|---|
| `reprovisionar_olt.py` | Linha de comando: opções, modos de execução e saída no terminal. |
| `olt/configuracao.py` | Carregamento do INI e das variáveis de ambiente. |
| `olt/planilha.py` | Leitura e validação da planilha de usuários PPPoE. |
| `olt/classificacao.py` | Associação de cada ONT a um registro da planilha. |
| `olt/sessao.py` | Sessão SSH com o equipamento (e a versão simulada). |
| `olt/descoberta.py` | Leitura do estado atual das ONTs. |
| `olt/comandos.py` | Montagem da sequência de reprovisionamento. |
| `olt/orquestrador.py` | Condução da execução, card a card. |
| `olt/relatorio.py` | Registro das execuções e relatório em CSV. |
| `olt/estados.py` | Estados e padrões compartilhados entre os módulos. |
| `olt/util.py`, `olt/erros.py`, `olt/interacao.py` | Auxiliares. |
| `exemplo_simulacao.py` | Executa a simulação com três ONTs ilustrativos (um de cada padrão de CPE). |
| `testes_reprovisionar.py` | Testes automatizados (sem conexão real). |
| `restaurar_versao.py` | Lista e restaura versões anteriores do código. |
| `CHANGELOG.md` | Histórico das versões. |
| `config_exemplo.ini` | Exemplo de configuração, sem credenciais reais. |
| `requirements.txt` | Dependências. |

## Instalação

```bash
pip3 install -r requirements.txt
```

O script usa apenas `openpyxl` e `paramiko` além da biblioteca padrão.

## Execução

> **Windows:** os exemplos usam `python3`/`pip3` (Linux/macOS). No Windows,
> troque por `python`/`pip` — o `python3` do console é um atalho da
> Microsoft Store e devolve *"Python was not found"*.

A execução **sempre começa em DRY-RUN**. Sem `--execute`, nenhuma alteração
é enviada: a OLT real é apenas consultada e o script mostra exatamente os
comandos que seriam executados.

```bash
# DRY-RUN: consulta a OLT, classifica as ONTs e mostra o que seria feito.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx

# Diagnóstico: mostra o que foi DESCOBERTO em cada ONT (e o que não foi),
# sem montar nem enviar comando de alteração.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --diagnostico

# Diagnóstico de uma única ONT.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --diagnostico --ont-id 42

# Transcrição completa: mostra TODOS os comandos (as consultas enviadas e os
# comandos de alteração que seriam enviados apenas com --execute).
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --roteiro --serial HWTC0D3BDDA7

# Modo debug: exibe o terminal da OLT ao vivo — cada comando enviado e a
# resposta crua da CLI, com o rótulo de leitura/alteração. As senhas aparecem
# em texto puro, que é exatamente o valor que vai para a OLT. Vale no DRY-RUN
# e no --execute.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --debug --serial HWTC0D3BDDA7

# Com --limit, o número vale para a classificação e para a execução: a
# contagem para assim que N ONTs entram no fluxo, e as demais não são nem
# consultadas na OLT. Sem --limit, o card é classificado inteiro (e a
# execução continua no padrão conservador de uma ONT por vez).
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --limit 2

# O mesmo --limit recorta o roteiro: a transcrição de duas ONTs, numa sessão só.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --roteiro --limit 2

# Gera também um relatório CSV do que foi simulado.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --limit 1 --report simulacao.csv

# Valida a conexão SSH sem executar nenhuma alteração.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --test-ssh

# Simulação com ONTs fictícios, sem conexão com a OLT.
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --mock
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

### Registro de execuções

Toda ONT que termina a sequência sem erro é gravada em
`registro_operacoes.json` com a data da execução. Numa execução seguinte ela
aparece como `JA_CONCLUIDA`, com a data, e não recebe comandos de novo: é o
que evita que uma execução interrompida repita o trabalho já feito.

Para repetir de propósito (bancada, ajuste de perfil, nova planilha), use
`--reexecutar`:

```bash
python3 reprovisionar_olt.py --file pppoes-cyber.xlsx --execute --serial HWTC0D3BDDA7 --reexecutar
```

Também dá para apagar a entrada da ONT do `registro_operacoes.json`.

## Configuração

A conexão e os perfis usados vêm de variáveis de ambiente ou de um arquivo INI
(`--config`). Veja `config_exemplo.ini` para a lista completa; nenhum valor
real é necessário para o modo DRY-RUN. São configuráveis o line-profile
(`ont-lineprofile-id`), o perfil TR-069 (`ont tr069-server-config`), o perfil
WAN (`ont wan-config`) e a prioridade de gerência (`ont ipconfig`); gemport e
o srv-profile são descobertos no estado atual da ONT a cada execução. Sem
`prioridade` no arquivo o script para antes de abrir a sessão e diz o que
falta — a OLT penaliza login, então o erro de configuração não custa uma
conexão. A VLAN vem de uma leitura única da
OLT (`display vlan all`): é a VLAN com mais service-ports — a que tem mais
CPEs conectadas — e é aplicada em todos os comandos (`ont ipconfig`,
`native-vlan`, `service-port` e `user-vlan`), para que toda CPE receba a
mesma VLAN. Quando essa leitura não é possível, o aviso sai no log e o fluxo
usa a VLAN de cada ONT. Se outro valor não puder ser identificado, a ONT fica
`INCONCLUSIVO` e nenhuma alteração é enviada.

### Autenticação por senha

```ini
[olt]
host = 192.168.0.10
porta = 22
usuario = operador
senha = a-senha-do-login
enable = enable
; senha_enable = ...   ; só se for diferente da senha de login
```

- O arquivo de configuração é ignorado pelo Git (`config.ini` no
  `.gitignore`). Proteja-o: `chmod 600 config.ini`.
- A senha **nunca** é enviada na linha de comando (evita aparecer na lista de
  processos) e nunca é gravada em log, relatório ou saída.
- A MA5800 exige `enable` para aceitar comandos de configuração (o prompt
  passa de `>` para `#`). O script envia `enable` logo após conectar e, se a
  OLT pedir senha para ele, reaproveita a senha de login (ou `senha_enable`).
- A chave do servidor é validada: a OLT precisa estar em `~/.ssh/known_hosts`.
  Se não estiver, acrescente com
  `ssh-keyscan -H <ip-da-olt> >> ~/.ssh/known_hosts`.

## Versionamento e retorno a versões anteriores

O código é versionado no git, com marcas (`v1.0.0`, `v1.1.0`, `v1.2.0`, ...)
em pontos já validados. O `CHANGELOG.md` resume o que mudou em cada uma.

```bash
# lista as versões disponíveis
python3 restaurar_versao.py

# restaura uma versão (o estado atual fica guardado em uma marca antes)
python3 restaurar_versao.py v1.1.0
```

Ao restaurar, o script roda a suíte de testes na versão restaurada e avisa
se ela passou. Para voltar à versão mais recente:

```bash
python3 restaurar_versao.py --atualizar
```

O histórico completo continua no GitHub, então qualquer commit também pode
ser retomado com `git checkout <commit>`.

## Relatório

O relatório CSV registra, por ONT: data, OLT, F/S/P, ONT ID, classe, motivo e
observações — não há comandos nem senhas nele. Na saída do terminal (resumo,
`--roteiro` e `--debug`) as senhas aparecem em texto puro, que é o valor
enviado à OLT: cuidado ao copiar essa saída para terceiros.
