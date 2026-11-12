"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../../worktracker/ui/index.html"), "utf8");
const firstScript = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function environment() {
  const stored = new Map();
  const elements = {};
  for (const id of ["auth-dialog", "auth-error", "auth-open", "auth-token", "auth-submit"]) {
    elements[id] = {textContent: "", value: "", open: false};
  }
  elements["auth-dialog"].showModal = function () { this.open = true; };
  elements["auth-dialog"].close = function () { this.open = false; };
  const context = vm.createContext({
    console,
    URL,
    location: {hash: "", origin: "https://tracker.example"},
    window: {addEventListener() {}},
    document: {getElementById: id => elements[id]},
    sessionStorage: {
      getItem: key => stored.get(key) || null,
      setItem: (key, value) => stored.set(key, value),
      removeItem: key => stored.delete(key),
    },
    storage: stored,
    elements,
    responses: [],
    requests: [],
    fetch: async (url, options) => {
      context.requests.push({url, options});
      const response = context.responses.shift();
      if (response instanceof Error) throw response;
      return response;
    },
  });
  vm.runInContext(firstScript + "\n;globalThis.ui = {api, authenticate, signOut};", context);
  return context;
}

function response(ok, status = 200, body = "{}") {
  return {ok, status, statusText: ok ? "OK" : "Unauthorized", text: async () => body};
}

test("the browser attaches its session token and preserves request headers", async () => {
  const env = environment();
  env.sessionStorage.setItem("worktracker.apiToken", "read-secret");
  vm.runInContext('authToken = sessionStorage.getItem("worktracker.apiToken") || "";', env);
  env.responses.push(response(true, 200, '{"projects":[]}'));
  await env.ui.api("/projects", {headers: {"X-Test": "kept"}});
  assert.equal(env.requests[0].url, "/worktracker/api/projects");
  assert.equal(env.requests[0].options.headers.Authorization, "Bearer read-secret");
  assert.equal(env.requests[0].options.headers["X-Test"], "kept");
  assert.equal(env.requests[0].options.headers["Content-Type"], "application/json");
  assert.equal(env.responses.length, 0);
});

test("a rejected token is cleared and opens an actionable login dialog", async () => {
  const env = environment();
  env.responses.push(response(false, 401, '{"detail":"invalid or missing API token"}'));
  await assert.rejects(env.ui.authenticate("wrong"), /invalid or missing API token/);
  assert.equal(env.sessionStorage.getItem("worktracker.apiToken"), null);
  assert.equal(env.elements["auth-dialog"].open, true);
  assert.match(env.elements["auth-error"].textContent, /invalid or missing API token/);
  assert.equal(env.elements["auth-open"].textContent, "Sign in");
});

test("open deployments connect without requiring a token", async () => {
  const env = environment();
  env.responses.push(response(true, 200, '{"scope":"open"}'));
  assert.equal(await env.ui.authenticate(""), true);
  assert.equal(env.sessionStorage.getItem("worktracker.apiToken"), null);
  assert.equal(env.elements["auth-dialog"].open, false);
});

test("network failures do not discard a saved token", async () => {
  const env = environment();
  env.responses.push(new Error("connection refused"));
  await assert.rejects(env.ui.authenticate("read-secret"), /connection refused/);
  assert.equal(env.sessionStorage.getItem("worktracker.apiToken"), "read-secret");
  assert.equal(env.elements["auth-dialog"].open, false);
});

test("sign-out removes the token and hides signed-in identity", () => {
  const env = environment();
  env.sessionStorage.setItem("worktracker.apiToken", "read-secret");
  env.ui.signOut();
  assert.equal(env.sessionStorage.getItem("worktracker.apiToken"), null);
  assert.equal(env.elements["auth-dialog"].open, true);
  assert.equal(env.elements["auth-open"].textContent, "Sign in");
});

test("all dashboard scripts remain valid JavaScript", () => {
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  assert.ok(scripts.length > 0);
  for (const script of scripts) new vm.Script(script[1]);
});
