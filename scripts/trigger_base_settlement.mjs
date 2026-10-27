import { x402Client } from "@x402/core/client";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { decodePaymentRequiredHeader, decodePaymentResponseHeader, encodePaymentSignatureHeader } from "@x402/core/http";
import { privateKeyToAccount } from "viem/accounts";
import fs from "node:fs";
import path from "node:path";

const BUYER_PRIVATE_KEY = process.env.BUYER_PRIVATE_KEY || "";
const account = privateKeyToAccount(BUYER_PRIVATE_KEY);
console.log("=============================================================");
console.log("BASE MAINNET EIP-3009 AUTOMATED SETTLEMENT RUNNER");
console.log("=============================================================");
console.log(`Buyer Account: ${account.address}`);

const GATEWAY_URL = "https://pulse402-oracle.ak10082247.workers.dev";
const ROUTES = [
  "/v1/pulse/base-liquidity",
  "/v1/pulse/polymarket-velocity",
  "/v1/pulse/threat-intel",
  "/v1/pulse/arb-routes"
];

const client = new x402Client().register("eip155:8453", new ExactEvmScheme(account));

async function settleRoute(pathUrl) {
  const url = `${GATEWAY_URL}${pathUrl}`;
  console.log(`\n-------------------------------------------------------------`);
  console.log(`Target Route: ${url}`);
  
  // 1. Fetch 402 challenge
  const chalRes = await fetch(url, { headers: { Accept: "application/json" } });
  if (chalRes.status !== 402) {
    console.log(`Expected 402, received ${chalRes.status}`);
    return { path: pathUrl, status: chalRes.status, success: false };
  }

  const encoded = chalRes.headers.get("PAYMENT-REQUIRED") || chalRes.headers.get("X-PAYMENT-REQUIRED");
  const required = decodePaymentRequiredHeader(encoded);
  const accept = required.accepts.find(a => a.network === "eip155:8453");
  if (!accept) throw new Error("No Base accept found in challenge");

  // 2. Sign EIP-712 authorization
  const payload = await client.createPaymentPayload({ ...required, accepts: [accept] });
  payload.resource = required.resource;
  if (required.extensions?.bazaar) {
    payload.extensions = { ...payload.extensions, bazaar: required.extensions.bazaar };
  }

  const sigHeader = encodePaymentSignatureHeader(payload);
  console.log("Submitting PAYMENT-SIGNATURE to PayAI Facilitator via Edge Gateway...");

  // 3. Paid call
  const paidRes = await fetch(url, {
    headers: {
      Accept: "application/json",
      "PAYMENT-SIGNATURE": sigHeader,
      "X-PAYMENT": sigHeader
    }
  });

  const resBody = await paidRes.text();
  console.log(`HTTP ${paidRes.status}`);

  if (paidRes.status === 200) {
    const receiptHdr = paidRes.headers.get("PAYMENT-RESPONSE") || paidRes.headers.get("X-PAYMENT-RESPONSE");
    let txHash = "";
    if (receiptHdr) {
      try {
        const decoded = decodePaymentResponseHeader(receiptHdr);
        txHash = decoded.transaction || decoded.txHash || "";
      } catch (e) {}
    }
    console.log(`🎉 SUCCESS! Base Mainnet Tx: ${txHash}`);
    console.log(`BaseScan: https://basescan.org/tx/${txHash}`);
    return { path: pathUrl, status: 200, success: true, txHash, basescan: `https://basescan.org/tx/${txHash}` };
  } else {
    console.log(`Payment failed: ${resBody.slice(0, 300)}`);
    return { path: pathUrl, status: paidRes.status, success: false, response: resBody };
  }
}

async function main() {
  const report = [];
  for (const r of ROUTES) {
    try {
      const res = await settleRoute(r);
      report.push(res);
    } catch (e) {
      console.error(`Error on ${r}:`, e.message);
      report.push({ path: r, success: false, error: e.message });
    }
  }

  console.log("\n=============================================================");
  console.log("SUMMARY REPORT");
  console.log("=============================================================");
  console.log(JSON.stringify(report, null, 2));

  fs.writeFileSync(
    path.resolve("scripts/base_settlement_report.json"),
    JSON.stringify({ timestamp: new Date().toISOString(), buyer: account.address, report }, null, 2)
  );
}

main().catch(console.error);
