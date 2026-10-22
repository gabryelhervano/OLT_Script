"""Testes automatizados do reprovisionamento de ONTs.

Os testes usam o transporte falso do módulo, sem conexão com equipamentos.
O transporte registra todos os comandos enviados, o que permite provar que
comandos de alteração nunca são enviados no DRY-RUN.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import olt as script
from olt import interacao
from olt.descoberta import (_consultar_configuracao_atual,
                          _consultar_perfil_ont)


REGISTRO_TESTE = os.path.join(tempfile.gettempdir(),
                              "registro_teste_reprovisionar.json")


def escrever(path, linhas):
    import openpyxl

    livro = openpyxl.Workbook()
    folha = livro.active
    folha.title = "usuarios"
    for numero, linha in enumerate(linhas):
        valores = [valor.strip('"') for valor in linha.split(",")]
        folha.append(valores)
    livro.save(path)


class TestesPlanilha(unittest.TestCase):
    def setUp(self):
        self.pasta = os.path.dirname(os.path.abspath(__file__))
        self.caminho = os.path.join(self.pasta, "planilha_teste.xlsx")

    def tearDown(self):
        if os.path.exists(self.caminho):
            os.remove(self.caminho)

    def _escrever(self, linhas):
        escrever(self.caminho, linhas)

    def test_leitura_sem_cabecalho(self):
        self._escrever([
            "joao,1234,00:11:22:33:44:55,HWTC00000001",
            "maria,12345,0A:0B:0C:0D:0E:0F,HWTC00000002",
        ])
        registros, problemas = script.ler_planilha(self.caminho)
        self.assertEqual(len(registros), 2)
        self.assertEqual(problemas, [])

    def test_preserva_zeros_e_caracteres_especiais(self):
        self._escrever([
            'joao,"0001230",00:11:22:33:44:55,HWTC00000001',
        ])
        registros, _ = script.ler_planilha(self.caminho)
        self.assertEqual(registros[0]["senha"], "0001230")

    def test_mac_aceito_com_ou_sem_traco(self):
        caminho = self.caminho
        escrever(caminho, [
            "cliente,senha123,AC8D-34D9-BD67,HWTC0D3BDDA7",
            "outro,senha123,28:68:D2:B2:1C:EF,48575443FA70D1B3",
        ])
        registros, problemas = script.ler_planilha(caminho)
        self.assertEqual(len(registros), 2)
        self.assertEqual(problemas, [])
        self.assertEqual(registros[0]["mac"], "AC:8D:34:D9:BD:67")

    def test_identifica_mac_e_serial_invalidos(self):
        self._escrever([
            "joao,1234,00:11:22:33:44,HWTC00000001",
            "maria,1234,00:11:22:33:44:55,1",
        ])
        registros, problemas = script.ler_planilha(self.caminho)
        self.assertEqual(registros, [])
        self.assertEqual([p["estado"] for p in problemas], ["INVALIDO", "INVALIDO"])

    def test_ausencia_de_usuario_e_serial_e_mac_e_aceita(self):
        self._escrever([
            "joao,1234,,HWTC00000001",
            "maria,1234,00:11:22:33:44:55,",
        ])
        registros, _ = script.ler_planilha(self.caminho)
        self.assertEqual(len(registros), 2)

    def test_sem_credencial_bloqueia(self):
        self._escrever([
            "joao,,00:11:22:33:44:55,HWTC00000001",
        ])
        registros, problemas = script.ler_planilha(self.caminho)
        self.assertEqual(registros, [])
        self.assertEqual(problemas[0]["estado"], "INVALIDO")

    def test_marcadores_de_ausencia_sao_ignorados(self):
        self._escrever([
            "joao,1234,FF:FF:FF:FF:FF:FF,FF:FF:FF:FF:FF:FF",
        ])
        registros, problemas = script.ler_planilha(self.caminho)
        self.assertEqual(registros, [])
        self.assertEqual(problemas[0]["estado"], "IGNORADO")

    def test_identificadores_duplicados_bloquem(self):
        self._escrever([
            "joao,1234,00:11:22:33:44:55,HWTC00000001",
            "joao,1234,00:11:22:33:44:66,HWTC00000001",
        ])
        registros, problemas = script.ler_planilha(self.caminho)
        self.assertEqual([r["estado"] for r in registros], ["DUPLICADO", "DUPLICADO"])
        self.assertEqual(problemas[0]["estado"], "DUPLICADO")


class TestesClassificacao(unittest.TestCase):
    def setUp(self):
        self.registros = [
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
                "serial": "HWTC0002",
                "estado": "VALIDO",
            },
        ]
        self.por_serial = {}
        self.por_usuario = {}
        for registro in self.registros:
            self.por_serial.setdefault(registro["serial"], []).append(registro)
            self.por_usuario.setdefault(registro["usuario"], []).append(registro)

    def classificar(self, atual):
        return script.classificar(
            atual, self.por_serial, self.por_usuario)

    def test_padrao_um_completo(self):
        resultado = self.classificar({
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
        })
        self.assertEqual(resultado["classe"], script.PADRAO_1_COMPLETA)

    def test_correspondencia_por_serial(self):
        resultado = self.classificar({
            "serial": "HWTC0002",
            "usuario": "",
        })
        self.assertEqual(resultado["classe"], script.PADRAO_2_SERIAL)

    def test_correspondencia_por_usuario(self):
        resultado = self.classificar({
            "serial": "",
            "usuario": "maria",
        })
        self.assertEqual(resultado["classe"], script.PADRAO_2_PPPOE)

    def test_divergencia_bloqueia(self):
        resultado = self.classificar({
            "serial": "HWTC0D3BDDA7",
            "usuario": "maria",
        })
        self.assertEqual(resultado["classe"], script.BLOQUEADA_DIVERGENCIA)

    def test_identificadores_ausentes(self):
        resultado = self.classificar({
            "serial": "HWTC0009",
            "usuario": "ninguem",
        })
        self.assertEqual(resultado["classe"], script.IGNORADA_SEM_IDENTIFICADORES)


RESPOSTA_CONFIG_ATUAL = """ ont-srvprofile gpon profile-id 100 profile-name "SRV-PADRAO"
 ont-lineprofile gpon profile-id 10 profile-name "LINE-PADRAO"
 ont add 4 42 sn-auth "HWTC0D3BDDA7" omci ont-lineprofile-id 10 ont-srvprofile-id 100 desc "joao"
 ont ipconfig 4 42 pppoe vlan 2551 priority 5 user-account username "joao" password "********"
 ont port native-vlan 4 42 eth 1 vlan 2551 priority 0
 ont tr069-server-config 4 42 profile-id 1
 ont internet-config 4 42 ip-index 0
 ont wan-config 4 42 ip-index 0 profile-id 10
 ont add 4 43 sn-auth "HWTC0D3BDDA9" omci ont-lineprofile-id 10 ont-srvprofile-id 100 desc "maria"
 ont tr069-server-config 4 43 profile-id 1
 ont internet-config 4 43 ip-index 0
 ont wan-config 4 43 ip-index 0 profile-id 10
