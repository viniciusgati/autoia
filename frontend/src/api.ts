import type {
  Artifact,
  Dashboard,
  Execution,
  MyProject,
  MyTask,
  Pipeline,
  Repository,
  RepositoryMember,
  RepositorySkill,
  Robot,
  RunEvent,
  StepDiff,
  SubTask,
  Task,
  TaskListItem,
  TaskProposal,
  TaskSummary,
  TimelineEvent,
  User,
  Workspace,
} from "./types";

/** Cache de ETag/body em memória: reenvia If-None-Match e reaproveita o corpo em 304. */
const etagCache = new Map<string, { etag: string; body: unknown }>();
const ETAG_CACHE_MAX = 200;

/** Callback global de sessão expirada (401 em qualquer rota protegida) —
 *  registrado pelo `AuthProvider` via `setOnUnauthorized`. */
let onUnauthorized: (() => void) | null = null;

/** Registra o tratador global de 401 (limpe com `setOnUnauthorized(null)`). */
export function setOnUnauthorized(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

/** Rotas cujo 401 NÃO representa sessão expirada: login = credencial inválida,
 *  `me`/`auth/config` = parte do boot da auth (o próprio fluxo trata). */
const NO_UNAUTHORIZED_CALLBACK = new Set([
  "/api/auth/login",
  "/api/auth/me",
  "/api/auth/config",
]);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  const isGet = method === "GET";
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");
  const cached = etagCache.get(path);
  if (isGet && cached) headers.set("If-None-Match", cached.etag);

  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (response.status === 401 && !NO_UNAUTHORIZED_CALLBACK.has(path)) {
    // Sessão inválida/expirada durante o uso → avisa o AuthProvider (Login).
    onUnauthorized?.();
  }
  if (response.status === 304 && cached) {
    return cached.body as T;
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* resposta não-JSON */
    }
    throw new Error(`${response.status}: ${detail}`);
  }
  if (response.status === 204) return undefined as T;
  const body = (await response.json()) as T;
  if (isGet) {
    const etag = response.headers.get("etag");
    if (etag) {
      if (etagCache.size >= ETAG_CACHE_MAX) etagCache.clear();
      etagCache.set(path, { etag, body });
    }
  }
  return body;
}

