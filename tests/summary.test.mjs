import { test } from "node:test";
import assert from "node:assert/strict";
import { summary } from "../mods/agent-resume/summary.js";

const item = (terminal, agent, where, reason) => ({ terminal, agent, where, session: "s", ...(reason ? { reason } : {}) });

test("lists where agents came back", () => {
  const n = summary({ resumed: [item("t1", "claude", "api / 1"), item("t2", "codex", null)], skipped: [] });
  assert.deepEqual(n, { title: "Resumed 2 agents", body: "api / 1, t2", level: "success", timeout: 5000 });
});

test("says what it couldn't bring back, but not sessions open elsewhere", () => {
  const n = summary({
    resumed: [item("t1", "claude", "api / 1")],
    skipped: [item("t2", "codex", "web / 2", "something else is running there"), item("t3", "claude", "x / 1", "open in another terminal")],
  });
  assert.equal(n.title, "Resumed 1 of 2 agents");
  assert.equal(n.body, "Not resumed: web / 2: something else is running there");
  assert.equal(n.level, "warning");
});

test("nothing to do", () => {
  assert.equal(summary({ resumed: [], skipped: [] }).title, "No agent to resume");
  assert.equal(summary({ resumed: [], skipped: [item("t3", "claude", "x / 1", "open in another terminal")] }).title, "No agent to resume");
  const n = summary({ resumed: [], skipped: [item("t2", "codex", "web / 2", "its folder is gone")] });
  assert.equal(n.title, "Couldn't resume 1 agent");
  assert.equal(n.body, "web / 2: its folder is gone");
});

test("a second press while a pass runs", () => {
  assert.equal(summary({ busy: true }).title, "Already resuming agents");
});
