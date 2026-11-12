/** Browser bridge for the separately supplied, trusted SKOPOS HTML prototype.
 * Does not reimplement its policy, data, rendering, controls or explanations.
 */
import fs from 'node:fs';
import http from 'node:http';
import crypto from 'node:crypto';
import {chromium} from 'playwright';

const source = fs.readFileSync(process.env.SKOPOS_HTML);
const sourceSha256 = crypto.createHash('sha256').update(source).digest('hex');
const expected = process.env.SKOPOS_SOURCE_SHA256;
if (!expected || sourceSha256 !== expected) throw Error('SKOPOS source fingerprint mismatch');
const token = process.env.SKOPOS_TEST_TOKEN;
if (!token) throw Error('ephemeral test token required');
let page, browser, revision = 1;
const replay = new Map();
let queue = Promise.resolve();
const errors = [];
const snapshot = async () => ({revision, source_sha256:sourceSha256,
  ...await page.evaluate(() => ({
    title:document.title,
    counts:{urgent:+document.getElementById('cU').textContent,uncertain:+document.getElementById('cD').textContent,monitor:+document.getElementById('cM').textContent,total:+document.getElementById('cT').textContent},
    rows:[...document.querySelectorAll('#rows tr')].map(row => ({id:row.cells[1].innerText.split('\n')[0],bin:row.cells[8].textContent,deadline:row.cells[9].innerText,tca_hours:Number(row.cells[2].innerText)})),
    detail:document.getElementById('detail').textContent,
    synthetic_banner:document.querySelector('.banner').innerText,
    human_boundary:document.querySelector('.foot').innerText,
    sort_mode:document.getElementById('bySk').className==='on'?'skopos':'probability'
  }))});
function response(res, code, data) { res.writeHead(code, {'Content-Type':'application/json'});res.end(JSON.stringify(data)); }
async function operation(body) {
  if (!body || typeof body!=='object' || Array.isArray(body) || Object.keys(body).some(k=>!['request_id','operation','expected_revision','warning_id'].includes(k)) || typeof body.request_id!=='string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(body.request_id)) return [422,{status:'REJECTED',reason:'invalid_request'}];
  const fingerprint = JSON.stringify(Object.fromEntries(Object.entries(body).sort()));
  if (replay.has(body.request_id)) {
    const previous = replay.get(body.request_id);
    return previous.fingerprint===fingerprint ? [200,{...previous.result,replayed:true}] : [409,{status:'REJECTED',reason:'request_identity_reuse'}];
  }
  if (!['snapshot','sort','select','update_tracking','reset'].includes(body.operation)) return [422,{status:'REJECTED',reason:'unsupported_operation'}];
  if (body.expected_revision!==revision) return [409,{status:'REJECTED',reason:'stale_revision',current_revision:revision}];
  if (body.operation==='select') {
    if (typeof body.warning_id!=='string' || !/^CDM-\d{4}$/.test(body.warning_id)) return [422,{status:'REJECTED',reason:'invalid_warning_id'}];
    const matches = page.locator('#rows tr').filter({has:page.locator('td:nth-child(2)', {hasText:body.warning_id})});
    if (await matches.count()!==1) return [422,{status:'REJECTED',reason:'unknown_warning'}];
    await matches.click();
  }
  if (body.operation==='sort') await page.locator('#bySk').click();
  if (body.operation==='update_tracking') {await page.locator('#newTrack').click();revision++;}
  if (body.operation==='reset') {await page.locator('#reset').click();revision++;}
  const result = {status:'COMPLETED',replayed:false,...await snapshot()};
  replay.set(body.request_id,{fingerprint,result});
  return [200,result];
}
const server = http.createServer(async(req,res)=>{
  if(req.method==='GET'&&req.url==='/prototype') {res.writeHead(200,{'Content-Type':'text/html'});res.end(source);return;}
  if(req.method==='GET'&&req.url==='/health') {response(res,200,{ready:!!page,source_sha256:sourceSha256});return;}
  if(req.method==='POST'&&req.url==='/evidence'&&req.headers.authorization==='Bearer '+token) {
    await queue;
    await page.screenshot({path:process.env.SKOPOS_SCREENSHOT,fullPage:true,timeout:5000});
    fs.writeFileSync(process.env.SKOPOS_BROWSER_ERRORS,JSON.stringify(errors));
    response(res,200,{saved:true});return;
  }
  if(req.method!=='POST'||req.url!=='/workflow') {response(res,404,{status:'REJECTED'});return;}
  if(req.headers.authorization!=='Bearer '+token) {response(res,401,{status:'REJECTED'});return;}
  let bytes=0,parts=[];
  for await(const part of req) {bytes+=part.length;if(bytes>16384) {response(res,413,{status:'REJECTED'});return;}parts.push(part);}
  let body;try{body=JSON.parse(Buffer.concat(parts));}catch{response(res,422,{status:'REJECTED',reason:'invalid_json'});return;}
  queue=queue.then(async()=>{try{const [code,data]=await operation(body);response(res,code,data);}catch{response(res,500,{status:'UNKNOWN',reason:'browser_execution_failed'});}});
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const url='http://127.0.0.1:'+server.address().port;
browser=await chromium.launch({headless:true,channel:'chrome'});
page=await browser.newPage({viewport:{width:1500,height:900}});
page.on('pageerror',err=>errors.push(String(err)));
await page.goto(url+'/prototype');
await page.waitForSelector('#rows tr');
fs.writeFileSync(process.env.SKOPOS_READY_FILE,JSON.stringify({url,source_sha256:sourceSha256}));
async function stop(){await queue;if(process.env.SKOPOS_BROWSER_ERRORS) fs.writeFileSync(process.env.SKOPOS_BROWSER_ERRORS,JSON.stringify(errors));await browser.close();server.close(()=>process.exit(0));}
process.on('SIGTERM',stop);process.on('SIGINT',stop);
