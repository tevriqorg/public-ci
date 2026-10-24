export type JobState =
  | "QUEUED" | "FETCHING" | "VERIFYING" | "PACKING" | "ENCRYPTING"
  | "FLASH_UPLOADING" | "READY" | "RETRIEVED" | "DOWNLOADED" | "CLEANUP" | "DONE"
  | "FAILED" | "CANCELLED" | "PAUSED"

export interface Health {
  ok: boolean
  service: string
  version: string
  tools: Record<string, boolean>
  qq_flash: {
    configured: boolean
    healthy: boolean
    qq?: { user_id?: number; nickname?: string }
    flash_pending?: number
    spool_files?: number
    error?: string
  }
  disk: { total: number; used: number; free: number }
  capabilities?: { pipeline?: { fetch_concurrency:number; process_concurrency:number; delivery_concurrency:number }; receiver?: { ticket_schema:string } }
  runtime?: { state?: string; logged_in?: string; active_jobs?: number; idle_minutes?: number }
}

export interface Job {
  id: string
  job_number: number
  job_number_label: string
  public_name_preview?: string | null
  state: JobState
  current_stage?: string | null
  stage_state?: string | null
  queue_position?: number | null
  waiting_reason?: string | null
  source_type: string
  source: string | null
  filename: string
  title: string | null
  engine: string
  progress: number
  upload_progress: number
  download_speed: number
  upload_speed: number
  eta_seconds: number | null
  original_size: number | null
  final_size: number | null
  sha256: string | null
  error: string | null
  retries: number
  cancel_requested: boolean
  created_at: string
  updated_at: string
  completed_at: string | null
  cleanup_at: string | null
  options: {
    delivery_profile?: "unified" | "desktop" | "iphone" | "android"
    pack?: boolean
    pack_format?: string
    encrypt?: boolean
    volume_size?: string | null
    delivery_backend?: string
    ttl_seconds?: number
    ack_policy?: string
    auth_ref?: string | null
    resolver_override?: string
    engine_override?: string
    matched_skill_id?: string
    applied_skill_id?: string
    failure_fingerprint?: string
    local_origin?: string
    local_display_path?: string | null
    public_names?: string[]
    scheduled?: boolean
    watcher_id?: string | null
    watcher_name?: string | null
    watch_run_id?: number | null
    watch_version?: string | null
  }
  result?: {
    backend?: string
    items?: Array<{
      share_link?: string
      file_set_id?: string
      filename?: string
      size?: number
      sha256?: string
      expire_time?: number
    }>
  } | null
  archive_password?: string | null
}

export interface LocalFileEntry {
  name: string
  path: string
  type: "directory" | "file"
  size: number | null
  modified_at: number
}

export interface LocalFileListing {
  path: string
  parent: string | null
  entries: LocalFileEntry[]
}

export interface JobEvent {
  id: number
  job_id: string | null
  type: string
  data: Record<string, unknown>
  created_at: string
}




export interface ReceiverTicket {
  schema: "p20.receiver-ticket/v2" | "p20.receiver-ticket/v1"
  transfer_id: string
  artifacts: Array<{
    name: string
    size: number
    sha256: string
    source: { kind: "qq_flash_share"; share_link: string }
    browser_resolve_path?: string
    browser_resolve_url: string
  }>
  postprocess?: {
    kind: "none" | "archive"
    format?: string
    encrypted?: boolean
    password?: string | null
    primary?: string
    remove_archive_after_success?: boolean
  }
  ack: { required: boolean; method: "POST"; url: string }
}

export interface WatcherTask {
  id: string
  name: string
  kind: "github_release" | "sparkle_appcast" | string
  enabled: boolean
  interval_seconds: number
  config: Record<string, unknown>
  last_item_key: string | null
  last_item_title: string | null
  last_job_id: string | null
  last_job_state: JobState | null
  last_job_number_label: string | null
  last_run_at: string | null
  last_outcome: string | null
  last_message: string | null
  next_run_at: string | null
}

export interface WatcherRun {
  id: number
  watcher_id: string
  outcome: string
  item_key: string | null
  item_title: string | null
  job_id: string | null
  job_state: JobState | null
  job_number_label: string | null
  message: string | null
  started_at: string
  finished_at: string | null
  details: Record<string, unknown>
}
