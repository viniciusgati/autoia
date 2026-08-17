"""Contexto das fases no prompt: janela recente + compactação determinística.

História task 79: as N fases imediatamente anteriores (default N=1) entram no
`step_context` do prompt com o resumo INTEGRAL; as mais antigas viram 1 linha
determinística (`Fase {pos} ({robô}) [{status}] — veredicto: {v} — {trecho}`),
com linha total ≤ 200 chars e trecho ≤ 160 chars — menos tokens dinâmicos não
cacheáveis em fases tardias, sem nunca truncar o banco (`TaskStep.summary`).
"""

from __future__ import annotations

import json
import stat

import pytest

from app.models import Task, TaskStep
from app.prompts import build_prompt
from app.worker import runner

# Robôs do seed com nomes/prefixos longos (o prefixo da linha compactada chega a
# ~57 chars com `browser-tester` + `NEEDS_WORK` — trecho fixo de 160 estouraria 200).
SEED_LONG_ROBOTS = ["developer", "tester", "avaliador", "merger", "deploy-tester", "browser-tester"]

# Resumo longo (primeira linha com 3000 chars + uma segunda linha) — o formato do
# contrato: a compactação usa a 1ª linha não vazia, e o banco nunca é truncado.
def _long_summary(seed: str = "x") -> str:
    return f"{seed * 3000}\nsegunda linha do resumo"


# ---- helper _compact_phase_line (Subtarefa 1) ----

def test_compact_phase_line_multiline_long_is_single_line_within_limit():
    line = runner._compact_phase_line(1, "qa", "done", "READY", _long_summary())
    assert "\n" not in line
    assert len(line) <= 200
    assert "Fase 1 (qa) [done]" in line
    assert "veredicto: READY" in line
    # o trecho do resumo é truncado dinamicamente (≤ 160 chars no máximo)
    prefix = "Fase 1 (qa) [done] — veredicto: READY — "
    assert line.startswith(prefix)
    assert len(line) - len(prefix) <= 160
    assert line.endswith("x")  # trecho da 1ª linha do resumo


@pytest.mark.parametrize("robot_name", SEED_LONG_ROBOTS)
def test_compact_phase_line_respects_total_line_with_seed_robots(robot_name):
    # pos 6 + NEEDS_WORK = o pior caso de prefixo (~57 chars) entre os robôs do seed
    line = runner._compact_phase_line(6, robot_name, "done", "NEEDS_WORK", _long_summary("y"))
    assert "\n" not in line
    assert len(line) <= 200
    assert f"Fase 6 ({robot_name}) [done] — veredicto: NEEDS_WORK — " in line


def test_compact_phase_line_empty_summary_uses_marker():
    line = runner._compact_phase_line(2, "po", "done", "READY", "   \n  \n")
    assert line.endswith("(sem resumo)")
    assert "\n" not in line
    assert len(line) <= 200


def test_compact_phase_line_missing_verdict_uses_neutral_marker():
    line = runner._compact_phase_line(0, "po", "done", None, "resumo curto")
    assert "veredicto: —" in line
    assert line.endswith("resumo curto")


def test_compact_phase_line_collapses_whitespace_and_takes_first_line():
    summary = "   \n  primeira   linha   com   espaços  \nsegunda linha"
    line = runner._compact_phase_line(0, "po", "done", "READY", summary)
    assert "primeira linha com espaços" in line
    assert "segunda linha" not in line  # só a 1ª linha não vazia entra
    assert "\n" not in line


def test_compact_phase_line_does_not_touch_database_input():
    """O helper é puro: recebe o resumo por parâmetro e não altera nada."""
    summary = _long_summary()
    line = runner._compact_phase_line(3, "tester", "done", "PASS", summary)
    assert summary.startswith("x" * 3000)  # entrada intacta
    assert "\n" not in line


# ---- janela recente no _build_step_context (Subtarefa 2) ----

def _previous_positions(flow, current_pos: int) -> list[int]:
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        return sorted((st.position for st in t.steps if st.position < current_pos))


def _done_previous_phases(flow, current_pos: int, seed: str = "x") -> None:
    """Marca as fases anteriores a `current_pos` como done, com resumo longo e
    veredicto realista (só review/verify/assess têm veredicto no fluxo real)."""
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        for st in t.steps:
            if st.position < current_pos:
                st.status = runner.STEP_DONE
                st.summary = _long_summary(seed)
                st.verdict = {"1": "READY", "3": "PASS"}.get(str(st.position))
        s.commit()


def _build_context(flow, current_pos: int, recent_phases: int) -> str:
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        current = next(st for st in t.steps if st.position == current_pos)
        return runner._build_step_context(
            s, t, current,
            str(flow["settings"].workspace_dir), "main", f"autoia/task-{t.id}",
            recent_phases=recent_phases,
        )


