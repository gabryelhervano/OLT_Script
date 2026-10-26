"""Classificação das ONTs nos três padrões de CPE.

A classificação cruza a identidade da ONT na OLT (serial e usuário PPPoE)
com os registros da planilha. Casos ambíguos ou divergentes são bloqueados.
"""

from .estados import (BLOQUEADA_AMBIGUIDADE, BLOQUEADA_DIVERGENCIA,
                      IGNORADA_SEM_IDENTIFICADORES, PADRAO_1_COMPLETA,
                      PADRAO_2_PPPOE, PADRAO_2_SERIAL)


def _encontrar(indice, identificador):
    """Localiza um identificador; devolve o registro ou None quando ambíguo."""
    entradas = indice.get(identificador)
    if not entradas:
        return None, False
    if len(entradas) > 1:
        return None, True
    return entradas[0], False


def classificar(ont_atual, indice_por_serial, indice_por_usuario):
    """Classifica a ONT nos três padrões e indica o método de associação."""
    serial_atual = ont_atual.get("serial") or ""
    usuario_atual = ont_atual.get("usuario") or ""
    registro_serial, ambiguo_serial = _encontrar(
        indice_por_serial, serial_atual) if serial_atual else (None, False)
    registro_usuario, ambiguo_usuario = _encontrar(
        indice_por_usuario, usuario_atual) if usuario_atual else (None, False)
    if ambiguo_serial or ambiguo_usuario:
        return {"classe": BLOQUEADA_AMBIGUIDADE,
                "motivo": "identificador duplicado"}
    if registro_serial is not None and registro_usuario is not None:
        if registro_serial is registro_usuario:
            return {"classe": PADRAO_1_COMPLETA, "registro": registro_serial}
        return {"classe": BLOQUEADA_DIVERGENCIA,
                "motivo": "divergência entre serial e usuário"}
    if registro_serial is not None:
        return {"classe": PADRAO_2_SERIAL, "registro": registro_serial}
    if registro_usuario is not None:
        return {"classe": PADRAO_2_PPPOE, "registro": registro_usuario}
    return {"classe": IGNORADA_SEM_IDENTIFICADORES,
            "motivo": "sem identificador"}


