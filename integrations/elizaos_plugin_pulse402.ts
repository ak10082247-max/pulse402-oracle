/**
 * ElizaOS Plugin for Pulse402 Oracle.
 *
 * Provides ElizaOS autonomous agents with real-time market alpha and pre-trade
 * security filters over the x402 V2 protocol (Base L2 & Solana).
 */

export interface PluginConfig {
  baseUrl?: string;
  sessionToken?: string;
}

export const pulse402Plugin = {
  name: 'pulse402',
  description: 'Real-time Polymarket orderflow, Base DEX liquidity, GoPlus threat intel, and cyclic arbitrage oracle feeds.',
  actions: [
    {
      name: 'GET_POLYMARKET_VELOCITY',
      description: 'Fetch high-velocity probability deltas, volume spikes, and orderbook pressure on Polymarket.',
      validate: async (runtime: any, message: any) => true,
      handler: async (runtime: any, message: any, state: any, options: any, callback: any) => {
        const baseUrl = runtime.getSetting('PULSE402_BASE_URL') || 'https://pulse402-oracle.ak10082247.workers.dev';
        const sessionToken = runtime.getSetting('PULSE402_SESSION_TOKEN');

        const headers: Record<string, string> = { 'Accept': 'application/json' };
        let url = `${baseUrl}/v1/pulse/polymarket-velocity`;

        if (sessionToken) {
          headers['X-Pulse-Session'] = sessionToken;
        } else {
          // Fall back to unmetered preview if no session pass configured
          url = `${baseUrl}/v1/preview/polymarket-velocity`;
        }

        const res = await fetch(url, { headers });
        if (!res.ok) {
          callback({ text: `Failed to fetch Polymarket velocity: HTTP ${res.status}` });
          return false;
        }

        const data = await res.json();
        callback({
          text: `Polymarket Alpha Feed (Observed: ${new Date(data.timestamp_ms).toISOString()}):\n` +
                JSON.stringify(data.data, null, 2),
          content: data,
        });
        return true;
      },
    },
    {
      name: 'GET_BASE_LIQUIDITY',
      description: 'Fetch verified Base DEX pool depth, fee tiers, and liquidity distribution across Aerodrome and Uniswap V3.',
      validate: async (runtime: any, message: any) => true,
      handler: async (runtime: any, message: any, state: any, options: any, callback: any) => {
        const baseUrl = runtime.getSetting('PULSE402_BASE_URL') || 'https://pulse402-oracle.ak10082247.workers.dev';
        const sessionToken = runtime.getSetting('PULSE402_SESSION_TOKEN');

        const headers: Record<string, string> = { 'Accept': 'application/json' };
        let url = `${baseUrl}/v1/pulse/base-liquidity`;

        if (sessionToken) {
          headers['X-Pulse-Session'] = sessionToken;
        } else {
          url = `${baseUrl}/v1/preview/base-liquidity`;
        }

        const res = await fetch(url, { headers });
        if (!res.ok) {
          callback({ text: `Failed to fetch Base liquidity: HTTP ${res.status}` });
          return false;
        }

        const data = await res.json();
        callback({
          text: `Base DEX Liquidity Snapshot (10s TTL):\n` + JSON.stringify(data.data, null, 2),
          content: data,
        });
        return true;
      },
    },
    {
      name: 'CHECK_TOKEN_SECURITY',
      description: 'Check GoPlus threat intelligence and token risk ratings before executing a trade on Base.',
      validate: async (runtime: any, message: any) => true,
      handler: async (runtime: any, message: any, state: any, options: any, callback: any) => {
        const baseUrl = runtime.getSetting('PULSE402_BASE_URL') || 'https://pulse402-oracle.ak10082247.workers.dev';
        const sessionToken = runtime.getSetting('PULSE402_SESSION_TOKEN');

        const headers: Record<string, string> = { 'Accept': 'application/json' };
        let url = `${baseUrl}/v1/pulse/threat-intel`;

        if (sessionToken) {
          headers['X-Pulse-Session'] = sessionToken;
        } else {
          url = `${baseUrl}/v1/preview/threat-intel`;
        }

        const res = await fetch(url, { headers });
        if (!res.ok) {
          callback({ text: `Failed to fetch security intel: HTTP ${res.status}` });
          return false;
        }

        const data = await res.json();
        callback({
          text: `Pre-Trade Token Threat Intel:\n` + JSON.stringify(data.data, null, 2),
          content: data,
        });
        return true;
      },
    },
  ],
};

export default pulse402Plugin;
