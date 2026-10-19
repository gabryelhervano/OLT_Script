"""Constantes de estado usadas pelo fluxo.

Ficam num módulo à parte para que a descoberta, o orquestrador e o
relatório compartilhem exatamente os mesmos valores.
"""

# Situações da ONT no fluxo de reprovisionamento.
ESTADO_SUCESSO = "SUCESSO"
ESTADO_PARCIAL = "PARCIAL"
ESTADO_INCONCLUSIVO = "INCONCLUSIVO"

# Etapa em que a ONT parou.
ETAPA_VALIDACAO = "validacao"

# Padrões de CPE reconhecidos na classificação.
PADRAO_1_COMPLETA = "PADRAO_1_CORRESPONDENCIA_COMPLETA"
PADRAO_2_SERIAL = "PADRAO_2_CORRESPONDENCIA_POR_SERIAL"
PADRAO_2_PPPOE = "PADRAO_2_CORRESPONDENCIA_POR_PPPOE"

# Situações em que a ONT não é reprovisionada.
IGNORADA_SEM_IDENTIFICADORES = "IGNORADA_SEM_IDENTIFICADORES"
BLOQUEADA_DIVERGENCIA = "BLOQUEADA_DIVERGENCIA"
BLOQUEADA_AMBIGUIDADE = "BLOQUEADA_AMBIGUIDADE"
