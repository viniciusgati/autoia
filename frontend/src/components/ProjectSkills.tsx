import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { RepositorySkill } from "../types";

/** Mensagem de erro da API sem o prefixo de status ("400: detalhe" → "detalhe"). */
function apiErrorMsg(e: unknown): string {
  return String(e).replace(/^\d+: /, "");
}

/** Formata bytes de forma legível (B / KB / MB). */
function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

type UploadMsg = { ok: string } | { err: string } | null;

/** Seção de skills do projeto (dentro das Configurações do RepoDashboard).
 *
 *  Admin do projeto: drop zone + escolher arquivo para enviar um `.zip` com
 *  `SKILL.md` na raiz, lista com nome/descrição/nº de arquivos/tamanho, preview
 *  do `SKILL.md` num modal e exclusão com confirmação. Não-admin vê apenas o
 *  aviso "Somente admin do projeto gerencia skills" (sem formulário nem ações). */
export default function ProjectSkills({
  repoId,
  isAdmin,
}: {
  repoId: number;
  isAdmin: boolean;
}) {
  const [skills, setSkills] = useState<RepositorySkill[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadMsg, setUploadMsg] = useState<UploadMsg>(null);
  const [dragOver, setDragOver] = useState(false);
  const [preview, setPreview] = useState<RepositorySkill | null>(null);
  const [confirmSkill, setConfirmSkill] = useState<RepositorySkill | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadSkills = useCallback(
    (signal?: AbortSignal) => {
      api
        .listProjectSkills(repoId, signal)
        .then((list) => {
          setSkills(list);
          setLoadError("");
        })
        .catch((e) => setLoadError(apiErrorMsg(e)));
    },
    [repoId],
  );

  useEffect(() => {
    if (!isAdmin) return;
    let active = true;
    api
      .listProjectSkills(repoId)
      .then((list) => {
        if (active) {
          setSkills(list);
          setLoadError("");
        }
      })
      .catch((e) => active && setLoadError(apiErrorMsg(e)));
    return () => {
      active = false;
    };
  }, [repoId, isAdmin]);

  // Não-admin: apenas o aviso, sem chamada de API (listagem retorna 403).
  if (!isAdmin) {
    return (
      <div className="skill-noadmin">
        🔒 Somente admin do projeto gerencia skills
      </div>
    );
  }

  const doUpload = async (file: File) => {
    setUploading(true);
    setUploadMsg(null);
    try {
      const skill = await api.uploadProjectSkill(repoId, file);
      setUploadMsg({ ok: `Skill "${skill.name}" enviada` });
      await loadSkills();
    } catch (e) {
      setUploadMsg({ err: apiErrorMsg(e) });
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleFile = (file: File | undefined | null) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setUploadMsg({ err: "Envie um arquivo .zip" });
      return;
    }
    void doUpload(file);
  };

  const doDelete = async () => {
    if (!confirmSkill) return;
    setDeleting(confirmSkill.id);
    setDeleteError("");
    try {
      await api.deleteProjectSkill(repoId, confirmSkill.id);
      setSkills((prev) => (prev ?? []).filter((s) => s.id !== confirmSkill.id));
      setConfirmSkill(null);
    } catch (e) {
      setDeleteError(apiErrorMsg(e));
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="skill-section">
      {/* Upload */}
      <div
        className={`skill-dropzone ${uploading ? "disabled" : ""} ${dragOver ? "dragging" : ""}`}
        onClick={() => !uploading && fileInputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          if (!uploading) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (uploading) return;
          handleFile(e.dataTransfer.files?.[0]);
        }}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept=".zip"
          disabled={uploading}
          onChange={(e) => handleFile(e.target.files?.[0])}
        />
        {uploading ? (
          <span className="skill-uploading">
            <span className="spinner" /> Enviando…
          </span>
        ) : (
          <>
            Arraste um .zip com <code>SKILL.md</code> na raiz, ou{" "}
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                fileInputRef.current?.click();
              }}
            >
              escolher arquivo
            </button>
          </>
        )}
      </div>

      {uploadMsg && "ok" in uploadMsg && (
        <div className="skill-upload-msg ok">✓ {uploadMsg.ok}</div>
      )}
      {uploadMsg && "err" in uploadMsg && (
        <div className="skill-upload-msg err">{uploadMsg.err}</div>
      )}

      {/* Lista / estados */}
      {loadError ? (
        <div className="step-error" style={{ marginTop: 10 }}>
          {loadError}
        </div>
      ) : skills == null ? (
        <div className="muted small" style={{ marginTop: 10 }}>
          carregando skills…
        </div>
      ) : skills.length === 0 ? (
        <div className="skill-empty">
          <div>
            <strong>Nenhuma skill configurada</strong> — envie um .zip com SKILL.md
          </div>
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
          >
            enviar primeira skill
          </button>
        </div>
      ) : (
        <div className="skill-list">
          {skills.map((s) => (
            <div className="skill-card" key={s.id}>
              <div className="skill-card-main">
                <div className="skill-card-name">{s.name}</div>
                {s.description && (
                  <div className="skill-card-desc">{s.description}</div>
                )}
              </div>
              <span className="skill-card-meta">
                {s.file_count} arquivo{s.file_count === 1 ? "" : "s"} · {fmtBytes(s.size_bytes)}
              </span>
              <div className="skill-card-actions">
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => setPreview(s)}
                >
                  Ver
                </button>
                <button
                  type="button"
                  className="link-btn danger-link"
                  onClick={() => {
                    setDeleteError("");
                    setConfirmSkill(s);
                  }}
                >
                  Excluir
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Preview do SKILL.md */}
      {preview && (
        <SkillPreviewModal
          repoId={repoId}
          skill={preview}
          onClose={() => setPreview(null)}
        />
      )}

      {/* Confirmação de exclusão */}
      {confirmSkill && (
        <div
          className="modal-overlay"
          onClick={() => deleting == null && setConfirmSkill(null)}
        >
          <div
            className="modal skill-confirm-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-head">
              <strong>Excluir skill</strong>
            </div>
            <div className="modal-body">
              <p>
                Excluir a skill '{confirmSkill.name}'? Os arquivos serão removidos
                do projeto e não é possível desfazer.
              </p>
              {deleteError && (
                <div className="skill-upload-msg err">{deleteError}</div>
              )}
            </div>
            <div className="modal-foot">
              <button
                className="danger"
                onClick={doDelete}
                disabled={deleting !== null}
              >
                {deleting === confirmSkill.id ? "Excluindo…" : "Excluir"}
              </button>
              <button
                onClick={() => setConfirmSkill(null)}
                disabled={deleting !== null}
              >
                Cancelar
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** Modal com o preview do `SKILL.md` (spinner enquanto carrega; erro com fechar). */
function SkillPreviewModal({
  repoId,
  skill,
  onClose,
}: {
  repoId: number;
  skill: RepositorySkill;
  onClose: () => void;
}) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api
      .getProjectSkillFile(repoId, skill.id)
      .then((text) => active && setContent(text))
      .catch((e) => active && setError(apiErrorMsg(e)));
    return () => {
      active = false;
    };
  }, [repoId, skill.id]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal skill-preview-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong>{skill.name}</strong>
          <button className="link-btn" onClick={onClose}>
            fechar
          </button>
        </div>
        <div className="modal-body">
          {error ? (
            <>
              <div className="step-error">{error}</div>
              <button onClick={onClose}>fechar</button>
            </>
          ) : content == null ? (
            <div className="muted">
              <span className="spinner" /> carregando SKILL.md…
            </div>
          ) : (
            <pre className="skill-preview">{content}</pre>
          )}
        </div>
      </div>
    </div>
  );
}
