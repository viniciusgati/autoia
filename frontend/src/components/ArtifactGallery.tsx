import { useEffect, useState } from "react";
import { api } from "../api";
import type { Artifact } from "../types";

interface Props {
  stepId: number;
}

/** Galeria de screenshots e outros arquivos gerados pelo robô na fase. */
export default function ArtifactGallery({ stepId }: Props) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [lightbox, setLightbox] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getArtifacts(stepId)
      .then((data) => {
        if (!cancelled) setArtifacts(data);
      })
      .catch(() => {
        /* sem artifacts é ok */
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [stepId]);

  if (loading) return null;
  if (artifacts.length === 0) return null;

  return (
    <>
      <h3>Screenshots da fase</h3>
      <div className="artifact-gallery">
        {artifacts.map((a) => (
          <div
            key={a.id}
            className="artifact-thumb"
            title={a.filename}
            onClick={() => setLightbox(api.getArtifactUrl(a.id))}
          >
            <img
              src={api.getArtifactUrl(a.id)}
              alt={a.filename}
              loading="lazy"
            />
            <span className="artifact-label">{a.filename}</span>
          </div>
        ))}
      </div>
      {lightbox && (
        <div className="artifact-lightbox" onClick={() => setLightbox(null)}>
          <img src={lightbox} alt="screenshot ampliado" />
        </div>
      )}
    </>
  );
}
