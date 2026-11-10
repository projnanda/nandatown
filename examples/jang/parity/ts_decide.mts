/**
 * Decide every recorded view with JANG's own TypeScript strategy and store the
 * result as `ts` next to the Python decision. Run from a JANGmarket checkout:
 *
 *   npx tsx <nandatown>/examples/jang/parity/ts_decide.mts <nandatown>/examples/jang/parity/decide_cases.json
 */
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const file = process.argv[2];
const repo = process.cwd();
const { decide } = await import(pathToFileURL(resolve(repo, "agents/rule-strategy.ts")).href);

const data = JSON.parse(readFileSync(file, "utf8"));
for (const c of data.cases) {
  const d = decide(c.view, c.options);
  c.ts = d;
}
writeFileSync(file, JSON.stringify(data, null, 1));
console.log(`decided ${data.cases.length} views with the TypeScript strategy`);
