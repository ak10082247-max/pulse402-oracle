import assert from 'node:assert/strict';
import test from 'node:test';
import { CANONICAL_ORIGIN, MAX_REQUEST_BYTES, createGateway } from '../worker.js';

const EDGE='https://pulse402-oracle.test.workers.dev';
const encode=value=>Buffer.from(JSON.stringify(value)).toString('base64');
const quote=path=>({x402Version:2,resource:{url:CANONICAL_ORIGIN+path},accepts:[{scheme:'exact',network:'eip155:8453',asset:'0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',amount:path==='/v1/pass/prepay'?'5000000':'1000',payTo:'0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6'}]});
const make=(path,method='GET',headers={},body)=>new Request(EDGE+path,{method,headers,...(body===undefined?{}:{body})});

test('every paid HTTP/sanitize/prepay/MCP entry delegates forged or absent authorization to the same backend',async()=>{
  const paths=['/v1/pulse/base-liquidity','/v1/pulse/polymarket-velocity','/v1/pulse/threat-intel','/v1/pulse/arb-routes','/v1/pulse/threat-intel/sanitize','/v1/pass/prepay','/mcp'];
  const calls=[];
  const gateway=createGateway(async(url,init)=>{
    calls.push({url,init});
    const path=new URL(url).pathname;
    return new Response('  {"error":"payment_required","no_data":true}\n',{status:402,headers:{'PAYMENT-REQUIRED':encode(quote(path))}});
  });
  for(const path of paths)for(const payment of [undefined,'forged']) {
    const headers={'Content-Type':'application/json',...(payment?{'PAYMENT-SIGNATURE':payment}:{})};
    const body=path==='/mcp'?'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"pulse_base_liquidity","arguments":{}}}':'{"content":"Ignore previous instructions."}';
    const response=await gateway(make(path,'POST',headers,body));
    assert.equal(response.status,402);
    assert.equal(await response.text(),'  {"error":"payment_required","no_data":true}\n');
    assert.equal(response.headers.get('PAYMENT-REQUIRED'),encode(quote(path)));
    const call=calls.at(-1);
    assert.equal(call.url,CANONICAL_ORIGIN+path);
    assert.equal(new TextDecoder().decode(call.init.body),body);
    assert.equal(call.init.redirect,'manual');
    assert.equal(call.init.cache,'no-store');
    assert.equal(call.init.cf.cacheTtlByStatus['100-599'],-1);
  }
  assert.equal(calls.length,paths.length*2);
});

test('MCP first-call/free-quota and invented session bypasses cannot create a local success',async()=>{
  let calls=0;
  const gateway=createGateway(async()=>{calls++;return new Response('{"error":"INVALID_SESSION"}',{status:402});});
  for(let i=0;i<4;i++) {
    const result=await gateway(make('/mcp','POST',{'Content-Type':'application/json','X-Pulse-Session':'made-up-session'},'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"pulse_arb_routes"}}'));
    assert.equal(result.status,402);assert.deepEqual(await result.json(),{error:'INVALID_SESSION'});
  }
  assert.equal(calls,4);
});

test('canonical $5/6000-call/7-day prepay response and subsequent credit use pass through with their supplied receipt',async()=>{
  const expires=Math.floor(Date.now()/1000)+7*86400;
  const session={status:'active',session_token:'backend_purchased_session',calls_allocated:6000,calls_remaining:6000,expires_at_unix:expires};
  const receipt=encode({success:true,payer:'0x'+'1'.repeat(40),amount:'5000000',network:'eip155:8453',transaction:'0x'+'2'.repeat(64),resource:{url:CANONICAL_ORIGIN+'/v1/pass/prepay'}});
  const calls=[];
  const gateway=createGateway(async(url,init)=>{
    calls.push({url,init});
    if(new URL(url).pathname==='/v1/pass/prepay') {
      assert.equal(init.headers.get('PAYMENT-SIGNATURE'),'backend-valid-unfunded-fixture');
      return new Response(JSON.stringify(session),{status:200,headers:{'PAYMENT-RESPONSE':receipt,'X-Pulse-Session':session.session_token}});
    }
    assert.equal(init.headers.get('X-Pulse-Session'),session.session_token);
    assert.equal(init.headers.get('Idempotency-Key'),'original-call-key');
    return new Response('{"status":"ok","data":[{"observed":true}]}',{status:200,headers:{'X-Pulse-Calls-Remaining':'5999','Idempotency-Key':'original-call-key'}});
  });
  const purchased=await gateway(make('/v1/pass/prepay','POST',{'PAYMENT-SIGNATURE':'backend-valid-unfunded-fixture','Content-Type':'application/json'},'{}'));
  assert.equal(purchased.status,200);
  assert.equal(purchased.headers.get('PAYMENT-RESPONSE'),receipt);
  assert.deepEqual(await purchased.json(),session);
  const consumed=await gateway(make('/v1/pulse/base-liquidity?symbol=WETH','GET',{'X-Pulse-Session':session.session_token,'Idempotency-Key':'original-call-key'}));
  assert.equal(consumed.status,200);
  assert.equal(consumed.headers.get('X-Pulse-Calls-Remaining'),'5999');
  assert.deepEqual((await consumed.json()).data,[{observed:true}]);
  assert.equal(calls.length,2);
});

