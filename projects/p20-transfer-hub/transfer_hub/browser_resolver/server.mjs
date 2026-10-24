import http from 'node:http';
import dns from 'node:dns/promises';
import net from 'node:net';
import { chromium } from '@playwright/test';

const streamRe = /(?:\.m3u8(?:[?#]|$)|\.mpd(?:[?#]|$)|\.ism\/manifest(?:[?#]|$))/i;

function blockedIPv4(address) {
  const parts = address.split('.').map(Number);
  if (parts.length !== 4 || parts.some(n => !Number.isInteger(n) || n < 0 || n > 255)) return true;
  const [a,b,c] = parts;
  return a === 0 || a === 10 || a === 127 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    (a === 192 && b === 0 && c === 0) ||
    (a === 192 && b === 0 && c === 2) ||
    (a === 198 && (b === 18 || b === 19)) ||
    (a === 198 && b === 51 && c === 100) ||
    (a === 203 && b === 0 && c === 113) || a >= 224;
}

function blockedIp(address) {
  const version = net.isIP(address);
  if (version === 4) return blockedIPv4(address);
  if (version !== 6) return true;
  const value = address.toLowerCase();
  if (value === '::' || value === '::1' || value.startsWith('fc') || value.startsWith('fd') ||
      /^fe[89ab]/.test(value) || value.startsWith('ff') || value.startsWith('2001:db8:')) return true;
  if (value.startsWith('::ffff:')) {
    const mapped = value.slice('::ffff:'.length);
    if (net.isIP(mapped) === 4) return blockedIPv4(mapped);
  }
  return false;
}

async function publicUrl(value, cache = new Map()) {
  try {
    const u = new URL(value);
    if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) return false;
    const key = u.hostname.toLowerCase();
    if (cache.has(key)) return cache.get(key);
    const records = net.isIP(u.hostname)
      ? [{address:u.hostname}]
      : await dns.lookup(u.hostname, {all:true, verbatim:true});
    const safe = records.length > 0 && records.every(record => !blockedIp(record.address));
    cache.set(key, safe);
    return safe;
  } catch {
    return false;
  }
}

const json = (res, code, body) => {
  res.writeHead(code, {'content-type':'application/json'});
  res.end(JSON.stringify(body));
};

async function resolve(source) {
  const safetyCache = new Map();
  if (!await publicUrl(source, safetyCache)) throw new Error('unsafe_source');
  const browser = await chromium.launch({headless:true});
  const context = await browser.newContext();
  const page = await context.newPage();
  const candidates=[];
  let navigationFailed = false;
  await context.route('**/*', async route => {
    const target = route.request().url();
    if (target.startsWith('data:') || target.startsWith('blob:') || target === 'about:blank') return route.continue();
    if (await publicUrl(target, safetyCache)) return route.continue();
    return route.abort('blockedbyclient');
  });
  page.on('request', req => {
    if (streamRe.test(req.url())) candidates.push({url:req.url(), headers:req.headers()});
  });
  try {
    await page.goto(source,{waitUntil:'domcontentloaded',timeout:20000});
    await page.waitForTimeout(5000);
  } catch {
    navigationFailed = true;
  }
  const ua=await page.evaluate(() => navigator.userAgent).catch(()=>null);
  if (!candidates.length && navigationFailed) {
    await browser.close();
    throw new Error('unsafe_redirect_or_navigation_failed');
  }
  const target=candidates.at(-1) || {url:source,headers:{}};
  if (!await publicUrl(target.url, safetyCache)) {
    await browser.close();
    throw new Error('unsafe_resolved_target');
  }
  const cookies=await context.cookies(target.url).catch(()=>[]);
  await browser.close();
  const headers={};
  for (const key of ['referer','origin','user-agent','authorization']) {
    if (target.headers[key]) headers[key.replace(/(^|-)(.)/g,(_,a,b)=>a+b.toUpperCase())]=target.headers[key];
  }
  if (ua && !headers['User-Agent']) headers['User-Agent']=ua;
  if (cookies.length) headers.Cookie=cookies.map(c=>`${c.name}=${c.value}`).join('; ');
  return {url:target.url,engine:streamRe.test(target.url)?'nm3u8dl':'aria2',headers};
}

http.createServer(async (req,res) => {
  if (req.method==='GET' && req.url==='/health') return json(res,200,{ok:true,browser:'chromium'});
  if (req.method!=='POST' || req.url!=='/operations/resolve_source') return json(res,404,{error:'not found'});
  let raw='';
  for await (const chunk of req) {
    raw+=chunk;
    if(raw.length>65536) return json(res,413,{error:'too large'});
  }
  try {
    const body=JSON.parse(raw||'{}');
    if(!await publicUrl(body.source)) return json(res,400,{error:'public http(s) source required'});
    return json(res,200,await resolve(body.source));
  } catch(e) {
    const error = ['unsafe_source','unsafe_redirect_or_navigation_failed','unsafe_resolved_target'].includes(e?.message)
      ? e.message : (e?.name || 'browser_error');
    return json(res,502,{error});
  }
}).listen(17611,'127.0.0.1');
