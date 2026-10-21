#!/usr/bin/env python3
"""Reprovisionamento de ONTs em OLTs Huawei via SSH.

Consulta as ONTs existentes na OLT, associa os registros da planilha PPPoE,
classifica os três padrões de CPE e executa o fluxo de reprovisionamento
(configuração PPPoE, OMCI, WAN e TR-069) quando autorizado.

Sem `--execute`, nenhuma alteração é enviada: apenas consultas de leitura
são feitas, com a sessão SSH aberta. As senhas nunca aparecem em logs,
relatórios ou mensagens de erro.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from olt.comandos import exibir_comandos  # noqa: E402
from olt.estados import ESTADO_SUCESSO  # noqa: E402
from olt.configuracao import (carregar_configuracao,  # noqa: E402
                              portas_configuradas)
from olt.descoberta import capturar_estado, listar_onts  # noqa: E402
from olt.erros import ErroOperacao  # noqa: E402
from olt.interacao import configurar_codificacao  # noqa: E402
from olt.orquestrador import Orquestrador  # noqa: E402
from olt.planilha import (indexar, ler_planilha,  # noqa: E402
                         resumo_planilha)
from olt.relatorio import gravar_relatorio  # noqa: E402
from olt.sessao import Simulador, conectar_ssh  # noqa: E402
from olt.util import agora_iso  # noqa: E402

logger = logging.getLogger("reprovisionar_olt")


# ---------------------------------------------------------------------------
# Saída no terminal
# ---------------------------------------------------------------------------

def _resumo_planilha(registros, problemas):
    contagem = resumo_planilha(registros, problemas)
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
    for identificador, resultado in sorted(resultados.items()):
        print("  %s -> %s" % (identificador, resultado["classe"]))
        if resultado.get("motivo"):
            print("      motivo: %s" % resultado["motivo"])
        estado = resultado.get("estado")
        if estado and resultado["classe"] != ESTADO_SUCESSO:
            print("      estado final na OLT: %s"
                  % " | ".join("%s=%s" % (chave, valor)
                               for chave, valor in estado.items()))


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
    for resultado in resultados.values():
        for comando in exibir_comandos(resultado.get("passos", [])):
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
    print("")
    for classe in sorted(contagem):
        print("  %-28s %d" % (classe, contagem[classe]))


# ---------------------------------------------------------------------------
# Modos de leitura da OLT
# ---------------------------------------------------------------------------

def _executar_diagnostico(sessao, configuracao, seletor):
    """Lê o estado de cada ONT e mostra o que foi descoberto (sem alterar)."""
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
        config_atual = _configuracao_atual(sessao)
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
        for campo in ("usuario", "vlane", "prioridade", "lineprofile_atual",
                      "srvprofile", "gemport", "user_vlan", "service_port",
                      "tipo_wan", "status_conexao"):
            valor = estado.get(campo)
            if valor in (None, ""):
                print("   %-17s: (não identificado)" % campo)
            else:
                print("   %-17s: %s" % (campo, valor))
        print("   %-17s: %s (configurado)"
              % ("line-profile", configuracao["lineprofile"]))
        print("   %-17s: %s (configurado)"
              % ("tr069-profile", configuracao["tr069profile"]))
        print("   %-17s: %s (configurado)"
              % ("wan-profile", configuracao["wanprofile"]))
        _exibir_perfis_da_olt(sessao, configuracao)


def _exibir_perfis_da_olt(sessao):
    """Mostra os perfis de TR-069 e WAN disponíveis na OLT.

    Ajuda a conferir qual identificador é o de TR-069 e qual é o de WAN: os
    números mudam de OLT para OLT.
    """
    for comando in ("display ont tr069-server-profile",
                    "display ont wan-profile"):
        try:
            saida = sessao.executar(comando)
        except ErroOperacao as erro:
            print("   %-17s: (erro: %s)" % (comando.split()[-1], erro))
            continue
        linhas = [linha.strip() for linha in saida.splitlines() if linha.strip()]
        if len(linhas) <= 3:
            print("   %-17s: %s" % (comando.split()[-1], " | ".join(linhas)))
        else:
            print("   %-17s:" % comando.split()[-1])
            for linha in linhas:
                print("      %s" % linha)


def _configuracao_atual(sessao):
    from olt.descoberta import _consultar_configuracao_atual

    return _consultar_configuracao_atual(sessao)


def _executar_teste_cli(sessao, configuracao):
    """Envia comandos inofensivos e mostra a resposta crua da CLI.

    Serve para conferir se a sessão está sincronizada e se os comandos
    realmente chegam ao equipamento.
    """
    for comando in ("display version", "display ont info summary 0/1/0"):
        try:
            saida = sessao.executar(comando)
        except ErroOperacao as erro:
            print("--- %s ---" % comando)
            print("  ERRO: %s" % erro)
            continue
        print("--- %s ---" % comando)
        for linha in saida.splitlines():
            print("  %s" % linha)
    print("--- prompt atual ---")
    print("  (o modo privilegiado aparece como '#' no fim do prompt)")


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------

def _parse_argumentos(argv):
    parser = argparse.ArgumentParser(
        description="Reprovisionamento de ONTs em OLTs Huawei.")
    parser.add_argument("--file", dest="arquivo", default="",
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
                        help="mostra o estado descoberto em cada ONT "
                             "(só leitura)")
    parser.add_argument("--roteiro", action="store_true",
                        help="mostra a transcrição completa de comandos "
                             "(leituras enviadas e alterações planejadas)")
    parser.add_argument("--versao", action="store_true",
                        help="mostra a versão e sai")
    return parser.parse_args(argv)


def _abrir_sessao(configuracao, executar, simular):
    """Abre a sessão com a OLT.

    Mesmo no DRY-RUN a OLT real é consultada; nenhum comando de alteração
    é enviado nesse modo. Com `--mock` a sessão é simulada e não há
    conexão.
    """
    if simular:
        return Simulador()
    try:
        return conectar_ssh(configuracao)
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
            print("       - se a conta exige troca de senha no "
                  "primeiro acesso")
            print("       - teste manual: ssh %s@%s"
                  % (configuracao["usuario"], configuracao["host"]))
        print("     use --mock para rodar sem OLT (dados simulados).")
        raise SystemExit(1)


def main(argv=None):
    configurar_codificacao()
    argumentos = _parse_argumentos(argv)
    if argumentos.versao:
        import olt

        print("reprovisionar_olt %s" % olt.__version__)
        return 0
    if not argumentos.arquivo:
        print("ERRO: informe a planilha com --file")
        return 1
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    configuracao = carregar_configuracao(argumentos.configuracao or None)
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

    registros, problemas = ler_planilha(argumentos.arquivo)
    print("planilha: %s" % _resumo_planilha(registros, problemas))
    _exibir_origem_configuracao(configuracao)
    indice_por_serial, indice_por_usuario = indexar(registros)
    seletor = {"ont_id": argumentos.ont_id, "serial": argumentos.serial}

    if argumentos.testar_ssh:
        sessao = _abrir_sessao(configuracao, executar, argumentos.mock)
        try:
            print("conexão SSH ok")
            _executar_teste_cli(sessao, configuracao)
        finally:
            sessao.fechar()
        print("concluído")
        return 0

    sessao = _abrir_sessao(configuracao, executar, argumentos.mock)
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
            _gravar_resultados(argumentos.relatorio, resultados, configuracao)
    finally:
        sessao.fechar()
    print("concluído")
    return 0


def _gravar_resultados(caminho, resultados, configuracao):
    """Grava o relatório CSV da operação."""
    linhas = []
    for identificador, resultado in sorted(resultados.items()):
        partes = identificador.split("/")
        linhas.append({
            "data": agora_iso(),
            "olt": configuracao["olt"],
            "fsp": "/".join(partes[:-1]),
            "ont_id": partes[-1],
            "classe": resultado["classe"],
            "estado": resultado.get("estado") and
            " | ".join("%s=%s" % (chave, valor)
                       for chave, valor in resultado["estado"].items()) or "",
            "motivo": resultado.get("motivo") or "",
            "observacoes": resultado.get("observacoes") or "",
        })
    gravar_relatorio(caminho, linhas)
    return linhas


if __name__ == "__main__":
    sys.exit(main())
