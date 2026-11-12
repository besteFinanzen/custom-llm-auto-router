import { Plugin } from "@opencode/plugin"
import { hostname } from "node:os"
import * as path from "node:path"
import { execFile } from "node:child_process"
import { promisify } from "node:util"
import {
  COMMIT_LOG_FORMAT,
  CommitWatcher,
  headersWithToken,
  parseCommitLog,
  parseGitState,
  requestHeaders,
  snapshotFingerprint,
  trackerBaseUrl,
  extractFileChanges,
  type CommitRecord,
  type GitSnapshot,
  type Identity,
} from "./tracking.js"

const exec = promisify(execFile)

const DEVICE = process.env.OPENCODE_DEVICE || hostname()
const TOOL = "opencode"
const TOOL_VERSION = process.env.OPENCODE_VERSION || undefined

type IdentityCache = Map<string, Promise<Identity | undefined>>

/** Resolve identity per session using the session's own directory. */
async function sessionIdentity(ctx: any, sessionID: string, kind: string, agent?: string): Promise<Identity> {
  const base: Identity = {
    session: sessionID,
    kind,
    device: DEVICE,
    tool: TOOL,
    toolVersion: TOOL_VERSION,
    agent,
    job: process.env.OPENCODE_JOB || undefined,
  }
  let location = ctx.location as { directory?: string; project?: { canonical?: string; directory?: string; id?: string } } | undefined
  try {
    const session = await ctx.session.get({ sessionID })
    if (session?.location?.directory) location = { ...location, directory: session.location.directory }
  } catch {
    /* fall back to the instance location */
  }
  const directory = location?.directory
  const project = location?.project
  if (project?.id) base.project = project.id
  const root = project?.canonical || project?.directory
  if (root) base.projectRoot = root
  if (directory) {
    base.sessionDirectory = directory
    base.worktree = directory
  }
  // git identity is resolved per session directory, so `/project` switches work
  const cwd = directory || root
  if (!cwd) return base
  try {
    const [{ stdout: head }, { stdout: branchOut }] = await Promise.all([
      exec("git", ["rev-parse", "HEAD"], { cwd }),
      exec("git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd }),
    ])
    base.head = head.trim() || undefined
    const branch = branchOut.trim()
    base.branch = branch && branch !== "HEAD" ? branch : undefined
    base.repoRoot = (await exec("git", ["rev-parse", "--show-toplevel"], { cwd })).stdout.trim() || undefined
  } catch {
    /* not a git repository: requests are tagged x-oc-no-git instead */
  }
  return base
}

async function gitSnapshot(cwd: string): Promise<GitSnapshot | undefined> {
  try {
    const [{ stdout: status }, { stdout: numstat }] = await Promise.all([
      exec("git", ["status", "--porcelain=v1", "-b"], { cwd, maxBuffer: 4 * 1024 * 1024 }),
      exec("git", ["diff", "--numstat", "HEAD"], { cwd, maxBuffer: 4 * 1024 * 1024 }),
    ])
    const snapshot = parseGitState(status, numstat)
    const head = (await exec("git", ["rev-parse", "HEAD"], { cwd })).stdout.trim()
    snapshot.head = head || undefined
    snapshot.repoRoot = cwd
    return snapshot
  } catch {
    return undefined
  }
}

async function newCommits(cwd: string, since?: string, branch?: string): Promise<CommitRecord[]> {
  const format = COMMIT_LOG_FORMAT
  const args = since ? ["log", `${since}..HEAD`, format] : ["log", "-n", "5", "HEAD", format]
  const { stdout } = await exec("git", args, { cwd, maxBuffer: 4 * 1024 * 1024 })
  return parseCommitLog(stdout, branch)
}

async function post(pathname: string, body: unknown): Promise<void> {
  const base = trackerBaseUrl()
  if (!base) return
  try {
    await fetch(`${base}${pathname}`, {
      method: "POST",
      headers: headersWithToken(),
      body: JSON.stringify(body),
    })
  } catch {
    /* tracking is best-effort: never break the user's session */
  }
}

export default Plugin.define({
  id: "router-tags",
  async setup(ctx) {
    const watcher = new CommitWatcher()
    const fingerprints = new Map<string, string>()
    const identities: IdentityCache = new Map()

    const identityFor = (sessionID: string, kind: string, agent?: string) => {
      const key = `${sessionID}|${kind}|${agent ?? ""}`
      let promise = identities.get(key)
      if (!promise) {
        promise = sessionIdentity(ctx, sessionID, kind, agent)
        identities.set(key, promise)
        // identity is cheap but not free: refresh it every 30 seconds
        setTimeout(() => identities.delete(key), 30_000)
      }
      return promise
    }

    const observe = async (identity: Identity): Promise<void> => {
      const cwd = identity.sessionDirectory || identity.projectRoot
      if (!cwd) return
      const snapshot = await gitSnapshot(cwd)
      if (!snapshot) return
      if (snapshot.branch) identity.branch = snapshot.branch
      if (snapshot.head) identity.head = snapshot.head
      const repoKey = identity.project || cwd
      const fingerprint = snapshotFingerprint(snapshot)
      if (fingerprints.get(repoKey) !== fingerprint) {
        fingerprints.set(repoKey, fingerprint)
        await post("/worktracker/api/snapshots", {
          id: fingerprint,
          session: identity.session,
          project: identity.project,
          repo_root: snapshot.repoRoot || cwd,
          branch: snapshot.branch,
          head: snapshot.head,
          dirty: snapshot.dirty,
          status: snapshot.status,
          files: snapshot.files,
          device: identity.device,
          tool: identity.tool,
        })
      }
      const previous = watcher.head(repoKey)
      if (snapshot.head && previous && snapshot.head !== previous) {
        try {
          const commits = await newCommits(cwd, previous, snapshot.branch)
          watcher.observe(repoKey, commits)
          for (const commit of commits) {
            await post("/worktracker/api/commits", {
              ...commit,
              committed_at: commit.committedAt,
              project: identity.project,
              repo_root: snapshot.repoRoot || cwd,
              source: "plugin",
              device: identity.device,
              tool: identity.tool,
            })
          }
          watcher.drain(repoKey)
        } catch {
          /* force-push / unrelated history: wait for the next tick */
        }
      } else if (snapshot.head && !previous) {
        // first sighting: remember HEAD, do not replay old history
        watcher.observe(repoKey, [])
        watcher.drain(repoKey)
      }
    }

    // 1. identity headers on every model request
    await ctx.session.hook("http.request", async (event: any) => {
      const identity = await identityFor(event.sessionID, event.kind)
      if (!identity) return
      const headers = requestHeaders(identity)
      for (const [key, value] of Object.entries(headers)) event.request.headers.set(key, value)
      // keep the snapshot fresh in the background; requests never wait on git
      void observe(identity)
    })

    // 2. tool events: which files an AI step touched (credits commits)
    await ctx.tool.hook("execute.after", async (event: any) => {
      if (event.status !== "completed") return
      const identity = await identityFor(event.sessionID, "primary").catch(() => undefined)
      if (!identity) return
      const files = extractFileChanges(event.tool, event.input)
      await post("/worktracker/api/tool-events", {
        id: event.id,
        session: event.sessionID,
        project: identity.project,
        branch: identity.branch,
        device: identity.device,
        tool: identity.tool,
        tool_name: event.tool,
        files,
      })
      void observe(identity)
    })

    // 3. session activity: keep presence/live view current
    await ctx.session.hook("prompt", async (event: any) => {
      const identity = await identityFor(event.sessionID, "primary").catch(() => undefined)
      if (!identity) return
      await post("/worktracker/api/presence", {
        session: identity.session,
        project_key: identity.project,
        branch: identity.branch,
        device: identity.device,
        tool: identity.tool,
        editor: TOOL,
        status: "active",
      })
      void observe(identity)
    })

    // 4. periodic ticks so idle sessions still report commits made in a terminal
    const controller = new AbortController()
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const sessionID = (event as { data?: { sessionID?: string } })?.data?.sessionID
          if (sessionID) {
            const identity = await identityFor(sessionID, "primary").catch(() => undefined)
            if (identity) void observe(identity)
          }
        }
      } catch {
        /* host shut down */
      }
    })()
  },
})
