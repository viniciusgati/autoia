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
  listRobots: (repositoryId?: number) => {
    const params = repositoryId != null ? `?repository_id=${repositoryId}` : "";
    return request<Robot[]>(`/api/robots${params}`);
  },
  createRobot: (data: { name: string; mission: string; role?: string; model?: string; repository_id?: number | null }) =>
    request<Robot>("/api/robots", { method: "POST", body: JSON.stringify(data) }),
  updateRobot: (id: number, data: Partial<Robot>) =>
    request<Robot>(`/api/robots/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteRobot: (id: number) =>
    request<void>(`/api/robots/${id}`, { method: "DELETE" }),

  // pipelines
  listPipelines: (repositoryId?: number) => {
    const params = repositoryId != null ? `?repository_id=${repositoryId}` : "";
    return request<Pipeline[]>(`/api/pipelines${params}`);
  },
  createPipeline: (data: { name: string; steps: { position: number; robot_id: number; post_merge?: boolean; pause_before?: boolean }[]; repository_id?: number | null }) =>
    request<Pipeline>("/api/pipelines", { method: "POST", body: JSON.stringify(data) }),
  deletePipeline: (id: number) =>
    request<void>(`/api/pipelines/${id}`, { method: "DELETE" }),

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
    executor?: string;
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
  approveStep: (taskId: number, position: number, note?: string) =>
    request<Task>(`/api/tasks/${taskId}/approve-step`, {
      method: "POST",
      body: JSON.stringify({ position, note }),
    }),
  updateTaskStory: (taskId: number, data: { description?: string; acceptance_criteria?: string | null }) =>
    request<Task>(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
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