"""


RESPOSTA_PERFIL_ONT = """  ONT-ID    Line profile name                 Service profile name
  -----------------------------------------------------------------------------
      42    LINE-PADRAO                       SRV-PADRAO
"""


RESPOSTA_SERVICE_PORT = """   INDEX VLAN VLAN     PORT F/ S/ P VPI  VCI   FLOW  FLOW       RX   TX   STATE
         ID   ATTR     TYPE                    TYPE  PARA
  -----------------------------------------------------------------------------
     796 2551 common   gpon 0/1 /4  42   37    vlan  2551       -    -    up
     797 2551 common   gpon 0/1 /4  43   38    vlan  2551       -    -    up
  -----------------------------------------------------------------------------
"""


RESPOSTA_RESUMO_ONTS = """  In port 0/1/4, the total of ONTs are: 2, online: 1
  -----------------------------------------------------------------------------
  ONT  Run     Last                Last                Last
  ID   State   UpTime              DownTime            DownCause
  -----------------------------------------------------------------------------
  42   online  2026-10-16 10:51:06 2026-10-16 10:47:01 dying-gasp
  43   offline 2026-10-16 14:40:18 2026-10-17 00:06:43 dying-gasp
  -----------------------------------------------------------------------------
  ONT        SN        Type          Distance Rx/Tx power  Description
  ID                                    (m)      (dBm)
  -----------------------------------------------------------------------------
  42  HWTC0D3BDDA7 HG8145V5         827   -20.55/2.30  joao
  43  HWTC0D3BDDA9 HG8245W5-6T      -     -/-          maria
  -----------------------------------------------------------------------------
"""


RESPOSTA_CONFIG_ATUAL_COM_PERFIL_NOVO = (
    ' ont-srvprofile gpon profile-id 100 profile-name "SRV-PADRAO"\n'
    ' ont add 4 42 sn-auth "HWTC0D3BDDA7" omci ont-lineprofile-id 11'
    ' ont-srvprofile-id 100 desc "joao"\n'
    ' ont tr069-server-config 4 42 profile-id 1\n'
    ' ont internet-config 4 42 ip-index 0\n'
    ' ont wan-config 4 42 ip-index 0 profile-id 10\n'
)


RESPOSTA_RESUMO_DOIS_ONTS = """  In port 0/1/4, the total of ONTs are: 2, online: 2
  ------------------------------------------------------------------------------
  ONT  Run     Last                Last                Last
  ID   State   UpTime              DownTime            DownCause
  ------------------------------------------------------------------------------
  42   online  2026-10-16 10:51:06 2026-10-16 10:47:01 dying-gasp
  43   online  2026-10-16 10:51:06 2026-10-16 10:47:01 dying-gasp
  ------------------------------------------------------------------------------
  ONT        SN        Type          Distance Rx/Tx power  Description
  ID                                    (m)      (dBm)
  ------------------------------------------------------------------------------
  42  HWTC0D3BDDA7 HG8145V5         827   -20.55/2.30  joao
  43  HWTC0D3BDDA9 HG8245W5-6T      837   -20.41/2.30  semusuario
  ------------------------------------------------------------------------------
