// New session dialog. Fields per the mockup, with client validation and inline API errors.

import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, createSession, getProjects } from "./api";
import { Modal } from "./Modal";
import { PlusIcon } from "./icons";
import { isValidName } from "./util";
import type { EnvView, PermissionMode, ProjectEntry, SessionView } from "./types";

interface NewSessionDialogProps {
  open: boolean;
  onClose: () => void;
  envs: EnvView[];
  defaultEnv: string;
  onCreated: (s: SessionView) => void;
}

type Kind = "claude" | "codex" | "shell";

const PERMISSION_OPTIONS: Array<{ value: "" | PermissionMode; label: string }> = [
  { value: "", label: "Use my default" },
  { value: "manual", label: "Manual approval" },
  { value: "acceptEdits", label: "Accept edits" },
  { value: "auto", label: "Auto" },
  { value: "plan", label: "Plan" },
];

export function NewSessionDialog({
  open,
  onClose,
  envs,
  defaultEnv,
  onCreated,
}: NewSessionDialogProps) {
  const availableEnvs = useMemo(() => envs.filter((e) => e.available), [envs]);
  const [env, setEnv] = useState(defaultEnv);
  const [kind, setKind] = useState<Kind>("claude");
  const [name, setName] = useState("");
  const [project, setProject] = useState("");
  const [prompt, setPrompt] = useState("");
  const [permission, setPermission] = useState<"" | PermissionMode>("");
  const [projects, setProjects] = useState<ProjectEntry[]>([]);
  const [nameError, setNameError] = useState<string | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const reqId = useRef(0);

  const currentEnv = envs.find((e) => e.id === env) ?? null;
  const claudeOk = currentEnv?.agents.claude ?? false;
  const codexOk = currentEnv?.agents.codex ?? false;

  // Reset when the dialog opens.
  useEffect(() => {
    if (!open) return;
    const startEnv = availableEnvs.some((e) => e.id === defaultEnv)
      ? defaultEnv
      : availableEnvs[0]?.id ?? defaultEnv;
    setEnv(startEnv);
    setName("");
    setProject("");
    setPrompt("");
    setPermission("");
    setNameError(null);
    setProjectError(null);
    setFormError(null);
  }, [open, defaultEnv, availableEnvs]);

  // Pick a valid agent when the env changes.
  useEffect(() => {
    if (!open) return;
    if (kind === "claude" && !claudeOk) setKind(codexOk ? "codex" : "shell");
    else if (kind === "codex" && !codexOk) setKind(claudeOk ? "claude" : "shell");
  }, [env, open, kind, claudeOk, codexOk]);

  // Load the project datalist for the chosen env.
  useEffect(() => {
    if (!open || !env) return;
    const id = ++reqId.current;
    setProjects([]);
    getProjects(env)
      .then((res) => {
        if (id === reqId.current) setProjects(res.projects);
      })
      .catch(() => {
        if (id === reqId.current) setProjects([]);
      });
  }, [open, env]);

  const submit = async () => {
    setFormError(null);
    setNameError(null);
    setProjectError(null);
    const trimmedName = name.trim();
    if (trimmedName && !isValidName(trimmedName)) {
      setNameError(
        "Use letters, numbers, spaces, dashes or underscores. Start with a letter or number.",
      );
      return;
    }
    setBusy(true);
    try {
      const created = await createSession({
        env,
        kind,
        name: trimmedName || undefined,
        project: project.trim() || undefined,
        prompt: kind === "shell" ? undefined : prompt.trim() || undefined,
        permission_mode:
          kind === "claude" && permission ? (permission as PermissionMode) : undefined,
      });
      onCreated(created);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === "duplicate_name" || err.code === "invalid_name") {
          setNameError(err.message);
        } else if (err.code === "invalid_project") {
          setProjectError(err.message);
        } else {
          setFormError(err.message);
        }
      } else {
        setFormError("Cannot reach relay.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      titleId="new-session-title"
      dismissLabel="Dismiss new session"
    >
      <form
        className="modal-body"
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy) void submit();
        }}
      >
        <div className="modal-head">
          <div>
            <h2 id="new-session-title">A fresh session.</h2>
            <p>Choose where your next agent will work.</p>
          </div>
        </div>

        <div className="form-row">
          <div className="form-field">
            <label htmlFor="new-work">Workspace</label>
            <select id="new-work" value={env} onChange={(e) => setEnv(e.target.value)}>
              {envs.map((e) => (
                <option key={e.id} value={e.id} disabled={!e.available}>
                  {e.label}
                  {e.available ? "" : " (unavailable)"}
                </option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="new-kind">Agent</label>
            <select
              id="new-kind"
              value={kind}
              onChange={(e) => setKind(e.target.value as Kind)}
            >
              <option value="claude" disabled={!claudeOk}>
                Claude{claudeOk ? "" : " (unavailable)"}
              </option>
              <option value="codex" disabled={!codexOk}>
                Codex{codexOk ? "" : " (unavailable)"}
              </option>
              <option value="shell">Shell</option>
            </select>
          </div>
        </div>

        <div className="form-field">
          <label htmlFor="new-name">
            Session name <span className="optional">· optional</span>
          </label>
          <input
            id="new-name"
            maxLength={48}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setNameError(null);
            }}
            aria-invalid={nameError ? true : undefined}
            aria-describedby={nameError ? "new-name-error" : "new-name-help"}
            placeholder="e.g. API fixes"
          />
          {nameError ? (
            <div className="field-error" id="new-name-error">
              {nameError}
            </div>
          ) : (
            <div className="field-help" id="new-name-help">
              An empty name gets the next available agent number.
            </div>
          )}
        </div>

        <div className="form-field">
          <label htmlFor="new-project">Project folder</label>
          <input
            id="new-project"
            list="project-list"
            maxLength={140}
            value={project}
            onChange={(e) => {
              setProject(e.target.value);
              setProjectError(null);
            }}
            aria-invalid={projectError ? true : undefined}
            aria-describedby={projectError ? "new-project-error" : "new-project-help"}
            placeholder="e.g. customer-portal"
          />
          <datalist id="project-list">
            {projects.map((p) => (
              <option key={p.path} value={p.name} />
            ))}
          </datalist>
          {projectError ? (
            <div className="field-error" id="new-project-error">
              {projectError}
            </div>
          ) : (
            <div className="field-help" id="new-project-help">
              Inside the selected workspace's projects directory.
            </div>
          )}
        </div>

        {kind !== "shell" ? (
          <div className="form-field">
            <label htmlFor="new-task">
              First prompt <span className="optional">· optional</span>
            </label>
            <textarea
              id="new-task"
              rows={3}
              maxLength={4000}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="What should this agent work on?"
            />
          </div>
        ) : null}

        {kind === "claude" ? (
          <div className="form-field">
            <label htmlFor="new-permission">Permissions</label>
            <select
              id="new-permission"
              value={permission}
              onChange={(e) => setPermission(e.target.value as "" | PermissionMode)}
            >
              {PERMISSION_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
        ) : null}

        {formError ? (
          <div className="field-error" role="alert">
            {formError}
          </div>
        ) : null}

        <div className="modal-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy}>
            <PlusIcon />
            {busy ? "Starting…" : "Start session"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
