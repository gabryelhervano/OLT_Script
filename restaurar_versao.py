"""Restaura uma versão anterior do reprovisionamento.

Uso:
    python3 restaurar_versao.py                lista as versões disponíveis
    python3 restaurar_versao.py v1.1.0         restaura a versão indicada
    python3 restaurar_versao.py --atualizar    volta para a última versão

A restauração cria uma marca do estado atual antes de trocar, de modo que
nada se perde, e roda a suíte de testes na versão restaurada.
"""

import os
import subprocess
import sys

DIRETORIO = os.path.dirname(os.path.abspath(__file__))


def git(*argumentos):
    return subprocess.run(
        ("git",) + argumentos,
        cwd=DIRETORIO,
        capture_output=True,
        text=True,
    )


def versoes():
    """Versões marcadas, da mais recente para a mais antiga."""
    saida = git("tag", "--list", "--sort=-v:refname",
                "--format=%(refname:short)|%(creatordate:short)|%(contents:subject)")
    linhas = []
    for linha in saida.stdout.splitlines():
        if not linha.strip():
            continue
        partes = linha.split("|", 2)
        while len(partes) < 3:
            partes.append("")
        linhas.append((partes[0], partes[1], partes[2]))
    return linhas


def estado_atual():
    return git("rev-parse", "--short", "HEAD").stdout.strip()


def restaurar(tag):
    """Coloca a árvore de trabalho na versão indicada."""
    if git("rev-parse", "-q", "--verify",
           "refs/tags/%s" % tag).returncode != 0:
        print("ERRO: a versão %s não existe" % tag)
        return 1
    marca = git("describe", "--tags", "--abbrev=0").stdout.strip()
    if marca == tag:
        print("A versão %s já está no estado atual." % tag)
        return 0
    antes = git("stash", "list").stdout.strip()
    if antes:
        print("Há alterações locais; guarde-as antes de continuar.")
        return 1
    atual = estado_atual()
    git("tag", "antes-do-rollback-%s" % atual)
    resultado = git("checkout", "-b", "rollback-%s" % tag, tag)
    if resultado.returncode:
        print("ERRO: %s" % resultado.stderr.strip())
        return 1
    print("Versão %s restaurada (o estado anterior ficou na marca "
          "antes-do-rollback-%s)." % (tag, atual))
    print("Rodando a suíte de testes...")
    testes = subprocess.run([sys.executable, "testes_reprovisionar.py"],
                            cwd=DIRETORIO)
    if testes.returncode:
        print("A suíte de testes falhou nesta versão.")
    else:
        print("Suíte de testes aprovada.")
    print("")
    print("Para voltar à última versão: python3 restaurar_versao.py --atualizar")
    return 0


def atualizar():
    """Volta para a branch main, com as últimas alterações."""
    resultado = git("checkout", "main")
    if resultado.returncode:
        print("ERRO: %s" % resultado.stderr.strip())
        return 1
    git("pull", "--ff-only")
    print("De volta à versão mais recente (%s)." % estado_atual())
    return 0


def listar():
    print("Versões disponíveis (da mais recente para a mais antiga):")
    print("")
    for tag, data, assunto in versoes():
        print("  %-12s %s  %s" % (tag, data, assunto))
    print("")
    print("Estado atual: %s" % estado_atual())
    print("Para restaurar: python3 restaurar_versao.py <versao>")
    return 0


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        return listar()
    if argv[0] == "--atualizar":
        return atualizar()
    return restaurar(argv[0])


if __name__ == "__main__":
    sys.exit(main())
