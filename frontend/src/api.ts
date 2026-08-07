import type {
  Dashboard,
  Pipeline,
  Repository,
  Robot,
  RunEvent,
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
  listTasks: () => request<Task[]>("/api/tasks"),
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
  reviewTask: (id: number, data: { action: "approve" | "cancel"; extra_budget: number; note?: string }) =>
    request<Task>(`/api/tasks/${id}/review`, { method: "POST", body: JSON.stringify(data) }),
  retryStep: (taskId: number, position: number) =>
    request<Task>(`/api/tasks/${taskId}/steps/${position}/retry`, { method: "POST" }),
  pmDecide: (taskId: number) =>
    request<Task>(`/api/tasks/${taskId}/pm/decide`, { method: "POST" }),

  // observabilidade
  listEvents: (stepId: number, kind?: string) => {
    const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
    return request<RunEvent[]>(`/api/steps/${stepId}/events${query}`);
  },
  getLog: async (stepId: number): Promise<string> => {
    const response = await fetch(`/api/steps/${stepId}/log`);
    return response.text();
  },

  // dashboard
  getDashboard: () => request<Dashboard>("/api/dashboard"),
};
