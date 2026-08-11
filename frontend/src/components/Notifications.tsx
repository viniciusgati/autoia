import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { api } from "../api";
import { BellIcon } from "./Icons";
import { usePolling } from "../lib/polling";
import type { Notice } from "../types";

const SEEN_KEY = "autoia_seen_notices";

function noticeKey(n: Notice): string {
  return `${n.task_id}:${n.kind}`;
}

function loadSeen(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(SEEN_KEY) ?? "[]") as string[]);
  } catch {
    return new Set();
  }
}

function saveSeen(seen: Set<string>): void {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify([...seen]));
  } catch {
    /* localStorage indisponível */
  }
}

function browserNotify(title: string, body: string): void {
  if (typeof Notification === "undefined") return;
  if (Notification.permission !== "granted") return;
  try {
    new Notification(title, { body, tag: "autoia-notice" });
  } catch {
    /* notificação bloqueada pelo navegador */
  }
}

export default function Notifications() {
  const [notices, setNotices] = useState<Notice[]>([]);
  const [seen, setSeen] = useState<Set<string>>(loadSeen);
  const [open, setOpen] = useState(false);
  const lastKeys = useRef<string[]>([]);
  const first = useRef(true);
  const location = useLocation();

  usePolling(
    (signal) => {
      api
        .getDashboard(undefined, signal)
        .then((d) => {
          setNotices(d.notices);
          const keys = d.notices.map(noticeKey);
          if (first.current) {
            lastKeys.current = keys;
            first.current = false;
            return;
          }
          const fresh = keys.filter((k) => !lastKeys.current.includes(k));
          for (const k of fresh) {
            const n = d.notices.find((x) => noticeKey(x) === k);
            if (n) browserNotify(`autoia — ${n.task_title}`, n.message);
          }
          lastKeys.current = keys;
        })
        .catch(() => {});
    },
    5000,
    [],
  );

  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  const unread = notices.filter((n) => !seen.has(noticeKey(n))).length;

  const toggle = () => {
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
    if (!open) {
      setSeen((current) => {
        const next = new Set(current);
        notices.forEach((n) => next.add(noticeKey(n)));
        saveSeen(next);
        return next;
      });
    }
    setOpen(!open);
  };

  return (
    <div className={`notif-wrap${open ? " open" : ""}`}>
      <button className="notif-bell" onClick={toggle} aria-label="Notificações">
        <BellIcon size={18} />
        {unread > 0 && <span className="notif-badge">{unread > 9 ? "9+" : unread}</span>}
      </button>

      {open && (
        <div className="notif-dropdown">
          <div className="notif-head">
            <strong>Notificações</strong>
            {notices.length === 0 && <span className="muted small">nenhuma</span>}
          </div>
          {notices.length === 0 ? (
            <div className="notif-empty muted small">Tudo tranquilo por aqui.</div>
          ) : (
            notices.map((n, i) => (
              <Link
                key={`${noticeKey(n)}-${i}`}
                to={`/${n.repository_id}/tasks/${n.task_id}`}
                className={`notif-item notif-${n.level}`}
              >
                <div className="notif-item-line">
                  <span className="notif-kind">{n.kind}</span>
                  <span className="notif-task">#{n.task_id} {n.task_title}</span>
                </div>
                <div className="notif-msg">{n.message}</div>
              </Link>
            ))
          )}
        </div>
      )}
    </div>
  );
}
