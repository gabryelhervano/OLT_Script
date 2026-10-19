#!/usr/bin/env python3
"""Reprovisionamento de ONTs em OLTs Huawei via SSH.

Consulta as ONTs existentes na OLT, associa os registros da planilha PPPoE,
classifica os três padrões de CPE e executa o fluxo validado de reprovisionamento
(configuração PPPoE, OMCI, WAN e TR-069) quando autorizado.

Sem `execute`, nenhuma alteração é enviada: apenas consultas de leitura são
executadas, sempre com a sessão SSH aberta. Senhas nunca aparecem em logs,
relatórios ou mensagens de erro.
"""

import configparser
import csv
import json
import logging
import os
import re
import select
import sys
import time
from datetime import datetime

try:
    import openpyxl
except ImportError:  # pragma: no cover
    raise SystemExit("openpyxl é necessário: pip install openpyxl")

try:
    import paramiko
except ImportError:  # pragma: no cover
    raise SystemExit("paramiko é necessário: pip install paramiko")

logger = logging.getLogger("reprovisionar_olt")


def texto(valor):
    """Converte valores lidos da planilha ou da CLI em texto preservando zeros."""
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).strip()


def agora_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Configuração (arquivo ini e variáveis de ambiente; sem credenciais fixas)
# ---------------------------------------------------------------------------

_PADROES_CONFIG = {
    "host": ("OLT_HOST", "127.0.0.1"),
    "porta": ("OLT_PORT", "22"),
    "usuario": ("OLT_USUARIO", ""),
    "senha": ("OLT_SENHA", ""),
    "chave": ("OLT_CHAVE", ""),
    "interface": ("OLT_INTERFACE", "0/1"),
    "lineprofile": ("OLT_LINEPROFILE", "11"),
}


def carregar_configuracao(caminho=None):
    """Carrega a configuração do operador a partir de ini ou variáveis de ambiente.

    Variáveis de ambiente têm precedência sobre o arquivo, permitindo usar o
    arquivo de exemplo sem credenciais reais.
    """
    parser = configparser.ConfigParser()
    if caminho and os.path.exists(caminho):
        parser.read(caminho)
    configuracao = {}
    for chave, (variavel, padrao) in _PADROES_CONFIG.items():
        configuracao[chave] = _variavel(variavel, parser, chave) or padrao
    return configuracao


def _variavel(nome, parser, chave):
    valor = os.environ.get(nome)
    if valor and valor.strip():
        return valor.strip()
    try:
        return parser.get("olt", chave, fallback="").strip()
    except configparser.Error:
        return ""


# ---------------------------------------------------------------------------
# Planilha de usuários
# ---------------------------------------------------------------------------

_CHAVE_USUARIO = "usuario"
_CHAVE_SENHA = "senha"
_CHAVE_MAC = "mac"
_CHAVE_SERIAL = "serial"

_COLUNAS_PADRAO = (_CHAVE_USUARIO, _CHAVE_SENHA, _CHAVE_MAC, _CHAVE_SERIAL)

_PADRAO_MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
_PADRAO_SERIAL = re.compile(r"^[0-9A-Za-z]{4,20}$")
_MASCARAS_MAC = frozenset(("ff:ff:ff:ff:ff:ff",))
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
    valores = [_texto(valor) for valor in list(linha)[:4]]
    return valores + [""] * (4 - len(valores))


def _texto(valor):
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).strip()


def _eh_mac(valor):
    if _PADRAO_MAC.match(valor) is None:
        return False
    return valor.lower() not in _MASCARAS_MAC


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
    primeira = [_texto(valor).lower() for valor in linhas[0]]
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
        return dicionario
    for indice, chave in atribuicao.items():
        dicionario[chave] = celulas[indice]
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


def resumo(registros, problemas):
    contagem = {}
    for registro in registros:
        estado = registro["estado"]
        contagem[estado] = contagem.get(estado, 0) + 1
    for problema in problemas:
        estado = problema["estado"]
        contagem[estado] = contagem.get(estado, 0) + 1
    return contagem


# ---------------------------------------------------------------------------
# Erros de uso
# ---------------------------------------------------------------------------


class ErroOperacao(Exception):
    """Base para falhas que interrompem a execução sem alterar a OLT."""


class RegistroInexistente(ErroOperacao):
    """Raised when an ONT cannot be identified with certainty."""


class CredenciaisNaoTransmitidas(ErroOperacao):
    """Raised when a value cannot be sent safely through the CLI."""


# ---------------------------------------------------------------------------
# SSH e CLI do equipamento
# ---------------------------------------------------------------------------


class Transporte:
    """Interface mínima usadas pela camada SSH; permite simulação em testes."""

    def executar(self, comando):
        raise NotImplementedError

    def fechar(self):
        pass


