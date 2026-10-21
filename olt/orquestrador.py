"""Orquestração do fluxo de reprovisionamento.

O orquestrador lista as ONTs, agrupa por card, pede a confirmação de cada
card e processa uma ONT por vez. No DRY-RUN nada é alterado: os comandos
são apenas montados e mostrados.
"""

import logging
import time

from . import interacao
from .classificacao import classificar
from .comandos import montar_passos
from .configuracao import PREFIXOS_SERIAL_PADRAO, portas_configuradas
from .descoberta import (_consultar_configuracao_atual, _servico_da_ont,
                      capturar_estado, listar_onts, listar_servicos,
                      resolver_perfis, validar_estado_final)
from .erros import ErroOperacao
from .estados import (DESCARTA_POR_SERIAL, ESTADO_INCONCLUSIVO,
                      ESTADO_SUCESSO, IGNORADA_SEM_IDENTIFICADORES,
                      JA_CONFIGURADA, PADRAO_1_COMPLETA, PADRAO_2_PPPOE,
                      PADRAO_2_SERIAL)
from .planilha import _CHAVE_SENHA, _CHAVE_USUARIO
from .util import agora_iso
from .relatorio import (ARQUIVO_REGISTRO, carregar_registro,
                       chave_ont, salvar_registro)

logger = logging.getLogger("reprovisionar_olt")


