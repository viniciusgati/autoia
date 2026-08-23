"""Testes unitários do contrato de veredicto e do parse da história (po)."""

from __future__ import annotations

from app import verdicts


def test_parse_pass_fail():
    assert verdicts.parse_pass_fail("PASS\nSUMMARY: ok") == "PASS"
    assert verdicts.parse_pass_fail("FAIL\nSUMMARY: quebrou") == "FAIL"
    assert verdicts.parse_pass_fail("pass\nSUMMARY: ok") == "PASS"
    assert verdicts.parse_pass_fail("PASS: tudo ok\nSUMMARY: x") == "PASS"
    # tolerante a preâmbulo: marcador como palavra isolada em qualquer linha
    assert verdicts.parse_pass_fail("Resumo da verificação:\nPASS\nSUMMARY: ok") == "PASS"
    assert verdicts.parse_pass_fail("Analisei tudo:\nFAIL: 2 critérios falharam\nfim") == "FAIL"
    # palavra no meio de texto não vale (evita falso positivo)
    assert verdicts.parse_pass_fail("os testes passaram com sucesso") is None
    assert verdicts.parse_pass_fail("portanto FAIL\nfim") is None
    assert verdicts.parse_pass_fail(None) is None


def test_parse_ready_work():
    assert verdicts.parse_ready_work("READY\nSUMMARY: ok") == "READY"
    assert verdicts.parse_ready_work("NEEDS_WORK\nSUMMARY: ambíguo") == "NEEDS_WORK"
    assert verdicts.parse_ready_work("Analisei a história.\nREADY\nSUMMARY: ok") == "READY"
    assert verdicts.parse_ready_work("PASS") is None


def test_parse_head_hash():
    assert verdicts.parse_head_hash("PASS\nSUMMARY: ok\nHEAD: 3e75ec8\n") == "3e75ec8"
    assert verdicts.parse_head_hash("FAIL\nSUMMARY: x\nHEAD: abc1234def") == "abc1234def"
    # tolerante a formatação (sem dois-pontos, hash completo, maiúsculo)
    assert verdicts.parse_head_hash("PASS\nHEAD = 9ab261e\n") == "9ab261e"
    assert verdicts.parse_head_hash("HEAD 6970629\nPASS") == "6970629"
    full = "0123456789abcdef0123456789abcdef01234567"
    assert verdicts.parse_head_hash(f"HEAD: {full}") == "0123456789ab"
    # ausente ou sem hash → None (tolerante: contratos antigos não citam HEAD)
    assert verdicts.parse_head_hash("PASS\nSUMMARY: ok") is None
    assert verdicts.parse_head_hash(None) is None


def test_parse_pm_decision():
    assert verdicts.parse_pm_decision("DECISÃO: retry 3\nMOTIVO: corrigível") == {
        "action": "retry",
        "position": 3,
        "reason": "corrigível",
    }
    # tolerante: nome do robô no lugar da posição → retry sem posição (fallback do runner)
    assert verdicts.parse_pm_decision("DECISÃO: retry tester\nMOTIVO: timeout corrigível") == {
        "action": "retry",
        "position": None,
        "reason": "timeout corrigível",
    }
    # número solto na linha (ex.: "retry fase 3") vale como posição
    assert verdicts.parse_pm_decision("DECISÃO: retry fase 3\nMOTIVO: corrigível")["position"] == 3
    # preâmbulo antes da linha de decisão não interfere
    assert verdicts.parse_pm_decision(
        "Analisei o contexto.\nDECISÃO: retry 2\nMOTIVO: corrigível"
    )["position"] == 2
    # número no MOTIVO não vira posição (só a linha DECISÃO é interpretada)
    assert verdicts.parse_pm_decision("DECISÃO: retry tester\nMOTIVO: fase 3 falhou")["position"] is None
    assert verdicts.parse_pm_decision("DECISÃO: continuar\nMOTIVO: progresso")["action"] == "continue"
    assert verdicts.parse_pm_decision("DECISÃO: escalar\nMOTIVO: humano")["action"] == "escalate"
    # inválido/ausente → escalar (default seguro)
    assert verdicts.parse_pm_decision(None)["action"] == "escalate"
    assert verdicts.parse_pm_decision("gibberish")["action"] == "escalate"
    assert verdicts.parse_pm_decision("DECISÃO: inventar")["action"] == "escalate"


def test_parse_story_with_markers():
    text = """## Descrição
Implementar uma calculadora simples.

## Critérios de aceite
- [ ] soma funciona
- [ ] divisão trata erro"""
    description, criteria = verdicts.parse_story(text)
    assert "calculadora" in description
    assert "## Critérios" not in description
    assert "- [ ] soma funciona" in criteria
    assert "- [ ] divisão trata erro" in criteria


def test_parse_story_without_markers():
    description, criteria = verdicts.parse_story("só uma ideia crua")
    assert description == "só uma ideia crua"
    assert criteria == ""


def test_read_and_remove_verdict(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    assert verdicts.read_verdict(str(checkout)) is None
    (checkout / verdicts.VERDICT_FILENAME).write_text("PASS\nSUMMARY: ok")
    assert verdicts.read_verdict(str(checkout)) == "PASS\nSUMMARY: ok"
    verdicts.remove_verdict(str(checkout))
    assert not (checkout / verdicts.VERDICT_FILENAME).exists()
    verdicts.remove_verdict(str(checkout))  # idempotente
