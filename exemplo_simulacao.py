"""Executa a simulação com três ONTs ilustrativos, um de cada padrão de CPE.

Uso:
    python3 exemplo_simulacao.py

Imprime o que seria reprovisionado e gera um relatório CSV, sem conexão com
a OLT.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reprovisionar_olt as script


RESPOSTA_LISTA_ONTS = """ONT ID : 42
  F/S/P : 0/1/4
  Serial : HWTC0001
ONT ID : 43
  F/S/P : 0/1/5
  Serial : HWTC0002
ONT ID : 44
  F/S/P : 0/1/6
  Serial : HWTC0009
"""

RESPOSTA_WAN_INFO = """  F/S/P : 0/1/4
  ONT ID : 42
  tr069profile: 1
  wanprofile: 10
  srvprofile: 100
  Service type : Tr069, Internet
  IPv4 Connection status : Connected
"""


RESPOSTA_WAN_INFO_43 = """  F/S/P : 0/1/5
  ONT ID : 43
  tr069profile: 1
  wanprofile: 10
  srvprofile: 100
  Service type : Tr069, Internet
  IPv4 Connection status : Connected
"""


RESPOSTA_WAN_INFO_44 = """  F/S/P : 0/1/6
  ONT ID : 44
  tr069profile: 1
  wanprofile: 10
  srvprofile: 100
  Service type : Tr069, Internet
  IPv4 Connection status : Connected
"""

RESPOSTA_IPCONFIG = """  PPPoE username : joao
  ONT MAC : 00:11:22:33:44:55
  ONT manage VLAN : 2551
  ONT manage priority : 5
"""


RESPOSTA_IPCONFIG_43 = """  PPPoE username :
  ONT MAC : 00:11:22:33:44:56
  ONT manage VLAN : 2551
  ONT manage priority : 5
"""


RESPOSTA_IPCONFIG_44 = """  PPPoE username :
  ONT MAC : 00:11:22:33:44:57
  ONT manage VLAN : 2551
  ONT manage priority : 5
"""


RESPOSTA_SERVICE_PORT = (
    "Service-port 796 vlan 2551 gpon 0/1/4 ont 42 gemport 37 "
    "multi-service user-vlan 2551 tag-transform translate\n"
    "Service-port 797 vlan 2551 gpon 0/1/5 ont 43 gemport 38 "
    "multi-service user-vlan 2551 tag-transform translate\n"
    "Service-port 798 vlan 2551 gpon 0/1/6 ont 44 gemport 39 "
    "multi-service user-vlan 2551 tag-transform translate"
)


def criar_transporte():
    transporte = script.SessaoSimulada()
    transporte.definir_resposta("display ont", RESPOSTA_LISTA_ONTS)
    transporte.definir_resposta(
        "display ont wan-info 0/1 4 42", RESPOSTA_WAN_INFO)
    transporte.definir_resposta(
        "display ont ipconfig 4 42", RESPOSTA_IPCONFIG)
    transporte.definir_resposta(
        "display ont wan-info 0/1 5 43", RESPOSTA_WAN_INFO_43)
    transporte.definir_resposta(
        "display ont ipconfig 5 43", RESPOSTA_IPCONFIG_43)
    transporte.definir_resposta(
        "display ont wan-info 0/1 6 44", RESPOSTA_WAN_INFO_44)
    transporte.definir_resposta(
        "display ont ipconfig 6 44", RESPOSTA_IPCONFIG_44)
    transporte.definir_resposta("display service-port", RESPOSTA_SERVICE_PORT)
    return transporte


def main():
    configuracao = {
        "host": "127.0.0.1",
        "porta": "22",
        "usuario": "",
        "senha": "",
        "chave": "",
        "interface": "0/1",
        "lineprofile": "11",
        "srvprofile": "100",
        "tr069profile": "1",
        "wanprofile": "10",
        "vlan": "2551",
        "prioridade": "5",
        "gemport": "37",
        "maximo": 10,
    }
    registros = [
        {
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "serial": "HWTC0001",
            "estado": "VALIDO",
        },
        {
            "usuario": "maria",
            "senha": "1234",
            "mac": "00:11:22:33:44:66",
            "serial": "HWTC0002",
            "estado": "VALIDO",
        },
    ]
    indice_por_serial, indice_por_usuario = script.indexar(registros)
    transporte = criar_transporte()
    orquestrador = script.Orquestrador(
        configuracao=configuracao,
        sessao=transporte,
        executar_alteracoes=False,
    )
    resultados = orquestrador.executar(registros, indice_por_serial,
                                     indice_por_usuario)
    for identificador, resultado in resultados.items():
        print("%s -> %s" % (identificador, resultado["classe"]))
        for comando in script.exibir_comandos(resultado.get("passos", [])):
            print("  %s" % comando)
    print("simulação concluída")


if __name__ == "__main__":
    main()