def _resumo_estado(estado):
    """Campos do estado que ajudam a entender o resultado da execução."""
    campos = ("usuario", "vlane", "prioridade", "gemport", "user_vlan",
              "service_port", "tipo_wan", "status_conexao")
    return {campo: estado.get(campo) for campo in campos
            if estado.get(campo)}


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
            logger.error("execução suspensa após %d falhas consecutivas",
                     self.falhas)

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
        self._lista_servicos = None
        self.caminho_registro = caminho_registro
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
        salvar_registro(self.caminho_registro, self.registro)
        return self.resultados

    def _agrupar(self, onts):
        """Agrupa as ONTs por card, para pedir a confirmação por card."""
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
            if not self._serial_permitido(dados.get("serial")):
                # A OLT só conversa com CPEs suportadas (Huawei): as demais
                # são descartadas e o fluxo segue para a próxima ONT.
                self.resultados[identificador] = {
                    "classe": DESCARTA_POR_SERIAL,
                    "motivo": "serial fora do perfil permitido: %s"
                              % (dados.get("serial") or "(não informado)"),
                }
                continue
            if self._sem_correspondencia(dados, indice_por_serial,
                                         indice_por_usuario):
                # Sem serial nem descrição na planilha, a ONT não entra no
                # fluxo: as consultas de leitura são caras e não iam mudar
                # o resultado.
                self.resultados[identificador] = {
                    "classe": IGNORADA_SEM_IDENTIFICADORES,
                    "motivo": "sem correspondência com a planilha",
                }
                continue
            try:
                estado_atual = capturar_estado(
                    self.sessao, dados["interface"], dados["porta"],
                    dados["ont_id"], self._configuracao_atual(),
                    self._listar_servicos())
            except ErroOperacao as erro:
                # Uma ONT ilegível não interrompe as demais do card.
                self.resultados[identificador] = {
                    "classe": ESTADO_INCONCLUSIVO,
                    "motivo": "estado não identificado: %s" % erro,
                }
                continue
            # Os perfis são resolvidos só para as ONTs que entraram no
            # fluxo: a consulta `display ont profile` é desnecessária para
            # as demais.
            resolver_perfis(
                self.sessao, dados["interface"], dados["porta"],
                dados["ont_id"], estado_atual, self._configuracao_atual())
            for campo in ("vlane", "prioridade", "srvprofile",
                         "lineprofile_atual", "gemport", "user_vlan",
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
                if self._ja_tem_o_perfil_desejado(estado_atual):
                    # A ONT já está com o line-profile pedido: reprovisionar
                    # de novo só geraria retrabalho e indisponibilidade.
                    resultado["classe"] = JA_CONFIGURADA
                    resultado["motivo"] = (
                        "line-profile %s já aplicado"
                        % self.configuracao["lineprofile"])
                else:
                    try:
                        resultado["passos"] = self._montar_passos(dados)
                    except ErroOperacao as erro:
                        resultado["classe"] = ESTADO_INCONCLUSIVO
                        resultado["motivo"] = "falha na execução: %s" % erro
                    executaveis.append(dados)
            self.resultados[identificador] = resultado
        if not executaveis:
            return True
        if not self.executar_alteracoes:
            logger.info(
                "simulação: card %s teria %d reprovisionamentos",
                card, len(executaveis))
            return True
        print("card %s: %d ONTs seriam reprovisionados"
              % (card, len(executaveis)))
        if not interacao.confirmar("Executar alterações neste card?"):
            return False
        return True

    def _listar_servicos(self):
        """Listagem de service-ports da OLT, lida uma vez por execução."""
        if self._lista_servicos is None:
            try:
                self._lista_servicos = listar_servicos(self.sessao)
            except ErroOperacao as erro:
                logger.error("leitura dos service-ports falhou: %s", erro)
                self._lista_servicos = []
        return self._lista_servicos

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
                logger.error("leitura da configuração corrente falhou: %s",
                             erro)
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
            if "ont_id" in selecionado and \
                    ont["ont_id"] != selecionado["ont_id"]:
                continue
            if "serial" in selecionado and \
                    ont.get("Serial", "") != selecionado["serial"]:
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
        if identificador in self.concluidos or \
                identificador in self.processados:
            return
        if self._classe(identificador) not in (
                PADRAO_1_COMPLETA, PADRAO_2_SERIAL, PADRAO_2_PPPOE):
            # A ONT não tem correspondência com a planilha (padrão 3,
            # divergência ou ambiguidade): não há o que reprovisionar.
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

    def _serial_permitido(self, serial):
        """Indica se a CPE é de um equipamento suportado pela OLT.

        O prefixo do serial é configurável; a comparação ignora maiúsculas
        e minúsculas. Serial vazio também é descartado, por não ser possível
        confirmar o equipamento.
        """
        configurado = self.configuracao.get("prefixos_serial")
        if configurado is None:
            configurado = PREFIXOS_SERIAL_PADRAO
        prefixos = [item.strip().upper() for item in
                    str(configurado).split(",") if item.strip()]
        if not prefixos:
            return True
        serial = (serial or "").strip().upper()
        if not serial:
            return False
        return any(serial.startswith(prefixo) for prefixo in prefixos)

    def _sem_correspondencia(self, dados, indice_por_serial,
                            indice_por_usuario):
        """Indica que a ONT não tem correspondência na planilha.

        A triagem usa o serial e a descrição configurada na ONT, que é o
        usuário PPPoE. Ela evita as consultas de leitura das ONTs que não
        entram na campanha.
        """
        if not self._filtrar_por_planilha():
            return False
        serial = dados.get("serial") or ""
        descricao = dados.get("descricao") or ""
        if serial and indice_por_serial.get(serial):
            return False
        if descricao and indice_por_usuario.get(descricao):
            return False
        return True

    def _filtrar_por_planilha(self):
        """Indica se a triagem por planilha está ligada."""
        valor = str(self.configuracao.get("filtrar_por_planilha") or "sim")
        return valor.strip().lower() in ("1", "sim", "yes", "true")

    def _ja_tem_o_perfil_desejado(self, estado_atual):
        """Indica se a ONT já usa o line-profile configurado.

        Reprovisionar de novo uma ONT já configurada só geraria retrabalho
        (e um breve período sem serviço) na CPE do cliente.
        """
        if self.configuracao.get("reprovisionar_ja_configuradas") in (
                "1", "sim", "yes", "true"):
            return False
        atual = estado_atual.get("lineprofile_atual")
        if not atual:
            return False
        return str(atual) == str(self.configuracao["lineprofile"])

    def _classe(self, identificador):
        """Classe atribuída à ONT na fase de classificação."""
        return self.resultados.get(identificador, {}).get("classe")

    def _executar(self, dados):
        configuracao = self._montar_configuracao(dados)
        estado_atual = capturar_estado(
            self.sessao, dados["interface"], dados["porta"], dados["ont_id"],
            self._configuracao_atual(), self._listar_servicos())
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
            self.sessao.alterar(comando)
            self.controle.registrar_sucesso()
        estado_final = capturar_estado(
            self.sessao, dados["interface"], dados["porta"], dados["ont_id"],
            self._configuracao_atual(), self._listar_servicos())
        classe, motivo = validar_estado_final(configuracao, estado_final)
        identificador = chave_ont(dados)
        self.resultados[identificador] = {
            "classe": classe,
            "motivo": motivo,
            "estado": _resumo_estado(estado_final),
        }
        self.registro[identificador] = {
            "classe": classe,
            "motivo": motivo,
            "data": agora_iso(),
            "estado": _resumo_estado(estado_final),
        }
        self._salvar()

    def _salvar(self):
        salvar_registro(self.caminho_registro, self.registro)

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
            "tr069profile": self.configuracao.get("tr069profile"),
            "wanprofile": self.configuracao.get("wanprofile"),
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
        for campo in ("tr069profile", "wanprofile"):
            if not configuracao[campo]:
                raise ErroOperacao(
                    "perfil não configurado: %s" % campo)
        return configuracao

    def _montar_passos(self, dados):
        configuracao = self._montar_configuracao(dados)
        # Revalida o estado atual antes de montar os comandos.
        capturar_estado(
            self.sessao, dados["interface"], dados["porta"], dados["ont_id"],
            self._configuracao_atual(), self._listar_servicos())
        return montar_passos(
            dados["interface"], configuracao,
            {"service_port": dados.get("service_port")})

    def _service_port_id(self, interface, porta, ont_id):
        """Identificador do service-port atual da ONT, descoberto por leitura.

        O valor é usado apenas no `undo service-port`; nenhum identificador
        novo é enviado no comando de criação.
        """
        servico = _servico_da_ont(
            self._listar_servicos(), interface, porta, ont_id)
        if servico:
            return servico["id"]
        return None

    def _ja_estava_configurado(self, configuracao, estado_atual):
        """A ONT já está com o perfil pedido: não há o que reconfigurar."""
        return self._ja_tem_o_perfil_desejado(estado_atual)

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


