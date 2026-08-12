import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setOnUnauthorized } from "./api";
import type { User } from "./types";

/** Estado de autenticação do frontend.
 *
 * Boot: consulta `GET /api/auth/config`; com auth OFF vira modo "anon"
 * (`user: null`, app liberado — comportamento legado); com auth ON tenta
 * restaurar a sessão via `GET /api/auth/me` (401 → tela de Login).
 * O callback global de 401 do `api` limpa o usuário e marca `expired` quando
 * a sessão expira durante o uso. */
interface AuthContextValue {
  /** Usuário logado (null com auth OFF ou sem sessão válida). */
  user: User | null;
  /** Auth ON/OFF (config do backend). */
  authEnabled: boolean;
  /** Boot da auth em andamento (config/me). */
  loading: boolean;
  /** Sessão expirou durante o uso (401 global) — Login mostra aviso. */
  expired: boolean;
  /** Falha na tentativa de logout — a sessão é mantida e dá para tentar de novo. */
  logoutError: string | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Limpa a flag de sessão expirada (ex.: ao voltar/engajar na tela de Login). */
  clearExpired: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [expired, setExpired] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);

  // Boot: descobre se a auth está ligada e tenta restaurar a sessão.
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const cfg = await api.getAuthConfig();
        if (!active) return;
        setAuthEnabled(cfg.enabled);
        if (!cfg.enabled) {
          // Auth OFF: modo legado, sem usuário — app liberado.
          setLoading(false);
          return;
        }
        try {
          const me = await api.me();
          if (!active) return;
          setUser(me);
        } catch {
          /* 401: sem sessão válida → mostra Login (user permanece null) */
        }
        setLoading(false);
      } catch {
        // Config indisponível (rede): assume auth OFF e libera o app; se o
        // backend exigir sessão, o 401 global das primeiras chamadas leva ao Login.
        if (!active) return;
        setAuthEnabled(false);
        setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  // 401 em qualquer chamada protegida → sessão expirada durante o uso.
  const onUnauthorized = useCallback(() => {
    setUser(null);
    setExpired(true);
  }, []);

  useEffect(() => {
    setOnUnauthorized(onUnauthorized);
    return () => setOnUnauthorized(null);
  }, [onUnauthorized]);

  const login = useCallback(async (email: string, password: string) => {
    const u = await api.login(email, password);
    setUser(u);
    setExpired(false);
    setLogoutError(null);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
      setUser(null);
      setLogoutError(null);
    } catch (e) {
      // Falha no logout: mantém a sessão e expõe erro discreto com retry
      // (a própria `logout` serve de tentativa novamente).
      setLogoutError(String(e));
    }
  }, []);

  const clearExpired = useCallback(() => setExpired(false), []);

  return (
    <AuthContext.Provider
      value={{ user, authEnabled, loading, expired, logoutError, login, logout, clearExpired }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth deve ser usado dentro de <AuthProvider>");
  return ctx;
}
