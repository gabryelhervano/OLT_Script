"""Testes automatizados do reprovisionamento de ONTs.

Os testes usam o transporte falso do módulo, sem conexão com equipamentos.
O transporte registra todos os comandos enviados, o que permite provar que
comandos de alteração nunca são enviados no DRY-RUN.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reprovisionar_olt as script


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
            "serial": "HWTC0001",
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
            "serial": "HWTC0001",
            "usuario": "maria",
        })
        self.assertEqual(resultado["classe"], script.BLOQUEADA_DIVERGENCIA)

    def test_identificadores_ausentes(self):
        resultado = self.classificar({
            "serial": "HWTC0009",
            "usuario": "ninguem",
        })
        self.assertEqual(resultado["classe"], script.IGNORADA_SEM_IDENTIFICADORES)


class TestesFluxo(unittest.TestCase):
    def setUp(self):
        self.sessao = script.SessaoSimulada()
        self.sessao.definir_resposta(
            "display ont", "ONT ID : 42\n  F/S/P : 0/1/4\n  Serial : HWTC0001\n")
        self.sessao.definir_resposta(
            "display service-port",
            "Service-port 796 vlan 2551 gpon 0/1/4 ont 42 gemport 37 "
            "multi-service user-vlan 2551 tag-transform translate")
        self.sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  srvprofile: 100\n"
            "  tr069profile: 1\n  wanprofile: 10\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        self.sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT MAC : 00:11:22:33:44:55\n"
            "  ONT manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")

    def _configuracao(self):
        return {
            "porta": 4,
            "ont_id": 42,
            "interface": "0/1",
            "serial": "HWTC0001",
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
        }

    def por_serial(self):
        indice = {}
        for registro in (
            {
                "serial": "HWTC0001",
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
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=self.sessao,
            executar_alteracoes=False,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0001",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0001": [registro]}
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
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=self.sessao,
            executar_alteracoes=False,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0001",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0001": [registro]}
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

    def test_bloqueio_quando_valor_nao_e_descoberto(self):
        # Falta o srvprofile na resposta das consultas: nada pode ser assumido,
        # então a ONT fica INCONCLUSIVO sem executar nenhum comando.
        sessao = script.SessaoSimulada()
        sessao.definir_resposta(
            "display ont", "ONT ID : 42\n  F/S/P : 0/1/4\n  Serial : HWTC0001\n")
        sessao.definir_resposta(
            "display service-port",
            "Service-port 796 vlan 2551 gpon 0/1/4 ont 42 gemport 37 "
            "multi-service user-vlan 2551 tag-transform translate")
        sessao.definir_resposta(
            "display ont wan-info 0/1 4 42",
            "  tr069profile: 1\n  wanprofile: 10\n"
            "  Service type : Tr069, Internet\n"
            "  IPv4 Connection status : Connected\n")
        sessao.definir_resposta(
            "display ont ipconfig 4 42",
            "  PPPoE username : joao\n"
            "  ONT MAC : 00:11:22:33:44:55\n"
            "  ONT manage VLAN : 2551\n"
            "  ONT manage priority : 5\n")
        configuracao = {
            "host": "127.0.0.1",
            "porta": "22",
            "usuario": "",
            "senha": "",
            "chave": "",
            "interface": "0/1",
            "lineprofile": 11,
        }
        orquestrador = script.Orquestrador(
            configuracao=configuracao,
            sessao=sessao,
            executar_alteracoes=True,
        )
        registro = {
            "interface": "0/1",
            "porta": 4,
            "ont_id": 42,
            "serial": "HWTC0001",
            "usuario": "joao",
            "senha": "1234",
            "mac": "00:11:22:33:44:55",
            "estado": "VALIDO",
        }
        indice_por_serial = {"HWTC0001": [registro]}
        indice_por_usuario = {"joao": [registro]}
        resposta_original = script._confirmar
        script._confirmar = lambda mensagem: True
        try:
            resultados = orquestrador.executar(
                [registro], indice_por_serial, indice_por_usuario)
        finally:
            script._confirmar = resposta_original
        info = resultados["0/1/4/42"]
        self.assertEqual(info["classe"], script.ESTADO_INCONCLUSIVO)
        self.assertTrue(info["motivo"].startswith("falha na execução"))
        enviados = [comando for comando in sessao.comandos
                    if comando.startswith(("undo", "ont ", "service-port"))]
        self.assertEqual(enviados, [])


if __name__ == "__main__":
    unittest.main()
