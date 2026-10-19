"""Reprovisionamento de ONTs em OLTs Huawei via SSH.

O pacote separa as responsabilidades do fluxo:

- `configuracao`: carrega o INI e as variáveis de ambiente;
- `planilha`: lê e valida os registros de usuário PPPoE;
- `classificacao`: associa cada ONT a um registro da planilha;
- `sessao`: conversa com a CLI do equipamento;
- `descoberta`: lê o estado atual da ONT;
- `comandos`: monta a sequência de reprovisionamento;
- `orquestrador`: conduz a execução, card a card;
- `relatorio`: registra o que foi feito e gera o relatório em CSV.
"""

from .classificacao import classificar
from .comandos import (comando_configurar_internet, comando_configurar_pppoe,
                       comando_configurar_tr069, comando_configurar_wan,
                       comando_criar_ont, comando_criar_service_port,
                       comando_native_vlan, exibir_comandos, montar_passos)
from .configuracao import carregar_configuracao, portas_configuradas
from .descoberta import capturar_estado, listar_onts, validar_estado_final
from .erros import ErroOperacao
from .estados import (BLOQUEADA_AMBIGUIDADE, BLOQUEADA_DIVERGENCIA,
                      ESTADO_INCONCLUSIVO, ESTADO_PARCIAL, ESTADO_SUCESSO,
                      IGNORADA_SEM_IDENTIFICADORES, JA_CONFIGURADA,
                      PADRAO_1_COMPLETA, PADRAO_2_PPPOE, PADRAO_2_SERIAL)
from .orquestrador import ControleExecucao, Orquestrador
from .planilha import indexar, ler_planilha, resumo_planilha
from .relatorio import carregar_registro, chave_ont, gravar_relatorio, \
    salvar_registro
from .sessao import SessaoSimulada, SessaoSSH, Simulador, Transporte, \
    conectar_ssh

__version__ = "1.1.0"

__all__ = [
    "__version__",
    "BLOQUEADA_AMBIGUIDADE",
    "BLOQUEADA_DIVERGENCIA",
    "ControleExecucao",
    "ESTADO_INCONCLUSIVO",
    "ESTADO_PARCIAL",
    "ESTADO_SUCESSO",
    "ErroOperacao",
    "IGNORADA_SEM_IDENTIFICADORES",
    "JA_CONFIGURADA",
    "Orquestrador",
    "PADRAO_1_COMPLETA",
    "PADRAO_2_PPPOE",
    "PADRAO_2_SERIAL",
    "SessaoSSH",
    "SessaoSimulada",
    "Simulador",
    "Transporte",
    "capturar_estado",
    "carregar_configuracao",
    "carregar_registro",
    "chave_ont",
    "classificar",
    "comando_configurar_internet",
    "comando_configurar_pppoe",
    "comando_configurar_tr069",
    "comando_configurar_wan",
    "comando_criar_ont",
    "comando_criar_service_port",
    "comando_native_vlan",
    "conectar_ssh",
    "exibir_comandos",
    "gravar_relatorio",
    "indexar",
    "ler_planilha",
    "listar_onts",
    "montar_passos",
    "portas_configuradas",
    "resumo_planilha",
    "salvar_registro",
    "validar_estado_final",
]
