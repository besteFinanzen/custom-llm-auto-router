# OpenCode integration

Two pieces make llm-auto-router a first-class OpenCode provider:

1. **Provider config** (`opencode.jsonc.example`) — registers a `router` provider
   with the five presets as selectable models.
2. **Plugin** (`plugins/router-tags/index.ts`) — tags each request with the
   OpenCode flow kind (`x-oc-kind`), session id (`x-oc-session`), stable project
   id (`x-oc-project`), project root (`x-oc-project-root`) and current worktree
   (`x-oc-worktree`) so the gateway can send auxiliary calls
   (title/compaction/generate) to the cheap `low` preset and attribute durable
   usage and work history to sessions and projects.

## Install

```bash
# 1. provider config — merge providers.router + the plugins entry into yours
$EDITOR ~/.config/opencode/opencode.jsonc   # see opencode.jsonc.example

# 2. plugin
mkdir -p ~/.config/opencode/plugins
cp -r plugins/router-tags ~/.config/opencode/plugins/router-tags
```

Make sure the gateway is running first (`python gateway.py`, listening on
`http://[IP_ADDRESS]:4141` by default) and adjust `settings.baseURL` if you bound
it elsewhere.

## Use

```bash
# pick a preset interactively
/models

# or on the command line
opencode --model router/high

# or pin one in an agent definition
#   "model": "router/orchestrator"   // primary agent
#   "model": "router/coder"          // execution subagent
```

If the plugin is not installed the gateway still works — it just treats every
call as a `primary` request and cannot attribute spend to a session or project.
Recopy the plugin after upgrading to enable project and worktree attribution.
No API session lookup or prompt content is needed for these identity headers.