"""


RESPOSTA_SERVICE_PORT_DOIS_ONTS = """   INDEX VLAN VLAN     PORT F/ S/ P VPI  VCI   FLOW  FLOW       RX   TX   STATE
         ID   ATTR     TYPE                    TYPE  PARA
  ---------------------------------------------------------------------------
     796 2551 common   gpon 0/1 /4  42   37    vlan  2551       -    -    up
     797 2551 common   gpon 0/1 /4  43   38    vlan  2551       -    -    up
  ---------------------------------------------------------------------------
"""


RESPOSTA_RESUMO_FABRICANTES = """  In port 0/1/4, the total of ONTs are: 3, online: 3
  ------------------------------------------------------------------------------
  ONT  Run     Last                Last                Last
  ID   State   UpTime              DownTime            DownCause
  ------------------------------------------------------------------------------
  42   online  2026-10-16 10:51:06 2026-10-16 10:47:01 dying-gasp
  43   online  2026-10-16 10:51:06 2026-10-16 10:47:01 dying-gasp
  44   online  2026-10-16 10:51:06 2026-10-16 10:47:01 dying-gasp
  ------------------------------------------------------------------------------
  ONT        SN        Type          Distance Rx/Tx power  Description
  ID                                    (m)      (dBm)
  ------------------------------------------------------------------------------
  42  48575443FA70D1B3 EG8145V5-V2      160   -19.70/2.32  cliente42
  43  5A595845CB03BB44 PX3321-T1        322   -18.38/2.26  cliente43
  44  414C434CFC65280D NOCLEICODEG1425G 248   -18.41/2.44  cliente44
  ------------------------------------------------------------------------------
