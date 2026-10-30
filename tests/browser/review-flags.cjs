/** Optional real-browser regressions. See docs/letools-integration.md. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const esbuild = require("esbuild");
const { chromium } = require("playwright-core");

async function main() {
  const ui = path.resolve(__dirname, "../../pipeline/server/viewer_ui");
  const temporary = fs.mkdtempSync(
    path.join(os.tmpdir(), "review-flags-browser-"),
  );
  const bundle = path.join(temporary, "bundle.js");
  await esbuild.build({
    entryPoints: [path.join(__dirname, "review-flags-app.tsx")],
    bundle: true,
    outfile: bundle,
    nodePaths: [
      path.dirname(path.dirname(require.resolve("react/package.json"))),
    ],
    plugins: [
      {
        name: "fixtures",
        setup(build) {
          build.onResolve({ filter: /^@\// }, ({ path: name }) => ({
            path: /flagged-episodes-context/.test(name)
              ? path.join(ui, "review-flags-context.tsx")
              : /batch-preview-model/.test(name)
                ? path.join(ui, "batch-preview-model.ts")
                : /review-flags-store/.test(name)
                  ? path.join(ui, "review-flags-store.ts")
                  : path.join(__dirname, "review-flags-fixtures.tsx"),
          }));
        },
      },
    ],
  });
  let race = false,
    requests = 0,
    held,
    received;
  const firstReceived = new Promise((resolve) => {
    received = resolve;
  });
  const cleanup = (generation) => ({
    entries: [],
    operation: null,
    cleanup_generation: generation,
    cleanup_history: [
      { id: "A", from: 0, to: 1, mapping: { 1: 0, 2: 1, 3: 2, 4: 3 } },
      { id: "B", from: 1, to: 2, mapping: { 1: 0, 2: 1, 3: 2 } },
    ].slice(0, generation),
  });
  const server = http.createServer((request, response) => {
    if (request.url.startsWith("/api/recycle")) {
      response.setHeader("Content-Type", "application/json");
      if (race && ++requests === 1) {
        held = response;
        received();
        return;
      }
      response.end(JSON.stringify(cleanup(race ? 2 : 0)));
    } else if (request.url === "/bundle.js") {
      response.setHeader("Content-Type", "application/javascript");
      response.end(fs.readFileSync(bundle));
    } else {
      response.setHeader("Content-Type", "text/html");
      response.end(
        '<html><div id="root"></div><script src="/bundle.js"></script></html>',
      );
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      executablePath:
        process.env.CHROME_BIN ||
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    });
    const context = await browser.newContext(),
      page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const url = `http://127.0.0.1:${server.address().port}`;
    await page.goto(url + "/?flags");
    await page.getByRole("button", { name: "initialize", exact: true }).click();
    await page.getByRole("button", { name: "toggle 3", exact: true }).click();
    await page.reload();
    await page.getByRole("button", { name: "catch up", exact: true }).click();
    assert.equal(await page.locator("#flags").textContent(), "[1]");
    console.log("PASS: closed browser catches up on two cleanups");

    await page.evaluate(() => localStorage.clear());
    await page.reload();
    await page.getByRole("button", { name: "initialize", exact: true }).click();
    await page.evaluate(() => {
      window.savedSetItem = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        if (key.includes("review-flags:"))
          throw new DOMException("full", "QuotaExceededError");
        return window.savedSetItem.call(this, key, value);
      };
    });
    await page.getByRole("button", { name: "toggle 0", exact: true }).click();
    await page.getByRole("button", { name: "toggle 1", exact: true }).click();
    await page.evaluate(() => {
      Storage.prototype.setItem = window.savedSetItem;
    });
    await page.getByRole("button", { name: "toggle 2", exact: true }).click();
    assert.equal(await page.locator("#flags").textContent(), "[0,1,2]");
    console.log("PASS: storage recovery preserves in-page flags");

    const other = await context.newPage();
    await other.goto(url + "/?flags");
    await other.getByRole("button", { name: "toggle 3", exact: true }).click();
    await page.waitForFunction(
      () => document.querySelector("#flags").textContent === "[0,1,2,3]",
    );
    await other.close();
    console.log("PASS: same-origin tabs synchronize flags");

    await page.evaluate(() =>
      localStorage.setItem(
        "bodypipeline:review-flags:v2:bp%2Ftest",
        JSON.stringify({ version: 2, generation: 1, flags: [3], notice: "" }),
      ),
    );
    race = true;
    await page.goto(url, { waitUntil: "domcontentloaded" });
    await Promise.race([
      firstReceived,
      new Promise((_, reject) => {
        const timer = setTimeout(
          () => reject(new Error("No recycle request")),
          10000,
        );
        timer.unref();
      }),
    ]);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.waitForFunction(
      () =>
        JSON.parse(
          localStorage.getItem("bodypipeline:review-flags:v2:bp%2Ftest"),
        ).generation === 2,
    );
    held.end(JSON.stringify(cleanup(1)));
    await page.evaluate(
      () =>
        new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)),
        ),
    );
    const snapshot = await page.evaluate(() =>
      JSON.parse(
        localStorage.getItem("bodypipeline:review-flags:v2:bp%2Ftest"),
      ),
    );
    assert.deepEqual(snapshot.flags, [2]);
    assert.equal(snapshot.generation, 2);
    assert.deepEqual(errors, []);
    console.log("PASS: cancelled stale response cannot remap flags again");
  } finally {
    await browser?.close();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(temporary, { recursive: true });
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
