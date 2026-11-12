import { Plugin } from "@opencode/plugin"

/**
 * Tags every model request with its OpenCode flow kind
 * (primary | compaction | title | generate) via the x-oc-kind header, and with
 * the session id via x-oc-session, and project identity via x-oc-project,
 * x-oc-project-root and x-oc-worktree.
 *
 * llm-auto-router uses these to route auxiliary calls (title/compaction/generate)
 * to the cheap `low` preset regardless of the session's model tier, and to group
 * spend by session on the dashboard.
 */
export default Plugin.define({
  id: "router-tags",
  async setup(ctx) {
    const location = ctx.location
    const project = location.project
    await ctx.session.hook("http.request", (event) => {
      event.request.headers.set("x-oc-kind", event.kind)
      event.request.headers.set("x-oc-session", event.sessionID)
      if (project?.id) event.request.headers.set("x-oc-project", project.id)
      const root = project?.canonical || project?.directory
      if (root) event.request.headers.set("x-oc-project-root", root)
      if (location.directory) event.request.headers.set("x-oc-worktree", location.directory)
    })
  },
})
