"""Registro das execuções e relatório em CSV.

O registro guarda o estado de cada ONT já reprovisionada, para que uma
nova execução não repita o trabalho. O relatório resume a operação e nunca
contém senhas.
"""

import csv
import json
import os
import sys


ARQUIVO_REGISTRO = "registro_operacoes.json"


def carregar_registro(caminho=ARQUIVO_REGISTRO):
    if not os.path.exists(caminho):
        return {}
    try:
        with open(caminho, "r", encoding="utf-8") as arquivo:
            return json.load(arquivo)
    except (OSError, ValueError):
        return {}


def salvar_registro(caminho, registro):
    with open(caminho, "w", encoding="utf-8") as arquivo:
        json.dump(registro, arquivo, indent=2, ensure_ascii=False)


def chave_ont(registro):
    fsp = "%s/%d" % (registro["interface"], registro["porta"])
    return "%s/%d" % (fsp, registro["ont_id"])


CABECHAS_RELATO = [
    "data", "olt", "fsp", "ont_id", "serial", "classe", "metodo",
    "estado", "etapa", "consultas", "validacoes", "tempo_ms", "motivo",
    "executou", "observacoes",
]


def _configurar_codificacao():
    """Garante UTF-8 na saída (necessário no console do Windows)."""
    for fluxo in (sys.stdout, sys.stderr):
        reconfigurar = getattr(fluxo, "reconfigure", None)
        if reconfigurar is None:
            continue
        try:
            reconfigurar(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def gravar_relatorio(caminho, linhas):
    # utf-8-sig: o Excel do Windows abre o relatório sem acentos quebrados.
    with open(caminho, "w", encoding="utf-8-sig", newline="") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(CABECHAS_RELATO)
        for linha in linhas:
            escritor.writerow(
                [linha.get(chave, "") for chave in CABECHAS_RELATO])


