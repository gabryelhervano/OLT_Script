"""Leitura e validação da planilha de usuários PPPoE.

A planilha tem quatro colunas: usuário, senha, MAC da ONT e serial da ONT.
O cabeçalho é opcional: quando existe, os nomes das colunas definem a
ordem; quando não existe, a ordem acima é usada.
"""

import re

try:
    import openpyxl
except ImportError:  # pragma: no cover
    raise SystemExit("openpyxl é necessário: pip install openpyxl")

from .erros import ErroOperacao
from .util import texto


_CHAVE_USUARIO = "usuario"
_CHAVE_SENHA = "senha"
_CHAVE_MAC = "mac"
_CHAVE_SERIAL = "serial"

_COLUNAS_PADRAO = (_CHAVE_USUARIO, _CHAVE_SENHA, _CHAVE_MAC, _CHAVE_SERIAL)

# A OLT mostra o MAC como XXXX-XXXX-XXXX; a planilha costuma usar
# XX:XX:XX:XX:XX:XX. As duas formas são aceitas.
_PADRAO_MAC = re.compile(
    r"^(?:[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"
    r"|[0-9A-Fa-f]{4}(?:-[0-9A-Fa-f]{4}){2})$")
_PADRAO_SERIAL = re.compile(r"^[0-9A-Za-z]{4,20}$")
_MASCARAS_MAC = frozenset(("ff:ff:ff:ff:ff:ff", "ffffffffffff"))
_MASCARAS_SERIAL = frozenset(("ffffffffffffffff", "ffff:ffff:ffff:ffff"))


def ler_planilha(caminho):
    """Lê a planilha sem presumir a existência de cabeçalho.

    Retorna ``(registros, problemas)``. Registros válidos são retornados em
    ordem; problemas registram linhas inválidas, ausentes ou duplicadas.
    """
    livro = openpyxl.load_workbook(
        filename=caminho, read_only=True, data_only=True)
    try:
        if not livro.sheetnames:
            raise ErroOperacao("planilha sem folhas")
        folha = livro.worksheets[0]
        linhas = list(folha.iter_rows(values_only=True))
    finally:
        livro.close()
    if not linhas:
        return [], []
    cabecalho = _cabecalho(linhas)
    primeira = 2 if cabecalho is not None else 1
    atribuicao = cabecalho
    registros = []
    problemas = []
    for numero, linha in enumerate(linhas[primeira - 1:], start=primeira):
        dicionario = _converter_celulas(_celulas(linha), atribuicao)
        estado, motivo = _validar(dicionario)
        if estado == "VALIDO":
            dicionario["estado"] = "VALIDO"
            dicionario["linha"] = numero
            registros.append(dicionario)
        else:
            problemas.append({
                "linha": numero,
                "estado": estado,
                "motivo": motivo,
            })
    _marcar_duplicados(registros, problemas)
    return registros, problemas


def _celulas(linha):
    valores = [texto(valor) for valor in list(linha)[:4]]
    return valores + [""] * (4 - len(valores))


def _eh_mac(valor):
    if _PADRAO_MAC.match(valor) is None:
        return False
    return valor.lower() not in _MASCARAS_MAC


def _normalizar_mac(valor):
    """Deixa o MAC no formato com dois-pontos (XX:XX:XX:XX:XX:XX)."""
    if not valor or "-" not in valor:
        return valor
    grupos = []
    for parte in valor.split("-"):
        grupos.extend(parte[indice:indice + 2]
                       for indice in range(0, len(parte), 2))
    return ":".join(grupos)


def _eh_serial(valor):
    if _PADRAO_SERIAL.match(valor) is None:
        return False
    return valor.lower() not in _MASCARAS_SERIAL


def _eh_marca_mac(valor):
    return valor.lower() in _MASCARAS_MAC


def _eh_marca_serial(valor):
    return valor.lower() in _MASCARAS_SERIAL


