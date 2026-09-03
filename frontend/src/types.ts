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
  auto_summary: boolean;
  /** Modo de sandbox de execução do projeto: null (herda global) | "off" | "fs" | "full". */
  sandbox: string | null;
  /** Repositórios onde este projeto pode criar tarefas (allowlist de saída;
   *  vazio = restritivo, só o próprio projeto). */
  task_targets: string[];
  /** Informações úteis injetadas no contexto dos robôs (DNS de deploy, URLs, env). */
  external_context: string | null;
}

/** Informações para o diálogo de confirmação de exclusão do projeto
 *  (GET /api/repositories/{id}/delete-info). */
export interface RepositoryDeleteInfo {
  active_tasks: number;
  checkout_path: string | null;
}

export interface Robot {
  id: number;
  repository_id: number | null;
  name: string;
  mission: string;
  role: string;
  model: string | null;
  active: boolean;
  archived: boolean;
  created_at: string;
}

/** Usuário do sistema (auth ON). */
export interface User {
  id: number;
  name: string;
  email: string;
  role: "admin" | "member";
  active: boolean;
  created_at: string;
}

/** Participação de um usuário em um projeto (GET /api/repositories/{id}/members). */
export interface RepositoryMember {
  id: number;
  repository_id: number;
  user_id: number;
  role: string;
  created_at: string;
  user: User | null;
}

/** Skill de projeto: metadados do upload de `.zip` com `SKILL.md` na raiz.
 *  Os arquivos ficam em `data/skills/<repository_id>/<skill_id>/`; este payload
 *  alimenta a lista/feedback da UI (nome, descrição do frontmatter, nº de
 *  arquivos e tamanho total). */
export interface RepositorySkill {
  id: number;
  repository_id: number;
  name: string;
  description: string;
  file_count: number;
  size_bytes: number;
  created_at: string;
}

/** Tarefa do usuário no dashboard pessoal (GET /api/me/tasks). */
export interface MyTask {
  id: number;
  repository_id: number;
  repository_name: string;
  title: string;
  status: string;
  cost_spent: number;
  budget_limit: number;
  updated_at: string;
}

/** Participação do usuário em um projeto (GET /api/me/projects). */
export interface MyProject {
  id: number;
  name: string;
  role: string;
  my_tasks_total: number;
  my_tasks_active: number;
  my_tasks_pending: number;
}

export interface PipelineStep {
  id: number;
  position: number;
  robot_id: number;
  post_merge: boolean;
  pause_before: boolean;
  robot: Robot | null;
}