class SessaoSimulada(Transporte):
    """Transporte falso para testes e para o modo `--mock`, sem conexão real.

    Registra todos os comandos enviados, permitindo provar que comandos de
    alteração nunca são enviados no DRY-RUN.
    """

    def __init__(self):
        self.comandos = []
        self.respostas = {}
        self.conectada = True

    def definir_resposta(self, comando, saida):
        self.respostas[comando] = saida

    def executar(self, comando):
        self.comandos.append(comando)
        return self.respostas.get(comando, RESPOSTA_OK)

    def fechar(self):
        self.conectada = False


RESPOSTA_OK = """{ <cr>||<K> }:

  Command:
          {comando}
   ---------------------------------------------------------------------
"""


class SessaoSSH:
    """Conexão SSH real com o CLI Huawei, com leitura controlada e timeout."""

    def __init__(self, configuracao):
        self.configuracao = configuracao
        self.conectada = False
        self.buffer = ""

    def conectar(self):
        host = self.configuracao["host"]
        porta = int(self.configuracao["porta"])
        chave = self.configuracao["chave"]
        if chave:
            cliente = paramiko.SSHClient()
            cliente.load_system_host_keys()
            cliente.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            cliente = paramiko.SSHClient()
            cliente.load_system_host_keys()
        cliente.connect(host, port=porta, username=self.configuracao["usuario"],
                      password=self.configuracao.get("senha", ""), look_for_keys=False,
                      allow_agent=False)
        self.sessao = cliente.invoke_shell()
        self.conectada = True
        self.buffer = ""

    def executar(self, comando):
        if not self.conectada:
            raise ErroOperacao("sessão SSH indisponível")
        comando = comando.strip()
        if not comando:
            return ""
        inicio = time.monotonic()
        self.sessao.send(comando + "\r")
        saida = ""
        while True:
            disponivel, _, _ = select.select([self.sessao], [], [], 1)
            if not disponivel:
                if self._prompt_no_buffer(saida):
                    break
                if time.monotonic() - inicio > self.timeout:
                    raise ErroOperacao("timeout na execução do comando")
                continue
            dados = self.sessao.recv(4096)
            if not dados:
                break
            saida += dados.decode("utf-8", "replace")
            if self._prompt_no_buffer(saida):
                break
        return saida

    @property
    def timeout(self):
        return float(os.environ.get("OLT_TIMEOUT_COMANDO", "30"))

    def _prompt_no_buffer(self, saida):
        if not saida or not saida.strip():
            return False
        ultima = saida.rstrip().splitlines()[-1].strip()
        return bool(re.search(r"[#>]\s*$", ultima))

    def fechar(self):
        if self.conectada:
            try:
                self.sessao.close()
            except Exception:
                logger.exception("falha ao encerrar a sessão SSH")
        self.conectada = False


def conectar_ssh(configuracao):
    sessao = SessaoSSH(configuracao)
    sessao.conectar()
    return sessao


# ---------------------------------------------------------------------------
# Consultas de leitura
# ---------------------------------------------------------------------------

def _campos(texto_saida):
    campos = {}
    for linha in texto_saida.splitlines():
        linha = linha.strip()
        if ":" not in linha:
            continue
        chave, valor = linha.split(":", 1)
        chave = chave.strip()
        if chave and chave not in campos:
            campos[chave] = valor.strip()
    return campos


def _salvar_ont(onts, atual, campos):
    if atual is None:
        return
    onts[atual] = {
        "ont_id": atual,
        "F/S/P": campos.get("F/S/P", ""),
        "Serial": campos.get("Serial", ""),
        "ONT MAC": campos.get("ONT MAC", ""),
        "Service type": campos.get("Service type", ""),
    }


def listar_onts(sessao):
    """Lista as ONTs presentes na OLT; retorna dicionários por ID da ONT.

    A resposta depende da CLI; linhas que apresentam `ONT ID` iniciam um novo
    equipamento. Os demais campos são capturados quando a CLI os apresenta.
    """
    saida = sessao.executar("display ont")
    onts = {}
    atual = None
    campos = {}
    for linha in saida.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        if "ONT ID" in linha:
            if atual is not None:
                _salvar_ont(onts, atual, campos)
            campos = _campos(linha)
            try:
                atual = int(campos["ONT ID"])
            except (KeyError, ValueError):
                atual = None
            if atual is not None:
                onts.setdefault(atual, {"ont_id": atual})
            continue
        if atual is None or ":" not in linha:
            continue
        chave, valor = linha.split(":", 1)
        chave = chave.strip()
        if chave in ("F/S/P", "Serial", "ONT MAC", "Service type"):
            campos[chave] = valor.strip()
    if atual is not None:
        _salvar_ont(onts, atual, campos)
    return onts


