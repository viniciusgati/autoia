import type {
  Artifact,
  Dashboard,
  Pipeline,
  Repository,
  Robot,
  RunEvent,
  SubTask,
  Task,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
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
  return response.json() as Promise<T>;
}

export const api = {
  // repositories
  listRepositories: () => request<Repository[]>("/api/repositories"),
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
  listRobots: () => request<Robot[]>("/api/robots"),
  createRobot: (data: { name: string; mission: string; model?: string }) =>
    request<Robot>("/api/robots", { method: "POST", body: JSON.stringify(data) }),
  updateRobot: (id: number, data: Partial<Robot>) =>
    request<Robot>(`/api/robots/${id}`, { method: "PUT", body: JSON.stringify(data) }),

  // pipelines
  listPipelines: () => request<Pipeline[]>("/api/pipelines"),
  createPipeline: (data: { name: string; steps: { position: number; robot_id: number }[] }) =>
    request<Pipeline>("/api/pipelines", { method: "POST", body: JSON.stringify(data) }),

  // tasks
  listTasks: (repositoryId?: number) => {
    const params = repositoryId != null ? `?repository_id=${repositoryId}` : "";
    return request<Task[]>(`/api/tasks${params}`);
  },
  getTask: (id: number) => request<Task>(`/api/tasks/${id}`),
  createTask: (data: {
    repository_id: number;
    pipeline_id: number;
    title: string;
    description: string;
    kind: string;
    budget_limit?: number;
  }) => request<Task>("/api/tasks", { method: "POST", body: JSON.stringify(data) }),
  startTask: (id: number) => request<Task>(`/api/tasks/${id}/start`, { method: "POST" }),
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

  // subtasks
  retrySubtask: (taskId: number, position: number) =>
    request<SubTask>(`/api/tasks/${taskId}/subtasks/${position}/retry`, {
      method: "POST",
    }),

  // observabilidade
  listEvents: (stepId: number, kind?: string, order?: "asc" | "desc") => {
    const params = new URLSearchParams();
    if (kind) params.set("kind", kind);
    if (order) params.set("order", order);
    const query = params.toString() ? `?${params}` : "";
    return request<RunEvent[]>(`/api/steps/${stepId}/events${query}`);
  },
  getLog: async (stepId: number): Promise<string> => {
    const response = await fetch(`/api/steps/${stepId}/log`);
    return response.text();
  },

  // dashboard
  getDashboard: (repositoryId?: number) => {
    const params = repositoryId != null ? `?repository_id=${repositoryId}` : "";
    return request<Dashboard>(`/api/dashboard${params}`);
  },

  // worker
  getWorkerStatus: () => request<{ alive: boolean; last_heartbeat_sec: number | null }>("/api/worker/status"),

  // artifacts
  getArtifacts: (stepId: number) => request<Artifact[]>(`/api/steps/${stepId}/artifacts`),
  getArtifactUrl: (artifactId: number) => `/api/steps/artifacts/${artifactId}/file`,
  deleteArtifacts: (stepId: number) =>
    request<{ deleted: number }>(`/api/steps/${stepId}/artifacts`, { method: "DELETE" }),
};
