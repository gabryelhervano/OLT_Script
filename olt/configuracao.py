"""Carregamento da configuração do operador.

Os valores vêm de variáveis de ambiente ou de um arquivo INI. O arquivo
padrão é `config.ini`, na pasta de trabalho; ele não é versionado, porque
pode conter a senha de acesso à OLT.
"""

import configparser
import os

ARQUIVO_CONFIG = "config.ini"

# Chave -> (variável de ambiente, valor padrão).
_PADROES_CONFIG = {
    "host": ("OLT_HOST", "127.0.0.1"),
    "porta": ("OLT_PORT", "22"),
    "usuario": ("OLT_USUARIO", ""),
    "senha": ("OLT_SENHA", ""),
    "chave": ("OLT_CHAVE", ""),
    "senha_enable": ("OLT_SENHA_ENABLE", ""),
    "enable": ("OLT_ENABLE", "enable"),
    "screen_length": ("OLT_SCREEN_LENGTH", "screen-length 0 temporary"),
    "terminador": ("OLT_TERMINADOR", "cr"),
    "atraso_tecla": ("OLT_ATRASO_TECLA", "0.01"),
    "largura": ("OLT_LARGURA", "512"),
    "altura": ("OLT_ALTURA", "1000"),
    "interface": ("OLT_INTERFACE", "0/1"),
    "lineprofile": ("OLT_LINEPROFILE", "11"),
    "reprovisionar_ja_configuradas": ("OLT_REPROVISIONAR_JA_CONFIGURADAS", ""),
    "seguir_sequencia_validada": ("OLT_SEQUENCIA_VALIDADA", "sim"),
    "portas": ("OLT_PORTAS", "0-15"),
}


def carregar_configuracao(caminho=None):
    """Monta a configuração a partir do INI e das variáveis de ambiente.

    Sem caminho informado, usa `config.ini` da pasta atual, se existir. As
    variáveis de ambiente têm precedência sobre o arquivo. A leitura tolera
    o BOM que o Bloco de Notas do Windows grava.
    """
    if caminho is None and os.path.exists(ARQUIVO_CONFIG):
        caminho = ARQUIVO_CONFIG
    parser = configparser.ConfigParser()
    if caminho and os.path.exists(caminho):
        parser.read(caminho, encoding="utf-8-sig")
    configuracao = {}
    for chave, (variavel, padrao) in _PADROES_CONFIG.items():
        configuracao[chave] = _variavel(variavel, parser, chave) or padrao
    configuracao["arquivo"] = caminho if caminho and os.path.exists(caminho) \
        else ""
    return configuracao


def _variavel(nome, parser, chave):
    """Lê um valor do ambiente e, se não houver, da seção [olt] do INI."""
    valor = os.environ.get(nome)
    if valor and valor.strip():
        return valor.strip()
    if parser.has_section("olt") and parser.has_option("olt", chave):
        return parser.get("olt", chave).strip()
    return ""


def portas_configuradas(configuracao):
    """Portas GPON a consultar.

    Aceita uma lista ("0,1,4") ou um intervalo ("0-15"). Sem configuração,
    todas as portas são consultadas.
    """
    configuradas = configuracao.get("portas")
    if not configuradas:
        return list(range(16))
    portas = []
    for item in str(configuradas).split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            inicio, fim = item.split("-", 1)
            portas.extend(range(int(inicio), int(fim) + 1))
        else:
            portas.append(int(item))
    return portas
