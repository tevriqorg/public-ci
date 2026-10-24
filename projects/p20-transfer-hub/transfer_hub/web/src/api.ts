import type { Health, Job, JobEvent, LocalFileListing, ReceiverTicket, WatcherRun, WatcherTask } from "./types"

export class UnauthorizedError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(init?.headers || {}),
    },
  })
  if (response.status === 401) throw new UnauthorizedError("当前连接未获 P20 私网授权，请通过 Tailnet 入口访问。")
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || "HTTP " + response.status)
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<Health>("/health"),
  runtimeStatus: () => request<{ ok: boolean; status?: Health["runtime"] }>("/api/v1/runtime/status"),
  runtimeStart: () => request<{ ok: boolean; status?: Health["runtime"]; log?: string[] }>("/api/v1/runtime/start", { method: "POST", body: "{}" }),
  runtimeStop: () => request<{ ok: boolean; status?: Health["runtime"] }>("/api/v1/runtime/stop", { method: "POST", body: "{}" }),
  jobs: () => request<Job[]>("/api/v1/jobs?limit=100"),
  watchers: () => request<WatcherTask[]>("/api/v1/watchers"),
  watcherRuns: (limit = 100) => request<WatcherRun[]>(`/api/v1/watcher-runs?limit=${limit}`),
  testWatcherNotification: () => request<{ok:boolean;status?:string;target?:string}>("/api/v1/watchers/test-notification", { method: "POST", body: "{}" }),
  clearHistory: () => request<{ deleted: number }>("/api/v1/jobs/history", { method: "DELETE" }),
  clearHistoryJob: (id: string) => request<{ job_id: string; cleared: boolean }>(`/api/v1/jobs/${id}/history`, { method: "DELETE" }),
  job: (id: string, reveal = false) =>
    request<Job>("/api/v1/jobs/" + id + (reveal ? "?reveal_secret=true" : "")),
  jobEvents: (id: string, limit = 30) =>
    request<JobEvent[]>("/api/v1/jobs/" + id + "/events?limit=" + limit),
  pause: (id: string) =>
    request<Job>("/api/v1/jobs/" + id + "/pause", { method: "POST", body: "{}" }),
  resume: (id: string) =>
    request<Job>("/api/v1/jobs/" + id + "/resume", { method: "POST", body: "{}" }),
  cancel: (id: string) =>
    request<Job>("/api/v1/jobs/" + id + "/cancel", { method: "POST", body: "{}" }),
  retry: (id: string) =>
    request<Job>("/api/v1/jobs/" + id + "/retry", { method: "POST", body: "{}" }),
  ack: (id: string) =>
    request<Job>("/api/v1/jobs/" + id + "/ack", { method: "POST", body: "{}" }),
  receiverTicket: (id: string) => request<ReceiverTicket>(`/api/v1/jobs/${id}/receiver-ticket`),
  receiverUrl: (path: string) => request<{url:string;name:string;size:number;sha256:string}>(path),
  skills: () => request<Array<{id:string;name:string;state:string}>>("/api/v1/skills"),
  createSkill: (payload:Record<string,unknown>) => request<{id:string;name:string;state:string}>("/api/v1/skills", {method:"POST",body:JSON.stringify(payload)}),
  applySkill: (jobId:string, skillId:string) => request<Job>(`/api/v1/jobs/${jobId}/apply-skill`, {method:"POST",body:JSON.stringify({skill_id:skillId})}),
  create: (payload: Record<string, unknown>) =>
    request<Job>("/api/v1/jobs", {
      method: "POST",
      body: JSON.stringify({ engine: "auto", ack_policy: "required", ...payload }),
    }),
  localFiles: (path = "") => request<LocalFileListing>("/api/v1/local-files?path=" + encodeURIComponent(path)),
  createLocalFile: (payload: Record<string, unknown>) =>
    request<Job>("/api/v1/local-files/jobs", { method: "POST", body: JSON.stringify(payload) }),

}
