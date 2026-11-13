/**
 * Pure, side-effect-free helpers for the router-tags plugin.
 *
 * Everything that needs to be tested (header building, snapshot fingerprints,
 * commit observation, tracker payloads) lives here so the plugin entry point
 * stays a thin wiring layer over the OpenCode host API.
 */
import { createHash } from "node:crypto"

export const REQUIRED_HEADERS = [
  "x-oc-kind",
  "x-oc-session",
  "x-oc-project",
  "x-oc-project-root",
  "x-oc-session-directory",
  "x-oc-device",
  "x-oc-tool",
] as const

export interface Identity {
  session: string
  kind: string
  project?: string
  projectRoot?: string
  sessionDirectory?: string
  worktree?: string
  branch?: string
  head?: string
  repoRoot?: string
  repoRemote?: string
  device: string
  tool: string
  toolVersion?: string
  job?: string
  agent?: string
}

export interface GitSnapshot {
  branch?: string
  head?: string
  repoRoot?: string
  remote?: string
  dirty: boolean
  status?: string
  files: Array<{ path: string; status?: string; additions?: number; deletions?: number }>
  diffstat?: Record<string, { additions?: number; deletions?: number }>
}

/** Headers that carry routing identity on every model request. */
export function requestHeaders(identity: Identity): Record<string, string> {
  const headers: Record<string, string> = {
    "x-oc-kind": identity.kind || "primary",
    "x-oc-session": identity.session,
    "x-oc-device": identity.device,
    "x-oc-tool": identity.tool,
  }
  if (identity.project) headers["x-oc-project"] = identity.project
  if (identity.projectRoot) headers["x-oc-project-root"] = identity.projectRoot
  if (identity.sessionDirectory) headers["x-oc-session-directory"] = identity.sessionDirectory
  if (identity.worktree) headers["x-oc-worktree"] = identity.worktree
  if (identity.toolVersion) headers["x-oc-tool-version"] = identity.toolVersion
  if (identity.job) headers["x-oc-job"] = identity.job
  if (identity.agent) headers["x-oc-agent"] = identity.agent
  if (identity.branch) {
    headers["x-oc-branch"] = identity.branch
    if (identity.head) headers["x-oc-git-head"] = identity.head
  } else {
    headers["x-oc-no-git"] = "1"
  }
  if (identity.repoRoot) headers["x-oc-repo-root"] = identity.repoRoot
  if (identity.repoRemote) headers["x-oc-repo-remote"] = identity.repoRemote
  return headers
}

/** Stable fingerprint of the parts of a snapshot that change on edits/commits. */
export function snapshotFingerprint(snapshot: GitSnapshot): string {
  const files = [...snapshot.files]
    .map((f) => `${f.path}:${f.status ?? ""}:${f.additions ?? 0}:${f.deletions ?? 0}`)
    .sort()
  return createHash("sha1")
    .update(
      JSON.stringify({
        branch: snapshot.branch ?? null,
        head: snapshot.head ?? null,
        dirty: snapshot.dirty,
        files,
      }),
    )
    .digest("hex")
}

/** Normalise `git status --porcelain=v1 -b` plus `git diff --numstat` output. */
export function parseGitState(statusText: string, numstatText = ""): GitSnapshot {
  const snapshot: GitSnapshot = { dirty: false, files: [] }
  const additions = new Map<string, { additions: number; deletions: number }>()
  for (const line of numstatText.split("\n")) {
    if (!line.trim()) continue
    const [add, del, path] = line.split("\t")
    if (!path) continue
    additions.set(path, { additions: Number(add) || 0, deletions: Number(del) || 0 })
  }
  const byPath = new Map<string, { path: string; status?: string; additions?: number; deletions?: number }>()
  for (const raw of statusText.split("\n")) {
    if (!raw.trim()) continue
    if (raw.startsWith("## ")) {
      const rest = raw.slice(3).trim()
      const head = rest.split("...")[0].split(" ")[0]
      if (head && head !== "HEAD" && !head.startsWith("No") && head !== "(no") snapshot.branch = head
      continue
    }
    const status = raw.slice(0, 2).trim() || undefined
    let path = raw.slice(3).trim()
    if (path.includes(" -> ")) path = path.split(" -> ").pop() as string
    const delta = additions.get(path)
    byPath.set(path, {
      path,
      status,
      additions: status === "D" ? undefined : delta?.additions,
      deletions: status === "D" ? undefined : delta?.deletions,
    })
  }
  snapshot.files = [...byPath.values()].sort((a, b) => a.path.localeCompare(b.path))
  snapshot.dirty = snapshot.files.length > 0
  snapshot.status = snapshot.files.length ? snapshot.files.map((f) => `${f.status ?? ""} ${f.path}`.trim()).join("\n") : ""
  return snapshot
}

