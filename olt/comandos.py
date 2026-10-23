"""Construção dos comandos enviados à OLT.

Os construtores recebem o dicionário de configuração já preenchido e
devolvem o comando pronto para envio. Nenhum valor é inventado aqui: os
parâmetros vêm do estado atual da ONT, exceto o line-profile, que é o
único configurável.
"""

from .erros import CredenciaisNaoTransmitidas, ErroOperacao
from .util import texto


def _aspas(valor):
    if '"' in valor or "\\" in valor or "\n" in valor:
        raise CredenciaisNaoTransmitidas(
            "valor não pode ser transmitido com segurança")
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
        "ont add %d %d sn-auth %s omci ont-lineprofile-id %d "
        "ont-srvprofile-id %d desc %s"
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
        "ont ipconfig %d %d pppoe vlan %d priority %d user-account "
        "username %s password %s"
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
    """Volta ao contexto de configuração raiz.

    É o que permite emitir o `service-port` logo em seguida.
    """
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
        "service-port vlan %d gpon %s ont %d gemport %d multi-service "
        "user-vlan %d tag-transform translate"
        % (
            _validar_vlan(configuracao["service_vlan"]),
            caminho_gpon,
            configuracao["ont_id"],
            int(configuracao["gemport"]),
            _validar_vlan(configuracao["user_vlan"]),
        )
    )


def montar_passos(interface, configuracao, estado_original):
    """Monta a sequência de comandos, na ordem em que deve ser executada.

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
    return passos


