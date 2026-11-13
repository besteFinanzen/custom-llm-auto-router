import { describe, expect, it } from "vitest"
import {
  COMMIT_LOG_FORMAT,
  CommitWatcher,
  extractFileChanges,
  normalizeRemote,
  parseCommitLog,
  parseGitState,
  requestHeaders,
  snapshotFingerprint,
} from "../tracking.js"

describe("requestHeaders", () => {
  const base = { session: "ses_1", kind: "primary", device: "laptop", tool: "opencode" }

  it("emits every enforced attribution header for a git session", () => {
    const headers = requestHeaders({
      ...base,
      project: "github.com/acme/app",
      projectRoot: "/src/app",
      sessionDirectory: "/src/app/.worktrees/feat",
      branch: "feat/worktracker",
      head: "abc123",
      job: "review",
      agent: "build",
    })
    expect(headers).toMatchObject({
      "x-oc-kind": "primary",
      "x-oc-session": "ses_1",
      "x-oc-project": "github.com/acme/app",
      "x-oc-project-root": "/src/app",
      "x-oc-session-directory": "/src/app/.worktrees/feat",
      "x-oc-device": "laptop",
      "x-oc-tool": "opencode",
      "x-oc-branch": "feat/worktracker",
      "x-oc-git-head": "abc123",
      "x-oc-job": "review",
      "x-oc-agent": "build",
    })
    expect(headers["x-oc-no-git"]).toBeUndefined()
  })

  it("emits x-oc-repo-remote when the identity carries a repo remote", () => {
    const headers = requestHeaders({
      ...base,
      project: "acme/app",
      projectRoot: "/src/app",
      repoRemote: "github.com/acme/app",
      repoRoot: "/src/app",
    })
    expect(headers["x-oc-repo-remote"]).toBe("github.com/acme/app")
  })

  it("marks non-git sessions so enforcement can allow them", () => {
    const headers = requestHeaders({ ...base, sessionDirectory: "/tmp/scratch" })
    expect(headers["x-oc-no-git"]).toBe("1")
    expect(headers["x-oc-branch"]).toBeUndefined()
  })

  it("defaults the flow kind", () => {
    expect(requestHeaders({ ...base, kind: "" })["x-oc-kind"]).toBe("primary")
  })
})

describe("normalizeRemote", () => {
  it("turns HTTPS remotes into host/owner/repo", () => {
    expect(normalizeRemote("https://github.com/acme/app.git")).toBe("github.com/acme/app")
    expect(normalizeRemote("https://gitlab.com/group/sub/project/")).toBe("gitlab.com/group/sub/project")
  })

  it("turns scp-style remotes into host/owner/repo", () => {
    expect(normalizeRemote("git@github.com:acme/app.git")).toBe("github.com/acme/app")
    expect(normalizeRemote("git@gitlab.example.com:team/repo.git")).toBe("gitlab.example.com/team/repo")
  })

  it("drops a trailing .git and normalises the path", () => {
    expect(normalizeRemote("https://github.com/acme/app/")).toBe("github.com/acme/app")
  })

  it("returns undefined for empty input", () => {
    expect(normalizeRemote("")).toBeUndefined()
  })
})

describe("parseGitState", () => {
  it("reads branch, entries and churn", () => {
    const status = ["## feat/x...origin/feat/x", " M src/a.ts", "?? new.py", "R  old.ts -> new.ts"].join("\n")
    const numstat = ["12\t3\tsrc/a.ts", "0\t0\tnew.py"].join("\n")
    const snapshot = parseGitState(status, numstat)
    expect(snapshot.branch).toBe("feat/x")
    expect(snapshot.dirty).toBe(true)
    expect(snapshot.files.map((f) => f.path)).toEqual(["new.py", "new.ts", "src/a.ts"])
    expect(snapshot.files.find((f) => f.path === "src/a.ts")?.additions).toBe(12)
  })

  it("reports a clean tree", () => {
    const snapshot = parseGitState("## main")
    expect(snapshot.dirty).toBe(false)
    expect(snapshot.files).toEqual([])
  })
})

describe("snapshotFingerprint", () => {
  it("is stable across file ordering and changes with content", () => {
    const a = { dirty: true, head: "1", branch: "main", files: [{ path: "b" }, { path: "a" }] }
    const b = { dirty: true, head: "1", branch: "main", files: [{ path: "a" }, { path: "b" }] }
    expect(snapshotFingerprint(a)).toBe(snapshotFingerprint(b))
    expect(snapshotFingerprint(a)).not.toBe(snapshotFingerprint({ ...a, files: [{ path: "a" }] }))
  })
})

describe("parseCommitLog", () => {
  it("parses the NUL-safe git log format", () => {
    const raw =
      "sha1\u001fparent1 parent2\u001fAda\u001fada@example.com\u001f2026-01-02T03:04:05+01:00\u001ffeat: x\u001e" +
      "sha2\u001f\u001fBob\u001fbob@example.com\u001f2026-01-01T00:00:00Z\u001ffix: y\u001e"
    const commits = parseCommitLog(raw, "main")
    expect(commits).toHaveLength(2)
    expect(commits[0]).toMatchObject({ sha: "sha1", parents: ["parent1", "parent2"], subject: "feat: x", branch: "main" })
    expect(commits[1].parents).toEqual([])
    expect(COMMIT_LOG_FORMAT).toContain("%x1f")
  })
})

describe("CommitWatcher", () => {
  it("queues only new commits and tracks HEAD", () => {
    const watcher = new CommitWatcher()
    const first = { sha: "a", parents: [], committedAt: "2026-01-01T00:00:00Z" }
    const second = { sha: "b", parents: ["a"], committedAt: "2026-01-02T00:00:00Z" }
    expect(watcher.observe("repo", [first])).toHaveLength(1)
    expect(watcher.head("repo")).toBe("a")
    expect(watcher.observe("repo", [first, second])).toHaveLength(2)
    expect(watcher.drain("repo").map((c) => c.sha)).toEqual(["a", "b"])
    expect(watcher.drain("repo")).toEqual([])
  })
})

describe("extractFileChanges", () => {
  it("collects explicit paths and apply-patch headers", () => {
    expect(extractFileChanges("edit", { filePath: "/x/a.ts" })).toEqual(["/x/a.ts"])
    expect(extractFileChanges("read", { paths: ["b.py", "b.py"] })).toEqual(["b.py"])
    const patch = "*** Begin Patch\n*** Update File: src/z.ts\n--- a/src/q.ts\n+++ b/src/q.ts\n*** End Patch"
    expect(extractFileChanges("apply_patch", { patch })).toEqual(["src/z.ts", "src/q.ts"])
    expect(extractFileChanges("bash", { command: "ls" })).toEqual([])
  })
})
