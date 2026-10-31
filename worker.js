/** Pulse402 edge gateway.
 * One canonical backend owns verification, acquisition, settlement, purchased
 * sessions, idempotency and real data. This Worker cannot mint a pass, grant a
 * free MCP call, sanitize an unpaid payload, or substitute example feed data.
 */
export const CANONICAL_ORIGIN = 'https://pulse402-oracle.onrender.com';
export const MAX_REQUEST_BYTES = 256 * 1024;
const METHODS = new Set(['GET', 'POST', 'HEAD', 'OPTIONS']);
const REQUEST_HEADERS = [
  'Accept', 'Content-Type', 'Origin', 'Access-Control-Request-Method', 'Access-Control-Request-Headers',
  'PAYMENT-SIGNATURE', 'X-PAYMENT', 'X-Pulse-Session', 'Idempotency-Key',
  'MCP-Protocol-Version', 'Mcp-Session-Id', 'Last-Event-Id',
];
const EXPOSE_HEADERS = [
  'PAYMENT-REQUIRED', 'PAYMENT-RESPONSE', 'X-Payment-Required', 'X-Payment-Response',
  'X-402-Amount', 'X-402-Payment-To', 'X-402-Network', 'X-402-Asset', 'X-402-Version',
  'X-Pulse-Session', 'X-Pulse-Calls-Remaining', 'Idempotency-Key', 'Idempotency-Replayed',
];
const RESPONSE_STRIP = ['Connection', 'Keep-Alive', 'Proxy-Authenticate', 'Proxy-Authorization',
  'TE', 'Trailer', 'Transfer-Encoding', 'Upgrade', 'Set-Cookie', 'Set-Cookie2'];

const ROUTE_MIN_ATOMIC = {
  '/v1/pulse/base-liquidity': 20000,
  '/v1/pulse/threat-intel': 20000,
  '/v1/pulse/threat-intel/sanitize': 20000,
  '/v1/pulse/polymarket-velocity': 50000,
  '/v1/pulse/arb-routes': 50000,
  '/v1/pulse/solana-velocity': 50000,
  '/v1/pass/prepay': 10000000,
};

function parsePaymentAmount(headerVal) {
  if (!headerVal || typeof headerVal !== 'string') return null;
  try {
    const raw = headerVal.trim().startsWith('{') ? headerVal : atob(headerVal);
    const parsed = JSON.parse(raw);
    if (parsed.payload?.authorization?.value !== undefined) {
      const v = Number(parsed.payload.authorization.value);
      if (Number.isFinite(v)) return v;
    }
    if (parsed.accepted?.amount !== undefined) {
      const v = Number(parsed.accepted.amount);
      if (Number.isFinite(v)) return v;
    }
  } catch {}
  return null;
}

class InputFailure extends Error {
  constructor(code, status) { super(code); this.status = status; }
}
function responseHeaders(source) {
  const headers = new Headers(source);
  for (const header of RESPONSE_STRIP) headers.delete(header);
  // Personalized payment/session responses must never become a reusable edge
  // cache entry. Protocol quote and receipt headers themselves stay unchanged.
  headers.set('Cache-Control', 'no-store, max-age=0');
  headers.set('CDN-Cache-Control', 'no-store');
  headers.set('Cloudflare-CDN-Cache-Control', 'no-store');
  headers.set('Access-Control-Allow-Origin', '*');
  headers.set('Access-Control-Allow-Methods', [...METHODS].join(', '));
  headers.set('Access-Control-Allow-Headers', REQUEST_HEADERS.join(', '));
  const exposed = new Set((headers.get('Access-Control-Expose-Headers') || '').split(',').map(value => value.trim()).filter(Boolean));
  for (const header of EXPOSE_HEADERS) exposed.add(header);
  headers.set('Access-Control-Expose-Headers', [...exposed].join(', '));
  headers.set('X-Pulse402-Canonical-Origin', CANONICAL_ORIGIN);
  return headers;
}
function failure(code, status, charged = false, receiptHeaders) {
  const headers = responseHeaders();
  headers.set('Content-Type', 'application/json');
  // If an origin redirects after settlement, retain any actual receipt for
  // reconciliation, while refusing to forward the redirect or create a receipt.
  for (const name of ['PAYMENT-REQUIRED', 'PAYMENT-RESPONSE', 'X-Payment-Required', 'X-Payment-Response']) {
    const value = receiptHeaders?.get(name); if (value) headers.set(name, value);
  }
  return new Response(JSON.stringify({ error: code, charged,
    ...(charged === 'unknown' ? { retry: 'Recover the original authorization and identical request; do not create another payment while settlement is uncertain.' } : {}) }), { status, headers });
}
async function requestBody(request) {
  if (!request.body) return undefined;
  const declared = request.headers.get('Content-Length');
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > MAX_REQUEST_BYTES)) throw new InputFailure('REQUEST_TOO_LARGE', 413);
  const encoding = request.headers.get('Content-Encoding');
  if (encoding && encoding.toLowerCase() !== 'identity') throw new InputFailure('UNSUPPORTED_CONTENT_ENCODING', 415);
  const reader = request.body.getReader();
  const chunks = []; let length = 0, timedOut = false;
  const timer = setTimeout(() => { timedOut = true; void reader.cancel().catch(() => {}); }, 15000);
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (timedOut) throw new InputFailure('REQUEST_BODY_TIMEOUT', 408);
      if (done) break;
      length += value.byteLength;
      if (length > MAX_REQUEST_BYTES) { await reader.cancel(); throw new InputFailure('REQUEST_TOO_LARGE', 413); }
      chunks.push(value);
    }
    const body = new Uint8Array(length); let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    return body;
  } finally { clearTimeout(timer); reader.releaseLock(); }
}

