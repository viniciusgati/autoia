import type {
  Artifact,
  Dashboard,
  Execution,
  Pipeline,
  Repository,
  Robot,
  RunEvent,
  StepDiff,
  SubTask,
  Task,
  TaskListItem,
  TaskProposal,
  TaskSummary,
  TimelineEvent,
  Workspace,
} from "./types";

/** Cache de ETag/body em memória: reenvia If-None-Match e reaproveita o corpo em 304. */
const etagCache = new Map<string, { etag: string; body: unknown }>();
const ETAG_CACHE_MAX = 200;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  const isGet = method === "GET";
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");
  const cached = etagCache.get(path);
  if (isGet && cached) headers.set("If-None-Match", cached.etag);

  const response = await fetch(path, { ...init, headers });
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
    const response = await fetch(`/api/steps/${stepId}/log`);
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
};
