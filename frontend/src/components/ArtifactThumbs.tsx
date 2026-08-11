import { useState } from "react";
import { api } from "../api";
import type { Artifact } from "../types";

/** Thumbs dos screenshots/arquivos gerados pelo robô numa fase — clique expande.
 *  Usado no Resumo (Nível 1): 1 imagem vale mais que mil palavras. */
export default function ArtifactThumbs({
  artifacts,
  label,
}: {
  artifacts: Artifact[];
  label?: string;
}) {
  const [lightbox, setLightbox] = useState<string | null>(null);

  if (!artifacts || artifacts.length === 0) return null;

  return (
    <div className="artifact-thumbs">
      {label && <div className="form-label">{label}</div>}
      <div className="artifact-gallery">
        {artifacts.map((a) => (
          <div
            key={a.id}
            className="artifact-thumb"
            title={a.filename}
            onClick={() => setLightbox(api.getArtifactUrl(a.id))}
          >
            <img src={api.getArtifactUrl(a.id)} alt={a.filename} loading="lazy" />
            <span className="artifact-label">{a.filename}</span>
          </div>
        ))}
      </div>
      {lightbox && (
        <div className="artifact-lightbox" onClick={() => setLightbox(null)}>
          <img src={lightbox} alt="screenshot ampliado" />
        </div>
      )}
    </div>
  );
}
