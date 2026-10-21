import { Plugin } from "@opencode/plugin"

/**
 * Tags every model request with its OpenCode flow kind
 * (primary | compaction | title | generate) via the x-oc-kind header, and with
 * the session id via x-oc-session.
 *
 * llm-auto-router uses these to route auxiliary calls (title/compaction/generate)
 * to the cheap `low` preset regardless of the session's model tier, and to group
 * spend by session on the dashboard.
 */
export default Plugin.define({
  id: "router-tags",
  async setup(ctx) {
    await ctx.session.hook("http.request", (event) => {
      event.request.headers.set("x-oc-kind", event.kind)
      event.request.headers.set("x-oc-session", event.sessionID)
    })
  },
})