# ---------------------------------------------------------------------------
# Montagem dos comandos (somente com parâmetros validados)
# ---------------------------------------------------------------------------


def _aspas(valor):
    if '"' in valor or "\\" in valor or "\n" in valor:
        raise CredenciaisNaoTransmitidas("valor não pode ser transmitido com segurança")
    return '"%s"' % valor


def _inteiro(valor, nome, minimo, maximo):
    try:
        numero = int(texto(valor))
    except ValueError:
        raise ErroOperacao("%s inválido" % nome)
    if numero < minimo or numero > maximo:
        raise ErroOperacao("%s fora do intervalo" % nome)
    return numero


def _validar_vlan(valor):
    return _inteiro(valor, "VLAN", 1, 4094)


def _validar_prioridade(valor):
    return _inteiro(valor, "prioridade", 0, 4094)


def comando_desconectar_service_port(service_port_id):
    return "undo service-port %d" % service_port_id


def comando_abrir_interface(interface):
    return "interface gpon %s" % interface


def comando_remover_ont(porta, ont_id):
    return "ont delete %d %d" % (porta, ont_id)


def _perfil(configuracao, chave):
    return _inteiro(configuracao[chave], chave, 1, 4096)


def _vlan(configuracao):
    return _validar_vlan(configuracao["vlan"])


def _prioridade(configuracao):
    return _validar_prioridade(configuracao["prioridade"])


def comando_criar_ont(configuracao):
    """Monta `ont add` a partir dos dados validados."""
    porta = configuracao["porta"]
    ont_id = configuracao["ont_id"]
    comando = (
        "ont add %d %d sn-auth %s omci ont-lineprofile-id %d ont-srvprofile-id %d desc %s"
        % (
            porta,
            ont_id,
            _aspas(configuracao["serial"]),
            _perfil(configuracao, "lineprofile"),
            _perfil(configuracao, "srvprofile"),
            _aspas(configuracao["descricao"]),
        )
    )
    return comando


def comando_configurar_pppoe(configuracao):
    comando = (
        "ont ipconfig %d %d pppoe vlan %d priority %d user-account username %s password %s"
        % (
            configuracao["porta"],
            configuracao["ont_id"],
            _vlan(configuracao),
            _prioridade(configuracao),
            _aspas(configuracao["usuario"]),
            _aspas(configuracao["senha"]),
        )
    )
    return comando


def comando_native_vlan(configuracao):
    return (
        "ont port native-vlan %d %d eth 1 vlan %d priority 0"
        % (
            configuracao["porta"],
            configuracao["ont_id"],
            _vlan(configuracao),
        )
    )


def comando_configurar_tr069(configuracao):
    return "ont tr069-server-config %d %d profile-id %d" % (
        configuracao["porta"],
        configuracao["ont_id"],
        _perfil(configuracao, "tr069profile"),
    )


def comando_configurar_internet(configuracao):
    """Ativa o serviço de internet do ONT (ip-index 0)."""
    return "ont internet-config %d %d ip-index 0" % (
        configuracao["porta"],
        configuracao["ont_id"],
    )


def comando_sair_interface():
    """Volta ao contexto de configuração raiz (necessário para o service-port)."""
    return "quit"


def comando_configurar_wan(configuracao):
    return "ont wan-config %d %d ip-index 0 profile-id %d" % (
        configuracao["porta"],
        configuracao["ont_id"],
        _perfil(configuracao, "wanprofile"),
    )


def comando_criar_service_port(configuracao):
    """A OLT atribui o identificador; nenhum ID é enviado no comando."""
    caminho_gpon = "%s/%d" % (
        configuracao["interface"], configuracao["porta"])
    return (
        "service-port vlan %d gpon %s ont %d gemport %d multi-service user-vlan %d tag-transform translate"
        % (
            _vlan(configuracao),
            caminho_gpon,
            configuracao["ont_id"],
            int(configuracao["gemport"]),
            _vlan(configuracao),
        )
    )


def comando_exibir_wan_info(interface, porta, ont_id):
    return "display ont wan-info %s %d %d" % (interface, porta, ont_id)


def comando_exibir_ipconfig(porta, ont_id):
    return "display ont ipconfig %d %d" % (porta, ont_id)


def _mascarar_valor(comando):
    return re.sub(r'password\s+"[^"]*"', 'password "********"', comando)


def exibir_comandos(comandos):
    return [_mascarar_valor(comando) for comando in comandos]


# ---------------------------------------------------------------------------
# Classificação dos três padrões de CPE
# ---------------------------------------------------------------------------