def _cabecalho(linhas):
    """Detecta a linha de cabeçalho apenas quando os nomes são reconhecíveis."""
    primeira = [texto(valor).lower() for valor in linhas[0]]
    atribuicao = {}
    for indice, celula in enumerate(primeira):
        for chave, nomes in (
            (_CHAVE_USUARIO, ("usuario", "usuário", "user", "pppoe user")),
            (_CHAVE_SENHA, ("senha", "password", "passwd", "senha pppoe")),
            (_CHAVE_MAC, ("mac", "endereco mac", "endereço mac")),
            (_CHAVE_SERIAL, ("serial", "número serial", "numero serial")),
        ):
            if celula in nomes:
                atribuicao[indice] = chave
    if not atribuicao:
        return None
    return _completar_atribuicao(atribuicao)


def _completar_atribuicao(atribuicao):
    usadas = set(atribuicao.values())
    livres = [chave for chave in _COLUNAS_PADRAO if chave not in usadas]
    indices = [indice for indice in range(len(_COLUNAS_PADRAO))
               if indice not in atribuicao]
    for chave, indice in zip(livres, indices):
        atribuicao[indice] = chave
    return atribuicao


def _converter_celulas(celulas, atribuicao):
    dicionario = {}
    if atribuicao is None:
        for indice, chave in enumerate(_COLUNAS_PADRAO):
            dicionario[chave] = celulas[indice]
    else:
        for indice, chave in atribuicao.items():
            dicionario[chave] = celulas[indice]
    if dicionario.get(_CHAVE_MAC):
        dicionario[_CHAVE_MAC] = _normalizar_mac(dicionario[_CHAVE_MAC])
    return dicionario


def _validar(dicionario):
    usuario = dicionario.get(_CHAVE_USUARIO, "")
    senha = dicionario.get(_CHAVE_SENHA, "")
    mac = dicionario.get(_CHAVE_MAC, "")
    serial = dicionario.get(_CHAVE_SERIAL, "")
    if not senha:
        return "INVALIDO", "senha ausente"
    if _eh_marca_mac(mac):
        return "IGNORADO", "ausencia de informacao no MAC"
    if _eh_marca_serial(serial):
        return "IGNORADO", "ausencia de informacao no serial"
    if usuario and not _eh_usuario(usuario):
        return "INVALIDO", "usuario invalido"
    if mac and not _eh_mac(mac):
        return "INVALIDO", "MAC invalido"
    if serial and not _eh_serial(serial):
        return "INVALIDO", "serial invalido"
    if not usuario and not serial:
        return "INVALIDO", "sem identificador para associação"
    return "VALIDO", ""


def _eh_usuario(valor):
    return len(valor) > 0


def _marcar_duplicados(registros, problemas):
    contagem = {}
    for registro in registros:
        if registro["estado"] != "VALIDO":
            continue
        for chave in (_CHAVE_SERIAL, _CHAVE_USUARIO):
            identificador = registro[chave]
            contagem.setdefault((chave, identificador), []).append(registro)
    duplicados = set()
    for entradas in contagem.values():
        if len(entradas) > 1:
            duplicados.update(id(registro) for registro in entradas)
    for registro in registros:
        if id(registro) in duplicados:
            registro["estado"] = "DUPLICADO"
            registro["motivo"] = "identificador duplicado"
            problemas.append({
                "linha": registro["linha"],
                "estado": "DUPLICADO",
                "motivo": "identificador duplicado",
            })


def indexar(registros):
    por_serial = {}
    por_usuario = {}
    for registro in registros:
        if registro["estado"] != "VALIDO":
            continue
        por_serial.setdefault(registro[_CHAVE_SERIAL], []).append(registro)
        por_usuario.setdefault(registro[_CHAVE_USUARIO], []).append(registro)
    return por_serial, por_usuario


def resumo_planilha(registros, problemas):
    contagem = {}
    for registro in registros:
        estado = registro["estado"]
        contagem[estado] = contagem.get(estado, 0) + 1
    for problema in problemas:
        estado = problema["estado"]
        contagem[estado] = contagem.get(estado, 0) + 1
    return contagem


