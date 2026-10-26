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
                      resolver_perfis, vlan_dominante)
from .erros import ErroOperacao
from .estados import (DESCARTA_POR_SERIAL, ESTADO_INCONCLUSIVO,
                      ESTADO_SUCESSO, IGNORADA_SEM_IDENTIFICADORES,
                      JA_CONFIGURADA, JA_CONCLUIDA, PADRAO_1_COMPLETA, PADRAO_2_PPPOE,
                      PADRAO_2_SERIAL)
from .planilha import _CHAVE_SENHA, _CHAVE_USUARIO
from .util import agora_iso
from .relatorio import (ARQUIVO_REGISTRO, carregar_registro,
                       chave_ont, salvar_registro)

logger = logging.getLogger("reprovisionar_olt")

# As três classes que entram no fluxo de reprovisionamento. As demais
# (padrão 3, divergência, ambiguidade e ONTs já configuradas) apenas
# constam no resultado.
_REPROVISIONAVEIS = (PADRAO_1_COMPLETA, PADRAO_2_SERIAL, PADRAO_2_PPPOE)


def _resultado(classe, motivo=""):
    """Resultado de uma ONT: a classe alcançada e o motivo que a explica."""
    return {"classe": classe, "motivo": motivo}


def _ligado(valor):
    """Interpreta um valor do config.ini como sim: 1, sim, yes ou true."""
    return str(valor or "").strip().lower() in ("1", "sim", "yes", "true")


class ControleExecucao:
    """Limites da execução: quantidade de ONTs, intervalos e tempo total."""

    def __init__(self, maximo, atraso, pausa_apos, tempo_maximo):
        self.maximo = maximo
        self.atraso = atraso
        self.pausa_apos = pausa_apos
        self.tempo_maximo = tempo_maximo
        self.em_execucao = False
        self.falhas = 0
        self.inicio = None
        self.processados = 0

    def iniciar(self):
        self.em_execucao = True
        self.falhas = 0
        self.inicio = time.monotonic()
        self.processados = 0

    def aguardar(self):
        """Espera o intervalo configurado antes da próxima ONT."""
        if self.em_execucao and self.atraso:
            time.sleep(self.atraso)

    def registrar_sucesso(self):
        """Um comando aceito zera a contagem de falhas consecutivas."""
        self.falhas = 0

    def registrar_falha(self):
        """Conta a falha; três seguidas pedem atenção do operador."""
        self.falhas += 1
        if self.falhas >= 3:
            logger.error("%d falhas consecutivas: verifique a OLT antes de "
                         "continuar", self.falhas)

    def registrar_pausa(self):
        """Pausa de 30 s a cada N ONTs, para não saturar a OLT."""
        if self.pausa_apos and self.processados % self.pausa_apos == 0:
            logger.info("pausa automática")
            time.sleep(30)

    def limite_atingido(self):
        """Indica que o limite de ONTs da execução já foi alcançado."""
        return self.processados >= self.maximo

    def verificar_tempo(self):
        """Interrompe quando o tempo máximo configurado se esgotou."""
        if not self.tempo_maximo or not self.inicio:
            return
        if time.monotonic() - self.inicio > self.tempo_maximo:
            raise ErroOperacao("tempo máximo de execução excedido")