PADRAO_1_COMPLETA = "PADRAO_1_CORRESPONDENCIA_COMPLETA"
PADRAO_2_SERIAL = "PADRAO_2_CORRESPONDENCIA_POR_SERIAL"
PADRAO_2_PPPOE = "PADRAO_2_CORRESPONDENCIA_POR_PPPOE"
IGNORADA_SEM_IDENTIFICADORES = "IGNORADA_SEM_IDENTIFICADORES"
BLOQUEADA_DIVERGENCIA = "BLOQUEADA_DIVERGENCIA"
BLOQUEADA_AMBIGUIDADE = "BLOQUEADA_AMBIGUIDADE"


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
        return {"classe": BLOQUEADA_AMBIGUIDADE, "motivo": "identificador duplicado"}
    if registro_serial is not None and registro_usuario is not None:
        if registro_serial is registro_usuario:
            return {"classe": PADRAO_1_COMPLETA, "registro": registro_serial}
        return {"classe": BLOQUEADA_DIVERGENCIA, "motivo": "divergencia entre serial e usuário"}
    if registro_serial is not None:
        return {"classe": PADRAO_2_SERIAL, "registro": registro_serial}
    if registro_usuario is not None:
        return {"classe": PADRAO_2_PPPOE, "registro": registro_usuario}
    return {"classe": IGNORADA_SEM_IDENTIFICADORES, "motivo": "sem identificador"}


# ---------------------------------------------------------------------------
# Fluxo de reprovisionamento
# ---------------------------------------------------------------------------

ESTADO_SUCESSO = "SUCESSO"
ESTADO_PARCIAL = "PARCIAL"
ESTADO_INCONCLUSIVO = "INCONCLUSIVO"
ETAPA_VALIDACAO = "validacao"


def _consultar_servicos(sessao, ont_id):
    """Lista os service-ports atuais; devolve ID, gemport e user-vlan."""
    saida = sessao.executar("display service-port")
    servicos = {}
    atual = None
    for linha in saida.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        partes = linha.split()
        if len(partes) < 2:
            continue
        if partes[0].lower().startswith("display") or \
                partes[0].lower().startswith("service-port"):
            try:
                atual = int(partes[1])
            except ValueError:
                atual = None
            if atual is not None:
                servicos.setdefault(atual, {})
                servicos[atual]["id"] = atual
        if atual is None:
            continue
        for indice, parte in enumerate(partes):
            if indice == 0 or indice + 1 >= len(partes):
                continue
            if parte.startswith("gemport"):
                servicos[atual]["gemport"] = partes[indice + 1]
            if parte.startswith("user-vlan"):
                servicos[atual]["user-vlan"] = partes[indice + 1]
            if parte == "ont":
                try:
                    identificado = int(partes[indice + 1])
                except ValueError:
                    continue
                if identificado == ont_id:
                    servicos[atual]["ont_id"] = identificado
    return servicos


def capturar_estado(sessao, interface, porta, ont_id):
    """Registra o estado atual da ONT, sem executar nenhum comando de alteração.

    Todos os demais valores são descobertos na resposta dos comandos de
    leitura; nenhum valor é assumido. Somente o lineprofile é configurável
    (definido fora desta função).
    """
    wan_info = sessao.executar(
        "display ont wan-info %s %d %d" % (interface, porta, ont_id))
    ipconfig = sessao.executar(
        "display ont ipconfig %d %d" % (porta, ont_id))
    servicos = _consultar_servicos(sessao, ont_id)
    servico = None
    for identificador, valores in servicos.items():
        if valores.get("ont_id") == ont_id:
            servico = valores
    vlane = _campos(ipconfig).get("ONT manage VLAN", "")
    if not vlane:
        raise ErroOperacao("VLAN atual não identificada")
    return {
        "usuario": _campos(ipconfig).get("PPPoE username", ""),
        "vlane": vlane,
        "prioridade": _campos(ipconfig).get("ONT manage priority", ""),
        "configuracao": _campos(ipconfig).get("ONT config type", ""),
        "srvprofile": _campos(wan_info).get("srvprofile"),
        "tr069profile": _campos(wan_info).get("tr069profile"),
        "wanprofile": _campos(wan_info).get("wanprofile"),
        "gemport": (servico or {}).get("gemport"),
        "user_vlan": (servico or {}).get("user-vlan") or vlane,
        "service_port": (servico or {}).get("id"),
        "wan": _campos(wan_info).get("Service type", ""),
        "status_conexao": _campos(wan_info).get("IPv4 Connection status", ""),
        "mac": _campos(ipconfig).get("ONT MAC", ""),
    }


