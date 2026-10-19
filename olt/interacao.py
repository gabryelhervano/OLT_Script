"""Interação com o operador durante a execução."""

import sys


def confirmar(mensagem):
    """Pede a confirmação do operador e devolve True quando aceita."""
    print("ATENÇÃO: " + mensagem)
    try:
        resposta = input().strip().lower()
    except EOFError:
        # Sem terminal (execução automatizada), a resposta é negativa.
        print("  (sem terminal: não confirmado)")
        return False
    return resposta in ("sim", "s", "yes", "1")


def configurar_codificacao():
    """Garante UTF-8 na saída (necessário no console do Windows)."""
    for fluxo in (sys.stdout, sys.stderr):
        reconfigurar = getattr(fluxo, "reconfigure", None)
        if reconfigurar is None:
            continue
        try:
            reconfigurar(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
