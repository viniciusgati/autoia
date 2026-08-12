import { useState } from "react";
import { Link } from "react-router-dom";
import ProposalCard from "./ProposalCard";
import DiffView from "./DiffView";
import { formatToolCall, sessionEventLine } from "../lib/events";
import Markdown from "../lib/markdown";
import type { ChatTurn, PhaseTurn, TaskTurn } from "../lib/chat";
import type { RunEvent, Task, TaskStep } from "../types";

/** Timeline como chat: uma bolha por tentativa de fase + turnos system inline.
 *  Fase `running` vira bolha "ao vivo" (comando atual + eventos streaming).
 */
export default function TaskChat({
  task,
  turns,
  repoNames,
  live,
  onProposalsChanged,
  onError,
  canAct = true,
}: {
  task: Task;
  turns: ChatTurn[];
  repoNames: Record<number, string>;
  live: { step: TaskStep; toolCall: RunEvent | null; events: RunEvent[] } | null;
  onProposalsChanged: () => void;
  onError: (message: string) => void;
  /** Permissão de atuação (responsável/admin) — repassa às propostas. */
  canAct?: boolean;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const liveStepId = live?.step.id;

  const toggle = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="chat chat-timeline">
      {turns.map((turn) => {
        if (turn.kind === "task") {
          return (
            <TaskIntro
              key={turn.id}
              turn={turn}
              repoNames={repoNames}
              parentRepoName={repoNames[task.repository_id]}
              onProposalsChanged={onProposalsChanged}
              onError={onError}
              canAct={canAct}
            />
          );
        }
        if (turn.kind === "phase") {
          return (
            <PhaseBubble
              key={turn.id}
              turn={turn}
              taskId={task.id}
              repoId={task.repository_id}
              expanded={expanded.has(turn.id)}
              onToggle={() => toggle(turn.id)}
              live={liveStepId === turn.stepId ? live : null}
            />
          );
        }
        return (
          <div key={turn.id} className={`chat-marker chat-marker-${turn.type}`}>
            {turn.text}
          </div>
        );
      })}
    </div>
  );
}

/** Mensagem 0 do chat: a tarefa (descrição + critérios + propostas). */
function TaskIntro({
  turn,
  repoNames,
  parentRepoName,
  onProposalsChanged,
  onError,
  canAct,
}: {
  turn: TaskTurn;
  repoNames: Record<number, string>;
  parentRepoName?: string;
  onProposalsChanged: () => void;
  onError: (message: string) => void;
  canAct?: boolean;
}) {
  const pending = (turn.proposals ?? []).filter((p) => p.status === "pending");
  return (
    <div className="chat-msg">
      <span className="chat-avatar chat-avatar-worker">📋</span>
      <div className="chat-bubble chat-bubble-kimi">
        <div className="chat-who">
          a tarefa <span className="muted small">· #{turn.title}</span>
        </div>
        <div className="chat-body">
          {turn.description ? (
            <Markdown text={turn.description} />
          ) : (
            <p className="muted">Sem descrição.</p>
          )}
          {turn.acceptanceCriteria && (
            <>
              <strong>Critérios de aceite</strong>
              <Markdown text={turn.acceptanceCriteria} />
            </>
          )}
        </div>
        {pending.length > 0 && (
          <div className="chat-proposals">
            <div className="chat-proposals-label">
              🧩 propostas de tasks filhas aguardando aprovação
            </div>
            {pending.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                repoNames={repoNames}
                parentRepoName={parentRepoName}
                onChanged={onProposalsChanged}
                onError={onError}
                canAct={canAct}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Bolha de uma tentativa de fase (robô). */
function PhaseBubble({
  turn,
  taskId,
  repoId,
  expanded,
  onToggle,
  live,
}: {
  turn: PhaseTurn;
  taskId: number;
  repoId: number;
  expanded: boolean;
  onToggle: () => void;
  live: { step: TaskStep; toolCall: RunEvent | null; events: RunEvent[] } | null;
}) {
  const avatar = turn.robotName.slice(0, 1).toUpperCase();
  const isRerun = turn.attempt > 1;
  const duration =
    turn.startedAt && turn.finishedAt
      ? duracao(turn.startedAt, turn.finishedAt)
      : null;

  return (
    <div className="chat-msg">
      <span className={`chat-avatar${isRerun ? " chat-avatar-rerun" : ""}`}>
        {isRerun ? "↺" : avatar}
      </span>
      <div className={`chat-bubble chat-bubble-kimi${turn.postMerge ? " chat-bubble-post" : ""}`}>
        <div className="chat-who">
          <strong>
            {turn.robotName} <span className="muted small">({turn.robotRole})</span>
          </strong>
          {isRerun && <span className="badge badge-warn">re-execução {turn.attempt}</span>}
          {turn.postMerge && <span className="badge badge-ok">pós-merge</span>}
          <span className="muted small">F{turn.position}</span>
          {turn.verdict && <span className="badge">{turn.verdict}</span>}
          {turn.cost > 0 && <span className="muted small">+{turn.cost.toFixed(2)} US$</span>}
        </div>

        {live ? (
          <LiveBody live={live} expanded={expanded} onToggle={onToggle} />
        ) : turn.summary ? (
          <div className="chat-body">
            <Markdown text={turn.summary} />
          </div>
        ) : (
          <div className="chat-body muted">
            {turn.error ? `Erro: ${turn.error}` : "Sem conteúdo registrado."}
          </div>
        )}

        {turn.error && <div className="error small">{turn.error}</div>}

        <div className="chat-meta">
          {duration && <span className="muted small">⏱ {duration}</span>}
          {turn.diffSummaryText && (
            <span className="muted small diff-summary">{turn.diffSummaryText}</span>
          )}
          {turn.diffStat && (
            <button className="link-btn" onClick={onToggle}>
              {expanded ? "recolher diff" : "ver diff"}
            </button>
          )}
          <Link to={`/${repoId}/tasks/${taskId}/phase/${turn.stepId}`} className="link-btn">
            detalhes completos →
          </Link>
        </div>
        {expanded && turn.diffStat && <DiffView code={turn.diffStat} compact />}      </div>
    </div>
  );
}

/** Corpo ao vivo de uma fase rodando: comando atual + últimos eventos. */
function LiveBody({
  live,
  expanded,
  onToggle,
}: {
  live: { step: TaskStep; toolCall: RunEvent | null; events: RunEvent[] };
  expanded: boolean;
  onToggle: () => void;
}) {
  const events = live.events.slice(-12).reverse();
  return (
    <div className="chat-body live-body">
      <div className="live-command">
        <span className="muted small">⚡ comando atual</span>
        <span className="mono">
          {live.toolCall ? formatToolCall(live.toolCall) : "aguardando interação…"}
        </span>
      </div>
      <ul className="session-events" style={{ marginTop: 6 }}>
        {events.map((e) => (
          <li key={e.id} className={`session-event session-event-${e.kind}`}>
            <span className="mono time">{new Date(e.ts).toLocaleTimeString()}</span>
            <span className="kind">{e.kind}</span>
            <span className="session-event-text">{sessionEventLine(e)}</span>
          </li>
        ))}
      </ul>
      <button className="link-btn" onClick={onToggle}>
        {expanded ? "recolher" : "ver eventos ao vivo"}
      </button>
      {expanded && (
        <pre className="event-payload">{JSON.stringify(live.events, null, 2)}</pre>
      )}
    </div>
  );
}

function duracao(started: string, finished: string): string {
  const ms = new Date(finished).getTime() - new Date(started).getTime();
  if (ms < 0) return "";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
