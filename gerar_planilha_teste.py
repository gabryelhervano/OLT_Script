"""Gera uma planilha de teste no formato esperado pelo reprovisionamento.

Uso:
    python3 gerar_planilha_teste.py [saida.xlsx]

Formato: 4 colunas, sem cabeçalho:
    usuário | senha | MAC da ONT | serial da ONT

A planilha é apenas ilustrativa: serve para validar a leitura, a
classificação e a descoberta de estado. Nenhuma alteração é feita na OLT.
"""

import os
import sys

try:
    import openpyxl
except ImportError:
    print("ERRO: instale as dependências com: pip install -r requirements.txt")
    sys.exit(1)


# (usuário, senha, MAC, serial)
EXEMPLOS = [
    ("sergioms", "60177519649", "28:68:D2:B2:1C:EF", "HWTC0D3BDDA7"),
    ("mariaap", "60177519650", "AC:8D:34:D9:BD:67", "48575443F1E71EA3"),
    ("joaoalberto", "60177519651", "AC:8D:34:D9:BD:70", "48575443FA70D1B3"),
    ("cleiaj", "60177519652", "5A:59:58:45:CB:03", "5A595845CB03BB44"),
    # MAC e serial de example (marcadores de ausência): a linha é ignorada
    ("semidentificador", "60177519653", "FFFFFFFFFFFFFFFF", "FFFFFFFFFFFFFFFF"),
    # linha sem MAC e sem serial, mas com usuário: association por PPPoE
    ("sousuario", "60177519654", "", ""),
]


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    saida = argv[0] if argv else "planilha_teste.xlsx"
    if os.path.exists(saida):
        print("ERRO: o arquivo já existe: %s" % saida)
        return 1
    livro = openpyxl.Workbook()
    folha = livro.active
    folha.title = "usuarios"
    for usuario, senha, mac, serial in EXEMPLOS:
        folha.append([usuario, senha, mac, serial])
    livro.save(saida)
    print("planilha de teste criada: %s (%d linhas)" % (saida, len(EXEMPLOS)))
    print("formato: usuário | senha | MAC | serial (sem cabeçalho)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