// Dependency injection is available to offline tests only; no client header,
// URL parameter, environment variable or Worker binding selects an upstream.
export function createGateway(fetcher = (...args) => fetch(...args)) {
  return async function gateway(request) {
    if (!METHODS.has(request.method)) return failure('METHOD_NOT_ALLOWED', 405);
    let body;
    try { body = await requestBody(request); }
    catch (error) { return failure(error instanceof InputFailure ? error.message : 'INVALID_REQUEST_BODY', error instanceof InputFailure ? error.status : 400); }
    const incoming = new URL(request.url);
    const upstream = new URL(CANONICAL_ORIGIN);
    // Assign path/search to a pre-created fixed URL. Resolving an arbitrary
    // relative string would let //attacker.example replace its authority.
    upstream.pathname = incoming.pathname;
    upstream.search = incoming.search;
    const headers = new Headers();
    for (const name of REQUEST_HEADERS) { const value = request.headers.get(name); if (value !== null) headers.set(name, value); }
    // Cookies, Authorization/admin credentials, forwarding/routing override
    // headers and Cloudflare access assertions are deliberately absent.
    headers.set('User-Agent', 'Pulse402-Canonical-Gateway/2.0');
    headers.set('Cache-Control', 'no-store'); headers.set('Pragma', 'no-cache');
    const potentiallyCharged = request.method === 'POST' || ['PAYMENT-SIGNATURE', 'X-PAYMENT', 'X-Pulse-Session'].some(name => request.headers.has(name));
    const requiredAtomic = ROUTE_MIN_ATOMIC[incoming.pathname];
    if (requiredAtomic) {
      const sigHeader = request.headers.get('PAYMENT-SIGNATURE') || request.headers.get('X-PAYMENT') || request.headers.get('X-Payment-Signature');
      if (sigHeader) {
        const offered = parsePaymentAmount(sigHeader);
        if (offered !== null && offered < requiredAtomic) {
          return failure('insufficient_payment_amount', 402);
        }
      }
    }
    try {
      // Cloudflare supports manual redirects (not Fetch's error mode).
      // Never retry a payment or relay its authorization to another origin.
      const result = await fetcher(upstream.href, {
        method: request.method, headers, ...(body === undefined ? {} : { body }),
        redirect: 'manual', cache: 'no-store', cf: { cacheTtlByStatus: { '100-599': -1 } },
        signal: AbortSignal.timeout(90000),
      });
      if (result.status >= 300 && result.status < 400) {
        void result.body?.cancel().catch(() => {});
        return failure('UPSTREAM_REDIRECT_REFUSED', 502, potentiallyCharged ? 'unknown' : false, result.headers);
      }
      // No JSON parsing/re-serialization, synthetic data, grant, free quota,
      // payment acceptance or fallback can change the origin's decision.
      return new Response(result.body, { status: result.status, statusText: result.statusText, headers: responseHeaders(result.headers) });
    } catch {
      return failure('CANONICAL_BACKEND_UNAVAILABLE', 502, potentiallyCharged ? 'unknown' : false);
    }
  };
}

export default { fetch: createGateway() };