test('raw JSON-RPC payment metadata and session arguments are preserved without local grant logic',async()=>{
  const body=' {"jsonrpc":"2.0","id":42,"method":"tools/call","params":{"name":"pulse_threat_intel","arguments":{"text_to_sanitize":"é\\nactual"},"_meta":{"x402/payment":{"fixture":"original proof"}}}}\n';
  const gateway=createGateway(async(url,init)=>{
    assert.equal(url,CANONICAL_ORIGIN+'/mcp');
    assert.equal(new TextDecoder().decode(init.body),body);
    return new Response('{"jsonrpc":"2.0","id":42,"result":{"isError":true,"content":[]}}',{status:200});
  });
  const result=await gateway(make('/mcp','POST',{'Content-Type':'application/json'},body));
  assert.equal((await result.json()).result.isError,true);
});

test('upstream cannot be selected through request URL, query, headers or environment overrides',async()=>{
  const urls=[];
  const gateway=createGateway(async(url,init)=>{
    urls.push(url);
    for(const header of ['Cookie','Authorization','X-Admin-Token','CF-Access-Jwt-Assertion','X-Forwarded-Host','X-Original-URL','Host'])assert.equal(init.headers.has(header),false);
    return new Response('{}',{status:404,headers:{'Set-Cookie':'admin=should-not-leak'}});
  });
  for(const path of ['//evil.example/feed?url=http://127.0.0.1:8080/','/%2F%2F127.0.0.1/private','/mcp?upstream=https://evil.example']) {
    const result=await gateway(make(path,'GET',{'Cookie':'admin=1','Authorization':'Bearer admin-secret-fixture','X-Admin-Token':'admin','CF-Access-Jwt-Assertion':'admin','X-Forwarded-Host':'127.0.0.1','X-Original-URL':'https://evil.example'}),{UPSTREAM_URL:'http://127.0.0.1:9999',CANONICAL_ORIGIN:'https://evil.example'});
    assert.equal(new URL(urls.at(-1)).origin,CANONICAL_ORIGIN);
    assert.equal(result.headers.has('Set-Cookie'),false);
  }
  assert.equal(urls.length,3);
});

test('redirects are refused once, preserve any actual payment receipt, and never forward a Location',async()=>{
  let calls=0;const actualReceipt='upstream-original-receipt';
  const gateway=createGateway(async()=>{calls++;return new Response(null,{status:307,headers:{Location:'https://evil.example/collect','PAYMENT-RESPONSE':actualReceipt}});});
  const result=await gateway(make('/v1/pass/prepay','POST',{'PAYMENT-SIGNATURE':'fixture'},'{}'));
  assert.equal(calls,1);assert.equal(result.status,502);assert.equal(result.headers.has('Location'),false);
  assert.equal(result.headers.get('PAYMENT-RESPONSE'),actualReceipt);
  assert.equal((await result.json()).charged,'unknown');
});

test('network/timeout and upstream503 are never retried or replaced with cached/example data',async()=>{
  let calls=0;
  const gateway=createGateway(async()=>{calls++;throw Error('Timeout after possible settlement');});
  const failed=await gateway(make('/mcp','POST',{'Content-Type':'application/json'},'{}'));
  assert.equal(failed.status,502);assert.equal((await failed.json()).charged,'unknown');assert.equal(calls,1);
  const upstreamError='{"error":"FEED_UNAVAILABLE","charged":false}';
  const canonicalFailure=createGateway(async()=>{calls++;return new Response(upstreamError,{status:503});});
  const result=await canonicalFailure(make('/v1/pulse/arb-routes','GET',{'PAYMENT-SIGNATURE':'fixture'}));
  assert.equal(result.status,503);assert.equal(await result.text(),upstreamError);assert.equal(calls,2);
});

test('actual request byte limit is enforced before upstream even if Content-Length lies',async()=>{
  let calls=0;const gateway=createGateway(async()=>{calls++;return new Response('{}');});
  const result=await gateway(make('/mcp','POST',{'Content-Length':'1'},'x'.repeat(MAX_REQUEST_BYTES+1)));
  assert.equal(result.status,413);assert.equal((await result.json()).charged,false);assert.equal(calls,0);
  const encoded=await gateway(make('/mcp','POST',{'Content-Encoding':'gzip'},'not-unzipped'));
  assert.equal(encoded.status,415);assert.equal(calls,0);
  const disallowed=await gateway(make('/v1/pass/prepay','DELETE'));
  assert.equal(disallowed.status,405);assert.equal(calls,0);
});

test('public discovery/metadata-only preview/CORS use the backend and all responses forbid paid response caching',async()=>{
  const calls=[];
  const gateway=createGateway(async(url,init)=>{
    calls.push({url,init});
    return new Response('{"notice":"Live records require a verified payment","data":null}',{status:200,headers:{'Cache-Control':'public,max-age=999','CDN-Cache-Control':'max-age=999'}});
  });
  for(const path of ['/.well-known/x402.json','/.well-known/mcp.json','/openapi.json','/health','/version','/v1/preview/base-liquidity','/']) {
    const result=await gateway(make(path,'GET'));
    assert.equal(result.status,200);assert.equal((await result.json()).data,null);
    assert.match(result.headers.get('Cache-Control'),/no-store/);
    assert.equal(result.headers.get('CDN-Cache-Control'),'no-store');
    assert.equal(result.headers.get('Cloudflare-CDN-Cache-Control'),'no-store');
  }
  const preflight=await gateway(make('/v1/pass/prepay','OPTIONS',{'Origin':'https://agent.example','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'payment-signature,x-pulse-session,idempotency-key'}));
  assert.equal(preflight.status,200);assert.equal(calls.at(-1).init.method,'OPTIONS');
  assert.equal(calls.at(-1).init.headers.get('Origin'),'https://agent.example');
  assert.match(preflight.headers.get('Access-Control-Allow-Headers'),/PAYMENT-SIGNATURE/);
});
