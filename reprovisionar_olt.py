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

ARQUIVO_CONFIG = "config.ini"

_PADROES_CONFIG = {
    "host": ("OLT_HOST", "127.0.0.1"),
    "porta": ("OLT_PORT", "22"),
    "usuario": ("OLT_USUARIO", ""),
    "senha": ("OLT_SENHA", ""),
    "chave": ("OLT_CHAVE", ""),
    "senha_enable": ("OLT_SENHA_ENABLE", ""),
    "enable": ("OLT_ENABLE", "enable"),
    "screen_length": ("OLT_SCREEN_LENGTH", "screen-length 0 temporary"),
    "banner": ("OLT_BANNER", "SSH-2.0-OpenSSH_8.9p1"),
    "interface": ("OLT_INTERFACE", "0/1"),
    "lineprofile": ("OLT_LINEPROFILE", "11"),
    "portas": ("OLT_PORTAS", "0-15"),
}


def carregar_configuracao(caminho=None):
    """Carrega a configuração do operador a partir de ini ou variáveis de ambiente.

    Sem caminho informado, usa `config.ini` da pasta atual, se existir.
    Variáveis de ambiente têm precedência sobre o arquivo, permitindo usar o
    arquivo de exemplo sem credenciais reais. A leitura tolera o BOM que o
    Bloco de Notas do Windows grava.
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
        self.comandos = []

    def conectar(self):
        host = self.configuracao["host"]
        porta = int(self.configuracao["porta"])
        usuario = self.configuracao["usuario"]
        senha = self.configuracao.get("senha", "")
        cliente = self._conectar(host, porta, usuario, senha)
        self.sessao = cliente.invoke_shell()
        self.conectada = True
        self.buffer = ""
        self._entrar_em_enable()
        self._desativar_paginacao()

    def _conectar(self, host, porta, usuario, senha):
        """Conecta discovering o método aceito pela OLT antes de autenticar.

        A MA5800 usa teclado-interativo (prompt "User password:") e pode
        bloquear novas tentativas por reentrada. Por isso a conexão é feita
        uma única vez: primeiro descobrimos os métodos aceitos e só então
        autenticamos, sem provocar uma tentativa recusada.
        """
        transporte = paramiko.Transport((host, porta))
        # Algumas OLTs resetam a conexão quando o identification string do
        # cliente não é reconhecido; o padrão é o do OpenSSH.
        identificacao = (self.configuracao.get("banner") or
                         "SSH-2.0-OpenSSH_8.9p1").strip()
        if identificacao:
            transporte.local_version = identificacao
        try:
            transporte.start_client(timeout=20)
            metodos = self._metodos_aceitos(transporte, usuario)
            if "keyboard-interactive" in metodos:
                logger.info("autenticando por teclado-interativo")
                transporte.auth_interactive(
                    usuario, lambda titulo, instrucoes, perguntas: [
                        senha for _ in perguntas])
            else:
                logger.info("autenticando por senha (metodos: %s)",
                            ",".join(metodos) or "desconhecido")
                transporte.auth_password(usuario, senha)
        except Exception:
            transporte.close()
            raise
        cliente = paramiko.SSHClient()
        cliente.load_system_host_keys()
        cliente.set_missing_host_key_policy(paramiko.RejectPolicy())
        cliente._transport = transporte
        return cliente

    def _metodos_aceitos(self, transporte, usuario):
        """Métodos de autenticação aceitos pelo servidor (sem falhar)."""
        try:
            transporte.auth_none(usuario)
            return []
        except paramiko.BadAuthenticationType as erro:
            return list(erro.allowed_types)
        except paramiko.AuthenticationException:
            return []

    def _entrar_em_enable(self):
        """Entra no modo privilegiado (`enable`), exigido por parte das CLIs.

        Sem isso, a OLT recusa os comandos de configuração (o prompt fica em
        `>` e não em `#`).
        """
        comando = (self.configuracao.get("enable") or "enable").strip()
        if not comando:
            return
        senha = (self.configuracao.get("senha_enable") or
                 self.configuracao.get("senha") or "")
        self.comandos.append(comando)
        self.sessao.send(comando + "\r")
        saida = self._ler_ate(
            lambda texto: "password" in texto.lower() or
            self._prompt_no_buffer(texto), 10)
        if "password" in saida.lower():
            if not senha:
                raise ErroOperacao(
                    "a OLT pediu senha de enable (OLT_SENHA_ENABLE)")
            self.sessao.send(senha + "\r")
            saida = self._ler_ate(self._prompt_no_buffer, 10)
        if "#" not in saida:
            logger.warning("modo privilegiado (enable) nao confirmado: %s",
                           _resumo(saida))

    def _desativar_paginacao(self):
        """Desliga a paginação da CLI (evita o prompt `--More--`).

        `screen-length 0 temporary` é o padrão da VRP: a saída inteira vem
        numa tela, sem esperar tecla. A forma temporária vale só para a
        sessão e não altera a configuração do equipamento.
        """
        configurado = (self.configuracao.get("screen_length") or
                       "auto").strip().lower()
        if configurado in ("none", "off", "nao", "não"):
            return
        candidatos = []
        if configurado not in ("auto", ""):
            candidatos.append(configurado)
        for variante in ("screen-length 0 temporary", "screen-length disable"):
            if variante not in candidatos:
                candidatos.append(variante)
        for comando in candidatos:
            self.comandos.append(comando)
            self.sessao.send(comando + "\r")
            saida = self._ler_ate(self._prompt_no_buffer, 10)
            if not _comando_desconhecido(saida):
                logger.info("paginacao da CLI desativada (%s)", comando)
                return
        logger.warning(
            "a CLI nao aceitou screen-length; listagens longas podem expirar "
            "(defina screen_length em config.ini)")

    def executar(self, comando):
        if not self.conectada:
            raise ErroOperacao("sessão SSH indisponível")
        comando = comando.strip()
        if not comando:
            return ""
        self.comandos.append(comando)
        self.sessao.send(comando + "\r")
        return self._ler_ate(self._prompt_no_buffer, self.timeout)

    def _ler_ate(self, predicado, timeout):
        """Lê a saída até o predicado ser satisfeito (ou o timeout)."""
        inicio = time.monotonic()
        saida = ""
        while True:
            disponivel, _, _ = select.select([self.sessao], [], [], 1)
            if not disponivel:
                if predicado(saida):
                    break
                if time.monotonic() - inicio > timeout:
                    raise ErroOperacao("timeout na execução do comando")
                continue
            dados = self.sessao.recv(4096)
            if not dados:
                break
            saida += dados.decode("utf-8", "replace")
            if predicado(saida):
                break
        return saida

    @property
    def timeout(self):
        return float(os.environ.get("OLT_TIMEOUT_COMANDO", "120"))

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


_PADRAO_POTENCIA = re.compile(r"^-?[\d.]+/-?[\d.]+$|^-/-")


def _parse_resumo_onts(saida, interface, porta):
    """Interpreta `display ont info summary <fsp>`; devolve as ONTs da porta.

    A resposta traz duas tabelas: a primeira com o estado de execução
    (online/offline) e a segunda com SN, tipo e descrição. A descrição é o
    texto configurado em `ont add ... desc`, que neste caso é o usuário.
    """
    estados = {}
    onts = []
    for linha in saida.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        partes = linha.split()
        if not partes[0].isdigit() or len(partes) < 2:
            continue
        try:
            ont_id = int(partes[0])
        except ValueError:
            continue
        if partes[1].lower() in ("online", "offline"):
            estados[ont_id] = partes[1].lower()
            continue
        serial = partes[1]
        descricao = ""
        for indice, parte in enumerate(partes):
            if _PADRAO_POTENCIA.match(parte):
                descricao = " ".join(partes[indice + 1:])
                break
        onts.append({
            "ont_id": ont_id,
            "F/S/P": "%s/%d" % (interface, porta),
            "Serial": serial,
            "Description": descricao,
            "estado": estados.get(ont_id, ""),
        })
    return onts


def listar_onts(sessao, interface, portas, alvo=None):
    """Lista as ONTs presentes na OLT; retorna dicionários por ID da ONT.

    A leitura é feita porta a porta com `display ont info summary <fsp>`.
    Quando `alvo` indica uma ONT específica (serial ou ID), a varredura
    para assim que ela é encontrada, aliviando a OLT de consultas extras.
    """
    onts = {}
    for porta in portas:
        saida = sessao.executar(
            "display ont info summary %s/%d" % (interface, porta))
        for ont in _parse_resumo_onts(saida, interface, porta):
            onts[ont["ont_id"]] = ont
        if alvo and _alvo_encontrado(onts, alvo):
            break
    return onts


def _alvo_encontrado(onts, alvo):
    if alvo.get("serial"):
        for ont in onts.values():
            if (ont.get("Serial") or "") == alvo["serial"]:
                return True
        return False
    if alvo.get("ont_id") is not None:
        return alvo["ont_id"] in onts
    return False


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
            _validar_vlan(configuracao["user_vlan"]),
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


_PADRAO_SRVPROFILE = re.compile(
    r"ont-srvprofile\s+gpon\s+profile-id\s+(?P<id>\d+)\s+"
    r"profile-name\s+\"(?P<nome>[^\"]+)\"")
_PADRAO_LINEPROFILE = re.compile(
    r"ont-lineprofile\s+gpon\s+profile-id\s+(?P<id>\d+)\s+"
    r"profile-name\s+\"(?P<nome>[^\"]+)\"")
_PADRAO_ONT_ADD = re.compile(
    r"ont\s+add\s+(?P<porta>\d+)\s+(?P<ont_id>\d+)\b.*?"
    r"ont-lineprofile-id\s+(?P<lineprofile>\d+)")
_PADRAO_ONT_SRVPROFILE = re.compile(
    r"ont\s+add\s+(?P<porta>\d+)\s+(?P<ont_id>\d+)\b.*?"
    r"ont-srvprofile-id\s+(?P<srvprofile>\d+)")
_PADRAO_ONT_DESC = re.compile(
    r"ont\s+add\s+(?P<porta>\d+)\s+(?P<ont_id>\d+)\b.*?"
    r"desc\s+\"(?P<descricao>[^\"]*)\"")
_PADRAO_ONT_TR069 = re.compile(
    r"ont\s+tr069-server-config\s+(?P<porta>\d+)\s+(?P<ont_id>\d+)\b.*?"
    r"profile-id\s+(?P<profile>\d+)")
_PADRAO_ONT_INTERNET = re.compile(
    r"ont\s+internet-config\s+(?P<porta>\d+)\s+(?P<ont_id>\d+)\b")
_PADRAO_ONT_WAN = re.compile(
    r"ont\s+wan-config\s+(?P<porta>\d+)\s+(?P<ont_id>\d+)\b.*?"
    r"profile-id\s+(?P<profile>\d+)")
_PADRAO_ONT_NATIVE_VLAN = re.compile(
    r"ont\s+port\s+native-vlan\s+(?P<porta>\d+)\s+(?P<ont_id>\d+)\b")
_PADRAO_ONT_IPCONFIG = re.compile(
    r"ont\s+ipconfig\s+(?P<porta>\d+)\s+(?P<ont_id>\d+)\b.*?"
    r"pppoe\s+vlan\s+(?P<vlan>\d+)\s+priority\s+(?P<prioridade>\d+)")
_PADRAO_ONT_USUARIO = re.compile(
    r"ont\s+ipconfig\s+(?P<porta>\d+)\s+(?P<ont_id>\d+)\b.*?"
    r"user-account\s+username\s+\"(?P<usuario>[^\"]*)\"")


def _consultar_configuracao_atual(sessao):
    """Lê a configuração corrente da OLT (somente leitura) e indexa as ONTs.

    Traz o que os `display ont ...` não expõem: os identificadores dos
    perfis (serviço, TR-069 e WAN) e quais configurações opcionais existem
    para cada ONT. A senha PPPoE que aparece no arquivo é ignorada e nunca
    é gravada em relatório, log ou saída.
    """
    saida = sessao.executar("display current-configuration | include ont")
    perfis = {"srv": {}, "linha": {}}
    onts = {}
    for linha in saida.splitlines():
        achado = _PADRAO_SRVPROFILE.search(linha)
        if achado:
            perfis["srv"][achado.group("nome")] = int(achado.group("id"))
            continue
        achado = _PADRAO_LINEPROFILE.search(linha)
        if achado:
            perfis["linha"][achado.group("nome")] = int(achado.group("id"))
            continue
        achado = _PADRAO_ONT_ADD.search(linha)
        if achado:
            chave = (int(achado.group("porta")), int(achado.group("ont_id")))
            dados_ont = onts.setdefault(chave, {})
            dados_ont["lineprofile"] = int(achado.group("lineprofile"))
            srvprofile = _PADRAO_ONT_SRVPROFILE.search(linha)
            if srvprofile:
                dados_ont["srvprofile"] = int(srvprofile.group("srvprofile"))
            descricao = _PADRAO_ONT_DESC.search(linha)
            if descricao:
                dados_ont["descricao"] = descricao.group("descricao")
            continue
        achado = _PADRAO_ONT_TR069.search(linha)
        if achado:
            chave = (int(achado.group("porta")), int(achado.group("ont_id")))
            onts.setdefault(chave, {})["tr069profile"] = int(
                achado.group("profile"))
            onts[chave]["tr069"] = True
            continue
        achado = _PADRAO_ONT_INTERNET.search(linha)
        if achado:
            chave = (int(achado.group("porta")), int(achado.group("ont_id")))
            onts.setdefault(chave, {})["internet"] = True
            continue
        achado = _PADRAO_ONT_WAN.search(linha)
        if achado:
            chave = (int(achado.group("porta")), int(achado.group("ont_id")))
            onts.setdefault(chave, {})["wanprofile"] = int(
                achado.group("profile"))
            onts[chave]["wan"] = True
            continue
        achado = _PADRAO_ONT_NATIVE_VLAN.search(linha)
        if achado:
            chave = (int(achado.group("porta")), int(achado.group("ont_id")))
            onts.setdefault(chave, {})["native_vlan"] = True
            continue
        achado = _PADRAO_ONT_IPCONFIG.search(linha)
        if achado:
            chave = (int(achado.group("porta")), int(achado.group("ont_id")))
            dados_ont = onts.setdefault(chave, {})
            dados_ont["vlan"] = achado.group("vlan")
            dados_ont["prioridade"] = achado.group("prioridade")
            continue
        achado = _PADRAO_ONT_USUARIO.search(linha)
        if achado:
            chave = (int(achado.group("porta")), int(achado.group("ont_id")))
            onts.setdefault(chave, {})["usuario"] = achado.group("usuario")
            continue
    return {"perfis": perfis, "onts": onts}


_PADRAO_SERVICE_PORT = re.compile(
    r"^\s*(?P<index>\d+)\s+(?P<vlan>\d+)\s+(?P<attr>\S+)\s+"
    r"gpon\s+(?P<frame>\d+/\d+)\s*/(?P<porta>\d+)\s+"
    r"(?P<vpi>\d+)\s+(?P<vci>\d+)\s+(?P<fluxo>\S+)\s+(?P<param>\S+)"
)


def _consultar_ipconfig(sessao, interface, porta, ont_id):
    """Lê o ipconfig da ONT.

    Na MA5800 o comando só existe na visão `interface gpon`; a sessão volta
    à configuração raiz ao final (`quit`).
    """
    sessao.executar("interface gpon %s" % interface)
    saida = sessao.executar(
        "display ont ipconfig %d %d" % (porta, ont_id))
    sessao.executar("quit")
    return saida


def _consultar_perfil_ont(sessao, interface, porta, ont_id):
    """Lê o nome dos perfis aplicados à ONT (`display ont profile`)."""
    saida = sessao.executar(
        "display ont profile %s %d %d" % (interface, porta, ont_id))
    for linha in saida.splitlines():
        partes = linha.split()
        if len(partes) < 3 or not partes[0].isdigit():
            continue
        if int(partes[0]) != ont_id:
            continue
        return {"linha": partes[1], "srv": partes[2]}
    return {}


def _consultar_servicos(sessao, interface, porta, ont_id):
    """Lista os service-ports da ONT; devolve ID, gemport e user-vlan.

    A listagem `display service-port all` é uma tabela "Switch-Oriented
    Flow List": o índice é o identificador do service-port, F/S/P indica a
    porta GPON, VPI é o identificador da ONT e VCI é o gemport.
    """
    saida = sessao.executar("display service-port all")
    alvo = "%s/%d" % (interface, porta)
    servicos = []
    for linha in saida.splitlines():
        encontrado = _PADRAO_SERVICE_PORT.match(linha)
        if not encontrado:
            continue
        valores = encontrado.groupdict()
        caminho = "%s/%s" % (valores["frame"], valores["porta"])
        if caminho != alvo:
            continue
        if int(valores["vpi"]) != ont_id:
            continue
        servicos.append({
            "id": int(valores["index"]),
            "vlan": valores["vlan"],
            "gpon": caminho,
            "ont_id": int(valores["vpi"]),
            "gemport": valores["vci"],
            "user-vlan": valores["param"],
        })
    return servicos


def _campo(campos, *chaves):
    """Devolve o primeiro campo presente entre as chaves informadas."""
    for chave in chaves:
        valor = campos.get(chave)
        if valor not in (None, ""):
            return valor
    return None


def capturar_estado(sessao, interface, porta, ont_id, config_atual=None):
    """Registra o estado atual da ONT, sem executar nenhum comando de alteração.

    Todos os demais valores são descobertos na resposta dos comandos de
    leitura; nenhum valor é assumido. Somente o lineprofile é configurável
    (definido fora desta função).
    """
    wan_info = sessao.executar(
        "display ont wan-info %s %d %d" % (interface, porta, ont_id))
    ipconfig = _consultar_ipconfig(sessao, interface, porta, ont_id)
    servicos = _consultar_servicos(
        sessao, interface, porta, ont_id)
    servico = servicos[0] if servicos else None
    campos_wan = _campos(wan_info)
    campos_ip = _campos(ipconfig)

    atual = (config_atual or {}).get("onts", {}).get((porta, ont_id))
    if atual is None:
        # A ONT não apareceu na configuração corrente: adota-se a sequência
        # validada (native-vlan, TR-069, internet e WAN) e os perfis precisam
        # ser descobertos; se não forem, a ONT fica bloqueada.
        atual = {
            "native_vlan": True,
            "tr069": True,
            "internet": True,
            "wan": True,
        }
    perfis = (config_atual or {}).get("perfis", {})
    nomes = _consultar_perfil_ont(sessao, interface, porta, ont_id)

    srvprofile = atual.get("srvprofile")
    if srvprofile is None and nomes.get("srv"):
        srvprofile = perfis.get("srv", {}).get(nomes["srv"])
    tr069profile = atual.get("tr069profile")
    if tr069profile is None and nomes.get("linha"):
        tr069profile = perfis.get("linha", {}).get(nomes["linha"])
    wanprofile = atual.get("wanprofile")
    usuario = _campo(campos_ip, "PPPoE username", "Username") or \
        atual.get("usuario") or ""
    vlane = _campo(campos_ip, "ONT manage VLAN", "Manage VLAN") or \
        atual.get("vlan")
    if not vlane:
        vlane = _campo(campos_wan, "Manage VLAN", "ONT manage VLAN")
    if not vlane:
        raise ErroOperacao("VLAN atual não identificada")
    prioridade = _campo(
        campos_ip, "ONT manage priority", "Manage priority") or \
        atual.get("prioridade")

    return {
        "usuario": usuario,
        "vlane": vlane,
        "prioridade": prioridade,
        "configuracao": _campo(campos_ip, "ONT config type", "Config type"),
        "lineprofile_atual": atual.get("lineprofile"),
        "srvprofile": srvprofile,
        "tr069profile": tr069profile,
        "wanprofile": wanprofile,
        "native_vlan": bool(atual.get("native_vlan")),
        "tr069": bool(atual.get("tr069")),
        "internet": bool(atual.get("internet")),
        "wan_config": bool(atual.get("wan")),
        "gemport": (servico or {}).get("gemport"),
        "user_vlan": (servico or {}).get("user-vlan") or vlane,
        "service_port": (servico or {}).get("id"),
        "tipo_wan": _campo(campos_wan, "Service type"),
        "status_conexao": _campo(campos_wan, "IPv4 Connection status"),
        "mac": _campo(campos_ip, "MAC address", "ONT MAC") or
        _campo(campos_wan, "MAC address", "ONT MAC"),
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
    if configuracao.get("native_vlan", True):
        passos.append(comando_native_vlan(configuracao))
    if configuracao.get("tr069"):
        passos.append(comando_configurar_tr069(configuracao))
    if configuracao.get("internet"):
        passos.append(comando_configurar_internet(configuracao))
    if configuracao.get("wan_config"):
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
    if estado["tipo_wan"] == "":
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
        self._config_atual = None
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
            try:
                estado_atual = capturar_estado(
                    self.sessao, dados["interface"], dados["porta"],
                    dados["ont_id"], self._configuracao_atual())
            except ErroOperacao as erro:
                # Uma ONT ilegível não interrompe as demais do card.
                self.resultados[identificador] = {
                    "classe": ESTADO_INCONCLUSIVO,
                    "motivo": "estado não identificado: %s" % erro,
                }
                continue
            for campo in ("vlane", "prioridade", "srvprofile",
                         "tr069profile", "wanprofile", "gemport",
                         "user_vlan", "service_port", "tr069", "internet",
                         "wan_config", "native_vlan"):
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

    def _configuracao_atual(self):
        """Configuração corrente da OLT, lida uma única vez por execução.

        Traz os identificadores dos perfis e quais configurações opcionais
        existem para cada ONT. A senha PPPoE presente no arquivo é ignorada
        e nunca é gravada em relatório, log ou saída.
        """
        if self._config_atual is None:
            try:
                self._config_atual = _consultar_configuracao_atual(self.sessao)
            except ErroOperacao as erro:
                logger.error("leitura da configuração corrente falhou: %s", erro)
                self._config_atual = {"perfis": {}, "onts": {}}
        return self._config_atual

    def _listar_onts(self):
        try:
            onts = listar_onts(
                self.sessao, self.configuracao["interface"], self._portas(),
                alvo=self.seletor or None)
        except ErroOperacao as erro:
            logger.error("consulta de ONTs falhou: %s", erro)
            return []
        return [self._dados(ont) for ont in self._filtrar(onts.values())]

    def _portas(self):
        """Portas GPON a consultar; configurável, com todas como padrão."""
        return portas_configuradas(self.configuracao)

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
            "usuario": estado_atual.get("usuario") or
            dados.get("descricao") or "",
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
            "tr069profile": dados.get("tr069profile"),
            "wanprofile": dados.get("wanprofile"),
            "tr069": dados.get("tr069", False),
            "internet": dados.get("internet", False),
            "wan_config": dados.get("wan_config", False),
            "native_vlan": dados.get("native_vlan", False),
            "usuario": dados["usuario"],
            "senha": dados["senha"],
            "gemport": dados["gemport"],
            "user_vlan": dados.get("user_vlan") or dados["vlane"],
        }
        for campo in ("vlan", "prioridade", "srvprofile", "gemport",
                     "user_vlan"):
            if not configuracao[campo]:
                raise ErroOperacao(
                    "valor atual não identificado: %s" % campo)
        if configuracao["tr069"] and not configuracao.get("tr069profile"):
            raise ErroOperacao("valor atual não identificado: tr069profile")
        if configuracao["wan_config"] and not configuracao.get("wanprofile"):
            raise ErroOperacao("valor atual não identificado: wanprofile")
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
        servicos = _consultar_servicos(
            self.sessao, interface, porta, ont_id)
        if servicos:
            return servicos[0]["id"]
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
            "descricao": atual.get("Description") or "",
            "situacao": atual.get("estado") or "",
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


def _exibir_origem_configuracao(configuracao):
    """Mostra de onde veio a configuração, para evitar confusão."""
    arquivo = configuracao.get("arquivo")
    if arquivo:
        print("  config: %s" % arquivo)
    else:
        print("  config: nenhum arquivo (usando variáveis de ambiente "
              "e valores padrão)")


def _exibir_passos(resultados):
    passos = []
    for resultado in resultados.values():
        passos.extend(resultado.get("passos", []))
    for comando in exibir_comandos(passos):
        print("  %s" % comando)


def _exibir_roteiro(sessao, resultados):
    """Mostra a transcrição completa do que o script faz na OLT.

    São listados os comandos de leitura efetivamente enviados e, em seguida,
    os comandos de alteração que seriam enviados apenas com `--execute`.
    """
    print("")
    print("=" * 72)
    print("TRANSCRIÇÃO - comandos enviados à OLT (somente leitura)")
    print("=" * 72)
    enviados = list(getattr(sessao, "comandos", []))
    if not enviados:
        print("  (nenhum comando registrado)")
    for comando in enviados:
        print("  [LEITURA] %s" % comando)
    print("")
    print("=" * 72)
    print("TRANSCRIÇÃO - comandos de ALTERAÇÃO (enviados só com --execute)")
    print("=" * 72)
    encontrado = False
    for identificador, resultado in sorted(resultados.items()):
        passos = resultado.get("passos", [])
        if not passos:
            continue
        encontrado = True
        print("")
        print("  ONT %s" % identificador)
        for comando in exibir_comandos(passos):
            print("    [ALTERAÇÃO] %s" % comando)
    if not encontrado:
        print("  (nenhuma ONT classificada para reprovisionamento)")


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


def portas_configuradas(configuracao):
    """Portas GPON a consultar; configurável, com todas como padrão."""
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


def _comando_desconhecido(saida):
    """Indica se a OLT recusou o comando enviado."""
    texto = (saida or "").lower()
    return any(marca in texto for marca in (
        "unknown command", "unrecognized command", "incomplete command",
        "wrong parameter", "parameter error", "too many parameters"))


def _resumo(texto, tamanho=200):
    """Resumo curto de uma saída, sem senhas, para diagnóstico."""
    limpo = (texto or "").strip()
    limpo = re.sub(r'(password|Password)\s*"[^"]*"', r'\1 "****"', limpo)
    limpo = re.sub(r"\s+", " ", limpo)
    if len(limpo) > tamanho:
        limpo = "..." + limpo[-tamanho:]
    return limpo


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
    parser.add_argument("--diagnostico", action="store_true",
                      help="mostra o estado descoberto em cada ONT (só leitura)")
    parser.add_argument("--roteiro", action="store_true",
                      help="mostra a transcrição completa de comandos "
                           "(leituras enviadas e alterações planejadas)")
    return parser.parse_args(argv)


def _conexao_configurada(configuracao):
    return bool(configuracao["host"]) and configuracao["host"] != "127.0.0.1"


def _executar_diagnostico(sessao, configuracao, seletor):
    """Lê o estado de cada ONT e mostra o que foi descoberto (sem alterar).

    Serve para validar na bancada o que o script enxerga na OLT antes de
    qualquer execução: nada além de consultas de leitura é enviado.
    """
    interface = configuracao["interface"]
    portas = portas_configuradas(configuracao)
    onts = listar_onts(sessao, interface, portas, alvo=seletor or None)
    if seletor.get("ont_id") is not None:
        onts = {chave: ont for chave, ont in onts.items()
                if chave == seletor["ont_id"]}
    if seletor.get("serial"):
        onts = {chave: ont for chave, ont in onts.items()
                if (ont.get("Serial") or "") == seletor["serial"]}
    if not onts:
        print("nenhuma ONT encontrada em %s (portas %s)"
              % (interface, ",".join(str(p) for p in portas)))
        return
    print("ONTs encontradas: %d" % len(onts))
    try:
        config_atual = _consultar_configuracao_atual(sessao)
    except ErroOperacao as erro:
        print("configuração corrente indisponível: %s" % erro)
        config_atual = {"perfis": {}, "onts": {}}
    for ont_id in sorted(onts):
        ont = onts[ont_id]
        partes = (ont.get("F/S/P") or "").split("/")
        porta = int(partes[-1]) if partes[-1].isdigit() else 0
        print("")
        print("ONT %d (%s) serial=%s descricao=%s estado=%s"
              % (ont_id, ont.get("F/S/P"), ont.get("Serial"),
                 ont.get("Description"), ont.get("estado")))
        try:
            estado = capturar_estado(
                sessao, interface, porta, ont_id, config_atual)
        except ErroOperacao as erro:
            print("   INCONCLUSIVO: %s" % erro)
            continue
        for campo in ("usuario", "vlane", "prioridade", "srvprofile",
                      "tr069profile", "wanprofile", "gemport", "user_vlan",
                      "service_port", "tipo_wan", "status_conexao", "mac"):
            valor = estado.get(campo)
            if valor in (None, ""):
                print("   %-15s: (não identificado)" % campo)
            else:
                print("   %-15s: %s" % (campo, valor))
        for flag in ("native_vlan", "tr069", "internet", "wan_config"):
            print("   %-15s: %s" % (flag, "sim" if estado.get(flag) else "não"))


def main(argv=None):
    _configurar_codificacao()
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
    elif argumentos.mock:
        print("modo: simulação local (OLT simulada, sem conexão)")
    else:
        print("modo: DRY-RUN na OLT (somente leitura, nada será alterado)")
    registros, problemas = ler_planilha(argumentos.Arquivo)
    print("planilha: %s" % _resumo_planilha(registros, problemas))
    _exibir_origem_configuracao(configuracao)
    indice_por_serial, indice_por_usuario = indexar(registros)
    seletor = {"ont_id": argumentos.ont_id, "serial": argumentos.serial}
    if argumentos.testar_ssh:
        sessao = conectar_ssh(configuracao)
        sessao.fechar()
        print("conexão SSH ok")
        print("concluído")
        return 0
    if executar or not argumentos.mock:
        # Mesmo no DRY-RUN a OLT real e consultada; nenhum comando de
        # alteracao e enviado nesse modo.
        try:
            sessao = conectar_ssh(configuracao)
        except Exception as erro:
            print("ERRO: nao foi possivel conectar na OLT: %s" % erro)
            if "known_hosts" in str(erro):
                print("     a chave do servidor nao esta em ~/.ssh/known_hosts;")
                print("     acrescente com:")
                print("       ssh-keyscan -H %s >> ~/.ssh/known_hosts"
                      % configuracao["host"])
            elif "Authentication" in str(erro) or "authentication" in str(erro):
                print("     a OLT recusou a autenticacao. conferir:")
                print("       - usuario e senha em config.ini")
                print("       - se a conta exige troca de senha no primeiro acesso")
                print("       - teste manual: ssh %s@%s"
                      % (configuracao["usuario"], configuracao["host"]))
            print("     use --mock para rodar sem OLT (dados simulados).")
            return 1
    else:
        sessao = Simulador()
    try:
        if argumentos.diagnostico:
            _executar_diagnostico(sessao, configuracao, seletor)
            return 0
        orquestrador = Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=executar,
            seletor=seletor,
        )
        resultados = orquestrador.executar(
            registros, indice_por_serial, indice_por_usuario)
        _exibir_detalhes(resultados)
        _exibir_configuracao(configuracao)
        if not executar:
            _exibir_passos(resultados)
        if argumentos.roteiro:
            _exibir_roteiro(sessao, resultados)
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