export interface CommitRecord {
  sha: string
  branch?: string
  parents: string[]
  subject?: string
  author?: string
  email?: string
  committedAt: string
}

/**
 * Reports new commits between the last observed HEAD and the current one.
 * Handles amended/rebased history by falling back to "HEAD moved" when the
 * previous SHA is not an ancestor anymore.
 */
export class CommitWatcher {
  private last = new Map<string, string>()
  private pending = new Map<string, CommitRecord[]>()

  /** Queue commits to be delivered on the next flush. */
  observe(repoKey: string, commits: CommitRecord[]): CommitRecord[] {
    if (!commits.length) return []
    this.last.set(repoKey, commits[commits.length - 1].sha)
    const queue = this.pending.get(repoKey) ?? []
    const seen = new Set(queue.map((c) => c.sha))
    for (const commit of commits) if (!seen.has(commit.sha)) queue.push(commit)
    this.pending.set(repoKey, queue)
    return queue
  }

  head(repoKey: string): string | undefined {
    return this.last.get(repoKey)
  }

  drain(repoKey: string): CommitRecord[] {
    const queue = this.pending.get(repoKey) ?? []
    this.pending.set(repoKey, [])
    return queue
  }

  reset(): void {
    this.last.clear()
    this.pending.clear()
  }
}

export const COMMIT_SEPARATOR = "\x1e"
export const COMMIT_FIELD_SEPARATOR = "\x1f"

/** Parses `git log --format=%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e`. */
export function parseCommitLog(text: string, branch?: string): CommitRecord[] {
  const out: CommitRecord[] = []
  for (const chunk of text.split(COMMIT_SEPARATOR)) {
    const line = chunk.trim()
    if (!line) continue
    const [sha, parents, author, email, committedAt, subject] = line.split(COMMIT_FIELD_SEPARATOR)
    if (!sha) continue
    out.push({
      sha: sha.trim(),
      branch,
      parents: (parents ?? "").trim().split(/\s+/).filter(Boolean),
      author: author || undefined,
      email: email || undefined,
      committedAt: committedAt || new Date().toISOString(),
      subject: subject || undefined,
    })
  }
  return out
}

/** Files touched by a tool call, used to credit usage to commits. */
export function extractFileChanges(name: string, input: unknown): string[] {
  if (!input || typeof input !== "object") return []
  const record = input as Record<string, unknown>
  const direct = [record.filePath, record.file, record.path, record.paths, record.files]
  const out: string[] = []
  for (const value of direct) {
    if (typeof value === "string" && value) out.push(value)
    else if (Array.isArray(value)) for (const item of value) if (typeof item === "string") out.push(item)
  }
  if (name.includes("patch")) {
    for (const patch of [record.patch, record.diff]) {
      if (typeof patch !== "string") continue
      for (const line of patch.split("\n")) {
        if (line.startsWith("*** Update File:") || line.startsWith("*** Add File:") || line.startsWith("*** Delete File:")) {
          out.push(line.split(":", 2)[1].trim())
        } else if (line.startsWith("+++ ") || line.startsWith("--- ")) {
          const path = line.slice(4).trim()
          if (path && path !== "/dev/null") out.push(path.replace(/^[ab]\//, ""))
        }
      }
    }
  }
  return [...new Set(out)]
}

export function trackerBaseUrl(): string | undefined {
  const raw = process.env.OPENCODE_ROUTER_URL || process.env.ROUTER_URL || ""
  if (!raw) return undefined
  const trimmed = raw.replace(/\/+$/, "")
  return trimmed.endsWith("/v1") ? trimmed.slice(0, -3) : trimmed
}

export function trackerToken(): string | undefined {
  return process.env.OPENCODE_ROUTER_TOKEN || process.env.OPENCODE_ROUTER_HOOK_TOKEN || undefined
}

export function headersWithToken(extra: Record<string, string> = {}): Record<string, string> {
  const token = trackerToken()
  return { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}), ...extra }
}

export const COMMIT_LOG_FORMAT = "%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e"

/** Normalise a git remote into a stable `host/owner/repo` project key. */
export function normalizeRemote(remote: string): string | undefined {
  if (!remote) return undefined
  let host = ""
  let path = ""
  if (remote.includes("://")) {
    try {
      const u = new URL(remote)
      host = (u.hostname || "").toLowerCase()
      path = u.pathname
    } catch {
      return undefined
    }
  } else {
    const hostPath = remote.split("@").pop() ?? remote
    const sep = hostPath.indexOf(":")
    if (sep >= 0) {
      host = hostPath.slice(0, sep).toLowerCase()
      path = hostPath.slice(sep + 1)
    } else {
      path = hostPath
    }
  }
  path = "/" + path.replace(/^\/+/, "").replace(/\/+$/, "").replace(/\.git$/, "")
  return host ? `${host}${path}` : path.replace(/^\/+/, "")
}
