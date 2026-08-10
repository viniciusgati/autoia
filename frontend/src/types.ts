export interface Repository {
  id: number;
  name: string;
  url: string;
  default_branch: string;
  local_path: string | null;
  created_at: string;
  // Configurações
  max_attempts: number | null;
  max_pm_decisions: number | null;
  run_timeout: number | null;
  task_budget: number | null;
  cost_per_interaction: number | null;
  risky_patterns_extra: string | null;
  db_rule: string | null;
  allow_auto_tasks: boolean;
  allow_external_tasks: boolean;
  default_pipeline_id: number | null;
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
  diff_stat: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  artifacts?: Artifact[];
}

export interface Artifact {
  id: number;
  step_id: number;
  filename: string;
  description: string | null;
  created_at: string;
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
  parent_task_id: number | null;
  children: Task[];
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
