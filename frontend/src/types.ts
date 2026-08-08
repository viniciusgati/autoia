export interface Repository {
  id: number;
  name: string;
  url: string;
  default_branch: string;
  local_path: string | null;
  created_at: string;
}

export interface Robot {
  id: number;
  name: string;
  mission: string;
  role: string;
  model: string | null;
  active: boolean;
  created_at: string;
}

export interface PipelineStep {
  id: number;
  position: number;
  robot_id: number;
  post_merge: boolean;
  robot: Robot | null;
}

export interface Pipeline {
  id: number;
  name: string;
  steps: PipelineStep[];
  created_at: string;
}

export interface TaskStep {
  id: number;
  position: number;
  robot: Robot | null;
  status: string;
  attempt: number;
  verdict: string | null;
  post_merge: boolean;
  log_path: string | null;
  summary: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface SubTask {
  id: number;
  position: number;
  title: string;
  description: string;
  acceptance_criteria: string | null;
  status: string;
  attempt: number;
  summary: string | null;
  verdict: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Task {
  id: number;
  repository_id: number;
  pipeline_id: number;
  title: string;
  description: string;
  kind: string;
  status: string;
  current_step: number;
  branch: string | null;
  acceptance_criteria: string | null;
  budget_limit: number;
  cost_spent: number;
  pm_decisions: number;
  feedback: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  steps: TaskStep[];
  subtasks: SubTask[];
}

export interface RunEvent {
  id: number;
  step_id: number;
  seq: number;
  ts: string;
  kind: string;
  payload: Record<string, unknown>;
  cost: number;
}

export interface Notice {
  task_id: number;
  task_title: string;
  task_status: string;
  level: "critical" | "warning";
  kind: string;
  message: string;
  ts: string;
}

export interface Dashboard {
  tasks_by_status: Record<string, number>;
  total_cost: number;
  total_tasks: number;
  guardrail_events: number;
  recent_guardrails: RunEvent[];
  notices: Notice[];
}