def test_build_step_context_recent_window_full_and_old_compacted(flow):
    """recent_phases=1: a fase imediatamente anterior entra INTEGRAL; as antigas
    entram compactadas em 1 linha ≤ 200 chars com veredicto presente."""
    _done_previous_phases(flow, current_pos=4, seed="x")
    ctx = _build_context(flow, 4, recent_phases=1)

    # fase 3 (tester) — janela recente: resumo INTEGRAL (sem [:500])
    assert f"Fase 3 (tester): {_long_summary('x')}" in ctx

    # fases 0, 1, 2 — compactadas: exatamente 1 linha cada, ≤ 200 chars
    compacted = [line for line in ctx.splitlines() if line.startswith(("Fase 0 ", "Fase 1 ", "Fase 2 "))]
    assert len(compacted) == 3
    for line in compacted:
        assert "\n" not in line
        assert len(line) <= 200
    assert compacted[0].startswith("Fase 0 (po) [done] — veredicto: —")  # po não tem veredicto
    assert compacted[1].startswith("Fase 1 (qa) [done] — veredicto: READY —")
    assert compacted[2].startswith("Fase 2 (developer) [done] — veredicto: —")


def test_build_step_context_size_grows_with_recent_window_only(flow):
    """Crescimento dominado pela janela recente, não pelo histórico integral."""
    _done_previous_phases(flow, current_pos=4, seed="x")
    ctx = _build_context(flow, 4, recent_phases=1)
    k = len(_previous_positions(flow, 4))  # 4 fases anteriores
    assert k == 4
    assert len(ctx) < 3000 + (k - 1) * 250 + 0 + 1000  # sem diff (checkout não-git)


def test_build_step_context_recent_phases_zero_compacts_all(flow):
    _done_previous_phases(flow, current_pos=4, seed="x")
    ctx = _build_context(flow, 4, recent_phases=0)

    assert f"Fase 3 (tester): {_long_summary('x')}" not in ctx  # nada integral
    lines = [line for line in ctx.splitlines() if line.startswith("Fase ")]
    assert len(lines) == 4
    for line in lines:
        assert len(line) <= 200
        assert "\n" not in line


def test_build_step_context_recent_phases_two_full_and_rest_compacted(flow):
    _done_previous_phases(flow, current_pos=4, seed="x")
    ctx = _build_context(flow, 4, recent_phases=2)

    # as 2 mais recentes (2 e 3) integrais; 0 e 1 compactadas
    assert f"Fase 3 (tester): {_long_summary('x')}" in ctx
    assert f"Fase 2 (developer): {_long_summary('x')}" in ctx
    compacted = [line for line in ctx.splitlines() if line.startswith(("Fase 0 ", "Fase 1 "))]
    assert len(compacted) == 2
    assert all(len(line) <= 200 for line in compacted)


def test_build_step_context_later_failed_phase_keeps_full_report(flow):
    """Fase posterior que falhou (bounce-back) NÃO é compactada — relatório completo."""
    _done_previous_phases(flow, current_pos=4, seed="x")
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        failed = next(st for st in t.steps if st.position == 5)  # merger (posterior)
        failed.status = runner.STEP_FAILED
        failed.summary = "relatório completo " * 99 + "relatório completo"  # ~1900 chars, sem espaço final
        failed.error = "erro da fase posterior"
        s.commit()

    ctx = _build_context(flow, 4, recent_phases=1)

    assert "FASE POSTERIOR 5 (merger) FALHOU:" in ctx
    assert "relatório completo " * 99 + "relatório completo" in ctx  # relatório COMPLETO, sem compactação
    assert not any("Fase 5 (merger)" in line and len(line) <= 200 for line in ctx.splitlines())


def test_build_step_context_default_recent_phases_is_one(flow):
    """Chamada direta sem o parâmetro continua funcionando (default recent_phases=1)."""
    _done_previous_phases(flow, current_pos=4, seed="x")
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        current = next(st for st in t.steps if st.position == 4)
        ctx = runner._build_step_context(
            s, t, current, str(flow["settings"].workspace_dir), "main", "b"
        )
    assert f"Fase 3 (tester): {_long_summary('x')}" in ctx
    compacted = [line for line in ctx.splitlines() if line.startswith(("Fase 0 ", "Fase 1 ", "Fase 2 "))]
    assert len(compacted) == 3


def test_build_step_context_recent_phase_without_summary_omitted(flow):
    """Fase da janela recente sem resumo é OMITIDA (comportamento atual preservado)."""
    _done_previous_phases(flow, current_pos=4, seed="x")
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        next(st for st in t.steps if st.position == 3).summary = None  # janela sem resumo
        s.commit()
    ctx = _build_context(flow, 4, recent_phases=1)
    assert "Fase 3 (tester):" not in ctx


def test_build_step_context_old_phase_without_summary_shows_marker(flow):
    """Fase ANTIGA sem resumo entra compactada com o marcador `(sem resumo)`."""
    _done_previous_phases(flow, current_pos=4, seed="x")
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        next(st for st in t.steps if st.position == 0).summary = None  # antiga sem resumo
        s.commit()
    ctx = _build_context(flow, 4, recent_phases=1)
    old = next(line for line in ctx.splitlines() if line.startswith("Fase 0 "))
    assert old.endswith("(sem resumo)")
    assert len(old) <= 200