"""


class TestesSessao(unittest.TestCase):
    def test_exibicao_dos_resultados(self):
        import io
        import contextlib

        import reprovisionar_olt as cli

        resultados = {
            "0/2/15/1": {
                "classe": "INCONCLUSIVO",
                "motivo": "a OLT recusou o comando",
            },
            "0/2/15/2": {"classe": "SUCESSO", "motivo": ""},
        }
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            cli._exibir_detalhes(resultados)
        texto = saida.getvalue()
        self.assertIn("0/2/15/1 -> INCONCLUSIVO", texto)
        self.assertIn("motivo: a OLT recusou o comando", texto)
        self.assertIn("0/2/15/2 -> SUCESSO", texto)
        # O SUCESSO não traz motivo nem estado: só a linha da classe.
        self.assertEqual(
            [linha.strip() for linha in texto.splitlines()
             if linha.strip() == "0/2/15/2 -> SUCESSO"],
            ["0/2/15/2 -> SUCESSO"])

    def test_deteccao_de_prompt(self):
        sessao = script.SessaoSSH({})
        self.assertTrue(sessao._prompt_no_buffer("OLT_MA5800-SANTA_RITA#"))
        self.assertTrue(sessao._prompt_no_buffer("OLT_MA5800-SANTA_RITA>"))
        self.assertTrue(
            sessao._prompt_no_buffer("OLT_MA5800-SANTA_RITA(config)#"))
        self.assertFalse(sessao._prompt_no_buffer(""))
        self.assertFalse(sessao._prompt_no_buffer("saida sem prompt"))

    def test_comandos_mascarados(self):
        comando = 'ont ipconfig 4 42 pppoe vlan 2551 password "segredo"'
        mascarado = script.exibir_comandos([comando])[0]
        self.assertIn('password "********"', mascarado)
        self.assertNotIn("segredo", mascarado)

    def test_modo_debug_exibe_o_terminal_da_olt(self):
        # O monitor recebe cada troca: comando, resposta crua e se era uma
        # alteração. A senha é mascarada no comando e na resposta (a CLI
        # ecoa o que recebeu).
        trocas = []
        sessao = script.SessaoSimulada(
            monitor=lambda comando, saida, alteracao: trocas.append(
                (comando, saida, alteracao)))
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  IPv4 Connection status : Connected")
        sessao.definir_resposta(
            'ont ipconfig 4 42 pppoe vlan 2551 password "1234"',
            '  Command:\n'
            '          ont ipconfig 4 42 pppoe vlan 2551 password "1234"\n'
            '   ----------------------------------------------------------------')
        sessao.executar("display ont wan-info 0/1 4 42")
        sessao.alterar('ont ipconfig 4 42 pppoe vlan 2551 password "1234"')
        self.assertEqual(len(trocas), 2)
        comando, saida, alteracao = trocas[0]
        self.assertEqual(comando, "display ont wan-info 0/1 4 42")
        self.assertIn("Connected", saida)
        self.assertFalse(alteracao)
        comando, saida, alteracao = trocas[1]
        self.assertTrue(alteracao)
        self.assertIn('password "********"', comando)
        self.assertIn('password "********"', saida)
        self.assertNotIn("1234", comando)
        self.assertNotIn("1234", saida)

    def test_monitor_do_cli_mostra_rotulo_e_resposta(self):
        import io
        import contextlib

        import reprovisionar_olt as cli

        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            cli._monitor_terminal("display version", "  Huawei V100R021",
                                  alteracao=False)
            cli._monitor_terminal("ont delete 4 42", "", alteracao=True)
        texto = saida.getvalue()
        self.assertIn("[leitura] display version", texto)
        self.assertIn("  Huawei V100R021", texto)
        self.assertIn("[ALTERAÇÃO] ont delete 4 42", texto)
        self.assertIn("(sem resposta)", texto)

    def test_argumento_debug(self):
        import reprovisionar_olt as cli

        argumentos = cli._parse_argumentos(["--file", "x.xlsx", "--debug"])
        self.assertTrue(argumentos.debug)
        self.assertFalse(
            cli._parse_argumentos(["--file", "x.xlsx"]).debug)

    def test_mock_aceita_comandos_de_alteracao(self):
        # `--mock --execute` usa o Simulador: sem `alterar` a execução
        # simulada quebrava com AttributeError.
        sessao = script.Simulador()
        sessao.alterar("ont delete 4 42")
        self.assertIn("ont delete 4 42", sessao.comandos)
        sessao.definir_resposta("comando ruim", "  Failure: recusado")
        with self.assertRaises(script.ErroOperacao):
            sessao.alterar("comando ruim")

    def test_diagnostico_lista_os_perfis_com_o_nome(self):
        import io
        import contextlib

        import reprovisionar_olt as cli

        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont tr069-server-profile all",
            "  Profile-id  Profile-name       Binding times\n"
            "  ---- --------------------------------------------------\n"
            "  10          anlix-flashman     1\n")
        sessao.definir_resposta(
            "display ont wan-profile all",
            "  Profile-ID  Profile-name       Binding times\n"
            "  ---- --------------------------------------------------\n"
            "  1           internet_tr069     2\n")
        configuracao = {"tr069profile": 10, "wanprofile": 1}
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            cli._exibir_perfis_da_olt(sessao, configuracao)
        texto = saida.getvalue()
        self.assertIn("tr069-profile    : 10 (anlix-flashman)", texto)
        self.assertIn("wan-profile      : 1 (internet_tr069)", texto)

    def test_diagnostico_aponta_quando_o_perfil_nao_existe(self):
        import io
        import contextlib

        import reprovisionar_olt as cli

        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont tr069-server-profile all",
            "  10          anlix-flashman     1\n")
        sessao.definir_resposta(
            "display ont wan-profile all",
            "  1           internet_tr069     2\n")
        configuracao = {"tr069profile": 3, "wanprofile": 1}
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            cli._exibir_perfis_da_olt(sessao, configuracao)
        texto = saida.getvalue()
        # O ID configurado não existe: a lista real é mostrada.
        self.assertIn("tr069-profile    : 3", texto)
        self.assertIn("disponíveis", texto)
        self.assertIn("10 (anlix-flashman)", texto)


class TestesFluxo(unittest.TestCase):
    def setUp(self):
        if os.path.exists(REGISTRO_TESTE):
            os.remove(REGISTRO_TESTE)
        self.sessao = script.SessaoSimulada()
        self.sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        self.sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        self.sessao.definir_resposta(
            "display current-configuration | include ont", RESPOSTA_CONFIG_ATUAL)
        self.sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        self.sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  srvprofile: 100\n"
            "  tr069profile: 1\n  wanprofile: 10\n"
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  MAC address : AC8D-34D9-BD67\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        self.sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  MAC address : 00:11:22:33:44:55\n"
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n")
        self.sessao.definir_resposta(
            "display ont wan-info 0/1 4 43",
            "  srvprofile: 100\n"
            "  tr069profile: 1\n  wanprofile: 10\n"
            "  Manage VLAN : 2552\n"
            "  Manage priority : 5\n"
            "  MAC address : AC8D-34D9-BD68\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        self.sessao.definir_resposta(
            "display ont ipconfig 4 43",
            "  PPPoE username : maria\n"
            "  ONT config type : PPPoE\n"
            "  MAC address : 00:11:22:33:44:56\n"
            "  Manage VLAN : 2552\n"
            "  Manage priority : 5\n")

    def _configuracao(self):
        return {
            "porta": 4,
            "ont_id": 42,
            "interface": "0/1",
            "serial": "HWTC0D3BDDA7",
            "descricao": "joao",
            "lineprofile": 11,
            "srvprofile": 100,
            "tr069profile": 1,
            "wanprofile": 10,
            "vlan": 2551,
            "prioridade": 5,
            "usuario": "joao",
            "senha": "000123",
            "gemport": 37,
            "user_vlan": 2551,
        }

    def por_serial(self):
        indice = {}
        for registro in (
            {
                "serial": "HWTC0D3BDDA7",
                "usuario": "joao",
                "senha": "1234",
            },
        ):
            indice.setdefault(registro["serial"], []).append(registro)
        return indice

    def por_usuario(self):
        indice = {}
        for registro in (
            {
                "usuario": "joao",
                "senha": "1234",
            },
        ):
            indice.setdefault(registro["usuario"], []).append(registro)
        return indice

    def test_simulacao_nao_envia_comandos_de_alteracao(self):
        configuracao = self._configuracao()
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=self.sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        resultados = orquestrador.executar(
            [], self.por_serial(), self.por_usuario())
        enviados = [comando for comando in self.sessao.comandos
                   if comando.startswith(("undo", "ont ", "service-port "))]
        self.assertEqual(enviados, [])

    def test_comando_de_criacao_nao_leva_identificador(self):
        comando = script.comando_criar_service_port(self._configuracao())
        partes = comando.split(" ")
        self.assertEqual(partes[0], "service-port")
        self.assertEqual(partes[1], "vlan")

    def test_senha_e_mascarada(self):
        comando = script.comando_configurar_pppoe(self._configuracao())
        self.assertIn('password "000123"', comando)
        mascarado = script.exibir_comandos([comando])[0]
        self.assertIn('password "********"', mascarado)
        self.assertNotIn("000123", mascarado)

    def test_descobrimento_usa_estado_atual(self):
        # Somente o line-profile vem da configuração; todos os demais valores
        # devem vir descobertos na resposta das consultas de leitura.
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=self.sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        dados_por_ont = {}
        _preparar_original = orquestrador._preparar_card

        def _preparar(card, onts, indice_por_serial, indice_por_usuario):
            for dados in onts:
                dados_por_ont[script.chave_ont(dados)] = dados
            return _preparar_original(
                card, onts, indice_por_serial, indice_por_usuario)

        orquestrador._preparar_card = _preparar
        resultados = orquestrador.executar(
            [registro], indice_por_serial, indice_por_usuario)
        info = resultados["0/1/4/42"]
        # Estado completo descoberto: o padrão completo é aceito no DRY-RUN.
        self.assertEqual(info["classe"], script.PADRAO_1_COMPLETA)
        self.assertIn("registro", info)
        # Os comandos montados usam o estado descoberto, não defaults locais.
        dados = dados_por_ont["0/1/4/42"]
        passos = orquestrador._montar_passos(dados)
        texto = "\n".join(passos)
        # O line-profile configurável difere do restante do estado descoberto.
        self.assertIn("ont-lineprofile-id 11", texto)
        self.assertIn("ont-srvprofile-id 100", texto)
        # VLAN, prioridade, user-vlan e gemport vieram da descoberta.
        self.assertIn("pppoe vlan 2551 priority 5", texto)
        self.assertIn("user-vlan 2551", texto)
        self.assertIn("gemport 37", texto)
        # Perfis WAN/TR-069 vieram da descoberta.
        self.assertIn("ont tr069-server-config 4 42 profile-id 1", texto)
        self.assertIn("ont wan-config 4 42 ip-index 0 profile-id 10", texto)

    def test_sequencia_respeita_contexto_da_cli(self):
        # `undo service-port` e `service-port` são comandos da configuração
        # raiz: o primeiro vem antes de entrar em `interface gpon` e o
        # segundo depois de sair com `quit`.
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=self.sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        dados_por_ont = {}
        _preparar_original = orquestrador._preparar_card

        def _preparar(card, onts, indice_por_serial, indice_por_usuario):
            for dados in onts:
                dados_por_ont[script.chave_ont(dados)] = dados
            return _preparar_original(
                card, onts, indice_por_serial, indice_por_usuario)

        orquestrador._preparar_card = _preparar
        orquestrador.executar(
            [registro], indice_por_serial, indice_por_usuario)
        passos = orquestrador._montar_passos(dados_por_ont["0/1/4/42"])
        comandos = [passo for passo in passos if not passo.startswith("display")]
        # O `undo` do service-port vem na raiz, antes da interface.
        self.assertEqual(comandos[0], "undo service-port 796")
        self.assertEqual(comandos[1], "interface gpon 0/1")
        # O serviço de internet precisa ser ativado.
        self.assertIn("ont internet-config 4 42 ip-index 0", comandos)
        # O `quit` devolve ao contexto raiz, onde o `service-port` é válido.
        posicao_quit = comandos.index("quit")
        self.assertTrue(comandos[posicao_quit + 1].startswith("service-port"))
        # O caminho GPON inclui a porta (interface/porta).
        self.assertIn(
            "service-port vlan 2551 gpon 0/1/4 ont 42 gemport 37 "
            "multi-service user-vlan 2551 tag-transform translate",
            comandos)

    def test_listagem_interpreta_resumo_da_porta(self):
        # `display ont info summary <fsp>` traz duas tabelas: estado e
        # SN/tipo/descrição. A descrição é o usuário configurado no `desc`.
        onts = script.listar_onts(self.sessao, "0/1", [4])
        self.assertEqual(sorted(onts), [42, 43])
        ont = onts[42]
        self.assertEqual(ont["Serial"], "HWTC0D3BDDA7")
        self.assertEqual(ont["Description"], "joao")
        self.assertEqual(ont["F/S/P"], "0/1/4")
        self.assertEqual(ont["estado"], "online")
        self.assertEqual(onts[43]["estado"], "offline")

    def test_ont_sem_correspondencia_nao_interrompe_o_card(self):
        # A ONT 43 não tem correspondência com a planilha: é descartada na
        # triagem e a ONT 42, do mesmo card, continua sendo processada.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont", RESPOSTA_CONFIG_ATUAL)
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  srvprofile: 100\n  tr069profile: 1\n  wanprofile: 10\n"
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resultados = orquestrador.executar(
            [registro], indice_por_serial, indice_por_usuario)
        # Sem correspondência com a planilha, a ONT é descartada na triagem
        # e nem chega a ser consultada.
        self.assertEqual(
            resultados["0/1/4/43"]["classe"],
            script.IGNORADA_SEM_IDENTIFICADORES)
        self.assertEqual(
            resultados["0/1/4/42"]["classe"], script.PADRAO_1_COMPLETA)

    def test_sequencia_validada_e_completa(self):
        # A sequência reprovisiona a ONT inteira: native-vlan, TR-069,
        # internet e WAN sempre entram, com os perfis configurados.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont",
            RESPOSTA_CONFIG_ATUAL)
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resultados = orquestrador.executar(
            [registro], indice_por_serial, indice_por_usuario)
        comandos = [passo for passo in resultados["0/1/4/42"]["passos"]
                    if not passo.startswith("display")]
        self.assertEqual(comandos, [
            "undo service-port 796",
            "interface gpon 0/1",
            "ont delete 4 42",
            "ont add 4 42 sn-auth \"HWTC0D3BDDA7\" omci ont-lineprofile-id 11"
            " ont-srvprofile-id 100 desc \"joao\"",
            "ont ipconfig 4 42 pppoe vlan 2551 priority 5 user-account"
            " username \"joao\" password \"1234\"",
            "ont port native-vlan 4 42 eth 1 vlan 2551 priority 0",
            "ont tr069-server-config 4 42 profile-id 1",
            "ont internet-config 4 42 ip-index 0",
            "ont wan-config 4 42 ip-index 0 profile-id 10",
            "quit",
            "service-port vlan 2551 gpon 0/1/4 ont 42 gemport 37"
            " multi-service user-vlan 2551 tag-transform translate",
        ])

    def test_ont_sem_correspondencia_nao_e_reprovisionada(self):
        # A ONT 43 não tem correspondência com a planilha (padrão 3). Mesmo
        # tendo service-port, ela não pode ser reprovisionada: o usuário e a
        # senha chegariam vazios.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_DOIS_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT_DOIS_ONTS)
        sessao.definir_resposta(
            "display current-configuration | include ont",
            RESPOSTA_CONFIG_ATUAL)
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  MAC address : AC8D-34D9-BD67\n"
            "  Service type : Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 43",
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 43",
            "  PPPoE username :\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
            "maximo": 5,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=True,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resposta_original = interacao.confirmar
        interacao.confirmar = lambda mensagem: True
        try:
            resultados = orquestrador.executar(
                [registro], indice_por_serial, indice_por_usuario)
        finally:
            interacao.confirmar = resposta_original
        self.assertEqual(
            resultados["0/1/4/43"]["classe"],
            script.IGNORADA_SEM_IDENTIFICADORES)
        alteracoes = [comando for comando in sessao.comandos
                      if comando.startswith(("undo", "ont delete", "ont add",
                                             "ont ipconfig", "service-port v"))]
        # Nenhum comando de alteração para a ONT 43.
        self.assertEqual([c for c in alteracoes if " 43" in c], [])
        # A ONT 42, compatível, foi reprovisionada por completo.
        self.assertTrue([c for c in alteracoes if " 42" in c])

    def test_ont_com_o_perfil_desejado_nao_e_reprovisionada(self):
        # A ONT 42 já está com o line-profile configurado (11): o script
        # não deve reprovisioná-la, para não gerar retrabalho na CPE.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont",
            RESPOSTA_CONFIG_ATUAL_COM_PERFIL_NOVO)
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resultados = orquestrador.executar(
            [registro], indice_por_serial, indice_por_usuario)
        info = resultados["0/1/4/42"]
        self.assertEqual(info["classe"], script.JA_CONFIGURADA)
        self.assertIn("line-profile 11", info["motivo"])
        # Nenhum comando de alteração foi preparado.
        self.assertNotIn("passos", info)

    def test_reprovisionar_ja_configuradas_quando_pedido(self):
        # A opção de configuração permite reprovisionar mesmo assim.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont",
            RESPOSTA_CONFIG_ATUAL_COM_PERFIL_NOVO)
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
            "reprovisionar_ja_configuradas": "sim",
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resultados = orquestrador.executar(
            [registro], indice_por_serial, indice_por_usuario)
        info = resultados["0/1/4/42"]
        self.assertEqual(info["classe"], script.PADRAO_1_COMPLETA)
        self.assertIn("passos", info)

    def test_perfil_em_uso_resolvido_pelo_nome(self):
        # A linha `ont add` da ONT veio truncada pelo terminal: o
        # line-profile em uso é resolvido pelo nome informado no
        # `display ont profile`, e a ONT é reconhecida como já configurada.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont",
            ' ont-lineprofile gpon profile-id 11 profile-name "LINE-NOVO"\n'
            ' ont-lineprofile gpon profile-id 10 profile-name "LINE-PADRAO"\n'
            ' ont-srvprofile gpon profile-id 100 profile-name "SRV-PADRAO"\n'
            ' ont add 4 42 sn-auth "HWTC0D3BDDA7" omci ont-lineprofile-id\n'
            ' 11 ont-srvprofile-id 100 desc "joao"\n')
        sessao.definir_resposta(
            "display ont profile 0 1 4 42",
            "  ONT-ID    Line profile name                 Service profile name\n"
            "  ----------------------------------------------------------------------\n"
            "       42    LINE-NOVO                        SRV-PADRAO\n")
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resultados = orquestrador.executar(
            [registro], indice_por_serial, indice_por_usuario)
        info = resultados["0/1/4/42"]
        self.assertEqual(info["classe"], script.JA_CONFIGURADA)

    def test_sequencia_validada_reaplica_tr069_e_wan(self):
        # A ONT 42 não tem TR-069/WAN próprios, mas a OLT usa o perfil
        # TR-069 1 e o WAN 10: a sequência validada os reaplica, porque a
        # ONT é apagada e recriada.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont",
            ' ont-srvprofile gpon profile-id 100 profile-name "SRV-PADRAO"\n'
            ' ont-lineprofile gpon profile-id 10 profile-name "LINE-PADRAO"\n'
            ' ont add 4 42 sn-auth "HWTC0D3BDDA7" omci ont-lineprofile-id 10'
            ' ont-srvprofile-id 100 desc "joao"\n'
            ' ont add 4 43 sn-auth "HWTC0D3BDDA9" omci ont-lineprofile-id 10'
            ' ont-srvprofile-id 100 desc "maria"\n'
            ' ont tr069-server-config 4 43 profile-id 1\n'
            ' ont internet-config 4 43 ip-index 0\n'
            ' ont wan-config 4 43 ip-index 0 profile-id 10\n')
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resultados = orquestrador.executar(
            [registro], indice_por_serial, indice_por_usuario)
        comandos = resultados["0/1/4/42"]["passos"]
        self.assertIn("ont tr069-server-config 4 42 profile-id 1", comandos)
        self.assertIn("ont internet-config 4 42 ip-index 0", comandos)
        self.assertIn("ont wan-config 4 42 ip-index 0 profile-id 10", comandos)
        self.assertIn(
            "ont port native-vlan 4 42 eth 1 vlan 2551 priority 0", comandos)

    def test_cpe_de_fabricante_suportado_e_descartada(self):
        # A OLT só conversa com CPEs Huawei: as demais são descartadas e o
        # fluxo segue para a próxima ONT do card.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_FABRICANTES)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont",
            RESPOSTA_CONFIG_ATUAL)
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        for ont_id in (42, 43, 44):
            sessao.definir_resposta(
                "display ont wan-info 0/1 4 %d" % ont_id,
                "  Manage VLAN : 2551\n"
                "  Manage priority : 5\n"
                "  Service type : Internet\n"
                "  IPv4 Connection status : Connected\n")
            sessao.definir_resposta(
                "display ont ipconfig 4 %d" % ont_id,
                "  PPPoE username : cliente%d\n"
                "  ONT config type : PPPoE\n"
                "  Manage VLAN : 2551\n"
                "  ONT manage priority : 5\n" % ont_id)
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=False,
            caminho_registro=REGISTRO_TESTE,
        )
        registros = []
        for ont_id, serial, usuario in ((42, "48575443FA70D1B3", "cliente42"),
                                        (43, "5A595845CB03BB44", "cliente43"),
                                        (44, "414C434CFC65280D", "cliente44")):
            registros.append({
                "interface": "0/1",
                "porta": 4,
                "ont_id": ont_id,
                "serial": serial,
                "usuario": usuario,
                "senha": "1234",
                "mac": "00:11:22:33:44:55",
                "estado": "VALIDO",
            })
        indice_por_serial = {r["serial"]: [r] for r in registros}
        indice_por_usuario = {r["usuario"]: [r] for r in registros}
        resultados = orquestrador.executar(
            registros, indice_por_serial, indice_por_usuario)
        # A Huawei (serial em hexadecimal) segue o processo normal.
        self.assertEqual(
            resultados["0/1/4/42"]["classe"], script.PADRAO_1_COMPLETA)
        # ZTE e Nokia são descartadas.
        for ont_id in (43, 44):
            info = resultados["0/1/4/%d" % ont_id]
            self.assertEqual(info["classe"], script.DESCARTA_POR_SERIAL)
            self.assertIn("serial fora do perfil permitido", info["motivo"])
        # Nenhum comando de alteração foi enviado para elas.
        alteracoes = [comando for comando in sessao.comandos
                      if comando.startswith(("undo", "ont delete", "ont add",
                                             "ont ipconfig", "service-port v"))]
        self.assertEqual([c for c in alteracoes if " 43" in c], [])
        self.assertEqual([c for c in alteracoes if " 44" in c], [])

    def test_comando_recusado_pela_olt_e_apontado(self):
        # Se a OLT recusar um comando de alteração, a ONT fica INCONCLUSIVA
        # com o motivo do erro, em vez de passar despercebida.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont",
            RESPOSTA_CONFIG_ATUAL)
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        # a OLT recusa o `ont wan-config`
        sessao.definir_resposta(
            "ont wan-config 4 42 ip-index 0 profile-id 10",
            "  Command:\n          ont wan-config 4 42 ip-index 0 profile-id 10\n"
            "    Failure: The ONT can not support\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=True,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resposta_original = interacao.confirmar
        interacao.confirmar = lambda mensagem: True
        try:
            resultados = orquestrador.executar(
                [registro], indice_por_serial, indice_por_usuario)
        finally:
            interacao.confirmar = resposta_original
        info = resultados["0/1/4/42"]
        self.assertEqual(info["classe"], script.ESTADO_INCONCLUSIVO)
        self.assertIn("can not support", info["motivo"])
        # O `service-port` não foi criado, porque o fluxo parou no erro.
        self.assertFalse([comando for comando in sessao.comandos
                          if comando.startswith("service-port v")])

    def test_sequencia_concluida_e_sucesso(self):
        # Terminar a sequência é o critério de sucesso: a OLT recusa um
        # comando inválido e o script para na hora, então não faz sentido
        # reler wan-info/ipconfig depois dos comandos.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont",
            RESPOSTA_CONFIG_ATUAL)
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", RESPOSTA_PERFIL_ONT)
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  Manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=True,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resposta_original = interacao.confirmar
        interacao.confirmar = lambda mensagem: True
        try:
            resultados = orquestrador.executar(
                [registro], indice_por_serial, indice_por_usuario)
        finally:
            interacao.confirmar = resposta_original
        info = resultados["0/1/4/42"]
        self.assertEqual(info["classe"], script.ESTADO_SUCESSO)
        self.assertFalse(info["motivo"])
        # O `service-port` foi recriado no fim da sequência.
        self.assertTrue([comando for comando in sessao.comandos
                         if comando.startswith("service-port v")])
        # wan-info e ipconfig são lidos uma única vez, na triagem.
        self.assertEqual(
            sessao.comandos.count("display ont wan-info 0/1 4 42"), 1)
        self.assertEqual(
            sessao.comandos.count("display ont ipconfig 4 42"), 1)

    def test_bloqueio_quando_valor_nao_e_descoberto(self):
        # Nem a configuração corrente nem o `display ont profile` revelam o
        # srvprofile: nada pode ser assumido, então a ONT fica INCONCLUSIVO
        # sem executar nenhum comando.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont info summary 0/1/4", RESPOSTA_RESUMO_ONTS)
        sessao.definir_resposta(
            "display service-port all", RESPOSTA_SERVICE_PORT)
        sessao.definir_resposta(
            "display current-configuration | include ont", "")
        sessao.definir_resposta(
            "display ont profile 0 1 4 42", "")
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  tr069profile: 1\n  wanprofile: 10\n"
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT config type : PPPoE\n"
            "  MAC address : 00:11:22:33:44:55\n"
            "  Manage VLAN : 2551\n"
            "  Manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
            "tr069profile": 1,
            "wanprofile": 10,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=True,
            caminho_registro=REGISTRO_TESTE,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0D3BDDA7",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0D3BDDA7": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resposta_original = interacao.confirmar
        interacao.confirmar = lambda mensagem: True
        try:
            resultados = orquestrador.executar(
                [registro], indice_por_serial, indice_por_usuario)
        finally:
            interacao.confirmar = resposta_original
        info = resultados["0/1/4/42"]
        self.assertEqual(info["classe"], script.ESTADO_INCONCLUSIVO)
        self.assertTrue(info["motivo"].startswith("falha na execução"))
        enviados = [comando for comando in sessao.comandos
                    if comando.startswith(("undo", "ont ", "service-port"))]
        self.assertEqual(enviados, [])


if __name__ == "__main__":
    unittest.main()
