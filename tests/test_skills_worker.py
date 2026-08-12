"""Testes da integração das skills de projeto com o executor (worker).

Cobre a materialização das skills no checkout (`.autoia/skills/<nome>/` e
`.opencode/skills/<nome>/`), a exclusão do versionamento via `.git/info/exclude`,
a seção `## Skills do projeto disponíveis` no prompt persistido das fases e do
PM, e o comportamento sem skills (nada muda no fluxo atual).
"""

from __future__ import annotations

import io
import os
import stat
import zipfile

from app.models import Task
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "content": "tarefa concluída"},
]

SKILL_MD_OK = "---\nname: minha-skill\ndescription: Skill de exemplo\n---\n# Conteúdo\n"


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _upload_skill(flow, name: str = "skill.zip") -> dict:
    zip_bytes = make_zip({"SKILL.md": SKILL_MD_OK.encode(), "docs/guia.md": b"# guia\n"})
    resp = flow["client"].post(
        f"/api/repositories/{flow['task']['repository_id']}/skills",
        files={"file": (name, zip_bytes, "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _checkout(flow) -> str:
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        return os.path.join(
            flow["settings"].workspace_dir, str(t.repository.id), f"task_{t.id}"
        )


def _prompt_event(flow, position: int = 0) -> str:
    """Prompt persistido (evento `prompt`) da fase na posição dada."""
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        step = next(st for st in sorted(t.steps, key=lambda x: x.position) if st.position == position)
        prompt_ev = next(e for e in step.events if e.kind == "prompt")
        return prompt_ev.payload["prompt"]


def test_phase_materializes_skills_and_prompt_section(flow, fake_kimi):
    """Repo com skills + fase executada: `.autoia/skills/<nome>/` e
    `.opencode/skills/<nome>/` no checkout e seção `nome — descrição` no prompt."""
    settings = flow["settings"]
    skill = _upload_skill(flow)
    settings.kimi_bin = fake_kimi(HARMLESS)
    settings.task_budget = 100.0

    checkout = _checkout(flow)
    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    runner.execute_step(settings, flow["session_factory"], step_id)

    # skills materializadas para os dois executores (kimi e opencode)
    for prefix in (".autoia", ".opencode"):
        skill_dir = os.path.join(checkout, prefix, "skills", skill["name"])
        assert os.path.isfile(os.path.join(skill_dir, "SKILL.md"))
        assert os.path.isfile(os.path.join(skill_dir, "docs", "guia.md"))

    # `.autoia/` (e `.opencode/`) excluídos do versionamento — nunca no git
    exclude = open(os.path.join(checkout, ".git", "info", "exclude")).read()
    assert ".autoia/" in exclude
    assert ".opencode/" in exclude

    # prompt persistido (evento `prompt`) contém a seção com `nome — descrição`
    prompt = _prompt_event(flow, 0)
    assert "## Skills do projeto disponíveis" in prompt
    assert f"- {skill['name']} — {skill['description']}" in prompt


def test_phase_without_skills_unchanged(flow, fake_kimi):
    """Repo sem skills: fase roda sem `.autoia/` e sem a seção no prompt."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS)
    settings.task_budget = 100.0

    checkout = _checkout(flow)
    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    runner.execute_step(settings, flow["session_factory"], step_id)

    assert not os.path.exists(os.path.join(checkout, ".autoia"))
    assert "## Skills do projeto disponíveis" not in _prompt_event(flow, 0)


def test_pm_decision_prompt_has_skills_section(flow, tmp_path):
    """O PM também recebe a seção de skills no prompt da decisão."""
    skill = _upload_skill(flow)
    settings = flow["settings"]
    settings.max_pm_decisions = 2

    script = tmp_path / "fake_pm"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "idx = sys.argv.index('-p') + 1\n"
        "with open('pm_prompt.txt', 'w') as f:\n"
        "    f.write(sys.argv[idx])\n"
        "with open('autoia_verdict.txt', 'w') as f:\n"
        "    f.write('DECISÃO: escalar\\nMOTIVO: precisa de humano\\n')\n"
        "print('{\"role\":\"assistant\",\"content\":\"decisão emitida\"}')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    settings.kimi_bin = str(script)

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        t.status = "failed"
        s.commit()

    runner._pm_decide(flow["session_factory"], settings, flow["task"]["id"], "test")

    # o PM clona o checkout se necessário e roda nele (pm_prompt.txt no cwd)
    checkout = _checkout(flow)
    prompt = open(os.path.join(checkout, "pm_prompt.txt")).read()
    assert "## Skills do projeto disponíveis" in prompt
    assert f"- {skill['name']} — {skill['description']}" in prompt
    # skills também materializadas para o PM
    assert os.path.isfile(
        os.path.join(checkout, ".autoia", "skills", skill["name"], "SKILL.md")
    )
