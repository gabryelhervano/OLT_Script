"""Leitura do estado atual das ONTs.

Toda a configuração usada na reprovisão é descoberta aqui, por consultas
de leitura. Quando um valor não pode ser identificado, a ONT é bloqueada
em vez de receber um valor presumido.
"""

import re

from .erros import ErroOperacao


def _campos(texto_saida):
    """Converte a saída da CLI em pares chave/valor."""
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


_PALAVRAS_CONTINUACAO = (
    "ont-srvprofile-id", "ont-lineprofile-id", "desc", "priority", "vlan",
    "user-account", "gem", "tcont", "profile-id", "gemport", "user-vlan",
)


def _desembrulhar(linhas, largura=79):
    """Junta as linhas truncadas pelo terminal.

    A OLT quebra as linhas longas na largura do terminal e a continuação
    vem na linha seguinte, indentada. Só são unidas as continuações
    reconhecidas (evita juntar comandos diferentes). A sessão também
    reserva um terminal largo, o que raramente torna a quebra necessária.
    """
    resultado = []
    for linha in linhas:
        if resultado and linha[:1].isspace() and linha.strip():
            palavra = linha.strip().split()[0].lower()
            if palavra in _PALAVRAS_CONTINUACAO:
                resultado[-1] = resultado[-1].rstrip() + " " + linha.strip()
                continue
        resultado.append(linha)
    return resultado


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
    for linha in _desembrulhar(saida.splitlines()):
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


def listar_perfis(sessao):
    """Perfis de TR-069 e WAN definidos na OLT.

    Os identificadores mudam de OLT para OLT, então listar os perfis
    disponíveis ajuda a conferir qual ID usar na reconfiguração.
    """
    perfis = {"tr069": {}, "wan": {}}
    for chave, comando in (("tr069", "display ont tr069-server-profile"),
                            ("wan", "display ont wan-profile all")):
        try:
            saida = sessao.executar(comando)
        except ErroOperacao:
            continue
        for linha in saida.splitlines():
            partes = linha.split()
            if len(partes) < 2 or not partes[0].isdigit():
                continue
            perfis[chave][int(partes[0])] = partes[1]
    return perfis


def _consultar_perfil_ont(sessao, interface, porta, ont_id):
    """Lê o nome dos perfis aplicados à ONT (`display ont profile`).

    O comando usa quadro/slot/porta separados por espaço (ex.: 0 1 4 9).
    """
    partes = interface.replace("/", " ")
    saida = sessao.executar(
        "display ont profile %s %d %d" % (partes, porta, ont_id))
    for linha in saida.splitlines():
        partes_linha = linha.split()
        if len(partes_linha) < 3 or not partes_linha[0].isdigit():
            continue
        if int(partes_linha[0]) != ont_id:
            continue
        return {"linha": partes_linha[1], "srv": partes_linha[2]}
    return {}


def listar_servicos(sessao):
    """Lê a listagem de service-ports da OLT.

    A listagem `display service-port all` é uma tabela "Switch-Oriented
    Flow List": o índice é o identificador do service-port, F/S/P indica a
    porta GPON, VPI é o identificador da ONT e VCI é o gemport. É a mesma
    tabela para todas as ONTs, por isso é lida uma única vez por execução.
    """
    saida = sessao.executar("display service-port all")
    servicos = []
    for linha in saida.splitlines():
        encontrado = _PADRAO_SERVICE_PORT.match(linha)
        if not encontrado:
            continue
        valores = encontrado.groupdict()
        servicos.append({
            "id": int(valores["index"]),
            "vlan": valores["vlan"],
            "gpon": "%s/%s" % (valores["frame"], valores["porta"]),
            "ont_id": int(valores["vpi"]),
            "gemport": valores["vci"],
            "user-vlan": valores["param"],
        })
    return servicos


def _servico_da_ont(servicos, interface, porta, ont_id):
    """Service-port que atende a ONT, se houver."""
    alvo = "%s/%d" % (interface, porta)
    for servico in servicos or []:
        if servico["gpon"] == alvo and servico["ont_id"] == ont_id:
            return servico
    return None


def resolver_perfis(sessao, interface, porta, ont_id, estado, config_atual):
    """Resolve os perfis da ONT pelo nome informado pela OLT.

    O `display ont profile` só é consultado para as ONTs que realmente
    serão reprovisionadas: nas demais ele não traz informação útil.
    """
    perfis = (config_atual or {}).get("perfis", {})
    nomes = _consultar_perfil_ont(sessao, interface, porta, ont_id)
    if not estado.get("srvprofile") and nomes.get("srv"):
        estado["srvprofile"] = perfis.get("srv", {}).get(nomes["srv"])
    if not estado.get("lineprofile_atual") and nomes.get("linha"):
        estado["lineprofile_atual"] = perfis.get("linha", {}).get(
            nomes["linha"])
    return estado


def _campo(campos, *chaves):
    """Devolve o primeiro campo presente entre as chaves informadas."""
    for chave in chaves:
        valor = campos.get(chave)
        if valor not in (None, ""):
            return valor
    return None


def _mac_util(*fontes):
    """Primeiro MAC válido (ignora o 0000-0000-0000 devolvido pela ONT)."""
    for campos in fontes:
        for chave in ("MAC address", "ONT MAC"):
            valor = (campos or {}).get(chave)
            if not valor:
                continue
            digitos = valor.replace(":", "").replace("-", "")
            if digitos and set(digitos) != {"0"}:
                return valor
    return ""


def capturar_estado(sessao, interface, porta, ont_id, config_atual=None,
                    servicos=None):
    """Registra o estado atual da ONT, sem executar nenhum comando de alteração.

    Todos os demais valores são descobertos na resposta dos comandos de
    leitura; nenhum valor é assumido. Somente o lineprofile é configurável
    (definido fora desta função).
    """
    wan_info = sessao.executar(
        "display ont wan-info %s %d %d" % (interface, porta, ont_id))
    ipconfig = _consultar_ipconfig(sessao, interface, porta, ont_id)
    if servicos is None:
        servicos = listar_servicos(sessao)
    servico = _servico_da_ont(servicos, interface, porta, ont_id)
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
    # O srv-profile e o line-profile em uso vêm da linha `ont add`; quando
    # ela não pôde ser lida (linha truncada pelo terminal), o nome
    # informado pelo `display ont profile` resolve o identificador.
    srvprofile = atual.get("srvprofile")
    lineprofile_atual = atual.get("lineprofile")
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
        "lineprofile_atual": lineprofile_atual,
        "srvprofile": srvprofile,
        "gemport": (servico or {}).get("gemport"),
        "user_vlan": (servico or {}).get("user-vlan") or vlane,
        "service_port": (servico or {}).get("id"),
        "tipo_wan": _campo(campos_wan, "Service type"),
        "status_conexao": _campo(campos_wan, "IPv4 Connection status"),
        "mac": _mac_util(campos_ip, campos_wan),
    }