# ---- contrato do build_prompt (Subtarefa 3) ----

def test_build_prompt_embeds_compacted_step_context_and_diff(settings):
    """`build_prompt` mantém a seção `### Contexto das fases`, o diff atual e o
    resumo INTEGRAL da fase imediatamente anterior dentro do contexto compactado."""
    from app.db import make_engine, make_session_factory
    from app.main import create_app

    app = create_app(settings)
    sf = make_session_factory(make_engine(settings.database_url))
    with sf() as s:
        from app.models import Robot

        robot = s.query(Robot).filter(Robot.name == "developer").one()
        task = Task(title="t", description="d")
        recent = _long_summary("r")
        compact_step_context = "\n".join(
            [
                runner._compact_phase_line(0, "po", "done", None, "história final"),
                runner._compact_phase_line(1, "qa", "done", "READY", _long_summary("q")),
                f"Fase 2 (developer): {recent}",
                "Diff atual:\n README.md | 2 +-",
            ]
        )
        prompt = build_prompt(robot, task, compact_step_context, "main")

    assert "### Contexto das fases" in prompt
    assert "Diff atual:" in prompt
    assert "README.md | 2 +-" in prompt
    assert f"Fase 2 (developer): {recent}" in prompt  # resumo integral (3000 chars)
    assert "Fase 0 (po) [done] — veredicto: — — " in prompt  # compactada
    for line in compact_step_context.splitlines():
        assert line in prompt  # seção preserva o contexto compactado na íntegra


# ---- fluxo completo até fase tardia (Subtarefa 3) ----

def _pass_fake(tmp_path, content, dump_prompt=None) -> str:
    """Fake kimi que emite `content` e escreve o veredicto conforme o prompt
    (READY para review, PASS para verify/assess) — o conftest não tem regra `pass`."""
    script = tmp_path / f"fake_pass_{len(list(tmp_path.glob('fake_pass_*')))}"
    dump = f"open({str(dump_prompt)!r}, 'w').write(prompt)\n" if dump_prompt else ""
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        f"for line in [{json.dumps({'role': 'assistant', 'content': content})}]:\n"
        "    print(json.dumps(line))\n"
        "    sys.stdout.flush()\n"
        "import os\n"
        "prompt = sys.argv[sys.argv.index('-p') + 1] if '-p' in sys.argv else sys.argv[2]\n"
        + dump
        + "v = None\n"
        "if 'VEREDICTO' in prompt.upper() and 'PASS' in prompt:\n"
        "    v = 'PASS\\nSUMMARY: testes ok'\n"
        "elif 'VEREDICTO' in prompt.upper() and 'READY' in prompt:\n"
        "    v = 'READY\\nSUMMARY: historia ok'\n"
        "if v:\n"
        "    with open('autoia_verdict.txt', 'w') as f:\n"
        "        f.write(v)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_flow_to_late_phase_keeps_summaries_integral(flow, fake_kimi, tmp_path):
    """Pipeline padrão até a fase 4 (avaliador): todas as fases persistem o resumo
    INTEGRAL no banco e o prompt da fase tardia usa a janela + compactação."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    long_text = _long_summary("f")
    # po/developer não emitem veredicto; qa usa a regra pronta (READY); tester e
    # avaliador exigem PASS (regra custom — não existe `pass` no VERDICT_RULES).
    no_verdict = fake_kimi([{"role": "assistant", "content": long_text}])
    ready = fake_kimi([{"role": "assistant", "content": long_text}], verdict="ready_pass")
    pass_fake = _pass_fake(tmp_path, long_text, dump_prompt=tmp_path / "last_prompt.txt")
    by_position = {0: no_verdict, 1: ready, 2: no_verdict, 3: pass_fake, 4: pass_fake}

    for _ in range(5):
        step_id = runner.claim_next(flow["session_factory"])
        assert step_id is not None
        with flow["session_factory"]() as s:
            pos = s.get(TaskStep, step_id).position
        settings.kimi_bin = by_position[pos]
        runner.execute_step(settings, flow["session_factory"], step_id)

    # todas as fases concluídas com o resumo INTEGRAL (fonte de verdade do histórico)
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        for st in t.steps:
            if st.position <= 4:
                assert st.status == runner.STEP_DONE, f"fase {st.position} falhou: {st.error}"
                assert st.summary == long_text, f"fase {st.position} truncou o summary"
            else:
                assert st.status == runner.STEP_PENDING  # não chegamos ao merger

    # prompt da fase tardia (avaliador): janela recente integral + antigas compactadas
    prompt = (tmp_path / "last_prompt.txt").read_text(encoding="utf-8")
    assert "### Contexto das fases" in prompt
    assert f"Fase 3 (tester): {long_text}" in prompt  # janela recente (integral)
    for pos, robot_name in ((0, "po"), (1, "qa"), (2, "developer")):
        line = next(l for l in prompt.splitlines() if l.startswith(f"Fase {pos} ({robot_name})"))
        assert len(line) <= 200
        assert "\n" not in line
