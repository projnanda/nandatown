import assert from "node:assert/strict";
import { test } from "node:test";
import { createDatasetCache, PRFetchError } from "../src/lib/pr-cache";
import { EMPTY_DATASET } from "../src/lib/hackathon-types";

test("webhook bursts retain a pending refresh and respect the anonymous request budget", async () => {
  let now = 0, loads = 0;
  const cache = createDatasetCache(async () => ({...EMPTY_DATASET, generated_at:String(++loads), stats:{...EMPTY_DATASET.stats,total_submissions:307}}), {now:()=>now, authenticated:false});
  assert.equal((await cache.get()).generated_at, "1");
  for (let i=0; i<20; i++) { cache.invalidate(); await cache.get(); }
  assert.equal(loads, 1);
  now = 300_000;
  assert.equal((await cache.get()).generated_at, "2");
  assert.equal(loads, 2);
});

test("periodic refresh works without a webhook and failures keep the last complete snapshot with backoff", async () => {
  let now = 0, loads = 0, fail = false;
  const cache = createDatasetCache(async () => { loads++; if (fail) throw new Error("GitHub 403"); return {...EMPTY_DATASET,generated_at:String(loads)}; }, {now:()=>now,authenticated:false});
  await cache.get();
  now = 600_000;
  fail = true;
  assert.equal((await cache.get()).generated_at, "1");
  assert.equal((await cache.get()).generated_at, "1");
  assert.equal(loads, 2);
  now += 300_000;
  fail = false;
  assert.equal((await cache.get()).generated_at, "3");
});

test("large histories stay cached and refresh frequency scales with pagination cost", async () => {
  let now = 0, loads = 0;
  const large = {...EMPTY_DATASET, generated_at:"ready", stats:{...EMPTY_DATASET.stats,total_submissions:1001}, padding:"x".repeat(5_000_000)};
  const cache = createDatasetCache(async () => { loads++; return large; }, {now:()=>now,authenticated:false});
  await cache.get();
  cache.invalidate();
  now = 600_000;
  assert.equal(await cache.get(), large);
  assert.equal(loads, 1);
  now = 825_000; // 11 pages * 60 minutes / 48 requests per hour
  await cache.get();
  assert.equal(loads, 2);
});

test("concurrent readers share one fetch and invalidation during a fetch is retained", async () => {
  let now = 0, loads = 0;
  let release!: () => void;
  const gate = new Promise<void>(resolve => {release=resolve;});
  const cache = createDatasetCache(async () => { loads++; await gate; return {...EMPTY_DATASET,generated_at:String(loads)}; }, {now:()=>now,authenticated:false});
  const a = cache.get(), b = cache.get();
  cache.invalidate();
  release();
  await Promise.all([a,b]);
  assert.equal(loads, 1);
  now = 300_000;
  await cache.get();
  assert.equal(loads, 2);
});

test("a cold failure is reported and repeated visitors do not hammer GitHub", async () => {
  let loads = 0;
  const cache = createDatasetCache(async () => { loads++; throw new Error("GitHub unavailable"); }, {now:()=>0,authenticated:false});
  await assert.rejects(cache.get, /GitHub unavailable/);
  await assert.rejects(cache.get, /GitHub unavailable/);
  assert.equal(loads, 1);
});

test("failed partial pagination and GitHub retry delays count toward the refresh budget", async () => {
  let now = 0, loads = 0;
  const cache = createDatasetCache(async () => { loads++; throw new PRFetchError("GitHub 403", 11, 900_000); }, {now:()=>now,authenticated:false});
  await assert.rejects(cache.get, /403/);
  now = 825_000;
  await assert.rejects(cache.get, /403/);
  assert.equal(loads, 1);
  now = 900_000;
  await assert.rejects(cache.get, /403/);
  assert.equal(loads, 2);
});