export interface Pipeline {
  id: number;
  repository_id: number | null;
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
  pause_before: boolean;
  log_path: string | null;
  summary: string | null;
  diff_stat: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  artifacts?: Artifact[];
  /** Modo de execução desta fase: null (herda a task) | "auto" | "manual". */
  execution_mode?: string | null;
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

export interface TaskProposal {
  id: number;
  task_id: number;
  step_id: number | null;
  position: number;
  title: string;
  description: string;
  kind: string;
  repository_id: number | null;
  target_repository_id: number | null;
  pipeline_id: number | null;
  status: "pending" | "accepted" | "rejected";
  created_at: string;
  accepted_task_id: number | null;
}

export interface TaskSummary {
  id: number;
  task_id: number;
  summary: string;
  request: string | null;
  implementation: string | null;
  changes: string[];
  result: "completed" | "partial" | "failed" | "pending" | null;
  issues: string[];
  files: string[];
  tasks_summary: string | null;
  model: string | null;
  created_at: string;
}

/** Evento da timeline cronológica de execução (resumo determinístico, sem LLM). */
export interface TimelineEvent {
  seq: number;
  ts: string;
  type: string;
  name: string;
  summary: string;
  status: string | null;
  duration_ms: number | null;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  raw: { kind: string; payload: Record<string, unknown> };
  step_id: number | null;
  step_position: number | null;
  step_robot: string | null;
  step_role: string | null;
}

/** Executores das fases (CLIs de robô LLM): kimi-code, opencode ou codex. */
export type Executor = "kimi" | "opencode" | "codex";

export interface Task {
  id: number;
  repository_id: number;
  pipeline_id: number;
  title: string;
  description: string;
  kind: string;
  status: string;
  executor: string;
  /** Modelo do executor escolhido na task (null = herda robô/default do executor). */
  model: string | null;
  current_step: number;
  branch: string | null;
  acceptance_criteria: string | null;
  budget_limit: number;
  cost_spent: number;
  pm_decisions: number;
  feedback: string | null;
  error: string | null;
  details: string | null;
  resume_instruction: string | null;
  block_reason_type: string | null;
  block_reason: string | null;
  block_question: string | null;
  block_options?: string[];
  summary: TaskSummary | null;
  created_at: string;
  updated_at: string;
  responsible_id: number | null;
  responsible: User | null;
  /** Modo de execução: "auto" (pipeline) | "manual" (human-in-the-loop). */
  mode: string;
  /** Ação de chat pendente no modo manual (null = aguardando o humano). */
  pending_action: string | null;
  /** Estado da ação de chat: "idle" | "queued" | "running". */
  chat_status: string;
  /** Associação organizacional Projeto > Épico (0..1 cada; null = sem associação). */
  project_id: number | null;
  epic_id: number | null;
  steps: TaskStep[];
  subtasks: SubTask[];
  proposals: TaskProposal[];
  parent_task_id: number | null;
  children: Task[];
}

/** Fase no payload "lean" de listas: sem o texto integral do resumo (só preview). */
export interface TaskStepListItem {
  id: number;
  position: number;
  robot: Robot | null;
  status: string;
  attempt: number;
  verdict: string | null;
  post_merge: boolean;
  pause_before: boolean;
  diff_stat: string | null;
  /** Preview do resumo (presente apenas na listagem lean; o completo é `summary`). */
  summary_preview?: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** Listagem leve de tasks p/ grids/dashboard (sem resumo LLM, children, propostas). */
export interface TaskListItem {
  id: number;
  repository_id: number;
  pipeline_id: number;
  title: string;
  kind: string;
  status: string;
  executor: string;
  model: string | null;
  current_step: number;
  budget_limit: number;
  cost_spent: number;
  pm_decisions: number;
  error: string | null;
  created_at: string;
  updated_at: string;
  parent_task_id: number | null;
  responsible_id: number | null;
  responsible: User | null;
  project_id: number | null;
  epic_id: number | null;
  steps: TaskStepListItem[];
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
  repository_id: number;
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
  // Propostas de tasks filhas aguardando decisão humana (aceitas seguem visíveis).
  proposals: TaskProposal[];
  // Dashboard pessoal (auth ON): usuário logado, tarefas dele e participações.
  user: User | null;
  my_tasks: MyTask[];
  projects: MyProject[];
}

export interface WorkerStatus {
  alive: boolean;
  last_heartbeat_sec: number | null;
}

/** Payload da página global "Execução" (GET /api/execution). */
export interface Execution {
  tasks: TaskListItem[];
  current_events: Record<string, RunEvent[]>;
  proposals: TaskProposal[];
  notices: Notice[];
  worker: WorkerStatus;
}

/** Resumo de UMA execução de fase ("O que foi entregue") gerado por LLM dedicada. */
export interface StepSummary {
  id: number;
  step_id: number;
  position: number;
  attempt: number;
  summary: string;
  changes: string[];
  result: "completed" | "partial" | "failed" | "pending" | null;
  issues: string[];
  files: string[];
  created_at: string;
}

/** Uma execução de fase na timeline do workspace (histórico imutável). */
export interface WorkspaceOccurrence {
  step_id: number;
  position: number;
  robot: { name: string; role: string } | null;
  attempt: number;
  run: number;
  is_rerun: boolean;
  status: string;
  goal: string | null;
  /** Missão desta execução ("por que esta execução existe") — humano, não é prompt. */
  mission: string | null;
  /** "llm" (LLM dedicada) ou "fallback" (derivado deterministicamente dos eventos). */
  mission_source: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** Duração total desta execução em ms (dos timestamps dos eventos; null se incompleta). */
  duration_ms: number | null;
  /** Custo acumulado desta execução (USD; kimi estimado, opencode real). */
  cost: number;
  last_activity: string | null;
  delivered_text: string | null;
  delivered: StepSummary | null;
  stop: { kind: string; reason: string; detail?: string } | null;
  proposals: TaskProposal[];
  files: string[];
  file_count: number;
  tests: { passed: number | null; failed: number | null; verdict: string | null } | null;
  system_activity: { ts: string; type: string; name: string; summary: string; status: string | null }[];
  events: TimelineEvent[];
  /** Branch onde vive a alteração (pre-merge: task; pós-merge: default do repo). */
  branch: string | null;
}

/** Pedido de decisão do agente aguardando resposta do usuário. */
export interface WorkspaceDecision {
  question: string;
  options: string[];
  context: string;
}

/** Payload da tela de trabalho (workspace). */
export interface Workspace {
  task: Task;
  summary: TaskSummary | null;
  occurrences: WorkspaceOccurrence[];
  decisions: WorkspaceDecision[];
  /** Chat human-in-the-loop (modo manual): transcript da task. */
  messages: TaskMessage[];
  /** Histórico de rodadas de agente no modo manual. */
  runs: TaskRun[];
  /** Agentes disponíveis para o dispatcher/menu (robôs do repo + globais). */
  agents: Robot[];
}

/** Uma interação do chat human-in-the-loop de uma task. */
export interface TaskMessage {
  id: number;
  task_id: number;
  seq: number;
  ts: string;
  kind: string;
  payload: Record<string, unknown>;
  cost: number;
}

/** Uma rodada de agente no modo human-in-the-loop. */
export interface TaskRun {
  id: number;
  task_id: number;
  robot_id: number | null;
  robot_name: string;
  robot_role: string;
  instruction: string;
  status: string;
  final_text: string | null;
  verdict: string | null;
  diff_stat: string | null;
  cost: number;
  started_at: string | null;
  finished_at: string | null;
}

/** Diff real (git) do commit de uma fase. */
export interface StepDiff {
  stat: string;
  diff: string;
  files: string[];
  commit: string | null;
}

/** Diff real (git) de UM arquivo dentro do commit de uma fase. */
export interface StepFileDiff {
  path: string;
  stat: string;
  diff: string;
  commit: string | null;
}

// ---------- Chamados (fluxo de atendimento: Projeto > Épico > Chamado) ----------

export interface Project {
  id: number;
  repository_id: number;
  name: string;
  description: string;
  status: string;
  summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetail extends Project {
  epics: Epic[];
  chamado_count: number;
}

export interface Epic {
  id: number;
  project_id: number;
  name: string;
  description: string;
  status: string;
  scope: string | null;
  summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface EpicDetail extends Epic {
  chamado_count: number;
}

/** Catálogo de tipos de etapa do fluxo de chamados. */
export interface ChamadoStageType {
  id: number;
  repository_id: number | null;
  name: string;
  description: string;
  is_initial: boolean;
  allowed_tools: string[];
  close_options: string[];
  delivery_config: Record<string, unknown>;
}

export interface ChamadoStage {
  id: number;
  chamado_id: number;
  stage_type_id: number;
  position: number;
  status: string;
  pending_action: string | null;
  decision: string | null;
  result: string | null;
  error: string | null;
  attempt: number;
  started_at: string | null;
  finished_at: string | null;
  stage_type_name: string | null;
}

export interface ChamadoMessage {
  id: number;
  chamado_id: number;
  stage_id: number;
  seq: number;
  ts: string;
  kind: string;
  payload: Record<string, unknown>;
  cost: number;
}

export interface Chamado {
  id: number;
  repository_id: number;
  project_id: number | null;
  epic_id: number | null;
  title: string;
  description: string;
  workflow_status: string;
  status: string;
  executor: string;
  /** Modelo do executor escolhido no chamado (null = default do executor). */
  model: string | null;
  budget_limit: number;
  cost_spent: number;
  error: string | null;
  responsible_id: number | null;
  created_at: string;
  updated_at: string;
  stages: ChamadoStage[];
}

export interface ToolInfo {
  key: string;
  label: string;
  description: string;
}

export interface ChamadoWorkspace {
  chamado: Chamado;
  stages: ChamadoStage[];
  messages: ChamadoMessage[];
  current_stage: ChamadoStage | null;
  tools: ToolInfo[];
  close_options: string[];
}

// ---------- Sistema (configuração geral) ----------

/** Alvos de limpeza aceitos no payload (id desconhecido → 400). */
export type CleanTarget = "logs" | "pytest_tmp" | "smoke" | "chrome_profiles";

/** Uma categoria do relatório de armazenamento (espelha `StorageCategory`). */
export interface StorageCategory {
  id: string;
  label: string;
  size_bytes: number;
  item_count: number;
  /** false = apenas medida (database/workspaces/skills); true = alvo de limpeza. */
  cleanable: boolean;
}

/** Relatório completo do armazenamento (5 categorias + total). */
export interface StorageReport {
  categories: StorageCategory[];
  total_bytes: number;
}

/** Resultado da limpeza de um alvo (itens removidos e bytes liberados). */
export interface CleanTargetResult {
  target: string;
  item_count: number;
  bytes_freed: number;
}

/** Resposta da limpeza: detalhe por alvo + total + relatório atualizado. */
export interface CleanResult {
  targets: CleanTargetResult[];
  total_bytes_freed: number;
  report: StorageReport;
}