export const api = {
  // auth (cookie autoia_session)
  getAuthConfig: () => request<{ enabled: boolean }>("/api/auth/config"),
  login: (email: string, password: string) =>
    request<User>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  me: () => request<User>("/api/auth/me"),
  // Bootstrap: só aceito com `users` vazio (primeiro registro vira admin global).
  register: (data: { name: string; email: string; password: string }) =>
    request<User>("/api/auth/register", { method: "POST", body: JSON.stringify(data) }),

  // usuários (admin global)
  listUsers: () => request<User[]>("/api/users"),
  createUser: (data: { name: string; email: string; password: string; role?: string }) =>
    request<User>("/api/users", { method: "POST", body: JSON.stringify(data) }),
  updateUser: (id: number, data: { name?: string; email?: string; password?: string; role?: string; active?: boolean }) =>
    request<User>(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(data) }),

  // dashboard pessoal
  getMyTasks: (signal?: AbortSignal) => request<MyTask[]>("/api/me/tasks", { signal }),
  getMyProjects: (signal?: AbortSignal) => request<MyProject[]>("/api/me/projects", { signal }),

  // membros do projeto + atribuição de responsável
  listMembers: (repoId: number, signal?: AbortSignal) =>
    request<RepositoryMember[]>(`/api/repositories/${repoId}/members`, { signal }),
  assignResponsible: (taskId: number, userId: number) =>
    request<Task>(`/api/tasks/${taskId}/responsible`, {
      method: "PUT",
      body: JSON.stringify({ user_id: userId }),
    }),

  // skills do projeto (admin do projeto; upload .zip com SKILL.md na raiz)
  listProjectSkills: (repoId: number, signal?: AbortSignal) =>
    request<RepositorySkill[]>(`/api/repositories/${repoId}/skills`, { signal }),
  uploadProjectSkill: async (repoId: number, file: File): Promise<RepositorySkill> => {
    // fetch próprio sem Content-Type manual: o browser define o boundary do multipart
    // (o helper `request` força `Content-Type: application/json`).
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(`/api/repositories/${repoId}/skills`, {
      method: "POST",
      body: formData,
      credentials: "same-origin",
    });
    if (response.status === 401) onUnauthorized?.();
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail ?? JSON.stringify(body);
      } catch {
        /* resposta não-JSON */
      }
      throw new Error(`${response.status}: ${detail}`);
    }
    return (await response.json()) as RepositorySkill;
  },
  deleteProjectSkill: (repoId: number, skillId: number) =>
    request<void>(`/api/repositories/${repoId}/skills/${skillId}`, { method: "DELETE" }),
  getProjectSkillFile: async (repoId: number, skillId: number): Promise<string> => {
    const response = await fetch(`/api/repositories/${repoId}/skills/${skillId}/file`, {
      credentials: "same-origin",
    });
    if (response.status === 401) onUnauthorized?.();
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail ?? JSON.stringify(body);
      } catch {
        /* resposta não-JSON */
      }
      throw new Error(`${response.status}: ${detail}`);
    }
    return response.text();
  },

  // repositories
  listRepositories: (signal?: AbortSignal) => request<Repository[]>("/api/repositories", { signal }),
  createRepository: (data: { name: string; url: string; default_branch: string }) =>
    request<Repository>("/api/repositories", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  deleteRepository: (id: number) =>
    request<void>(`/api/repositories/${id}`, { method: "DELETE" }),
  updateRepository: (id: number, data: Partial<Repository>) =>
    request<Repository>(`/api/repositories/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  // robots
  listRobots: (repositoryId?: number) => {
    const params = repositoryId != null ? `?repository_id=${repositoryId}` : "";
    return request<Robot[]>(`/api/robots${params}`);
  },  createRobot: (data: { name: string; mission: string; role?: string; model?: string; repository_id?: number | null }) =>
    request<Robot>("/api/robots", { method: "POST", body: JSON.stringify(data) }),
  updateRobot: (id: number, data: Partial<Robot>) =>
    request<Robot>(`/api/robots/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteRobot: (id: number) =>
    request<void>(`/api/robots/${id}`, { method: "DELETE" }),

  // pipelines
  listPipelines: (repositoryId?: number, signal?: AbortSignal) => {
    const params = repositoryId != null ? `?repository_id=${repositoryId}` : "";
    return request<Pipeline[]>(`/api/pipelines${params}`, { signal });
  },
  createPipeline: (data: { name: string; steps: { position: number; robot_id: number; post_merge?: boolean; pause_before?: boolean }[]; repository_id?: number | null }) =>
    request<Pipeline>("/api/pipelines", { method: "POST", body: JSON.stringify(data) }),
  deletePipeline: (id: number) =>
    request<void>(`/api/pipelines/${id}`, { method: "DELETE" }),

  // tasks
  listTasks: (repositoryId?: number, signal?: AbortSignal) => {
    const params = repositoryId != null ? `?repository_id=${repositoryId}` : "";
    return request<TaskListItem[]>(`/api/tasks${params}`, { signal });
  },
  getTask: (id: number, signal?: AbortSignal) => request<Task>(`/api/tasks/${id}`, { signal }),
  createTask: (data: {
    repository_id: number;
    pipeline_id: number;
    title: string;
    description: string;
    kind: string;
    executor?: string;
    budget_limit?: number;
  }) => request<Task>("/api/tasks", { method: "POST", body: JSON.stringify(data) }),
  startTask: (id: number) => request<Task>(`/api/tasks/${id}/start`, { method: "POST" }),
  pauseTask: (id: number) => request<Task>(`/api/tasks/${id}/pause`, { method: "POST" }),
  resumeTask: (id: number) => request<Task>(`/api/tasks/${id}/resume`, { method: "POST" }),
  cancelTask: (id: number) => request<Task>(`/api/tasks/${id}/cancel`, { method: "POST" }),
  deleteTask: (id: number) =>
    request<void>(`/api/tasks/${id}`, { method: "DELETE" }),
  reviewTask: (id: number, data: { action: "approve" | "cancel"; extra_budget: number; note?: string }) =>
    request<Task>(`/api/tasks/${id}/review`, { method: "POST", body: JSON.stringify(data) }),
  retryStep: (taskId: number, position: number, note?: string) =>
    request<Task>(`/api/tasks/${taskId}/steps/${position}/retry`, {
      method: "POST",
      body: note ? JSON.stringify({ note }) : undefined,
    }),
  setFeedback: (taskId: number, text: string) =>
    request<Task>(`/api/tasks/${taskId}/feedback`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  clearFeedback: (taskId: number) =>
    request<Task>(`/api/tasks/${taskId}/feedback`, { method: "DELETE" }),
  pmDecide: (taskId: number) =>
    request<Task>(`/api/tasks/${taskId}/pm/decide`, { method: "POST" }),
  bouncebackTask: (taskId: number, targetPosition: number, note?: string, reviewedBy?: string) =>
    request<Task>(`/api/tasks/${taskId}/bounceback`, {
      method: "POST",
      body: JSON.stringify({ target_position: targetPosition, note, reviewed_by: reviewedBy || "humano" }),
    }),
  approveStep: (taskId: number, position: number, note?: string) =>
    request<Task>(`/api/tasks/${taskId}/approve-step`, {
      method: "POST",
      body: JSON.stringify({ position, note }),
    }),
  updateTaskStory: (taskId: number, data: { description?: string; acceptance_criteria?: string | null; details?: string | null }) =>
    request<Task>(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  // resumo do desenvolvimento (LLM dedicada)
  getTaskSummary: (taskId: number) => request<TaskSummary | null>(`/api/tasks/${taskId}/summary`),
  regenerateSummary: (taskId: number) =>
    request<TaskSummary | null>(`/api/tasks/${taskId}/summary/regenerate`, { method: "POST" }),

  // timeline cronológica da execução
  getTaskTimeline: (taskId: number, signal?: AbortSignal) =>
    request<TimelineEvent[]>(`/api/tasks/${taskId}/timeline`, { signal }),

  // workspace (tela de trabalho)
  getWorkspace: (taskId: number, signal?: AbortSignal) =>
    request<Workspace>(`/api/tasks/${taskId}/workspace`, { signal }),
  getStepDiff: (taskId: number, position: number) =>
    request<StepDiff>(`/api/tasks/${taskId}/steps/${position}/diff`),
  sendInstruction: (taskId: number, data: { instruction: string; position?: number }) =>
    request<Task>(`/api/tasks/${taskId}/instruction`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  // bloqueio + retomada por instrução
  continueBlocked: (taskId: number, instruction: string) =>
    request<Task>(`/api/tasks/${taskId}/blocked/continue`, {
      method: "POST",
      body: JSON.stringify({ instruction }),
    }),

  // propostas de tasks filhas (aprovação humana)
  listProposals: (taskId: number) => request<TaskProposal[]>(`/api/tasks/${taskId}/proposals`),
  acceptProposal: (taskId: number, proposalId: number) =>
    request<Task>(`/api/tasks/${taskId}/proposals/${proposalId}/accept`, { method: "POST" }),
  rejectProposal: (taskId: number, proposalId: number) =>
    request<Task>(`/api/tasks/${taskId}/proposals/${proposalId}/reject`, { method: "POST" }),

  // subtasks
  retrySubtask: (taskId: number, position: number) =>
    request<SubTask>(`/api/tasks/${taskId}/subtasks/${position}/retry`, {
      method: "POST",
    }),

  // observabilidade
  listEvents: (stepId: number, kind?: string, order?: "asc" | "desc", signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (kind) params.set("kind", kind);
    if (order) params.set("order", order);
    const query = params.toString() ? `?${params}` : "";
    return request<RunEvent[]>(`/api/steps/${stepId}/events${query}`, { signal });
  },
  getLog: async (stepId: number): Promise<string> => {
    const response = await fetch(`/api/steps/${stepId}/log`, { credentials: "same-origin" });
    return response.text();
  },

  // dashboard
  getDashboard: (repositoryId?: number, signal?: AbortSignal) => {
    const params = repositoryId != null ? `?repository_id=${repositoryId}` : "";
    return request<Dashboard>(`/api/dashboard${params}`, { signal });
  },

  // execução (página global)
  getExecution: (repositoryId?: number, signal?: AbortSignal) => {
    const params = repositoryId != null ? `?repository_id=${repositoryId}` : "";
    return request<Execution>(`/api/execution${params}`, { signal });
  },

  // worker
  getWorkerStatus: (signal?: AbortSignal) =>
    request<{ alive: boolean; last_heartbeat_sec: number | null }>("/api/worker/status", { signal }),

  // artifacts
  getArtifacts: (stepId: number) => request<Artifact[]>(`/api/steps/${stepId}/artifacts`),
  getArtifactUrl: (artifactId: number) => `/api/steps/artifacts/${artifactId}/file`,
  deleteArtifacts: (stepId: number) =>
    request<{ deleted: number }>(`/api/steps/${stepId}/artifacts`, { method: "DELETE" }),

  // tratador global de 401 (sessão expirada)
  setOnUnauthorized,
};
