"""Funções auxiliares compartilhadas."""

from datetime import datetime


def texto(valor):
    """Converte um valor lido da planilha ou da CLI em texto.

    Os zeros à esquerda são preservados e os números lidos como float
    inteiro voltam a ser inteiros.
    """
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).strip()


def agora_iso():
    """Data e hora no formato usado no registro e no relatório."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def mascarar_valor(comando):
    """Substitui a senha de um comando pelo equivalente mascarado."""
    import re

    return re.sub(r'password\s+"[^"]*"', 'password "********"', comando)


def resumo_texto(texto_saida, tamanho=200):
    """Versão curta de uma saída da CLI, sem senhas, para diagnóstico."""
    limpo = (texto_saida or "").strip()
    limpo = mascarar_valor(limpo)
    limpo = " ".join(limpo.split())
    if len(limpo) > tamanho:
        limpo = "..." + limpo[-tamanho:]
    return limpo