def montar_passos(interface, configuracao, estado_original):
    """Constrói a sequência de comandos que seria executada, em ordem de execução.

    `undo service-port` e `service-port` são comandos da configuração raiz:
    o primeiro é enviado antes de entrar em `interface gpon` e o segundo
    depois de sair dela (`quit`).
    """
    porta = configuracao["porta"]
    ont_id = configuracao["ont_id"]
    passos = []
    service_port_id = estado_original.get("service_port")
    if service_port_id:
        passos.append(comando_desconectar_service_port(service_port_id))
    passos.append(comando_abrir_interface(interface))
    passos.append(comando_remover_ont(porta, ont_id))
    passos.append(comando_criar_ont(configuracao))
    passos.append(comando_configurar_pppoe(configuracao))
    passos.append(comando_native_vlan(configuracao))
    passos.append(comando_configurar_tr069(configuracao))
    passos.append(comando_configurar_internet(configuracao))
    passos.append(comando_configurar_wan(configuracao))
    passos.append(comando_sair_interface())
    passos.append(comando_criar_service_port(configuracao))
    passos.append(comando_exibir_wan_info(interface, porta, ont_id))
    passos.append(comando_exibir_ipconfig(porta, ont_id))
    return passos


def validar_estado_final(configuracao, estado):
    """Compara o estado capturado com o esperado; devolve estado e motivo."""
    usuario = configuracao["usuario"]
    if estado["usuario"] != usuario:
        return ESTADO_PARCIAL, "usuário PPPoE incorreto"
    if estado["vlane"] != configuracao["vlan"]:
        return ESTADO_PARCIAL, "VLAN incorreta"
    if estado["prioridade"] != configuracao["prioridade"]:
        return ESTADO_PARCIAL, "prioridade incorreta"
    if estado["configuracao"] != "PPPoE":
        return ESTADO_PARCIAL, "configuração PPPoE incorreta"
    if estado["wan"] == "":
        return ESTADO_PARCIAL, "configuração WAN ausente"
    if estado["status_conexao"] != "Connected":
        return ESTADO_PARCIAL, "conexão não estabilizada"
    return ESTADO_SUCESSO, ""


# ---------------------------------------------------------------------------
# Registro persistente e controle de execução
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

CABECHAS_RELATO = [
    "data", "olt", "fsp", "ont_id", "serial", "classe", "metodo",
    "estado", "etapa", "consultas", "validacoes", "tempo_ms", "motivo",
    "executou", "observacoes",
]


