"""Executa a simulação com três ONTs ilustrativos, um de cada padrão de CPE.

Uso:
    python3 exemplo_simulacao.py

Imprime o que seria reprovisionado e gera um relatório CSV, sem conexão com
a OLT. As respostas simuladas usam o formato real da MA5800 (V100R021).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import olt as script


def _resumo_porta(onts):
    """Monta a saída de `display ont info summary <fsp>`."""
    linhas = [
        "  In port 0/1/%d, the total of ONTs are: %d, online: %d"
        % (onts[0]["porta"], len(onts), len(onts)),
        "  " + "-" * 78,
        "  ONT  Run     Last                Last                Last",
        "  ID   State   UpTime              DownTime            DownCause",
        "  " + "-" * 78,
    ]
    for ont in onts:
        linhas.append(
            "  %-4d %-7s 2026-10-16 10:51:06 2026-10-16 10:47:01 dying-gasp"
            % (ont["ont_id"], "online"))
    linhas.append("  " + "-" * 78)
    linhas.append(
        "  ONT        SN        Type          Distance Rx/Tx power  Description")
    linhas.append("  ID                                    (m)      (dBm)")
    linhas.append("  " + "-" * 78)
    for ont in onts:
        linhas.append(
            "  %-4d %-11s %-15s %-6s %-12s %s"
            % (ont["ont_id"], ont["serial"], ont["tipo"], "827",
               "-20.55/2.30", ont["descricao"]))
    linhas.append("  " + "-" * 78)
    return "\n".join(linhas) + "\n"


RESPOSTA_PORTA_4 = _resumo_porta([
    {"porta": 4, "ont_id": 42, "serial": "HWTC0D3BDDA7", "tipo": "HG8145V5",
     "descricao": "joao"},
])

RESPOSTA_PORTA_5 = _resumo_porta([
    {"porta": 5, "ont_id": 43, "serial": "HWTC0D3BDDA9", "tipo": "EG8145V5-V2",
     "descricao": "maria"},
])

RESPOSTA_PORTA_6 = _resumo_porta([
    {"porta": 6, "ont_id": 44, "serial": "HWTC0D3BDDA5", "tipo": "HG8245W5-6T",
     "descricao": "sema_identificador"},
])


def _wan_info(ont_id, fsp):
    return """  F/S/P                      : %s
  ONT ID                     : %d
  srvprofile: 100
  tr069profile: 1
  wanprofile: 10
  Manage VLAN                : 2551
  Manage priority            : 5
  MAC address                : AC8D-34D9-BD67
  Service type               : Tr069, Internet
  IPv4 Connection status     : Connected
""" % (fsp, ont_id)


def _ipconfig(usuario, mac):
    return """  PPPoE username : %s
  ONT config type : PPPoE
  MAC address : %s
  Manage VLAN : 2551
  Manage priority : 5
""" % (usuario, mac)


RESPOSTA_CONFIG_ATUAL = """ ont-srvprofile gpon profile-id 100 profile-name "SRV-PADRAO"
 ont-lineprofile gpon profile-id 10 profile-name "LINE-PADRAO"
 ont add 4 42 sn-auth "HWTC0D3BDDA7" omci ont-lineprofile-id 10 ont-srvprofile-id 100 desc "joao"
 ont ipconfig 4 42 pppoe vlan 2551 priority 5 user-account username "joao" password "********"
 ont port native-vlan 4 42 eth 1 vlan 2551 priority 0
 ont tr069-server-config 4 42 profile-id 1
 ont internet-config 4 42 ip-index 0
 ont wan-config 4 42 ip-index 0 profile-id 10
 ont add 5 43 sn-auth "HWTC0D3BDDA9" omci ont-lineprofile-id 10 ont-srvprofile-id 100 desc "maria"
 ont tr069-server-config 5 43 profile-id 1
 ont internet-config 5 43 ip-index 0
 ont wan-config 5 43 ip-index 0 profile-id 10
"""


RESPOSTA_PERFIL_ONT = """  ONT-ID    Line profile name                 Service profile name
  -----------------------------------------------------------------------------
      42    LINE-PADRAO                       SRV-PADRAO
"""


RESPOSTA_PERFIL_ONT_43 = """  ONT-ID    Line profile name                 Service profile name
  -----------------------------------------------------------------------------
      43    LINE-PADRAO                       SRV-PADRAO
"""


RESPOSTA_SERVICE_PORT = (
    "   INDEX VLAN VLAN     PORT F/ S/ P VPI  VCI   FLOW  FLOW       RX   TX"
    "   STATE\n"
    "         ID   ATTR     TYPE                    TYPE  PARA\n"
    "  ------------------------------------------------------------------------"
    "-----\n"
    "     796 2551 common   gpon 0/1 /4  42   37    vlan  2551       -    -"
    "    up\n"
    "     797 2551 common   gpon 0/1 /5  43   38    vlan  2551       -    -"
    "    up\n"
    "     798 2551 common   gpon 0/1 /6  44   39    vlan  2551       -    -"
    "    up\n"
    "  ------------------------------------------------------------------------"
    "-----\n"
)


def criar_transporte():
    transporte = script.SessaoSimulada()
    transporte.definir_resposta(
        "display ont info summary 0/1/4", RESPOSTA_PORTA_4)
    transporte.definir_resposta(
        "display ont info summary 0/1/5", RESPOSTA_PORTA_5)
    transporte.definir_resposta(
        "display ont info summary 0/1/6", RESPOSTA_PORTA_6)
    transporte.definir_resposta(
        "display ont wan-info 0/1 4 42", _wan_info(42, "0/1/4"))
    transporte.definir_resposta(
        "display ont ipconfig 4 42", _ipconfig("joao", "00:11:22:33:44:55"))
    transporte.definir_resposta(
        "display ont wan-info 0/1 5 43", _wan_info(43, "0/1/5"))
    transporte.definir_resposta(
        "display ont ipconfig 5 43", _ipconfig("maria", "00:11:22:33:44:66"))
    transporte.definir_resposta(
        "display ont wan-info 0/1 6 44", _wan_info(44, "0/1/6"))
    transporte.definir_resposta(
        "display ont ipconfig 6 44", _ipconfig("", "00:11:22:33:44:77"))
    transporte.definir_resposta(
        "display service-port all", RESPOSTA_SERVICE_PORT)
    transporte.definir_resposta(
        "display current-configuration | include ont", RESPOSTA_CONFIG_ATUAL)
    transporte.definir_resposta(
        "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
    transporte.definir_resposta(
        "display ont profile 0 1 5 43", RESPOSTA_PERFIL_ONT_43)
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
        "tr069profile": "1",
        "wanprofile": "10",
        "prioridade": "5",
        "portas": "0-15",
        "maximo": 10,
    }
    registros = [
        {
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "serial": "HWTC0D3BDDA7",
            "estado": "VALIDO",
        },
        {
            "usuario": "maria",
            "senha": "1234",
            "mac": "00:11:22:33:44:66",
            "serial": "HWTC0D3BDDA9",
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
        if resultado.get("motivo"):
            print("      motivo: %s" % resultado["motivo"])
        for comando in script.exibir_comandos(resultado.get("passos", [])):
            print("  %s" % comando)
    print("simulação concluída")


if __name__ == "__main__":
    main()
