/**
 * Pulse402 Oracle Tool Wrapper for Coinbase AgentKit & LangChain.
 *
 * Implements autonomous x402 V2 EIP-3009 payment negotiation on Base L2
 * and session-based High-Frequency Trading (HFT) pass consumption.
 *
 * Unit Economics:
 * - Tier 1: $0.005 USDC (/v1/pulse/base-liquidity, /v1/pulse/threat-intel)
 * - Tier 2: $0.010 USDC (/v1/pulse/polymarket-velocity, /v1/pulse/arb-routes)
 * - High-Frequency Pass: $5.00 USDC for 2,500 zero-latency calls ($0.002/call)
 */

export interface Pulse402Config {
  baseUrl?: string;
  sessionToken?: string;
  walletPrivateKey?: string; // Hex private key for Base EIP-3009 signing
  merchantAddress?: string;  // 0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6
  usdcContract?: string;     // 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
}

export interface OracleFeedResponse<T = any> {
  status: 'ok' | 'error';
  timestamp_ms: number;
  ttl_seconds: number;
  data: T;
  receipt?: {
    success: boolean;
    payer: string;
    transaction: string;
    amount: string;
    network: string;
  };
}

export class Pulse402AgentKitTool {
  private baseUrl: string;
  private sessionToken?: string;
  private walletPrivateKey?: string;
  private merchantAddress: string;
  private usdcContract: string;

  constructor(config: Pulse402Config = {}) {
    this.baseUrl = config.baseUrl || 'https://pulse402-oracle.ak10082247.workers.dev';
    this.sessionToken = config.sessionToken;
    this.walletPrivateKey = config.walletPrivateKey;
    this.merchantAddress = config.merchantAddress || '0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6';
    this.usdcContract = config.usdcContract || '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
  }

  /**
   * Sets or updates the active HFT session token.
   */
  public setSessionToken(token: string): void {
    this.sessionToken = token;
  }

  /**
   * Fetches free unmetered preview of market signals.
   */
  public async getPreview(feed: 'base-liquidity' | 'polymarket-velocity' | 'threat-intel' | 'arb-routes'): Promise<any> {
    const res = await fetch(`${this.baseUrl}/v1/preview/${feed}`, {
      headers: { 'Accept': 'application/json' },
    });
    if (!res.ok) throw new Error(`Preview failed with HTTP ${res.status}`);
    return await res.json();
  }

  /**
   * Consumes a feed using an active HFT Session Pass (zero on-chain latency).
   */
  public async fetchWithSessionPass<T = any>(route: string, queryParams: Record<string, string> = {}): Promise<OracleFeedResponse<T>> {
    if (!this.sessionToken) {
      throw new Error('Session token not set. Call acquireSessionPass() or pass sessionToken to constructor.');
    }

    const url = new URL(`${this.baseUrl}${route}`);
    for (const [k, v] of Object.entries(queryParams)) {
      url.searchParams.set(k, v);
    }

    const res = await fetch(url.toString(), {
      method: 'GET',
      headers: {
        'Accept': 'application/json',
        'X-Pulse-Session': this.sessionToken,
      },
    });

    if (res.status === 402) {
      throw new Error('Session pass expired or credit depleted. Purchase a new pass via acquireSessionPass().');
    }

    if (!res.ok) {
      throw new Error(`Feed request failed with status ${res.status}: ${await res.text()}`);
    }

    const remaining = res.headers.get('x-pulse-calls-remaining');
    const data = await res.json();
    return {
      ...data,
      callsRemaining: remaining ? parseInt(remaining, 10) : undefined,
    };
  }

  /**
   * Acquires a 2,500-call HFT Session Pass for $5.00 USDC via Base EIP-3009.
   */
  public async acquireSessionPass(): Promise<{ sessionToken: string; callsAllocated: number }> {
    const challengeRes = await fetch(`${this.baseUrl}/v1/pass/prepay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });

    if (challengeRes.status !== 402) {
      throw new Error(`Expected 402 challenge, received ${challengeRes.status}`);
    }

    const paymentRequiredHeader = challengeRes.headers.get('payment-required');
    if (!paymentRequiredHeader) {
      throw new Error('Missing PAYMENT-REQUIRED header from prepay challenge');
    }

    const quote = JSON.parse(atob(paymentRequiredHeader));
    const baseAccepted = quote.accepts.find((a: any) => a.network === 'eip155:8453');
    if (!baseAccepted) throw new Error('Base network not supported in prepay challenge');

    if (!this.walletPrivateKey) {
      throw new Error('walletPrivateKey is required to sign EIP-3009 prepay authorization');
    }

    // AgentKit / Viem / Ethers EIP-712 TransferWithAuthorization signing would occur here.
    // In production, instantiate your Viem WalletClient or AgentKit WalletProvider.
    const signedPayload = await this.signEip3009Authorization(baseAccepted, '5000000');

    const paymentSignature = btoa(JSON.stringify({
      x402Version: 2,
      resource: quote.resource,
      accepted: baseAccepted,
      payload: signedPayload,
    }));

    const settleRes = await fetch(`${this.baseUrl}/v1/pass/prepay`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'PAYMENT-SIGNATURE': paymentSignature,
      },
      body: JSON.stringify({}),
    });

    if (!settleRes.ok) {
      throw new Error(`Prepay session allocation failed with HTTP ${settleRes.status}: ${await settleRes.text()}`);
    }

    const sessionData = await settleRes.json();
    this.sessionToken = sessionData.session_token;
    return {
      sessionToken: sessionData.session_token,
      callsAllocated: sessionData.calls_allocated,
    };
  }

  private async signEip3009Authorization(accepted: any, amount: string): Promise<any> {
    // Scaffold: replace with real wallet.signTypedData in your AgentKit execution harness
    return {
      authorization: {
        from: '0x...',
        to: accepted.payTo,
        value: amount,
        validAfter: '0',
        validBefore: Math.floor(Date.now() / 1000 + 180).toString(),
        nonce: '0x' + Array.from(crypto.getRandomValues(new Uint8Array(32))).map(b => b.toString(16).padStart(2, '0')).join(''),
      },
      signature: '0x...',
    };
  }
}
