"""Registro das execuções e relatório em CSV.

O registro guarda o estado de cada ONT já reprovisionada, para que uma
nova execução não repita o trabalho. O relatório resume a operação e não
contém comandos nem senhas.
"""

import csv
import json
import os


ARQUIVO_REGISTRO = "registro_operacoes.json"

# Colunas do relatório CSV, na ordem em que o Excel as mostra.
CABECALHOS_RELATORIO = [
    "data", "olt", "fsp", "ont_id", "classe", "motivo", "observacoes",
]


def carregar_registro(caminho=ARQUIVO_REGISTRO):
    """Lê o registro de ONTs já reprovisionadas; sem arquivo, é vazio."""
    if not os.path.exists(caminho):
        return {}
    try:
        with open(caminho, "r", encoding="utf-8") as arquivo:
            return json.load(arquivo)
    except (OSError, ValueError):
        return {}


def salvar_registro(caminho, registro):
    """Grava o registro das ONTs já reprovisionadas."""
    with open(caminho, "w", encoding="utf-8") as arquivo:
        json.dump(registro, arquivo, indent=2, ensure_ascii=False)


def chave_ont(dados):
    """Identificador da ONT no registro e no relatório: F/S/P e ID."""
    return "%s/%d/%d" % (dados["interface"], dados["porta"],
                         dados["ont_id"])


def gravar_relatorio(caminho, linhas):
    """Grava o relatório da execução.

    utf-8-sig: o Excel do Windows abre o arquivo sem os acentos quebrados.
    """
    with open(caminho, "w", encoding="utf-8-sig", newline="") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(CABECALHOS_RELATORIO)
        for linha in linhas:
            escritor.writerow(
                [linha.get(chave, "") for chave in CABECALHOS_RELATORIO])
