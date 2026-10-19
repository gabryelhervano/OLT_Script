"""Erros usados pela ferramenta."""


class ErroOperacao(Exception):
    """Falha que interrompe a execução sem alterar a OLT."""


class CredenciaisNaoTransmitidas(ErroOperacao):
    """O valor contém caracteres que a CLI não transmite com segurança."""
