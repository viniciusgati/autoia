import type { ReactNode } from "react";

/** "?" que mostra tooltip ao passar o mouse. */
export default function HelpTip({ children }: { children: ReactNode }) {
  return (
    <span className="help-tip" tabIndex={0}>
      <span className="help-tip-icon">?</span>
      <span className="help-tip-text">{children}</span>
    </span>
  );
}
