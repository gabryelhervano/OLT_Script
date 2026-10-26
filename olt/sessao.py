"""Sessão com a OLT.

Duas implementações de transporte: a sessão SSH real, usada na execução e
no DRY-RUN, e a sessão simulada, usada nos testes e no modo `--mock`.
"""

import logging
import os
import re
import select
import time

import paramiko

from .erros import ErroOperacao
from .util import resumo_texto

logger = logging.getLogger("reprovisionar_olt")

_UNDO_PAGINACAO = {
    "scroll": "undo scroll",
    "scroll 512": "undo scroll",
    "screen-length 0 temporary": "undo screen-length temporary",
    "screen-length disable": "undo screen-length",
}


# Respostas em que a OLT recusa o comando. As consultas de leitura podem
# falhar assim sem que a ONT esteja com erro (por exemplo, uma ONT sem WAN),
# por isso a checagem vale para os comandos de alteração.
MARCAS_DE_ERRO = (
    "failure:",
    "error:",
    "% unknown command",
    "% unrecognized command",
    "% incomplete command",
    "% parameter error",
    "% wrong parameter",
    "% too many parameters",
)

# Marcas de comando que a OLT não reconhece. Valem para as consultas de
# leitura também: um `display` inexistente não significa erro na ONT.
_MARCAS_DE_COMANDO_DESCONHECIDO = (
    "unknown command", "unrecognized command", "incomplete command",
    "wrong parameter", "parameter error", "too many parameters",
)


def erro_na_resposta(saida):
    """Devolve a linha de erro da resposta da CLI, se houver."""
    for linha in (saida or "").splitlines():
        limpa = linha.strip().lower()
        if any(marca in limpa for marca in MARCAS_DE_ERRO):
            return linha.strip()
    return None


class Transporte:
    """Base comum às sessões: define o observador do modo `--debug`.

    A sessão SSH e a simulada trocam com a OLT do mesmo jeito, por isso a
    vigilância da troca fica num único lugar.
    """

    def executar(self, comando):
        raise NotImplementedError

    def fechar(self):
        pass

    def _monitorar(self, comando, saida, alteracao):
        """Repassa a troca ao modo debug, exatamente como foi trocada."""
        if self.monitor is None:
            return
        self.monitor(comando, saida, alteracao)


RESPOSTA_OK = """{ <cr>||<K> }:

  Command:
          {comando}
   ---------------------------------------------------------------------
"""


class SessaoSimulada(Transporte):
    """Transporte falso para testes e para o modo `--mock`, sem conexão real.

    Registra todos os comandos enviados, permitindo provar que comandos de
    alteração nunca são enviados no DRY-RUN.
    """

    def __init__(self, monitor=None):
        self.comandos = []
        self.respostas = {}
        self.conectada = True
        self.monitor = monitor

    def definir_resposta(self, comando, saida):
        self.respostas[comando] = saida

    def executar(self, comando):
        return self._trocar(comando, alteracao=False)

    def alterar(self, comando):
        """Envia um comando de alteração, recusando respostas de erro."""
        saida = self._trocar(comando, alteracao=True)
        erro = erro_na_resposta(saida)
        if erro:
            raise ErroOperacao("a OLT recusou o comando: %s" % erro)
        return saida

    def _trocar(self, comando, alteracao):
        self.comandos.append(comando)
        saida = self.respostas.get(comando, RESPOSTA_OK)
        self._monitorar(comando, saida, alteracao)
        return saida

    def fechar(self):
        self.conectada = False