class Orquestrador:
    """Coordena a leitura, a classificação e a execução de uma única OLT."""

    def __init__(self, configuracao, sessao, executar_alteracoes,
                caminho_registro=ARQUIVO_REGISTRO, seletor=None):
        self.configuracao = configuracao
        self.sessao = sessao
        self.executar_alteracoes = executar_alteracoes
        # `--limit` explícito: a mesma contagem que limita a execução também
        # corta a classificação. Sem o argumento, o card é lido por inteiro.
        self.maximo_classificacao = configuracao.get("maximo_classificacao")
        self._prontas = 0
        self.controle = None
        self.resultados = {}
        self.processados = set()
        self._config_atual = None
        self._lista_servicos = None
        self.caminho_registro = caminho_registro
        self._vlan_olt = None
        self.registro = carregar_registro(caminho_registro)
        self.concluidos = self._concluidos_da_campanha()
        self.seletor = seletor or {}

    def executar(self, registros, indice_por_serial, indice_por_usuario):
        self.processados = set()
        self._prontas = 0
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
            if self._limite_da_classificacao_atingido(card):
                break
            if self._triagem(dados, indice_por_serial, indice_por_usuario):
                continue
            resultado = self._avaliar_ont(dados, indice_por_serial,
                                          indice_por_usuario, executaveis)
            self.resultados[chave_ont(dados)] = resultado
            if resultado["classe"] in _REPROVISIONAVEIS:
                self._prontas += 1
        return self._confirmar_card(card, executaveis)

    def _limite_da_classificacao_atingido(self, card):
        """Indica que o limite de ONTs já foi achado e a leitura pode parar.

        O corte existe só quando `--limit` foi passado na linha de comando:
        sem ele o card é classificado inteiro, como sempre. As ONTs que
        ficaram de fora nem são consultadas, o que economiza centenas de
        comandos e encurta a sessão com a OLT — que penaliza login.
        """
        if self.maximo_classificacao is None:
            return False
        if self._prontas < self.maximo_classificacao:
            return False
        print("card %s: limite de %d ONTs atingido; as demais não foram "
              "classificadas" % (card, self.maximo_classificacao))
        return True

    def _triagem(self, dados, indice_por_serial, indice_por_usuario):
        """Triagem sem consulta à OLT; devolve True quando a ONT fica fora.

        Registrada como SUCESSO em campanha anterior, serial fora do
        perfil permitido ou sem correspondência na planilha. Em todos os
        casos o motivo fica registrado, para não parecer que a ONT
        simplesmente sumiu.
        """
        identificador = chave_ont(dados)
        if identificador in self.concluidos:
            anterior = self.registro.get(identificador) or {}
            self.resultados[identificador] = _resultado(
                JA_CONCLUIDA,
                "reprovisionada em %s (registro); use --reexecutar para "
                "repetir" % (anterior.get("data") or "data desconhecida"))
            return True
        if not self._serial_permitido(dados.get("serial")):
            # A OLT só conversa com CPEs suportadas (Huawei); as demais
            # são descartadas e o fluxo segue para a próxima ONT.
            self.resultados[identificador] = _resultado(
                DESCARTA_POR_SERIAL,
                "serial fora do perfil permitido: %s"
                % (dados.get("serial") or "(não informado)"))
            return True
        if self._sem_correspondencia(dados, indice_por_serial,
                                     indice_por_usuario):
            # Sem serial nem descrição na planilha, a ONT não entra no
            # fluxo: as consultas de leitura são caras e não iam mudar o
            # resultado.
            self.resultados[identificador] = _resultado(
                IGNORADA_SEM_IDENTIFICADORES,
                "sem correspondência com a planilha")
            return True
        return False

    def _avaliar_ont(self, dados, indice_por_serial, indice_por_usuario,
                     executaveis):
        """Lê o estado da ONT, classifica e monta os passos, quando couber.

        A ONT entra em `executaveis` — a lista que leva à confirmação do
        card — assim que é classificada num dos padrões reprovisionáveis,
        mesmo que a montagem dos passos venha a falhar depois. O resultado
        é devolvido para o chamador registrar e contar.
        """
        try:
            estado_atual = capturar_estado(
                self.sessao, dados["interface"], dados["porta"],
                dados["ont_id"], self._configuracao_atual(),
                self._listar_servicos(), self._vlan_da_olt())
        except ErroOperacao as erro:
            # Uma ONT ilegível não interrompe as demais do card.
            return _resultado(
                ESTADO_INCONCLUSIVO, "estado não identificado: %s" % erro)
        # Os perfis são resolvidos só para as ONTs que entraram no fluxo:
        # a consulta `display ont profile` é desnecessária para as demais.
        resolver_perfis(
            self.sessao, dados["interface"], dados["porta"],
            dados["ont_id"], estado_atual, self._configuracao_atual())
        self._absorver_estado(dados, estado_atual)
        resultado = self._classificar(
            dados, estado_atual, indice_por_serial, indice_por_usuario)
        if "registro" in resultado:
            dados["usuario"] = resultado["registro"][_CHAVE_USUARIO]
            dados["senha"] = resultado["registro"][_CHAVE_SENHA]
        if resultado["classe"] in _REPROVISIONAVEIS:
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
        return resultado

    def _absorver_estado(self, dados, estado_atual):
        """Copia para a ONT os campos que só a leitura descobre."""
        for campo in ("vlane", "prioridade", "srvprofile",
                      "lineprofile_atual", "gemport", "user_vlan",
                      "service_vlan", "service_port"):
            if campo in estado_atual:
                dados[campo] = estado_atual[campo]

    def _confirmar_card(self, card, executaveis):
        """Pede a confirmação do card quando há ONTs a reprovisionar."""
        if not executaveis:
            return True
        if not self.executar_alteracoes:
            logger.info("simulação: card %s teria %d reprovisionamentos",
                        card, len(executaveis))
            return True
        print("card %s: %d ONTs seriam reprovisionados"
              % (card, len(executaveis)))
        return interacao.confirmar("Executar alterações neste card?")

    def _listar_servicos(self):
        """Listagem de service-ports da OLT, lida uma vez por execução."""
        if self._lista_servicos is None:
            try:
                self._lista_servicos = listar_servicos(self.sessao)
            except ErroOperacao as erro:
                logger.error("leitura dos service-ports falhou: %s", erro)
                self._lista_servicos = []
        return self._lista_servicos

    def _vlan_da_olt(self):
        """VLAN mais usada na OLT, lida uma única vez por execução.

        É a mesma para todas as ONTs; quando não pode ser lida, o fluxo
        continua com a VLAN descoberta em cada ONT, como antes.
        """
        if self._vlan_olt is None:
            try:
                self._vlan_olt = vlan_dominante(self.sessao)
            except ErroOperacao as erro:
                logger.warning(
                    "%s: usando a VLAN de cada ONT", erro)
                self._vlan_olt = ""
        return self._vlan_olt

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
        return [self._dados_da_ont(ont)
                for ont in self._filtrar(onts.values())]

    def _portas(self):
        """Portas GPON a consultar; configurável, com todas como padrão."""
        return portas_configuradas(self.configuracao)

    def _filtrar(self, onts):
        """Mantém só as ONTs pedidas com `--ont-id` e `--serial`."""
        alvo_id = self.seletor.get("ont_id")
        alvo_serial = self.seletor.get("serial")
        selecionadas = []
        for ont in onts:
            if alvo_id is not None and ont["ont_id"] != alvo_id:
                continue
            if alvo_serial and ont.get("Serial", "") != alvo_serial:
                continue
            selecionadas.append(ont)
        return selecionadas

    def _classificar(self, dados, estado_atual, indice_por_serial,
                     indice_por_usuario):
        ont_atual = {
            "serial": dados.get("serial") or "",
            "usuario": estado_atual.get("usuario") or
            dados.get("descricao") or "",
        }
        return classificar(ont_atual, indice_por_serial, indice_por_usuario)

    def _processar(self, dados, indice_por_serial, indice_por_usuario):
        """Executa (ou só simula) o passo de uma ONT já classificada."""
        identificador = chave_ont(dados)
        if identificador in self.concluidos or \
                identificador in self.processados:
            return
        if self._classe(identificador) not in _REPROVISIONAVEIS:
            # A ONT não tem correspondência com a planilha (padrão 3,
            # divergência ou ambiguidade): não há o que reprovisionar.
            return
        if self.controle.limite_atingido():
            return
        self.controle.verificar_tempo()
        self.controle.aguardar()
        self.controle.processados += 1
        self.controle.registrar_pausa()
        self.processados.add(identificador)
        try:
            if self.executar_alteracoes:
                self._executar(dados)
        except ErroOperacao as erro:
            self.controle.registrar_falha()
            self.resultados[identificador] = _resultado(
                ESTADO_INCONCLUSIVO, "falha na execução: %s" % erro)

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
        return _ligado(self.configuracao.get("filtrar_por_planilha") or "sim")

    def _concluidos_da_campanha(self):
        """ONTs registradas como SUCESSO numa campanha anterior.

        Com `--reexecutar` o registro é ignorado e elas entram de novo.
        """
        if self._reexecutar():
            return set()
        return {identificador
                for identificador, info in self.registro.items()
                if info.get("classe") == ESTADO_SUCESSO}

    def _reexecutar(self):
        """Indica se o operador pediu para repetir ONTs já concluídas."""
        return _ligado(self.configuracao.get("reexecutar"))

    def _ja_tem_o_perfil_desejado(self, estado_atual):
        """Indica se a ONT já usa o line-profile configurado.

        Reprovisionar de novo uma ONT já configurada só geraria retrabalho
        (e um breve período sem serviço) na CPE do cliente.
        """
        if _ligado(self.configuracao.get("reprovisionar_ja_configuradas")):
            return False
        atual = estado_atual.get("lineprofile_atual")
        if not atual:
            return False
        return str(atual) == str(self.configuracao["lineprofile"])

    def _classe(self, identificador):
        """Classe atribuída à ONT na fase de classificação."""
        return self.resultados.get(identificador, {}).get("classe")

    def _executar(self, dados):
        """Aplica a sequência; a ONT só é SUCESSO se a OLT aceitar tudo."""
        configuracao = self._montar_configuracao(dados)
        # O estado da ONT foi lido na triagem; aqui só interessa o
        # identificador do service-port, que vem da listagem em cache.
        service_port = self._service_port_id(
            dados["interface"], dados["porta"], dados["ont_id"])
        identificador = chave_ont(dados)
        if not service_port:
            self.resultados[identificador] = _resultado(
                ESTADO_INCONCLUSIVO, "service-port original não identificado")
            self._salvar()
            return
        passos = montar_passos(dados["interface"], configuracao,
                               {"service_port": service_port})
        self.resultados[identificador]["passos"] = passos
        for comando in passos:
            self.sessao.alterar(comando)
            self.controle.registrar_sucesso()
        # Chegar ao fim da sequência é o critério de sucesso: um comando
        # recusado pela OLT interrompe a execução e a ONT fica
        # INCONCLUSIVA, com o motivo do erro.
        self.resultados[identificador] = _resultado(ESTADO_SUCESSO)
        self.registro[identificador] = {
            "classe": ESTADO_SUCESSO,
            "motivo": "",
            "data": agora_iso(),
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
            "prioridade": self.configuracao.get("prioridade"),
            "srvprofile": dados["srvprofile"],
            "tr069profile": self.configuracao.get("tr069profile"),
            "wanprofile": self.configuracao.get("wanprofile"),
            "usuario": dados["usuario"],
            "senha": dados["senha"],
            "gemport": dados["gemport"],
            "user_vlan": dados.get("user_vlan") or dados["vlane"],
            "service_vlan": dados.get("service_vlan") or dados["vlane"],
        }
        for campo in ("vlan", "srvprofile", "gemport",
                      "user_vlan", "service_vlan"):
            if not configuracao[campo]:
                raise ErroOperacao(
                    "valor atual não identificado: %s" % campo)
        for campo in ("tr069profile", "wanprofile"):
            if not configuracao[campo]:
                raise ErroOperacao(
                    "perfil não configurado: %s" % campo)
        if not configuracao["prioridade"]:
            raise ErroOperacao(
                "prioridade não configurada no config.ini")
        return configuracao

    def _montar_passos(self, dados):
        configuracao = self._montar_configuracao(dados)
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
        return servico["id"] if servico else None

    def _dados_da_ont(self, atual):
        """Converte uma linha da listagem nos dados de trabalho da ONT."""
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
        }


