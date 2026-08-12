import { useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";

/** Tela de login (auth ON). Três estados:
 *  (a) submit → botão "entrar" desabilitado com indicador de carregamento;
 *  (b) credencial inválida (401 do login) → erro inline "E-mail ou senha
 *      inválidos", mantendo o e-mail preenchido e o foco no campo de e-mail;
 *  (c) sucesso → navega para `/`.
 *  Com `expired` (sessão expirada durante o uso), mostra o aviso antes do form. */
export default function Login() {
  const { login, expired, clearExpired } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const emailRef = useRef<HTMLInputElement>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !password || busy) return;
    setBusy(true);
    setError("");
    clearExpired();
    try {
      await login(email.trim(), password);
      navigate("/");
    } catch (err) {
      // `err` é um `Error("401: ...")` — usa `.message` (sem o prefixo "Error: ")
      // para o teste de prefixo funcionar; 401 do login = credencial inválida
      // (não dispara o callback global de sessão).
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg.startsWith("401") ? "E-mail ou senha inválidos" : msg);
      emailRef.current?.focus();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">autoia</div>
        <h2 className="login-title">Entrar</h2>

        {expired && <div className="login-warning">Sessão expirada, entre novamente</div>}
        {error && <div className="login-error">{error}</div>}

        <div className="form-field">
          <label className="form-label">E-mail</label>
          <input
            ref={emailRef}
            type="email"
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
              clearExpired();
            }}
            autoComplete="username"
            autoFocus
            required
          />
        </div>
        <div className="form-field">
          <label className="form-label">Senha</label>
          <input
            type="password"
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
              clearExpired();
            }}
            autoComplete="current-password"
            required
          />
        </div>

        <button className="login-submit" type="submit" disabled={busy}>
          {busy ? "entrando…" : "entrar"}
        </button>
      </form>
    </div>
  );
}