def gravar_relatorio(caminho, linhas):
    with open(caminho, "w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(CABECHAS_RELATO)
        for linha in linhas:
            escritor.writerow([linha.get(chave, "") for chave in CABECHAS_RELATO])


# ---------------------------------------------------------------------------
# Orquestrador, modos de execução e relatório final
# ---------------------------------------------------------------------------

ESTADO_SUCESSO = "SUCESSO"
ESTADO_PARCIAL = "PARCIAL"
ESTADO_INCONCLUSIVO = "INCONCLUSIVO"
ETAPA_VALIDACAO = "validacao"


class ControleExecucao:
    """Limites de execução e suspensão após falhas consecutivas."""

    def __init__(self, maximo, atraso, pausa_apos, tempo_maximo):
        self.maximo = maximo
        self.atraso = atraso
        self.pausa_apos = pausa_apos
        self.tempo_maximo = tempo_maximo
        self.em_execucao = False
        self.falhas = 0
        self.suspenso = False
        self.inicio = None
        self.processados = 0

    def iniciar(self):
        self.em_execucao = True
        self.falhas = 0
        self.suspenso = False
        self.inicio = time.monotonic()
        self.processados = 0

    def aguardar(self):
        if self.em_execucao and self.atraso:
            time.sleep(self.atraso)

    def registrar_sucesso(self):
        self.falhas = 0

    def registrar_falha(self):
        self.falhas += 1
        if self.falhas >= 3:
            self.suspenso = True
            logger.error("execução suspensa após %d falhas consecutivas", self.falhas)

    def verificar_limite(self, processados):
        if processados >= self.maximo:
            return False
        return True

    def registrar_pausa(self):
        if self.pausa_apos and self.processados % self.pausa_apos == 0:
            logger.info("pausa automática")
            time.sleep(30)

    @property
    def esgotado(self):
        return self.falhas >= 3 or self.suspenso


def _checar_tempo(controle):
    if controle.tempo_maximo and controle.inicio:
        if time.monotonic() - controle.inicio > controle.tempo_maximo:
            raise ErroOperacao("tempo máximo de execução excedido")


class Simulador:
    """Substitui a conexão SSH para o modo `--mock` e para os testes.

    Registra todos os comandos enviados, sem tocar em equipamentos reais.
    """

    def __init__(self):
        self.comandos = []

    def executar(self, comando):
        self.comandos.append(comando)
        return RESPOSTA_OK

    def fechar(self):
        pass


class Orquestrador:
    """Coordena a leitura, a classificação e a execução de uma única OLT."""

    def __init__(self, configuracao, sessao, executar_alteracoes,
                caminho_registro=ARQUIVO_REGISTRO, seletor=None):
        self.configuracao = configuracao
        self.sessao = sessao
        self.executar_alteracoes = executar_alteracoes
        self.controle = None
        self.resultados = {}
        self.processados = set()
        self.registro = carregar_registro(caminho_registro)
        self.concluidos = set()
        for identificador, info in self.registro.items():
            if info.get("classe") == ESTADO_SUCESSO:
                self.concluidos.add(identificador)
        self.seletor = seletor or {}

    def executar(self, registros, indice_por_serial, indice_por_usuario):
        self.processados = set()
        self.controle = ControleExecucao(
            maximo=self.configuracao.get("maximo", 1),
            atraso=self.configuracao.get("atraso", 0),
            pausa_apos=self.configuracao.get("pausa", 0),
            tempo_maximo=self.configuracao.get("tempo_maximo"),
        )
        self.controle.iniciar()
        for card, onts in self._agrupar(self._listar_onts()):
            if not self._preparar_card(
                    card, onts, indice_por_serial, indice_por_usuario):
                return self.resultados
            for dados in onts:
                self._processar(dados, indice_por_serial, indice_por_usuario)
        salvar_registro(ARQUIVO_REGISTRO, self.registro)
        return self.resultados

    def _agrupar(self, onts):
        """Agrada as ONTs por card, para pedir a confirmação por card."""
        grupos = {}
        for dados in onts:
            card = dados["interface"]
            grupos.setdefault(card, []).append(dados)
        return sorted(grupos.items())

    def _preparar_card(self, card, onts, indice_por_serial,
                    indice_por_usuario):
        """Classifica o card e confirma antes de executar qualquer comando."""
        executaveis = []
        for dados in onts:
            identificador = chave_ont(dados)
            if identificador in self.concluidos:
                continue
            estado_atual = capturar_estado(
                self.sessao, dados["interface"], dados["porta"], dados["ont_id"])
            for campo in ("vlane", "prioridade", "srvprofile",
                         "tr069profile", "wanprofile", "gemport",
                         "service_port"):
                if campo in estado_atual:
                    dados[campo] = estado_atual[campo]
            resultado = self._classificar(
                dados, estado_atual, indice_por_serial, indice_por_usuario)
            if "registro" in resultado:
                dados["usuario"] = resultado["registro"][_CHAVE_USUARIO]
                dados["senha"] = resultado["registro"][_CHAVE_SENHA]
            if resultado["classe"] in (PADRAO_1_COMPLETA, PADRAO_2_SERIAL,
                                      PADRAO_2_PPPOE):
                try:
                    resultado["passos"] = self._montar_passos(dados)
                except ErroOperacao as erro:
                    resultado["classe"] = ESTADO_INCONCLUSIVO
                    resultado["motivo"] = "falha na execução: %s" % erro
                self.resultados[identificador] = resultado
                executaveis.append(dados)
            else:
                self.resultados[identificador] = resultado
        if not executaveis:
            return True
        if not self.executar_alteracoes:
            logger.info(
                "simulação: card %s teria %d reprovisionamentos",
                card, len(executaveis))
            return True
        print("card %s: %d ONTs seriam reprovisionados" % (card, len(executaveis)))
        if not _confirmar("Executar alterações neste card?"):
            return False
        return True

    def _listar_onts(self):
        try:
            onts = listar_onts(self.sessao)
        except ErroOperacao as erro:
            logger.error("consulta de ONTs falhou: %s", erro)
            return []
        return [self._dados(ont) for ont in self._filtrar(onts.values())]

    def _filtrar(self, onts):
        selecionado = {}
        if self.seletor.get("ont_id") is not None:
            selecionado["ont_id"] = self.seletor["ont_id"]
        if self.seletor.get("serial"):
            selecionado["serial"] = self.seletor["serial"]
        resultado = []
        for ont in onts:
            if "ont_id" in selecionado and ont["ont_id"] != selecionado["ont_id"]:
                continue
            if "serial" in selecionado and ont.get("Serial", "") != selecionado["serial"]:
                continue
            resultado.append(ont)
        return resultado

    def _classificar(self, dados, estado_atual, indice_por_serial,
                  indice_por_usuario):
        ont_atual = {
            "serial": dados.get("serial") or "",
            "usuario": estado_atual.get("usuario") or "",
        }
        return classificar(ont_atual, indice_por_serial, indice_por_usuario)

    def _processar(self, dados, indice_por_serial, indice_por_usuario):
        identificador = chave_ont(dados)
        if identificador in self.concluidos or identificador in self.processados:
            return
        if not self.controle.verificar_limite(self.controle.processados):
            return
        _checar_tempo(self.controle)
        self.controle.aguardar()
        self.controle.processados += 1
        self.controle.registrar_pausa()
        self.processados.add(identificador)
        try:
            if self.executar_alteracoes:
                self._executar(dados)
        except ErroOperacao as erro:
            self.controle.registrar_falha()
            self.resultados[chave_ont(dados)] = {
                "classe": ESTADO_INCONCLUSIVO,
                "motivo": "falha na execução: %s" % erro,
            }

    def _executar(self, dados):
        configuracao = self._montar_configuracao(dados)
        estado_atual = capturar_estado(
            self.sessao, dados["interface"], dados["porta"], dados["ont_id"])
        estado_atual["service_port"] = self._service_port_id(
            dados["interface"], dados["porta"], dados["ont_id"])
        if self._ja_estava_configurado(configuracao, estado_atual):
            self.resultados[chave_ont(dados)]["classe"] = ESTADO_SUCESSO
            self._salvar()
            return
        if not estado_atual["service_port"]:
            self.resultados[chave_ont(dados)] = {
                "classe": ESTADO_INCONCLUSIVO,
                "motivo": "service-port original não identificado",
            }
            self._salvar()
            return
        passos = montar_passos(
            dados["interface"], configuracao, estado_atual)
        self.resultados[chave_ont(dados)]["passos"] = passos
        for comando in passos:
            if comando.startswith("display"):
                continue
            self.sessao.executar(comando)
            self.controle.registrar_sucesso()
        estado_final = capturar_estado(
            self.sessao, dados["interface"], dados["porta"], dados["ont_id"])
        classe, motivo = validar_estado_final(configuracao, estado_final)
        self.resultados[chave_ont(dados)] = {
            "classe": classe,
            "motivo": motivo,
        }
        self._salvar()

    def _salvar(self):
        salvar_registro(ARQUIVO_REGISTRO, self.registro)

    def _montar_configuracao(self, dados):
        configuracao = {
            "porta": dados["porta"],
            "ont_id": dados["ont_id"],
            "interface": dados["interface"],
            "serial": dados["serial"],
            "descricao": dados.get("descricao") or dados["usuario"],
            "lineprofile": self.configuracao["lineprofile"],
            "vlan": dados["vlane"],
            "prioridade": dados["prioridade"],
            "srvprofile": dados["srvprofile"],
            "tr069profile": dados["tr069profile"],
            "wanprofile": dados["wanprofile"],
            "usuario": dados["usuario"],
            "senha": dados["senha"],
            "gemport": dados["gemport"],
        }
        for campo in ("vlan", "prioridade", "srvprofile",
                     "tr069profile", "wanprofile", "gemport"):
            if not configuracao[campo]:
                raise ErroOperacao(
                    "valor atual não identificado: %s" % campo)
        return configuracao

    def _montar_passos(self, dados):
        configuracao = self._montar_configuracao(dados)
        # Revalida o estado atual antes de montar os comandos (somente leitura).
        capturar_estado(
            self.sessao, dados["interface"], dados["porta"], dados["ont_id"])
        return montar_passos(
            dados["interface"], configuracao,
            {"service_port": dados.get("service_port")})

    def _service_port_id(self, interface, porta, ont_id):
        """Identificador do service-port atual da ONT, descoberto por leitura.

        O valor é usado apenas no `undo service-port`; nenhum identificador
        novo é enviado no comando de criação.
        """
        servicos = _consultar_servicos(self.sessao, ont_id)
        for identificador, valores in servicos.items():
            if valores.get("ont_id") == ont_id:
                return valores.get("id")
        return None

    def _ja_estava_configurado(self, configuracao, estado_atual):
        return False

    def _dados(self, atual):
        fsp = atual.get("F/S/P") or ""
        partes = fsp.split("/")
        if len(partes) < 2:
            raise ErroOperacao("F/S/P não informado")
        return {
            "interface": "/".join(partes[:-1]),
            "porta": int(partes[-1]),
            "ont_id": atual["ont_id"],
            "serial": atual.get("Serial") or "",
            "usuario": atual.get("usuario") or "",
            "senha": atual.get("senha") or "",
            "gemport": atual.get("gemport") or "",
        }


# ---------------------------------------------------------------------------
# Entrada e saída
# ---------------------------------------------------------------------------

def _resumo_planilha(registros, problemas):
    contagem = resumo(registros, problemas)
    return (
        "válidos: %d | inválidos: %d | duplicados: %d | ausentes: %d"
        % (
            contagem.get("VALIDO", 0),
            contagem.get("INVALIDO", 0),
            contagem.get("DUPLICADO", 0),
            contagem.get("IGNORADO", 0),
        )
    )


def _exibir_detalhes(resultados):
    for identificador, resultado in resultados.items():
        print("  %s -> %s" % (identificador, resultado["classe"]))
        if resultado.get("motivo"):
            print("      motivo: %s" % resultado["motivo"])
        for comando in exibir_comandos(resultado.get("passos", [])):
            print("      %s" % comando)


def _exibir_configuracao(configuracao):
    print("  interface GPON: %s" % configuracao["interface"])
    print("  perfil linha (configurável): %s" % configuracao["lineprofile"])
    print("  demais parâmetros são descobertos no estado atual da ONT")


def _exibir_passos(resultados):
    passos = []
    for resultado in resultados.values():
        passos.extend(resultado.get("passos", []))
    for comando in exibir_comandos(passos):
        print("  %s" % comando)


def _exibir_contagem(resultados):
    contagem = {}
    for resultado in resultados.values():
        classe = resultado["classe"]
        contagem[classe] = contagem.get(classe, 0) + 1
    for classe, quantidade in sorted(contagem.items()):
        print("  %s: %d" % (classe, quantidade))


def gravar_resultados(caminho, resultados, configuracao):
    linhas = []
    for identificador, resultado in resultados.items():
        linhas.append({
            "data": agora_iso(),
            "olt": configuracao["olt"],
            "identificador": identificador,
            "classe": resultado["classe"],
            "motivo": resultado.get("motivo") or "",
        })
    gravar_relatorio(caminho, linhas)
    return linhas


def _confirmar(mensagem):
    print("ATENÇÃO: " + mensagem)
    resposta = input().strip().lower()
    return resposta in ("sim", "s", "yes", "1")


def _parse_argumentos(argv):
    import argparse

    parser = argparse.ArgumentParser(
        description="Reprovisionamento de ONTs em OLTs Huawei.")
    parser.add_argument("--file", dest="Arquivo", required=True,
                      help="caminho da planilha PPPoE")
    parser.add_argument("--olt", dest="olt", default="",
                      help="identificação da OLT configurada")
    parser.add_argument("--execute", dest="executar", action="store_true",
                      help="executa alterações (exige confirmações)")
    parser.add_argument("--ont-id", dest="ont_id", type=int, default=None,
                      help="ONT ID para testar uma única CPE")
    parser.add_argument("--serial", dest="serial", default="",
                      help="serial para testar uma única CPE")
    parser.add_argument("--limit", dest="limite", type=int, default=1,
                      help="máximo de ONTs a reprovisionar")
    parser.add_argument("--delay", dest="atraso", type=float, default=0,
                      help="intervalo mínimo entre operações")
    parser.add_argument("--pausa", dest="pausa", type=int, default=0,
                      help="pausas automáticas a cada N ONTs")
    parser.add_argument("--tempo-maximo", dest="tempo_maximo", type=float,
                      default=None, help="limite de tempo em segundos")
    parser.add_argument("--report", dest="relatorio", default="",
                      help="caminho do relatório CSV")
    parser.add_argument("--config", dest="configuracao", default="",
                      help="caminho do arquivo de configuração")
    parser.add_argument("--test-ssh", dest="testar_ssh", action="store_true",
                      help="valida a conexão SSH sem alterar a OLT")
    parser.add_argument("--mock", action="store_true",
                      help="executa o fluxo com uma OLT simulada")
    return parser.parse_args(argv)


def _conexao_configurada(configuracao):
    return bool(configuracao["host"]) and configuracao["host"] != "127.0.0.1"


def main(argv=None):
    argumentos = _parse_argumentos(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    configuracao = carregar_configuracao(
        argumentos.configuracao or None)
    configuracao["olt"] = argumentos.olt or configuracao["host"]
    configuracao["maximo"] = argumentos.limite
    configuracao["atraso"] = argumentos.atraso
    configuracao["pausa"] = argumentos.pausa
    if argumentos.tempo_maximo:
        configuracao["tempo_maximo"] = argumentos.tempo_maximo
    executar = argumentos.executar
    if executar:
        print("modo: execução real (haverá confirmação por card)")
    else:
        print("modo: simulação (DRY-RUN)")
    registros, problemas = ler_planilha(argumentos.Arquivo)
    print("planilha: %s" % _resumo_planilha(registros, problemas))
    indice_por_serial, indice_por_usuario = indexar(registros)
    if argumentos.testar_ssh:
        sessao = conectar_ssh(configuracao)
        sessao.fechar()
        print("conexão SSH ok")
    elif executar:
        sessao = conectar_ssh(configuracao)
    else:
        sessao = Simulador()
    try:
        orquestrador = Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=executar,
            seletor={"ont_id": argumentos.ont_id, "serial": argumentos.serial},
        )
        resultados = orquestrador.executar(
            registros, indice_por_serial, indice_por_usuario)
        _exibir_detalhes(resultados)
        _exibir_configuracao(configuracao)
        if not executar:
            _exibir_passos(resultados)
        _exibir_contagem(resultados)
        if argumentos.relatorio:
            gravar_resultados(
                argumentos.relatorio, resultados, configuracao)
    finally:
        sessao.fechar()
    print("concluído")
    return 0


if __name__ == "__main__":
    sys.exit(main())