class SessaoSSH(Transporte):
    """Conexão SSH real com o CLI Huawei, com leitura controlada e timeout."""

    def __init__(self, configuracao, monitor=None):
        self.configuracao = configuracao
        self.conectada = False
        self.buffer = ""
        self.comandos = []
        self.monitor = monitor
        self._undo_paginacao = ""
        self._saida_parcial = ""

    def conectar(self):
        host = self.configuracao["host"]
        porta = int(self.configuracao["porta"])
        usuario = self.configuracao["usuario"]
        senha = self.configuracao.get("senha", "")
        cliente = self._conectar(host, porta, usuario, senha)
        self.sessao = cliente.invoke_shell(
            width=self.largura, height=self.altura)
        self.conectada = True
        self.buffer = ""
        # Consome o banner de login antes de qualquer comando: sem isso as
        # respostas ficam dessincronizadas (o prompt do banner é confundido
        # com a resposta do comando) e a leitura seguinte expira.
        self._esperar_prompt()
        self._entrar_em_enable()
        self._desativar_paginacao()
        self._entrar_em_configuracao()

    def _esperar_prompt(self, timeout=20):
        """Aguarda o prompt inicial da CLI (banner de login)."""
        return self._ler_ate(self._prompt_no_buffer, timeout)

    def _conectar(self, host, porta, usuario, senha):
        """Conecta descobrindo antes o método de autenticação aceito.

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

    def alterar(self, comando):
        """Envia um comando de alteração, recusando respostas de erro.

        Sem essa checagem, um comando rejeitado pela OLT passaria
        despercebido e a ONT ficaria mal configurada.
        """
        saida = self._enviar_ler(comando, alteracao=True)
        erro = erro_na_resposta(saida)
        if erro:
            raise ErroOperacao("a OLT recusou o comando: %s" % erro)
        return saida

    def _entrar_em_enable(self):
        """Entra no modo privilegiado (`enable`), exigido por parte das CLIs.

        Sem isso, a OLT recusa os comandos de configuração (o prompt fica em
        `>` e não em `#`). O comando é repetido uma vez porque algumas CLIs
        perdem o primeiro envio (ainda processando o banner de login).
        """
        comando = (self.configuracao.get("enable") or "enable").strip()
        if not comando:
            return
        senha = (self.configuracao.get("senha_enable") or
                 self.configuracao.get("senha") or "")
        for tentativa in range(2):
            saida = self._executar_enable(comando, senha)
            if "#" in saida:
                return
        logger.warning("modo privilegiado (enable) não confirmado: %s",
                       resumo_texto(saida))

    def _executar_enable(self, comando, senha):
        self.comandos.append(comando)
        self._enviar(comando + self.terminador)
        saida = self._ler_ate(
            lambda texto: "password" in texto.lower() or
            self._prompt_no_buffer(texto), 10)
        if "password" in saida.lower():
            if not senha:
                raise ErroOperacao(
                    "a OLT pediu senha de enable (OLT_SENHA_ENABLE)")
            self._enviar(senha + self.terminador)
            saida = self._ler_ate(self._prompt_no_buffer, 10)
        self._monitorar(comando, saida, alteracao=False)
        return saida

    @property
    def terminador(self):
        """Terminador de linha enviado à CLI (CR, LF ou CRLF)."""
        valor = str(self.configuracao.get("terminador") or "cr")
        valor = valor.strip().lower()
        return {"lf": "\n", "crlf": "\r\n", "cr": "\r"}.get(valor, "\r")

    def _desativar_paginacao(self):
        """Desliga a paginação da CLI (evita o prompt `--More--`).

        A MA5800 não possui `screen-length`; o equivalente é `scroll` sem
        argumento, conforme indicado pela própria Huawei no fórum do
        netmiko ("This is the equivalent to screen-length 0 temporary").
        `scroll 512` amplia a tela e serve como alternativa. Nenhuma das
        variantes altera a configuração do equipamento.
        """
        configurado = (self.configuracao.get("screen_length") or
                       "auto").strip().lower()
        if configurado in ("none", "off", "nao", "não"):
            return
        candidatos = []
        if configurado not in ("auto", ""):
            candidatos.append(configurado)
        for variante in ("scroll", "scroll 512", "screen-length 0 temporary",
                         "screen-length disable"):
            if variante not in candidatos:
                candidatos.append(variante)
        for comando in candidatos:
            saida = self._tentar_comando(comando, enter_extra=True)
            if not _comando_desconhecido(saida):
                logger.info("paginação da CLI desativada (%s)", comando)
                self._undo_paginacao = _UNDO_PAGINACAO.get(
                    comando, "undo scroll")
                return
        # Algumas CLIs só aceitam o comando na visão de configuração.
        if self._tentar_na_visao_de_configuracao(candidatos):
            return
        logger.warning(
            "a CLI não aceitou nenhum comando de desativação da paginação "
            "(%s); listagens longas podem expirar", ", ".join(candidatos))

    def _tentar_na_visao_de_configuracao(self, candidatos):
        """Tenta os comandos dentro de `system-view` e volta ao modo normal."""
        entrada = self._tentar_comando("system-view", timeout=8)
        if _comando_desconhecido(entrada) or "[" not in entrada:
            return False
        for comando in candidatos:
            saida = self._tentar_comando(comando)
            if not _comando_desconhecido(saida):
                logger.info("paginação da CLI desativada (%s, em system-view)",
                            comando)
                self._undo_paginacao = _UNDO_PAGINACAO.get(
                    comando, "undo scroll")
                self._tentar_comando("quit", timeout=8)
                return True
        self._tentar_comando("quit", timeout=8)
        return False

    def _entrar_em_configuracao(self):
        """Entra na visão de configuração, onde os comandos de alteração
        são válidos.

        Na MA5800 o comando é `config` (na VRP padrão, `system-view`).
        """
        for comando in ("config", "system-view"):
            saida = self._tentar_comando(comando, timeout=15)
            # A MA5800 usa "(config)"; a VRP padrão usa "[~Nome]".
            if ("(" in saida or "[" in saida) and \
                    not _comando_desconhecido(saida):
                logger.info("visão de configuração ativa (%s)", comando)
                return True
        logger.warning("não foi possível entrar na visão de configuração")
        return False

    def _enviar_comando(self, comando, enter_extra=True):
        """Envia um comando à CLI, com um Enter adicional.

        Na MA5800 cada comando precisa de um Enter a mais para ser
        concluído: sem ele a linha fica pendente e o próximo comando se
        junta a ela ("display version display ont ..."). Antes de enviar,
        a sessão é aguardada ficar ociosa, para não perder caracteres
        enquanto a OLT ainda processa o comando anterior.
        """
        self._aguardar_ociosa(espera_max=5, silencio=0.3)
        self.comandos.append(comando)
        self._enviar(comando + self.terminador)
        if enter_extra:
            self._enviar(self.terminador)

    def _tentar_comando(self, comando, timeout=20, enter_extra=True):
        """Envia um comando e devolve a resposta crua da CLI.

        Algumas CLIs não reimprimem o prompt depois de determinados
        comandos (o `scroll` desliga a exibição do "Command:"). Nesse
        caso a espera expira, mas a sessão continua viva: o que já chegou
        é devolvido e a CLI é aguardada ficar ociosa antes do próximo
        comando.
        """
        self._enviar_comando(comando, enter_extra=enter_extra)
        try:
            saida = self._ler_ate(self._prompt_no_buffer, timeout)
        except ErroOperacao:
            saida = self._saida_parcial
            self._aguardar_ociosa()
        self._monitorar(comando, saida, alteracao=False)
        return saida

    def _aguardar_ociosa(self, espera_max=15, silencio=1.0):
        """Aguarda a CLI terminar o processamento, sem enviar nada."""
        fim = time.monotonic() + espera_max
        ultima = time.monotonic()
        partes = []
        while time.monotonic() < fim:
            disponivel, _, _ = select.select([self.sessao], [], [], 0.2)
            if not disponivel:
                if time.monotonic() - ultima >= silencio:
                    break
                continue
            dados = self.sessao.recv(4096)
            if not dados:
                break
            partes.append(dados.decode("utf-8", "replace"))
            ultima = time.monotonic()
        return "".join(partes)

    def executar(self, comando):
        return self._enviar_ler(comando, alteracao=False)

    def _enviar_ler(self, comando, alteracao):
        """Envia um comando e devolve a resposta crua da CLI."""
        if not self.conectada:
            raise ErroOperacao("sessão SSH indisponível")
        comando = comando.strip()
        if not comando:
            return ""
        self._enviar_comando(comando)
        saida = self._ler_ate(self._prompt_no_buffer, self.timeout)
        self._monitorar(comando, saida, alteracao)
        return saida

    def _enviar(self, texto):
        """Envia o texto à CLI caractere a caractere.

        Algumas CLIs da VRP descartam caracteres quando o texto chega rápido
        demais (os espaços somem do comando enviado).
        """
        atraso = self.atraso_tecla
        for caractere in texto:
            self.sessao.send(caractere)
            if atraso:
                time.sleep(atraso)

    def _numero(self, chave, padrao, tipo):
        """Número lido da configuração; o padrão vale quando é inválido."""
        try:
            return tipo(self.configuracao.get(chave) or padrao)
        except (TypeError, ValueError):
            return padrao

    @property
    def largura(self):
        """Largura do terminal (evita que a OLT quebre as linhas longas)."""
        return self._numero("largura", 512, int)

    @property
    def altura(self):
        """Altura do terminal reportada à OLT."""
        return self._numero("altura", 1000, int)

    @property
    def atraso_tecla(self):
        """Pausa entre caracteres enviados (evita perda de caracteres)."""
        return self._numero("atraso_tecla", 0.01, float)

    def _ler_ate(self, predicado, timeout):
        """Lê a saída até o predicado ser satisfeito (ou o timeout)."""
        inicio = time.monotonic()
        texto = ""
        while True:
            disponivel, _, _ = select.select([self.sessao], [], [], 1)
            if not disponivel:
                if predicado(texto):
                    break
                if time.monotonic() - inicio > timeout:
                    self._saida_parcial = texto
                    raise ErroOperacao("timeout na execução do comando")
                continue
            dados = self.sessao.recv(4096)
            if not dados:
                break
            texto += dados.decode("utf-8", "replace")
            if predicado(texto):
                # Coleta o que ainda estiver chegando: sem isso a resposta
                # seguinte seria lida no comando seguinte (desalinhamento).
                texto += self._drenar()
                break
        return texto

    def _drenar(self, espera=0.4):
        """Recolhe dados que ainda estejam chegando após o prompt."""
        fim = time.monotonic() + espera
        partes = []
        while time.monotonic() < fim:
            disponivel, _, _ = select.select([self.sessao], [], [], 0.05)
            if not disponivel:
                continue
            dados = self.sessao.recv(4096)
            if not dados:
                break
            partes.append(dados.decode("utf-8", "replace"))
        return "".join(partes)

    @property
    def timeout(self):
        return float(os.environ.get("OLT_TIMEOUT_COMANDO", "120"))

    def _prompt_no_buffer(self, saida):
        if not saida or not saida.strip():
            return False
        ultima = saida.rstrip().splitlines()[-1].strip()
        return bool(re.search(r"[#>]\s*$", ultima))

    def fechar(self):
        """Sai da visão de configuração, restaura a paginação e encerra."""
        if not self.conectada:
            return
        self._encerrar_sem_erro("quit")
        if self._undo_paginacao and \
                self._encerrar_sem_erro(self._undo_paginacao):
            logger.info("paginação da CLI restaurada (%s)",
                        self._undo_paginacao)
        self._fechar_canal()
        self.conectada = False

    def _encerrar_sem_erro(self, comando):
        """Envia um comando de despedida; falhas não interrompem o fecho."""
        try:
            self._tentar_comando(comando, timeout=5)
            return True
        except Exception:
            logger.exception("falha ao enviar %r ao encerrar", comando)
            return False

    def _fechar_canal(self):
        """Fecha o canal SSH; falhas aqui não interrompem o programa."""
        try:
            self.sessao.close()
        except Exception:
            logger.exception("falha ao encerrar a sessão SSH")


def conectar_ssh(configuracao, monitor=None):
    sessao = SessaoSSH(configuracao, monitor=monitor)
    sessao.conectar()
    return sessao


class Simulador(SessaoSimulada):
    """Substitui a conexão SSH para o modo `--mock` e para os testes.

    Herda da sessão simulada: registra todos os comandos enviados, aceita o
    monitor do modo debug e não toca em equipamentos reais.
    """


def _comando_desconhecido(saida):
    """Indica se a OLT recusou o comando enviado."""
    texto = (saida or "").lower()
    return any(marca in texto for marca in _MARCAS_DE_COMANDO_DESCONHECIDO)
